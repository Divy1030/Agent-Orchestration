from __future__ import annotations

import os
import asyncio
from pathlib import Path
from copy import deepcopy
from typing import Any

import httpx
from dotenv import load_dotenv
from fastapi import BackgroundTasks, FastAPI, HTTPException, Request, Response
from fastapi.responses import RedirectResponse
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field
from backend.flow_schema import DEMO_FLOW, FlowValidationError, canvas_graph, validate_flow
from backend.runtime import FlowRuntime, SessionStore
from backend.agent_executor import OpenRouterExecutor
from backend.tool_registry import execute_tool, list_tools
from backend.mcp_tools import McpConnectionError, call_mcp_tool, discover_mcp_tools, expand_mcp_servers
from backend.auth_models import (FlowSaveRequest, GitHubBranchRequest, GitHubCommitRequest, GitHubPullRequestRequest,
                                  LoginRequest, PublicChatRequest, RegisterRequest, ResourceRequest, SessionCreateRequest,
                                  SpaceContextRequest,
                                  SpaceMemoryRequest, SpaceSkillRequest, SpaceTaskRequest, SpaceTaskStatusRequest,
                                  SpaceTaskBranchRequest, SpaceTaskPullRequestRequest, SpaceTaskApplyProposalRequest, SpaceApprovalRequest,
                                  SpaceTaskRejectProposalRequest, SpaceTaskBatchRequest, SpaceTaskAdvanceRequest, SpaceTaskUpdateRequest,
                                  SpaceAgentRequest, SpaceAgentRoleRequest, SpaceJoinCodeRequest, SpaceJoinRequest, SpaceMemberRoleRequest,
                                  SpaceProjectRequest, SpaceProjectSettingsRequest, WorkspaceRequest)
from backend.mongo_store import MongoRuntimeStore, MongoStore, now as mongo_store_now, verify_password
from backend.clerk_auth import ClerkAuthError, clerk_user_from_request
from backend.secure_store import decrypt_secret, encrypt_secret
from backend.github_service import (GitHubAuthorizationError, clerk_github_token, commit_files, create_branch, ensure_branch_is_current,
                                    create_pull_request, github_app_install_url, github_app_installation_token, repository_branches,
                                    github_app_state, github_app_state_payload, read_file, repository_snapshot,
                                    verify_repository)

load_dotenv(Path(__file__).resolve().parents[1] / ".env")
load_dotenv(Path(__file__).resolve().parent / ".env", override=False)

app = FastAPI(title="Agent Orchestration Base", version="0.1.0")


def cors_origins() -> list[str]:
    """Return an explicit allow-list; never reflect arbitrary credentialed origins."""
    configured = os.getenv("CORS_ALLOWED_ORIGINS", "")
    configured += "," + os.getenv("FRONTEND_URL", "")
    origins = {origin.strip().rstrip("/") for origin in configured.split(",") if origin.strip()}
    if not origins:
        origins = {"http://localhost:5173", "http://127.0.0.1:5173"}
    return sorted(origins)


app.add_middleware(CORSMiddleware, allow_origins=cors_origins(), allow_credentials=True,
                   allow_methods=["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"],
                   allow_headers=["Authorization", "Content-Type"])

FLOW = validate_flow(DEMO_FLOW)
GRAPH = canvas_graph(FLOW)
mongo = MongoStore()
openrouter_executor = OpenRouterExecutor()


def auth_cookie_options() -> dict[str, Any]:
    return {
        "httponly": True,
        "secure": os.getenv("AUTH_COOKIE_SECURE", "false").lower() == "true",
        "samesite": os.getenv("AUTH_COOKIE_SAMESITE", "lax").lower(),
        "max_age": 7 * 86400,
    }


async def execute_agent(node: dict[str, Any], messages: list[dict[str, str]]) -> dict[str, Any]:
    config = dict(node.get("config", {}))
    agent_id = config.get("agent_id")
    if agent_id:
        from bson import ObjectId
        agent = mongo.db.agents.find_one({"_id": ObjectId(agent_id)})
        if not agent:
            raise ValueError("Connected agent was not found")
        # Agent documents are the source of truth. Keep only the node fallback
        # reply locally so a disconnected/renamed agent cannot silently drift.
        node_guidance = {key: config[key] for key in ("workflow_instructions", "routing_instructions", "instructions", "tools", "model", "temperature", "max_tokens") if key in config}
        config = {**agent.get("config", {}), **node_guidance,
                  "reply": config.get("reply", agent.get("config", {}).get("reply", ""))}
    attached_tools = [tool for tool in config.get("tools", []) if isinstance(tool, dict)]
    mcp_servers = [tool for tool in attached_tools if tool.get("tool_id") == "mcp"]
    mcp_tools = expand_mcp_servers(mcp_servers)
    attached_tools = [tool for tool in attached_tools if tool.get("tool_id") != "mcp"]
    approvals_enabled = os.getenv("ORCHESTRATION_TOOL_APPROVALS", "true").lower() == "true"
    approval_required = [tool for tool in mcp_tools if tool.get("approval_required", True)] if approvals_enabled else []
    live_mode = os.getenv("ORCHESTRATION_TOOL_MODE", "dry_run").lower() == "live"
    runnable_mcp_tools = [tool for tool in mcp_tools if tool not in approval_required] if live_mode else []
    blocked_mcp_tools = approval_required if live_mode else mcp_tools
    mcp_status = "awaiting_approval" if live_mode else "dry_run"
    mcp_reason = "This MCP tool requires approval before it can run." if live_mode else "MCP execution is disabled while ORCHESTRATION_TOOL_MODE is not live."
    config["tool_results"] = [{"status": mcp_status, "server": tool.get("mcp_server_name") or tool.get("name") or "MCP Server",
                               "function": tool.get("mcp_tool_name") or tool.get("name"), "reason": mcp_reason}
                              for tool in blocked_mcp_tools]
    mcp_tools = runnable_mcp_tools
    selected_tools = await select_attached_tools(attached_tools, messages)
    tool_results = []
    for attached_tool in selected_tools:
        tool_node = {"id": f"{node.get('id', 'agent')}-{attached_tool.get('tool_id', 'tool')}",
                     "type": "tool", "label": attached_tool.get("label", attached_tool.get("tool_id", "Tool")),
                     "config": attached_tool}
        tool_results.append(await execute_tool(tool_node, messages))
    config["tool_results"].extend(tool_results)
    workspace_id = node.get("workspace_id") or config.get("workspace_id")
    workspace_key = None
    if workspace_id:
        from bson import ObjectId
        row = mongo.db.workspace_credentials.find_one({"workspace_id": ObjectId(workspace_id), "provider": "openrouter"}) if mongo.configured else None
        if not row and mongo.configured and agent_id:
            agent_row = mongo.db.agents.find_one({"_id": ObjectId(agent_id)}, {"workspace_id": 1})
            agent_workspace_id = (agent_row or {}).get("workspace_id")
            if agent_workspace_id and str(agent_workspace_id) != str(ObjectId(workspace_id)):
                row = mongo.db.workspace_credentials.find_one({"workspace_id": agent_workspace_id, "provider": "openrouter"})
        if row:
            workspace_key = decrypt_secret(row["encrypted_value"]).strip()
        if not workspace_key:
            raise RuntimeError("Workspace OpenRouter key is not configured. Add and test the encrypted key in Settings before running this workflow.")
        assignment = mongo.db.space_agents.find_one({"workspace_id": __import__("bson").ObjectId(workspace_id), "agent_id": __import__("bson").ObjectId(agent_id)}) if mongo.configured and agent_id else None
        if assignment:
            config["project_role"] = assignment.get("role", "coder")
        project_context = mongo.db.space_contexts.find_one({"workspace_id": __import__("bson").ObjectId(workspace_id)}, {"_id": 0}) if mongo.configured else None
        if project_context:
            config["project_context"] = {key: project_context.get(key, []) for key in ("project_goal", "guidelines", "patterns", "instructions")}
        if mongo.configured:
            config["shared_project_context"] = mongo.shared_agent_context(str(workspace_id))
    if workspace_id and agent_id and mongo.configured:
        mongo.set_agent_presence(str(workspace_id), str(agent_id), "working")
        mongo.record_space_activity(str(workspace_id), "agent_started", "Agent started working on a workflow", str(agent_id))
    try:
        return await openrouter_executor.execute({**node, "config": config}, messages,
                                                 mcp_tools=mcp_tools, mcp_tool_caller=call_mcp_tool,
                                                 api_key=workspace_key)
    finally:
        if workspace_id and agent_id and mongo.configured:
            mongo.set_agent_presence(str(workspace_id), str(agent_id), "idle")
            mongo.record_space_activity(str(workspace_id), "agent_completed", "Agent finished its workflow turn", str(agent_id))


