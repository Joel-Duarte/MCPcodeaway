"""Tests for Phase 10 composable profiles, scaling backends, logging, metrics, and ROI API."""

from __future__ import annotations

import builtins
import io
import json
import logging
from pathlib import Path
from typing import Any

import httpx
import pytest
import yaml
from opentelemetry import trace

from gateway.auth import LocalDevKeyManager
from gateway.config import AbacRuleConfig, GatewaySettings, UpstreamConfig
from gateway.observability import JsonLogFormatter
from gateway.profiles import resolve_profile
from gateway.server import GatewayRuntime, create_gateway_app
from gateway.state import PostgresAuditStore
from gateway.upstream_clients import DiscoveredTool, UpstreamClientManager


def test_composable_profiles_inheritance_and_overrides(tmp_path: Path) -> None:
    prod = resolve_profile("production", "config/profiles.yaml")
    assert prod.app_env == "prod"
    assert prod.auth_mode == "oidc"
    assert prod.features.dashboard_ui is False
    assert prod.features.comparator is False
    assert prod.features.dev_auth is False
    assert prod.features.admin_write is False
    assert prod.features.metrics is True
    assert prod.features.audit_api is True

    demo = resolve_profile("demo", "config/profiles.yaml")
    assert demo.inheritance_chain == ["production", "staging", "demo"]
    assert demo.features.dev_auth is True
    assert demo.features.dashboard_ui is True

    finance = resolve_profile("finance_strict", "config/profiles.yaml")
    assert finance.inheritance_chain == ["production", "finance_strict"]
    assert finance.features.admin_read is False
    assert finance.require_tool_grounding is True

    # Custom profile without `extends:` starts on top of barebones `production`.
    custom_yaml = tmp_path / "profiles.yaml"
    custom_yaml.write_text(
        yaml.safe_dump(
            {
                "profiles": {
                    "custom_team": {"features": {"dashboard_ui": True}},
                    "loop_a": {"extends": "loop_b"},
                    "loop_b": {"extends": "loop_a"},
                }
            }
        ),
        encoding="utf-8",
    )
    custom = resolve_profile("custom_team", custom_yaml)
    assert custom.inheritance_chain == ["production", "custom_team"]
    assert custom.app_env == "prod"
    assert custom.auth_mode == "oidc"
    assert custom.features.dashboard_ui is True
    assert custom.features.dev_auth is False
    assert custom.features.comparator is False

    # Explicit overrides take precedence over profile defaults.
    overridden = GatewaySettings(
        profile="custom_team",
        profiles_config_path=str(custom_yaml),
        auth_mode="dev",
        upstreams_config_path=str(tmp_path / "none.yaml"),
    )
    assert overridden.effective_profile_name == "custom_team"
    assert overridden.auth_mode == "dev"
    assert overridden.app_env == "prod"

    with pytest.raises(ValueError, match="Circular profile inheritance"):
        resolve_profile("loop_a", custom_yaml)
    with pytest.raises(ValueError, match="Unknown gateway profile"):
        resolve_profile("missing_profile", custom_yaml)


@pytest.mark.asyncio
async def test_barebones_production_profile_route_gating(tmp_path: Path) -> None:
    settings = GatewaySettings(
        profile="production",
        sqlite_path=str(tmp_path / "gateway.db"),
        upstreams_config_path=str(tmp_path / "missing.yaml"),
    )
    app = create_gateway_app(settings=settings, preloaded_tools={})
    transport = httpx.ASGITransport(app=app)

    async with httpx.AsyncClient(transport=transport, base_url="http://gateway") as gw:
        health = await gw.get("/health")
        assert health.status_code == 200
        assert health.json()["profile"] == "production"
        assert health.json()["features"]["dashboard_ui"] is False

        ready = await gw.get("/ready")
        assert ready.status_code == 200
        assert ready.json()["ready"] is True

        metrics = await gw.get("/metrics")
        assert metrics.status_code == 200

        for hidden_method, hidden_path, body, expected_status in (
            ("GET", "/dashboard", None, 404),
            ("GET", "/benchmarks/latest", None, 404),
            ("GET", "/dev/oidc/jwks", None, 404),
            ("POST", "/dev/auth/token", {"persona": "finance_analyst"}, 404),
            ("POST", "/compare/run", {"scenario": "reconcile_overdue"}, 404),
            ("GET", "/admin/upstreams/agent-prompt", None, 404),
            ("POST", "/admin/upstreams", {"name": "x", "type": "mcp_http"}, 405),
            ("DELETE", "/admin/upstreams/x", None, 404),
        ):
            res = await gw.request(hidden_method, hidden_path, json=body)
            assert res.status_code == expected_status, (hidden_method, hidden_path, res.text)

        for protected_method, protected_path in (
            ("POST", "/mcp"),
            ("GET", "/audit/value"),
            ("GET", "/admin/upstreams"),
        ):
            res = await gw.request(protected_method, protected_path)
            assert res.status_code == 401, (protected_method, protected_path, res.text)


