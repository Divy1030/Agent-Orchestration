"""MongoDB persistence for identity, workspaces, flows, tools, and sessions."""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import secrets
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any


def now() -> datetime:
    return datetime.now(timezone.utc)


SPACE_LIMITS = {
    "free": {"spaces": 2, "members": 5},
    "ad-astra": {"spaces": 5, "members": 15},
    "pro": {"spaces": 15, "members": 50},
    "team": {"spaces": 100, "members": 250},
}


def hash_password(password: str) -> str:
    salt = secrets.token_bytes(16)
    digest = hashlib.scrypt(password.encode(), salt=salt, n=2**14, r=8, p=1)
    return f"scrypt$16384$8$1${salt.hex()}${digest.hex()}"


def verify_password(password: str, encoded: str) -> bool:
    try:
        algorithm, n, r, p, salt_hex, digest_hex = encoded.split("$")
        if algorithm != "scrypt":
            return False
        actual = hashlib.scrypt(password.encode(), salt=bytes.fromhex(salt_hex), n=int(n), r=int(r), p=int(p))
        return hmac.compare_digest(actual.hex(), digest_hex)
    except (ValueError, TypeError):
        return False


class MongoStore:
    def __init__(self, uri: str | None = None, database: str | None = None):
        self.uri = uri or os.getenv("MONGODB_URI", "")
        self.database_name = database or os.getenv("MONGODB_DATABASE", "agent_orchestration")
        self._db = None

    @property
    def configured(self) -> bool:
        return bool(self.uri)

    @property
    def db(self):
        if not self.configured:
            raise RuntimeError("MONGODB_URI is not configured")
        if self._db is None:
            from pymongo import MongoClient
            client = MongoClient(self.uri, serverSelectionTimeoutMS=5000, connectTimeoutMS=5000, tz_aware=True)
            self._db = client[self.database_name]
            self._ensure_indexes()
        return self._db

    def _ensure_indexes(self) -> None:
        self._db.users.create_index("email", unique=True)
        # A Clerk identity is linked onto the existing Mongo user document.
        # The sparse unique index leaves legacy users untouched and prevents
        # accidentally associating one Clerk account with multiple records.
        self._db.users.create_index("clerk_user_id", unique=True, sparse=True)
        self._db.workspaces.create_index("slug", unique=True)
        self._db.memberships.create_index([("workspace_id", 1), ("user_id", 1)], unique=True)
        self._db.workspaces.create_index("join_code_hash", unique=True, sparse=True)
        self._db.space_projects.create_index("workspace_id", unique=True)
        self._db.space_agents.create_index([("workspace_id", 1), ("agent_id", 1)], unique=True)
        self._db.space_contexts.create_index("workspace_id", unique=True)
        self._db.space_activity.create_index([("workspace_id", 1), ("created_at", -1)])
        self._db.space_memory.create_index([("workspace_id", 1), ("created_at", -1)])
        self._db.space_skills.create_index([("workspace_id", 1), ("name", 1)], unique=True)
        self._db.space_tasks.create_index([("workspace_id", 1), ("updated_at", -1)])
        self._db.space_approvals.create_index([("workspace_id", 1), ("status", 1), ("created_at", -1)])
        self._db.space_agent_presence.create_index("workspace_id")
        self._db.flows.create_index([("workspace_id", 1), ("slug", 1)], unique=True)
        self._db.flow_versions.create_index([("flow_id", 1), ("version", 1)], unique=True)
        self._db.auth_sessions.create_index("token_hash", unique=True)
        self._db.auth_sessions.create_index("expires_at", expireAfterSeconds=0)
        self._db.flows.create_index("public_token_hash", sparse=True, unique=True)
        self._db.tools.create_index([("workspace_id", 1), ("name", 1)], unique=True)
        self._db.agents.create_index([("workspace_id", 1), ("name", 1)], unique=True)
        self._db.connectors.create_index([("workspace_id", 1), ("name", 1)], unique=True)
        self._db.credential_refs.create_index([("workspace_id", 1), ("name", 1)], unique=True)
        self._db.workspace_credentials.create_index([("workspace_id", 1), ("provider", 1)], unique=True)
        self._db.promo_redemptions.create_index([("user_id", 1), ("code", 1)], unique=True)

    def create_user(self, email: str, password: str, name: str) -> dict[str, Any]:
        from pymongo.errors import DuplicateKeyError
        doc = {"email": email.lower().strip(), "name": name.strip() or email.split("@")[0],
               "password_hash": hash_password(password), "created_at": now(),
               "subscription": {"plan": "free", "status": "active", "billing_interval": "month",
                                "paid": False, "currency": "USD", "amount": 0,
                                "purchased_at": None, "current_period_start": now(),
                                "current_period_end": now() + timedelta(days=30),
                                "next_billing_at": now() + timedelta(days=30),
                                "cancel_at_period_end": False, "promo_code": None,
                                "promo_redeemed_at": None, "provider": None},
               "usage": {"workflow_runs": 0, "period_started_at": now()}}
        try:
            result = self.db.users.insert_one(doc)
        except DuplicateKeyError as exc:
            raise ValueError("An account with this email already exists") from exc
        doc["_id"] = result.inserted_id
        return self.public_user(doc)

    def user_by_email(self, email: str) -> dict[str, Any] | None:
        doc = self.db.users.find_one({"email": email.lower().strip()})
        return self.public_user(doc) if doc else None

    def user_by_clerk_id(self, clerk_user_id: str) -> dict[str, Any] | None:
        doc = self.db.users.find_one({"clerk_user_id": clerk_user_id})
        return self.public_user(doc) if doc else None

    def clerk_identity_for_user(self, user_id: str) -> str | None:
        from bson import ObjectId
        doc = self.db.users.find_one({"_id": ObjectId(user_id)}, {"clerk_user_id": 1})
        return doc.get("clerk_user_id") if doc else None

    def link_clerk_identity(self, clerk_user_id: str, email: str, name: str = "") -> dict[str, Any] | None:
        """Link a verified Clerk identity to an existing account, never create one."""
        from pymongo.errors import DuplicateKeyError

        normalized_email = email.lower().strip()
        existing = self.db.users.find_one({"clerk_user_id": clerk_user_id})
        if existing:
            return self.public_user(existing)
        legacy = self.db.users.find_one({"email": normalized_email})
        if not legacy:
            return None
        try:
            result = self.db.users.update_one(
                {"_id": legacy["_id"], "$or": [
                    {"clerk_user_id": {"$exists": False}},
                    {"clerk_user_id": None},
                    {"clerk_user_id": clerk_user_id},
                ]},
                {"$set": {"clerk_user_id": clerk_user_id}},
            )
        except DuplicateKeyError:
            return None
        if not result.matched_count:
            return None
        linked = self.db.users.find_one({"_id": legacy["_id"]})
        return self.public_user(linked) if linked else None

    def create_clerk_user(self, clerk_user_id: str, email: str, name: str = "") -> dict[str, Any]:
        """Create a Mongo identity for a verified Clerk user during open signup."""
        from pymongo.errors import DuplicateKeyError

        normalized_email = email.lower().strip()
        try:
            result = self.db.users.insert_one({
                "email": normalized_email,
                "name": name.strip() or normalized_email.split("@")[0],
                "clerk_user_id": clerk_user_id,
                "created_at": now(),
            })
        except DuplicateKeyError:
            linked = self.link_clerk_identity(clerk_user_id, normalized_email, name)
            if linked:
                return linked
            raise ValueError("An account with this email already exists")
        return self.public_user(self.db.users.find_one({"_id": result.inserted_id}))

    def user_for_login(self, email: str) -> dict[str, Any] | None:
        doc = self.db.users.find_one({"email": email.lower().strip()})
        if doc:
            doc["id"] = str(doc["_id"])
        return doc

    @staticmethod
    def public_user(doc: dict[str, Any]) -> dict[str, Any]:
        return {"id": str(doc.get("_id", doc.get("id"))), "email": doc["email"], "name": doc.get("name", ""),
                "subscription": doc.get("subscription", {"plan": "free", "status": "active", "paid": False}),
                "usage": doc.get("usage", {})}

    def subscription_for_user(self, user_id: str) -> dict[str, Any]:
        from bson import ObjectId
        doc = self.db.users.find_one({"_id": ObjectId(user_id)}, {"subscription": 1, "usage": 1}) or {}
        subscription = doc.get("subscription", {})
        plan = subscription.get("plan", "free")
        limits = {"free": 25, "ad-astra": 500, "pro": 1000, "team": 5000}
        return {"subscription": subscription, "usage": doc.get("usage", {}), "limits": {"workflow_runs": limits.get(plan, 25)}}

    def consume_workspace_execution(self, workspace_id: str) -> dict[str, Any]:
        """Atomically consume one workflow execution from the workspace owner's allowance."""
        from bson import ObjectId
        workspace = self.db.workspaces.find_one({"_id": ObjectId(workspace_id)}, {"owner_id": 1})
        if not workspace:
            raise ValueError("Workspace not found")
        user = self.db.users.find_one({"_id": workspace["owner_id"]}) or {}
        subscription = user.get("subscription", {})
        plan = subscription.get("plan", "free")
        limits = {"free": 25, "ad-astra": 500, "pro": 1000, "team": 5000}
        limit = limits.get(plan, 25)
        timestamp = now()
        period_end = subscription.get("current_period_end")
        if not period_end or period_end <= timestamp:
            period_start = timestamp
            period_end = timestamp + timedelta(days=30)
            self.db.users.update_one({"_id": user["_id"]}, {"$set": {"usage.workflow_runs": 0, "usage.period_started_at": period_start,
                                                                        "subscription.current_period_start": period_start,
                                                                        "subscription.current_period_end": period_end,
                                                                        "subscription.next_billing_at": period_end}})
        from pymongo import ReturnDocument
        result = self.db.users.find_one_and_update(
            {"_id": user["_id"], "usage.workflow_runs": {"$lt": limit}},
            {"$inc": {"usage.workflow_runs": 1}, "$set": {"usage.last_workflow_run_at": timestamp}},
            return_document=ReturnDocument.AFTER,
            projection={"usage": 1, "subscription": 1},
        )
        if not result:
            raise PermissionError(f"Monthly workflow execution limit reached ({limit}). Upgrade your subscription to continue.")
        return {"used": result.get("usage", {}).get("workflow_runs", 0), "limit": limit,
                "plan": result.get("subscription", {}).get("plan", plan)}

    def redeem_promo(self, user_id: str, code: str) -> dict[str, Any]:
        from bson import ObjectId
        from pymongo.errors import DuplicateKeyError
        normalized = code.strip().lower()
        if normalized != "ad-astra26":
            raise ValueError("That promo code is not valid")
        user = self.db.users.find_one({"_id": ObjectId(user_id)})
        if not user:
            raise ValueError("User not found")
        existing = self.db.promo_redemptions.find_one({"user_id": ObjectId(user_id), "code": normalized})
        if existing:
            raise ValueError("This promo code has already been redeemed")
        start = now()
        end = start + timedelta(days=30)
        subscription = {**user.get("subscription", {}), "plan": "ad-astra", "status": "active", "paid": False,
                        "currency": "USD", "amount": 0, "billing_interval": "month", "purchased_at": start,
                        "current_period_start": start, "current_period_end": end, "next_billing_at": end,
                        "cancel_at_period_end": False, "promo_code": normalized, "promo_redeemed_at": start,
                        "provider": "promo"}
        try:
            self.db.promo_redemptions.insert_one({"user_id": ObjectId(user_id), "code": normalized, "redeemed_at": start,
                                                  "period_end": end})
        except DuplicateKeyError as exc:
            raise ValueError("This promo code has already been redeemed") from exc
        self.db.users.update_one({"_id": ObjectId(user_id)}, {"$set": {"subscription": subscription}})
        return subscription

    def workspace_credentials(self, user_id: str, workspace_id: str) -> list[dict[str, Any]]:
        if not self.is_member(user_id, workspace_id):
            raise PermissionError("Workspace access denied")
        from bson import ObjectId
        return [{"provider": row["provider"], "label": row.get("label", row["provider"]),
                 "last_four": row.get("last_four", ""), "created_at": row.get("created_at"),
                 "updated_at": row.get("updated_at"), "last_tested_at": row.get("last_tested_at")}
                for row in self.db.workspace_credentials.find({"workspace_id": ObjectId(workspace_id), "provider": "openrouter"})]

    def save_workspace_credential(self, user_id: str, workspace_id: str, provider: str, encrypted_value: str, label: str, last_four: str) -> dict[str, Any]:
        if not self.is_member(user_id, workspace_id):
            raise PermissionError("Workspace access denied")
        from bson import ObjectId
        timestamp = now()
        self.db.workspace_credentials.update_one(
            {"workspace_id": ObjectId(workspace_id), "provider": provider},
            {"$set": {"encrypted_value": encrypted_value, "label": label, "last_four": last_four,
                      "updated_at": timestamp}, "$setOnInsert": {"created_at": timestamp}}, upsert=True)
        return {"provider": provider, "label": label, "last_four": last_four, "updated_at": timestamp}

    def get_workspace_credential(self, user_id: str, workspace_id: str, provider: str) -> dict[str, Any] | None:
        if not self.is_member(user_id, workspace_id):
            raise PermissionError("Workspace access denied")
        from bson import ObjectId
        return self.db.workspace_credentials.find_one({"workspace_id": ObjectId(workspace_id), "provider": provider})

    def remove_workspace_credential(self, user_id: str, workspace_id: str, provider: str) -> None:
        if not self.is_member(user_id, workspace_id):
            raise PermissionError("Workspace access denied")
        from bson import ObjectId
        self.db.workspace_credentials.delete_one({"workspace_id": ObjectId(workspace_id), "provider": provider})

    def create_workspace(self, user_id: str, name: str) -> dict[str, Any]:
        from bson import ObjectId
        user = self.db.users.find_one({"_id": ObjectId(user_id)}) or {}
        plan = user.get("subscription", {}).get("plan", "free")
        max_spaces = SPACE_LIMITS.get(plan, SPACE_LIMITS["free"])["spaces"]
        if self.db.memberships.count_documents({"user_id": ObjectId(user_id), "role": "owner"}) >= max_spaces:
            raise PermissionError(f"Your {plan} plan allows up to {max_spaces} Spaces")
        slug = "-".join(name.lower().split()) or "workspace"
        existing = self.db.workspaces.count_documents({"slug": {"$regex": f"^{slug}"}})
        if existing:
            slug = f"{slug}-{existing + 1}"
        join_code = secrets.token_urlsafe(8).replace("-", "").replace("_", "")[:10].upper()
        doc = {"name": name.strip() or "Workspace", "slug": slug, "owner_id": ObjectId(user_id),
               "join_code_hash": hashlib.sha256(join_code.encode()).hexdigest(), "created_at": now()}
        result = self.db.workspaces.insert_one(doc)
        doc["id"] = str(result.inserted_id)
        self.db.memberships.insert_one({"workspace_id": result.inserted_id, "user_id": ObjectId(user_id), "role": "owner", "created_at": now()})
        return {"id": doc["id"], "name": doc["name"], "slug": doc["slug"], "role": "owner", "join_code": join_code}

    def regenerate_join_code(self, user_id: str, workspace_id: str) -> dict[str, Any]:
        from bson import ObjectId
        workspace = self.db.workspaces.find_one({"_id": ObjectId(workspace_id), "owner_id": ObjectId(user_id)})
        if not workspace:
            raise PermissionError("Only the Space owner can rotate the join code")
        join_code = secrets.token_urlsafe(8).replace("-", "").replace("_", "")[:10].upper()
        self.db.workspaces.update_one({"_id": ObjectId(workspace_id)}, {"$set": {"join_code_hash": hashlib.sha256(join_code.encode()).hexdigest(), "join_code_updated_at": now()}})
        return {"workspace_id": workspace_id, "join_code": join_code}

    def join_workspace(self, user_id: str, code: str) -> dict[str, Any]:
        from bson import ObjectId
        workspace = self.db.workspaces.find_one({"join_code_hash": hashlib.sha256(code.strip().upper().encode()).hexdigest()})
        if not workspace:
            raise ValueError("Invalid or expired Space join code")
        if self.db.memberships.find_one({"workspace_id": workspace["_id"], "user_id": ObjectId(user_id)}):
            raise ValueError("You are already a member of this Space")
        owner = self.db.users.find_one({"_id": workspace["owner_id"]}) or {}
        plan = owner.get("subscription", {}).get("plan", "free")
        max_members = SPACE_LIMITS.get(plan, SPACE_LIMITS["free"])["members"]
        if self.db.memberships.count_documents({"workspace_id": workspace["_id"]}) >= max_members:
            raise PermissionError("This Space has reached its member limit")
        self.db.memberships.insert_one({"workspace_id": workspace["_id"], "user_id": ObjectId(user_id), "role": "member", "created_at": now()})
        return {"id": str(workspace["_id"]), "name": workspace["name"], "slug": workspace["slug"], "role": "member"}

    def space_members(self, user_id: str, workspace_id: str) -> list[dict[str, Any]]:
        from bson import ObjectId
        if not self.is_member(user_id, workspace_id):
            raise PermissionError("Space access denied")
        rows = self.db.memberships.aggregate([{"$match": {"workspace_id": ObjectId(workspace_id)}}, {"$lookup": {"from": "users", "localField": "user_id", "foreignField": "_id", "as": "user"}}, {"$unwind": "$user"}])
        return [{"user_id": str(row["user_id"]), "name": row["user"].get("name", ""), "email": row["user"].get("email", ""), "role": row["role"]} for row in rows]

    @staticmethod
    def _space_agent_public(agent: dict[str, Any], assignment: dict[str, Any] | None = None, source_space: str | None = None) -> dict[str, Any]:
        config = agent.get("config", {})
        result = {"id": str(agent["_id"]), "name": agent.get("name", ""), "description": agent.get("description", ""),
                  "model": config.get("model"), "provider": config.get("provider", "OpenRouter"),
                  "source_space": source_space}
        if assignment:
            result.update({"role": assignment.get("role", "coder"), "assigned_at": assignment.get("created_at")})
        return result

    def space_agents(self, user_id: str, workspace_id: str) -> list[dict[str, Any]]:
        from bson import ObjectId
        if not self.is_member(user_id, workspace_id):
            raise PermissionError("Space access denied")
        rows = self.db.space_agents.aggregate([
            {"$match": {"workspace_id": ObjectId(workspace_id)}},
            {"$lookup": {"from": "agents", "localField": "agent_id", "foreignField": "_id", "as": "agent"}},
            {"$unwind": "$agent"},
        ])
        result = []
        for row in rows:
            agent = self._space_agent_public(row["agent"], row)
            presence = self.db.space_agent_presence.find_one({"workspace_id": ObjectId(workspace_id), "agent_id": row["agent"]["_id"]}) or {}
            agent["status"] = presence.get("status", "idle")
            agent["last_activity_at"] = presence.get("updated_at")
            result.append(agent)
        return result

    def set_agent_presence(self, workspace_id: str, agent_id: str, status: str) -> None:
        from bson import ObjectId
        self.db.space_agent_presence.update_one(
            {"workspace_id": ObjectId(workspace_id), "agent_id": ObjectId(agent_id)},
            {"$set": {"status": status, "updated_at": now()}}, upsert=True,
        )

    def record_space_activity(self, workspace_id: str, event_type: str, message: str, agent_id: str | None = None, metadata: dict[str, Any] | None = None) -> None:
        from bson import ObjectId
        self.db.space_activity.insert_one({"workspace_id": ObjectId(workspace_id), "event_type": event_type,
                                           "message": message, "agent_id": ObjectId(agent_id) if agent_id else None,
                                           "metadata": metadata or {}, "created_at": now()})

    def space_context(self, user_id: str, workspace_id: str) -> dict[str, Any]:
        from bson import ObjectId
        if not self.is_member(user_id, workspace_id):
            raise PermissionError("Space access denied")
        row = self.db.space_contexts.find_one({"workspace_id": ObjectId(workspace_id)})
        return {"project_goal": "", "guidelines": [], "patterns": [], "instructions": [], "version": 0} if not row else {
            "project_goal": row.get("project_goal", ""), "guidelines": row.get("guidelines", []),
            "patterns": row.get("patterns", []), "instructions": row.get("instructions", []), "version": row.get("version", 1),
        }

    def save_space_context(self, user_id: str, workspace_id: str, context: dict[str, Any]) -> dict[str, Any]:
        from bson import ObjectId
        membership = self.db.memberships.find_one({"workspace_id": ObjectId(workspace_id), "user_id": ObjectId(user_id)})
        if not membership or membership.get("role") not in {"owner", "admin"}:
            raise PermissionError("Only Space owners and admins can edit project context")
        current = self.db.space_contexts.find_one({"workspace_id": ObjectId(workspace_id)}) or {}
        version = int(current.get("version", 0)) + 1
        document = {"workspace_id": ObjectId(workspace_id), **context, "version": version, "updated_by": ObjectId(user_id), "updated_at": now()}
        self.db.space_contexts.update_one({"workspace_id": ObjectId(workspace_id)}, {"$set": document, "$setOnInsert": {"created_at": now()}}, upsert=True)
        self.record_space_activity(workspace_id, "context_updated", f"Project context updated to version {version}", metadata={"version": version})
        return self.space_context(user_id, workspace_id)

    @staticmethod
    def _space_manager(membership: dict[str, Any] | None) -> bool:
        return bool(membership and membership.get("role") in {"owner", "admin"})

    def _space_membership(self, user_id: str, workspace_id: str) -> dict[str, Any] | None:
        from bson import ObjectId
        return self.db.memberships.find_one({"workspace_id": ObjectId(workspace_id), "user_id": ObjectId(user_id)})

    @staticmethod
    def _public_memory(row: dict[str, Any]) -> dict[str, Any]:
        return {"id": str(row["_id"]), "kind": row.get("kind", "fact"), "content": row.get("content", ""),
                "source": row.get("source", "manual"), "created_at": row.get("created_at"),
                "updated_at": row.get("updated_at")}

    def space_memory(self, user_id: str, workspace_id: str) -> list[dict[str, Any]]:
        if not self.is_member(user_id, workspace_id):
            raise PermissionError("Space access denied")
        return [self._public_memory(row) for row in self.db.space_memory.find({"workspace_id": __import__("bson").ObjectId(workspace_id)}).sort("created_at", -1).limit(100)]

    def add_space_memory(self, user_id: str, workspace_id: str, kind: str, content: str, source: str) -> dict[str, Any]:
        membership = self._space_membership(user_id, workspace_id)
        if not self._space_manager(membership):
            raise PermissionError("Only Space owners and admins can add shared project memory")
        from bson import ObjectId
        timestamp = now()
        row = {"workspace_id": ObjectId(workspace_id), "kind": kind, "content": content.strip(), "source": source.strip() or "manual",
               "created_by": ObjectId(user_id), "created_at": timestamp, "updated_at": timestamp}
        result = self.db.space_memory.insert_one(row)
        row["_id"] = result.inserted_id
        self.record_space_activity(workspace_id, "memory_added", f"Shared {kind} added to project memory")
        return self._public_memory(row)

    def delete_space_memory(self, user_id: str, workspace_id: str, memory_id: str) -> None:
        membership = self._space_membership(user_id, workspace_id)
        if not self._space_manager(membership):
            raise PermissionError("Only Space owners and admins can remove shared project memory")
        from bson import ObjectId
        result = self.db.space_memory.delete_one({"_id": ObjectId(memory_id), "workspace_id": ObjectId(workspace_id)})
        if not result.deleted_count:
            raise ValueError("Project memory entry not found")

    @staticmethod
    def _public_skill(row: dict[str, Any]) -> dict[str, Any]:
        return {"id": str(row["_id"]), "name": row.get("name", ""), "description": row.get("description", ""),
                "instructions": row.get("instructions", []), "patterns": row.get("patterns", []),
                "enabled": bool(row.get("enabled", True)), "updated_at": row.get("updated_at")}

    def space_skills(self, user_id: str, workspace_id: str) -> list[dict[str, Any]]:
        if not self.is_member(user_id, workspace_id):
            raise PermissionError("Space access denied")
        return [self._public_skill(row) for row in self.db.space_skills.find({"workspace_id": __import__("bson").ObjectId(workspace_id)}).sort("name", 1)]

    def save_space_skill(self, user_id: str, workspace_id: str, skill: dict[str, Any]) -> dict[str, Any]:
        membership = self._space_membership(user_id, workspace_id)
        if not self._space_manager(membership):
            raise PermissionError("Only Space owners and admins can manage reusable skills")
        from bson import ObjectId
        timestamp = now()
        clean = {key: value for key, value in skill.items() if key in {"name", "description", "instructions", "patterns"}}
        clean["name"] = clean["name"].strip()
        clean["description"] = clean.get("description", "").strip()
        clean["instructions"] = [str(item).strip() for item in clean.get("instructions", []) if str(item).strip()][:30]
        clean["patterns"] = [str(item).strip() for item in clean.get("patterns", []) if str(item).strip()][:30]
        clean.update({"workspace_id": ObjectId(workspace_id), "enabled": True, "updated_by": ObjectId(user_id), "updated_at": timestamp})
        self.db.space_skills.update_one({"workspace_id": ObjectId(workspace_id), "name": clean["name"]}, {"$set": clean, "$setOnInsert": {"created_at": timestamp}}, upsert=True)
        row = self.db.space_skills.find_one({"workspace_id": ObjectId(workspace_id), "name": clean["name"]})
        self.record_space_activity(workspace_id, "skill_updated", f"Reusable skill updated: {clean['name']}")
        return self._public_skill(row)

    def delete_space_skill(self, user_id: str, workspace_id: str, skill_id: str) -> None:
        membership = self._space_membership(user_id, workspace_id)
        if not self._space_manager(membership):
            raise PermissionError("Only Space owners and admins can remove reusable skills")
        from bson import ObjectId
        result = self.db.space_skills.delete_one({"_id": ObjectId(skill_id), "workspace_id": ObjectId(workspace_id)})
        if not result.deleted_count:
            raise ValueError("Reusable skill not found")

    def space_tasks(self, user_id: str, workspace_id: str) -> list[dict[str, Any]]:
        if not self.is_member(user_id, workspace_id):
            raise PermissionError("Space access denied")
        return [self._public_task(row) for row in self.db.space_tasks.find({"workspace_id": __import__("bson").ObjectId(workspace_id)}).sort("updated_at", -1).limit(100)]

    @staticmethod
    def _public_task(row: dict[str, Any]) -> dict[str, Any]:
        return {"id": str(row["_id"]), "title": row.get("title", ""), "description": row.get("description", ""),
                "status": row.get("status", "todo"), "priority": row.get("priority", "normal"),
                "assignee_agent_id": str(row["assignee_agent_id"]) if row.get("assignee_agent_id") else None,
                "created_at": row.get("created_at"), "updated_at": row.get("updated_at")}

    def create_space_task(self, user_id: str, workspace_id: str, task: dict[str, Any]) -> dict[str, Any]:
        membership = self._space_membership(user_id, workspace_id)
        if not membership:
            raise PermissionError("Space access denied")
        from bson import ObjectId
        agent_id = ObjectId(task["assignee_agent_id"]) if task.get("assignee_agent_id") else None
        if agent_id and not self.db.space_agents.find_one({"workspace_id": ObjectId(workspace_id), "agent_id": agent_id}):
            raise ValueError("The selected agent is not assigned to this Space")
        timestamp = now()
        row = {"workspace_id": ObjectId(workspace_id), "title": task["title"].strip(), "description": task.get("description", "").strip(),
               "status": task.get("status", "todo"), "priority": task.get("priority", "normal"), "assignee_agent_id": agent_id,
               "created_by": ObjectId(user_id), "created_at": timestamp, "updated_at": timestamp}
        result = self.db.space_tasks.insert_one(row)
        row["_id"] = result.inserted_id
        self.record_space_activity(workspace_id, "task_created", f"Task created: {row['title']}")
        return self._public_task(row)

    def update_space_task_status(self, user_id: str, workspace_id: str, task_id: str, status: str) -> dict[str, Any]:
        membership = self._space_membership(user_id, workspace_id)
        if not membership:
            raise PermissionError("Space access denied")
        from bson import ObjectId
        result = self.db.space_tasks.update_one({"_id": ObjectId(task_id), "workspace_id": ObjectId(workspace_id)}, {"$set": {"status": status, "updated_at": now()}})
        if not result.matched_count:
            raise ValueError("Task not found")
        row = self.db.space_tasks.find_one({"_id": ObjectId(task_id)})
        self.record_space_activity(workspace_id, "task_updated", f"Task status changed: {row.get('title', '')} → {status}")
        return self._public_task(row)

    def space_approvals(self, user_id: str, workspace_id: str) -> list[dict[str, Any]]:
        if not self.is_member(user_id, workspace_id):
            raise PermissionError("Space access denied")
        return [{"id": str(row["_id"]), "action_type": row.get("action_type", ""), "title": row.get("title", ""),
                 "details": row.get("details", ""), "task_id": str(row["task_id"]) if row.get("task_id") else None,
                 "status": row.get("status", "pending"), "created_at": row.get("created_at"), "decided_at": row.get("decided_at")}
                for row in self.db.space_approvals.find({"workspace_id": __import__("bson").ObjectId(workspace_id)}).sort("created_at", -1).limit(100)]

    def request_space_approval(self, user_id: str, workspace_id: str, request: dict[str, Any]) -> dict[str, Any]:
        if not self.is_member(user_id, workspace_id):
            raise PermissionError("Space access denied")
        from bson import ObjectId
        timestamp = now()
        row = {"workspace_id": ObjectId(workspace_id), "action_type": request["action_type"].strip(), "title": request["title"].strip(),
               "details": request.get("details", "").strip(), "task_id": ObjectId(request["task_id"]) if request.get("task_id") else None,
               "status": "pending", "requested_by": ObjectId(user_id), "created_at": timestamp}
        result = self.db.space_approvals.insert_one(row)
        row["_id"] = result.inserted_id
        self.record_space_activity(workspace_id, "approval_requested", f"Approval requested: {row['title']}")
        return {"id": str(result.inserted_id), "action_type": row["action_type"], "title": row["title"], "details": row["details"], "status": row["status"], "created_at": timestamp}

    def decide_space_approval(self, user_id: str, workspace_id: str, approval_id: str, status: str) -> dict[str, Any]:
        membership = self._space_membership(user_id, workspace_id)
        if not self._space_manager(membership):
            raise PermissionError("Only Space owners and admins can decide approvals")
        from bson import ObjectId
        timestamp = now()
        result = self.db.space_approvals.update_one({"_id": ObjectId(approval_id), "workspace_id": ObjectId(workspace_id), "status": "pending"}, {"$set": {"status": status, "decided_by": ObjectId(user_id), "decided_at": timestamp}})
        if not result.matched_count:
            raise ValueError("Pending approval not found")
        row = self.db.space_approvals.find_one({"_id": ObjectId(approval_id)})
        self.record_space_activity(workspace_id, "approval_decided", f"Approval {status}: {row.get('title', '')}")
        return {"id": str(row["_id"]), "status": row["status"], "title": row.get("title", ""), "decided_at": row.get("decided_at")}

    def shared_agent_context(self, workspace_id: str) -> dict[str, Any]:
        from bson import ObjectId
        memory = [self._public_memory(row) for row in self.db.space_memory.find({"workspace_id": ObjectId(workspace_id)}).sort("created_at", -1).limit(50)]
        skills = [self._public_skill(row) for row in self.db.space_skills.find({"workspace_id": ObjectId(workspace_id), "enabled": True}).sort("name", 1).limit(30)]
        tasks = [self._public_task(row) for row in self.db.space_tasks.find({"workspace_id": ObjectId(workspace_id), "status": {"$ne": "done"}}).sort("updated_at", -1).limit(50)]
        return {"memory": memory, "skills": skills, "open_tasks": tasks}

    def space_project_dashboard(self, user_id: str, workspace_id: str) -> dict[str, Any]:
        from bson import ObjectId
        detail = self.space_detail(user_id, workspace_id)
        agents = self.space_agents(user_id, workspace_id)
        events = []
        for row in self.db.space_activity.find({"workspace_id": ObjectId(workspace_id)}).sort("created_at", -1).limit(30):
            events.append({"id": str(row["_id"]), "type": row.get("event_type"), "message": row.get("message", ""),
                           "agent_id": str(row["agent_id"]) if row.get("agent_id") else None, "created_at": row.get("created_at")})
        working = any(agent.get("status") == "working" for agent in agents)
        state = "not_connected" if not detail.get("project") else ("in_progress" if working else "ready")
        return {"space": detail, "agents": agents, "context": self.space_context(user_id, workspace_id),
                "memory": self.space_memory(user_id, workspace_id), "skills": self.space_skills(user_id, workspace_id),
                "tasks": self.space_tasks(user_id, workspace_id), "approvals": self.space_approvals(user_id, workspace_id),
                "activity": events, "project_state": state}

    def available_agents_for_space(self, user_id: str, workspace_id: str) -> list[dict[str, Any]]:
        from bson import ObjectId
        if not self.is_member(user_id, workspace_id):
            raise PermissionError("Space access denied")
        memberships = list(self.db.memberships.find({"user_id": ObjectId(user_id)}, {"workspace_id": 1}))
        workspace_ids = [row["workspace_id"] for row in memberships]
        assigned = {row["agent_id"] for row in self.db.space_agents.find({"workspace_id": ObjectId(workspace_id)}, {"agent_id": 1})}
        spaces = {row["_id"]: row.get("name", "") for row in self.db.workspaces.find({"_id": {"$in": workspace_ids}}, {"name": 1})}
        return [self._space_agent_public(agent, source_space=spaces.get(agent.get("workspace_id"), ""))
                for agent in self.db.agents.find({"workspace_id": {"$in": workspace_ids}, "_id": {"$nin": list(assigned)}}).sort("name", 1)]

    def assign_space_agent(self, user_id: str, workspace_id: str, agent_id: str, role: str) -> dict[str, Any]:
        from bson import ObjectId
        if not self.db.workspaces.find_one({"_id": ObjectId(workspace_id), "owner_id": ObjectId(user_id)}) and not self.db.memberships.find_one({"workspace_id": ObjectId(workspace_id), "user_id": ObjectId(user_id), "role": "admin"}):
            raise PermissionError("Only Space owners and admins can assign agents")
        agent = self.db.agents.find_one({"_id": ObjectId(agent_id)})
        if not agent or not self.db.memberships.find_one({"workspace_id": agent["workspace_id"], "user_id": ObjectId(user_id)}):
            raise ValueError("Agent is not available to this user")
        assignment = {"workspace_id": ObjectId(workspace_id), "agent_id": ObjectId(agent_id), "role": role, "created_at": now(), "updated_at": now()}
        self.db.space_agents.update_one({"workspace_id": ObjectId(workspace_id), "agent_id": ObjectId(agent_id)}, {"$set": assignment}, upsert=True)
        return self._space_agent_public(agent, assignment)

    def update_space_agent_role(self, user_id: str, workspace_id: str, agent_id: str, role: str) -> None:
        from bson import ObjectId
        if not self.db.workspaces.find_one({"_id": ObjectId(workspace_id), "owner_id": ObjectId(user_id)}) and not self.db.memberships.find_one({"workspace_id": ObjectId(workspace_id), "user_id": ObjectId(user_id), "role": "admin"}):
            raise PermissionError("Only Space owners and admins can manage agent roles")
        result = self.db.space_agents.update_one({"workspace_id": ObjectId(workspace_id), "agent_id": ObjectId(agent_id)}, {"$set": {"role": role, "updated_at": now()}})
        if not result.matched_count:
            raise ValueError("Agent is not assigned to this Space")

    def remove_space_agent(self, user_id: str, workspace_id: str, agent_id: str) -> None:
        from bson import ObjectId
        if not self.db.workspaces.find_one({"_id": ObjectId(workspace_id), "owner_id": ObjectId(user_id)}) and not self.db.memberships.find_one({"workspace_id": ObjectId(workspace_id), "user_id": ObjectId(user_id), "role": "admin"}):
            raise PermissionError("Only Space owners and admins can remove agents")
        self.db.space_agents.delete_one({"workspace_id": ObjectId(workspace_id), "agent_id": ObjectId(agent_id)})

    def update_space_member_role(self, owner_id: str, workspace_id: str, member_id: str, role: str) -> None:
        from bson import ObjectId
        if not self.db.workspaces.find_one({"_id": ObjectId(workspace_id), "owner_id": ObjectId(owner_id)}):
            raise PermissionError("Only the Space owner can manage member roles")
        if not self.db.memberships.find_one({"workspace_id": ObjectId(workspace_id), "user_id": ObjectId(member_id)}):
            raise ValueError("Space member not found")
        self.db.memberships.update_one({"workspace_id": ObjectId(workspace_id), "user_id": ObjectId(member_id)}, {"$set": {"role": role}})

    def space_detail(self, user_id: str, workspace_id: str) -> dict[str, Any]:
        from bson import ObjectId
        workspace = self.db.workspaces.find_one({"_id": ObjectId(workspace_id)})
        membership = self.db.memberships.find_one({"workspace_id": ObjectId(workspace_id), "user_id": ObjectId(user_id)})
        if not workspace or not membership:
            raise PermissionError("Space access denied")
        project_doc = self.db.space_projects.find_one({"workspace_id": ObjectId(workspace_id)}, {"_id": 0})
        project = self._public_space_project(project_doc)
        return {"id": workspace_id, "name": workspace["name"], "slug": workspace["slug"], "role": membership["role"],
                "owner": str(workspace["owner_id"]) == user_id, "project": project}

    @staticmethod
    def _public_space_project(project: dict[str, Any] | None) -> dict[str, Any] | None:
        if not project:
            return None
        return {key: value for key, value in project.items()
                if key not in {"github_token_encrypted", "workspace_id", "github_installation_id"}}

    def save_space_project(self, user_id: str, workspace_id: str, repository_url: str, default_branch: str) -> dict[str, Any]:
        from bson import ObjectId
        workspace = self.db.workspaces.find_one({"_id": ObjectId(workspace_id)})
        membership = self.db.memberships.find_one({"workspace_id": ObjectId(workspace_id), "user_id": ObjectId(user_id)})
        if not workspace or not membership or membership.get("role") not in {"owner", "admin"}:
            raise PermissionError("Only Space owners and admins can connect a project")
        if not repository_url.startswith("https://github.com/"):
            raise ValueError("Only GitHub repository URLs are supported")
        project = {"workspace_id": ObjectId(workspace_id), "provider": "github", "repository_url": repository_url.rstrip("/"),
                   "default_branch": default_branch.strip() or "main", "connection_status": "awaiting_github_authorization", "updated_at": now()}
        self.db.space_projects.update_one({"workspace_id": ObjectId(workspace_id)}, {
            "$set": project,
            "$unset": {"github_token_encrypted": "", "github_scopes": "", "github_full_name": "",
                       "github_private": "", "github_permissions": "", "github_default_branch": "",
                       "github_auth_type": "", "github_installation_id": ""},
            "$setOnInsert": {"created_at": now()},
        }, upsert=True)
        project.pop("workspace_id", None)
        return self._public_space_project(project) or {}

    def authorize_space_github(self, user_id: str, workspace_id: str, encrypted_token: str,
                               repository: dict[str, Any], scopes: list[str]) -> dict[str, Any]:
        from bson import ObjectId
        workspace = self.db.workspaces.find_one({"_id": ObjectId(workspace_id)})
        membership = self.db.memberships.find_one({"workspace_id": ObjectId(workspace_id), "user_id": ObjectId(user_id)})
        if not workspace or not membership or membership.get("role") not in {"owner", "admin"}:
            raise PermissionError("Only Space owners and admins can authorize GitHub")
        project = self.db.space_projects.find_one({"workspace_id": ObjectId(workspace_id)})
        if not project:
            raise ValueError("Save a GitHub repository before authorizing it")
        update = {"github_token_encrypted": encrypted_token, "github_scopes": scopes,
                  "connection_status": "authorized", "github_full_name": repository.get("full_name"),
                  "github_private": bool(repository.get("private")),
                  "github_permissions": repository.get("permissions") or {},
                  "github_default_branch": repository.get("default_branch"), "updated_at": now()}
        self.db.space_projects.update_one({"_id": project["_id"]}, {"$set": update})
        updated = self.db.space_projects.find_one({"_id": project["_id"]}, {"_id": 0})
        return self._public_space_project(updated) or {}

    def disconnect_space_github(self, user_id: str, workspace_id: str) -> None:
        from bson import ObjectId
        membership = self.db.memberships.find_one({"workspace_id": ObjectId(workspace_id), "user_id": ObjectId(user_id)})
        if not membership or membership.get("role") not in {"owner", "admin"}:
            raise PermissionError("Only Space owners and admins can disconnect GitHub")
        self.db.space_projects.update_one({"workspace_id": ObjectId(workspace_id)}, {"$unset": {
            "github_token_encrypted": "", "github_scopes": "", "github_full_name": "",
            "github_private": "", "github_permissions": "", "github_default_branch": "",
            "github_auth_type": "", "github_installation_id": "",
        }, "$set": {"connection_status": "awaiting_github_authorization", "updated_at": now()}})

    def github_token_for_space(self, user_id: str, workspace_id: str) -> tuple[str, dict[str, Any]]:
        from bson import ObjectId
        from backend.secure_store import decrypt_secret
        if not self.db.memberships.find_one({"workspace_id": ObjectId(workspace_id), "user_id": ObjectId(user_id)}):
            raise PermissionError("Space access denied")
        project = self.db.space_projects.find_one({"workspace_id": ObjectId(workspace_id)})
        if not project or not project.get("github_token_encrypted"):
            raise ValueError("GitHub is not authorized for this Space")
        return decrypt_secret(project["github_token_encrypted"]), self._public_space_project(project) or {}

    def remove_space_project(self, user_id: str, workspace_id: str) -> None:
        from bson import ObjectId
        membership = self.db.memberships.find_one({"workspace_id": ObjectId(workspace_id), "user_id": ObjectId(user_id)})
        if not membership or membership.get("role") not in {"owner", "admin"}:
            raise PermissionError("Only Space owners and admins can disconnect a project")
        self.db.space_projects.delete_one({"workspace_id": ObjectId(workspace_id)})

    def workspaces_for_user(self, user_id: str) -> list[dict[str, Any]]:
        from bson import ObjectId
        rows = self.db.memberships.aggregate([{"$match": {"user_id": ObjectId(user_id)}}, {"$lookup": {"from": "workspaces", "localField": "workspace_id", "foreignField": "_id", "as": "workspace"}}, {"$unwind": "$workspace"}])
        return [{"id": str(row["workspace"]["_id"]), "name": row["workspace"]["name"], "slug": row["workspace"]["slug"], "role": row["role"]} for row in rows]

    def update_workspace(self, user_id: str, workspace_id: str, name: str, role: str) -> dict[str, Any]:
        from bson import ObjectId
        membership = self.db.memberships.find_one({"workspace_id": ObjectId(workspace_id), "user_id": ObjectId(user_id)})
        if not membership:
            raise PermissionError("Workspace access denied")
        if membership.get("role") != "owner":
            raise PermissionError("Only the workspace owner can edit workspace details")
        clean_name = name.strip()
        if not clean_name:
            raise ValueError("Workspace name is required")
        self.db.workspaces.update_one({"_id": ObjectId(workspace_id)}, {"$set": {"name": clean_name, "updated_at": now()}})
        self.db.memberships.update_one({"workspace_id": ObjectId(workspace_id), "user_id": ObjectId(user_id)}, {"$set": {"role": role}})
        workspace = self.db.workspaces.find_one({"_id": ObjectId(workspace_id)})
        return {"id": str(workspace["_id"]), "name": workspace["name"], "slug": workspace["slug"], "role": role}

    def is_member(self, user_id: str, workspace_id: str) -> bool:
        from bson import ObjectId
        return bool(self.db.memberships.find_one({"user_id": ObjectId(user_id), "workspace_id": ObjectId(workspace_id)}))

    def create_flow(self, user_id: str, workspace_id: str, flow: dict[str, Any]) -> dict[str, Any]:
        from bson import ObjectId
        if not self.is_member(user_id, workspace_id):
            raise PermissionError("Workspace access denied")
        doc = {"workspace_id": ObjectId(workspace_id), "slug": flow["id"], "name": flow["name"], "created_by": ObjectId(user_id), "created_at": now(), "updated_at": now(), "current_version": flow["version"], "status": "draft", "published_version": None}
        result = self.db.flows.insert_one(doc)
        flow_doc = {"flow_id": result.inserted_id, "version": flow["version"], "flow": flow, "created_by": ObjectId(user_id), "created_at": now()}
        self.db.flow_versions.insert_one(flow_doc)
        return {"id": str(result.inserted_id), "slug": doc["slug"], "name": doc["name"], "version": flow["version"]}

    def save_flow_version(self, user_id: str, flow_id: str, flow: dict[str, Any]) -> dict[str, Any]:
        from bson import ObjectId
        flow_doc = self.db.flows.find_one({"_id": ObjectId(flow_id)})
        if not flow_doc or not self.is_member(user_id, str(flow_doc["workspace_id"])):
            raise PermissionError("Flow access denied")
        next_version = int(flow_doc.get("current_version", 0)) + 1
        flow["version"] = next_version
        self.db.flow_versions.insert_one({"flow_id": ObjectId(flow_id), "version": next_version, "flow": flow, "created_by": ObjectId(user_id), "created_at": now()})
        # Saving a new draft must not unpublish the currently live version.
        # Keep published_version/status (and its public token) unchanged until
        # the user explicitly publishes this new version or unpublishes it.
        self.db.flows.update_one({"_id": ObjectId(flow_id)}, {"$set": {"name": flow["name"], "current_version": next_version, "updated_at": now()}})
        return {"flow_id": flow_id, "version": next_version}

    def list_flows(self, user_id: str, workspace_id: str) -> list[dict[str, Any]]:
        from bson import ObjectId
        if not self.is_member(user_id, workspace_id):
            raise PermissionError("Workspace access denied")
        result = []
        for row in self.db.flows.find({"workspace_id": ObjectId(workspace_id)}).sort("updated_at", -1):
            version = self.db.flow_versions.find_one({"flow_id": row["_id"], "version": row.get("current_version", 1)})
            result.append({"id": str(row["_id"]), "slug": row["slug"], "name": row["name"], "version": row.get("current_version", 1), "status": row.get("status", "draft"), "published_version": row.get("published_version"), "agent_id": (version or {}).get("flow", {}).get("agent_id"), "updated_at": row.get("updated_at")})
        return result

    def get_flow(self, user_id: str, flow_id: str) -> dict[str, Any] | None:
        from bson import ObjectId
        row = self.db.flows.find_one({"_id": ObjectId(flow_id)})
        if not row or not self.is_member(user_id, str(row["workspace_id"])):
            raise PermissionError("Flow access denied")
        version = self.db.flow_versions.find_one({"flow_id": ObjectId(flow_id), "version": row.get("current_version", 1)})
        return {"id": str(row["_id"]), "workspace_id": str(row["workspace_id"]), "name": row["name"], "slug": row["slug"], "version": row.get("current_version", 1), "status": row.get("status", "draft"), "published_version": row.get("published_version"), "flow": version["flow"] if version else None}

    def list_flow_versions(self, user_id: str, flow_id: str) -> list[dict[str, Any]]:
        from bson import ObjectId
        flow = self.db.flows.find_one({"_id": ObjectId(flow_id)})
        if not flow or not self.is_member(user_id, str(flow["workspace_id"])):
            raise PermissionError("Flow access denied")
        return [{"version": row["version"], "created_at": row.get("created_at"), "created_by": str(row["created_by"])}
                for row in self.db.flow_versions.find({"flow_id": ObjectId(flow_id)}).sort("version", -1)]

    def publish_flow(self, user_id: str, flow_id: str, version: int | None = None) -> dict[str, Any]:
        from bson import ObjectId
        flow = self.db.flows.find_one({"_id": ObjectId(flow_id)})
        if not flow or not self.is_member(user_id, str(flow["workspace_id"])):
            raise PermissionError("Flow access denied")
        version = version or flow.get("current_version", 1)
        if not self.db.flow_versions.find_one({"flow_id": ObjectId(flow_id), "version": version}):
            raise ValueError("Flow version does not exist")
        public_token = None
        if not flow.get("public_token_hash"):
            public_token = secrets.token_urlsafe(48)
        fields = {"status": "published", "published_version": version, "updated_at": now()}
        if public_token:
            fields.update({"public_token_hash": hashlib.sha256(public_token.encode()).hexdigest(), "public_token_issued_at": now()})
        self.db.flows.update_one({"_id": ObjectId(flow_id)}, {"$set": fields})
        return {"flow_id": flow_id, "slug": flow["slug"], "version": version, "status": "published", "public_token": public_token, "token_created": bool(public_token)}

    def rotate_public_token(self, user_id: str, flow_id: str) -> dict[str, str]:
        from bson import ObjectId
        flow = self.db.flows.find_one({"_id": ObjectId(flow_id)})
        if not flow or not self.is_member(user_id, str(flow["workspace_id"])):
            raise PermissionError("Flow access denied")
        token = secrets.token_urlsafe(48)
        self.db.flows.update_one({"_id": ObjectId(flow_id)}, {"$set": {"public_token_hash": hashlib.sha256(token.encode()).hexdigest(), "public_token_issued_at": now(), "updated_at": now()}})
        return {"flow_id": flow_id, "slug": flow["slug"], "public_token": token}

    def unpublish_flow(self, user_id: str, flow_id: str) -> dict[str, Any]:
        from bson import ObjectId
        flow = self.db.flows.find_one({"_id": ObjectId(flow_id)})
        if not flow or not self.is_member(user_id, str(flow["workspace_id"])):
            raise PermissionError("Flow access denied")
        self.db.flows.update_one({"_id": ObjectId(flow_id)}, {"$set": {"status": "draft", "updated_at": now()}})
        return {"flow_id": flow_id, "status": "draft"}

    def pause_flow(self, user_id: str, flow_id: str) -> dict[str, Any]:
        from bson import ObjectId
        flow = self.db.flows.find_one({"_id": ObjectId(flow_id)})
        if not flow or not self.is_member(user_id, str(flow["workspace_id"])):
            raise PermissionError("Flow access denied")
        self.db.flows.update_one({"_id": ObjectId(flow_id)}, {"$set": {"status": "paused", "updated_at": now()}})
        return {"flow_id": flow_id, "status": "paused"}

    def delete_flow(self, user_id: str, flow_id: str) -> None:
        from bson import ObjectId
        flow = self.db.flows.find_one({"_id": ObjectId(flow_id)})
        if not flow or not self.is_member(user_id, str(flow["workspace_id"])):
            raise PermissionError("Flow access denied")
        self.db.flow_versions.delete_many({"flow_id": ObjectId(flow_id)})
        self.db.runtime_sessions.delete_many({"flow_id": flow_id})
        self.db.flows.delete_one({"_id": ObjectId(flow_id)})

    def published_flow(self, user_id: str, flow_id: str) -> dict[str, Any]:
        from bson import ObjectId
        flow = self.db.flows.find_one({"_id": ObjectId(flow_id), "status": "published"})
        if not flow or not self.is_member(user_id, str(flow["workspace_id"])):
            raise PermissionError("Published flow not found")
        version = self.db.flow_versions.find_one({"flow_id": ObjectId(flow_id), "version": flow["published_version"]})
        if not version:
            raise ValueError("Published flow version is missing")
        return version["flow"]

    def public_flow_by_token(self, slug: str, token: str) -> dict[str, Any] | None:
        token_hash = hashlib.sha256(token.encode()).hexdigest()
        row = self.db.flows.find_one({"slug": slug, "status": "published", "public_token_hash": token_hash})
        if not row:
            return None
        version = self.db.flow_versions.find_one({"flow_id": row["_id"], "version": row.get("published_version")})
        if not version:
            return None
        return {"flow_id": str(row["_id"]), "slug": row["slug"], "version": version["version"], "flow": version["flow"], "workspace_id": str(row["workspace_id"])}

    def issue_session(self, user_id: str) -> str:
        token = secrets.token_urlsafe(32)
        self.db.auth_sessions.insert_one({"token_hash": hashlib.sha256(token.encode()).hexdigest(), "user_id": user_id, "created_at": now(), "expires_at": now() + timedelta(days=7)})
        return token

    def user_by_session(self, token: str) -> dict[str, Any] | None:
        row = self.db.auth_sessions.find_one({"token_hash": hashlib.sha256(token.encode()).hexdigest(), "expires_at": {"$gt": now()}})
        if not row:
            return None
        from bson import ObjectId
        user = self.db.users.find_one({"_id": ObjectId(row["user_id"])})
        return self.public_user(user) if user else None

    def revoke_session(self, token: str) -> None:
        self.db.auth_sessions.delete_one({"token_hash": hashlib.sha256(token.encode()).hexdigest()})

    def create_resource(self, user_id: str, workspace_id: str, collection: str, request: dict[str, Any]) -> dict[str, Any]:
        from bson import ObjectId
        if not self.is_member(user_id, workspace_id):
            raise PermissionError("Workspace access denied")
        name = str(request.get("name", "")).strip()
        if not name:
            raise ValueError("Name is required")
        if any(key.lower() in {"secret", "token", "password", "api_key", "private_key"} for key in request.get("config", {})):
            raise ValueError("Secrets must be stored outside resource configuration")
        doc = {"workspace_id": ObjectId(workspace_id), "name": name, "slug": self._resource_slug(name), "description": request.get("description", ""), "config": request.get("config", {}), "created_by": ObjectId(user_id), "created_at": now(), "updated_at": now()}
        result = self.db[collection].insert_one(doc)
        return {"id": str(result.inserted_id), "name": doc["name"], "slug": doc["slug"], "description": doc["description"], "config": doc["config"]}

    @staticmethod
    def _resource_slug(name: str) -> str:
        import re
        return re.sub(r"-+", "-", re.sub(r"[^a-z0-9]+", "-", name.lower())).strip("-") or "agent"

    def update_agent(self, user_id: str, agent_id: str, config: dict[str, Any]) -> dict[str, Any]:
        from bson import ObjectId
        try:
            agent_object_id = ObjectId(agent_id)
        except Exception as exc:
            raise ValueError("Agent not found") from exc
        if any(key.lower() in {"secret", "token", "password", "api_key", "private_key"} for key in config):
            raise ValueError("Secrets must be stored outside resource configuration")
        agent = self.db.agents.find_one({"_id": agent_object_id})
        if not agent or not self.is_member(user_id, str(agent["workspace_id"])):
            raise PermissionError("Agent access denied")
        self.db.agents.update_one({"_id": agent_object_id}, {"$set": {"config": config, "updated_at": now()}})
        return {"id": agent_id, "name": agent["name"], "slug": agent.get("slug") or self._resource_slug(agent["name"]), "description": agent.get("description", ""), "config": config}

    def delete_agent(self, user_id: str, agent_id: str) -> None:
        from bson import ObjectId
        try:
            agent_object_id = ObjectId(agent_id)
        except Exception as exc:
            raise ValueError("Agent not found") from exc
        agent = self.db.agents.find_one({"_id": agent_object_id})
        if not agent or not self.is_member(user_id, str(agent["workspace_id"])):
            raise PermissionError("Agent access denied")
        if self.agent_connections(user_id, agent_id):
            raise ValueError("Disconnect this agent from all workflows before deleting it")
        self.db.agents.delete_one({"_id": agent_object_id})

    def list_resources(self, user_id: str, workspace_id: str, collection: str) -> list[dict[str, Any]]:
        from bson import ObjectId
        if not self.is_member(user_id, workspace_id):
            raise PermissionError("Workspace access denied")
        connected_counts: dict[str, int] = {}
        if collection == "agents":
            for flow in self.db.flows.find({"workspace_id": ObjectId(workspace_id)}):
                version = self.db.flow_versions.find_one({"flow_id": flow["_id"], "version": flow.get("current_version", 1)})
                agent_id = (version or {}).get("flow", {}).get("agent_id")
                if agent_id:
                    connected_counts[agent_id] = connected_counts.get(agent_id, 0) + 1
        return [
            {
                "id": str(row["_id"]),
                "name": row["name"],
                "slug": row.get("slug") or self._resource_slug(row["name"]),
                "description": row.get("description", ""),
                "config": row.get("config", {}),
                "connected_workflows": connected_counts.get(str(row["_id"]), 0) if collection == "agents" else None,
            }
            for row in self.db[collection].find({"workspace_id": ObjectId(workspace_id)}).sort("name", 1)
        ]

    def agent_connections(self, user_id: str, agent_id: str) -> list[dict[str, Any]]:
        from bson import ObjectId
        try:
            agent = self.db.agents.find_one({"_id": ObjectId(agent_id)})
        except Exception as exc:
            raise ValueError("Agent not found") from exc
        if not agent or not self.is_member(user_id, str(agent["workspace_id"])):
            raise PermissionError("Agent access denied")
        connections: list[dict[str, Any]] = []
        for flow in self.db.flows.find({"workspace_id": agent["workspace_id"]}).sort("updated_at", -1):
            version = self.db.flow_versions.find_one({"flow_id": flow["_id"], "version": flow.get("current_version", 1)})
            document = version.get("flow", {}) if version else {}
            if document.get("agent_id") == agent_id:
                connections.append({
                    "flow_id": str(flow["_id"]),
                    "flow_name": flow.get("name", "Untitled workflow"),
                    "flow_status": flow.get("status", "draft"),
                    "flow_version": flow.get("current_version", 1),
                })
        return connections

    def set_flow_agent(self, user_id: str, flow_id: str, agent_id: str | None) -> dict[str, Any]:
        from bson import ObjectId
        flow_doc = self.db.flows.find_one({"_id": ObjectId(flow_id)})
        if not flow_doc or not self.is_member(user_id, str(flow_doc["workspace_id"])):
            raise PermissionError("Flow access denied")
        if agent_id:
            agent = self.db.agents.find_one({"_id": ObjectId(agent_id), "workspace_id": flow_doc["workspace_id"]})
            if not agent:
                raise ValueError("Agent does not belong to this workspace")
        version = self.db.flow_versions.find_one({"flow_id": flow_doc["_id"], "version": flow_doc.get("current_version", 1)})
        if not version:
            raise ValueError("Flow version is missing")
        flow = version["flow"]
        if agent_id:
            flow["agent_id"] = agent_id
        else:
            flow.pop("agent_id", None)
        return self.save_flow_version(user_id, flow_id, flow)


