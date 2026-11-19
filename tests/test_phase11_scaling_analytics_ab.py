"""Tests for Phase 11 LB/Redis sessions, keys, admin auth, telemetry, analytics, A/B & UI."""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlparse

import httpx
from fastapi.testclient import TestClient

from gateway.ab_testing import evaluate_mode_algorithm
from gateway.auth import LocalDevKeyManager
from gateway.config import AbExperimentConfig, GatewaySettings, UpstreamConfig
from gateway.dispatcher import (
    SubcallRecord,
    build_provenance_envelope,
    verify_provenance_envelope,
)
from gateway.sandbox_worker import create_sandbox_worker_app
from gateway.server import GatewayRuntime, create_gateway_app
from gateway.state import InMemoryCacheBackend, PostgresAuditStore, SQLiteAuditStore
from gateway.upstream_clients import DiscoveredTool, UpstreamClientManager


def test_multikey_rotation_worker_and_provenance() -> None:
    worker_app = create_sandbox_worker_app(worker_secret="new_worker_key, old_worker_key")
    client = TestClient(worker_app)

    # Both primary and secondary keys work for WebSocket reverse-RPC.
    for key in ("new_worker_key", "old_worker_key"):
        with client.websocket_connect(
            "/ws/execute", headers={"Authorization": f"Bearer {key}"}
        ) as ws:
            ws.send_json({"type": "run", "code": "1 + 2", "tool_names": []})
            msg = ws.receive_json()
            assert msg["type"] == "done"
            assert msg["sandbox_result"]["ok"] is True
            assert msg["sandbox_result"]["result"] == 3

    # Unknown key is rejected cleanly.
    rejected = False
    try:
        with client.websocket_connect(
            "/ws/execute", headers={"Authorization": "Bearer wrong_key"}
        ) as ws:
            ws.receive_json()
    except Exception:
        rejected = True
    assert rejected is True

    # Worker /health exposes ProcessStatsCollector fields.
    health = client.get("/health")
    assert health.status_code == 200
    hdata = health.json()
    assert hdata["status"] == "ok"
    assert "cpu_percent" in hdata
    assert "rss_mb" in hdata
    assert hdata["total_executions"] >= 2

    # Provenance signs with primary key and verifies across rotated keys.
    sc = SubcallRecord(
        tool="demo__list",
        server="demo",
        duration_ms=2.0,
        raw_bytes=120,
        status="ok",
        auth_type="none",
        output_sha256="abc",
    )
    env = build_provenance_envelope(
        allowed_servers={"demo"},
        subcalls=[sc],
        observed_tool_outputs=[{"id": "INV-100"}],
        returned_value={"id": "INV-100"},
        signing_secret=["sig_v2", "sig_v1"],
    )
    assert verify_provenance_envelope(env, "sig_v2, sig_v1") is True
    assert verify_provenance_envelope(env, "sig_v3, sig_v2") is True
    assert verify_provenance_envelope(env, "sig_v1") is False


async def test_shared_cache_mcp_session_and_catalog_epoch(tmp_path: Path) -> None:
    shared_cache = InMemoryCacheBackend()
    mgr_a = UpstreamClientManager(cache=shared_cache, session_ttl_seconds=120)
    mgr_b = UpstreamClientManager(cache=shared_cache, session_ttl_seconds=120)

    await mgr_a._set_session_id("srv:tenant:sub", "sess-shared-123")
    assert await mgr_b._get_session_id("srv:tenant:sub") == "sess-shared-123"
    await mgr_b._delete_session_id("srv:tenant:sub")
    assert await mgr_a._get_session_id("srv:tenant:sub") is None

    # Cross-replica catalog_epoch synchronization.
    tool_v1 = DiscoveredTool(
        server="demo",
        name="ping",
        qualified_name="demo__ping",
        description="Ping v1",
    )
    tool_v2 = DiscoveredTool(
        server="demo",
        name="pong",
        qualified_name="demo__pong",
        description="Pong v2",
    )
    s1 = GatewaySettings(auth_mode="dev", sqlite_path=str(tmp_path / "r1.db"))
    s2 = GatewaySettings(auth_mode="dev", sqlite_path=str(tmp_path / "r1.db"))
    rt1 = GatewayRuntime(settings=s1, cache=shared_cache, preloaded_tools={"demo": [tool_v1]})
    rt2 = GatewayRuntime(settings=s2, cache=shared_cache, preloaded_tools={"demo": [tool_v2]})
    await rt1.store.init()
    await rt2.store.init()
    try:
        epoch = await rt1.bump_catalog_epoch()
        assert isinstance(epoch, str) and len(epoch) > 0
        assert rt2._local_catalog_epoch != epoch
        await rt2.sync_catalog_if_stale()
        assert rt2._local_catalog_epoch == epoch
    finally:
        await rt1.shutdown()
        await rt2.shutdown()


