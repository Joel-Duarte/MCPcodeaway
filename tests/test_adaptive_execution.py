from datetime import UTC, datetime, timedelta

import httpx
import pytest

from benchmarks.live_llm_bench import export_adaptive_evidence
from gateway.adaptive_execution import resolve_adaptive_recommendation, summarize_pairs
from gateway.auth import CallerIdentity, LocalDevKeyManager
from gateway.config import AbExperimentConfig, GatewaySettings, UpstreamConfig
from gateway.server import GatewayRuntime, create_gateway_app
from gateway.state import SQLiteAuditStore
from gateway.upstream_clients import DiscoveredTool


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


@pytest.mark.asyncio
async def test_ab_assignment_preserves_explicit_execution_and_discovery_axis(tmp_path) -> None:
    settings = GatewaySettings(
        auth_mode="dev",
        sqlite_path=str(tmp_path / "ab-mode-precedence.db"),
        upstreams_config_path=str(tmp_path / "missing-upstreams.yaml"),
    )
    runtime = GatewayRuntime(settings=settings, preloaded_tools={})
    await runtime.store.init()
    upstream = UpstreamConfig(
        name="experiment_api",
        type="mcp_http",
        url="https://api.example.test/mcp",
        auth_type="none",
        allow_all_callers=True,
        ab_experiment=AbExperimentConfig(
            enabled=True,
            experiment_id="exp_mode_axes",
            codemode_traffic_pct=100,
        ),
    )
    runtime.registry._static[upstream.name] = upstream
    identity = CallerIdentity(sub="caller-1", tenant_id="tenant-1")

    explicit = await runtime.resolve_dynamic_mode(
        identity,
        requested_exec="classic",
        requested_disc="upfront",
        requested_mode="traditional",
        allowed_servers=None,
        intent=None,
        request_id="explicit-classic",
        explicit_override=False,
    )
    assert explicit[:3] == ("classic", "upfront", "traditional")
    assert explicit[3] == "explicit:traditional"
    assert explicit[5] is None

    assigned = await runtime.resolve_dynamic_mode(
        identity,
        requested_exec="auto",
        requested_disc="deferred",
        requested_mode="auto",
        allowed_servers=None,
        intent=None,
        request_id="auto-deferred",
        explicit_override=False,
    )
    assert assigned[:3] == ("codemode", "deferred", "codemode")
    assert assigned[5:] == ("exp_mode_axes", "codemode")

    runtime.registry._static[upstream.name] = upstream.model_copy(
        update={"preferred_mode": "traditional_discover"}
    )
    preferred = await runtime.resolve_dynamic_mode(
        identity,
        requested_exec="auto",
        requested_disc="upfront",
        requested_mode="auto",
        allowed_servers=None,
        intent=None,
        request_id="upstream-preference",
        explicit_override=False,
        target_tool="experiment_api__get_record",
    )
    assert preferred[:3] == ("classic", "deferred", "traditional_discover")
    assert preferred[3] == "upstream_preference:experiment_api:traditional_discover"

    shared_preference = await runtime.resolve_dynamic_mode(
        identity,
        requested_exec="auto",
        requested_disc="upfront",
        requested_mode="auto",
        allowed_servers=None,
        intent=None,
        request_id="shared-upstream-preference",
        explicit_override=False,
    )
    assert shared_preference[:3] == ("classic", "deferred", "traditional_discover")

    explicit_discovery = await runtime.resolve_dynamic_mode(
        identity,
        requested_exec="auto",
        requested_disc="upfront",
        requested_mode="auto",
        allowed_servers=None,
        intent=None,
        request_id="explicit-discovery",
        explicit_override=False,
        discovery_override=True,
        target_tool="experiment_api__get_record",
    )
    assert explicit_discovery[:3] == ("classic", "upfront", "traditional")

    runtime.registry._static[upstream.name] = upstream
    runtime.catalog.list_accessible_tools = lambda *args, **kwargs: [
        DiscoveredTool(
            server=upstream.name,
            name="write_record",
            qualified_name=f"{upstream.name}__write_record",
            mutating=True,
        )
    ]
    write_request = await runtime.resolve_dynamic_mode(
        identity,
        requested_exec="auto",
        requested_disc="upfront",
        requested_mode="auto",
        allowed_servers=None,
        intent="update record",
        request_id="read-only-experiment-write",
        explicit_override=False,
        target_tool=f"{upstream.name}__write_record",
    )
    assert write_request[5:] == (None, None)
    assert not str(write_request[3]).startswith("ab_experiment:")
    await runtime.store.close()