@pytest.mark.asyncio
async def test_pooled_http_client_reuse_and_shutdown(tmp_path: Path) -> None:
    manager = UpstreamClientManager()
    c1 = manager._get_client()
    c2 = manager._get_client()
    assert c1 is c2
    assert c1.is_closed is False
    await manager.close()
    assert c1.is_closed is True
    assert manager._owned_client is None

    settings = GatewaySettings(
        sqlite_path=str(tmp_path / "gw.db"),
        upstreams_config_path=str(tmp_path / "missing.yaml"),
    )
    runtime = GatewayRuntime(settings=settings, preloaded_tools={})
    owned = runtime.client_manager._get_client()
    await runtime.shutdown()
    assert owned.is_closed is True


@pytest.mark.asyncio
async def test_stateful_mcp_session_id_negotiation_and_traceparent() -> None:
    calls_seen: list[dict[str, Any]] = []
    session_counter = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal session_counter
        body = json.loads(request.content.decode("utf-8"))
        method = body.get("method")
        sid = request.headers.get("mcp-session-id")
        calls_seen.append(
            {
                "method": method,
                "mcp_session_id": sid,
                "traceparent": request.headers.get("traceparent"),
                "sub": request.headers.get("authorization"),
            }
        )
        if method == "initialize":
            session_counter += 1
            return httpx.Response(
                200,
                headers={"Mcp-Session-Id": f"sess-{session_counter}"},
                json={"jsonrpc": "2.0", "id": 1, "result": {"protocolVersion": "2025-03-26"}},
            )
        if sid == "expired-force":
            return httpx.Response(404, json={"error": "Session expired"})
        if not sid:
            return httpx.Response(400, json={"error": "Missing Mcp-Session-Id"})
        return httpx.Response(
            200,
            json={
                "jsonrpc": "2.0",
                "id": 1,
                "result": {"structuredContent": {"session_echo": sid}},
            },
        )

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    manager = UpstreamClientManager(http_client=client)
    upstream = UpstreamConfig(
        name="stateful_srv",
        type="mcp_http",
        url="https://mcp.example/mcp",
        auth_type="none",
        stateful_session=True,
    )
    tool = DiscoveredTool(
        server="stateful_srv",
        name="run_step",
        qualified_name="stateful_srv__run_step",
    )

    tracer = trace.get_tracer("test.traceparent")
    with tracer.start_as_current_span("test.outbound"):
        res_a = await manager.call_tool(upstream, tool, {}, session_scope="tenant-a:user-1")
        res_a2 = await manager.call_tool(upstream, tool, {}, session_scope="tenant-a:user-1")
        res_b = await manager.call_tool(upstream, tool, {}, session_scope="tenant-a:user-2")

    assert res_a == {"session_echo": "sess-1"}
    assert res_a2 == {"session_echo": "sess-1"}
    assert res_b == {"session_echo": "sess-2"}
    assert all(entry["traceparent"] for entry in calls_seen)

    # Simulate stale server session -> manager recovers cleanly via re-initialize + retry.
    manager._session_ids["stateful_srv:tenant-a:user-1"] = "expired-force"
    recovered = await manager.call_tool(upstream, tool, {}, session_scope="tenant-a:user-1")
    assert recovered == {"session_echo": "sess-3"}

    await client.aclose()


