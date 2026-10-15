"""Built-in tool catalog and safe execution boundary for workflow tool nodes.

Tool nodes store a tool identifier and a credential reference, never provider
secrets. Execution is dry-run by default; live HTTP execution is opt-in.
"""

from __future__ import annotations

import os
from typing import Any
from urllib.parse import urlparse

import httpx


TOOL_DEFINITIONS: tuple[dict[str, Any], ...] = (
    {
        "id": "http_request",
        "label": "HTTP Request",
        "description": "Call an external API or service.",
        "category": "HTTP",
        "auth": "optional",
        "operations": ["request"],
        "fields": [
            {"name": "method", "type": "select", "options": ["GET", "POST", "PUT", "PATCH", "DELETE"]},
            {"name": "url", "type": "url", "required": True},
            {"name": "headers", "type": "object"},
            {"name": "body", "type": "object"},
        ],
    },
    {
        "id": "gmail",
        "label": "Gmail",
        "description": "Send, read, search, and manage emails.",
        "category": "Google",
        "auth": "oauth2",
        "operations": ["send", "search", "read"],
        "fields": [
            {"name": "operation", "type": "select", "options": ["send", "search", "read"], "required": True},
            {"name": "to", "type": "string"},
            {"name": "subject", "type": "string"},
            {"name": "body", "type": "string"},
            {"name": "query", "type": "string"},
        ],
    },
    {
        "id": "google_sheets",
        "label": "Google Sheets",
        "description": "Read, append, and update spreadsheet data.",
        "category": "Google",
        "auth": "oauth2",
        "operations": ["read", "append", "update"],
        "fields": [
            {"name": "operation", "type": "select", "options": ["read", "append", "update"], "required": True},
            {"name": "spreadsheet_id", "type": "string", "required": True},
            {"name": "range", "type": "string", "required": True},
            {"name": "values", "type": "array"},
        ],
    },
    {
        "id": "google_calendar",
        "label": "Google Calendar",
        "description": "Create events and check availability.",
        "category": "Google",
        "auth": "oauth2",
        "operations": ["create_event", "list_events", "availability"],
        "fields": [
            {"name": "operation", "type": "select", "options": ["create_event", "list_events", "availability"], "required": True},
            {"name": "calendar_id", "type": "string"},
            {"name": "start", "type": "datetime"},
            {"name": "end", "type": "datetime"},
            {"name": "summary", "type": "string"},
        ],
    },
)

TOOL_BY_ID = {tool["id"]: tool for tool in TOOL_DEFINITIONS}
TOOL_ID_BY_LABEL = {tool["label"].lower(): tool["id"] for tool in TOOL_DEFINITIONS}


class ToolConfigurationError(ValueError):
    """Raised when a tool node cannot be safely configured."""


def list_tools() -> list[dict[str, Any]]:
    return [dict(tool) for tool in TOOL_DEFINITIONS]


def tool_id_for_node(node: dict[str, Any]) -> str | None:
    config = node.get("config") or {}
    configured = config.get("tool_id")
    if isinstance(configured, str) and configured in TOOL_BY_ID:
        return configured
    label = node.get("label")
    return TOOL_ID_BY_LABEL.get(str(label).strip().lower()) if label else None


def validate_tool_config(node: dict[str, Any]) -> None:
    tool_id = tool_id_for_node(node)
    if not tool_id:
        raise ToolConfigurationError(f"node {node.get('id')}: config.tool_id must identify a supported tool")
    config = node.get("config") or {}
    if not isinstance(config, dict):
        raise ToolConfigurationError(f"node {node.get('id')}: config must be an object")
    parameters = config.get("parameters", {})
    if not isinstance(parameters, dict):
        raise ToolConfigurationError(f"node {node.get('id')}: config.parameters must be an object")
    if "credential_id" in config and not isinstance(config["credential_id"], str):
        raise ToolConfigurationError(f"node {node.get('id')}: config.credential_id must be a string")
    if "approval_required" in config and not isinstance(config["approval_required"], bool):
        raise ToolConfigurationError(f"node {node.get('id')}: config.approval_required must be boolean")
    operation = parameters.get("operation")
    if operation is not None and operation not in TOOL_BY_ID[tool_id]["operations"]:
        raise ToolConfigurationError(f"node {node.get('id')}: unsupported {tool_id} operation {operation!r}")


