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
        self._db.workspaces.create_index("slug", unique=True)
        self._db.memberships.create_index([("workspace_id", 1), ("user_id", 1)], unique=True)
        self._db.flows.create_index([("workspace_id", 1), ("slug", 1)], unique=True)
        self._db.flow_versions.create_index([("flow_id", 1), ("version", 1)], unique=True)
        self._db.auth_sessions.create_index("token_hash", unique=True)
        self._db.auth_sessions.create_index("expires_at", expireAfterSeconds=0)
        self._db.flows.create_index("public_token_hash", sparse=True, unique=True)
        self._db.tools.create_index([("workspace_id", 1), ("name", 1)], unique=True)
        self._db.agents.create_index([("workspace_id", 1), ("name", 1)], unique=True)
        self._db.connectors.create_index([("workspace_id", 1), ("name", 1)], unique=True)
        self._db.credential_refs.create_index([("workspace_id", 1), ("name", 1)], unique=True)

    def create_user(self, email: str, password: str, name: str) -> dict[str, Any]:
        from pymongo.errors import DuplicateKeyError
        doc = {"email": email.lower().strip(), "name": name.strip() or email.split("@")[0],
               "password_hash": hash_password(password), "created_at": now()}
        try:
            result = self.db.users.insert_one(doc)
        except DuplicateKeyError as exc:
            raise ValueError("An account with this email already exists") from exc
        doc["_id"] = result.inserted_id
        return self.public_user(doc)

    def user_by_email(self, email: str) -> dict[str, Any] | None:
        doc = self.db.users.find_one({"email": email.lower().strip()})
        return self.public_user(doc) if doc else None

    def user_for_login(self, email: str) -> dict[str, Any] | None:
        doc = self.db.users.find_one({"email": email.lower().strip()})
        if doc:
            doc["id"] = str(doc["_id"])
        return doc

    @staticmethod
    def public_user(doc: dict[str, Any]) -> dict[str, Any]:
        return {"id": str(doc.get("_id", doc.get("id"))), "email": doc["email"], "name": doc.get("name", "")}

    def create_workspace(self, user_id: str, name: str) -> dict[str, Any]:
        from bson import ObjectId
        slug = "-".join(name.lower().split()) or "workspace"
        existing = self.db.workspaces.count_documents({"slug": {"$regex": f"^{slug}"}})
        if existing:
            slug = f"{slug}-{existing + 1}"
        doc = {"name": name.strip() or "Workspace", "slug": slug, "owner_id": ObjectId(user_id), "created_at": now()}
        result = self.db.workspaces.insert_one(doc)
        doc["id"] = str(result.inserted_id)
        self.db.memberships.insert_one({"workspace_id": result.inserted_id, "user_id": ObjectId(user_id), "role": "owner", "created_at": now()})
        return {"id": doc["id"], "name": doc["name"], "slug": doc["slug"], "role": "owner"}

    def workspaces_for_user(self, user_id: str) -> list[dict[str, Any]]:
        from bson import ObjectId
        rows = self.db.memberships.aggregate([{"$match": {"user_id": ObjectId(user_id)}}, {"$lookup": {"from": "workspaces", "localField": "workspace_id", "foreignField": "_id", "as": "workspace"}}, {"$unwind": "$workspace"}])
        return [{"id": str(row["workspace"]["_id"]), "name": row["workspace"]["name"], "slug": row["workspace"]["slug"], "role": row["role"]} for row in rows]

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
        return {"flow_id": str(row["_id"]), "slug": row["slug"], "version": version["version"], "flow": version["flow"]}

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
