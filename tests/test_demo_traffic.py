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
_GOLDEN_BUSINESS_VALUE_WORKFLOW = next(
    flow for flow in _GOLDEN_DEMO_FLOWS if flow["kind"] == "business_value_attributed_workflow"
)


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
    assert {
        "mock_delayed_write",
        "mock_idempotent_replay",
        "mock_idempotency_conflict",
        "mock_upstream_rejection",
        "mock_lost_response_status_readback",
        "mock_upstream_pending_reconciliation",
        "gateway_rbac_denial",
        "business_value_attributed_workflow",
        "passive_workflow_and_ui",
    } <= {flow["kind"] for flow in _GOLDEN_DEMO_FLOWS}


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
            },
            {
                "name": "hr_directory",
                "type": "openapi",
                "url": "http://mock-backends/hr",
                "openapi_spec_url": "http://mock-backends/hr/openapi.json",
                "auth_type": "none",
                "allowed_roles": ["hr_ops", "hr_writer", "gateway_admin", "admin"],
                "allowed_groups": ["hr", "platform-admins"],
                "allowed_operations": [
                    "list_hr_employees",
                    "get_hr_employee",
                    "update_hr_employee",
                ],
                "tool_effects": {
                    "list_hr_employees": "read",
                    "get_hr_employee": "read",
                    "update_hr_employee": "write",
                },
                "tool_approval_policies": {
                    "list_hr_employees": {"mode": "none", "version": 1},
                    "get_hr_employee": {"mode": "none", "version": 1},
                    "update_hr_employee": {
                        "mode": "requester_confirmation",
                        "version": 1,
                        "confirmation_message": "Confirm the demo profile update.",
                    },
                },
                "mutating_tools": ["update_hr_employee"],
            },
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
        assert golden["traffic_requests"] == golden["traffic_workload_cases"]
        assert golden["business_value_workflow"]["status"] == "completed"
        assert (
            golden["business_value_workflow"]["workflow_id"]
            == _GOLDEN_BUSINESS_VALUE_WORKFLOW["workflow_id"]
        )
        assert (
            golden["business_value_workflow"]["completion_count"]
            == _GOLDEN_BUSINESS_VALUE_WORKFLOW["expected_completions"]
        )
        assert (
            golden["business_value_workflow"]["manual_baseline_minutes"]
            == _GOLDEN_BUSINESS_VALUE_WORKFLOW["expected_manual_baseline_minutes"]
        )
        assert golden["business_value_workflow"]["requests"] == 5
        assert golden["business_value_workflow"]["attribution_mode"] == "passive_matched"
        assert golden["business_value_workflow"]["mcp_app_ui_verified"] is True
        assert (
            golden["traffic_requests"] + golden["business_value_workflow"]["requests"]
            == status["requests_sent"]
        )
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
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://gateway"
        ) as report_client:
            report = await report_client.get(
                "/admin/business-value/report", headers={"Authorization": f"Bearer {token}"}
            )
        assert report.status_code == 200
        report_workflows = {
            row["selected_id"]: row for row in report.json()["by_integration_workflow_day"]
        }
        report_key = _GOLDEN_BUSINESS_VALUE_WORKFLOW["workflow_id"]
        assert report_workflows[report_key]["completed_count"] == 1
        assert report_workflows[report_key]["estimated_manual_baseline_minutes"] == 8
        key_file = tmp_path / ".mcpcodeaway-demo-recovery-keys.json"
        assert key_file.exists()
        assert key_file.stat().st_mode & 0o077 == 0