async def test_audit_retention_prune_sqlite_postgres_and_admin_endpoint(tmp_path: Path) -> None:
    sqlite_store = SQLiteAuditStore(str(tmp_path / "prune.db"))
    await sqlite_store.init()
    await sqlite_store.record_run(
        {
            "run_id": "run-old",
            "tenant_id": "t1",
            "user_sub": "u1",
            "mode": "codemode",
            "created_at": "2020-01-01T00:00:00+00:00",
        }
    )
    await sqlite_store.record_run(
        {
            "run_id": "run-new",
            "tenant_id": "t1",
            "user_sub": "u1",
            "mode": "codemode",
        }
    )
    deleted = await sqlite_store.prune_runs_older_than(30)
    assert deleted == 1
    remaining = await sqlite_store.list_runs(limit=10)
    assert [r["run_id"] for r in remaining] == ["run-new"]
    await sqlite_store.close()

    # Fake asyncpg pool test for PostgresAuditStore.prune_runs_older_than.
    class FakePool:
        def __init__(self) -> None:
            self.calls: list[tuple[str, tuple[Any, ...]]] = []

        async def execute(self, sql: str, *args: Any) -> str:
            self.calls.append((sql, args))
            return "DELETE 7"

    fake_pool = FakePool()
    pg_store = PostgresAuditStore(pool=fake_pool)
    pg_deleted = await pg_store.prune_runs_older_than(14)
    assert pg_deleted == 7
    assert "DELETE FROM audit_runs WHERE created_at < $1" in fake_pool.calls[0][0]

    # /admin/audit/prune endpoint enforcement + execution.
    settings = GatewaySettings(
        auth_mode="dev",
        profile="demo",
        require_admin_auth=True,
        sqlite_path=str(tmp_path / "ep_prune.db"),
        upstreams_config_path=str(tmp_path / "none.yaml"),
    )
    dev_keys = LocalDevKeyManager(issuer=settings.oidc_issuer)
    app = create_gateway_app(settings=settings, dev_keys=dev_keys, preloaded_tools={})
    rt = app.state.runtime
    await rt.startup()
    await rt.store.record_run(
        {
            "run_id": "old-api-run",
            "tenant_id": "tenant-acme-dev",
            "user_sub": "u1",
            "mode": "traditional",
            "created_at": "2021-01-01T00:00:00+00:00",
        }
    )
    try:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://gw.local"
        ) as gw:
            unauth = await gw.post("/admin/audit/prune?retention_days=30")
            assert unauth.status_code == 401
            fin_tok = dev_keys.issue_user_token(persona="finance_analyst")
            forbidden = await gw.post(
                "/admin/audit/prune?retention_days=30",
                headers={"Authorization": f"Bearer {fin_tok}"},
            )
            assert forbidden.status_code == 403
            adm_tok = dev_keys.issue_user_token(persona="gateway_admin")
            ok_resp = await gw.post(
                "/admin/audit/prune?retention_days=30",
                headers={"Authorization": f"Bearer {adm_tok}"},
            )
            assert ok_resp.status_code == 200
            assert ok_resp.json()["deleted_runs"] == 1
    finally:
        await rt.shutdown()


