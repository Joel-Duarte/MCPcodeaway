"""Golden mock coverage for generated write traffic and MCP error accounting."""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest
from fastapi import FastAPI
from fastapi.responses import JSONResponse

from examples.mock_backends import create_mock_backends_app
from gateway.auth import LocalDevKeyManager
from gateway.config import GatewaySettings
from gateway.demo_traffic import DemoTrafficRunner
from gateway.mcp_protocol import request_headers, request_meta
from gateway.server import create_gateway_app
from gateway.state import SQLiteAuditStore

_GOLDEN_DEMO_FLOWS = json.loads(
    (Path(__file__).parent / "golden" / "flows.json").read_text(encoding="utf-8")
)["demo_traffic_flows"]


def _call_payload(tool: str, arguments: dict[str, object]) -> dict[str, object]:
    return {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "tools/call",
        "params": {"_meta": request_meta(), "name": tool, "arguments": arguments},
    }


@pytest.mark.asyncio
async def test_recovery_workbench_supports_delays_idempotency_errors_and_status() -> None:
    app = create_mock_backends_app()
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://mock"
    ) as client:
        tool = "submit_change"
        headers = request_headers("tools/call", tool_name=tool)
        args = {
            "idempotency_key": "demo-key-1",
            "resource_id": "resource-1",
            "change": "set active",
            "delay_ms": 10,
        }
        first = await client.post("/recovery/mcp", headers=headers, json=_call_payload(tool, args))
        assert first.status_code == 200
        assert first.json()["result"]["structuredContent"]["replayed"] is False

        replay = await client.post("/recovery/mcp", headers=headers, json=_call_payload(tool, args))
        assert replay.json()["result"]["structuredContent"]["replayed"] is True

        conflict = await client.post(
            "/recovery/mcp",
            headers=headers,
            json=_call_payload(tool, {**args, "resource_id": "resource-2"}),
        )
        assert conflict.status_code == 200
        assert conflict.json()["error"]["code"] == -32602

        rejected = await client.post(
            "/recovery/mcp",
            headers=headers,
            json=_call_payload(
                tool,
                {**args, "idempotency_key": "demo-reject", "simulate_failure": True},
            ),
        )
        assert rejected.json()["error"]["message"] == "Simulated upstream rejection before commit"

        lost_key = "demo-lost-response"
        lost = await client.post(
            "/recovery/mcp",
            headers=headers,
            json=_call_payload(
                tool,
                {
                    **args,
                    "idempotency_key": lost_key,
                    "simulate_lost_response": True,
                },
            ),
        )
        assert lost.status_code == 504
        status_headers = request_headers("tools/call", tool_name="get_change_status")
        status = await client.post(
            "/recovery/mcp",
            headers=status_headers,
            json=_call_payload("get_change_status", {"idempotency_key": lost_key}),
        )
        assert status.json()["result"]["structuredContent"]["status"] == "committed"


@pytest.mark.asyncio
async def test_demo_runner_counts_jsonrpc_errors_even_when_http_is_200() -> None:
    app = FastAPI()

    @app.post("/mcp")
    async def return_rpc_error() -> JSONResponse:
        return JSONResponse(
            {"jsonrpc": "2.0", "id": 1, "error": {"code": -32602, "message": "Rejected"}}
        )

    keys = LocalDevKeyManager()
    runtime = SimpleNamespace(
        settings=SimpleNamespace(app_env="sandbox", effective_profile_name="demo"),
        dev_keys=keys,
    )
    runner = DemoTrafficRunner(runtime, app)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://gateway"
    ) as client:
        result = await runner._dispatch_one(
            client,
            "gateway_admin",
            "mode=traditional",
            "demo:rejected_call",
            {"name": "mock__reject", "arguments": {}},
            None,
        )
    assert result is False
    assert runner.requests_sent == 1
    assert runner.errors_count == 1
    assert runner.rpc_errors_count == 1


def test_demo_workflow_evidence_contains_matched_pairs_for_multiple_workloads() -> None:
    runtime = SimpleNamespace(
        settings=SimpleNamespace(app_env="sandbox", effective_profile_name="demo"),
        dev_keys=LocalDevKeyManager(),
    )
    rows = DemoTrafficRunner(runtime).simulated_workflow_evidence()
    assert len(rows) == 4 * 9 * 5 * 2
    assert {row["workload"] for row in rows} == {
        "single_read",
        "small_fanout",
        "large_fanout",
        "data_reconciliation",
        "large_data_reconciliation",
        "batch_compute",
        "sequential_workflow",
        "mutating_workflow",
        "large_payload_processing",
    }
    assert {row["discovery_config"]["model_source"] for row in rows} == {"demo-simulator"}
    assert {row["mode"] for row in rows} == {"traditional", "codemode"}
    assert len({(row["model"], row["input_tokens"]) for row in rows}) > 20
    assert {row["model"] for row in rows} == {
        "claude-sonnet-5.5",
        "gpt-6.1-sol",
        "gemini-4-hydrogen",
        "qwen3.5-9b",
    }
    assert all(row["input_tokens"] > 0 and row["output_tokens"] > 0 for row in rows)