@pytest.mark.asyncio
async def test_read_only_experiment_eligibility_is_per_target_tool(tmp_path) -> None:
    runtime = GatewayRuntime(
        settings=GatewaySettings(
            auth_mode="dev",
            sqlite_path=str(tmp_path / "ab-mixed-upstream.db"),
            upstreams_config_path=str(tmp_path / "missing-upstreams.yaml"),
        ),
        preloaded_tools={},
    )
    await runtime.store.init()
    upstream = UpstreamConfig(
        name="mixed_api",
        type="mcp_http",
        url="https://api.example.test/mcp",
        auth_type="none",
        allow_all_callers=True,
        ab_experiment=AbExperimentConfig(
            enabled=True,
            experiment_id="mixed_read_experiment",
            codemode_traffic_pct=100,
            read_only_only=True,
        ),
        tool_effects={"read_item": "read", "write_item": "write"},
        mutating_tools=["write_item"],
    )
    runtime.registry._static[upstream.name] = upstream
    runtime.catalog.set_server_tools(
        upstream,
        [
            DiscoveredTool(
                server="mixed_api",
                name="read_item",
                qualified_name="mixed_api__read_item",
                input_schema={"type": "object", "properties": {}},
                mutating=False,
            ),
            DiscoveredTool(
                server="mixed_api",
                name="write_item",
                qualified_name="mixed_api__write_item",
                input_schema={"type": "object", "properties": {}},
                mutating=True,
            ),
        ],
    )
    identity = CallerIdentity(sub="caller-1", tenant_id="tenant-1")

    read = await runtime.resolve_dynamic_mode(
        identity,
        requested_exec="auto",
        requested_disc="upfront",
        requested_mode="auto",
        allowed_servers=None,
        intent=None,
        request_id="mixed-read",
        explicit_override=False,
        target_tool="mixed_api__read_item",
    )
    write = await runtime.resolve_dynamic_mode(
        identity,
        requested_exec="auto",
        requested_disc="upfront",
        requested_mode="auto",
        allowed_servers=None,
        intent=None,
        request_id="mixed-write",
        explicit_override=False,
        target_tool="mixed_api__write_item",
    )
    mixed_code = await runtime.resolve_dynamic_mode(
        identity,
        requested_exec="auto",
        requested_disc="upfront",
        requested_mode="auto",
        allowed_servers=None,
        intent=None,
        request_id="mixed-code-read-write",
        explicit_override=False,
        target_tool="mixed_api__read_item",
        target_tools=["mixed_api__read_item", "mixed_api__write_item"],
    )

    assert read[5:] == ("mixed_read_experiment", "codemode")
    assert write[5] is None
    assert mixed_code[5] is None
    await runtime.store.close()


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
    for row in evidence:
        row["input_tokens"] = 100 if row["mode"] == "codemode" else 200
        row["output_tokens"] = 0
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://gateway"
        ) as client:
            report = await client.get(
                "/admin/adaptive-execution/report",
                headers={"Authorization": f"Bearer {viewer}"},
            )
            assert report.status_code == 200
            assert report.json()["routing_window_days"] == 30
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
            report = await client.get(
                "/admin/adaptive-execution/report?window=1h",
                headers={"Authorization": f"Bearer {viewer}"},
            )
            exact_row = report.json()["rows"][0]
            assert exact_row["recommended_mode"] == "codemode"
            assert exact_row["live_routing"]["recommended_mode"] == "codemode"
            assert exact_row["live_routing"]["evidence_tier"] == "exact"
            assert report.json()["routing_window_days"] == 30
            dedup = await client.post(
                "/admin/adaptive-execution/evidence",
                headers={"Authorization": f"Bearer {admin}"},
                json={"evidence": evidence},
            )
            assert dedup.json()["imported"] == 0
            retired_calibration = await client.get(
                "/admin/ab/settings", headers={"Authorization": f"Bearer {admin}"}
            )
            assert retired_calibration.status_code == 404
            retired_model_report = await client.get(
                "/admin/ab/model-performance", headers={"Authorization": f"Bearer {admin}"}
            )
            assert retired_model_report.status_code == 404
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


