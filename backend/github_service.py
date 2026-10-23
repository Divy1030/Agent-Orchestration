"""Server-side GitHub authorization through the user's Clerk connection."""

from __future__ import annotations

import os
import base64
import json
import logging
import re
import time
from urllib.parse import urlparse

import httpx

from backend.secure_store import decrypt_secret, encrypt_secret

logger = logging.getLogger(__name__)


class GitHubAuthorizationError(Exception):
    def __init__(self, message: str, status_code: int = 503):
        super().__init__(message)
        self.status_code = status_code


def _repository_parts(repository_url: str) -> tuple[str, str]:
    parsed = urlparse(repository_url.strip())
    if parsed.scheme != "https" or parsed.netloc.lower() != "github.com" or parsed.query or parsed.fragment:
        raise GitHubAuthorizationError("Only GitHub repository URLs are supported", 422)
    parts = [part for part in parsed.path.split("/") if part]
    if len(parts) != 2:
        raise GitHubAuthorizationError("Use a repository URL such as https://github.com/org/repository", 422)
    return parts[0], parts[1].removesuffix(".git")


def _branch_name(value: str, field: str = "Branch name") -> str:
    branch = value.strip()
    if (not re.fullmatch(r"[A-Za-z0-9._/-]{1,120}", branch)
            or branch.startswith(("/", ".", "-")) or branch.endswith(("/", ".", ".lock"))
            or ".." in branch or "//" in branch or "@{" in branch):
        raise GitHubAuthorizationError(f"{field} is invalid", 422)
    return branch


def clerk_github_token(clerk_user_id: str) -> tuple[str, list[str]]:
    secret_key = os.getenv("CLERK_SECRET_KEY", "").strip()
    if not secret_key:
        raise GitHubAuthorizationError("Clerk is not configured on the server", 503)
    try:
        from clerk_backend_api import Clerk

        clerk = Clerk(bearer_auth=secret_key)
        # The installed Python SDK models this operation as a list. Clerk's
        # paginated response is an object, which causes that SDK version to
        # fail validation before it reaches our token extraction logic.
        response = clerk.users.get_o_auth_access_token(user_id=clerk_user_id, provider="github", paginated=False, limit=10)
        tokens = getattr(response, "data", response)
        if isinstance(tokens, dict):
            tokens = tokens.get("data", [])
        if tokens:
            token = tokens[0]
            value = getattr(token, "token", None) or (token.get("token") if isinstance(token, dict) else None)
            scopes = getattr(token, "scopes", None) or (token.get("scopes") if isinstance(token, dict) else None) or []
            if value:
                return value, list(scopes)
        raise GitHubAuthorizationError(
            "No GitHub account is connected to this Clerk user. Open the Clerk account menu, connect GitHub, then try again.", 409
        )
    except GitHubAuthorizationError:
        raise
    except Exception as exc:
        logger.exception("Clerk GitHub OAuth token lookup failed for user %s", clerk_user_id)
        raise GitHubAuthorizationError(
            "Clerk could not return a GitHub access token. Reconnect GitHub from the Clerk account menu and try again.", 502
        ) from exc


def verify_repository(repository_url: str, token: str) -> dict:
    owner, repository = _repository_parts(repository_url)
    try:
        response = httpx.get(
            f"https://api.github.com/repos/{owner}/{repository}",
            headers={"Authorization": f"Bearer {token}", "Accept": "application/vnd.github+json",
                     "X-GitHub-Api-Version": "2022-11-28"},
            timeout=10,
        )
    except httpx.HTTPError as exc:
        raise GitHubAuthorizationError("GitHub could not be reached", 503) from exc
    if response.status_code in {401, 403}:
        raise GitHubAuthorizationError("GitHub authorization is missing or does not have repository access", 403)
    if response.status_code == 404:
        raise GitHubAuthorizationError("GitHub repository was not found or is not accessible to this account", 404)
    if response.status_code >= 400:
        raise GitHubAuthorizationError("GitHub rejected the repository authorization", 502)
    data = response.json()
    permissions = data.get("permissions") or {}
    if permissions and not permissions.get("push"):
        raise GitHubAuthorizationError("GitHub authorization needs push access so agents can create branches and pull requests", 403)
    return {"full_name": data.get("full_name"), "private": bool(data.get("private")),
            "default_branch": data.get("default_branch") or "main", "permissions": permissions}


