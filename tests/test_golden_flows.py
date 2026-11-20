"""Deterministic Golden Dataset runner executing real flows through MCPcodeaway."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any

import httpx
import pytest
from fastapi import FastAPI, Header, HTTPException

from examples.mock_backends import create_mock_backends_app
from gateway.auth import LocalDevKeyManager
from gateway.config import GatewaySettings
from gateway.dispatcher import verify_provenance_envelope
from gateway.mcp_protocol import request_headers, request_meta
from gateway.server import create_gateway_app
from gateway.state import SQLiteAuditStore

_GOLDEN_PATH = Path(__file__).parent / "golden" / "flows.json"
_GOLDEN_SPEC = json.loads(_GOLDEN_PATH.read_text(encoding="utf-8"))


async def _mcp_call(
    client: httpx.AsyncClient,
    token: str,
    method: str,
    params: dict[str, Any] | None = None,
    *,
    query: str = "",
    headers: dict[str, str] | None = None,
) -> httpx.Response:
    url = f"/mcp?{query}" if query else "/mcp"
    req_headers = {
        "Authorization": f"Bearer {token}",
        **request_headers(method, tool_name=(params or {}).get("name")),
        **(headers or {}),
    }
    if query:
        for part in query.split("&"):
            if part.startswith("mode="):
                req_headers.setdefault("X-Admin-Force-Mode", part.split("=", 1)[1])
    return await client.post(
        url,
        headers=req_headers,
        json={
            "jsonrpc": "2.0",
            "id": 1,
            "method": method,
            "params": {"_meta": request_meta(), **(params or {})},
        },
    )


def _build_legacy_sloppy_backend(base_backends: FastAPI) -> FastAPI:
    legacy_app = FastAPI()

    @legacy_app.post("/mcp")
    async def legacy_mcp(
        request: dict[str, Any], x_legacy_key: str | None = Header(default=None)
    ) -> dict[str, Any]:
        if x_legacy_key != "golden-legacy-key":
            raise HTTPException(status_code=401, detail="Invalid legacy API key")
        if request.get("method") == "server/discover":
            return {
                "jsonrpc": "2.0",
                "id": request["id"],
                "result": {"supportedVersions": ["2026-07-28"]},
            }
        if request.get("method") == "tools/list":
            return {
                "jsonrpc": "2.0",
                "id": request["id"],
                "result": {
                    "tools": [
                        {
                            "name": "list_accounts",
                            "description": "List client accounts with region filter.",
                            "inputSchema": {
                                "type": "object",
                                "properties": {"region": {"type": "string"}},
                            },
                        }
                    ]
                },
            }
        res = {
            "count": 2,
            "rows": [
                {
                    "customer_id": "CUST-1003",
                    "region": "EU",
                    "contact_email": "alice@merchant3.example",
                    "api_token": "sk-secret-value-leak-12345",
                },
                {
                    "customer_id": "CUST-1024",
                    "region": "US",
                    "contact_email": "bob@merchant24.example",
                    "api_token": "sk-secret-value-leak-67890",
                },
            ],
        }
        return {
            "jsonrpc": "2.0",
            "id": request["id"],
            "result": {"content": [{"type": "text", "text": json.dumps(res)}]},
        }

    combined = FastAPI()
    combined.mount("/legacy", legacy_app)
    combined.mount("/", base_backends)
    return combined


@pytest.mark.parametrize("flow", _GOLDEN_SPEC["flows"], ids=lambda f: f["id"])
async def test_golden_end_to_end_flows(
    flow: dict[str, Any], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    kind = flow["kind"]

    if kind == "profile_gate":
        settings = GatewaySettings(
            profile=flow["profile"],
            # This flow verifies profile feature gates, not production secret
            # provisioning. Keep the production feature profile while using the
            # local verifier for its authorized metrics request.
            app_env="sandbox",
            auth_mode="dev",
            sqlite_path=str(tmp_path / f"{flow['id']}.db"),
            upstreams_config_path=str(tmp_path / "missing.yaml"),
        )
        dev_keys = LocalDevKeyManager(issuer=settings.oidc_issuer)
        app = create_gateway_app(settings=settings, dev_keys=dev_keys, preloaded_tools={})
        async with app.router.lifespan_context(app):
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app), base_url="http://gw"
            ) as gw:
                for p in flow["open_paths"]:
                    headers = {}
                    if p == "/metrics":
                        token = dev_keys.issue_user_token(
                            persona="gateway_admin", audience=settings.oidc_audience
                        )
                        headers["Authorization"] = f"Bearer {token}"
                    assert (await gw.get(p, headers=headers)).status_code == 200
                for blocked in flow["blocked_gets"]:
                    assert (await gw.get(blocked)).status_code == 404
        return

    monkeypatch.setenv("LEGACY_GOLDEN_KEY", "golden-legacy-key")
    settings = GatewaySettings(
        auth_mode="dev",
        profile="demo",
        app_env="sandbox",
        require_admin_auth=True,
        provenance_signing_secret="golden-prov-secret",
        sqlite_path=str(tmp_path / f"{flow['id']}.db"),
    )
    dev_keys = LocalDevKeyManager(issuer=settings.oidc_issuer)
    backends_app = create_mock_backends_app(
        public_key_pem=dev_keys.public_pem, expected_issuer=settings.oidc_issuer
    )
    combined = _build_legacy_sloppy_backend(backends_app)
    backend_client = httpx.AsyncClient(
        transport=httpx.ASGITransport(app=combined), base_url="http://127.0.0.1:8001"
    )
    app = create_gateway_app(
        settings=settings,
        store=SQLiteAuditStore(settings.sqlite_path),
        dev_keys=dev_keys,
        http_client=backend_client,
    )

    try:
        async with app.router.lifespan_context(app):
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app), base_url="http://gw"
            ) as gw:
                if kind == "mcp_tools_list":
                    tok = dev_keys.issue_user_token(persona=flow["persona"])
                    resp = await _mcp_call(gw, tok, "tools/list", query=flow["query"])
                    assert resp.status_code == 200
                    names = [t["name"] for t in resp.json()["result"]["tools"]]
                    for pref in flow["expected_present_prefixes"]:
                        assert any(n.startswith(pref) for n in names)
                    for pref in flow["expected_absent_prefixes"]:
                        assert all(not n.startswith(pref) for n in names)

                elif kind == "obo_rls_matrix":
                    eu_tok = dev_keys.issue_user_token(persona=flow["eu_persona"])
                    us_tok = dev_keys.issue_user_token(persona=flow["us_persona"])
                    eu_code = (
                        "res = await snowflake_ledger__query_debt_ledger(status='overdue')\n"
                        "res['rows']"
                    )
                    eu_res = (
                        await _mcp_call(
                            gw,
                            eu_tok,
                            "tools/call",
                            {"name": "execute_code", "arguments": {"code": eu_code}},
                            query="mode=codemode",
                        )
                    ).json()["result"]["structuredContent"]
                    us_res = (
                        await _mcp_call(
                            gw,
                            us_tok,
                            "tools/call",
                            {"name": "execute_code", "arguments": {"code": eu_code}},
                            query="mode=codemode",
                        )
                    ).json()["result"]["structuredContent"]
                    assert all(r["region"] == flow["expected_eu_region"] for r in eu_res["result"])
                    assert any(
                        r["customer_id"] == flow["expected_eu_customer"] for r in eu_res["result"]
                    )
                    assert all(r["region"] == flow["expected_us_region"] for r in us_res["result"])
                    assert any(
                        r["customer_id"] == flow["expected_us_customer"] for r in us_res["result"]
                    )

                elif kind == "mcp_codemode_exec":
                    tok = dev_keys.issue_user_token(persona=flow["persona"])
                    sc = (
                        await _mcp_call(
                            gw,
                            tok,
                            "tools/call",
                            {"name": "execute_code", "arguments": {"code": flow["code"]}},
                            query=flow["query"],
                        )
                    ).json()["result"]["structuredContent"]
                    assert sc["ok"] is flow["expected_ok"]
                    if flow["expected_ok"]:
                        assert sc["_provenance"]["grounded"] is flow["expected_grounded"]
                        assert verify_provenance_envelope(sc["_provenance"], "golden-prov-secret")
                        got_ids = {r["customer_id"] for r in sc["result"]}
                        assert set(flow["expected_customer_ids"]) <= got_ids
                        if flow.get("expect_compression"):
                            assert sc["returned_bytes"] < sc["raw_upstream_bytes"]
                    else:
                        assert sc["error_type"] == flow["expected_error_type"]
                        assert flow["expected_error_substring"] in sc["error"]

                elif kind == "legacy_abac_and_pii":
                    adm_tok = dev_keys.issue_user_token(persona="gateway_admin")
                    reg = await gw.post(
                        "/admin/upstreams",
                        headers={"Authorization": f"Bearer {adm_tok}"},
                        json={
                            "name": "legacy_abac_svc",
                            "type": "mcp_http",
                            "url": "http://127.0.0.1:8001/legacy/mcp",
                            "auth_type": "api_key",
                            "secret_env_var": "LEGACY_GOLDEN_KEY",
                            "api_key_header": "X-Legacy-Key",
                            "allowed_roles": ["finance_analyst"],
                            "abac_rules": [
                                {
                                    "claim": "client_groups",
                                    "arg_name": "region",
                                    "response_list_key": "rows",
                                    "row_field": "region",
                                }
                            ],
                        },
                    )
                    assert reg.status_code == 200, reg.text
                    eu_tok = dev_keys.issue_user_token(persona=flow["persona"])
                    res = (
                        await _mcp_call(
                            gw,
                            eu_tok,
                            "tools/call",
                            {
                                "name": "execute_code",
                                "arguments": {"code": "await legacy_abac_svc__list_accounts()"},
                            },
                            query="mode=codemode",
                        )
                    ).json()["result"]["structuredContent"]
                    assert res["ok"] is True
                    cids = [r["customer_id"] for r in res["result"]["rows"]]
                    assert flow["expected_allowed_customer"] in cids
                    assert flow["expected_blocked_customer"] not in cids
                    serialized = json.dumps(res["result"])
                    assert flow["expected_redacted_marker"] in serialized
                    assert "sk-secret-value-leak" not in serialized

                elif kind == "mcp_auto_route":
                    tok = dev_keys.issue_user_token(persona=flow["persona"])
                    resp = await _mcp_call(
                        gw,
                        tok,
                        "tools/list",
                        query=flow["query"],
                        headers=flow.get("headers"),
                    )
                    assert resp.status_code == 200
                    assert flow["expected_header_prefix"] in resp.headers.get(
                        "x-gateway-mode-decision", ""
                    )
                    names = {t["name"] for t in resp.json()["result"]["tools"]}
                    assert flow["expected_tool_present"] in names

                elif kind == "control_plane_rbac":
                    adm_tok = dev_keys.issue_user_token(persona=flow["admin_persona"])
                    usr_tok = dev_keys.issue_user_token(persona=flow["non_admin_persona"])
                    assert (await gw.get("/admin/upstreams")).status_code == 401
                    assert (
                        await gw.get(
                            "/admin/upstreams", headers={"Authorization": f"Bearer {usr_tok}"}
                        )
                    ).status_code == 403
                    assert (
                        await gw.get(
                            f"/audit/analytics?tenant_id={flow['foreign_tenant']}",
                            headers={"Authorization": f"Bearer {usr_tok}"},
                        )
                    ).status_code == 403
                    for ep in ("/admin/upstreams", "/admin/telemetry/runtime", "/audit/analytics"):
                        ok_r = await gw.get(ep, headers={"Authorization": f"Bearer {adm_tok}"})
                        assert ok_r.status_code == 200, (ep, ok_r.text)
                else:
                    raise AssertionError(f"Unsupported golden flow kind: {kind}")
    finally:
        await backend_client.aclose()


async def test_startup_discovery_recovers_after_transient_failure(tmp_path: Path) -> None:
    """Gateway startup schedules background retries and clears resolved startup_warnings."""
    attempts = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            return httpx.Response(500, json={"error": "jwks not ready yet"})
        if json.loads(request.content).get("method") == "server/discover":
            return httpx.Response(
                200,
                json={
                    "jsonrpc": "2.0",
                    "id": 1,
                    "result": {"supportedVersions": ["2026-07-28"]},
                },
            )
        return httpx.Response(
            200,
            json={
                "jsonrpc": "2.0",
                "id": 1,
                "result": {
                    "tools": [
                        {
                            "name": "ping",
                            "description": "Ping service",
                            "inputSchema": {"type": "object", "properties": {}},
                        }
                    ]
                },
            },
        )

    cfg_path = tmp_path / "upstreams.yaml"
    cfg_path.write_text(
        json.dumps(
            {
                "upstreams": [
                    {
                        "name": "transient_svc",
                        "type": "mcp_http",
                        "url": "http://transient.local/mcp",
                        "auth_type": "none",
                        "allowed_roles": ["finance_analyst", "admin"],
                        "allowed_groups": ["finance", "platform"],
                        "allowed_scopes": ["mcp.tools"],
                        "enabled": True,
                    }
                ]
            }
        ),
        encoding="utf-8",
    )

    mock_client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    settings = GatewaySettings(
        auth_mode="dev",
        profile="demo",
        sqlite_path=str(tmp_path / "retry.db"),
        upstreams_config_path=str(cfg_path),
    )
    app = create_gateway_app(settings=settings, http_client=mock_client)
    rt = app.state.runtime
    await rt.startup()
    try:
        assert len(rt.startup_warnings) == 1
        assert "transient_svc" in rt.startup_warnings[0]
        assert rt._startup_retry_task is not None

        for _ in range(40):
            if not rt.startup_warnings:
                break
            await asyncio.sleep(0.05)

        assert rt.startup_warnings == []
        assert rt.catalog.get_tool("transient_svc__ping") is not None
    finally:
        await rt.shutdown()
        assert rt._startup_retry_task is None
        await mock_client.aclose()