def test_live_resolver_returns_exact_recommendation_details() -> None:
    rows = _pair()
    for row in rows:
        row["input_tokens"] = 100 if row["mode"] == "codemode" else 200
        row["output_tokens"] = 0

    decision = resolve_adaptive_recommendation(rows, "m", "single_read")

    assert decision["recommended_mode"] == "codemode"
    assert decision["evidence_tier"] == "exact"
    assert decision["pairs"] == 5
    assert decision["status"] == "recommended"


def test_live_resolver_reports_workload_family_fallback() -> None:
    rows = _pair()
    for row in rows:
        row["workload"] = "large_fanout"
        row["input_tokens"] = 100 if row["mode"] == "codemode" else 200
        row["output_tokens"] = 0

    decision = resolve_adaptive_recommendation(rows, "m", "small_fanout")

    assert decision["recommended_mode"] == "codemode"
    assert decision["evidence_tier"] == "workload_family"
    assert decision["evidence_workload"] == "large_fanout"
    assert decision["pairs"] == 5


def test_live_resolver_explains_sparse_and_conflicting_evidence() -> None:
    sparse = resolve_adaptive_recommendation(_pair(3), "m", "single_read")
    assert sparse["recommended_mode"] is None
    assert sparse["status"] == "insufficient_evidence"

    traditional = _pair()
    codemode = _pair()
    for row in traditional:
        row["workload"] = "small_fanout"
        row["input_tokens"] = 100 if row["mode"] == "traditional" else 200
        row["output_tokens"] = 0
    for row in codemode:
        row["workload"] = "large_fanout"
        row["evidence_id"] = f"large:{row['evidence_id']}"
        row["input_tokens"] = 200 if row["mode"] == "traditional" else 100
        row["output_tokens"] = 0

    conflict = resolve_adaptive_recommendation(traditional + codemode, "m", "single_read")
    assert conflict["recommended_mode"] is None
    assert conflict["status"] == "conflicting_evidence"


@pytest.mark.asyncio
async def test_demo_workflow_evidence_is_reported_but_never_recommended(tmp_path) -> None:
    keys = LocalDevKeyManager()
    settings = GatewaySettings(
        auth_mode="dev",
        profile="demo",
        sqlite_path=str(tmp_path / "demo-evidence.db"),
        upstreams_config_path=str(tmp_path / "no-upstreams.yaml"),
        allowed_tenants=["tenant-acme-dev"],
    )
    app = create_gateway_app(settings=settings, dev_keys=keys, preloaded_tools={})
    admin = keys.issue_user_token(persona="gateway_admin", audience=settings.oidc_audience)
    headers = {"Authorization": f"Bearer {admin}"}
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://gateway"
        ) as client:
            profiles = await client.get("/admin/demo/models", headers=headers)
            profile_list = profiles.json()["models"]
            profile = profile_list[0]
            profile["input_token_range"] = [710, 2220]
            profile["output_token_range"] = [150, 490]
            updated = await client.post(
                "/admin/demo/models", headers=headers, json={"models": profile_list}
            )
            assert updated.status_code == 200
            assert updated.json()["models"][0]["input_token_range"] == [710, 2220]
            assert updated.json()["models"][0]["output_token_range"] == [150, 490]
            invalid_profile = {**profile, "input_token_range": [2220, 710]}
            invalid = await client.post(
                "/admin/demo/models",
                headers=headers,
                json={"models": [invalid_profile, *profile_list[1:]]},
            )
            assert invalid.status_code == 422
            seeded = await client.post("/admin/demo/workflow-evidence", headers=headers)
            assert seeded.status_code == 200, seeded.text
            assert seeded.json()["evidence_rows"] == 360
            assert seeded.json()["workloads_per_model"] == 9
            report = await client.get("/admin/adaptive-execution/report", headers=headers)
            rows = report.json()["rows"]
            assert len(rows) == 36
            assert all(row["evidence_tier"] == "demo_simulated" for row in rows)
            assert all(row["recommended_mode"] is None for row in rows)
            assert {row["simulated_recommendation"] for row in rows} == {
                "codemode",
                "traditional",
                None,
            }
            assert any(row["traditional"]["mean_cost_usd"] is not None for row in rows)
            assert any(
                row["traditional"]["mean_provider_tokens"]
                != row["codemode"]["mean_provider_tokens"]
                for row in rows
                if row["traditional"] and row["codemode"]
            )
            stored = await app.state.runtime.store.list_adaptive_evidence("tenant-acme-dev")
            assert stored == []


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
        assert active[3].startswith("adaptive_exact:single_read:codemode;")
        assert "USD cost is at least 5% lower" in active[3]
    execution = await resolve("tools/call:execute_code", active=True)
    assert execution[0] == "codemode"
    assert execution[3].startswith("adaptive_exact:single_read:codemode;")
    rollback = await resolve("tools/list", active=False)
    assert "adaptive_recommendation:exact:single_read:codemode" in rollback[3]
    risky = await resolve("tools/call", active=True, write=True)
    assert "adaptive_unavailable:mutating_tools" in str(risky[3])
    explicit = await resolve("tools/call", active=True, explicit=True)
    assert explicit[3] == "explicit:traditional_discover"
    await runtime.store.close()