def test_demo_traffic_cases_match_additive_golden_catalog() -> None:
    runtime = SimpleNamespace(
        settings=SimpleNamespace(app_env="sandbox", effective_profile_name="demo"),
        dev_keys=LocalDevKeyManager(),
        registry=SimpleNamespace(
            list_upstreams=lambda: [
                SimpleNamespace(
                    name="recovery_workbench",
                    enabled=True,
                    url="http://mock-backends:8001/recovery/mcp",
                )
            ]
        ),
        catalog=SimpleNamespace(get_server_tools=lambda _server: []),
    )
    labels = {item[2] for item in DemoTrafficRunner(runtime)._next_workloads()}
    golden_labels = {
        "recovery:delayed_write",
        "recovery:idempotent_replay",
        "recovery:idempotency_conflict",
        "recovery:upstream_rejection",
        "recovery:lost_response_then_status_read",
        "recovery:status_after_lost_response",
        "recovery:upstream_stays_pending",
        "recovery:rbac_write_denied",
    }
    assert golden_labels <= labels
    assert {flow["kind"] for flow in _GOLDEN_DEMO_FLOWS} == {
        "mock_delayed_write",
        "mock_idempotent_replay",
        "mock_idempotency_conflict",
        "mock_upstream_rejection",
        "mock_lost_response_status_readback",
        "mock_upstream_pending_reconciliation",
        "gateway_rbac_denial",
    }


@pytest.mark.asyncio
async def test_gateway_demo_burst_exercises_write_and_failure_spectrum(tmp_path: Path) -> None:
    mock_app = create_mock_backends_app()
    upstream_config = {
        "upstreams": [
            {
                "name": "recovery_workbench",
                "demo_only": True,
                "type": "mcp_http",
                "url": "http://mock-backends/recovery/mcp",
                "auth_type": "none",
                "allowed_roles": ["gateway_admin"],
                "allowed_groups": ["platform-admins"],
                "allowed_scopes": ["mcp.tools", "mcp.write"],
                "allowed_operations": ["submit_change", "get_change_status"],
                "tool_effects": {"submit_change": "write", "get_change_status": "read"},
                "mutating_tools": ["submit_change"],
                "tool_recovery_policies": {
                    "submit_change": {
                        "enabled": True,
                        "idempotency_key_argument": "idempotency_key",
                        "synchronous_budget_seconds": 0.4,
                        "max_attempts": 3,
                        "max_recovery_age_seconds": 30,
                        "upstream_key_retention_seconds": 60,
                        "status_tool": "get_change_status",
                        "status_key_argument": "idempotency_key",
                    }
                },
                "request_timeout_seconds": 5,
            }
        ]
    }
    config_path = tmp_path / "upstreams.json"
    config_path.write_text(json.dumps(upstream_config), encoding="utf-8")
    settings = GatewaySettings(
        auth_mode="dev",
        profile="demo",
        upstreams_config_path=str(config_path),
        sqlite_path=str(tmp_path / "gateway.db"),
        allowed_tenants=["tenant-acme-dev"],
    )
    keys = LocalDevKeyManager(issuer=settings.oidc_issuer)
    backend_client = httpx.AsyncClient(
        transport=httpx.ASGITransport(app=mock_app), base_url="http://mock-backends"
    )
    app = create_gateway_app(
        settings=settings,
        dev_keys=keys,
        store=SQLiteAuditStore(settings.sqlite_path),
        http_client=backend_client,
    )
    async with app.router.lifespan_context(app):
        token = keys.issue_user_token(persona="gateway_admin")
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://gateway"
        ) as client:
            response = await client.post(
                "/admin/demo/golden-run",
                headers={"Authorization": f"Bearer {token}"},
            )
        assert response.status_code == 200
        golden = response.json()
        status = app.state.runtime.demo_traffic.status()
        assert golden["traffic_requests"] == status["requests_sent"]
        assert golden["traffic_requests"] == golden["traffic_workload_cases"]
        assert golden["traffic_ok"] >= 3
        assert golden["discovery_requests"] == 2
        assert golden["recovery_operations_created"] >= 4
        assert golden["recovery_operations_pending"] >= 1
        assert status["discovery_requests_count"] == 2
        assert status["golden_runs_count"] == 1
        assert status["task_polls_count"] >= 1
        assert status["write_calls_count"] >= 5
        assert status["rpc_errors_count"] >= 4
        assert status["errors_count"] >= 4
        operations = await app.state.runtime.store.list_operations(
            "tenant-acme-dev", statuses=None, limit=100
        )
        assert len(operations) >= 4
        assert {row["status"] for row in operations} & {"pending", "working"}
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://gateway"
        ) as metrics_client:
            metrics = await metrics_client.get(
                "/admin/operations/metrics", headers={"Authorization": f"Bearer {token}"}
            )
        assert metrics.status_code == 200
        assert metrics.json()["open"] >= 1
        assert any(
            row["upstream_name"] == "recovery_workbench"
            for row in metrics.json()["by_upstream_24h"]
        )
        key_file = tmp_path / ".mcpcodeaway-demo-recovery-keys.json"
        assert key_file.exists()
        assert key_file.stat().st_mode & 0o077 == 0
