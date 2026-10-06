"""Persistent, channel-neutral flow runtime for the Phase 1 MVP."""

from __future__ import annotations

import json
import sqlite3
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Awaitable, Callable

from backend.flow_schema import validate_flow

RouteSelector = Callable[[dict[str, Any], str, str, list[dict[str, str]]], Awaitable[dict[str, Any]]]
AgentExecutor = Callable[[dict[str, Any], list[dict[str, str]]], Awaitable[dict[str, Any]]]


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


class SessionStore:
    def __init__(self, path: str):
        self.path = path
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as db:
            db.execute("""CREATE TABLE IF NOT EXISTS sessions (
                id TEXT PRIMARY KEY, flow_id TEXT NOT NULL, flow_version INTEGER NOT NULL,
                status TEXT NOT NULL, state_json TEXT NOT NULL, created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            )""")
            db.execute("""CREATE TABLE IF NOT EXISTS execution_traces (
                id INTEGER PRIMARY KEY AUTOINCREMENT, session_id TEXT NOT NULL,
                sequence INTEGER NOT NULL, event_json TEXT NOT NULL, created_at TEXT NOT NULL
            )""")

    def _connect(self) -> sqlite3.Connection:
        db = sqlite3.connect(self.path)
        db.row_factory = sqlite3.Row
        return db

    def create(self, flow: dict[str, Any]) -> dict[str, Any]:
        start = next(node["id"] for node in flow["nodes"] if node["type"] == "start")
        session = {
            "id": uuid.uuid4().hex, "flow_id": flow["id"], "flow_version": flow["version"],
            "status": "running", "current_node": start, "variables": {},
            "messages": [], "trace": [], "created_at": utc_now(), "updated_at": utc_now(),
        }
        with self._connect() as db:
            db.execute("INSERT INTO sessions VALUES (?, ?, ?, ?, ?, ?, ?)", (
                session["id"], flow["id"], flow["version"], session["status"],
                json.dumps(session), session["created_at"], session["updated_at"],
            ))
        return session

    def get(self, session_id: str) -> dict[str, Any] | None:
        with self._connect() as db:
            row = db.execute("SELECT state_json FROM sessions WHERE id = ?", (session_id,)).fetchone()
        return json.loads(row["state_json"]) if row else None

    def save(self, session: dict[str, Any]) -> None:
        session["updated_at"] = utc_now()
        with self._connect() as db:
            db.execute("UPDATE sessions SET status = ?, state_json = ?, updated_at = ? WHERE id = ?", (
                session["status"], json.dumps(session), session["updated_at"], session["id"],
            ))

    def trace(self, session_id: str, event: dict[str, Any]) -> None:
        with self._connect() as db:
            sequence = db.execute("SELECT COUNT(*) FROM execution_traces WHERE session_id = ?", (session_id,)).fetchone()[0] + 1
            db.execute("INSERT INTO execution_traces (session_id, sequence, event_json, created_at) VALUES (?, ?, ?, ?)", (
                session_id, sequence, json.dumps({"sequence": sequence, **event}), utc_now(),
            ))


class FlowRuntime:
    def __init__(self, store: SessionStore, agent_executor: AgentExecutor | None = None):
        self.store = store
        self.agent_executor = agent_executor

    async def start(self, flow: dict[str, Any]) -> dict[str, Any]:
        validate_flow(flow)
        return self.store.create(flow)

    async def turn(self, flow: dict[str, Any], session_id: str, message: str, route_selector: RouteSelector) -> dict[str, Any]:
        validate_flow(flow)
        session = self.store.get(session_id)
        if not session:
            raise ValueError("Session not found")
        if session["status"] != "running":
            raise ValueError("Session is already complete")
        if not message.strip():
            raise ValueError("Message cannot be empty")
        session["messages"].append({"role": "user", "content": message})
        nodes = {node["id"]: node for node in flow["nodes"]}
        started = time.perf_counter()
        current = session["current_node"]
        node = nodes[current]
        event = {"type": "node_started", "node": current, "node_type": node["type"]}
        session["trace"].append(event)
        self.store.trace(session_id, event)

        decision = await route_selector(flow, current, message, session["messages"])
        target = decision.get("target") or current
        if target not in nodes:
            raise ValueError("Route selected an unknown node")
        session["current_node"] = target
        target_node = nodes[target]
        agent_result: dict[str, Any] = {}
        reply = target_node.get("config", {}).get("reply", "")
        if target_node["type"] == "agent" and self.agent_executor:
            agent_result = await self.agent_executor(target_node, session["messages"])
            reply = agent_result.get("reply", reply)
        if reply:
            session["messages"].append({"role": "assistant", "content": reply})
        if target_node["type"] == "end":
            session["status"] = "completed"
        transition = {"type": "transition", "source": current, "target": target,
                      "choice": decision.get("choice"), "provider": decision.get("provider"),
                      "latency_ms": round((time.perf_counter() - started) * 1000, 1)}
        if agent_result:
            transition["agent"] = {key: value for key, value in agent_result.items() if key != "reply"}
        session["trace"].append(transition)
        self.store.trace(session_id, transition)
        self.store.save(session)
        return {"session_id": session_id, "status": session["status"], "source_node": current, "current_node": target,
                "reply": reply, "decision": decision, "trace": session["trace"][-2:]}
