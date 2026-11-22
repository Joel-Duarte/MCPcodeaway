"""Verify explicit JSON-RPC ID behavior through the official Python MCP SDK."""

from __future__ import annotations

from typing import Any

import httpx2
import mcp.shared._otel as mcp_otel
import pytest
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, Response
from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client
from mcp.shared.exceptions import MCPError
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.trace import NoOpTracerProvider

from tests.support.mcp_request_id import call_tool_with_request_id


def _recording_app() -> tuple[FastAPI, list[dict[str, Any]], dict[str | int, dict[str, Any]]]:
    app = FastAPI()
    calls: list[dict[str, Any]] = []
    bodies_by_id: dict[str | int, dict[str, Any]] = {}

    @app.post("/mcp")
    async def mcp(request: Request) -> Response:
        body = await request.json()
        method = body.get("method")
        request_id = body.get("id")
        if method == "notifications/initialized":
            return Response(status_code=202)
        if method == "initialize":
            return JSONResponse(
                {
                    "jsonrpc": "2.0",
                    "id": request_id,
                    "result": {
                        "protocolVersion": body["params"]["protocolVersion"],
                        "capabilities": {"tools": {}},
                        "serverInfo": {"name": "request-id-recorder", "version": "1.0.0"},
                    },
                }
            )
        if method == "tools/list":
            return JSONResponse(
                {
                    "jsonrpc": "2.0",
                    "id": request_id,
                    "result": {
                        "tools": [
                            {
                                "name": "create_ticket",
                                "inputSchema": {
                                    "type": "object",
                                    "properties": {"summary": {"type": "string"}},
                                    "required": ["summary"],
                                    "additionalProperties": False,
                                },
                            }
                        ]
                    },
                }
            )
        if method != "tools/call":
            return JSONResponse(
                {
                    "jsonrpc": "2.0",
                    "id": request_id,
                    "error": {"code": -32601, "message": "method not found"},
                }
            )

        calls.append(body)
        previous = bodies_by_id.get(request_id)
        if previous is not None and previous != body:
            return JSONResponse(
                {
                    "jsonrpc": "2.0",
                    "id": request_id,
                    "error": {"code": -32021, "message": "request ID reused with a different body"},
                }
            )
        bodies_by_id[request_id] = body
        arguments = body.get("params", {}).get("arguments", {})
        result = {"accepted": True, "summary": arguments.get("summary")}
        return JSONResponse(
            {
                "jsonrpc": "2.0",
                "id": request_id,
                "result": {
                    "content": [{"type": "text", "text": "accepted"}],
                    "structuredContent": result,
                },
            }
        )

    return app, calls, bodies_by_id


@pytest.mark.asyncio
async def test_python_sdk_can_retransmit_same_id_and_reject_changed_body(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        mcp_otel,
        "_tracer",
        NoOpTracerProvider().get_tracer("mcp-request-id-contract-test"),
    )
    app, calls, bodies_by_id = _recording_app()
    transport = httpx2.ASGITransport(app=app)
    async with httpx2.AsyncClient(transport=transport, base_url="http://mcp.test") as http_client:
        async with streamable_http_client(
            "http://mcp.test/mcp", http_client=http_client
        ) as streams:
            async with ClientSession(*streams) as session:
                await session.initialize()

                first = await call_tool_with_request_id(
                    session, "support-operation-1", "create_ticket", {"summary": "late delivery"}
                )
                retransmission = await call_tool_with_request_id(
                    session, "support-operation-1", "create_ticket", {"summary": "late delivery"}
                )
                with pytest.raises(MCPError, match="request ID reused with a different body"):
                    await call_tool_with_request_id(
                        session, "support-operation-1", "create_ticket", {"summary": "wrong body"}
                    )
                deliberate_repeat = await call_tool_with_request_id(
                    session, "support-operation-2", "create_ticket", {"summary": "late delivery"}
                )

    assert first.structured_content == retransmission.structured_content
    assert deliberate_repeat.structured_content == first.structured_content
    assert calls[0] == calls[1]
    assert calls[0]["id"] == calls[1]["id"] == "support-operation-1"
    assert calls[2]["id"] == "support-operation-1"
    assert calls[3]["id"] == "support-operation-2"
    assert calls[3]["params"]["arguments"] == calls[0]["params"]["arguments"]
    assert len(bodies_by_id) == 2


@pytest.mark.asyncio
async def test_sdk_trace_injection_changes_body_for_same_id_and_arguments(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        mcp_otel, "_tracer", TracerProvider().get_tracer("mcp-request-id-contract-test")
    )
    app, calls, _ = _recording_app()
    transport = httpx2.ASGITransport(app=app)
    async with httpx2.AsyncClient(transport=transport, base_url="http://mcp.test") as http_client:
        async with streamable_http_client(
            "http://mcp.test/mcp", http_client=http_client
        ) as streams:
            async with ClientSession(*streams) as session:
                await session.initialize()
                await call_tool_with_request_id(
                    session, "support-operation-1", "create_ticket", {"summary": "late delivery"}
                )
                with pytest.raises(MCPError, match="request ID reused with a different body"):
                    await call_tool_with_request_id(
                        session,
                        "support-operation-1",
                        "create_ticket",
                        {"summary": "late delivery"},
                    )

    assert calls[0]["id"] == calls[1]["id"] == "support-operation-1"
    assert calls[0]["params"]["arguments"] == calls[1]["params"]["arguments"]
    assert calls[0]["params"]["_meta"]["traceparent"] != calls[1]["params"]["_meta"]["traceparent"]
