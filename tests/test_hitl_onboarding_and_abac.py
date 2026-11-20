"""Phase 8 tests: HITL onboarding wizard, agent prompt generator, and row-level ABAC/RLS."""

from __future__ import annotations

import json
from typing import Any

import httpx
from fastapi import FastAPI, Header, HTTPException

from examples.mock_backends import create_mock_backends_app
from gateway.auth import LocalDevKeyManager
from gateway.config import GatewaySettings
from gateway.mcp_protocol import request_headers, request_meta
from gateway.server import GatewayRuntime, create_gateway_app
from gateway.state import SQLiteAuditStore


async def _post_mcp(
    client: httpx.AsyncClient,
    token: str,
    method: str,
    params: dict[str, Any] | None = None,
    *,
    query: str = "",
) -> dict[str, Any]:
    url = f"/mcp?{query}" if query else "/mcp"
    resp = await client.post(
        url,
        headers={
            "Authorization": f"Bearer {token}",
            **request_headers(method, tool_name=(params or {}).get("name")),
        },
        json={
            "jsonrpc": "2.0",
            "id": 1,
            "method": method,
            "params": {"_meta": request_meta(), **(params or {})},
        },
    )
    assert resp.status_code == 200, resp.text
    return resp.json()


async def test_hitl_onboarding_guidance_curation_and_agent_prompt(tmp_path) -> None:
    settings = GatewaySettings(
        app_env="sandbox",
        auth_mode="dev",
        sqlite_path=str(tmp_path / "hitl.db"),
    )
    dev_keys = LocalDevKeyManager(issuer=settings.oidc_issuer)
    backends_app = create_mock_backends_app(
        public_key_pem=dev_keys.public_pem, expected_issuer=settings.oidc_issuer
    )
    stub_upstream = FastAPI()

    @stub_upstream.get("/health")
    async def stub_health():
        return {"ok": True}

    @stub_upstream.get("/orders")
    async def stub_orders():
        return {"orders": []}

    combined = FastAPI()
    combined.mount("/curated", stub_upstream)
    combined.mount("/", backends_app)

    backend_client = httpx.AsyncClient(
        transport=httpx.ASGITransport(app=combined), base_url="http://127.0.0.1:8001"
    )
    app = create_gateway_app(
        settings=settings,
        store=SQLiteAuditStore(settings.sqlite_path),
        dev_keys=dev_keys,
        http_client=backend_client,
    )
    fin_token = dev_keys.issue_user_token(persona="finance_analyst")

    openapi_spec = {
        "openapi": "3.1.0",
        "info": {"title": "Curated Orders API", "version": "1.0.0"},
        "components": {
            "securitySchemes": {
                "CorpOAuth": {
                    "type": "oauth2",
                    "flows": {
                        "authorizationCode": {
                            "authorizationUrl": "https://idp.example/auth",
                            "tokenUrl": "https://idp.example/token",
                            "scopes": {"orders.read": "Read orders"},
                        }
                    },
                },
                "LegacyKey": {"type": "apiKey", "in": "header", "name": "X-Legacy-Key"},
            }
        },
        "paths": {
            "/health": {
                "get": {
                    "operationId": "get_health",
                    "summary": "Health check",
                    "responses": {
                        "200": {
                            "description": "OK",
                            "content": {
                                "application/json": {
                                    "schema": {
                                        "type": "object",
                                        "properties": {"ok": {"type": "boolean"}},
                                    }
                                }
                            },
                        }
                    },
                }
            },
            "/orders": {
                "get": {
                    "operationId": "list_orders",
                    "summary": "List orders (missing output schema)",
                }
            },
            "/orders/{order_id}": {
                "delete": {
                    "operationId": "delete_order",
                    "summary": "Delete an order",
                    "parameters": [
                        {
                            "name": "order_id",
                            "in": "path",
                            "required": True,
                            "schema": {"type": "string"},
                        }
                    ],
                }
            },
        },
    }

    try:
        async with app.router.lifespan_context(app):
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app), base_url="http://gw"
            ) as gw:
                # 1. Generic AI onboarding prompt + UpstreamConfig schema endpoint
                prompt_resp = await gw.get("/admin/upstreams/agent-prompt")
                assert prompt_resp.status_code == 200
                prompt_data = prompt_resp.json()
                assert "delegated_token" in prompt_data["prompt"]
                assert "abac_rules" in prompt_data["prompt"]
                assert "abac_rules" in prompt_data["schema"]["properties"]
                assert "allowed_operations" in prompt_data["schema"]["properties"]

                # 2. Inspect OpenAPI spec detects securitySchemes + missing outputSchema + writes
                insp = await gw.post(
                    "/admin/upstreams/inspect",
                    json={
                        "name": "curated_api",
                        "type": "openapi",
                        "url": "http://127.0.0.1:8001/curated",
                        "openapi_spec": openapi_spec,
                        "auth_type": "none",
                    },
                )
                assert insp.status_code == 200, insp.text
                inspection = insp.json()["inspection"]
                assert inspection["total_discovered_count"] == 3
                guidance = inspection["onboarding_guidance"]
                targets = {(c["target"], c["field"]) for c in guidance["checklist"]}
                assert ("corporate_idp", "downstream_audience") in targets
                assert ("gateway", "secret_env_var") in targets
                assert ("target_backend", "outputSchema") in targets
                assert ("gateway", "allowed_operations") in targets
                assert "list_orders" in guidance["missing_output_schema_tools"]
                assert "CorpOAuth" in guidance["agent_onboarding_prompt"]

                # 3. Register with HITL allowed_operations curation excluding delete & list_orders
                reg = await gw.post(
                    "/admin/upstreams",
                    json={
                        "name": "curated_api",
                        "type": "openapi",
                        "url": "http://127.0.0.1:8001/curated",
                        "openapi_spec": openapi_spec,
                        "auth_type": "none",
                        "allowed_roles": ["finance_analyst"],
                        "allowed_operations": ["get_health"],
                    },
                )
                assert reg.status_code == 200, reg.text
                assert reg.json()["inspection"]["selected_operations"] == ["get_health"]

                listed = await _post_mcp(gw, fin_token, "tools/list", query="mode=traditional")
                tool_names = {t["name"] for t in listed["result"]["tools"]}
                assert "curated_api__get_health" in tool_names
                assert "curated_api__delete_order" not in tool_names
                assert "curated_api__list_orders" not in tool_names

                dash = await gw.get("/dashboard")
                assert dash.status_code == 200
                assert "MCPcodeaway" in dash.text
                assert "/admin/mcp" in prompt_data["prompt"]
                assert "WebMCP" in prompt_data["prompt"]
    finally:
        await backend_client.aclose()


