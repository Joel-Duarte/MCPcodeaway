"""Focused contract tests for the deterministic support MCP fixture."""

from __future__ import annotations

import httpx
import pytest

from examples.mock_support_workflow import (
    TOOL_NAMES,
    MockSupportScenario,
    MockSupportState,
    create_mock_support_workflow_app,
)
from gateway.mcp_protocol import request_headers, request_meta


async def _rpc(
    client: httpx.AsyncClient,
    method: str,
    *,
    request_id: int = 1,
    name: str | None = None,
    arguments: dict[str, object] | None = None,
) -> httpx.Response:
    headers = request_headers(method, tool_name=name)
    params: dict[str, object] = {"_meta": request_meta()}
    if name is not None:
        params.update({"name": name, "arguments": arguments or {}})
    return await client.post(
        "/mcp",
        headers=headers,
        json={"jsonrpc": "2.0", "id": request_id, "method": method, "params": params},
    )


async def _call(
    client: httpx.AsyncClient,
    name: str,
    arguments: dict[str, object],
    *,
    request_id: int = 1,
) -> httpx.Response:
    return await _rpc(client, "tools/call", request_id=request_id, name=name, arguments=arguments)


def _structured(response: httpx.Response) -> dict[str, object]:
    return response.json()["result"]["structuredContent"]


@pytest.mark.asyncio
async def test_mock_support_tools_discover_with_output_schemas() -> None:
    app = create_mock_support_workflow_app()
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://mock"
    ) as client:
        response = await _rpc(client, "tools/list")

    assert response.status_code == 200
    result = response.json()["result"]
    assert [tool["name"] for tool in result["tools"]] == list(TOOL_NAMES)
    assert all("outputSchema" in tool for tool in result["tools"])


@pytest.mark.asyncio
async def test_mock_support_workflow_returns_stable_fixture_evidence_and_ticket() -> None:
    state = MockSupportState()
    app = create_mock_support_workflow_app(state)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://mock"
    ) as client:
        customer = await _call(client, "find_customer", {"customer_reference": "CUST-1001"})
        account = await _call(client, "get_account", {"customer_id": "CUST-1001"})
        orders = await _call(client, "get_order_history", {"customer_id": "CUST-1001"})
        ticket = await _call(
            client, "create_ticket", {"customer_id": "CUST-1001", "summary": "Late delivery"}
        )
        repeated = await _call(
            client,
            "create_ticket",
            {"customer_id": "CUST-1001", "summary": "Late delivery"},
            request_id=2,
        )

    assert _structured(customer) == {"customer": {"id": "CUST-1001", "name": "Avery Example"}}
    assert _structured(account) == {"account": {"id": "ACCT-1001", "status": "active"}}
    assert _structured(orders) == {"orders": [{"id": "ORD-1001", "status": "delivered"}]}
    assert (
        _structured(ticket)
        == _structured(repeated)
        == {"ticket": {"id": "TKT-5001", "status": "open"}}
    )
    assert len(state.tickets) == 1
    assert [event["tool"] for event in state.events] == list(TOOL_NAMES) + ["create_ticket"]


@pytest.mark.asyncio
async def test_missing_lookup_evidence_is_visible_when_call_is_omitted() -> None:
    state = MockSupportState()
    app = create_mock_support_workflow_app(state)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://mock"
    ) as client:
        await _call(client, "find_customer", {"customer_reference": "CUST-1001"})
        await _call(client, "get_account", {"customer_id": "CUST-1001"})
        await _call(
            client, "create_ticket", {"customer_id": "CUST-1001", "summary": "Late delivery"}
        )

    called = {event["tool"] for event in state.events}
    assert "get_order_history" not in called
    assert "create_ticket" in called


@pytest.mark.asyncio
async def test_invalid_result_and_upstream_error_switches_are_deterministic() -> None:
    state = MockSupportState(
        scenario=MockSupportScenario(
            invalid_result_tools=frozenset({"get_order_history"}),
            upstream_error_tools=frozenset({"get_account"}),
        )
    )
    app = create_mock_support_workflow_app(state)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://mock"
    ) as client:
        invalid = await _call(client, "get_order_history", {"customer_id": "CUST-1001"})
        failed = await _call(client, "get_account", {"customer_id": "CUST-1001"})

    assert _structured(invalid) == {"unexpected": "invalid fixture result"}
    assert failed.json()["error"]["code"] == -32050
    assert "Configured upstream error" in failed.json()["error"]["message"]


@pytest.mark.asyncio
async def test_ambiguous_ticket_response_applies_effect_once_then_allows_recovery() -> None:
    state = MockSupportState(scenario=MockSupportScenario(ambiguous_ticket_response_once=True))
    app = create_mock_support_workflow_app(state)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://mock"
    ) as client:
        lost = await _call(
            client, "create_ticket", {"customer_id": "CUST-1001", "summary": "Late delivery"}
        )
        recovered = await _call(
            client,
            "create_ticket",
            {"customer_id": "CUST-1001", "summary": "Late delivery"},
            request_id=2,
        )

    assert lost.status_code == 503
    assert "lost response" in lost.json()["error"]
    assert _structured(recovered) == {"ticket": {"id": "TKT-5001", "status": "open"}}
    assert len(state.tickets) == 1


@pytest.mark.asyncio
async def test_gateway_harness_directives_are_exposed_without_faking_upstream_calls() -> None:
    scenario = MockSupportScenario(approval_denied=True, persistence_failure=True)
    state = MockSupportState(scenario=scenario)
    app = create_mock_support_workflow_app(state)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://mock"
    ) as client:
        result = await _call(client, "find_customer", {"customer_reference": "CUST-1001"})

    assert scenario.approval_denied is True
    assert scenario.persistence_failure is True
    assert _structured(result)["customer"]["id"] == "CUST-1001"
    assert len(state.events) == 1


@pytest.mark.asyncio
async def test_malformed_tool_arguments_return_jsonrpc_invalid_params() -> None:
    app = create_mock_support_workflow_app()
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://mock"
    ) as client:
        response = await _call(client, "find_customer", {})

    assert response.json()["error"]["code"] == -32602
    assert "customer_reference" in response.json()["error"]["message"]
