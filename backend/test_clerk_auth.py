import os
import unittest
from unittest.mock import Mock, patch

from starlette.requests import Request

from backend.clerk_auth import clerk_user_from_request


def bearer_request() -> Request:
    return Request({
        "type": "http",
        "method": "GET",
        "scheme": "https",
        "server": ("frontend.example", 443),
        "path": "/api/auth/me",
        "headers": [(b"authorization", b"Bearer test-session")],
    })


class ClerkAuthTests(unittest.TestCase):
    @patch.dict(os.environ, {
        "CLERK_SECRET_KEY": "sk_test_placeholder",
        "CLERK_AUTHORIZED_PARTIES": "https://frontend.example",
    })
    @patch("clerk_backend_api.Clerk")
    def test_existing_clerk_link_returns_original_mongo_user(self, clerk_factory):
        state = Mock(is_signed_in=True, payload={"sub": "user_clerk_123"})
        clerk_factory.return_value.authenticate_request.return_value = state
        mongo = Mock()
        mongo.user_by_clerk_id.return_value = {"id": "mongo_123", "email": "user@example.com", "name": "Existing"}

        user = clerk_user_from_request(bearer_request(), mongo)

        self.assertEqual(user["id"], "mongo_123")
        mongo.link_clerk_identity.assert_not_called()

    @patch.dict(os.environ, {
        "CLERK_SECRET_KEY": "sk_test_placeholder",
        "CLERK_AUTHORIZED_PARTIES": "https://frontend.example",
    })
    @patch("backend.clerk_auth.httpx.get")
    @patch("clerk_backend_api.Clerk")
    def test_first_link_requires_verified_primary_email(self, clerk_factory, http_get):
        clerk_factory.return_value.authenticate_request.return_value = Mock(
            is_signed_in=True, payload={"sub": "user_clerk_456"}
        )
        http_get.return_value.json.return_value = {
            "primary_email_address_id": "email_1",
            "email_addresses": [{
                "id": "email_1",
                "email_address": "USER@example.com",
                "verification": {"status": "verified"},
            }],
            "first_name": "New Clerk Name",
        }
        mongo = Mock()
        mongo.user_by_clerk_id.return_value = None
        mongo.link_clerk_identity.return_value = {"id": "mongo_123", "email": "user@example.com", "name": "Existing"}

        user = clerk_user_from_request(bearer_request(), mongo)

        self.assertEqual(user["id"], "mongo_123")
        mongo.link_clerk_identity.assert_called_once_with("user_clerk_456", "USER@example.com", "New Clerk Name")


if __name__ == "__main__":
    unittest.main()