async def test_dual_mode_admin_auth_oidc_pkce_dev_and_cookie(tmp_path: Path) -> None:
    idp_keys = LocalDevKeyManager(issuer="https://idp.test")
    admin_jwt = idp_keys.issue_user_token(persona="gateway_admin")
    non_admin_jwt = idp_keys.issue_user_token(persona="finance_analyst")

    def idp_handler(request: httpx.Request) -> httpx.Response:
        if str(request.url) == "https://idp.test/jwks":
            return httpx.Response(200, json=idp_keys.jwks())
        if str(request.url) == "https://idp.test/token":
            body = parse_qs(request.content.decode("utf-8"))
            assert body.get("grant_type") == ["authorization_code"]
            assert body.get("code_verifier")
            code = body["code"][0]
            return httpx.Response(
                200,
                json={
                    "access_token": non_admin_jwt if code == "code-non-admin" else admin_jwt,
                    "token_type": "Bearer",
                },
            )
        return httpx.Response(404)

    http_client = httpx.AsyncClient(transport=httpx.MockTransport(idp_handler))
    settings = GatewaySettings(
        auth_mode="oidc",
        profile="demo",
        require_admin_auth=True,
        oidc_issuer="https://idp.test",
        oidc_jwks_uri="https://idp.test/jwks",
        oidc_token_endpoint="https://idp.test/token",
        oidc_audience="api://mcp-code-gateway",
        oidc_client_id="web-admin",
        admin_oidc_authorization_endpoint="https://idp.test/authorize",
        admin_session_secret="cookie-key-new, cookie-key-old",
        sqlite_path=str(tmp_path / "oidc_admin.db"),
        upstreams_config_path=str(tmp_path / "none.yaml"),
    )
    app = create_gateway_app(settings=settings, http_client=http_client, preloaded_tools={})
    rt = app.state.runtime
    await rt.startup()
    try:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://gw.local"
        ) as gw:
            cfg = (await gw.get("/auth/admin/config")).json()
            assert cfg["oidc_enabled"] is True
            assert cfg["dev_auth_enabled"] is False

            # Initiate OIDC PKCE login.
            login_resp = await gw.get(
                "/auth/admin/login?next_url=/dashboard/analytics", follow_redirects=False
            )
            assert login_resp.status_code == 302
            loc = urlparse(login_resp.headers["location"])
            q = parse_qs(loc.query)
            assert q["code_challenge_method"] == ["S256"]
            state_1 = q["state"][0]

            # Callback with non-admin token fails 403.
            bad_cb = await gw.get(
                f"/auth/admin/callback?code=code-non-admin&state={state_1}",
                follow_redirects=False,
            )
            assert bad_cb.status_code == 403

            # Start second PKCE flow and complete as gateway_admin.
            login_2 = await gw.get("/auth/admin/login?next_url=/dashboard/", follow_redirects=False)
            state_2 = parse_qs(urlparse(login_2.headers["location"]).query)["state"][0]
            ok_cb = await gw.get(
                f"/auth/admin/callback?code=code-admin&state={state_2}",
                follow_redirects=False,
            )
            assert ok_cb.status_code == 302
            assert ok_cb.headers["location"] == "/dashboard/"
            assert "mcpcodeaway_admin_session" in gw.cookies

            # Cookie unlocks /auth/admin/session, /admin/*, and /audit/analytics.
            sess = (await gw.get("/auth/admin/session")).json()
            assert sess["authenticated"] is True
            assert sess["is_admin"] is True

            ups = await gw.get("/admin/upstreams")
            assert ups.status_code == 200

            ana = await gw.get("/audit/analytics")
            assert ana.status_code == 200

            # Logout clears cookie.
            out = await gw.post("/auth/admin/logout")
            assert out.json()["logged_out"] is True
            gw.cookies.clear()
            assert (await gw.get("/admin/upstreams")).status_code == 401
    finally:
        await rt.shutdown()
        await http_client.aclose()

    # Dev-mode /dev-login & /token-login checks.
    dev_settings = GatewaySettings(
        auth_mode="dev",
        profile="demo",
        require_admin_auth=True,
        sqlite_path=str(tmp_path / "dev_admin.db"),
        upstreams_config_path=str(tmp_path / "none.yaml"),
    )
    dev_keys = LocalDevKeyManager(issuer=dev_settings.oidc_issuer)
    dev_app = create_gateway_app(settings=dev_settings, dev_keys=dev_keys, preloaded_tools={})
    dev_rt = dev_app.state.runtime
    await dev_rt.startup()
    try:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=dev_app), base_url="http://gw.local"
        ) as gw:
            assert (
                await gw.post("/auth/admin/dev-login", json={"persona": "finance_analyst"})
            ).status_code == 403
            ok_dev = await gw.post("/auth/admin/dev-login", json={"persona": "gateway_admin"})
            assert ok_dev.status_code == 200
            assert ok_dev.json()["authenticated"] is True
            assert (await gw.get("/admin/upstreams")).status_code == 200

            gw.cookies.clear()
            tok = dev_keys.issue_user_token(persona="gateway_admin")
            tok_login = await gw.post("/auth/admin/token-login", json={"token": tok})
            assert tok_login.status_code == 200
            assert (await gw.get("/admin/telemetry/runtime")).status_code == 200
    finally:
        await dev_rt.shutdown()


