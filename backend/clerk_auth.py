"""Clerk session verification and safe linking to existing Mongo accounts."""

from __future__ import annotations

import os
from typing import Any

import httpx
from fastapi import Request

from backend.mongo_store import MongoStore


class ClerkAuthError(Exception):
    def __init__(self, message: str, status_code: int = 401):
        super().__init__(message)
        self.status_code = status_code


def _authorized_parties() -> list[str]:
    return [value.strip().rstrip("/") for value in os.getenv("CLERK_AUTHORIZED_PARTIES", "").split(",") if value.strip()]


def _registrations_enabled() -> bool:
    configured = os.getenv("ALLOW_NEW_REGISTRATIONS")
    if configured is not None:
        return configured.strip().lower() in {"1", "true", "yes", "on"}
    return not bool(os.getenv("RENDER"))


def clerk_user_from_request(request: Request, store: MongoStore) -> dict[str, Any] | None:
    """Return a Mongo user for a valid Clerk bearer session, or None if absent.

    Existing Mongo user IDs remain canonical. First use of a Clerk identity is
    linked only when Clerk confirms the primary email is verified and it
    exactly matches a pre-existing Mongo account. New identities are provisioned
    only when ALLOW_NEW_REGISTRATIONS is explicitly enabled.
    """
    authorization = request.headers.get("authorization", "")
    if not authorization.lower().startswith("bearer "):
        return None

    secret_key = os.getenv("CLERK_SECRET_KEY", "").strip()
    parties = _authorized_parties()
    if not secret_key or not parties:
        raise ClerkAuthError("Clerk authentication is not fully configured", 503)

    try:
        from clerk_backend_api import Clerk
        from clerk_backend_api.security.types import AuthenticateRequestOptions

        token_request = httpx.Request(
            method=request.method,
            url=str(request.url),
            headers=request.headers.raw,
        )
        clerk = Clerk(bearer_auth=secret_key)
        state = clerk.authenticate_request(
            token_request,
            AuthenticateRequestOptions(secret_key=secret_key, authorized_parties=parties),
        )
    except Exception as exc:
        raise ClerkAuthError("Could not verify Clerk session") from exc

    if not state.is_signed_in or not isinstance(state.payload, dict):
        raise ClerkAuthError("Invalid or expired Clerk session")
    clerk_user_id = state.payload.get("sub")
    if not isinstance(clerk_user_id, str) or not clerk_user_id:
        raise ClerkAuthError("Clerk session is missing its user identity")

    existing = store.user_by_clerk_id(clerk_user_id)
    if existing:
        return existing

    try:
        response = httpx.get(
            f"https://api.clerk.com/v1/users/{clerk_user_id}",
            headers={"Authorization": f"Bearer {secret_key}"},
            timeout=8,
        )
        response.raise_for_status()
        profile = response.json()
    except Exception as exc:
        raise ClerkAuthError("Could not load the verified Clerk profile", 503) from exc

    primary_id = profile.get("primary_email_address_id")
    primary = next((item for item in profile.get("email_addresses", []) if item.get("id") == primary_id), None)
    verification = (primary or {}).get("verification") or {}
    email = (primary or {}).get("email_address", "")
    if not email or verification.get("status") != "verified":
        raise ClerkAuthError("A verified primary email is required to link this account", 403)

    name = " ".join(part for part in (profile.get("first_name"), profile.get("last_name")) if part).strip()
    user = store.link_clerk_identity(clerk_user_id, email, name)
    if not user and _registrations_enabled():
        try:
            user = store.create_clerk_user(clerk_user_id, email, name)
            if not store.workspaces_for_user(user["id"]):
                store.create_workspace(user["id"], "My Workspace")
        except ValueError as exc:
            raise ClerkAuthError(str(exc), 409) from exc
    if not user:
        raise ClerkAuthError("This Clerk account is not linked to an existing workspace account", 403)
    return user