def github_app_install_url(state: str) -> str:
    slug = os.getenv("GITHUB_APP_SLUG", "").strip()
    if not slug:
        raise GitHubAuthorizationError("GitHub App is not configured. Set GITHUB_APP_SLUG on the backend.", 503)
    from urllib.parse import quote
    return f"https://github.com/apps/{quote(slug, safe='')}/installations/new?state={quote(state, safe='')}"


def github_app_state(user_id: str, space_id: str) -> str:
    payload = {"user_id": user_id, "space_id": space_id, "expires_at": int(time.time()) + 15 * 60}
    return encrypt_secret(json.dumps(payload, separators=(",", ":")))


def github_app_state_payload(state: str) -> dict:
    try:
        payload = json.loads(decrypt_secret(state))
    except (ValueError, TypeError, RuntimeError, json.JSONDecodeError) as exc:
        raise GitHubAuthorizationError("The GitHub authorization session is invalid or expired", 400) from exc
    if int(payload.get("expires_at", 0)) < int(time.time()):
        raise GitHubAuthorizationError("The GitHub authorization session is expired. Start again.", 400)
    if not payload.get("user_id") or not payload.get("space_id"):
        raise GitHubAuthorizationError("The GitHub authorization session is invalid", 400)
    return payload


def _github_app_jwt() -> str:
    app_id = os.getenv("GITHUB_APP_ID", "").strip()
    private_key = os.getenv("GITHUB_APP_PRIVATE_KEY", "").strip().replace("\\n", "\n")
    if not app_id or not private_key:
        raise GitHubAuthorizationError("GitHub App is not configured. Set GITHUB_APP_ID and GITHUB_APP_PRIVATE_KEY on the backend.", 503)
    try:
        from cryptography.hazmat.primitives import hashes, serialization
        from cryptography.hazmat.primitives.asymmetric import padding
        key = serialization.load_pem_private_key(private_key.encode("utf-8"), password=None)
        header = {"alg": "RS256", "typ": "JWT"}
        claims = {"iat": int(time.time()) - 60, "exp": int(time.time()) + 9 * 60, "iss": app_id}
        encode = lambda value: base64.urlsafe_b64encode(json.dumps(value, separators=(",", ":")).encode()).rstrip(b"=").decode()
        unsigned = f"{encode(header)}.{encode(claims)}"
        signature = key.sign(unsigned.encode("ascii"), padding.PKCS1v15(), hashes.SHA256())
        return f"{unsigned}.{base64.urlsafe_b64encode(signature).rstrip(b'=').decode()}"
    except (ValueError, TypeError, AttributeError) as exc:
        raise GitHubAuthorizationError("GITHUB_APP_PRIVATE_KEY is not a valid PEM private key", 503) from exc


def github_app_installation_token(installation_id: str, repository_url: str) -> tuple[str, dict]:
    owner, repository = _repository_parts(repository_url)
    try:
        response = httpx.post(
            f"https://api.github.com/app/installations/{installation_id}/access_tokens",
            headers={"Authorization": f"Bearer {_github_app_jwt()}", "Accept": "application/vnd.github+json",
                     "X-GitHub-Api-Version": "2022-11-28"},
            json={"repositories": [repository]}, timeout=15,
        )
    except httpx.HTTPError as exc:
        raise GitHubAuthorizationError("GitHub could not be reached", 503) from exc
    if response.status_code in {401, 403}:
        raise GitHubAuthorizationError("GitHub rejected the GitHub App installation", 403)
    if response.status_code == 404:
        raise GitHubAuthorizationError("The GitHub App installation was not found", 404)
    if response.status_code >= 400:
        raise GitHubAuthorizationError("GitHub rejected the GitHub App installation", 502)
    token = response.json().get("token")
    if not token:
        raise GitHubAuthorizationError("GitHub did not return an installation token", 502)
    repository_info = verify_repository(f"https://github.com/{owner}/{repository}", token)
    return token, repository_info