async def run_space_task(space_id: str, task_id: str, user_id: str) -> None:
    """Run one claimed Space task and leave it in review for human/PR handoff."""
    try:
        from bson import ObjectId
        task = mongo.db.space_tasks.find_one({"_id": ObjectId(task_id), "workspace_id": ObjectId(space_id)})
        if not task or not task.get("assignee_agent_id"):
            raise ValueError("The task has no assigned agent")
        assignment = mongo.db.space_agents.find_one({"workspace_id": ObjectId(space_id), "agent_id": task["assignee_agent_id"]})
        role = (assignment or {}).get("role", "coder")
        branch = (task.get("execution") or {}).get("branch")
        try:
            token, project = mongo.github_token_for_space(user_id, space_id)
            if not branch:
                branch = f"agent/task-{task_id[:8]}"
                branch_result = create_branch(project["repository_url"], token, branch, project.get("default_branch") or "main")
                mongo.set_space_task_branch(space_id, task_id, branch_result["branch"], branch_result.get("sha"))
        except (GitHubAuthorizationError, PermissionError, ValueError):
            if role in {"coder", "tester", "reviewer", "security"}:
                raise
        mongo.record_space_task_event(space_id, task_id, "started", f"{role.title()} agent started", branch)
        mongo.update_space_task_phase(space_id, task_id, "reading_repository")
        shared_context = mongo.shared_agent_context(space_id)
        repository_context = await build_task_repository_context(user_id, space_id, task, branch)
        dependency_artifacts = mongo.dependency_artifacts(space_id, task)
        artifact_context = "\n\n".join(f"[{item.get('artifact_type', 'artifact')}]\n{item.get('content', '')[:60000]}" for item in dependency_artifacts) or "No predecessor artifacts are available."
        prompt = f"""You are the assigned {role} agent for a collaborative software project.

Task title: {task.get('title', '')}
Task description: {task.get('description', '') or 'No additional description was provided.'}
Priority: {task.get('priority', 'normal')}

Use the shared project memory, skills, patterns, open tasks, repository snapshot, and predecessor artifacts below as context. The repository snapshot is pinned to one commit SHA; do not assume files changed outside it. Do not claim that code was changed, committed, or pushed. For planning tasks, produce a structured plan with files, reasons, dependencies, acceptance criteria, and verification. For implementation tasks, when you can safely specify the exact text, include proposed file changes in this exact JSON shape inside a fenced json block: {{"summary":"...","files":[{{"path":"safe/relative/path","content":"complete UTF-8 file content"}}],"verification":["..."]}}. For test or review tasks, report concrete findings and verification results. Only include complete files you are confident should change; never include secrets, credentials, binary data, or paths outside the repository.

Shared context:
{shared_context}

Repository context (server-fetched, bounded, and pinned):
{repository_context}

Artifacts produced by predecessor tasks:
{artifact_context}
"""
        mongo.update_space_task_phase(space_id, task_id, "calling_model")
        mongo.record_space_task_event(space_id, task_id, "context_ready", "Repository context and predecessor artifacts loaded")
        result = await execute_agent({"type": "agent", "workspace_id": space_id,
                                      "config": {"agent_id": str(task["assignee_agent_id"]),
                                                 "workspace_id": space_id, "project_role": role,
                                                 "max_tokens": 12000, "temperature": 0.1}},
                                     [{"role": "user", "content": prompt}])
        reply = result.get("reply", "")
        mongo.update_space_task_phase(space_id, task_id, "saving_result")
        proposal = extract_agent_proposal(reply)
        proposed_files = [item.get("path") for item in (proposal or {}).get("files", []) if item.get("path")]
        mongo.record_space_task_event(space_id, task_id, "proposal_ready", f"Agent produced {len(proposed_files)} proposed file change(s)", files=proposed_files)
        output_status = "complete" if proposal or role in {"planner", "tester", "reviewer"} else "unstructured"
        mongo.finish_space_task(space_id, task_id, {"reply": reply, "provider": result.get("provider"),
                                                     "model": result.get("model"), "role": role,
                                                     "output_status": output_status})
        if proposal:
            from bson import ObjectId
            mongo.db.space_tasks.update_one({"_id": ObjectId(task_id), "workspace_id": ObjectId(space_id)},
                                            {"$set": {"execution.proposed_changes": proposal}})
            if role in {"coder", "fix"} and branch:
                token, project = mongo.github_token_for_space(user_id, space_id)
                mongo.record_space_task_event(space_id, task_id, "writing_branch", "Writing proposed changes to isolated task branch", files=proposed_files)
                commit = commit_files(project["repository_url"], token, branch, f"Agent changes: {task.get('title', 'task')}", proposal["files"])
                mongo.set_space_task_commit(space_id, task_id, commit)
                mongo.record_space_task_event(space_id, task_id, "branch_updated", "Isolated task branch updated", files=proposed_files)
        tests = []
        if role == "tester":
            tests = [{"name": "Agent verification", "status": "reported", "details": reply[:1000]}]
            mongo.record_space_task_event(space_id, task_id, "tests_reported", "Tester reported verification results", tests=tests)
        artifact_type = "plan" if role == "planner" or task.get("task_type") == "plan" else ("test" if role == "tester" or task.get("task_type") == "test" else ("review" if role == "reviewer" or task.get("task_type") == "review" else "implementation"))
        mongo.add_space_task_artifact(space_id, task_id, artifact_type, reply,
                                      {"role": role, "branch": repository_context.get("branch"), "branch_sha": repository_context.get("branch_sha"),
                                       "files_read": repository_context.get("files_read", [])}, user_id)
        project_settings = mongo.space_project_settings(user_id, space_id) if mongo.space_detail(user_id, space_id).get("role") == "owner" else None
        if project_settings is None:
            from bson import ObjectId
            project = mongo.db.space_projects.find_one({"workspace_id": ObjectId(space_id)}, {"git_settings": 1}) or {}
            project_settings = {"auto_proceed": (project.get("git_settings") or {}).get("auto_proceed", True)}
        if project_settings.get("auto_proceed", True):
            try:
                next_task = mongo.advance_space_task(user_id, space_id, task_id, "auto", system=True)
                mongo.claim_space_task(user_id, space_id, next_task["id"])
                await run_space_task(space_id, next_task["id"], user_id)
            except ValueError:
                pass
    except Exception as exc:
        mongo.fail_space_task(space_id, task_id, f"{type(exc).__name__}: {exc}")


async def build_task_repository_context(user_id: str, space_id: str, task: dict[str, Any], task_branch: str | None = None) -> dict[str, Any]:
    """Build a bounded, secret-free repository context for one agent turn."""
    try:
        token, project = mongo.github_token_for_space(user_id, space_id)
        snapshot = repository_snapshot(project["repository_url"], token, task_branch or project.get("default_branch") or "main")
    except (GitHubAuthorizationError, PermissionError, ValueError, RuntimeError):
        return {"available": False, "reason": "No authorized GitHub repository is available for this Space."}
    tree = snapshot.get("tree", [])
    task_text = f"{task.get('title', '')} {task.get('description', '')}".lower()
    important = {"readme.md", "package.json", "pyproject.toml", "requirements.txt", "go.mod", "cargo.toml", "tsconfig.json", "next.config.js", "next.config.mjs", "vite.config.ts"}
    candidates = [item for item in tree if item.get("type") == "blob" and isinstance(item.get("path"), str)
                  and not item["path"].startswith((".git/", ".env", "node_modules/"))
                  and not item["path"].endswith((".lock", ".map"))]
    def score(item: dict[str, Any]) -> tuple[int, str]:
        path = item["path"]
        lower = path.lower()
        name = lower.rsplit("/", 1)[-1]
        points = 100 if name in important else 0
        points += sum(8 for word in task_text.split() if len(word) > 3 and word in lower)
        points += 4 if lower.startswith(("src/", "app/", "api/", "backend/", "frontend/")) else 0
        return (-points, path)
    selected = sorted(candidates, key=score)[:12]
    files, total_bytes = [], 0
    for item in selected:
        try:
            opened = read_file(project["repository_url"], token, item["path"], snapshot.get("branch"))
        except GitHubAuthorizationError:
            continue
        content = opened.get("content", "")
        size = len(content.encode("utf-8"))
        if size > 40_000 or total_bytes + size > 220_000:
            continue
        total_bytes += size
        files.append({"path": item["path"], "content": content})
    return {"available": True, "repository": snapshot.get("full_name"), "branch": snapshot.get("branch"),
            "branch_sha": snapshot.get("branch_sha"), "tree": snapshot.get("tree", [])[:250],
            "recent_commits": snapshot.get("commits", [])[:10], "files": files,
            "files_read": [file["path"] for file in files]}


async def run_space_task_batch(space_id: str, user_id: str, mode: str) -> None:
    """Run dependency-ready tasks in waves; each task remains independently observable."""
    while True:
        ready = mongo.ready_space_tasks(user_id, space_id)
        if not ready:
            return
        if mode == "sequential":
            task = ready[0]
            try:
                mongo.claim_space_task(user_id, space_id, task["id"])
            except (PermissionError, ValueError):
                return
            await run_space_task(space_id, task["id"], user_id)
            latest = mongo.db.space_tasks.find_one({"_id": __import__("bson").ObjectId(task["id"]), "workspace_id": __import__("bson").ObjectId(space_id)}, {"execution.status": 1}) or {}
            if (latest.get("execution") or {}).get("status") == "failed":
                return
            continue
        claimed = []
        for task in ready:
            try:
                mongo.claim_space_task(user_id, space_id, task["id"])
                claimed.append(task["id"])
            except (PermissionError, ValueError):
                continue
        if not claimed:
            return
        await asyncio.gather(*(run_space_task(space_id, task_id, user_id) for task_id in claimed))
        failed = mongo.db.space_tasks.count_documents({"_id": {"$in": [__import__("bson").ObjectId(task_id) for task_id in claimed]}, "execution.status": "failed"})
        if failed:
            return


def extract_agent_proposal(reply: str) -> dict[str, Any] | None:
    """Accept only a bounded, repository-relative proposal from the agent output."""
    import json
    import re
    match = re.search(r"```json\s*(\{.*?\})\s*```", reply or "", re.DOTALL | re.IGNORECASE)
    if not match:
        return None
    try:
        payload = json.loads(match.group(1))
    except json.JSONDecodeError:
        return None
    files = payload.get("files") if isinstance(payload, dict) else None
    if not isinstance(files, list) or not files or len(files) > 20:
        return None
    clean_files = []
    total_bytes = 0
    for item in files:
        path = str(item.get("path", "")).strip().lstrip("/") if isinstance(item, dict) else ""
        content = item.get("content") if isinstance(item, dict) else None
        if not path or ".." in path.split("/") or path.startswith(".git/") or not isinstance(content, str):
            return None
        total_bytes += len(content.encode("utf-8"))
        if len(content.encode("utf-8")) > 250_000 or total_bytes > 1_000_000:
            return None
        clean_files.append({"path": path, "content": content})
    return {"summary": str(payload.get("summary", ""))[:2000], "files": clean_files, "status": "pending",
            "verification": [str(item)[:500] for item in payload.get("verification", [])[:20]] if isinstance(payload.get("verification", []), list) else []}