def validate_attached_tool(tool: dict[str, Any], node_id: str) -> None:
    if not isinstance(tool, dict) or tool.get("tool_id") not in TOOL_BY_ID:
        raise ToolConfigurationError(f"node {node_id}: attached tool must use a supported tool_id")
    if "credential_id" in tool and not isinstance(tool["credential_id"], str):
        raise ToolConfigurationError(f"node {node_id}: attached tool credential_id must be a string")
    if "approval_required" in tool and not isinstance(tool["approval_required"], bool):
        raise ToolConfigurationError(f"node {node_id}: attached tool approval_required must be boolean")
    parameters = tool.get("parameters", {})
    if not isinstance(parameters, dict):
        raise ToolConfigurationError(f"node {node_id}: attached tool parameters must be an object")
    operation = parameters.get("operation")
    if operation is not None and operation not in TOOL_BY_ID[tool["tool_id"]]["operations"]:
        raise ToolConfigurationError(f"node {node_id}: unsupported attached tool operation {operation!r}")


def tool_public_config(node: dict[str, Any]) -> dict[str, Any]:
    """Return non-secret tool metadata suitable for trace/UI responses."""
    tool_id = tool_id_for_node(node)
    return {
        "tool_id": tool_id,
        "credential_id": (node.get("config") or {}).get("credential_id"),
        "approval_required": bool((node.get("config") or {}).get("approval_required", True)),
        "parameters": (node.get("config") or {}).get("parameters", {}),
    }


async def execute_tool(node: dict[str, Any], messages: list[dict[str, str]]) -> dict[str, Any]:
    """Execute a tool with an explicit dry-run safety default.

    Provider-specific OAuth adapters are intentionally not inferred from a
    node label. Gmail, Sheets, and Calendar return a structured configuration
    status until a credential-backed adapter is registered.
    """
    validate_tool_config(node)
    config = node.get("config") or {}
    tool_id = tool_id_for_node(node)
    assert tool_id is not None
    definition = TOOL_BY_ID[tool_id]
    credential_id = config.get("credential_id")
    if definition["auth"] != "optional" and not credential_id:
        return {"status": "blocked", "tool_id": tool_id, "reason": "A credential must be connected before this tool can run."}
    if config.get("approval_required", True) and os.getenv("ORCHESTRATION_TOOL_APPROVALS", "true").lower() == "true":
        return {"status": "awaiting_approval", "tool_id": tool_id, "reason": "This tool action requires approval before execution."}
    if os.getenv("ORCHESTRATION_TOOL_MODE", "dry_run").lower() != "live":
        return {"status": "dry_run", "tool_id": tool_id, "input": tool_public_config(node), "reason": "Tool execution is in dry-run mode."}
    if tool_id != "http_request":
        return {"status": "unavailable", "tool_id": tool_id, "reason": "No credential-backed adapter is registered for this tool yet."}

    parameters = config.get("parameters", {})
    url = str(parameters.get("url", ""))
    parsed = urlparse(url)
    configured_hosts = {host.strip().lower() for host in os.getenv("ORCHESTRATION_HTTP_ALLOWED_HOSTS", "").split(",") if host.strip()}
    allowed_hosts = {"api.nasdaq.com", *configured_hosts}
    if parsed.scheme not in {"http", "https"} or not parsed.hostname or parsed.hostname.lower() not in allowed_hosts:
        return {"status": "blocked", "tool_id": tool_id, "reason": "HTTP URL is not on the configured allowlist."}
    method = str(parameters.get("method", "GET")).upper()
    headers = {"User-Agent": "Agent-Orchestration/0.1", "Accept": "application/json"}
    headers.update(parameters.get("headers") or {})
    try:
        async with httpx.AsyncClient(timeout=httpx.Timeout(10, connect=3), follow_redirects=False) as client:
            response = await client.request(method, url, headers=headers, json=parameters.get("body"))
    except httpx.HTTPError as exc:
        return {"status": "failed", "tool_id": tool_id, "reason": f"HTTP request failed: {type(exc).__name__}"}
    return {"status": "completed", "tool_id": tool_id, "http_status": response.status_code, "output": response.text[:4000]}
