"""Deterministic test-only MCP upstream for support workflow attribution tests.

The fixture deliberately models the upstream business API only. Approval denial
and evidence-persistence failure are harness directives: the gateway test must
exercise those at the gateway boundary, since neither is an upstream outcome.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from gateway.mcp_protocol import (
    MCP_CLIENT_CAPABILITIES_META,
    MCP_PROTOCOL_VERSION,
    MCP_PROTOCOL_VERSION_META,
    MCP_SERVER_INFO_META,
)

TOOL_NAMES = (
    "find_customer",
    "get_account",
    "get_order_history",
    "create_ticket",
)

_TOOLS: list[dict[str, Any]] = [
    {
        "name": "find_customer",
        "description": "Find a customer by their reference.",
        "inputSchema": {
            "type": "object",
            "properties": {"customer_reference": {"type": "string"}},
            "required": ["customer_reference"],
            "additionalProperties": False,
        },
        "outputSchema": {
            "type": "object",
            "properties": {
                "customer": {
                    "type": "object",
                    "properties": {"id": {"type": "string"}, "name": {"type": "string"}},
                    "required": ["id", "name"],
                    "additionalProperties": False,
                }
            },
            "required": ["customer"],
            "additionalProperties": False,
        },
    },
    {
        "name": "get_account",
        "description": "Read the customer's account.",
        "inputSchema": {
            "type": "object",
            "properties": {"customer_id": {"type": "string"}},
            "required": ["customer_id"],
            "additionalProperties": False,
        },
        "outputSchema": {
            "type": "object",
            "properties": {
                "account": {
                    "type": "object",
                    "properties": {"id": {"type": "string"}, "status": {"type": "string"}},
                    "required": ["id", "status"],
                    "additionalProperties": False,
                }
            },
            "required": ["account"],
            "additionalProperties": False,
        },
    },
    {
        "name": "get_order_history",
        "description": "Read the customer's order history.",
        "inputSchema": {
            "type": "object",
            "properties": {"customer_id": {"type": "string"}},
            "required": ["customer_id"],
            "additionalProperties": False,
        },
        "outputSchema": {
            "type": "object",
            "properties": {"orders": {"type": "array", "items": {"type": "object"}}},
            "required": ["orders"],
            "additionalProperties": False,
        },
    },
    {
        "name": "create_ticket",
        "description": "Create a support ticket.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "customer_id": {"type": "string"},
                "summary": {"type": "string"},
                "idempotency_key": {"type": "string"},
            },
            "required": ["customer_id", "summary"],
            "additionalProperties": False,
        },
        "outputSchema": {
            "type": "object",
            "properties": {
                "ticket": {
                    "type": "object",
                    "properties": {"id": {"type": "string"}, "status": {"type": "string"}},
                    "required": ["id", "status"],
                    "additionalProperties": False,
                }
            },
            "required": ["ticket"],
            "additionalProperties": False,
        },
    },
]


@dataclass
class MockSupportScenario:
    """Fault switches and gateway-harness directives for deterministic tests."""

    invalid_result_tools: frozenset[str] = frozenset()
    upstream_error_tools: frozenset[str] = frozenset()
    ambiguous_ticket_response_once: bool = False
    approval_denied: bool = False
    persistence_failure: bool = False


@dataclass
class MockSupportState:
    """Mutable fixture state exposed to tests for assertions and fault control."""

    scenario: MockSupportScenario = field(default_factory=MockSupportScenario)
    customers: dict[str, dict[str, str]] = field(
        default_factory=lambda: {
            "CUST-1001": {"id": "CUST-1001", "name": "Avery Example"},
            "CUST-1002": {"id": "CUST-1002", "name": "Riley Sample"},
        }
    )
    accounts: dict[str, dict[str, str]] = field(
        default_factory=lambda: {
            "CUST-1001": {"id": "ACCT-1001", "status": "active"},
            "CUST-1002": {"id": "ACCT-1002", "status": "review"},
        }
    )
    orders: dict[str, list[dict[str, str]]] = field(
        default_factory=lambda: {
            "CUST-1001": [{"id": "ORD-1001", "status": "delivered"}],
            "CUST-1002": [{"id": "ORD-1002", "status": "returned"}],
        }
    )
    tickets: list[dict[str, str]] = field(default_factory=list)
    events: list[dict[str, Any]] = field(default_factory=list)
    _ticket_by_idempotency_key: dict[str, dict[str, str]] = field(default_factory=dict)
    _ambiguous_response_used: bool = False


def _error(req_id: Any, code: int, message: str, status: int = 200) -> JSONResponse:
    return JSONResponse(
        {"jsonrpc": "2.0", "id": req_id, "error": {"code": code, "message": message}},
        status_code=status,
    )


def _result(req_id: Any, result: dict[str, Any]) -> JSONResponse:
    result.setdefault("_meta", {MCP_SERVER_INFO_META: {"name": "mock-support", "version": "1.0.0"}})
    return JSONResponse({"jsonrpc": "2.0", "id": req_id, "result": result})


def _ticket_id(state: MockSupportState) -> str:
    return f"TKT-{len(state.tickets) + 5001:04d}"


def _tool_result(state: MockSupportState, name: str, args: dict[str, Any]) -> dict[str, Any]:
    if name == "find_customer":
        reference = str(args["customer_reference"])
        customer = state.customers.get(reference)
        if customer is None:
            raise ValueError(f"Unknown customer reference '{reference}'")
        data: dict[str, Any] = {"customer": dict(customer)}
    elif name == "get_account":
        customer_id = str(args["customer_id"])
        account = state.accounts.get(customer_id)
        if account is None:
            raise ValueError(f"Unknown customer '{customer_id}'")
        data = {"account": dict(account)}
    elif name == "get_order_history":
        customer_id = str(args["customer_id"])
        orders = state.orders.get(customer_id)
        if orders is None:
            raise ValueError(f"Unknown customer '{customer_id}'")
        data = {"orders": [dict(order) for order in orders]}
    elif name == "create_ticket":
        customer_id = str(args["customer_id"])
        if customer_id not in state.customers:
            raise ValueError(f"Unknown customer '{customer_id}'")
        summary = str(args["summary"])
        key = str(args.get("idempotency_key") or f"{customer_id}\0{summary}")
        ticket = state._ticket_by_idempotency_key.get(key)
        if ticket is None:
            ticket = {"id": _ticket_id(state), "status": "open"}
            state._ticket_by_idempotency_key[key] = ticket
            state.tickets.append(ticket)
        data = {"ticket": dict(ticket)}
    else:
        raise KeyError(f"Unknown tool '{name}'")

    if name in state.scenario.invalid_result_tools:
        return {"unexpected": "invalid fixture result"}
    return data


def create_mock_support_workflow_app(
    state: MockSupportState | None = None,
) -> FastAPI:
    """Build an in-memory, test-only MCP support API with observable state."""

    fixture = state or MockSupportState()
    app = FastAPI(title="Mock Support Workflow MCP API")
    app.state.mock_support = fixture

    @app.post("/mcp")
    async def mcp(request: Request) -> JSONResponse:
        payload = await request.json()
        req_id = payload.get("id")
        method = payload.get("method")
        params = payload.get("params") or {}
        meta = params.get("_meta") or {}
        if (
            payload.get("jsonrpc") != "2.0"
            or not isinstance(req_id, (str, int))
            or isinstance(req_id, bool)
            or meta.get(MCP_PROTOCOL_VERSION_META) != MCP_PROTOCOL_VERSION
            or not isinstance(meta.get(MCP_CLIENT_CAPABILITIES_META), dict)
            or request.headers.get("mcp-protocol-version") != MCP_PROTOCOL_VERSION
            or request.headers.get("mcp-method") != method
        ):
            return _error(req_id, -32020, "Invalid current MCP request framing", 400)
        if request.headers.get("mcp-session-id"):
            return _error(req_id, -32020, "MCP protocol sessions are not supported", 400)
        if method == "server/discover":
            return _result(
                req_id,
                {
                    "supportedVersions": [MCP_PROTOCOL_VERSION],
                    "capabilities": {"tools": {}},
                    "ttlMs": 30000,
                    "cacheScope": "private",
                },
            )
        if method == "tools/list":
            return _result(req_id, {"tools": _TOOLS, "ttlMs": 0, "cacheScope": "private"})
        if method != "tools/call":
            return _error(req_id, -32601, f"Unsupported method '{method}'")

        name = str(params.get("name") or "")
        if request.headers.get("mcp-name") != name:
            return _error(req_id, -32020, "Mcp-Name does not match tool name", 400)
        args = params.get("arguments") or {}
        if not isinstance(args, dict):
            return _error(req_id, -32602, "Tool arguments must be an object")
        fixture.events.append({"tool": name, "arguments": dict(args)})
        if name not in TOOL_NAMES:
            return _error(req_id, -32601, f"Unknown tool '{name}'")
        if name in fixture.scenario.upstream_error_tools:
            return _error(req_id, -32050, f"Configured upstream error for '{name}'")
        try:
            data = _tool_result(fixture, name, args)
        except KeyError:
            return _error(req_id, -32602, "Missing required tool argument")
        except ValueError:
            return _error(req_id, -32602, "Invalid tool arguments")

        if (
            name == "create_ticket"
            and fixture.scenario.ambiguous_ticket_response_once
            and not fixture._ambiguous_response_used
        ):
            fixture._ambiguous_response_used = True
            return JSONResponse(
                {"error": "simulated lost response after ticket creation"}, status_code=503
            )
        return _result(
            req_id,
            {"content": [{"type": "text", "text": json.dumps(data)}], "structuredContent": data},
        )

    return app


__all__ = [
    "MockSupportScenario",
    "MockSupportState",
    "TOOL_NAMES",
    "create_mock_support_workflow_app",
]
