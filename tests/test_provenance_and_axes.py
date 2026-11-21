"""Phase 7 tests: Decoupled 2x2 axes, WS worker auth, Monty provenance & bulk-200 benchmark."""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import socket
from typing import Any

import httpx
import pytest
import uvicorn
from fastapi import FastAPI

from examples.mock_backends import create_mock_backends_app
from gateway.auth import LocalDevKeyManager
from gateway.catalog import render_python_stub, to_mcp_tool_definition
from gateway.comparator import run_side_by_side_comparison
from gateway.config import GatewaySettings
from gateway.mcp_protocol import request_headers, request_meta
from gateway.sandbox_worker import create_sandbox_worker_app
from gateway.server import GatewayRuntime, create_gateway_app
from gateway.state import SQLiteAuditStore


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


async def _post_mcp(
    client: httpx.AsyncClient,
    token: str,
    method: str,
    params: dict[str, Any] | None = None,
    *,
    query: str = "",
    headers: dict[str, str] | None = None,
    req_id: int = 1,
) -> dict[str, Any]:
    url = f"/mcp?{query}" if query else "/mcp"
    req_headers = {
        "Authorization": f"Bearer {token}",
        **request_headers(method, tool_name=(params or {}).get("name")),
    }
    if headers:
        req_headers.update(headers)
    if "X-Execution-Mode" in req_headers:
        exec_mode = req_headers["X-Execution-Mode"]
        req_headers.setdefault(
            "X-Admin-Force-Mode", "traditional" if exec_mode == "classic" else exec_mode
        )
    if query:
        for part in query.split("&"):
            if part.startswith(("mode=", "execution=")):
                req_headers.setdefault("X-Admin-Force-Mode", part.split("=", 1)[1])
    resp = await client.post(
        url,
        headers=req_headers,
        json={
            "jsonrpc": "2.0",
            "id": req_id,
            "method": method,
            "params": {"_meta": request_meta(), **(params or {})},
        },
    )
    assert resp.status_code == 200, resp.text
    return resp.json()


async def test_decoupled_execution_and_discovery_2x2_axes(tmp_path) -> None:
    settings = GatewaySettings(auth_mode="dev", sqlite_path=str(tmp_path / "axes.db"))
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
    fin_token = dev_keys.issue_user_token(persona="finance_analyst")
    ops_token = dev_keys.issue_user_token(persona="ops_engineer")

    try:
        async with app.router.lifespan_context(app):
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app), base_url="http://gw"
            ) as gw:
                up_cm = await _post_mcp(
                    gw,
                    fin_token,
                    "tools/list",
                    query="execution=codemode&discovery_mode=upfront",
                )
                up_tools = up_cm["result"]["tools"]
                assert [t["name"] for t in up_tools] == ["execute_code"]
                desc = up_tools[0]["description"]
                assert "snowflake_ledger__query_debt_ledger" in desc
                assert "billing_legacy_eu__list_eu_invoices" in desc
                assert "ops_ticketing__list_terminal_incidents" not in desc

                alias_cm = await _post_mcp(
                    gw, fin_token, "tools/list", query="mode=codemode_upfront"
                )
                assert [t["name"] for t in alias_cm["result"]["tools"]] == ["execute_code"]

                exec_res = await _post_mcp(
                    gw,
                    fin_token,
                    "tools/call",
                    {
                        "name": "execute_code",
                        "arguments": {
                            "code": (
                                "await snowflake_ledger__get_customer_balance("
                                "customer_id='CUST-1003')"
                            )
                        },
                    },
                    query="execution=codemode&discovery_mode=upfront",
                )
                assert exec_res["result"]["structuredContent"]["ok"] is True
                assert (
                    exec_res["result"]["structuredContent"]["result"]["customer_id"] == "CUST-1003"
                )

                def_cm = await _post_mcp(
                    gw,
                    fin_token,
                    "tools/list",
                    query="execution=codemode&discovery_mode=deferred",
                )
                assert [t["name"] for t in def_cm["result"]["tools"]] == [
                    "list_servers",
                    "search_tools",
                    "execute_code",
                ]

                cls_up = await _post_mcp(
                    gw,
                    fin_token,
                    "tools/list",
                    headers={"X-Execution-Mode": "classic", "X-Discovery-Mode": "upfront"},
                )
                cls_names = {t["name"] for t in cls_up["result"]["tools"]}
                assert "snowflake_ledger__query_debt_ledger" in cls_names
                assert "execute_code" not in cls_names

                cls_def = await _post_mcp(
                    gw,
                    fin_token,
                    "tools/list",
                    headers={"X-Execution-Mode": "classic", "X-Discovery-Mode": "deferred"},
                )
                assert [t["name"] for t in cls_def["result"]["tools"]] == [
                    "list_servers",
                    "search_tools",
                ]

                hyb_up = await _post_mcp(
                    gw,
                    ops_token,
                    "tools/list",
                    query="execution=hybrid&discovery_mode=upfront",
                )
                assert [t["name"] for t in hyb_up["result"]["tools"]] == [
                    "execute_code",
                    "ops_ticketing__create_collection_case",
                ]
                assert (
                    "ops_ticketing__list_terminal_incidents"
                    in hyb_up["result"]["tools"][0]["description"]
                )
    finally:
        await backend_client.aclose()