def _github_request(token: str, method: str, path: str, **kwargs) -> dict:
    headers = {"Authorization": f"Bearer {token}", "Accept": "application/vnd.github+json",
               "X-GitHub-Api-Version": "2022-11-28"}
    try:
        response = httpx.request(method, f"https://api.github.com{path}", headers=headers, timeout=20, **kwargs)
    except httpx.HTTPError as exc:
        raise GitHubAuthorizationError("GitHub could not be reached", 503) from exc
    if response.status_code >= 400:
        detail = response.json().get("message", "GitHub rejected the request") if response.content else "GitHub rejected the request"
        status = 403 if response.status_code in {401, 403} else (404 if response.status_code == 404 else 502)
        raise GitHubAuthorizationError(detail, status)
    return response.json() if response.content else {}


def repository_snapshot(repository_url: str, token: str, branch: str | None = None) -> dict:
    owner, repository = _repository_parts(repository_url)
    repo = _github_request(token, "GET", f"/repos/{owner}/{repository}")
    selected_branch = branch or repo.get("default_branch") or "main"
    ref = _github_request(token, "GET", f"/repos/{owner}/{repository}/git/ref/heads/{selected_branch}")
    commit_sha = ref.get("object", {}).get("sha")
    tree = _github_request(token, "GET", f"/repos/{owner}/{repository}/git/trees/{commit_sha}", params={"recursive": "1"})
    commits = _github_request(token, "GET", f"/repos/{owner}/{repository}/commits", params={"sha": selected_branch, "per_page": 10})
    return {"full_name": repo.get("full_name"), "private": bool(repo.get("private")),
            "default_branch": repo.get("default_branch") or "main", "branch": selected_branch,
            "branch_sha": commit_sha, "tree": [{"path": item.get("path"), "type": item.get("type"), "size": item.get("size", 0)}
                                                for item in tree.get("tree", []) if item.get("type") in {"blob", "tree"}],
            "commits": [{"sha": item.get("sha"), "message": (item.get("commit", {}).get("message") or "").splitlines()[0],
                         "author": (item.get("commit", {}).get("author") or {}).get("name"),
                         "date": (item.get("commit", {}).get("author") or {}).get("date")} for item in commits]}


def repository_branches(repository_url: str, token: str) -> list[str]:
    owner, repository = _repository_parts(repository_url)
    branches = _github_request(token, "GET", f"/repos/{owner}/{repository}/branches", params={"per_page": 100})
    return [str(item.get("name")) for item in branches if item.get("name")]


def read_file(repository_url: str, token: str, path: str, branch: str | None = None) -> dict:
    clean_path = path.strip().lstrip("/")
    if not clean_path or ".." in clean_path.split("/") or clean_path.startswith(".git/"):
        raise GitHubAuthorizationError("File path is invalid", 422)
    owner, repository = _repository_parts(repository_url)
    data = _github_request(token, "GET", f"/repos/{owner}/{repository}/contents/{clean_path}", params={"ref": branch} if branch else {})
    if data.get("type") != "file" or not data.get("content"):
        raise GitHubAuthorizationError("The selected path is not a readable file", 422)
    try:
        content = base64.b64decode(data["content"].encode("ascii")).decode("utf-8")
    except (ValueError, UnicodeDecodeError) as exc:
        raise GitHubAuthorizationError("Only UTF-8 text files can be opened in the editor", 422) from exc
    if len(content.encode("utf-8")) > 250_000:
        raise GitHubAuthorizationError("This file is larger than the 250 KB editor limit", 422)
    return {"path": clean_path, "content": content, "sha": data.get("sha"), "branch": branch}


def create_branch(repository_url: str, token: str, branch: str, from_branch: str, reuse_existing: bool = False) -> dict:
    branch = _branch_name(branch)
    from_branch = _branch_name(from_branch, "Source branch name")
    owner, repository = _repository_parts(repository_url)
    source = _github_request(token, "GET", f"/repos/{owner}/{repository}/git/ref/heads/{from_branch}")
    sha = source.get("object", {}).get("sha")
    try:
        created = _github_request(token, "POST", f"/repos/{owner}/{repository}/git/refs", json={"ref": f"refs/heads/{branch}", "sha": sha})
        return {"branch": branch, "sha": created.get("object", {}).get("sha")}
    except GitHubAuthorizationError as exc:
        # Task branches are deterministic (`agent/task-<id>`) so a retry can
        # safely resume the same branch after a worker timeout or page reload.
        # Manual branch creation remains strict unless the caller opts in.
        if not reuse_existing or str(exc) != "Reference already exists":
            raise
        existing = _github_request(token, "GET", f"/repos/{owner}/{repository}/git/ref/heads/{branch}")
        return {"branch": branch, "sha": existing.get("object", {}).get("sha"), "reused": True}


