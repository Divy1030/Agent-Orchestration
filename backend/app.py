from __future__ import annotations

import os
from copy import deepcopy
from typing import Any

import httpx
from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Request, Response
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field
from backend.flow_schema import DEMO_FLOW, FlowValidationError, canvas_graph, validate_flow
from backend.runtime import FlowRuntime, SessionStore
from backend.agent_executor import OpenRouterExecutor
from backend.tool_registry import execute_tool, list_tools
from backend.mcp_tools import McpConnectionError, call_mcp_tool, discover_mcp_tools, expand_mcp_servers
from backend.auth_models import FlowSaveRequest, LoginRequest, PublicChatRequest, RegisterRequest, ResourceRequest, SessionCreateRequest, WorkspaceRequest
from backend.mongo_store import MongoRuntimeStore, MongoStore, verify_password
from backend.clerk_auth import ClerkAuthError, clerk_user_from_request

load_dotenv()

app = FastAPI(title="Agent Orchestration Base", version="0.1.0")
# Reflect any HTTP(S) origin so credentialed browser requests work. A literal
# allow_origins=["*"] is incompatible with credentialed CORS in browsers.
app.add_middleware(CORSMiddleware, allow_origin_regex=r"https?://.*", allow_credentials=True, allow_methods=["*"], allow_headers=["*"])

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
        node_guidance = {key: config[key] for key in ("workflow_instructions", "routing_instructions", "instructions", "tools") if key in config}
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
    return await openrouter_executor.execute({**node, "config": config}, messages,
                                             mcp_tools=mcp_tools, mcp_tool_caller=call_mcp_tool)


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
    return {"workspace": mongo.create_workspace(user["id"], request.name)}


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
    session_id = request.session_id
    if not session_id:
        session = await runtime.start(flow)
        session_id = session["id"]
    else:
        session = runtime.store.get(session_id)
        if not session or session.get("flow_id") != published["flow_id"]:
            raise HTTPException(404, "Chat session not found")
    try:
        result = await runtime.turn(flow, session_id, request.message, runtime_route)
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
        if session.get("flow_id") != FLOW["id"]:
            user = current_user(http_request)
            stored_flow = mongo.get_flow(user["id"], session["flow_id"])
            if not stored_flow or not stored_flow.get("flow"):
                raise ValueError("Workflow version is missing")
            flow = validate_flow(stored_flow["flow"])
        return await runtime.turn(flow, session_id, request.message, runtime_route)
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