def _tool_matches_message(tool: dict[str, Any], message: str) -> bool:
    text = message.lower()
    identity = f"{tool.get('tool_id', '')} {tool.get('label', '')} {tool.get('name', '')} {tool.get('description', '')}".lower()
    aliases = {
        "weather": ("weather", "forecast", "temperature", "rain", "wind", "climate"),
        "stock": ("stock", "share price", "market", "ticker", "equity"),
        "calendar": ("calendar", "meeting", "appointment", "availability", "event"),
        "email": ("email", "mail", "inbox", "gmail", "send a message"),
        "sheet": ("spreadsheet", "sheet", "row", "column", "cell"),
    }
    for key, words in aliases.items():
        if key in identity and any(word in text for word in words):
            return True
    return any(token in text for token in identity.split() if len(token) > 3)


async def select_attached_tools(tools: list[dict[str, Any]], messages: list[dict[str, str]]) -> list[dict[str, Any]]:
    """Use Jev to select one relevant attached tool when a node has several."""
    if len(tools) <= 1:
        return tools
    latest = next((item.get("content", "") for item in reversed(messages) if item.get("role") == "user"), "")
    if os.getenv("JEV_MOCK", "true").lower() == "true":
        return [tool for tool in tools if _tool_matches_message(tool, latest)]
    api_key = os.getenv("JEV_API_KEY") or os.getenv("TYPESAFE_API_KEY") or os.getenv("SYSTEM_ONE_API_KEY")
    if not api_key:
        return [tool for tool in tools if _tool_matches_message(tool, latest)]
    choices = {f"tool_{index}": tool for index, tool in enumerate(tools)}
    choices["none"] = None
    payload = {
        "model": os.getenv("JEV_MODEL", "jev-latest"),
        "state": {"latest_message": latest, "available_tools": [
            {"id": key, "name": tool.get("name", tool.get("label", tool.get("tool_id"))), "description": tool.get("description", "")}
            for key, tool in choices.items() if tool
        ]},
        "questions": {"tool": {"type": "choice", "instructions": "Choose the single attached tool that best answers the latest user request, or none if no tool is relevant.", "criteria": {
            key: (tool.get("description") or tool.get("name") or tool.get("label") or tool.get("tool_id", "")) for key, tool in choices.items()
        }}},
    }
    try:
        async with httpx.AsyncClient(timeout=httpx.Timeout(8, connect=3)) as client:
            response = await client.post(jev_url(), headers={"Authorization": f"Bearer {api_key}"}, json=payload)
        response.raise_for_status()
        choice = response.json().get("answers", {}).get("tool", {}).get("choice")
        return [choices[choice]] if choice in choices and choices[choice] else []
    except Exception:
        return [tool for tool in tools if _tool_matches_message(tool, latest)]


runtime = FlowRuntime(MongoRuntimeStore(mongo) if mongo.configured else SessionStore(os.getenv("ORCHESTRATION_DB_PATH", "data/orchestration.db")), agent_executor=execute_agent, tool_executor=execute_tool)


class RunRequest(BaseModel):
    message: str = Field(min_length=1, max_length=4000)
    node: str | None = None
    history: list[dict[str, str]] = Field(default_factory=list)


class McpDiscoverRequest(BaseModel):
    url: str = Field(min_length=1, max_length=2000)


class ValidateRequest(BaseModel):
    flow: dict[str, Any]


class MessageRequest(BaseModel):
    message: str = Field(min_length=1, max_length=4000)


class AgentConnectionRequest(BaseModel):
    agent_id: str | None = None


class PromoCodeRequest(BaseModel):
    code: str = Field(min_length=1, max_length=64)


class WorkspaceCredentialRequest(BaseModel):
    provider: str = Field(pattern=r"^openrouter$")
    key: str = Field(min_length=8, max_length=4096)
    label: str = Field(default="", max_length=120)


class WorkspaceUpdateRequest(BaseModel):
    name: str = Field(min_length=1, max_length=120)
    role: str = Field(pattern=r"^(owner|admin|member)$")


def require_mongo() -> MongoStore:
    if not mongo.configured:
        raise HTTPException(503, "MONGODB_URI is not configured")
    try:
        mongo.db.command("ping")
    except Exception as exc:
        raise HTTPException(503, "MongoDB is unavailable. Start MongoDB or set MONGODB_URI to a reachable database.") from exc
    return mongo


def current_user(request: Request) -> dict[str, Any]:
    store = require_mongo()
    # Clerk bearer sessions are used by the new React client. Keep Mongo's
    # existing ao_session cookie available during rollout for existing users.
    if request.headers.get("authorization", "").lower().startswith("bearer "):
        try:
            user = clerk_user_from_request(request, store)
        except ClerkAuthError as exc:
            raise HTTPException(exc.status_code, str(exc)) from exc
        if not user:
            raise HTTPException(401, "Invalid Clerk session")
        return user
    token = request.cookies.get(os.getenv("AUTH_COOKIE_NAME", "ao_session"))
    user = store.user_by_session(token) if token else None
    if not user:
        raise HTTPException(401, "Authentication required")
    return user


def jev_url() -> str:
    return os.getenv("JEV_BASE_URL", "https://api.typesafe.ai/v1").rstrip("/") + "/systemone"


async def choose_edge(graph: dict[str, Any], node_id: str, message: str, history: list[dict[str, str]]) -> dict[str, Any]:
    routes = graph["nodes"][node_id].get("routes", {})
    if not routes:
        return {"ok": True, "choice": None, "target": node_id, "provider": "none"}
    if os.getenv("JEV_MOCK", "true").lower() == "true":
        normalized = message.lower().strip()
        farewell = any(term in normalized for term in ("goodbye", "bye", "stop", "quit", "end the conversation", "that's all", "that is all"))
        node_map = graph["nodes"]
        candidates = [(key, route) for key, route in routes.items() if route.get("target") in node_map]
        if farewell:
            terminal = next(((key, route) for key, route in candidates if node_map[route["target"]].get("terminal") or route["target"] == "end"), None)
            if terminal:
                key, route = terminal
                return {"ok": True, "choice": key, "target": route["target"], "provider": "mock"}
        current_kind = node_map[node_id].get("kind")
        if current_kind != "agent":
            agent_target = next(((key, route) for key, route in candidates if node_map[route["target"]].get("kind") == "agent"), None)
            if agent_target:
                key, route = agent_target
                return {"ok": True, "choice": key, "target": route["target"], "provider": "mock"}
        else:
            self_route = next(((key, route) for key, route in candidates if route["target"] == node_id), None)
            if self_route:
                key, route = self_route
                return {"ok": True, "choice": key, "target": route["target"], "provider": "mock"}
            return {"ok": True, "choice": "stay", "target": node_id, "provider": "mock"}
        same_agent = next(((key, route) for key, route in candidates if node_map[route["target"]].get("kind") == "agent"), None)
        if same_agent:
            key, route = same_agent
        else:
            key, route = candidates[0]
        return {"ok": True, "choice": key, "target": route["target"], "provider": "mock"}
    api_key = os.getenv("JEV_API_KEY") or os.getenv("TYPESAFE_API_KEY") or os.getenv("SYSTEM_ONE_API_KEY")
    if not api_key:
        raise HTTPException(503, "JEV_API_KEY is not configured")
    route_options = dict(routes)
    if graph["nodes"][node_id].get("kind") == "agent":
        route_options.setdefault("stay", {"target": node_id, "description": "The user is continuing the current topic; keep the conversation on this agent node."})
    payload = {
        "model": os.getenv("JEV_MODEL", "jev-latest"),
        "state": {"current_node": node_id, "conversation_history": history, "latest_message": message,
                  "workflow_instructions": graph.get("jev_instructions", ""),
                  "current_node_instructions": graph["nodes"][node_id].get("instructions", []),
                  "current_node_routing_instructions": graph["nodes"][node_id].get("routing_instructions", "")},
        "questions": {"next_step": {
            "type": "choice",
            "instructions": "Choose exactly one valid next edge from the latest message and conversation context. Prefer ending when the user explicitly wants to stop. Follow workflow instructions and current-node guidance as operational constraints.",
            "criteria": {key: route.get("description", "") for key, route in route_options.items()},
        }},
    }
    async with httpx.AsyncClient(timeout=httpx.Timeout(8, connect=3)) as client:
        response = await client.post(jev_url(), headers={"Authorization": f"Bearer {api_key}"}, json=payload)
    response.raise_for_status()
    raw = response.json()
    answer = raw.get("answers", {}).get("next_step", {})
    choice = answer.get("choice")
    if answer.get("type") != "choice" or choice not in route_options:
        raise HTTPException(502, "Jev returned an invalid route choice")
    return {"ok": True, "choice": choice, "target": route_options[choice]["target"], "provider": "jev", "raw": raw}


@app.get("/api/health")
def health() -> dict[str, str]:
    return {"status": "ok"}


@app.get("/api/tools")
def tools_catalog() -> dict[str, Any]:
    """Return the built-in tool catalog; secrets are never included."""
    return {"tools": list_tools()}


@app.post("/api/mcp/discover")
async def mcp_discover(payload: McpDiscoverRequest, request: Request) -> dict[str, Any]:
    current_user(request)
    try:
        return {"tools": await discover_mcp_tools(payload.url)}
    except McpConnectionError as exc:
        raise HTTPException(422, str(exc)) from exc