async def test_runtime_telemetry_analytics_cors_and_cross_tenant_isolation(
    tmp_path: Path,
) -> None:
    def remote_worker_handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/health":
            return httpx.Response(
                200,
                json={
                    "status": "ok",
                    "cpu_percent": 14.5,
                    "rss_mb": 64.2,
                    "load_avg_1m": 0.42,
                    "cpu_count": 2,
                    "uptime_seconds": 100.0,
                    "active_executions": 1,
                    "total_executions": 5,
                },
            )
        return httpx.Response(404)

    http_client = httpx.AsyncClient(transport=httpx.MockTransport(remote_worker_handler))
    settings = GatewaySettings(
        auth_mode="dev",
        profile="demo",
        require_admin_auth=True,
        sandbox_backend="remote",
        sandbox_worker_url="ws://worker-svc:8090/ws/execute",
        cors_allowed_origins=["https://console.example.com"],
        sqlite_path=str(tmp_path / "analytics.db"),
        upstreams_config_path=str(tmp_path / "none.yaml"),
    )
    dev_keys = LocalDevKeyManager(issuer=settings.oidc_issuer)
    app = create_gateway_app(
        settings=settings, dev_keys=dev_keys, http_client=http_client, preloaded_tools={}
    )
    rt = app.state.runtime
    await rt.startup()
    await rt.store.record_run(
        {
            "run_id": "run-fin-1",
            "tenant_id": "tenant-acme-dev",
            "user_sub": "alice",
            "user_email": "alice@acme.test",
            "user_groups": ["finance"],
            "user_client_groups": ["eu_accountant"],
            "mode": "codemode",
            "ok": True,
            "duration_ms": 20.0,
            "schema_tokens": 463,
            "returned_tokens": 120,
            "raw_upstream_bytes": 40000,
            "returned_bytes": 480,
            "estimated_tokens_saved": 9500,
            "schema_tokens_saved": 2400,
            "mode_decision_reason": "auto->codemode (score=85.0): Heavy payload",
            "mode_decision_score": 85.0,
            "subcall_count": 1,
            "subcalls": [
                {
                    "server": "billing_eu",
                    "tool": "list",
                    "status": "ok",
                    "raw_bytes": 40000,
                    "duration_ms": 12.0,
                }
            ],
        }
    )
    await rt.store.record_run(
        {
            "run_id": "run-ops-1",
            "tenant_id": "tenant-other",
            "user_sub": "bob",
            "user_email": "bob@other.test",
            "user_groups": ["ops"],
            "mode": "traditional",
            "ok": True,
            "duration_ms": 10.0,
            "schema_tokens": 1800,
            "returned_tokens": 500,
            "raw_upstream_bytes": 2000,
            "returned_bytes": 2000,
            "estimated_tokens_saved": 0,
            "schema_tokens_saved": 0,
            "subcall_count": 1,
            "subcalls": [
                {
                    "server": "ops_api",
                    "tool": "ping",
                    "status": "ok",
                    "raw_bytes": 2000,
                    "duration_ms": 5.0,
                }
            ],
        }
    )

    try:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://gw.local"
        ) as gw:
            adm_tok = dev_keys.issue_user_token(persona="gateway_admin")
            fin_tok = dev_keys.issue_user_token(persona="finance_analyst")

            # Live remote worker telemetry + CORS header check.
            rt_resp = await gw.get(
                "/admin/telemetry/runtime",
                headers={
                    "Authorization": f"Bearer {adm_tok}",
                    "Origin": "https://console.example.com",
                },
            )
            assert rt_resp.status_code == 200
            assert (
                rt_resp.headers.get("access-control-allow-origin") == "https://console.example.com"
            )
            rt_body = rt_resp.json()
            assert rt_body["sandbox_worker"]["status"] == "ok"
            assert rt_body["sandbox_worker"]["stats"]["cpu_percent"] == 14.5
            assert "cpu_percent" in rt_body["gateway"]

            # Analytics across groups/users/client_groups/model pricing.
            ana = (
                await gw.get(
                    "/audit/analytics?group=finance&model=gpt-4.1",
                    headers={"Authorization": f"Bearer {adm_tok}"},
                )
            ).json()
            assert ana["summary"]["total_runs"] == 1
            assert ana["pricing"]["model"] == "gpt-4.1"
            assert ana["by_company_group"][0]["group"] == "finance"
            assert ana["by_client_group"][0]["client_group"] == "eu_accountant"
            assert ana["by_user"][0]["user_email"] == "alice@acme.test"
            assert ana["mode_decisions"][0]["avg_score"] == 85.0

            # Non-admin tenant isolation on /audit/analytics.
            cross = await gw.get(
                "/audit/analytics?tenant_id=tenant-other",
                headers={"Authorization": f"Bearer {fin_tok}"},
            )
            assert cross.status_code == 403
    finally:
        await rt.shutdown()
        await http_client.aclose()