async def test_provenance_envelope_and_grounding_policy(tmp_path) -> None:
    signing_secret = "unit-test-prov-secret"
    settings = GatewaySettings(
        auth_mode="dev",
        sqlite_path=str(tmp_path / "prov.db"),
        provenance_signing_secret=signing_secret,
    )
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
    fin_token = dev_keys.issue_user_token(persona="finance_analyst")

    try:
        async with app.router.lifespan_context(app):
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app), base_url="http://gw"
            ) as gw:
                valid_code = (
                    "bal = await snowflake_ledger__get_customer_balance(customer_id='CUST-1003')\n"
                    "inv = await billing_legacy_eu__list_eu_invoices(status='overdue')\n"
                    "inv_amt = [i['amount'] for i in inv['invoices'] "
                    "if i['customer_id'] == 'CUST-1003'][0]\n"
                    "{'customer_id': bal['customer_id'], "
                    "'delta': bal['ledger_balance'] - inv_amt}"
                )
                ok_resp = await _post_mcp(
                    gw,
                    fin_token,
                    "tools/call",
                    {"name": "execute_code", "arguments": {"code": valid_code}},
                    headers={"X-Require-Grounding": "true"},
                )
                ok_sc = ok_resp["result"]["structuredContent"]
                assert ok_sc["ok"] is True
                assert ok_sc["result"] == {"customer_id": "CUST-1003", "delta": 450.0}
                prov = ok_sc["_provenance"]
                assert prov["enforced_by"] == "host_dispatcher"
                assert prov["grounded"] is True
                assert prov["unverified_identifiers"] == []
                assert prov["subcall_count"] == 2
                assert len(prov["tool_output_digests"]) == 2
                assert set(prov["tools_called"]) == {
                    "snowflake_ledger__get_customer_balance",
                    "billing_legacy_eu__list_eu_invoices",
                }
                check_body = {k: v for k, v in prov.items() if k != "provenance_hmac"}
                expected_hmac = hmac.new(
                    signing_secret.encode("utf-8"),
                    json.dumps(check_body, sort_keys=True, separators=(",", ":")).encode("utf-8"),
                    hashlib.sha256,
                ).hexdigest()
                assert hmac.compare_digest(prov["provenance_hmac"], expected_hmac)

                fab_resp = await _post_mcp(
                    gw,
                    fin_token,
                    "tools/call",
                    {
                        "name": "execute_code",
                        "arguments": {"code": "{'customer_id': 'CUST-1003', 'delta': 999}"},
                    },
                    query="require_grounding=true",
                )
                assert fab_resp["result"]["isError"] is True
                fab_sc = fab_resp["result"]["structuredContent"]
                assert fab_sc["ok"] is False
                assert fab_sc["error_type"] == "UngroundedResultError"
                assert fab_sc["_provenance"]["grounded"] is False

                spoof_code = (
                    "await snowflake_ledger__get_customer_balance(customer_id='CUST-1003')\n"
                    "{'customer_id': 'CUST-9999', 'delta': 450.0}"
                )
                spoof_resp = await _post_mcp(
                    gw,
                    fin_token,
                    "tools/call",
                    {"name": "execute_code", "arguments": {"code": spoof_code}},
                    headers={"X-Require-Grounding": "true"},
                )
                spoof_sc = spoof_resp["result"]["structuredContent"]
                assert spoof_sc["ok"] is False
                assert spoof_sc["error_type"] == "UnverifiedEntityError"
                assert spoof_sc["_provenance"]["unverified_identifiers"] == ["CUST-9999"]
    finally:
        await backend_client.aclose()


