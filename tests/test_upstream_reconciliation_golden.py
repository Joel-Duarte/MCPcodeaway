"""Golden reconciliation proof through the gateway and idempotent mock API."""

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
from gateway.server import create_gateway_app
from gateway.state import SQLiteAuditStore

_GOLDEN = json.loads((Path(__file__).parent / "golden" / "flows.json").read_text(encoding="utf-8"))[
    "reconciliation_flows"
]


async def _call_tool(
    client: httpx.AsyncClient,
    token: str,
    name: str,
    arguments: dict[str, Any],
) -> dict[str, Any]:
    response = await client.post(
        "/mcp?mode=traditional&servers=idempotency_api",
        headers={
            "Authorization": f"Bearer {token}",
            **request_headers("tools/call", tool_name=name),
        },
        json={
            "jsonrpc": "2.0",
            "id": 1,
            "method": "tools/call",
            "params": {"_meta": request_meta(), "name": name, "arguments": arguments},
        },
    )
    assert response.status_code == httpx.codes.OK, response.text
    return response.json()["result"]["structuredContent"]


@pytest.mark.parametrize("flow", _GOLDEN, ids=lambda flow: flow["id"])
async def test_reconciliation_golden_scenarios_are_declared(flow: dict[str, Any]) -> None:
    assert flow["id"]
    assert flow["kind"] in {
        "lost_response_status_readback",
        "idempotent_replay",
        "idempotency_conflict",
    }


async def test_gateway_reconciliation_uses_api_key_idempotency_and_status_readback(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("RECONCILIATION_MOCK_API_KEY", "local-test-secret")
    monkeypatch.setenv("MOCK_IDEMPOTENCY_API_KEY", "local-test-secret")
    mock_app = create_mock_backends_app()
    upstream_config = {
        "upstreams": [
            {
                "name": "idempotency_api",
                "type": "openapi",
                "url": "http://mock-backends",
                "auth_type": "api_key",
                "secret_env_var": "RECONCILIATION_MOCK_API_KEY",
                "api_key_header": "X-API-Key",
                "openapi_spec": mock_app.openapi(),
                "allowed_roles": ["finance_analyst", "admin"],
                "allowed_operations": [
                    "create_idempotent_operation",
                    "get_idempotent_operation_status",
                    "get_idempotent_operation_metrics",
                ],
                "tool_effects": {
                    "create_idempotent_operation": "write",
                    "get_idempotent_operation_status": "read",
                    "get_idempotent_operation_metrics": "read",
                },
                "mutating_tools": ["create_idempotent_operation"],
                "max_requests_per_minute": 5,
                "max_concurrent_calls": 10,
            }
        ]
    }
    config_path = tmp_path / "upstreams.yaml"
    config_path.write_text(json.dumps(upstream_config), encoding="utf-8")
    settings = GatewaySettings(
        app_env="sandbox",
        auth_mode="dev",
        profile="demo",
        upstreams_config_path=str(config_path),
        sqlite_path=str(tmp_path / "gateway.db"),
        provenance_signing_secret="reconciliation-golden-secret",
    )
    keys = LocalDevKeyManager(issuer=settings.oidc_issuer)
    backend_client = httpx.AsyncClient(transport=httpx.ASGITransport(app=mock_app))
    app = create_gateway_app(
        settings=settings,
        store=SQLiteAuditStore(settings.sqlite_path),
        dev_keys=keys,
        http_client=backend_client,
    )
    token = keys.issue_user_token(
        persona="gateway_admin",
        overrides={"roles": ["admin"], "scopes": ["mcp.tools", "mcp.write"]},
    )
    try:
        async with app.router.lifespan_context(app):
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app), base_url="http://gateway"
            ) as gateway:
                names_response = await gateway.post(
                    "/mcp?mode=traditional&servers=idempotency_api",
                    headers={"Authorization": f"Bearer {token}", **request_headers("tools/list")},
                    json={
                        "jsonrpc": "2.0",
                        "id": 1,
                        "method": "tools/list",
                        "params": {"_meta": request_meta()},
                    },
                )
                assert names_response.status_code == httpx.codes.OK, names_response.text
                names = {item["name"] for item in names_response.json()["result"]["tools"]}
                assert "idempotency_api__create_idempotent_operation" in names
                assert "idempotency_api__get_idempotent_operation_status" in names

                created = await _call_tool(
                    gateway,
                    token,
                    "idempotency_api__create_idempotent_operation",
                    {
                        "idempotency_key": "reconcile-2026-001",
                        "item_id": "invoice-42",
                        "simulate_lost_response": True,
                    },
                )
                assert created["ok"] is False
                assert created["error_type"] == "upstream_unavailable"
                assert created["outcome_unknown"] is True
                assert created["retry_attempts"] == 0

                status = await _call_tool(
                    gateway,
                    token,
                    "idempotency_api__get_idempotent_operation_status",
                    {"idempotency_key": "reconcile-2026-001"},
                )
                assert status["ok"] is True
                assert status["result"]["status"] == _GOLDEN[0]["expected_status"]

                replay = await _call_tool(
                    gateway,
                    token,
                    "idempotency_api__create_idempotent_operation",
                    {
                        "idempotency_key": "reconcile-2026-001",
                        "item_id": "invoice-42",
                    },
                )
                assert replay["ok"] is True
                assert replay["result"]["replayed"] is True
                assert replay["result"]["operation_id"] == status["result"]["operation_id"]

                conflict = await _call_tool(
                    gateway,
                    token,
                    "idempotency_api__create_idempotent_operation",
                    {
                        "idempotency_key": "reconcile-2026-001",
                        "item_id": "different-invoice",
                    },
                )
                assert conflict["ok"] is False
                assert conflict["upstream_status_code"] == _GOLDEN[2]["expected_http_status"]

                metrics = await _call_tool(
                    gateway,
                    token,
                    "idempotency_api__get_idempotent_operation_metrics",
                    {},
                )
                assert metrics["ok"] is True
                assert (
                    metrics["result"]["side_effect_count"]
                    == _GOLDEN[0]["expected_side_effect_count"]
                )

                limited = await _call_tool(
                    gateway,
                    token,
                    "idempotency_api__get_idempotent_operation_metrics",
                    {},
                )
                assert limited["ok"] is False
                assert limited["error_type"] == "upstream_capacity_exceeded"
                assert limited["retry_attempts"] == 0
    finally:
        await backend_client.aclose()


async def test_mock_idempotency_api_requires_its_api_key() -> None:
    app = create_mock_backends_app()
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://mock-backends"
    ) as client:
        response = await client.post(
            "/idempotency/operations",
            json={"idempotency_key": "missing-key", "item_id": "invoice-1"},
        )
    assert response.status_code == httpx.codes.UNAUTHORIZED
