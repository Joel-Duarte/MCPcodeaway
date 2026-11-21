from datetime import UTC, datetime, timedelta

import httpx
import pytest

from benchmarks.live_llm_bench import export_adaptive_evidence
from gateway.adaptive_execution import summarize_pairs
from gateway.auth import LocalDevKeyManager
from gateway.config import GatewaySettings
from gateway.server import create_gateway_app
from gateway.state import SQLiteAuditStore


def _pair(repeats: int = 5, *, cheap_wrong: bool = False) -> list[dict]:
    rows = []
    for i in range(repeats):
        for mode in ("traditional", "codemode"):
            correct = not (cheap_wrong and mode == "codemode")
            rows.append(
                {
                    "version": 1,
                    "evidence_id": f"{i}:{mode}",
                    "created_at": datetime.now(UTC).isoformat(),
                    "model": "m",
                    "workload": "single_read",
                    "scenario_id": f"s{i}",
                    "repeat_id": f"r{i}",
                    "mode": mode,
                    "correct": correct,
                    "grounded": correct,
                    "wall_ms": 100,
                    "upstream_calls": 1,
                    "recovery_count": 0,
                }
            )
    return rows


def test_quality_floor_beats_cheap_incorrect_arm() -> None:
    row = summarize_pairs(_pair(cheap_wrong=True))[0]
    assert row["recommended_mode"] == "traditional"


def test_sparse_and_stale_evidence_stays_reportable_but_not_recommended() -> None:
    sparse = summarize_pairs(_pair(3))[0]
    assert sparse["pairs"] == 3
    assert sparse["recommended_mode"] is None
    stale = _pair()
    for item in stale:
        item["created_at"] = (datetime.now(UTC) - timedelta(days=31)).isoformat()
    assert summarize_pairs(stale) == []


def test_reconciliation_profiles_can_have_different_winners() -> None:
    evidence = _pair()
    for row in evidence:
        row["input_tokens"] = 200 if row["mode"] == "traditional" else 500
        row["output_tokens"] = 0
    large = _pair()
    for row in large:
        row["workload"] = "large_data_reconciliation"
        row["evidence_id"] = f"large:{row['evidence_id']}"
        if row["mode"] == "traditional":
            row.update(correct=False, grounded=False, input_tokens=500, output_tokens=0)
        else:
            row.update(correct=True, grounded=True, input_tokens=100, output_tokens=0)
    winners = {
        row["workload"]: row["recommended_mode"]
        for row in summarize_pairs(evidence + large, pricing={"m": {"input": 1, "output": 1}})
    }
    assert winners == {"single_read": "traditional", "large_data_reconciliation": "codemode"}


def test_missing_prices_use_provider_tokens_and_incomplete_usage_stays_unknown() -> None:
    priced = _pair()
    for row in priced:
        row["input_tokens"] = 100 if row["mode"] == "codemode" else 200
        row["output_tokens"] = 20
    assert summarize_pairs(priced)[0]["recommended_mode"] == "codemode"
    for row in priced:
        row["input_tokens"] = None
    summary = summarize_pairs(priced)[0]
    assert summary["codemode"]["mean_provider_tokens"] is None
    assert summary["recommended_mode"] is None


def test_evidence_export_counts_workflow_once_with_many_subcalls() -> None:
    report = {
        "generated_at": datetime.now(UTC).isoformat(),
        "runs": [
            {
                "model": "m",
                "task": "single_lookup",
                "execution": "codemode",
                "mode": "codemode",
                "discovery_mode": "deferred",
                "discovery_backend": "bm25",
                "extra_synthetic_tools": 50,
                "correct": True,
                "provenance_grounded": True,
                "prompt_tokens": 100,
                "completion_tokens": 20,
                "duration_ms": 300,
                "subcalls_total": 3,
                "turns": 2,
                "self_corrections": 0,
                "run_ids": ["r1"],
            }
        ],
    }
    rows = export_adaptive_evidence(report)
    assert len(rows) == 1
    assert rows[0]["upstream_calls"] == 3
    assert rows[0]["input_tokens"] == 100


@pytest.mark.asyncio
async def test_adaptive_evidence_import_is_tenant_scoped_and_idempotent() -> None:
    store = SQLiteAuditStore(":memory:")
    await store.init()
    rows = _pair()
    assert await store.import_adaptive_evidence("a", rows) == len(rows)
    assert await store.import_adaptive_evidence("a", rows) == 0
    assert len(await store.list_adaptive_evidence("a")) == len(rows)
    assert await store.list_adaptive_evidence("b") == []
    await store.close()