async def test_remote_worker_ws_auth_and_prod_fail_closed(tmp_path) -> None:
    port = _free_port()
    secret = "super-secret-ws-key"
    worker_app = create_sandbox_worker_app(min_processes=1, max_processes=2, worker_secret=secret)
    server = uvicorn.Server(
        uvicorn.Config(worker_app, host="127.0.0.1", port=port, log_level="error")
    )
    server_task = asyncio.create_task(server.serve())

    dev_keys = LocalDevKeyManager(issuer="http://127.0.0.1:8000/dev/oidc")
    backends_app = create_mock_backends_app(
        public_key_pem=dev_keys.public_pem, expected_issuer="http://127.0.0.1:8000/dev/oidc"
    )
    backend_client = httpx.AsyncClient(
        transport=httpx.ASGITransport(app=backends_app), base_url="http://127.0.0.1:8001"
    )

    try:
        async with httpx.AsyncClient(timeout=2.0) as probe:
            for _ in range(40):
                try:
                    if (await probe.get(f"http://127.0.0.1:{port}/health")).status_code == 200:
                        break
                except httpx.HTTPError:
                    await asyncio.sleep(0.05)

        with pytest.raises(ValueError, match="auth_mode must be 'oidc'"):
            GatewayRuntime(
                settings=GatewaySettings(
                    app_env="prod",
                    auth_mode="dev",
                    sandbox_backend="remote",
                    sandbox_worker_url=f"ws://127.0.0.1:{port}/ws/execute",
                    sandbox_worker_secret="",
                    sqlite_path=str(tmp_path / "prod.db"),
                )
            )

        production_settings = GatewaySettings(
            app_env="prod",
            auth_mode="oidc",
            token_exchange_dialect="rfc8693",
            allowed_tenants=["tenant-prod"],
            postgres_dsn="postgresql://gateway:secret@postgres.example.test/mcpcodeaway",
            redis_url="redis://redis.example.test:6379/0",
            oidc_issuer="https://idp.example.test/tenant",
            oidc_audience="api://gateway-prod",
            oidc_jwks_uri="https://idp.example.test/keys",
            oidc_token_endpoint="https://idp.example.test/token",
            oidc_client_id="gateway-prod-client",
            oidc_client_secret="production-oidc-client-secret",
            allowed_upstream_hosts=["idp.example.test"],
            gateway_public_url="https://gateway.example.test",
            admin_oidc_authorization_endpoint="https://idp.example.test/authorize",
            admin_oidc_redirect_uri="https://gateway.example.test/auth/admin/callback",
            admin_oidc_api_scope="api://gateway-prod/gateway.access",
            provenance_signing_secret="p" * 40,
            admin_session_secret="a" * 40,
            sandbox_backend="remote",
            sandbox_worker_url=f"ws://127.0.0.1:{port}/ws/execute",
            sandbox_worker_secret="",
        )
        with pytest.raises(ValueError, match="sandbox_worker_secret"):
            GatewayRuntime(settings=production_settings)

        bad_app = create_gateway_app(
            settings=GatewaySettings(
                auth_mode="dev",
                sandbox_backend="remote",
                sandbox_worker_url=f"ws://127.0.0.1:{port}/ws/execute",
                sandbox_worker_secret="wrong-secret",
                sqlite_path=str(tmp_path / "bad.db"),
            ),
            dev_keys=dev_keys,
            http_client=backend_client,
        )
        fin_token = dev_keys.issue_user_token(persona="finance_analyst")
        async with bad_app.router.lifespan_context(bad_app):
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=bad_app), base_url="http://gw"
            ) as gw:
                denied = await _post_mcp(
                    gw,
                    fin_token,
                    "tools/call",
                    {"name": "execute_code", "arguments": {"code": "1 + 1"}},
                )
                assert denied["result"]["isError"] is True
                assert denied["result"]["structuredContent"]["ok"] is False

        good_app = create_gateway_app(
            settings=GatewaySettings(
                auth_mode="dev",
                sandbox_backend="remote",
                sandbox_worker_url=f"ws://127.0.0.1:{port}/ws/execute",
                sandbox_worker_secret=secret,
                sqlite_path=str(tmp_path / "good.db"),
            ),
            dev_keys=dev_keys,
            http_client=backend_client,
        )
        async with good_app.router.lifespan_context(good_app):
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=good_app), base_url="http://gw"
            ) as gw:
                ok_res = await _post_mcp(
                    gw,
                    fin_token,
                    "tools/call",
                    {
                        "name": "execute_code",
                        "arguments": {
                            "code": (
                                "await snowflake_ledger__get_customer_balance("
                                "customer_id='CUST-1003')"
                            )
                        },
                    },
                )
                sc = ok_res["result"]["structuredContent"]
                assert sc["ok"] is True
                assert sc["_provenance"]["grounded"] is True
                assert sc["_provenance"]["tools_called"] == [
                    "snowflake_ledger__get_customer_balance"
                ]
    finally:
        server.should_exit = True
        await server_task
        await backend_client.aclose()