@pytest.mark.asyncio
async def test_postgres_audit_store_crud_and_optional_dep(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class FakePgPool:
        def __init__(self) -> None:
            self.ddl: list[str] = []
            self.runs: dict[str, dict[str, Any]] = {}
            self.upstreams: dict[str, dict[str, Any]] = {}
            self.closed = False

        async def execute(self, sql: str, *args: Any) -> str:
            norm = " ".join(sql.split())
            if norm.startswith("CREATE"):
                self.ddl.append(norm)
                return "CREATE TABLE"
            if norm.startswith("INSERT INTO audit_runs"):
                self.runs[args[0]] = json.loads(args[5])
                return "INSERT 0 1"
            if norm.startswith("INSERT INTO dynamic_upstreams"):
                self.upstreams[args[0]] = json.loads(args[1])
                return "INSERT 0 1"
            if norm.startswith("DELETE FROM dynamic_upstreams"):
                existed = args[0] in self.upstreams
                self.upstreams.pop(args[0], None)
                return "DELETE 1" if existed else "DELETE 0"
            raise AssertionError(f"Unexpected SQL: {norm}")

        async def fetch(self, sql: str, *args: Any) -> list[dict[str, Any]]:
            norm = " ".join(sql.split())
            if "FROM audit_runs" in norm:
                if "WHERE tenant_id" in norm:
                    tenant, limit = args[0], args[1]
                    items = [r for r in self.runs.values() if r.get("tenant_id") == tenant]
                else:
                    limit = args[0]
                    items = list(self.runs.values())
                items.sort(key=lambda r: r["created_at"], reverse=True)
                return [{"payload_json": json.dumps(r)} for r in items[:limit]]
            if "FROM dynamic_upstreams" in norm:
                return [
                    {"config_json": json.dumps(self.upstreams[k])} for k in sorted(self.upstreams)
                ]
            raise AssertionError(f"Unexpected fetch SQL: {norm}")

        async def fetchrow(self, sql: str, *args: Any) -> dict[str, Any] | None:
            run = self.runs.get(args[0])
            return {"payload_json": json.dumps(run)} if run is not None else None

        async def close(self) -> None:
            self.closed = True

    pool = FakePgPool()
    store = PostgresAuditStore("postgresql://user:pass@localhost/db", pool=pool)
    await store.init()
    assert len(pool.ddl) == 4

    await store.record_run(
        {"run_id": "r1", "tenant_id": "t1", "created_at": "2026-01-01T00:00:01Z"}
    )
    await store.record_run(
        {"run_id": "r2", "tenant_id": "t2", "created_at": "2026-01-01T00:00:02Z"}
    )
    assert [r["run_id"] for r in await store.list_runs()] == ["r2", "r1"]
    assert [r["run_id"] for r in await store.list_runs(tenant_id="t1")] == ["r1"]
    assert (await store.get_run("r2"))["tenant_id"] == "t2"

    await store.save_dynamic_upstream("srv_a", {"name": "srv_a", "type": "mcp_http"})
    assert len(await store.list_dynamic_upstreams()) == 1
    assert await store.delete_dynamic_upstream("srv_a") is True
    assert await store.delete_dynamic_upstream("srv_a") is False
    await store.close()
    assert pool.closed is True

    # Missing asyncpg gives actionable extra message.
    real_import = builtins.__import__

    def fake_import(name: str, *args: Any, **kwargs: Any):
        if name == "asyncpg":
            raise ImportError("no asyncpg")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", fake_import)
    unconnected = PostgresAuditStore("postgresql://user:pass@localhost/db")
    with pytest.raises(RuntimeError, match="mcpcodeaway\\[postgres\\]"):
        await unconnected.init()


@pytest.mark.asyncio
async def test_structured_json_logs_metrics_and_value_roi_api(tmp_path: Path) -> None:
    large_rows = [
        {
            "id": f"INV-{idx:04d}",
            "client_group": "EU" if idx < 2 else "US",
            "note": f"ssn=123-45-6789 key=sk-live1234567890abcdef pad={'x' * 500}",
        }
        for idx in range(6)
    ]

    def upstream_handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/ledger/rows":
            return httpx.Response(200, json={"rows": large_rows, "count": len(large_rows)})
        return httpx.Response(404)

    http_client = httpx.AsyncClient(transport=httpx.MockTransport(upstream_handler))
    openapi_spec = {
        "openapi": "3.0.0",
        "info": {"title": "Ledger", "version": "1.0.0"},
        "paths": {
            "/rows": {
                "get": {
                    "operationId": "get_rows",
                    "responses": {"200": {"description": "ok"}},
                }
            }
        },
    }
    upstreams_yaml = tmp_path / "upstreams.yaml"
    upstreams_yaml.write_text(
        yaml.safe_dump(
            {
                "upstreams": [
                    {
                        "name": "ledger_api",
                        "type": "openapi",
                        "url": "http://mock.local/ledger",
                        "openapi_spec": openapi_spec,
                        "auth_type": "delegated_token",
                        "downstream_audience": "api://ledger",
                        "downstream_scope": "ledger.read",
                        "abac_rules": [
                            AbacRuleConfig(
                                claim="client_groups",
                                response_list_key="rows",
                                row_field="client_group",
                            ).model_dump()
                        ],
                    }
                ]
            }
        ),
        encoding="utf-8",
    )

    settings = GatewaySettings(
        app_env="uat",
        auth_mode="dev",
        require_admin_auth=True,
        allowed_tenants=["tenant-acme-dev", "tenant-other"],
        sqlite_path=str(tmp_path / "gw.db"),
        upstreams_config_path=str(upstreams_yaml),
        context_overflow_threshold_tokens=500,
        cost_per_1m_input_tokens_usd=4.0,
    )
    dev_keys = LocalDevKeyManager(issuer=settings.oidc_issuer)
    app = create_gateway_app(
        settings=settings,
        dev_keys=dev_keys,
        http_client=http_client,
    )

    log_buf = io.StringIO()
    mem_handler = logging.StreamHandler(log_buf)
    mem_handler.setFormatter(JsonLogFormatter())
    gw_logger = logging.getLogger("mcpcodeaway")
    gw_logger.addHandler(mem_handler)

    try:
        eu_tok = dev_keys.issue_user_token(persona="eu_accountant")
        transport = httpx.ASGITransport(app=app)
        async with app.router.lifespan_context(app):
            async with httpx.AsyncClient(transport=transport, base_url="http://gateway") as gw:
                for idx in range(2):
                    res = await gw.post(
                        "/mcp?mode=codemode",
                        headers={
                            "Authorization": f"Bearer {eu_tok}",
                            "X-Request-Id": f"req-roi-{idx}",
                        },
                        json={
                            "jsonrpc": "2.0",
                            "id": idx + 1,
                            "method": "tools/call",
                            "params": {
                                "name": "execute_code",
                                "arguments": {
                                    "code": (
                                        "data = await ledger_api__get_rows()\n"
                                        "ids = [r['id'] for r in data['rows']]\n"
                                        "{'count': data['count'], 'ids': ids}"
                                    )
                                },
                            },
                        },
                    )
                    assert res.status_code == 200
                    body = res.json()["result"]["structuredContent"]
                    assert body["ok"] is True
                    assert body["result"]["ids"] == ["INV-0000", "INV-0001"]
                    assert res.headers.get("x-request-id") == f"req-roi-{idx}"

                prom = await gw.get("/metrics")
                assert prom.status_code == 200
                for metric_name in (
                    "gateway_mcp_requests_total",
                    "gateway_tokens_saved_total",
                    "gateway_abac_rows_filtered_total",
                    "gateway_redactions_total",
                    "gateway_obo_token_cache_total",
                    "gateway_execution_duration_seconds_bucket",
                ):
                    assert metric_name in prom.text

                val_res = await gw.get(
                    "/audit/value", headers={"Authorization": f"Bearer {eu_tok}"}
                )
                assert val_res.status_code == 200
                val = val_res.json()
                assert val["window_runs"] == 2
                assert val["tenant_scope"] == "tenant-acme-dev"
                assert val["efficiency"]["total_tokens_saved"] > 0
                assert val["efficiency"]["estimated_llm_cost_saved_usd"] > 0
                assert val["efficiency"]["context_overflows_prevented"] == 2
                assert val["efficiency"]["obo_token_cache_misses"] == 1
                assert val["efficiency"]["obo_token_cache_hits"] == 1
                assert val["security_and_governance"]["abac_rows_filtered"] == 8
                assert val["security_and_governance"]["pii_secrets_redacted"] >= 24

                cross = await gw.get(
                    "/audit/value?tenant_id=tenant-other",
                    headers={"Authorization": f"Bearer {eu_tok}"},
                )
                assert cross.status_code == 403
    finally:
        gw_logger.removeHandler(mem_handler)
        await http_client.aclose()

    raw_logs = log_buf.getvalue()
    assert eu_tok not in raw_logs
    parsed_logs = [json.loads(line) for line in raw_logs.strip().splitlines() if line.strip()]
    events = {entry["event"] for entry in parsed_logs}
    assert {
        "auth.obo.exchanged",
        "abac.filtered",
        "redaction.applied",
        "sandbox.executed",
        "mcp.request.completed",
    } <= events
    roi_logs = [entry for entry in parsed_logs if entry.get("request_id") == "req-roi-0"]
    assert roi_logs and all("trace_id" in entry for entry in roi_logs)


def test_multitarget_dockerfile_and_compose_prod_profile() -> None:
    dockerfile_text = Path("Dockerfile").read_text(encoding="utf-8")
    assert "AS gateway" in dockerfile_text
    assert "AS sandbox-worker" in dockerfile_text
    assert "AS gateway-demo" in dockerfile_text
    gw_section = dockerfile_text.split("AS gateway")[1].split("AS sandbox-worker")[0]
    assert "examples" not in gw_section

    compose = yaml.safe_load(Path("docker-compose.yml").read_text(encoding="utf-8"))
    prod_gw = compose["services"]["gateway-prod"]
    assert prod_gw["build"]["target"] == "gateway"
    assert prod_gw["environment"]["GATEWAY_PROFILE"] == "production"
    assert "GATEWAY_POSTGRES_DSN" in prod_gw["environment"]
    assert "prod" in compose["services"]["postgres"]["profiles"]
    assert "prod" in compose["services"]["redis"]["profiles"]
    assert "prod" in compose["services"]["sandbox-worker"]["profiles"]