@app.post("/api/auth/register")
def register(request: RegisterRequest, response: Response) -> dict[str, Any]:
    if not registrations_enabled():
        raise HTTPException(403, "New registrations are temporarily closed while setup is in progress.")
    store = require_mongo()
    try:
        user = store.create_user(request.email, request.password, request.name)
    except ValueError as exc:
        raise HTTPException(409, str(exc)) from exc
    store.create_workspace(user["id"], request.workspace_name)
    token = store.issue_session(user["id"])
    response.set_cookie(os.getenv("AUTH_COOKIE_NAME", "ao_session"), token, **auth_cookie_options())
    return {"user": user}


def registrations_enabled() -> bool:
    configured = os.getenv("ALLOW_NEW_REGISTRATIONS")
    if configured is not None:
        return configured.strip().lower() in {"1", "true", "yes", "on"}
    # Local development is open for testing; Render stays closed unless explicitly enabled.
    return not bool(os.getenv("RENDER"))


@app.post("/api/auth/login")
def login(request: LoginRequest, response: Response) -> dict[str, Any]:
    store = require_mongo()
    user = store.user_for_login(request.email)
    if not user or not verify_password(request.password, user["password_hash"]):
        raise HTTPException(401, "Invalid email or password")
    token = store.issue_session(user["id"])
    response.set_cookie(os.getenv("AUTH_COOKIE_NAME", "ao_session"), token, **auth_cookie_options())
    return {"user": store.public_user(user)}


@app.post("/api/auth/logout")
def logout(request: Request, response: Response) -> dict[str, bool]:
    store = require_mongo()
    name = os.getenv("AUTH_COOKIE_NAME", "ao_session")
    token = request.cookies.get(name)
    if token:
        store.revoke_session(token)
    response.delete_cookie(name, secure=os.getenv("AUTH_COOKIE_SECURE", "false").lower() == "true", samesite=os.getenv("AUTH_COOKIE_SAMESITE", "lax").lower())
    return {"ok": True}


@app.get("/api/auth/me")
def me(request: Request) -> dict[str, Any]:
    return {"user": current_user(request)}


@app.get("/api/workspaces")
def list_workspaces(request: Request) -> dict[str, Any]:
    user = current_user(request)
    return {"workspaces": mongo.workspaces_for_user(user["id"])}


@app.post("/api/workspaces")
def create_workspace(request: WorkspaceRequest, http_request: Request) -> dict[str, Any]:
    user = current_user(http_request)
    try:
        return {"workspace": mongo.create_workspace(user["id"], request.name)}
    except PermissionError as exc:
        raise HTTPException(403, str(exc)) from exc