async def test_bulk_200_dataset_and_offline_savings(tmp_path) -> None:
    settings = GatewaySettings(auth_mode="dev", sqlite_path=str(tmp_path / "bulk.db"))
    dev_keys = LocalDevKeyManager(issuer=settings.oidc_issuer)
    backends_app = create_mock_backends_app(
        public_key_pem=dev_keys.public_pem, expected_issuer=settings.oidc_issuer
    )
    backend_client = httpx.AsyncClient(
        transport=httpx.ASGITransport(app=backends_app), base_url="http://127.0.0.1:8001"
    )
    runtime = GatewayRuntime(
        settings=settings,
        store=SQLiteAuditStore(settings.sqlite_path),
        dev_keys=dev_keys,
        http_client=backend_client,
    )
    await runtime.startup()
    try:
        identity = await runtime.validator.validate_token(
            dev_keys.issue_user_token(persona="finance_analyst")
        )
        cmp_res = await run_side_by_side_comparison(
            runtime, identity, scenario="reconcile_bulk_200"
        )
        cm = cmp_res["code_mode"]
        assert cm["ok"] is True
        assert cm["result"] == [
            {"customer_id": "CUST-2041", "delta": 615.0},
            {"customer_id": "CUST-2118", "delta": -480.0},
            {"customer_id": "CUST-2199", "delta": 1250.0},
        ]
        assert cmp_res["deltas"]["context_token_savings_pct"] >= 50.0
        assert cmp_res["deltas"]["payload_bytes_saved"] > 20000
    finally:
        await runtime.shutdown()
        await backend_client.aclose()


