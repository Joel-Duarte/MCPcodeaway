"""Shared helpers for the gateway's current, sessionless MCP HTTP contract."""

from __future__ import annotations

import base64
from typing import Any

MCP_PROTOCOL_VERSION = "2026-07-28"
MCP_PROTOCOL_VERSION_META = "io.modelcontextprotocol/protocolVersion"
MCP_CLIENT_CAPABILITIES_META = "io.modelcontextprotocol/clientCapabilities"
MCP_CLIENT_INFO_META = "io.modelcontextprotocol/clientInfo"
MCP_SERVER_INFO_META = "io.modelcontextprotocol/serverInfo"


def request_meta(*, client_name: str = "mcpcodeaway-gateway") -> dict[str, Any]:
    return {
        MCP_PROTOCOL_VERSION_META: MCP_PROTOCOL_VERSION,
        MCP_CLIENT_CAPABILITIES_META: {},
        MCP_CLIENT_INFO_META: {"name": client_name, "version": "0.1.0"},
    }


def request_headers(method: str, *, tool_name: str | None = None) -> dict[str, str]:
    headers = {
        "Content-Type": "application/json",
        "Accept": "application/json, text/event-stream",
        "MCP-Protocol-Version": MCP_PROTOCOL_VERSION,
        "Mcp-Method": method,
    }
    if tool_name is not None:
        invalid_token_char = any(
            not (character.isalnum() or character in "!#$%&'*+-.^_`|~") for character in tool_name
        )
        if not tool_name.isascii() or invalid_token_char:
            encoded = base64.b64encode(tool_name.encode("utf-8")).decode("ascii")
            tool_name = f"=?base64?{encoded}?="
        headers["Mcp-Name"] = tool_name
    return headers
