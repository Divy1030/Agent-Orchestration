import os
import unittest
from unittest.mock import patch

from backend.github_service import GitHubAuthorizationError, _branch_name, _repository_parts, commit_files
from backend.secure_store import decrypt_secret, encrypt_secret


class GitHubSecurityTests(unittest.TestCase):
    def test_repository_must_be_https_github_without_query_or_fragment(self):
        self.assertEqual(_repository_parts("https://github.com/acme/app.git"), ("acme", "app"))
        for url in (
            "http://github.com/acme/app",
            "https://github.com.evil.example/acme/app",
            "https://github.com/acme/app?redirect=evil",
            "https://github.com/acme/app#fragment",
        ):
            with self.assertRaises(GitHubAuthorizationError):
                _repository_parts(url)

    def test_branch_names_cannot_escape_github_path(self):
        self.assertEqual(_branch_name("feature/agent-1"), "feature/agent-1")
        for branch in ("../main", "feature//main", "feature?x=1", "feature#x", "feature@{1}", ".hidden"):
            with self.assertRaises(GitHubAuthorizationError):
                _branch_name(branch)

    @patch("backend.github_service._github_request")
    def test_commit_validates_branch_before_network_write(self, request):
        with self.assertRaises(GitHubAuthorizationError):
            commit_files("https://github.com/acme/app", "token", "feature?x=1", "message", [{"path": "a.txt", "content": "ok"}])
        request.assert_not_called()


class CredentialSecurityTests(unittest.TestCase):
    @patch.dict(os.environ, {"CREDENTIAL_ENCRYPTION_KEY": ""}, clear=False)
    def test_encryption_requires_server_key(self):
        with self.assertRaisesRegex(RuntimeError, "CREDENTIAL_ENCRYPTION_KEY is not configured"):
            encrypt_secret("secret")

    def test_encryption_round_trip_does_not_store_plaintext(self):
        from cryptography.fernet import Fernet

        with patch.dict(os.environ, {"CREDENTIAL_ENCRYPTION_KEY": Fernet.generate_key().decode()}):
            encrypted = encrypt_secret("github-token-value")
            self.assertNotEqual(encrypted, "github-token-value")
            self.assertEqual(decrypt_secret(encrypted), "github-token-value")


if __name__ == "__main__":
    unittest.main()