@app.post("/api/spaces/join")
def join_space(request: SpaceJoinRequest, http_request: Request) -> dict[str, Any]:
    user = current_user(http_request)
    try:
        return {"space": mongo.join_workspace(user["id"], request.code)}
    except PermissionError as exc:
        raise HTTPException(403, str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc


@app.post("/api/spaces/{space_id}/join-code/rotate")
def rotate_space_join_code(space_id: str, request: SpaceJoinCodeRequest, http_request: Request) -> dict[str, Any]:
    user = current_user(http_request)
    if not request.github_collaborator_confirmed:
        raise HTTPException(422, "Confirm the invited user has been added to the GitHub repository before generating a join code")
    try:
        return {"space": mongo.regenerate_join_code(user["id"], space_id)}
    except PermissionError as exc:
        raise HTTPException(403, str(exc)) from exc


@app.get("/api/spaces/{space_id}/members")
def list_space_members(space_id: str, http_request: Request) -> dict[str, Any]:
    user = current_user(http_request)
    try:
        return {"members": mongo.space_members(user["id"], space_id)}
    except PermissionError as exc:
        raise HTTPException(403, str(exc)) from exc


@app.get("/api/spaces/{space_id}/agents")
def list_space_agents(space_id: str, http_request: Request) -> dict[str, Any]:
    user = current_user(http_request)
    try:
        return {"agents": mongo.space_agents(user["id"], space_id)}
    except PermissionError as exc:
        raise HTTPException(403, str(exc)) from exc


@app.get("/api/spaces/{space_id}/project")
def get_space_project_dashboard(space_id: str, http_request: Request) -> dict[str, Any]:
    user = current_user(http_request)
    try:
        return {"project": mongo.space_project_dashboard(user["id"], space_id)}
    except PermissionError as exc:
        raise HTTPException(403, str(exc)) from exc


@app.get("/api/spaces/{space_id}/context")
def get_space_context(space_id: str, http_request: Request) -> dict[str, Any]:
    user = current_user(http_request)
    try:
        return {"context": mongo.space_context(user["id"], space_id)}
    except PermissionError as exc:
        raise HTTPException(403, str(exc)) from exc


@app.put("/api/spaces/{space_id}/context")
def save_space_context(space_id: str, request: SpaceContextRequest, http_request: Request) -> dict[str, Any]:
    user = current_user(http_request)
    clean = {key: [item.strip() for item in values if item.strip()][:30] if isinstance(values, list) else values.strip()
             for key, values in request.model_dump().items()}
    try:
        return {"context": mongo.save_space_context(user["id"], space_id, clean)}
    except PermissionError as exc:
        raise HTTPException(403, str(exc)) from exc


@app.get("/api/spaces/{space_id}/memory")
def get_space_memory(space_id: str, http_request: Request) -> dict[str, Any]:
    user = current_user(http_request)
    try:
        return {"memory": mongo.space_memory(user["id"], space_id)}
    except PermissionError as exc:
        raise HTTPException(403, str(exc)) from exc


@app.post("/api/spaces/{space_id}/memory")
def add_space_memory(space_id: str, request: SpaceMemoryRequest, http_request: Request) -> dict[str, Any]:
    user = current_user(http_request)
    try:
        return {"memory": mongo.add_space_memory(user["id"], space_id, request.kind, request.content, request.source)}
    except PermissionError as exc:
        raise HTTPException(403, str(exc)) from exc


@app.delete("/api/spaces/{space_id}/memory/{memory_id}")
def delete_space_memory(space_id: str, memory_id: str, http_request: Request) -> dict[str, bool]:
    user = current_user(http_request)
    try:
        mongo.delete_space_memory(user["id"], space_id, memory_id)
        return {"ok": True}
    except PermissionError as exc:
        raise HTTPException(403, str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(404, str(exc)) from exc


@app.get("/api/spaces/{space_id}/skills")
def get_space_skills(space_id: str, http_request: Request) -> dict[str, Any]:
    user = current_user(http_request)
    try:
        return {"skills": mongo.space_skills(user["id"], space_id)}
    except PermissionError as exc:
        raise HTTPException(403, str(exc)) from exc


@app.put("/api/spaces/{space_id}/skills")
def save_space_skill(space_id: str, request: SpaceSkillRequest, http_request: Request) -> dict[str, Any]:
    user = current_user(http_request)
    try:
        return {"skill": mongo.save_space_skill(user["id"], space_id, request.model_dump())}
    except PermissionError as exc:
        raise HTTPException(403, str(exc)) from exc


@app.delete("/api/spaces/{space_id}/skills/{skill_id}")
def delete_space_skill(space_id: str, skill_id: str, http_request: Request) -> dict[str, bool]:
    user = current_user(http_request)
    try:
        mongo.delete_space_skill(user["id"], space_id, skill_id)
        return {"ok": True}
    except PermissionError as exc:
        raise HTTPException(403, str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(404, str(exc)) from exc


@app.get("/api/spaces/{space_id}/tasks")
def get_space_tasks(space_id: str, http_request: Request) -> dict[str, Any]:
    user = current_user(http_request)
    try:
        return {"tasks": mongo.space_tasks(user["id"], space_id)}
    except PermissionError as exc:
        raise HTTPException(403, str(exc)) from exc


@app.post("/api/spaces/{space_id}/tasks")
def create_space_task(space_id: str, request: SpaceTaskRequest, http_request: Request) -> dict[str, Any]:
    user = current_user(http_request)
    try:
        return {"task": mongo.create_space_task(user["id"], space_id, request.model_dump())}
    except PermissionError as exc:
        raise HTTPException(403, str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc


@app.patch("/api/spaces/{space_id}/tasks/{task_id}")
def update_space_task(space_id: str, task_id: str, request: SpaceTaskUpdateRequest, http_request: Request) -> dict[str, Any]:
    user = current_user(http_request)
    try:
        return {"task": mongo.update_space_task(user["id"], space_id, task_id, request.model_dump(exclude_unset=True))}
    except PermissionError as exc:
        raise HTTPException(403, str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(404, str(exc)) from exc


@app.get("/api/spaces/{space_id}/tasks/{task_id}/artifacts")
def get_space_task_artifacts(space_id: str, task_id: str, http_request: Request) -> dict[str, Any]:
    user = current_user(http_request)
    try:
        return {"artifacts": mongo.space_task_artifacts(user["id"], space_id, task_id)}
    except PermissionError as exc:
        raise HTTPException(403, str(exc)) from exc


@app.post("/api/spaces/{space_id}/tasks/{task_id}/advance", status_code=202)
def advance_space_task(space_id: str, task_id: str, request: SpaceTaskAdvanceRequest, http_request: Request) -> dict[str, Any]:
    user = current_user(http_request)
    try:
        return {"task": mongo.advance_space_task(user["id"], space_id, task_id, request.mode, request.agent_id, request.role)}
    except PermissionError as exc:
        raise HTTPException(403, str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(409, str(exc)) from exc


@app.delete("/api/spaces/{space_id}/tasks/{task_id}")
def delete_space_task(space_id: str, task_id: str, http_request: Request) -> dict[str, bool]:
    user = current_user(http_request)
    try:
        mongo.delete_space_task(user["id"], space_id, task_id)
        return {"ok": True}
    except PermissionError as exc:
        raise HTTPException(403, str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(409, str(exc)) from exc


@app.post("/api/spaces/{space_id}/tasks/{task_id}/start", status_code=202)
def start_space_task(space_id: str, task_id: str, background_tasks: BackgroundTasks, http_request: Request) -> dict[str, Any]:
    user = current_user(http_request)
    try:
        task = mongo.claim_space_task(user["id"], space_id, task_id)
        background_tasks.add_task(run_space_task, space_id, task_id, user["id"])
        return {"task": task, "message": "The assigned agent has started working. Results will be ready for review."}
    except PermissionError as exc:
        raise HTTPException(403, str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(409, str(exc)) from exc


@app.post("/api/spaces/{space_id}/tasks/start-all", status_code=202)
def start_all_space_tasks(space_id: str, request: SpaceTaskBatchRequest, background_tasks: BackgroundTasks, http_request: Request) -> dict[str, Any]:
    user = current_user(http_request)
    try:
        ready = mongo.ready_space_tasks(user["id"], space_id)
        if not ready:
            tasks = mongo.space_tasks(user["id"], space_id)
            todo = [task for task in tasks if task.get("status") == "todo"]
            if not todo:
                raise ValueError("No tasks are waiting to start. Add a task or move an existing task back to To do.")
            if any(not task.get("assignee_agent_id") for task in todo):
                raise ValueError("No task agent is assigned. Assign at least one agent to this Space before starting the queue.")
            if any(task.get("depends_on") for task in todo):
                waiting = sum(1 for task in todo if task.get("depends_on"))
                raise ValueError(f"{waiting} task(s) are waiting for dependent tasks to finish.")
            status_counts = ", ".join(f"{status}={sum(1 for task in tasks if task.get('status') == status)}" for status in ("todo", "in_progress", "review", "done"))
            raise ValueError(f"No dependency-ready tasks are available ({status_counts}).")
        background_tasks.add_task(run_space_task_batch, space_id, user["id"], request.mode)
        return {"mode": request.mode, "started_count": len(ready), "message": "Ready tasks have been queued."}
    except PermissionError as exc:
        raise HTTPException(403, str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(409, str(exc)) from exc


@app.post("/api/spaces/{space_id}/tasks/reset-running")
def reset_running_space_tasks(space_id: str, http_request: Request) -> dict[str, Any]:
    user = current_user(http_request)
    try:
        count = mongo.reset_running_space_tasks(user["id"], space_id)
        return {"reset_count": count, "message": f"Reset {count} running task(s). They can now be started again."}
    except PermissionError as exc:
        raise HTTPException(403, str(exc)) from exc


@app.post("/api/spaces/{space_id}/tasks/{task_id}/branch")
def prepare_space_task_branch(space_id: str, task_id: str, request: SpaceTaskBranchRequest, http_request: Request) -> dict[str, Any]:
    user = current_user(http_request)
    try:
        token, project = github_contributor_context(user["id"], space_id)
        task = mongo.space_task_for_user(user["id"], space_id, task_id)
        branch = (request.branch or f"agent/task-{task_id[:8]}").strip()
        result = create_branch(project["repository_url"], token, branch, project.get("default_branch") or "main")
        return {"task": mongo.set_space_task_branch(space_id, task_id, result["branch"], result.get("sha")), "branch": result}
    except GitHubAuthorizationError as exc:
        raise HTTPException(exc.status_code, str(exc)) from exc
    except PermissionError as exc:
        raise HTTPException(403, str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(409, str(exc)) from exc
    except RuntimeError as exc:
        raise HTTPException(503, str(exc)) from exc


@app.post("/api/spaces/{space_id}/tasks/{task_id}/pull-request")
def open_space_task_pull_request(space_id: str, task_id: str, request: SpaceTaskPullRequestRequest, http_request: Request) -> dict[str, Any]:
    user = current_user(http_request)
    try:
        token, project = github_contributor_context(user["id"], space_id)
        task = mongo.space_task_for_user(user["id"], space_id, task_id)
        branch = (task.get("execution") or {}).get("branch")
        if not branch:
            raise ValueError("Prepare a task branch before opening a pull request")
        title = request.title.strip() or task.get("title", "Agent task")
        pull_request = create_pull_request(project["repository_url"], token, title, request.body, branch, project.get("default_branch") or "main")
        return {"task": mongo.set_space_task_pull_request(space_id, task_id, pull_request), "pull_request": pull_request}
    except GitHubAuthorizationError as exc:
        raise HTTPException(exc.status_code, str(exc)) from exc
    except PermissionError as exc:
        raise HTTPException(403, str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(409, str(exc)) from exc
    except RuntimeError as exc:
        raise HTTPException(503, str(exc)) from exc


@app.post("/api/spaces/{space_id}/tasks/{task_id}/apply-proposal")
def apply_space_task_proposal(space_id: str, task_id: str, request: SpaceTaskApplyProposalRequest, http_request: Request) -> dict[str, Any]:
    user = current_user(http_request)
    try:
        token, project = github_owner_commit_context(user["id"], space_id)
        task = mongo.space_task_for_user(user["id"], space_id, task_id)
        execution = task.get("execution") or {}
        branch = execution.get("branch")
        proposal = execution.get("proposed_changes") or {}
        files = proposal.get("files") if isinstance(proposal, dict) else None
        if not branch:
            raise ValueError("Prepare a task branch before applying the proposal")
        if not isinstance(files, list) or not files:
            raise ValueError("This task has no valid proposed file changes to apply")
        selected_paths = {path.strip().lstrip("/") for path in request.paths if path.strip()} if request.paths else None
        if selected_paths is not None:
            files = [file for file in files if file.get("path") in selected_paths]
            if not files:
                raise ValueError("Select at least one proposed file to apply")
        message = request.message.strip() or f"Apply agent proposal: {task.get('title', 'task')}"
        commit = commit_files(project["repository_url"], token, branch, message, files)
        return {"task": mongo.set_space_task_commit(space_id, task_id, commit), "commit": commit}
    except GitHubAuthorizationError as exc:
        raise HTTPException(exc.status_code, str(exc)) from exc
    except PermissionError as exc:
        raise HTTPException(403, str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(409, str(exc)) from exc
    except RuntimeError as exc:
        raise HTTPException(503, str(exc)) from exc


@app.post("/api/spaces/{space_id}/tasks/{task_id}/reject-proposal")
def reject_space_task_proposal(space_id: str, task_id: str, request: SpaceTaskRejectProposalRequest, http_request: Request) -> dict[str, Any]:
    user = current_user(http_request)
    try:
        return {"task": mongo.reject_space_task_proposal(user["id"], space_id, task_id, request.reason.strip())}
    except PermissionError as exc:
        raise HTTPException(403, str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(409, str(exc)) from exc


@app.get("/api/spaces/{space_id}/approvals")
def get_space_approvals(space_id: str, http_request: Request) -> dict[str, Any]:
    user = current_user(http_request)
    try:
        return {"approvals": mongo.space_approvals(user["id"], space_id)}
    except PermissionError as exc:
        raise HTTPException(403, str(exc)) from exc


@app.post("/api/spaces/{space_id}/approvals")
def request_space_approval(space_id: str, request: SpaceApprovalRequest, http_request: Request) -> dict[str, Any]:
    user = current_user(http_request)
    try:
        return {"approval": mongo.request_space_approval(user["id"], space_id, request.model_dump())}
    except PermissionError as exc:
        raise HTTPException(403, str(exc)) from exc


@app.post("/api/spaces/{space_id}/approvals/{approval_id}/{decision}")
def decide_space_approval(space_id: str, approval_id: str, decision: str, http_request: Request) -> dict[str, Any]:
    if decision not in {"approved", "rejected"}:
        raise HTTPException(422, "Approval decision must be approved or rejected")
    user = current_user(http_request)
    try:
        return {"approval": mongo.decide_space_approval(user["id"], space_id, approval_id, decision)}
    except PermissionError as exc:
        raise HTTPException(403, str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(404, str(exc)) from exc


@app.get("/api/spaces/{space_id}/agents/available")
def list_available_space_agents(space_id: str, http_request: Request) -> dict[str, Any]:
    user = current_user(http_request)
    try:
        return {"agents": mongo.available_agents_for_space(user["id"], space_id)}
    except PermissionError as exc:
        raise HTTPException(403, str(exc)) from exc


@app.post("/api/spaces/{space_id}/agents")
def assign_space_agent(space_id: str, request: SpaceAgentRequest, http_request: Request) -> dict[str, Any]:
    user = current_user(http_request)
    try:
        return {"agent": mongo.assign_space_agent(user["id"], space_id, request.agent_id, request.role)}
    except PermissionError as exc:
        raise HTTPException(403, str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc


@app.patch("/api/spaces/{space_id}/agents/{agent_id}")
def update_space_agent_role(space_id: str, agent_id: str, request: SpaceAgentRoleRequest, http_request: Request) -> dict[str, bool]:
    user = current_user(http_request)
    try:
        mongo.update_space_agent_role(user["id"], space_id, agent_id, request.role)
        return {"ok": True}
    except PermissionError as exc:
        raise HTTPException(403, str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc


@app.delete("/api/spaces/{space_id}/agents/{agent_id}")
def remove_space_agent(space_id: str, agent_id: str, http_request: Request) -> dict[str, bool]:
    user = current_user(http_request)
    try:
        mongo.remove_space_agent(user["id"], space_id, agent_id)
        return {"ok": True}
    except PermissionError as exc:
        raise HTTPException(403, str(exc)) from exc


@app.get("/api/spaces/{space_id}")
def get_space_detail(space_id: str, http_request: Request) -> dict[str, Any]:
    user = current_user(http_request)
    try:
        return {"space": mongo.space_detail(user["id"], space_id)}
    except PermissionError as exc:
        raise HTTPException(403, str(exc)) from exc


@app.put("/api/spaces/{space_id}/project")
def save_space_project(space_id: str, request: SpaceProjectRequest, http_request: Request) -> dict[str, Any]:
    user = current_user(http_request)
    try:
        return {"project": mongo.save_space_project(user["id"], space_id, request.repository_url, request.default_branch)}
    except PermissionError as exc:
        raise HTTPException(403, str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc


@app.get("/api/spaces/{space_id}/project/settings")
def get_space_project_settings(space_id: str, http_request: Request) -> dict[str, Any]:
    user = current_user(http_request)
    try:
        return {"settings": mongo.space_project_settings(user["id"], space_id),
                "can_manage": mongo.space_detail(user["id"], space_id).get("role") == "owner"}
    except PermissionError as exc:
        raise HTTPException(403, str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc


@app.patch("/api/spaces/{space_id}/project/settings")
def update_space_project_settings(space_id: str, request: SpaceProjectSettingsRequest, http_request: Request) -> dict[str, Any]:
    user = current_user(http_request)
    try:
        return {"settings": mongo.update_space_project_settings(user["id"], space_id, request.model_dump())}
    except PermissionError as exc:
        raise HTTPException(403, str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc


@app.delete("/api/spaces/{space_id}/project")
def remove_space_project(space_id: str, http_request: Request) -> dict[str, bool]:
    user = current_user(http_request)
    try:
        mongo.remove_space_project(user["id"], space_id)
        return {"ok": True}
    except PermissionError as exc:
        raise HTTPException(403, str(exc)) from exc


@app.post("/api/spaces/{space_id}/github/verify")
def verify_space_github(space_id: str, http_request: Request) -> dict[str, Any]:
    user = current_user(http_request)
    clerk_user_id = mongo.clerk_identity_for_user(user["id"])
    if not clerk_user_id:
        raise HTTPException(409, "GitHub authorization requires a Clerk-linked account")
    try:
        detail = mongo.space_detail(user["id"], space_id)
        if detail.get("role") not in {"owner", "admin"}:
            raise PermissionError("Only Space owners and admins can authorize GitHub")
        project = detail.get("project")
        if not project:
            raise GitHubAuthorizationError("Save a GitHub repository before authorizing it", 422)
        token, scopes = clerk_github_token(clerk_user_id)
        repository = verify_repository(project["repository_url"], token)
        return {"project": mongo.authorize_space_github(user["id"], space_id, encrypt_secret(token), repository, scopes)}
    except GitHubAuthorizationError as exc:
        raise HTTPException(exc.status_code, str(exc)) from exc
    except PermissionError as exc:
        raise HTTPException(403, str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc
    except RuntimeError as exc:
        raise HTTPException(503, str(exc)) from exc


@app.get("/api/spaces/{space_id}/github/app/install")
def start_space_github_app_install(space_id: str, http_request: Request) -> dict[str, str]:
    user = current_user(http_request)
    try:
        from bson import ObjectId
        detail = mongo.space_detail(user["id"], space_id)
        if not detail.get("project"):
            raise GitHubAuthorizationError("Save a GitHub repository before installing the GitHub App", 422)
        membership = mongo.db.memberships.find_one({"workspace_id": ObjectId(space_id), "user_id": ObjectId(user["id"])})
        if not membership or membership.get("role") not in {"owner", "admin"}:
            raise PermissionError("Only Space owners and admins can authorize GitHub")
        state = github_app_state(user["id"], space_id)
        return {"install_url": github_app_install_url(state)}
    except GitHubAuthorizationError as exc:
        raise HTTPException(exc.status_code, str(exc)) from exc
    except PermissionError as exc:
        raise HTTPException(403, str(exc)) from exc
    except RuntimeError as exc:
        raise HTTPException(503, str(exc)) from exc


@app.get("/api/github/app/callback")
def finish_github_app_install(state: str, installation_id: str | None = None, setup_action: str | None = None) -> Response:
    frontend = (os.getenv("FRONTEND_URL", "").strip() or "http://localhost:5173").rstrip("/")
    try:
        payload = github_app_state_payload(state)
        space_id = payload["space_id"]
        user_id = payload["user_id"]
        detail = mongo.space_detail(user_id, space_id)
        project = detail.get("project")
        if not project:
            raise GitHubAuthorizationError("Save a GitHub repository before authorizing it", 422)
        if setup_action == "uninstall":
            raise GitHubAuthorizationError("The GitHub App installation was cancelled", 400)
        if not installation_id:
            raise GitHubAuthorizationError("GitHub did not return an installation", 400)
        token, repository = github_app_installation_token(installation_id, project["repository_url"])
        updated = mongo.authorize_space_github(user_id, space_id, encrypt_secret(token), repository,
                                               ["github_app", "contents:write", "pull_requests:write"])
        from bson import ObjectId
        mongo.db.space_projects.update_one({"workspace_id": ObjectId(space_id)}, {"$set": {
            "github_auth_type": "github_app", "github_installation_id": str(installation_id)
        }})
        del updated
        return RedirectResponse(f"{frontend}/spaces?github=authorized&space_id={space_id}", status_code=303)
    except (GitHubAuthorizationError, PermissionError, ValueError, RuntimeError) as exc:
        status = exc.status_code if isinstance(exc, GitHubAuthorizationError) else 400
        from urllib.parse import quote
        message = quote(str(exc), safe="")
        space_id = ""
        try:
            space_id = str(github_app_state_payload(state).get("space_id", ""))
        except GitHubAuthorizationError:
            pass
        return RedirectResponse(f"{frontend}/spaces?github=error&space_id={space_id}&message={message}", status_code=303)


@app.get("/api/spaces/{space_id}/github/preflight")
def preflight_space_github(space_id: str, http_request: Request) -> dict[str, Any]:
    user = current_user(http_request)
    try:
        mongo.space_detail(user["id"], space_id)
    except PermissionError as exc:
        raise HTTPException(403, str(exc)) from exc
    clerk_user_id = mongo.clerk_identity_for_user(user["id"])
    if not clerk_user_id:
        return {"connected": False, "message": "This account is not linked to Clerk GitHub OAuth."}
    try:
        _, scopes = clerk_github_token(clerk_user_id)
        return {"connected": True, "scopes": scopes}
    except GitHubAuthorizationError as exc:
        if exc.status_code == 409:
            return {"connected": False, "message": str(exc)}
        raise HTTPException(exc.status_code, str(exc)) from exc


@app.post("/api/spaces/{space_id}/github/disconnect")
def disconnect_space_github(space_id: str, http_request: Request) -> dict[str, bool]:
    user = current_user(http_request)
    try:
        mongo.disconnect_space_github(user["id"], space_id)
        return {"ok": True}
    except PermissionError as exc:
        raise HTTPException(403, str(exc)) from exc


@app.post("/api/spaces/{space_id}/github/test")
def test_space_github(space_id: str, http_request: Request) -> dict[str, Any]:
    user = current_user(http_request)
    try:
        token, project = mongo.github_token_for_space(user["id"], space_id)
        repository = verify_repository(project["repository_url"], token)
        return {"ok": True, "repository": repository}
    except GitHubAuthorizationError as exc:
        raise HTTPException(exc.status_code, str(exc)) from exc
    except PermissionError as exc:
        raise HTTPException(403, str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc
    except RuntimeError as exc:
        raise HTTPException(503, str(exc)) from exc


def github_mutation_context(user_id: str, space_id: str) -> tuple[str, dict[str, Any]]:
    detail = mongo.space_detail(user_id, space_id)
    if detail.get("role") not in {"owner", "admin"}:
        raise PermissionError("Only Space owners and admins can change the repository")
    token, project = mongo.github_token_for_space(user_id, space_id)
    return token, project


def github_contributor_context(user_id: str, space_id: str) -> tuple[str, dict[str, Any]]:
    detail = mongo.space_detail(user_id, space_id)
    if detail.get("role") not in {"owner", "admin", "member"}:
        raise PermissionError("Only Space contributors can create branches or pull requests")
    token, project = mongo.github_token_for_space(user_id, space_id)
    return token, project


def github_owner_commit_context(user_id: str, space_id: str) -> tuple[str, dict[str, Any]]:
    detail = mongo.space_detail(user_id, space_id)
    if detail.get("role") != "owner":
        raise PermissionError("Only the Space owner can commit directly; create a pull request instead")
    token, project = mongo.github_token_for_space(user_id, space_id)
    return token, project


@app.get("/api/spaces/{space_id}/github/snapshot")
def get_space_github_snapshot(space_id: str, http_request: Request, branch: str | None = None) -> dict[str, Any]:
    user = current_user(http_request)
    try:
        token, project = mongo.github_token_for_space(user["id"], space_id)
        return {"snapshot": repository_snapshot(project["repository_url"], token, branch)}
    except GitHubAuthorizationError as exc:
        raise HTTPException(exc.status_code, str(exc)) from exc
    except PermissionError as exc:
        raise HTTPException(403, str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc
    except RuntimeError as exc:
        raise HTTPException(503, str(exc)) from exc


@app.get("/api/spaces/{space_id}/github/file")
def get_space_github_file(space_id: str, path: str, http_request: Request, branch: str | None = None) -> dict[str, Any]:
    user = current_user(http_request)
    try:
        token, project = mongo.github_token_for_space(user["id"], space_id)
        return {"file": read_file(project["repository_url"], token, path, branch)}
    except GitHubAuthorizationError as exc:
        raise HTTPException(exc.status_code, str(exc)) from exc
    except PermissionError as exc:
        raise HTTPException(403, str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc
    except RuntimeError as exc:
        raise HTTPException(503, str(exc)) from exc


@app.get("/api/spaces/{space_id}/github/branches")
def list_space_github_branches(space_id: str, http_request: Request) -> dict[str, Any]:
    user = current_user(http_request)
    try:
        detail = mongo.space_detail(user["id"], space_id)
        if detail.get("role") != "owner":
            raise PermissionError("Only the Space owner can view project Git settings")
        token, project = mongo.github_token_for_space(user["id"], space_id)
        return {"branches": repository_branches(project["repository_url"], token)}
    except GitHubAuthorizationError as exc:
        raise HTTPException(exc.status_code, str(exc)) from exc
    except PermissionError as exc:
        raise HTTPException(403, str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc
    except RuntimeError as exc:
        raise HTTPException(503, str(exc)) from exc


@app.post("/api/spaces/{space_id}/github/branches")
def create_space_github_branch(space_id: str, request: GitHubBranchRequest, http_request: Request) -> dict[str, Any]:
    user = current_user(http_request)
    try:
        token, project = github_contributor_context(user["id"], space_id)
        settings = mongo.space_project_settings(user["id"], space_id)
        if mongo.space_detail(user["id"], space_id).get("role") != "owner" and settings["require_branch_for_non_owners"] and request.branch.strip() == settings["default_branch"]:
            raise ValueError("Non-owner changes must use a dedicated branch")
        return {"branch": create_branch(project["repository_url"], token, request.branch, request.from_branch)}
    except GitHubAuthorizationError as exc:
        raise HTTPException(exc.status_code, str(exc)) from exc
    except PermissionError as exc:
        raise HTTPException(403, str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc
    except RuntimeError as exc:
        raise HTTPException(503, str(exc)) from exc


@app.post("/api/spaces/{space_id}/github/commits")
def commit_space_github_files(space_id: str, request: GitHubCommitRequest, http_request: Request) -> dict[str, Any]:
    user = current_user(http_request)
    try:
        token, project = github_owner_commit_context(user["id"], space_id)
        settings = mongo.space_project_settings(user["id"], space_id)
        if settings["pull_before_commit"]:
            ensure_branch_is_current(project["repository_url"], token, request.branch, settings["default_branch"])
        return {"commit": commit_files(project["repository_url"], token, request.branch, request.message, request.files)}
    except GitHubAuthorizationError as exc:
        raise HTTPException(exc.status_code, str(exc)) from exc
    except PermissionError as exc:
        raise HTTPException(403, str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc
    except RuntimeError as exc:
        raise HTTPException(503, str(exc)) from exc


@app.post("/api/spaces/{space_id}/github/pull-requests")
def create_space_github_pull_request(space_id: str, request: GitHubPullRequestRequest, http_request: Request) -> dict[str, Any]:
    user = current_user(http_request)
    try:
        token, project = github_contributor_context(user["id"], space_id)
        settings = mongo.space_project_settings(user["id"], space_id)
        detail = mongo.space_detail(user["id"], space_id)
        if detail.get("role") != "owner" and settings["require_pull_request_for_non_owners"] and request.head.strip() == request.base.strip():
            raise ValueError("Non-owner changes must be submitted from a separate branch")
        return {"pull_request": create_pull_request(project["repository_url"], token, request.title, request.body, request.head, request.base)}
    except GitHubAuthorizationError as exc:
        raise HTTPException(exc.status_code, str(exc)) from exc
    except PermissionError as exc:
        raise HTTPException(403, str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc
    except RuntimeError as exc:
        raise HTTPException(503, str(exc)) from exc


@app.patch("/api/spaces/{space_id}/members/{member_id}")
def update_space_member_role(space_id: str, member_id: str, request: SpaceMemberRoleRequest, http_request: Request) -> dict[str, bool]:
    user = current_user(http_request)
    try:
        mongo.update_space_member_role(user["id"], space_id, member_id, request.role)
        return {"ok": True}
    except PermissionError as exc:
        raise HTTPException(403, str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc


@app.patch("/api/workspaces/{workspace_id}")
def update_workspace(workspace_id: str, request: WorkspaceUpdateRequest, http_request: Request) -> dict[str, Any]:
    user = current_user(http_request)
    try:
        return {"workspace": mongo.update_workspace(user["id"], workspace_id, request.name, request.role)}
    except PermissionError as exc:
        raise HTTPException(403, str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc


@app.get("/api/billing")
def billing(http_request: Request) -> dict[str, Any]:
    user = current_user(http_request)
    return mongo.subscription_for_user(user["id"])


@app.post("/api/billing/promo")
def redeem_promo(request: PromoCodeRequest, http_request: Request) -> dict[str, Any]:
    user = current_user(http_request)
    try:
        subscription = mongo.redeem_promo(user["id"], request.code)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    return {"subscription": subscription}


@app.get("/api/workspaces/{workspace_id}/credentials")
def list_workspace_credentials(workspace_id: str, http_request: Request) -> dict[str, Any]:
    user = current_user(http_request)
    try:
        return {"credentials": mongo.workspace_credentials(user["id"], workspace_id)}
    except PermissionError as exc:
        raise HTTPException(403, str(exc)) from exc


@app.put("/api/workspaces/{workspace_id}/credentials")
def rotate_workspace_credential(workspace_id: str, request: WorkspaceCredentialRequest, http_request: Request) -> dict[str, Any]:
    user = current_user(http_request)
    try:
        normalized_key = request.key.strip()
        if not normalized_key:
            raise HTTPException(422, "OpenRouter key cannot be blank")
        result = mongo.save_workspace_credential(user["id"], workspace_id, request.provider,
                                                 encrypt_secret(normalized_key), request.label.strip() or request.provider.title(),
                                                 normalized_key[-4:])
        return {"credential": result}
    except PermissionError as exc:
        raise HTTPException(403, str(exc)) from exc
    except RuntimeError as exc:
        raise HTTPException(503, str(exc)) from exc


@app.post("/api/workspaces/{workspace_id}/credentials/{provider}/test")
async def test_workspace_credential(workspace_id: str, provider: str, http_request: Request) -> dict[str, Any]:
    user = current_user(http_request)
    try:
        row = mongo.get_workspace_credential(user["id"], workspace_id, provider)
        if not row:
            raise HTTPException(404, "Credential not found")
        key = decrypt_secret(row["encrypted_value"]).strip()
        if provider == "openrouter":
            # /models is a public catalog and may return 200 even for an
            # invalid bearer token. /key validates the credential itself.
            url, headers = "https://openrouter.ai/api/v1/key", {"Authorization": f"Bearer {key}"}
        else:
            raise HTTPException(400, "Unsupported provider")
        async with httpx.AsyncClient(timeout=httpx.Timeout(8, connect=3)) as client:
            response = await client.get(url, headers=headers)
        response.raise_for_status()
        from bson import ObjectId
        mongo.db.workspace_credentials.update_one({"_id": row["_id"]}, {"$set": {"last_tested_at": mongo_store_now()}})
        return {"ok": True, "provider": provider}
    except HTTPException:
        raise
    except httpx.HTTPStatusError as exc:
        detail = ""
        try:
            payload = exc.response.json()
            if isinstance(payload, dict):
                error_payload = payload.get("error")
                detail = str(error_payload.get("message") if isinstance(error_payload, dict) else error_payload or payload.get("message") or "")
        except (ValueError, TypeError):
            detail = ""
        raise HTTPException(502, f"{provider.title()} rejected this key (HTTP {exc.response.status_code}): {detail[:300] or exc.response.reason_phrase}") from exc
    except Exception as exc:
        raise HTTPException(502, f"{provider.title()} key test failed: {str(exc)[:300]}") from exc


@app.delete("/api/workspaces/{workspace_id}/credentials/{provider}")
def remove_workspace_credential(workspace_id: str, provider: str, http_request: Request) -> dict[str, bool]:
    user = current_user(http_request)
    try:
        mongo.remove_workspace_credential(user["id"], workspace_id, provider)
        return {"ok": True}
    except PermissionError as exc:
        raise HTTPException(403, str(exc)) from exc


@app.post("/api/workspaces/{workspace_id}/flows")
def create_flow(workspace_id: str, request: FlowSaveRequest, http_request: Request) -> dict[str, Any]:
    user = current_user(http_request)
    try:
        validated = validate_flow(request.flow)
        return {"flow": mongo.create_flow(user["id"], workspace_id, validated)}
    except FlowValidationError as exc:
        raise HTTPException(422, {"message": str(exc), "errors": exc.errors})


@app.post("/api/flows/{flow_id}/versions")
def save_flow_version(flow_id: str, request: FlowSaveRequest, http_request: Request) -> dict[str, Any]:
    user = current_user(http_request)
    try:
        validated = validate_flow(request.flow)
        return {"version": mongo.save_flow_version(user["id"], flow_id, validated)}
    except FlowValidationError as exc:
        raise HTTPException(422, {"message": str(exc), "errors": exc.errors})


@app.get("/api/workspaces/{workspace_id}/flows")
def list_flows(workspace_id: str, request: Request) -> dict[str, Any]:
    user = current_user(request)
    try:
        return {"flows": mongo.list_flows(user["id"], workspace_id)}
    except PermissionError as exc:
        raise HTTPException(403, str(exc))


@app.get("/api/flows/{flow_id}")
def get_flow(flow_id: str, request: Request) -> dict[str, Any]:
    user = current_user(request)
    try:
        flow = mongo.get_flow(user["id"], flow_id)
        if not flow:
            raise HTTPException(404, "Flow not found")
        flow["graph"] = canvas_graph(flow["flow"])
        return flow
    except PermissionError as exc:
        raise HTTPException(403, str(exc))


@app.get("/api/flows/{flow_id}/versions")
def list_flow_versions(flow_id: str, request: Request) -> dict[str, Any]:
    user = current_user(request)
    try:
        return {"versions": mongo.list_flow_versions(user["id"], flow_id)}
    except PermissionError as exc:
        raise HTTPException(403, str(exc))


@app.post("/api/flows/{flow_id}/publish")
def publish_flow(flow_id: str, request: Request) -> dict[str, Any]:
    user = current_user(request)
    try:
        result = mongo.publish_flow(user["id"], flow_id)
        base_url = os.getenv("PUBLIC_API_BASE_URL") or os.getenv("RENDER_EXTERNAL_URL") or str(request.base_url).rstrip("/")
        result["public_endpoint"] = f"{base_url}/api/workflows/{result['slug']}/chat"
        return {"flow": result}
    except (PermissionError, ValueError) as exc:
        raise HTTPException(422, str(exc))


@app.post("/api/flows/{flow_id}/public-token/rotate")
def rotate_public_token(flow_id: str, request: Request) -> dict[str, Any]:
    user = current_user(request)
    try:
        result = mongo.rotate_public_token(user["id"], flow_id)
        base_url = os.getenv("PUBLIC_API_BASE_URL") or os.getenv("RENDER_EXTERNAL_URL") or str(request.base_url).rstrip("/")
        return {**result, "public_endpoint": f"{base_url}/api/workflows/{result['slug']}/chat"}
    except PermissionError as exc:
        raise HTTPException(403, str(exc)) from exc


@app.post("/api/flows/{flow_id}/unpublish")
def unpublish_flow(flow_id: str, request: Request) -> dict[str, Any]:
    user = current_user(request)
    try:
        return {"flow": mongo.unpublish_flow(user["id"], flow_id)}
    except PermissionError as exc:
        raise HTTPException(403, str(exc))


@app.post("/api/flows/{flow_id}/pause")
def pause_flow(flow_id: str, request: Request) -> dict[str, Any]:
    user = current_user(request)
    try:
        return {"flow": mongo.pause_flow(user["id"], flow_id)}
    except PermissionError as exc:
        raise HTTPException(403, str(exc))


@app.delete("/api/flows/{flow_id}")
def delete_flow(flow_id: str, request: Request) -> dict[str, Any]:
    user = current_user(request)
    try:
        mongo.delete_flow(user["id"], flow_id)
        return {"ok": True}
    except PermissionError as exc:
        raise HTTPException(403, str(exc))


@app.delete("/api/agents/{agent_id}")
def delete_agent(agent_id: str, request: Request) -> dict[str, Any]:
    user = current_user(request)
    try:
        mongo.delete_agent(user["id"], agent_id)
        return {"ok": True}
    except PermissionError as exc:
        raise HTTPException(403, str(exc))
    except ValueError as exc:
        raise HTTPException(422, str(exc))


@app.put("/api/agents/{agent_id}")
def update_agent(agent_id: str, payload: ResourceRequest, request: Request) -> dict[str, Any]:
    user = current_user(request)
    try:
        data = payload.model_dump() if hasattr(payload, "model_dump") else payload.dict()
        return {"agent": mongo.update_agent(user["id"], agent_id, data.get("config", {}))}
    except PermissionError as exc:
        raise HTTPException(403, str(exc))
    except ValueError as exc:
        raise HTTPException(422, str(exc))


def resource_endpoints(collection: str, resource_name: str):
    @app.get(f"/api/workspaces/{{workspace_id}}/{resource_name}")
    def list_resource(workspace_id: str, request: Request) -> dict[str, Any]:
        user = current_user(request)
        try:
            return {resource_name: mongo.list_resources(user["id"], workspace_id, collection)}
        except PermissionError as exc:
            raise HTTPException(403, str(exc))

    @app.post(f"/api/workspaces/{{workspace_id}}/{resource_name}")
    def create_resource(workspace_id: str, payload: ResourceRequest, request: Request) -> dict[str, Any]:
        user = current_user(request)
        try:
            payload_data = payload.model_dump() if hasattr(payload, "model_dump") else payload.dict()
            return {resource_name[:-1]: mongo.create_resource(user["id"], workspace_id, collection, payload_data)}
        except PermissionError as exc:
            raise HTTPException(403, str(exc))
        except ValueError as exc:
            raise HTTPException(422, str(exc))


resource_endpoints("tools", "tools")
resource_endpoints("agents", "agents")
resource_endpoints("connectors", "connectors")
resource_endpoints("credential_refs", "credential-refs")


@app.get("/api/agents/{agent_id}/connections")
def agent_connections(agent_id: str, request: Request) -> dict[str, Any]:
    user = current_user(request)
    try:
        return {"connections": mongo.agent_connections(user["id"], agent_id)}
    except PermissionError as exc:
        raise HTTPException(403, str(exc))
    except ValueError as exc:
        raise HTTPException(404, str(exc))


@app.put("/api/flows/{flow_id}/agent")
def connect_agent(flow_id: str, payload: AgentConnectionRequest, request: Request) -> dict[str, Any]:
    user = current_user(request)
    try:
        mongo.set_flow_agent(user["id"], flow_id, payload.agent_id)
        flow = mongo.get_flow(user["id"], flow_id)
        if not flow:
            raise HTTPException(404, "Flow not found")
        flow["graph"] = canvas_graph(flow["flow"])
        return flow
    except PermissionError as exc:
        raise HTTPException(403, str(exc))
    except ValueError as exc:
        raise HTTPException(422, str(exc))


@app.delete("/api/flows/{flow_id}/agent")
def disconnect_agent(flow_id: str, request: Request) -> dict[str, Any]:
    return connect_agent(flow_id, AgentConnectionRequest(agent_id=None), request)


@app.get("/api/graph")
def graph() -> dict[str, Any]:
    return deepcopy(GRAPH)


@app.get("/api/flow")
def flow() -> dict[str, Any]:
    return deepcopy(FLOW)


@app.post("/api/flow/validate")
def validate_flow_endpoint(request: ValidateRequest) -> dict[str, Any]:
    try:
        validated = validate_flow(request.flow)
    except FlowValidationError as exc:
        raise HTTPException(422, detail={"message": str(exc), "errors": exc.errors})
    return {"valid": True, "flow_id": validated["id"], "version": validated["version"]}


async def runtime_route(flow: dict[str, Any], node_id: str, message: str, history: list[dict[str, str]]) -> dict[str, Any]:
    return await choose_edge(canvas_graph(flow), node_id, message, history)


@app.post("/api/sessions")
async def create_session(request: SessionCreateRequest, http_request: Request) -> dict[str, Any]:
    flow = FLOW
    if request.flow_id:
        user = current_user(http_request)
        try:
            stored_flow = mongo.get_flow(user["id"], request.flow_id)
            if not stored_flow or not stored_flow.get("flow"):
                raise ValueError("Workflow version is missing")
            flow = deepcopy(stored_flow["flow"])
            # Keep the persisted flow id in the runtime session so later turns
            # can reload the exact published workflow instead of DEMO_FLOW.
            flow["id"] = request.flow_id
            flow = validate_flow(flow)
            flow["workspace_id"] = stored_flow["workspace_id"]
        except (PermissionError, ValueError) as exc:
            raise HTTPException(422, str(exc))
    session = await runtime.start(flow)
    return {"session_id": session["id"], "flow_id": session["flow_id"], "flow_version": session["flow_version"],
            "status": session["status"], "current_node": session["current_node"]}


def public_token_from_request(request: Request) -> str | None:
    authorization = request.headers.get("authorization", "")
    if authorization.lower().startswith("bearer "):
        return authorization[7:].strip()
    return request.headers.get("x-workflow-token")


@app.post("/api/workflows/{slug}/chat")
@app.post("/api/public/workflows/{slug}/chat", include_in_schema=False)
async def public_workflow_chat(slug: str, request: PublicChatRequest, http_request: Request) -> dict[str, Any]:
    store = require_mongo()
    token = public_token_from_request(http_request)
    published = store.public_flow_by_token(slug, token or "")
    if not published:
        raise HTTPException(401, "Invalid, expired, or unpublished workflow token")
    flow = deepcopy(published["flow"])
    flow["id"] = published["flow_id"]
    flow = validate_flow(flow)
    flow["workspace_id"] = published["workspace_id"]
    session_id = request.session_id
    if not session_id:
        session = await runtime.start(flow)
        session_id = session["id"]
    else:
        session = runtime.store.get(session_id)
        if not session or session.get("flow_id") != published["flow_id"]:
            raise HTTPException(404, "Chat session not found")
    try:
        store.consume_workspace_execution(published["workspace_id"])
        result = await runtime.turn(flow, session_id, request.message, runtime_route)
    except PermissionError as exc:
        raise HTTPException(402, str(exc)) from exc
    except (ValueError, RuntimeError) as exc:
        raise HTTPException(400, str(exc)) from exc
    return {"workflow": published["slug"], "version": published["version"], **result}


@app.get("/api/sessions/{session_id}")
def get_session(session_id: str) -> dict[str, Any]:
    session = runtime.store.get(session_id)
    if not session:
        raise HTTPException(404, "Session not found")
    return session


@app.post("/api/sessions/{session_id}/messages")
async def send_session_message(session_id: str, request: MessageRequest, http_request: Request) -> dict[str, Any]:
    try:
        session = runtime.store.get(session_id)
        if not session:
            raise ValueError("Session not found")
        flow = FLOW
        workspace_id = None
        if session.get("flow_id") != FLOW["id"]:
            user = current_user(http_request)
            stored_flow = mongo.get_flow(user["id"], session["flow_id"])
            if not stored_flow or not stored_flow.get("flow"):
                raise ValueError("Workflow version is missing")
            flow = validate_flow(stored_flow["flow"])
            workspace_id = stored_flow.get("workspace_id")
            if workspace_id:
                flow["workspace_id"] = workspace_id
        store = require_mongo()
        if workspace_id:
            store.consume_workspace_execution(str(workspace_id))
        return await runtime.turn(flow, session_id, request.message, runtime_route)
    except PermissionError as exc:
        raise HTTPException(402, str(exc)) from exc
    except (ValueError, RuntimeError) as exc:
        raise HTTPException(400, str(exc))


@app.post("/api/run")
async def run(request: RunRequest) -> dict[str, Any]:
    node = request.node or GRAPH["start_node"]
    if node not in GRAPH["nodes"]:
        raise HTTPException(400, "Unknown node")
    decision = await choose_edge(GRAPH, node, request.message, request.history)
    target = decision["target"]
    return {"source": node, "target": target, "choice": decision["choice"], "decision": decision,
            "node": GRAPH["nodes"][target], "ended": bool(GRAPH["nodes"][target].get("terminal"))}