async def test_typed_output_schemas_and_keyerror_shape_hints(tmp_path) -> None:
    settings = GatewaySettings(
        app_env="sandbox",
        auth_mode="dev",
        sqlite_path=str(tmp_path / "typed.db"),
    )
    dev_keys = LocalDevKeyManager(issuer=settings.oidc_issuer)
    backends_app = create_mock_backends_app(
        public_key_pem=dev_keys.public_pem, expected_issuer=settings.oidc_issuer
    )
    schemaless = FastAPI()

    @schemaless.post("/mcp")
    async def schemaless_mcp(request: dict[str, Any]):
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
                            "name": "fetch_widget",
                            "description": "Fetch widget without outputSchema",
                            "inputSchema": {"type": "object", "properties": {}},
                        }
                    ]
                },
            }
        return {
            "jsonrpc": "2.0",
            "id": request["id"],
            "result": {
                "content": [
                    {
                        "type": "text",
                        "text": json.dumps({"widget_records": [{"sku": "SKU-1", "stock_qty": 9}]}),
                    }
                ]
            },
        }

    combined = FastAPI()
    combined.mount("/schemaless", schemaless)
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

    try:
        async with app.router.lifespan_context(app):
            rt: GatewayRuntime = app.state.runtime
            sf_tool = rt.catalog.get_tool("snowflake_ledger__query_debt_ledger")
            eu_tool = rt.catalog.get_tool("billing_legacy_eu__list_eu_invoices")
            assert sf_tool is not None and sf_tool.output_schema is not None
            assert eu_tool is not None and eu_tool.output_schema is not None
            assert "outputSchema" in to_mcp_tool_definition(sf_tool)
            assert "outputSchema" in to_mcp_tool_definition(eu_tool)

            sf_stub = render_python_stub(sf_tool)
            eu_stub = render_python_stub(eu_tool)
            assert "-> dict[str, Any]:" in sf_stub
            assert "rows" in sf_stub and "ledger_balance" in sf_stub
            assert "invoices" in eu_stub and "amount" in eu_stub

            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app), base_url="http://gw"
            ) as gw:
                up = await _post_mcp(gw, fin_token, "tools/list", query="mode=codemode_upfront")
                up_desc = up["result"]["tools"][0]["description"]
                assert "Returns: {'queried_by': str, 'rows':" in up_desc
                assert "ledger_balance" in up_desc and "invoices" in up_desc

                reg = await gw.post(
                    "/admin/upstreams",
                    json={
                        "name": "widget_api",
                        "type": "mcp_http",
                        "url": "http://127.0.0.1:8001/schemaless/mcp",
                        "auth_type": "none",
                        "allowed_roles": ["finance_analyst"],
                        "allowed_operations": ["fetch_widget"],
                        "tool_effects": {"fetch_widget": "read"},
                        "mutating_tools": [],
                    },
                )
                assert reg.status_code == 200, reg.text
                w_tool = rt.catalog.get_tool("widget_api__fetch_widget")
                assert w_tool is not None and w_tool.output_schema is None

                err_resp = await _post_mcp(
                    gw,
                    fin_token,
                    "tools/call",
                    {
                        "name": "execute_code",
                        "arguments": {
                            "code": ("w = await widget_api__fetch_widget()\nw['wrong_key']")
                        },
                    },
                    query="mode=codemode",
                )
                err_sc = err_resp["result"]["structuredContent"]
                assert err_sc["ok"] is False
                assert err_sc["error_type"] in {"KeyError", "MontyRuntimeError"}
                assert "KeyError" in err_sc["error"]
                assert err_sc["observed_output_shapes"] == {
                    "widget_api__fetch_widget": {
                        "widget_records": [{"sku": "str", "stock_qty": "int"}]
                    }
                }
                assert "Observed return shapes from tools called in this run" in err_sc["error"]
    finally:
        await backend_client.aclose()
