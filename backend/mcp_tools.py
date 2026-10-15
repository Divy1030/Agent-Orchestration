"""Client-side helpers for remote MCP Streamable HTTP servers."""

from __future__ import annotations

import json
from typing import Any
from urllib.parse import urlparse

from backend.tool_registry import _blocked_http_target


class McpConnectionError(ValueError):
    """Raised when an MCP server cannot be safely contacted or used."""


def expand_mcp_servers(servers: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Flatten server connections only at the model function-calling boundary.

    Workflow storage and UI treat each MCP server as one attachment; each
    server-provided function is expanded here so the model can choose a call.
    Older workflows with one function per attachment remain readable.
    """
    functions: list[dict[str, Any]] = []
    for server in servers:
        remote_functions = server.get("functions")
        if isinstance(remote_functions, list):
            for remote in remote_functions:
                if not isinstance(remote, dict) or not remote.get("name"):
                    continue
                functions.append({
                    **remote,
                    "mcp_tool_name": str(remote["name"]),
                    "mcp_server_name": str(server.get("name") or server.get("label") or "MCP Server"),
                    "server_url": server.get("server_url", ""),
                    "approval_required": server.get("approval_required", True),
                })
        elif server.get("mcp_tool_name"):
            # Backward compatibility for the earlier per-function format.
            functions.append({**server, "name": server.get("mcp_tool_name")})
    return functions


def validate_mcp_url(url: str) -> str:
    parsed = urlparse(url.strip())
    if parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password or parsed.fragment:
        raise McpConnectionError("MCP server URL must be an HTTPS URL without embedded credentials or a fragment.")
    blocked = _blocked_http_target(url.strip())
    if blocked:
        raise McpConnectionError(blocked)
    return url.strip()


def _plain_tool(tool: Any) -> dict[str, Any]:
    if hasattr(tool, "model_dump"):
        data = tool.model_dump(mode="json", by_alias=True)
    elif hasattr(tool, "dict"):
        data = tool.dict(by_alias=True)
    else:
        data = dict(tool)
    schema = data.get("inputSchema", data.get("input_schema", {}))
    if not isinstance(schema, dict):
        schema = {"type": "object", "properties": {}}
    if len(json.dumps(schema, ensure_ascii=False, default=str)) > 32000:
        raise McpConnectionError("An MCP server returned an input schema that exceeds the 32 KB limit.")
    return {
        "name": str(data.get("name", ""))[:128],
        "description": str(data.get("description", ""))[:2000],
        "input_schema": schema,
    }


async def discover_mcp_tools(url: str) -> list[dict[str, Any]]:
    safe_url = validate_mcp_url(url)
    try:
        from mcp import Client

        async with Client(safe_url) as client:
            response = await client.list_tools()
        tools = [_plain_tool(tool) for tool in response.tools[:100]]
        return [tool for tool in tools if tool["name"]]
    except McpConnectionError:
        raise
    except Exception as exc:
        raise McpConnectionError(f"Could not discover MCP tools ({type(exc).__name__}). Check the server URL and availability.") from exc


async def call_mcp_tool(url: str, name: str, arguments: dict[str, Any]) -> dict[str, Any]:
    safe_url = validate_mcp_url(url)
    try:
        from mcp import Client

        async with Client(safe_url) as client:
            result = await client.call_tool(name, arguments)
        if hasattr(result, "model_dump"):
            data = result.model_dump(mode="json", by_alias=True)
        elif hasattr(result, "dict"):
            data = result.dict(by_alias=True)
        else:
            data = {"content": getattr(result, "content", []), "isError": getattr(result, "isError", False)}
        # MCP tool output is untrusted external content and is size-limited before
        # it is passed to the model as tool-result context.
        encoded = json.dumps(data, ensure_ascii=False, default=str)
        return {"is_error": bool(data.get("isError", data.get("is_error", False))), "content": encoded[:16000]}
    except Exception as exc:
        return {"is_error": True, "content": f"MCP tool call failed ({type(exc).__name__})."}