class MongoRuntimeStore:
    """Adapter with the same interface as the local SQLite session store."""
    def __init__(self, mongo: MongoStore):
        self.mongo = mongo

    def create(self, flow: dict[str, Any]) -> dict[str, Any]:
        start = next(node["id"] for node in flow["nodes"] if node["type"] == "start")
        session = {"id": uuid.uuid4().hex, "flow_id": flow["id"], "flow_version": flow["version"], "status": "running", "current_node": start, "variables": {}, "messages": [], "trace": [], "created_at": now().isoformat(), "updated_at": now().isoformat()}
        self.mongo.db.runtime_sessions.insert_one({"_id": session["id"], **session})
        return session

    def get(self, session_id: str) -> dict[str, Any] | None:
        row = self.mongo.db.runtime_sessions.find_one({"_id": session_id})
        if not row:
            return None
        row.pop("_id", None)
        return row

    def save(self, session: dict[str, Any]) -> None:
        session["updated_at"] = now().isoformat()
        self.mongo.db.runtime_sessions.replace_one({"_id": session["id"]}, {"_id": session["id"], **session}, upsert=True)

    def trace(self, session_id: str, event: dict[str, Any]) -> None:
        self.mongo.db.runtime_traces.insert_one({"session_id": session_id, "event": event, "created_at": now()})