async def test_downstream_rls_via_obo_client_groups_in_codemode(tmp_path) -> None:
    settings = GatewaySettings(auth_mode="dev", sqlite_path=str(tmp_path / "rls.db"))
    dev_keys = LocalDevKeyManager(issuer=settings.oidc_issuer)
    backends_app = create_mock_backends_app(
        public_key_pem=dev_keys.public_pem, expected_issuer=settings.oidc_issuer
    )
    backend_client = httpx.AsyncClient(
        transport=httpx.ASGITransport(app=backends_app), base_url="http://127.0.0.1:8001"
    )
    app = create_gateway_app(
        settings=settings,
        store=SQLiteAuditStore(settings.sqlite_path),
        dev_keys=dev_keys,
        http_client=backend_client,
    )
    eu_token = dev_keys.issue_user_token(persona="eu_accountant")
    us_token = dev_keys.issue_user_token(persona="us_accountant")

    try:
        async with app.router.lifespan_context(app):
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app), base_url="http://gw"
            ) as gw:
                # 1. eu_accountant querying unfiltered Snowflake + EU invoices gets ONLY EU rows
                eu_exec = await _post_mcp(
                    gw,
                    eu_token,
                    "tools/call",
                    {
                        "name": "execute_code",
                        "arguments": {
                            "code": (
                                "ledger = await snowflake_ledger__query_debt_ledger("
                                "status='overdue')\n"
                                "eu_inv = await billing_legacy_eu__list_eu_invoices("
                                "status='overdue')\n"
                                "regs = sorted(list({r['region'] for r in ledger['rows']}))\n"
                                "cids = [r['customer_id'] for r in ledger['rows']]\n"
                                "{\n"
                                "  'regions': regs,\n"
                                "  'cust_ids': cids,\n"
                                "  'eu_count': eu_inv['count'],\n"
                                "}"
                            )
                        },
                    },
                    query="mode=codemode",
                )
                eu_sc = eu_exec["result"]["structuredContent"]
                assert eu_sc["ok"] is True
                assert eu_sc["result"]["regions"] == ["EU"]
                assert "CUST-1003" in eu_sc["result"]["cust_ids"]
                assert "CUST-1024" not in eu_sc["result"]["cust_ids"]

                # 2. eu_accountant trying cross-group Snowflake point lookup or US billing fails 403
                eu_cross = await _post_mcp(
                    gw,
                    eu_token,
                    "tools/call",
                    {
                        "name": "execute_code",
                        "arguments": {
                            "code": (
                                "await snowflake_ledger__get_customer_balance("
                                "customer_id='CUST-1024')"
                            )
                        },
                    },
                    query="mode=codemode",
                )
                assert eu_cross["result"]["structuredContent"]["ok"] is False
                assert "403" in eu_cross["result"]["structuredContent"]["error"]

                eu_us_bill = await _post_mcp(
                    gw,
                    eu_token,
                    "tools/call",
                    {
                        "name": "execute_code",
                        "arguments": {
                            "code": "await billing_legacy_us__list_us_invoices(status='overdue')"
                        },
                    },
                    query="mode=codemode",
                )
                assert eu_us_bill["result"]["structuredContent"]["ok"] is False
                assert "403" in eu_us_bill["result"]["structuredContent"]["error"]

                # 3. us_accountant gets ONLY US rows via its own isolated OBO token exchange
                us_exec = await _post_mcp(
                    gw,
                    us_token,
                    "tools/call",
                    {
                        "name": "execute_code",
                        "arguments": {
                            "code": (
                                "ledger = await snowflake_ledger__query_debt_ledger("
                                "status='overdue')\n"
                                "us_inv = await billing_legacy_us__list_us_invoices("
                                "status='overdue')\n"
                                "regs = sorted(list({r['region'] for r in ledger['rows']}))\n"
                                "cids = [r['customer_id'] for r in ledger['rows']]\n"
                                "{\n"
                                "  'regions': regs,\n"
                                "  'cust_ids': cids,\n"
                                "  'us_count': us_inv['count'],\n"
                                "}"
                            )
                        },
                    },
                    query="mode=codemode",
                )
                us_sc = us_exec["result"]["structuredContent"]
                assert us_sc["ok"] is True
                assert us_sc["result"]["regions"] == ["US"]
                assert "CUST-1024" in us_sc["result"]["cust_ids"]
                assert "CUST-1003" not in us_sc["result"]["cust_ids"]
    finally:
        await backend_client.aclose()