async def test_ab_engine_algorithm_auto_split_and_onboarding_llm(tmp_path: Path) -> None:
    small = evaluate_mode_algorithm(
        tool_count=2,
        classic_schema_tokens=300,
        avg_payload_bytes=200,
        mutating_ratio=1.0,
        typed_output_ratio=0.0,
    )
    assert small.execution == "classic"
    assert small.score < 55.0

    large = evaluate_mode_algorithm(
        tool_count=120,
        classic_schema_tokens=15000,
        avg_payload_bytes=24000,
        typed_output_ratio=0.8,
        intent="reconcile and summarize all records",
        server_count=3,
    )
    assert large.execution == "codemode"
    assert large.score >= 55.0

    # Mock upstream + mock OpenAI-compatible LLM endpoint.
    tool_schemas = [
        DiscoveredTool(
            server="inv_api",
            name="list_items",
            qualified_name="inv_api__list_items",
            description="List inventory items",
            mutating=False,
            http_method="GET",
            http_path="/items",
            output_schema={"type": "object"},
        ),
        DiscoveredTool(
            server="inv_api",
            name="create_item",
            qualified_name="inv_api__create_item",
            description="Create item",
            mutating=True,
            http_method="POST",
            http_path="/items",
        ),
    ]

    llm_turn_state = {"calls": 0}

    def upstream_and_llm_handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/openapi.json":
            return httpx.Response(
                200,
                json={
                    "openapi": "3.1.0",
                    "info": {"title": "Inv API", "version": "1.0.0"},
                    "paths": {
                        "/items": {
                            "get": {
                                "operationId": "list_items",
                                "summary": "List inventory items",
                                "responses": {"200": {"description": "OK"}},
                            }
                        }
                    },
                },
            )
        if request.url.path == "/items":
            return httpx.Response(200, json={"items": [{"sku": "SKU-9", "qty": 42}]})
        if request.url.path == "/v1/chat/completions":
            body = json.loads(request.content.decode("utf-8"))
            msgs = body["messages"]
            tool_names = [t["function"]["name"] for t in body.get("tools", [])]
            has_tool_reply = any(m.get("role") == "tool" for m in msgs)
            if not has_tool_reply:
                is_cm = "execute_code" in tool_names
                tc = (
                    {
                        "id": "call_1",
                        "type": "function",
                        "function": {
                            "name": "execute_code",
                            "arguments": json.dumps({"code": "await inv_api__list_items()"}),
                        },
                    }
                    if is_cm
                    else {
                        "id": "call_1",
                        "type": "function",
                        "function": {"name": "inv_api__list_items", "arguments": "{}"},
                    }
                )
                return httpx.Response(
                    200,
                    json={
                        "choices": [{"message": {"role": "assistant", "tool_calls": [tc]}}],
                        "usage": {
                            "prompt_tokens": 120 if is_cm else 320,
                            "completion_tokens": 30,
                        },
                    },
                )
            llm_turn_state["calls"] += 1
            return httpx.Response(
                200,
                json={
                    "choices": [{"message": {"role": "assistant", "content": "SKU-9 qty 42"}}],
                    "usage": {"prompt_tokens": 80, "completion_tokens": 20},
                },
            )
        return httpx.Response(404)

    http_client = httpx.AsyncClient(transport=httpx.MockTransport(upstream_and_llm_handler))
    cfg = UpstreamConfig(
        name="inv_api",
        type="openapi",
        url="http://mock-backend.local",
        openapi_spec_url="http://mock-backend.local/openapi.json",
        auth_type="none",
        allowed_roles=["finance_analyst", "gateway_admin"],
        ab_experiment=AbExperimentConfig(
            enabled=True,
            experiment_id="exp_inv_split",
            codemode_traffic_pct=100,
        ),
    )
    settings = GatewaySettings(
        auth_mode="dev",
        profile="demo",
        sqlite_path=str(tmp_path / "ab.db"),
        upstreams_config_path=str(tmp_path / "none.yaml"),
    )
    dev_keys = LocalDevKeyManager(issuer=settings.oidc_issuer)
    app = create_gateway_app(
        settings=settings,
        dev_keys=dev_keys,
        http_client=http_client,
        preloaded_tools={"inv_api": tool_schemas},
    )
    rt = app.state.runtime
    await rt.startup()
    cfg_no_ab = cfg.model_copy(update={"ab_experiment": None})
    rt.registry._static["inv_api"] = cfg_no_ab
    rt.catalog.set_server_tools(cfg_no_ab, tool_schemas)
    try:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://gw.local"
        ) as gw:
            adm_tok = dev_keys.issue_user_token(persona="gateway_admin")
            fin_tok = dev_keys.issue_user_token(persona="finance_analyst")

            # Layer 1: execution=auto routes algorithmically and logs reason + score.
            auto_list = await gw.post(
                "/mcp?execution=auto",
                headers={
                    "Authorization": f"Bearer {fin_tok}",
                    "X-Intent": "reconcile inventory",
                },
                json={"jsonrpc": "2.0", "id": 1, "method": "tools/list", "params": {}},
            )
            assert auto_list.status_code == 200
            assert "auto->" in auto_list.headers.get("x-gateway-mode-decision", "")

            # Layer 3: enable live A/B split on inv_api and verify deterministic attribution.
            rt.registry._static["inv_api"] = cfg
            rt.catalog.set_server_tools(cfg, tool_schemas)

            auto_exec = await gw.post(
                "/mcp?execution=auto",
                headers={"Authorization": f"Bearer {fin_tok}"},
                json={
                    "jsonrpc": "2.0",
                    "id": 2,
                    "method": "tools/call",
                    "params": {
                        "name": "execute_code",
                        "arguments": {"code": "await inv_api__list_items()"},
                    },
                },
            )
            assert auto_exec.status_code == 200
            assert "experiment_id" in json.dumps((await rt.store.list_runs(limit=5))[0])

            # Onboarding A/B Lab: instant replay + live mocked LLM arm.
            ab_resp = await gw.post(
                "/admin/upstreams/ab-test",
                headers={"Authorization": f"Bearer {adm_tok}"},
                json={
                    "upstream_name": "inv_api",
                    "intent": "reconcile inventory",
                    "classic_calls": [{"tool": "inv_api__list_items", "arguments": {}}],
                    "codemode_code": "await inv_api__list_items()",
                    "llm_config": {
                        "base_url": "http://mock-llm.local/v1",
                        "model": "mock-gpt",
                        "prompt": "Report SKU quantity.",
                        "expected_substrings": ["SKU-9"],
                    },
                },
            )
            assert ab_resp.status_code == 200
            ab_data = ab_resp.json()
            assert ab_data["replay_ab"]["winner"] in {"code_mode", "classic_or_tie", "classic"}
            assert ab_data["live_llm_ab"]["winner"] == "code_mode"
            assert ab_data["live_llm_ab"]["code_mode"]["accuracy_passed"] is True

            exps = await gw.get(
                "/admin/ab/experiments", headers={"Authorization": f"Bearer {adm_tok}"}
            )
            assert exps.status_code == 200
            assert "exp_inv_split" in exps.json()["results"]

            # Layer 4: Empirical model score learning + Admin model score overrides.
            qwen_small = evaluate_mode_algorithm(
                tool_count=8,
                classic_schema_tokens=800,
                avg_payload_bytes=1200,
                typed_output_ratio=0.5,
                model_name="qwen/qwen3.5-9b",
                model_score_delta=-18.0,
            )
            flash_strong = evaluate_mode_algorithm(
                tool_count=8,
                classic_schema_tokens=800,
                avg_payload_bytes=1200,
                typed_output_ratio=0.5,
                model_name="gemini-2.5-flash",
                model_score_delta=15.0,
            )
            assert qwen_small.execution == "classic"
            assert flash_strong.execution == "codemode"

            save_score = await gw.post(
                "/admin/routing/models",
                headers={"Authorization": f"Bearer {adm_tok}"},
                json={
                    "model_name": "custom-local-7b",
                    "score_delta": -20.0,
                    "preferred_mode": "classic",
                    "notes": "Benchmarked better on Classic MCP",
                },
            )
            assert save_score.status_code == 200
            assert "custom-local-7b" in save_score.json()["admin_overrides"]

            del_score = await gw.delete(
                "/admin/routing/models/custom-local-7b",
                headers={"Authorization": f"Bearer {adm_tok}"},
            )
            assert del_score.status_code == 200
            assert del_score.json()["deleted"] is True

            auto_promote_cfg = cfg.model_copy(
                update={
                    "ab_experiment": AbExperimentConfig(
                        enabled=True,
                        experiment_id="exp_model_learn",
                        codemode_traffic_pct=50,
                        auto_promote_min_runs=2,
                        auto_switch_enabled=True,
                    )
                }
            )
            rt.registry.upsert(auto_promote_cfg)
            await rt.store.record_run(
                {
                    "run_id": "learn_cm_1",
                    "tenant_id": "tenant-acme-dev",
                    "user_sub": "u1",
                    "mode": "codemode",
                    "ok": True,
                    "experiment_id": "exp_model_learn",
                    "ab_variant": "codemode",
                    "model_name": "gemini-2.5-flash",
                    "estimated_tokens_saved": 4000,
                    "schema_tokens_saved": 500,
                }
            )
            await rt.store.record_run(
                {
                    "run_id": "learn_cl_1",
                    "tenant_id": "tenant-acme-dev",
                    "user_sub": "u1",
                    "mode": "traditional",
                    "ok": True,
                    "experiment_id": "exp_model_learn",
                    "ab_variant": "classic",
                    "model_name": "gemini-2.5-flash",
                    "estimated_tokens_saved": 0,
                    "schema_tokens_saved": 0,
                }
            )
            learned_resp = await gw.post(
                "/mcp?execution=auto",
                headers={
                    "Authorization": f"Bearer {fin_tok}",
                    "X-Model-Name": "gemini-2.5-flash",
                },
                json={"jsonrpc": "2.0", "id": 99, "method": "tools/list", "params": {}},
            )
            assert learned_resp.status_code == 200
            assert "ab_auto_promoted:exp_model_learn:codemode" in learned_resp.headers.get(
                "x-gateway-mode-decision", ""
            )
            assert learned_resp.headers.get("x-gateway-model") == "gemini-2.5-flash"

            # Layer 5: Per-tool + per-server + per-model auto-promotion & auth remediation guidance
            await rt.store.record_run(
                {
                    "run_id": "tool_cm_fail_1",
                    "tenant_id": "tenant-acme-dev",
                    "user_sub": "u1",
                    "mode": "codemode",
                    "ok": False,
                    "tool_name": "execute_code",
                    "subcalls": [{"server": "inv_api", "tool": "inv_api__list_items"}],
                    "experiment_id": "exp_model_learn",
                    "ab_variant": "codemode",
                    "model_name": "qwen/qwen3.5-9b",
                    "estimated_tokens_saved": 0,
                    "schema_tokens_saved": 0,
                }
            )
            await rt.store.record_run(
                {
                    "run_id": "tool_cl_ok_1",
                    "tenant_id": "tenant-acme-dev",
                    "user_sub": "u1",
                    "mode": "traditional",
                    "ok": True,
                    "tool_name": "inv_api__list_items",
                    "subcalls": [{"server": "inv_api", "tool": "inv_api__list_items"}],
                    "experiment_id": "exp_model_learn",
                    "ab_variant": "classic",
                    "model_name": "qwen/qwen3.5-9b",
                    "estimated_tokens_saved": 100,
                    "schema_tokens_saved": 0,
                }
            )
            tool_learned_resp = await gw.post(
                "/mcp?execution=auto",
                headers={
                    "Authorization": f"Bearer {fin_tok}",
                    "X-Model-Name": "qwen/qwen3.5-9b",
                    "X-Intent": "call inv_api__list_items now",
                },
                json={"jsonrpc": "2.0", "id": 100, "method": "tools/list", "params": {}},
            )
            assert tool_learned_resp.status_code == 200
            assert (
                "ab_auto_promoted_tool:exp_model_learn:inv_api__list_items:classic"
                in tool_learned_resp.headers.get("x-gateway-mode-decision", "")
            )

            routing_models = await gw.get(
                "/admin/routing/models",
                headers={"Authorization": f"Bearer {adm_tok}"},
            )
            assert routing_models.status_code == 200
            learned_map = routing_models.json()["learned_from_runs"]
            assert learned_map["gemini-2.5-flash"]["score_delta"] > 0
            assert learned_map["qwen/qwen3.5-9b"]["score_delta"] < 0

            unauth_mcp = await gw.post(
                "/mcp",
                json={"jsonrpc": "2.0", "id": 101, "method": "tools/list", "params": {}},
            )
            assert unauth_mcp.status_code == 401
            assert "Remediation for agent/user:" in unauth_mcp.json()["detail"]
            assert "/.well-known/oauth-protected-resource" in unauth_mcp.json()["detail"]
    finally:
        await rt.shutdown()
        await http_client.aclose()