@pytest.mark.asyncio
async def test_ensure_demo_solutions_seeds_all_upstreams_with_zero_runs(tmp_path: Path) -> None:
    keys = LocalDevKeyManager(issuer="http://127.0.0.1:8000/dev/oidc")
    mock_app = create_mock_backends_app(
        public_key_pem=keys.public_pem,
        expected_issuer=keys.issuer,
    )
    upstream_config = {
        "upstreams": [
            {
                "name": "global_ticketing",
                "type": "openapi",
                "url": "http://mock-backends/ticketing",
                "openapi_spec_url": "http://mock-backends/ticketing/openapi.json",
                "auth_type": "delegated_token",
                "downstream_audience": "api://ticketing-system",
                "downstream_scope": "mcp.tools ops.write",
                "allowed_roles": [
                    "ops_engineer",
                    "ticketing_reader",
                    "ticketing_writer",
                    "gateway_admin",
                    "admin",
                ],
                "allowed_groups": ["operations", "platform-admins"],
                "allowed_operations": [
                    "list_ticketing_queues",
                    "list_tickets",
                    "create_ticket",
                    "get_ticket",
                    "assign_ticket",
                    "escalate_ticket",
                    "resolve_ticket",
                    "add_ticket_comment",
                    "get_ticket_sla",
                    "get_ticketing_metrics",
                ],
                "mutating_tools": [
                    "create_ticket",
                    "assign_ticket",
                    "escalate_ticket",
                    "resolve_ticket",
                    "add_ticket_comment",
                ],
            },
            {
                "name": "hr_directory",
                "type": "openapi",
                "url": "http://mock-backends/hr",
                "openapi_spec_url": "http://mock-backends/hr/openapi.json",
                "auth_type": "none",
                "allowed_roles": ["hr_ops", "hr_writer", "gateway_admin", "admin"],
                "allowed_groups": ["hr", "platform-admins"],
                "allowed_operations": [
                    "list_hr_employees",
                    "get_hr_employee",
                    "update_hr_employee",
                ],
                "tool_approval_policies": {
                    "update_hr_employee": {
                        "mode": "requester_confirmation",
                        "version": 1,
                        "confirmation_message": "Confirm HR update.",
                    }
                },
                "mutating_tools": ["update_hr_employee"],
            },
            {
                "name": "crm_accounts",
                "type": "openapi",
                "url": "http://mock-backends/crm",
                "openapi_spec_url": "http://mock-backends/crm/openapi.json",
                "auth_type": "none",
                "allowed_roles": ["sales_ops", "finance_analyst", "gateway_admin", "admin"],
                "allowed_groups": ["sales", "finance", "platform"],
                "allowed_operations": [
                    "list_crm_accounts",
                    "list_crm_accounts_summary",
                    "list_crm_accounts_metrics",
                ],
            },
            {
                "name": "devops_ci",
                "type": "openapi",
                "url": "http://mock-backends/devops",
                "openapi_spec_url": "http://mock-backends/devops/openapi.json",
                "auth_type": "none",
                "allowed_roles": ["platform_eng", "gateway_admin", "admin"],
                "allowed_groups": ["engineering", "platform"],
                "allowed_operations": [
                    "list_ci_deployments",
                    "list_ci_deployments_health",
                    "list_ci_deployments_metrics",
                ],
            },
        ]
    }
    config_path = tmp_path / "upstreams.json"
    config_path.write_text(json.dumps(upstream_config), encoding="utf-8")
    settings = GatewaySettings(
        auth_mode="dev",
        profile="demo",
        upstreams_config_path=str(config_path),
        sqlite_path=str(tmp_path / "solutions.db"),
        allowed_tenants=["tenant-acme-dev"],
    )
    backend_client = httpx.AsyncClient(
        transport=httpx.ASGITransport(app=mock_app), base_url="http://mock-backends"
    )
    app = create_gateway_app(
        settings=settings,
        dev_keys=keys,
        store=SQLiteAuditStore(settings.sqlite_path),
        http_client=backend_client,
    )
    try:
        async with app.router.lifespan_context(app):
            rt = app.state.runtime
            runs_before = await rt.store.list_runs(limit=10)
            assert runs_before == []

            published = await rt.store.list_current_published_integration_definitions(
                "tenant-acme-dev"
            )
            by_upstream = {row["upstream_name"]: row["definition"] for row in published}
            assert set(by_upstream) == {
                "global_ticketing",
                "crm_accounts",
                "devops_ci",
            }
            crm_exps = {exp["id"] for exp in by_upstream["crm_accounts"]["experiences"]}
            devops_exps = {exp["id"] for exp in by_upstream["devops_ci"]["experiences"]}
            crm_wfs = {wf["id"] for wf in by_upstream["crm_accounts"]["workflows"]}
            devops_wfs = {wf["id"] for wf in by_upstream["devops_ci"]["workflows"]}
            assert "crm_account_summary_ui" in crm_exps
            assert "devops_build_status_ui" in devops_exps
            assert "account_risk_review" in crm_wfs
            assert "pipeline_incident_triage" in devops_wfs

            golden = await rt.demo_traffic.golden_run()
            assert golden["traffic"]["sent"] > 0
            assert golden["ticketing_workflow"]["status"] == "completed"
            assert golden["business_value_workflow"]["status"] == "completed"
            published_after = await rt.store.list_current_published_integration_definitions(
                "tenant-acme-dev"
            )
            hr_def = next(
                row["definition"]
                for row in published_after
                if row["upstream_name"] == "hr_directory"
            )
            hr_wfs = {wf["id"] for wf in hr_def["workflows"]}
            hr_exps = {exp["id"] for exp in hr_def["experiences"]}
            assert {
                "demo_fictional_employee_profile_update",
                "employee_role_audit_and_update",
            } <= hr_wfs
            assert {"hr_employee_lookup_ui", "hr_employee_inspector_ui"} <= hr_exps
            runs_after = await rt.store.list_runs(limit=10)
            assert len(runs_after) > 0
    finally:
        await backend_client.aclose()
