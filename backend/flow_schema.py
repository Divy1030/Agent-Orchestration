"""Canonical flow contract and safety validation for the orchestration runtime."""

from __future__ import annotations

from collections import defaultdict, deque
from typing import Any

from backend.tool_registry import ToolConfigurationError, validate_tool_config

NODE_TYPES = {"start", "message", "agent", "collect", "condition", "jev_router", "tool", "end"}
VARIABLE_TYPES = {"string", "number", "boolean", "email", "date", "object"}


class FlowValidationError(ValueError):
    def __init__(self, errors: list[str]):
        self.errors = errors
        super().__init__("Flow validation failed: " + "; ".join(errors))


def validate_flow(flow: Any) -> dict[str, Any]:
    """Validate and return a flow without executing configuration expressions."""
    errors: list[str] = []
    if not isinstance(flow, dict):
        raise FlowValidationError(["flow must be an object"])

    for field in ("id", "version", "name", "goal", "nodes", "edges", "variables"):
        if field not in flow:
            errors.append(f"missing required field: {field}")
    if errors:
        raise FlowValidationError(errors)

    if not isinstance(flow["id"], str) or not flow["id"].strip():
        errors.append("id must be a non-empty string")
    if not isinstance(flow["version"], int) or flow["version"] < 1:
        errors.append("version must be a positive integer")
    if not isinstance(flow["goal"], dict) or not isinstance(flow["goal"].get("primary"), str):
        errors.append("goal.primary must be a string")
    if "agent_id" in flow and flow["agent_id"] is not None and not isinstance(flow["agent_id"], str):
        errors.append("agent_id must be a string or null")
    if "jev_instructions" in flow and not isinstance(flow["jev_instructions"], str):
        errors.append("jev_instructions must be a string")

    nodes = flow["nodes"]
    if not isinstance(nodes, list) or not nodes:
        errors.append("nodes must be a non-empty array")
        nodes = []
    node_ids = [node.get("id") for node in nodes if isinstance(node, dict)]
    if len(node_ids) != len(set(node_ids)):
        errors.append("node ids must be unique")
    node_map: dict[str, dict[str, Any]] = {}
    for node in nodes:
        if not isinstance(node, dict):
            errors.append("every node must be an object")
            continue
        node_id, node_type = node.get("id"), node.get("type")
        if not isinstance(node_id, str) or not node_id.strip():
            errors.append("every node requires a non-empty id")
            continue
        if node_type not in NODE_TYPES:
            errors.append(f"node {node_id}: unsupported type {node_type!r}")
        if node_type == "agent" and "agent_id" in node.get("config", {}) and not isinstance(node["config"]["agent_id"], str):
            errors.append(f"node {node_id}: config.agent_id must be a string")
        config = node.get("config", {})
        if not isinstance(config, dict):
            errors.append(f"node {node_id}: config must be an object")
        else:
            instructions = config.get("instructions", [])
            if not isinstance(instructions, list) or not all(isinstance(item, str) and item.strip() for item in instructions):
                errors.append(f"node {node_id}: config.instructions must be a list of non-empty strings")
            for field in ("routing_instructions", "system_prompt"):
                if field in config and not isinstance(config[field], str):
                    errors.append(f"node {node_id}: config.{field} must be a string")
            if node_type == "tool":
                try:
                    validate_tool_config(node)
                except ToolConfigurationError as exc:
                    errors.append(str(exc))
        node_map[node_id] = node
    starts = [node_id for node_id, node in node_map.items() if node.get("type") == "start"]
    ends = [node_id for node_id, node in node_map.items() if node.get("type") == "end"]
    if len(starts) != 1:
        errors.append("flow must contain exactly one start node")
    if not ends:
        errors.append("flow must contain at least one end node")

    variables = flow["variables"]
    if not isinstance(variables, dict):
        errors.append("variables must be an object")
        variables = {}
    for name, spec in variables.items():
        if not isinstance(spec, dict) or spec.get("type") not in VARIABLE_TYPES:
            errors.append(f"variable {name}: invalid type")
        elif "required" in spec and not isinstance(spec["required"], bool):
            errors.append(f"variable {name}: required must be boolean")

    edges = flow["edges"]
    if not isinstance(edges, list):
        errors.append("edges must be an array")
        edges = []
    adjacency: dict[str, list[str]] = defaultdict(list)
    incoming: dict[str, int] = defaultdict(int)
    edge_keys: set[tuple[str, str, str]] = set()
    for edge in edges:
        if not isinstance(edge, dict):
            errors.append("every edge must be an object")
            continue
        source, target = edge.get("from"), edge.get("to")
        key = str(edge.get("key") or edge.get("when") or "default")
        if source not in node_map:
            errors.append(f"edge references unknown source node: {source}")
        if target not in node_map:
            errors.append(f"edge references unknown target node: {target}")
        if source in node_map and target in node_map:
            edge_identity = (source, target, key)
            if edge_identity in edge_keys:
                errors.append(f"duplicate edge: {source} -> {target} ({key})")
            edge_keys.add(edge_identity)
            adjacency[source].append(target)
            incoming[target] += 1

    if starts:
        reachable: set[str] = set()
        queue = deque(starts)
        while queue:
            current = queue.popleft()
            if current in reachable:
                continue
            reachable.add(current)
            queue.extend(adjacency[current])
        disconnected = sorted(set(node_map) - reachable)
        if disconnected:
            errors.append("disconnected nodes: " + ", ".join(disconnected))

    if errors:
        raise FlowValidationError(errors)
    return flow