async def test_ui_governance_infra_dockerfile_ci_and_spa_fallback(tmp_path: Path) -> None:
    for doc in ("DESIGN.md", "AGENTS.md", "web/AGENTS.md", ".agents/rules/ui-craft.md"):
        assert Path(doc).is_file(), f"Missing governance doc: {doc}"

    nginx_conf = Path("deploy/nginx/nginx.conf").read_text(encoding="utf-8")
    assert "${GATEWAY_UPSTREAM_HOST}:${GATEWAY_UPSTREAM_PORT}" in nginx_conf
    assert "X-MCP-Route-Mode" in nginx_conf
    assert "proxy_buffering off;" in nginx_conf

    compose = Path("docker-compose.yml").read_text(encoding="utf-8")
    assert "nginx:1.27-alpine" in compose
    assert "lb:" in compose

    for wf_path, needle in (
        (".github/workflows/backend-ci.yml", "agent_preflight.py"),
        (".github/workflows/backend-ci.yml", "ruff format --check"),
        (".github/workflows/frontend-ci.yml", "npm run generate"),
        (".github/workflows/qa-ci.yml", "--verify-thresholds"),
        (".github/workflows/security-ci.yml", "codeql-action"),
        (".github/workflows/security-ci.yml", "gitleaks"),
        (".github/dependabot.yml", "github-actions"),
    ):
        assert needle in Path(wf_path).read_text(encoding="utf-8")

    for doc in (
        "CLAUDE.md",
        "tests/golden/flows.json",
        "benchmarks/thresholds.json",
        "scripts/agent_preflight.py",
        "scripts/pre-ci.sh",
    ):
        assert Path(doc).is_file(), f"Missing Phase 12 QA/governance file: {doc}"

    # Barebones production Docker target must contain zero Node, SPA, or examples.
    dockerfile = Path("Dockerfile").read_text(encoding="utf-8")
    assert "FROM base-runtime AS gateway" in dockerfile
    gw_block = dockerfile.split("FROM base-runtime AS gateway")[1].split("FROM ")[0]
    assert "web-builder" not in gw_block
    assert "nodejs" not in gw_block
    assert "examples" not in gw_block

    # Legacy static HTML dashboard is removed cleanly.
    assert not Path("src/gateway/static/dashboard.html").exists()

    # SPA /dashboard remains reachable, and Demo Traffic + Self-Admin MCP work in demo mode.
    dev_keys = LocalDevKeyManager()
    settings = GatewaySettings(
        auth_mode="dev",
        profile="demo",
        sqlite_path=str(tmp_path / "dash.db"),
        upstreams_config_path=str(tmp_path / "none.yaml"),
    )
    app = create_gateway_app(settings=settings, dev_keys=dev_keys, preloaded_tools={})
    rt = app.state.runtime
    await rt.startup()
    try:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://gw.local"
        ) as gw:
            main_dash = await gw.get("/dashboard")
            assert main_dash.status_code == 200
            assert "MCPcodeaway" in main_dash.text

            adm_tok = dev_keys.issue_user_token(persona="gateway_admin")
            fin_tok = dev_keys.issue_user_token(persona="finance_analyst")

            # Self-Admin MCP rejects non-admin callers and serves tools to gateway_admin.
            denied_mcp = await gw.post(
                "/admin/mcp",
                headers={"Authorization": f"Bearer {fin_tok}"},
                json={"jsonrpc": "2.0", "id": 1, "method": "tools/list", "params": {}},
            )
            assert denied_mcp.status_code == 403

            listed_mcp = await gw.post(
                "/admin/mcp",
                headers={"Authorization": f"Bearer {adm_tok}"},
                json={"jsonrpc": "2.0", "id": 1, "method": "tools/list", "params": {}},
            )
            assert listed_mcp.status_code == 200
            tool_names = {t["name"] for t in listed_mcp.json()["result"]["tools"]}
            assert {"admin_list_upstreams", "admin_save_upstream", "admin_demo_traffic_burst"} <= (
                tool_names
            )

            burst = await gw.post(
                "/admin/demo/traffic/burst?count=2",
                headers={"Authorization": f"Bearer {adm_tok}"},
            )
            assert burst.status_code == 200
            assert burst.json()["sent"] == 2

            # Custom model pricing CRUD + analytics model selector merge
            saved_p = await gw.post(
                "/admin/pricing/models",
                headers={"Authorization": f"Bearer {adm_tok}"},
                json={
                    "model_name": "custom-haiku",
                    "input_per_1m_usd": 0.8,
                    "output_per_1m_usd": 4.0,
                },
            )
            assert saved_p.status_code == 200
            assert "custom-haiku" in saved_p.json()["models"]
            listed_p = await gw.get(
                "/admin/pricing/models",
                headers={"Authorization": f"Bearer {adm_tok}"},
            )
            assert listed_p.status_code == 200
            assert "qwen3.5-9b" in listed_p.json()["models"]
            ana_p = await gw.get(
                "/audit/analytics?model=custom-haiku",
                headers={"Authorization": f"Bearer {adm_tok}"},
            )
            assert ana_p.status_code == 200
            assert ana_p.json()["pricing"]["model"] == "custom-haiku"
            assert ana_p.json()["pricing"]["input_per_1m_usd"] == 0.8
            del_p = await gw.delete(
                "/admin/pricing/models/custom-haiku",
                headers={"Authorization": f"Bearer {adm_tok}"},
            )
            assert del_p.status_code == 200
            assert del_p.json()["deleted"] is True

            # Tiered demo traffic start/tier/stop
            t_start = await gw.post(
                "/admin/demo/traffic/start?tier=stress",
                headers={"Authorization": f"Bearer {adm_tok}"},
            )
            assert t_start.status_code == 200
            assert t_start.json()["tier"] == "stress"
            assert t_start.json()["running"] is True
            t_swap = await gw.post(
                "/admin/demo/traffic/tier?tier=light",
                headers={"Authorization": f"Bearer {adm_tok}"},
            )
            assert t_swap.status_code == 200
            assert t_swap.json()["tier"] == "light"
            t_stop = await gw.post(
                "/admin/demo/traffic/stop",
                headers={"Authorization": f"Bearer {adm_tok}"},
            )
            assert t_stop.status_code == 200
            assert t_stop.json()["running"] is False

            # Alerts rules + evaluation, roles catalog, and paginated audit search
            roles_r = await gw.get("/admin/roles", headers={"Authorization": f"Bearer {adm_tok}"})
            assert roles_r.status_code == 200
            assert "finance_analyst" in roles_r.json()["roles"]
            assert "engineering" in roles_r.json()["groups"]

            rules_save = await gw.post(
                "/admin/alerts/rules",
                headers={"Authorization": f"Bearer {adm_tok}"},
                json={
                    "error_rate_pct_max": 10.0,
                    "p95_latency_ms_max": 500.0,
                    "grounding_blocks_max": 1,
                },
            )
            assert rules_save.status_code == 200
            alerts_r = await gw.get("/admin/alerts", headers={"Authorization": f"Bearer {adm_tok}"})
            assert alerts_r.status_code == 200
            assert "alerts" in alerts_r.json()

            paged_r = await gw.get(
                "/audit/runs?limit=1&offset=0&status=all",
                headers={"Authorization": f"Bearer {adm_tok}"},
            )
            assert paged_r.status_code == 200
            assert paged_r.json()["total_count"] >= 1
            assert len(paged_r.json()["runs"]) == 1
            err_r = await gw.get(
                "/audit/runs?limit=10&offset=0&status=errors",
                headers={"Authorization": f"Bearer {adm_tok}"},
            )
            assert err_r.status_code == 200
            assert err_r.json()["total_count"] >= 1
    finally:
        await rt.shutdown()

    assert sys.version_info >= (3, 12)


def test_live_websocket_telemetry_stream_and_summary_only_payload(tmp_path: Path) -> None:
    from starlette.testclient import TestClient

    dev_keys = LocalDevKeyManager()
    settings = GatewaySettings(
        auth_mode="dev",
        profile="demo",
        sqlite_path=str(tmp_path / "ws.db"),
        upstreams_config_path=str(tmp_path / "none.yaml"),
    )
    app = create_gateway_app(settings=settings, dev_keys=dev_keys, preloaded_tools={})
    adm_tok = dev_keys.issue_user_token(persona="gateway_admin")
    with TestClient(app) as client:
        with client.websocket_connect(f"/admin/ws/telemetry?token={adm_tok}") as ws:
            snap = ws.receive_json()
            assert snap["type"] == "snapshot"
            assert "gateway" in snap["runtime"]
            burst_r = client.post(
                "/admin/demo/traffic/burst?count=1",
                headers={"Authorization": f"Bearer {adm_tok}"},
            )
            assert burst_r.status_code == 200
            evt = ws.receive_json()
            assert evt["type"] == "run_recorded"
            assert "run_id" in evt
            assert "raw_payload" not in evt
            assert "code" not in evt