@pytest.mark.asyncio
async def test_active_mcp_auto_routes_two_mock_models_to_opposite_winners(tmp_path) -> None:
    from gateway.config import UpstreamConfig
    from gateway.mcp_protocol import request_headers, request_meta
    from gateway.upstream_clients import DiscoveredTool

    keys = LocalDevKeyManager()
    settings = GatewaySettings(
        auth_mode="dev",
        profile="demo",
        sqlite_path=str(tmp_path / "two-model-routing.db"),
        upstreams_config_path=str(tmp_path / "no-upstreams.yaml"),
        allowed_tenants=["tenant-acme-dev"],
    )
    tool = DiscoveredTool(
        server="ledger",
        name="get_customer_balance",
        qualified_name="ledger__get_customer_balance",
        input_schema={
            "type": "object",
            "properties": {"customer_id": {"type": "string"}},
            "required": ["customer_id"],
        },
    )
    app = create_gateway_app(
        settings=settings,
        dev_keys=keys,
        preloaded_tools={"ledger": [tool]},
    )
    admin = keys.issue_user_token(persona="gateway_admin", audience=settings.oidc_audience)
    finance = keys.issue_user_token(persona="finance_analyst", audience=settings.oidc_audience)
    headers = {"Authorization": f"Bearer {admin}"}
    evidence = []
    expected_winners = {
        "mock-classic-wins": "traditional",
        "mock-codemode-wins": "codemode",
    }
    for model, winner in expected_winners.items():
        for row in _pair():
            row["model"] = model
            row["evidence_id"] = f"{model}:{row['evidence_id']}"
            row["fixture_id"] = "mock-single-read"
            row["input_tokens"] = 100 if row["mode"] == winner else 200
            row["output_tokens"] = 0
            evidence.append(row)

    async with app.router.lifespan_context(app):
        runtime = app.state.runtime
        upstream = UpstreamConfig(
            name="ledger",
            type="mcp_http",
            url="http://preloaded.local",
            auth_type="none",
            allowed_roles=["finance_analyst", "gateway_admin"],
            allowed_operations=["get_customer_balance"],
        )
        runtime.registry._static["ledger"] = upstream
        runtime.catalog.set_server_tools(upstream, [tool])
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://gateway"
        ) as client:
            imported = await client.post(
                "/admin/adaptive-execution/evidence",
                headers=headers,
                json={"evidence": evidence},
            )
            assert imported.status_code == 200, imported.text
            assert imported.json()["imported"] == 20
            active = await client.post(
                "/admin/adaptive-execution/settings",
                headers=headers,
                json={"mode": "active"},
            )
            assert active.status_code == 200
            report = await client.get("/admin/adaptive-execution/report", headers=headers)
            reported_winners = {
                row["model"]: row["live_routing"]["recommended_mode"]
                for row in report.json()["rows"]
                if row.get("live_routing")
            }
            assert reported_winners == expected_winners

            for model, winner in expected_winners.items():
                call_headers = {
                    **request_headers("tools/list"),
                    "Authorization": f"Bearer {finance}",
                    "X-Model-Name": model,
                    "X-Intent": "look up one customer balance",
                }
                result = await client.post(
                    "/mcp?execution=auto&discovery_mode=x_intent",
                    headers=call_headers,
                    json={
                        "jsonrpc": "2.0",
                        "id": model,
                        "method": "tools/list",
                        "params": {"_meta": request_meta()},
                    },
                )
                assert result.status_code == 200, result.text
                decision = result.headers["x-gateway-mode-decision"]
                assert decision.startswith(f"adaptive_exact:single_read:{winner};")
                listed = result.json()["result"]["tools"]
                names = {row["name"] for row in listed}
                if winner == "codemode":
                    assert "execute_code" in names
                else:
                    assert "ledger__get_customer_balance" in names
                    assert "execute_code" not in names


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