def ensure_branch_is_current(repository_url: str, token: str, branch: str, base_branch: str) -> None:
    """Reject commits from a branch that has fallen behind the configured base branch."""
    branch = _branch_name(branch)
    base_branch = _branch_name(base_branch, "Base branch name")
    if branch == base_branch:
        return
    owner, repository = _repository_parts(repository_url)
    comparison = _github_request(token, "GET", f"/repos/{owner}/{repository}/compare/{base_branch}...{branch}")
    if comparison.get("status") == "behind" or int(comparison.get("behind_by") or 0) > 0:
        raise GitHubAuthorizationError("Pull the latest default branch into this branch before committing", 409)


def commit_files(repository_url: str, token: str, branch: str, message: str, files: list[dict]) -> dict:
    if not message.strip() or len(message) > 200:
        raise GitHubAuthorizationError("Commit message must be between 1 and 200 characters", 422)
    if not files or len(files) > 20:
        raise GitHubAuthorizationError("A commit must contain between 1 and 20 files", 422)
    total_bytes = 0
    normalized: list[dict] = []
    for item in files:
        path = str(item.get("path", "")).strip().lstrip("/")
        content = item.get("content")
        if not path or ".." in path.split("/") or path.startswith(".git/") or not isinstance(content, str):
            raise GitHubAuthorizationError("Each file needs a safe path and text content", 422)
        encoded = content.encode("utf-8")
        total_bytes += len(encoded)
        if len(encoded) > 250_000:
            raise GitHubAuthorizationError("Each file is limited to 250 KB", 422)
        normalized.append({"path": path, "content": encoded})
    if total_bytes > 1_000_000:
        raise GitHubAuthorizationError("A commit is limited to 1 MB of text", 422)
    branch = _branch_name(branch)
    owner, repository = _repository_parts(repository_url)
    ref = _github_request(token, "GET", f"/repos/{owner}/{repository}/git/ref/heads/{branch}")
    parent_sha = ref.get("object", {}).get("sha")
    parent = _github_request(token, "GET", f"/repos/{owner}/{repository}/git/commits/{parent_sha}")
    blobs = []
    for item in normalized:
        blob = _github_request(token, "POST", f"/repos/{owner}/{repository}/git/blobs",
                               json={"content": base64.b64encode(item["content"]).decode("ascii"), "encoding": "base64"})
        blobs.append({"path": item["path"], "mode": "100644", "type": "blob", "sha": blob.get("sha")})
    tree = _github_request(token, "POST", f"/repos/{owner}/{repository}/git/trees",
                           json={"base_tree": parent.get("tree", {}).get("sha"), "tree": blobs})
    commit = _github_request(token, "POST", f"/repos/{owner}/{repository}/git/commits",
                             json={"message": message.strip(), "tree": tree.get("sha"), "parents": [parent_sha]})
    _github_request(token, "PATCH", f"/repos/{owner}/{repository}/git/refs/heads/{branch}",
                    json={"sha": commit.get("sha"), "force": False})
    return {"branch": branch, "commit_sha": commit.get("sha"), "files": [item["path"] for item in normalized]}


def create_pull_request(repository_url: str, token: str, title: str, body: str, head: str, base: str) -> dict:
    if not title.strip() or len(title) > 256:
        raise GitHubAuthorizationError("Pull request title must be between 1 and 256 characters", 422)
    head = _branch_name(head, "Head branch name")
    base = _branch_name(base, "Base branch name")
    owner, repository = _repository_parts(repository_url)
    pr = _github_request(token, "POST", f"/repos/{owner}/{repository}/pulls",
                         json={"title": title.strip(), "body": body[:10000], "head": head, "base": base})
    return {"number": pr.get("number"), "url": pr.get("html_url"), "title": pr.get("title"), "state": pr.get("state")}