@pytest.mark.asyncio
async def test_adaptive_admin_api_viewer_and_tenant_boundaries(tmp_path) -> None:
    keys = LocalDevKeyManager()
    settings = GatewaySettings(
        auth_mode="dev",
        profile="demo",
        sqlite_path=str(tmp_path / "adaptive-api.db"),
        allowed_tenants=["tenant-acme-dev", "tenant-other"],
    )
    app = create_gateway_app(settings=settings, dev_keys=keys, preloaded_tools={})
    admin = keys.issue_user_token(persona="gateway_admin", audience=settings.oidc_audience)
    viewer = keys.issue_user_token(
        audience=settings.oidc_audience,
        overrides={"roles": ["operator_viewer"], "tenant_id": "tenant-acme-dev"},
    )
    other_tenant_admin = keys.issue_user_token(
        persona="gateway_admin",
        audience=settings.oidc_audience,
        overrides={"sub": "other-admin", "tenant_id": "tenant-other"},
    )
    evidence = _pair()
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://gateway"
        ) as client:
            report = await client.get(
                "/admin/adaptive-execution/report",
                headers={"Authorization": f"Bearer {viewer}"},
            )
            assert report.status_code == 200
            denied = await client.post(
                "/admin/adaptive-execution/evidence",
                headers={"Authorization": f"Bearer {viewer}"},
                json={"evidence": evidence},
            )
            assert denied.status_code == 403
            future_evidence = _pair(1)
            future_time = (datetime.now(UTC) + timedelta(minutes=6)).isoformat()
            for row in future_evidence:
                row["created_at"] = future_time
            rejected_future = await client.post(
                "/admin/adaptive-execution/evidence",
                headers={"Authorization": f"Bearer {admin}"},
                json={"evidence": future_evidence},
            )
            assert rejected_future.status_code == 400
            imported = await client.post(
                "/admin/adaptive-execution/evidence",
                headers={"Authorization": f"Bearer {admin}"},
                json={"evidence": evidence},
            )
            assert imported.status_code == 200, imported.text
            assert imported.json()["imported"] == len(evidence)
            dedup = await client.post(
                "/admin/adaptive-execution/evidence",
                headers={"Authorization": f"Bearer {admin}"},
                json={"evidence": evidence},
            )
            assert dedup.json()["imported"] == 0
            other = await client.get(
                "/admin/adaptive-execution/report",
                headers={"Authorization": f"Bearer {other_tenant_admin}"},
            )
            assert other.json()["rows"] == []


def test_metrics_require_full_attempt_coverage_and_use_nearest_rank_p95() -> None:
    rows = _pair()
    for row in rows:
        row["input_tokens"] = 100
        row["output_tokens"] = 10
        row["wall_ms"] = 100 if row["mode"] == "traditional" else 1000
        row["upstream_calls"] = 1
        row["recovery_count"] = 0
    rows[-1]["input_tokens"] = None
    summary = summarize_pairs(rows, pricing={"m": {"input": 1.0, "output": 2.0}})[0]
    assert summary["traditional"]["mean_provider_tokens"] == 110
    assert summary["codemode"]["mean_provider_tokens"] is None
    assert summary["codemode"]["mean_cost_usd"] is None
    assert summary["codemode"]["p95_wall_ms"] == 1000
    assert summary["traditional"]["mean_cost_usd"] == pytest.approx(0.00012)


def test_exact_qualified_recommendation_is_not_discarded() -> None:
    rows = _pair()
    for row in rows:
        row["workload"] = "single_read"
        row["model"] = "m"
    for row in rows:
        row["input_tokens"] = 100 if row["mode"] == "codemode" else 200
        row["output_tokens"] = 0
    result = summarize_pairs(rows)
    assert result[0]["pairs"] == 5
    assert result[0]["recommended_mode"] == "codemode"