async def test_host_side_abac_enforcement_for_api_key_upstream(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("LEGACY_SHARED_API_KEY", "legacy-secret-123")
    settings = GatewaySettings(
        app_env="sandbox",
        auth_mode="dev",
        sqlite_path=str(tmp_path / "abac.db"),
    )
    dev_keys = LocalDevKeyManager(issuer=settings.oidc_issuer)
    backends_app = create_mock_backends_app(
        public_key_pem=dev_keys.public_pem, expected_issuer=settings.oidc_issuer
    )

    legacy_calls: list[dict[str, Any]] = []
    legacy_app = FastAPI()

    @legacy_app.post("/mcp")
    async def legacy_mcp(
        request: dict[str, Any],
        x_legacy_key: str | None = Header(default=None),
    ):
        if x_legacy_key != "legacy-secret-123":
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
                            "name": "list_client_accounts",
                            "description": "List accounts with optional region parameter.",
                            "inputSchema": {
                                "type": "object",
                                "properties": {"region": {"type": "string"}},
                            },
                        },
                        {
                            "name": "get_account",
                            "description": "Fetch a single account record.",
                            "inputSchema": {
                                "type": "object",
                                "properties": {"customer_id": {"type": "string"}},
                                "required": ["customer_id"],
                            },
                        },
                    ]
                },
            }
        params = request.get("params") or {}
        name = params.get("name")
        args = params.get("arguments") or {}
        legacy_calls.append({"name": name, "args": args})
        if name == "list_client_accounts":
            # Simulates a sloppy legacy API returning mixed EU + US rows even when filtered
            res = {
                "count": 2,
                "rows": [
                    {"customer_id": "CUST-1003", "region": "EU", "balance": 1825.0},
                    {"customer_id": "CUST-1024", "region": "US", "balance": 3680.0},
                ],
            }
        else:
            res = {"customer_id": "CUST-1024", "region": "US", "balance": 3680.0}
        return {
            "jsonrpc": "2.0",
            "id": request["id"],
            "result": {"content": [{"type": "text", "text": json.dumps(res)}]},
        }

    combined = FastAPI()
    combined.mount("/legacy", legacy_app)
    combined.mount("/", backends_app)

    backend_client = httpx.AsyncClient(
        transport=httpx.ASGITransport(app=combined), base_url="http://127.0.0.1:8001"
    )
    app = create_gateway_app(
        settings=settings,
        store=SQLiteAuditStore(settings.sqlite_path),
        dev_keys=dev_keys,
        http_client=backend_client,
    )
    eu_token = dev_keys.issue_user_token(persona="eu_accountant")

    try:
        async with app.router.lifespan_context(app):
            rt: GatewayRuntime = app.state.runtime
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app), base_url="http://gw"
            ) as gw:
                reg = await gw.post(
                    "/admin/upstreams",
                    json={
                        "name": "legacy_erp",
                        "type": "mcp_http",
                        "url": "http://127.0.0.1:8001/legacy/mcp",
                        "auth_type": "api_key",
                        "secret_env_var": "LEGACY_SHARED_API_KEY",
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
                assert rt.catalog.get_tool("legacy_erp__list_client_accounts") is not None

                # 1. Pre-call ABAC blocks disallowed arg region='US' BEFORE touching upstream
                legacy_calls.clear()
                denied_arg = await _post_mcp(
                    gw,
                    eu_token,
                    "tools/call",
                    {
                        "name": "execute_code",
                        "arguments": {
                            "code": "await legacy_erp__list_client_accounts(region='US')"
                        },
                    },
                    query="mode=codemode",
                )
                assert denied_arg["result"]["structuredContent"]["ok"] is False
                assert "ABAC policy denied" in denied_arg["result"]["structuredContent"]["error"]
                assert len(legacy_calls) == 0

                # 2. Unfiltered call injects region='EU' & post-call strips leaked US row!
                legacy_calls.clear()
                filtered_res = await _post_mcp(
                    gw,
                    eu_token,
                    "tools/call",
                    {
                        "name": "execute_code",
                        "arguments": {"code": "await legacy_erp__list_client_accounts()"},
                    },
                    query="mode=codemode",
                )
                sc = filtered_res["result"]["structuredContent"]
                assert sc["ok"] is True
                assert legacy_calls == [{"name": "list_client_accounts", "args": {"region": "EU"}}]
                assert sc["result"]["count"] == 1
                assert [r["customer_id"] for r in sc["result"]["rows"]] == ["CUST-1003"]

                # 3. Single-record US lookup returned by upstream is blocked before entering Monty
                denied_row = await _post_mcp(
                    gw,
                    eu_token,
                    "tools/call",
                    {
                        "name": "execute_code",
                        "arguments": {
                            "code": "await legacy_erp__get_account(customer_id='CUST-1024')"
                        },
                    },
                    query="mode=codemode",
                )
                assert denied_row["result"]["structuredContent"]["ok"] is False
                assert (
                    "ABAC response policy denied"
                    in denied_row["result"]["structuredContent"]["error"]
                )
    finally:
        await backend_client.aclose()