def canvas_graph(flow: dict[str, Any]) -> dict[str, Any]:
    """Adapt the canonical contract to the current React Flow canvas shape."""
    routes: dict[str, dict[str, dict[str, Any]]] = defaultdict(dict)
    for edge in flow["edges"]:
        key = str(edge.get("key") or edge.get("when") or "default")
        routes[edge["from"]][key] = {
            "target": edge["to"],
            "description": edge.get("description") or edge.get("when") or key,
        }
    nodes = {}
    layout = {}
    for index, node in enumerate(flow["nodes"]):
        node_id = node["id"]
        position = node.get("position") or {"x": 80 + (index % 4) * 360, "y": 100 + (index // 4) * 220}
        layout[node_id] = position
        nodes[node_id] = {
            "label": node.get("label", node_id.replace("_", " ").title()),
            "kind": node["type"],
            "action": node["type"],
            "reply": node.get("config", {}).get("reply", ""),
            "instructions": node.get("config", {}).get("instructions", []),
            "routing_instructions": node.get("config", {}).get("routing_instructions", ""),
            "routes": routes[node_id],
            "terminal": node["type"] == "end",
        }
    return {
        "id": flow["id"], "version": flow["version"], "name": flow["name"],
        "description": flow.get("description", ""), "start_node": next(n["id"] for n in flow["nodes"] if n["type"] == "start"),
        "nodes": nodes, "layout": layout,
        "jev_instructions": flow.get("jev_instructions", ""),
    }


DEMO_FLOW = {
    "id": "demo-flow", "version": 1, "name": "Jev routing demo",
    "description": "A generalized graph where Jev selects the next edge.",
    "goal": {"primary": "complete_conversation", "success_conditions": []},
    "variables": {},
    "jev_instructions": "Use the latest user message and conversation history. Follow the current node guidance, choose only a declared edge, and stay on the current node when the user is unclear.",
    "nodes": [
        {"id": "welcome", "type": "start", "label": "Welcome", "config": {"reply": "Welcome. How can I help?"}, "position": {"x": 80, "y": 160}},
        {"id": "answer", "type": "agent", "label": "Answer", "config": {"reply": "Answer the user's latest request using the available context.", "model": "openai/gpt-4o-mini", "system_prompt": "You are a helpful conversational AI agent. Answer clearly and concisely.", "temperature": 0.2, "max_tokens": 300}, "position": {"x": 440, "y": 80}},
        {"id": "finish", "type": "end", "label": "Finish", "config": {"reply": "Goodbye."}, "position": {"x": 800, "y": 260}},
    ],
    "edges": [
        {"from": "welcome", "to": "answer", "key": "answer", "description": "The user asks for information or help."},
        {"from": "welcome", "to": "finish", "key": "finish", "description": "The user wants to end the interaction."},
        {"from": "answer", "to": "welcome", "key": "welcome", "description": "The user starts a new topic or needs clarification."},
        {"from": "answer", "to": "finish", "key": "finish", "description": "The user wants to end the interaction."},
    ],
}
