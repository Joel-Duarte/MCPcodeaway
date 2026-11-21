"""Golden end-to-end recovery journeys through the MCP gateway."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import httpx
import pytest

from examples.mock_backends import create_mock_backends_app
from gateway.auth import LocalDevKeyManager
from gateway.config import GatewaySettings
from gateway.mcp_protocol import request_headers, request_meta
from gateway.monitoring import UpstreamHealthMonitor
from gateway.server import create_gateway_app
from gateway.state import SQLiteAuditStore
from gateway.upstream_clients import DiscoveredTool

_GOLDEN_PATH = Path(__file__).parent / "golden" / "flows.json"
_RECOVERY_FLOWS = json.loads(_GOLDEN_PATH.read_text(encoding="utf-8"))["recovery_flows"]


def _fixture_tools() -> list[DiscoveredTool]:
    return [
        DiscoveredTool(
            server="recovery_fixture",
            name="read_state",
            qualified_name="recovery_fixture__read_state",
            description="Read the durable test state.",
            input_schema={"type": "object", "properties": {}},
            output_schema={"type": "object"},
        ),
        DiscoveredTool(
            server="recovery_fixture",
            name="write_state",
            qualified_name="recovery_fixture__write_state",
            description="Write one item into durable test state.",
            input_schema={
                "type": "object",
                "properties": {"item_id": {"type": "string"}},
                "required": ["item_id"],
            },
            output_schema={"type": "object"},
            mutating=True,
        ),
    ]


async def _call_tool(
    client: httpx.AsyncClient,
    token: str,
    tool_name: str,
    arguments: dict[str, Any] | None = None,
) -> dict[str, Any]:
    response = await client.post(
        "/mcp?mode=traditional",
        headers={
            "Authorization": f"Bearer {token}",
            **request_headers("tools/call", tool_name=tool_name),
        },
        json={
            "jsonrpc": "2.0",
            "id": 1,
            "method": "tools/call",
            "params": {
                "name": tool_name,
                "arguments": arguments or {},
                "_meta": request_meta(client_name="recovery-golden"),
            },
        },
    )
    assert response.status_code == httpx.codes.OK, response.text
    return response.json()["result"]["structuredContent"]


@pytest.mark.parametrize("flow", _RECOVERY_FLOWS, ids=lambda flow: flow["id"])
async def test_golden_upstream_recovery_flow(flow: dict[str, Any], tmp_path: Path) -> None:
    state: dict[str, Any] = {
        "healthy": True,
        "read_calls": 0,
        "write_calls": 0,
        "writes": [],
    }

    def response(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/health":
            return httpx.Response(
                200 if state["healthy"] else 503,
                json={"healthy": state["healthy"]},
                request=request,
            )

        if request.url.path != "/mcp":
            return httpx.Response(404, request=request)

        payload = json.loads(request.content)
        method = payload["method"]
        if method == "server/discover":
            result: dict[str, Any] = {"supportedVersions": ["2026-07-28"]}
        elif method == "tools/list":
            result = {"tools": [tool.model_dump() for tool in _fixture_tools()]}
        elif method == "tools/call":
            params = payload["params"]
            name = params["name"]
            arguments = params.get("arguments", {})
            if name == "read_state":
                state["read_calls"] += 1
                fail_count = flow.get("failures_before_success", 0)
                should_fail = (
                    flow["kind"] == "read_retry_exhausted"
                    or state["read_calls"] <= int(fail_count or 0)
                    or not state["healthy"]
                )
                if should_fail:
                    return httpx.Response(
                        503, json={"error": "fixture unavailable"}, request=request
                    )
                tool_result = {"writes": list(state["writes"])}
            elif name == "write_state":
                state["write_calls"] += 1
                state["writes"].append(str(arguments["item_id"]))
                if flow["kind"] == "write_outcome_unknown":
                    return httpx.Response(
                        504,
                        json={"error": "response lost after commit"},
                        request=request,
                    )
                tool_result = {"written": True}
            else:
                return httpx.Response(404, request=request)

            return httpx.Response(
                200,
                json={
                    "jsonrpc": "2.0",
                    "id": payload["id"],
                    "result": {
                        "content": [{"type": "text", "text": json.dumps(tool_result)}],
                        "structuredContent": tool_result,
                        "isError": False,
                    },
                },
                request=request,
            )

        return httpx.Response(
            200,
            json={"jsonrpc": "2.0", "id": payload["id"], "result": result},
            request=request,
        )

    backend_client = httpx.AsyncClient(transport=httpx.MockTransport(response))
    config: dict[str, Any] = {
        "upstreams": [
            {
                "name": "recovery_fixture",
                "type": "mcp_http",
                "url": "http://recovery-fixture/mcp",
                "auth_type": "none",
                "allowed_roles": ["finance_analyst", "admin"],
                "allowed_operations": ["read_state", "write_state"],
                "tool_effects": {"read_state": "read", "write_state": "write"},
                "mutating_tools": ["write_state"],
                "health_check": {
                    "enabled": flow["kind"] == "circuit_recovery",
                    "url": "http://recovery-fixture/health",
                    "interval_seconds": 10,
                    "timeout_seconds": 0.5,
                    "failure_threshold": flow.get("failure_threshold", 2),
                    "success_threshold": flow.get("success_threshold", 2),
                },
            }
        ]
    }
    config_path = tmp_path / f"{flow['id']}.yaml"
    config_path.write_text(json.dumps(config), encoding="utf-8")
    settings = GatewaySettings(
        app_env="sandbox",
        auth_mode="dev",
        profile="demo",
        upstreams_config_path=str(config_path),
        sqlite_path=str(tmp_path / f"{flow['id']}.db"),
        provenance_signing_secret="recovery-golden-secret",
    )
    dev_keys = LocalDevKeyManager(issuer=settings.oidc_issuer)
    app = create_gateway_app(
        settings=settings,
        store=SQLiteAuditStore(settings.sqlite_path),
        dev_keys=dev_keys,
        http_client=backend_client,
        preloaded_tools={"recovery_fixture": _fixture_tools()},
    )
    token = dev_keys.issue_user_token(persona="finance_analyst")
    write_token = dev_keys.issue_user_token(
        persona="gateway_admin",
        overrides={"roles": ["admin"], "scopes": ["mcp.tools", "mcp.write"]},
    )

    try:
        async with app.router.lifespan_context(app):
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app), base_url="http://gateway"
            ) as gateway:
                if flow["kind"] in {"read_retry_success", "read_retry_exhausted"}:
                    result = await _call_tool(gateway, token, "recovery_fixture__read_state")
                    assert state["read_calls"] == flow["expected_calls"]
                    if flow["kind"] == "read_retry_success":
                        assert result["ok"] is True
                        assert result["retry_attempts"] == 1
                    else:
                        assert result["ok"] is False
                        assert result["error_type"] == flow["expected_error_type"]
                        assert result["retry_attempts"] == flow["expected_retry_attempts"]
                        assert result["outcome_unknown"] is False

                elif flow["kind"] == "write_outcome_unknown":
                    result = await _call_tool(
                        gateway,
                        write_token,
                        "recovery_fixture__write_state",
                        {"item_id": flow["id"]},
                    )
                    assert result["ok"] is False
                    assert result["error_type"] == flow["expected_error_type"]
                    assert result["retry_attempts"] == flow["expected_retry_attempts"]
                    assert result["outcome_unknown"] is flow["expected_outcome_unknown"]
                    assert "response lost after commit" not in str(result["error"])
                    assert state["write_calls"] == flow["expected_write_count"]

                    readback = await _call_tool(gateway, token, "recovery_fixture__read_state")
                    assert readback["ok"] is True
                    assert readback["result"]["writes"] == [flow["id"]]
                    assert state["write_calls"] == flow["expected_write_count"]

                elif flow["kind"] == "circuit_recovery":
                    runtime = app.state.runtime
                    upstream = runtime.registry.get("recovery_fixture")
                    assert upstream is not None
                    peer_monitor = UpstreamHealthMonitor(runtime.client_manager)
                    state["healthy"] = False
                    await runtime.upstream_health.check(upstream)
                    failed_status = await runtime.upstream_health.check(upstream)
                    assert failed_status["status"] == "unhealthy"
                    await peer_monitor.check(upstream)
                    peer_failed_status = await peer_monitor.check(upstream)
                    assert peer_failed_status["status"] == "unhealthy"

                    calls_before_block = state["read_calls"]
                    blocked = await _call_tool(gateway, token, "recovery_fixture__read_state")
                    assert blocked["ok"] is False
                    assert blocked["error_type"] == flow["expected_error_type"]
                    assert state["read_calls"] == calls_before_block

                    state["healthy"] = True
                    await runtime.upstream_health.check(upstream)
                    recovered_status = await runtime.upstream_health.check(upstream)
                    assert recovered_status["status"] == "healthy"
                    assert peer_monitor.is_circuit_open(upstream.name)
                    await peer_monitor.check(upstream)
                    peer_recovered_status = await peer_monitor.check(upstream)
                    assert peer_recovered_status["status"] == "healthy"
                    recovered = await _call_tool(gateway, token, "recovery_fixture__read_state")
                    assert recovered["ok"] is True
                    assert state["read_calls"] == calls_before_block + 1
                else:
                    raise AssertionError(f"Unsupported recovery flow: {flow['kind']}")
    finally:
        await backend_client.aclose()


async def test_readiness_fault_control_is_disabled_by_default(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("MOCK_ENABLE_READINESS_FAILURE_CONTROLS", raising=False)
    app = create_mock_backends_app()
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://mock-backends"
    ) as client:
        response = await client.post("/readiness/test/upstream-health", json={"available": False})
    assert response.status_code == httpx.codes.NOT_FOUND
