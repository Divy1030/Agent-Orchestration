from __future__ import annotations

import os
from copy import deepcopy
from typing import Any

import httpx
from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field
from backend.flow_schema import DEMO_FLOW, FlowValidationError, canvas_graph, validate_flow

load_dotenv()

app = FastAPI(title="Agent Orchestration Base", version="0.1.0")
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])

FLOW = validate_flow(DEMO_FLOW)
GRAPH = canvas_graph(FLOW)


class RunRequest(BaseModel):
    message: str = Field(min_length=1, max_length=4000)
    node: str | None = None
    history: list[dict[str, str]] = Field(default_factory=list)


class ValidateRequest(BaseModel):
    flow: dict[str, Any]


def jev_url() -> str:
    return os.getenv("JEV_BASE_URL", "https://api.typesafe.ai/v1").rstrip("/") + "/systemone"


async def choose_edge(graph: dict[str, Any], node_id: str, message: str, history: list[dict[str, str]]) -> dict[str, Any]:
    routes = graph["nodes"][node_id].get("routes", {})
    if not routes:
        return {"ok": True, "choice": None, "target": node_id, "provider": "none"}
    if os.getenv("JEV_MOCK", "true").lower() == "true":
        key = next(iter(routes))
        return {"ok": True, "choice": key, "target": routes[key]["target"], "provider": "mock"}
    api_key = os.getenv("JEV_API_KEY") or os.getenv("TYPESAFE_API_KEY") or os.getenv("SYSTEM_ONE_API_KEY")
    if not api_key:
        raise HTTPException(503, "JEV_API_KEY is not configured")
    payload = {
        "model": os.getenv("JEV_MODEL", "jev-latest"),
        "state": {"current_node": node_id, "conversation_history": history, "latest_message": message},
        "questions": {"next_step": {
            "type": "choice",
            "instructions": "Choose exactly one valid next edge from the latest message and conversation context. Prefer ending when the user explicitly wants to stop.",
            "criteria": {key: route.get("description", "") for key, route in routes.items()},
        }},
    }
    async with httpx.AsyncClient(timeout=httpx.Timeout(8, connect=3)) as client:
        response = await client.post(jev_url(), headers={"Authorization": f"Bearer {api_key}"}, json=payload)
    response.raise_for_status()
    raw = response.json()
    answer = raw.get("answers", {}).get("next_step", {})
    choice = answer.get("choice")
    if answer.get("type") != "choice" or choice not in routes:
        raise HTTPException(502, "Jev returned an invalid route choice")
    return {"ok": True, "choice": choice, "target": routes[choice]["target"], "provider": "jev", "raw": raw}


@app.get("/api/health")
def health() -> dict[str, str]:
    return {"status": "ok"}


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


@app.post("/api/run")
async def run(request: RunRequest) -> dict[str, Any]:
    node = request.node or GRAPH["start_node"]
    if node not in GRAPH["nodes"]:
        raise HTTPException(400, "Unknown node")
    decision = await choose_edge(GRAPH, node, request.message, request.history)
    target = decision["target"]
    return {"source": node, "target": target, "choice": decision["choice"], "decision": decision,
            "node": GRAPH["nodes"][target], "ended": bool(GRAPH["nodes"][target].get("terminal"))}