@pytest.mark.asyncio
async def test_adaptive_route_context_survives_discovery_execution_and_rollback(tmp_path) -> None:
    from gateway.auth import CallerIdentity
    from gateway.server import GatewayRuntime
    from gateway.upstream_clients import DiscoveredTool

    settings = GatewaySettings(
        auth_mode="dev",
        sqlite_path=str(tmp_path / "adaptive-route.db"),
        model_pricing={"m": {"input": 1.0, "output": 1.0}},
    )
    runtime = GatewayRuntime(settings=settings, preloaded_tools={})
    await runtime.store.init()
    identity = CallerIdentity(sub="operator", tenant_id="tenant-acme-dev")
    tool = DiscoveredTool(server="ledger", name="get_balance", qualified_name="ledger__get_balance")
    runtime.catalog.list_accessible_tools = lambda *args, **kwargs: [tool]
    evidence = _pair()
    for row in evidence:
        row["input_tokens"] = 100 if row["mode"] == "codemode" else 200
        row["output_tokens"] = 0
    await runtime.store.import_adaptive_evidence(identity.tenant_id, evidence)

    async def resolve(method: str, *, active: bool, write: bool = False, explicit: bool = False):
        await runtime.store.save_setting(
            f"adaptive_execution:{identity.tenant_id}", {"mode": "active" if active else "observe"}
        )
        chosen_tool = tool.model_copy(update={"mutating": write})
        runtime.catalog.list_accessible_tools = lambda *args, **kwargs: [chosen_tool]
        return await runtime.resolve_dynamic_mode(
            identity,
            requested_exec="classic" if explicit else "auto",
            requested_disc="x_intent",
            requested_mode="traditional_discover",
            allowed_servers=None,
            intent="find account balance",
            request_id=f"{method}-{active}-{write}-{explicit}",
            explicit_override=explicit,
            model_name="m",
            workload_profile="single_read",
        )

    for method in ("tools/list", "search_tools"):
        observed = await resolve(method, active=False)
        assert "adaptive_recommendation:exact:single_read:codemode" in observed[3]
        active = await resolve(method, active=True)
        assert active[0] == "codemode" and active[1] == "x_intent"
        assert active[3] == "adaptive_exact:single_read:codemode"
    execution = await resolve("tools/call:execute_code", active=True)
    assert execution[0] == "codemode" and execution[3] == "adaptive_exact:single_read:codemode"
    rollback = await resolve("tools/list", active=False)
    assert "adaptive_recommendation:exact:single_read:codemode" in rollback[3]
    risky = await resolve("tools/call", active=True, write=True)
    assert "adaptive_" not in str(risky[3])
    explicit = await resolve("tools/call", active=True, explicit=True)
    assert explicit[3] == "explicit:traditional_discover"
    await runtime.store.close()


@pytest.mark.asyncio
async def test_benchmark_export_round_trips_through_admin_import(tmp_path) -> None:
    keys = LocalDevKeyManager()
    settings = GatewaySettings(
        auth_mode="dev",
        profile="demo",
        sqlite_path=str(tmp_path / "adaptive-roundtrip.db"),
        allowed_tenants=["tenant-acme-dev"],
    )
    app = create_gateway_app(settings=settings, dev_keys=keys, preloaded_tools={})
    token = keys.issue_user_token(persona="gateway_admin", audience=settings.oidc_audience)
    report = {
        "generated_at": datetime.now(UTC).isoformat(),
        "provider": "openai_compatible",
        "runs": [
            {
                "model": "m",
                "task": "single_lookup",
                "execution": execution,
                "discovery_mode": "x_intent",
                "discovery_backend": "bm25",
                "reasoning_effort": "low",
                "extra_synthetic_tools": 0,
                "correct": True,
                "provenance_grounded": execution == "codemode",
                "audit_evidence_verified": execution == "classic",
                "prompt_tokens": 100,
                "completion_tokens": 20,
                "duration_ms": 200,
                "subcalls_total": 1,
                "turns": 1,
                "self_corrections": 0,
                "run_ids": [f"run-{execution}"],
            }
            for execution in ("classic", "codemode")
        ],
    }
    evidence = export_adaptive_evidence(report)
    assert len(evidence) == 2
    assert isinstance(evidence[0]["discovery_config"]["model_config"], str)
    assert evidence[0]["scenario_id"] == evidence[1]["scenario_id"]
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://gateway"
        ) as client:
            response = await client.post(
                "/admin/adaptive-execution/evidence",
                headers={"Authorization": f"Bearer {token}"},
                json={"evidence": evidence},
            )
            assert response.status_code == 200, response.text
            assert response.json()["imported"] == 2


def test_pairing_requires_fixture_and_discovery_configuration_match() -> None:
    rows = _pair(1)
    rows[0]["fixture_id"] = "fixture-a"
    rows[1]["fixture_id"] = "fixture-b"
    rows[0]["discovery_config"] = {"backend": "bm25"}
    rows[1]["discovery_config"] = {"backend": "vector"}
    result = summarize_pairs(rows)[0]
    assert result["pairs"] == 0


