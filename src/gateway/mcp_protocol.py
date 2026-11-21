"""Shared helpers for the gateway's current, sessionless MCP HTTP contract."""

from __future__ import annotations

import base64
from typing import Any

MCP_PROTOCOL_VERSION = "2026-07-28"
MCP_PROTOCOL_VERSION_META = "io.modelcontextprotocol/protocolVersion"
MCP_CLIENT_CAPABILITIES_META = "io.modelcontextprotocol/clientCapabilities"
MCP_CLIENT_INFO_META = "io.modelcontextprotocol/clientInfo"
MCP_SERVER_INFO_META = "io.modelcontextprotocol/serverInfo"
MCP_TASKS_EXTENSION = "io.modelcontextprotocol/tasks"


def request_meta(
    *, client_name: str = "mcpcodeaway-gateway", supports_tasks: bool = False
) -> dict[str, Any]:
    capabilities: dict[str, Any] = {}
    if supports_tasks:
        capabilities["extensions"] = {MCP_TASKS_EXTENSION: {}}
    return {
        MCP_PROTOCOL_VERSION_META: MCP_PROTOCOL_VERSION,
        MCP_CLIENT_CAPABILITIES_META: capabilities,
        MCP_CLIENT_INFO_META: {"name": client_name, "version": "0.1.0"},
    }


def request_headers(
    method: str, *, tool_name: str | None = None, task_id: str | None = None
) -> dict[str, str]:
    headers = {
        "Content-Type": "application/json",
        "Accept": "application/json, text/event-stream",
        "MCP-Protocol-Version": MCP_PROTOCOL_VERSION,
        "Mcp-Method": method,
    }
    if tool_name is not None:
        headers["Mcp-Name"] = tool_name
    elif method in {"tasks/get", "tasks/update", "tasks/cancel"} and task_id is not None:
        headers["Mcp-Name"] = task_id
    name = headers.get("Mcp-Name")
    if name is not None:
        invalid_token_char = any(
            not (character.isalnum() or character in "!#$%&'*+-.^_`|~") for character in name
        )
        if not name.isascii() or invalid_token_char:
            encoded = base64.b64encode(name.encode("utf-8")).decode("ascii")
            headers["Mcp-Name"] = f"=?base64?{encoded}?="
    return headers
