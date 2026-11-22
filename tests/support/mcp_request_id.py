"""Explicit JSON-RPC request IDs for MCP Python SDK client contract tests.

The current SDK's ``ClientSession.call_tool`` does not expose ``CallOptions``.
This helper uses its stamping seam to pass the documented dispatcher
``request_id`` option while keeping call construction, protocol negotiation,
transport, serialization and response parsing in the real SDK.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

from mcp import ClientSession
from mcp.shared.dispatcher import RequestId


@asynccontextmanager
async def _request_id_for_next_call(
    session: ClientSession, request_id: RequestId
) -> AsyncIterator[None]:
    """Override the request ID for one ``ClientSession.call_tool`` operation."""

    stamp = session._stamp

    def stamp_with_id(data: dict[str, Any], options: dict[str, Any]) -> None:
        stamp(data, options)
        if data.get("method") == "tools/call":
            options["request_id"] = request_id

    session._stamp = stamp_with_id
    try:
        yield
    finally:
        session._stamp = stamp


async def call_tool_with_request_id(
    session: ClientSession,
    request_id: RequestId,
    name: str,
    arguments: dict[str, Any],
) -> Any:
    """Call a tool using an explicit outer JSON-RPC ID over the session transport."""

    async with _request_id_for_next_call(session, request_id):
        return await session.call_tool(name, arguments)