def test_workload_inference_requires_clear_intent_not_catalog_size() -> None:
    from gateway.adaptive_execution import infer_workload
    from gateway.upstream_clients import DiscoveredTool

    tools = [
        DiscoveredTool(server="s", name=f"tool_{i}", qualified_name=f"s__tool_{i}")
        for i in range(30)
    ]
    assert infer_workload("help me with this", tools) is None
    assert infer_workload("compare datasets", tools) == "data_reconciliation"
    assert infer_workload("compare and update all rows", tools) is None
    assert (
        infer_workload("compare all rows in this large dataset", tools)
        == "large_data_reconciliation"
    )


def test_scorer_ignores_future_dated_records() -> None:
    future = _pair()
    future_at = (datetime.now(UTC) + timedelta(days=2)).isoformat()
    for row in future:
        row["created_at"] = future_at
    assert summarize_pairs(future) == []


@pytest.mark.asyncio
async def test_benchmark_mode_helpers_send_valid_execution_and_discovery_headers() -> None:
    from benchmarks.live_llm_bench import _call_mcp_tool, _list_mcp_tools

    captured: list[httpx.Request] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        captured.append(request)
        method = request.headers["mcp-method"]
        body = (
            {"result": {"tools": []}}
            if method == "tools/list"
            else {"result": {"structuredContent": {}}}
        )
        return httpx.Response(200, json=body)

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler), base_url="http://gateway")
    expected = {
        "traditional": "traditional",
        "traditional_discover": "traditional_discover",
        "codemode_upfront": "codemode_upfront",
        "codemode": "codemode",
        "hybrid": "hybrid",
        "classic:x_intent": "traditional_discover",
    }
    for mode, preset in expected.items():
        await _list_mcp_tools(client, "token", mode, x_intent="compare all rows")
        await _call_mcp_tool(
            client, "token", mode, 1, "search_tools", {}, x_intent="compare all rows"
        )
        assert captured[-2].headers["x-admin-force-mode"] == preset
        assert captured[-1].headers["x-admin-force-mode"] == preset
        assert captured[-1].headers["x-intent"] == "compare all rows"
        if mode == "classic:x_intent":
            assert captured[-1].headers["x-discovery-mode"] == "x_intent"
    await client.aclose()


@pytest.mark.asyncio
async def test_mutating_benchmark_jobs_get_isolated_mock_backend_state(tmp_path) -> None:
    from benchmarks.live_llm_bench import _create_isolated_mutating_job_app
    from gateway.mcp_protocol import request_headers, request_meta

    keys = LocalDevKeyManager()
    from pathlib import Path

    upstreams = str(Path(__file__).resolve().parents[1] / "config" / "upstreams.yaml")
    app_a, backend_a, http_a = _create_isolated_mutating_job_app(
        str(tmp_path),
        discovery_backend="bm25",
        dev_keys=keys,
        upstreams_config_path=upstreams,
        preloaded_tools={},
    )
    app_b, backend_b, http_b = _create_isolated_mutating_job_app(
        str(tmp_path),
        discovery_backend="bm25",
        dev_keys=keys,
        upstreams_config_path=upstreams,
        preloaded_tools={},
    )
    token = keys.issue_user_token(persona="ops_engineer", audience="api://ops-ticketing")

    async def create_case(backend) -> str:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=backend), base_url="http://mock"
        ) as client:
            response = await client.post(
                "/ops/mcp",
                headers={
                    **request_headers("tools/call", tool_name="create_collection_case"),
                    "Authorization": f"Bearer {token}",
                },
                json={
                    "jsonrpc": "2.0",
                    "id": 1,
                    "method": "tools/call",
                    "params": {
                        "_meta": request_meta(),
                        "name": "create_collection_case",
                        "arguments": {
                            "customer_id": "CUST-1003",
                            "reason": "test",
                            "priority": "high",
                        },
                    },
                },
            )
            assert response.status_code == 200
            return response.json()["result"]["structuredContent"]["case_id"]

    assert app_a.state.runtime.store is not app_b.state.runtime.store
    assert await create_case(backend_a) == "CASE-0001"
    assert await create_case(backend_b) == "CASE-0001"
    await http_a.aclose()
    await http_b.aclose()
