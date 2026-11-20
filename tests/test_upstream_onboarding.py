"""Phase 3 tests: Plug-and-play MCP/OpenAPI onboarding, prod locks, comparator, and dashboard."""

from __future__ import annotations

import json
import sys
from pathlib import Path

import httpx
import pytest
from fastapi import FastAPI

from examples.mock_backends import build_synthetic_tools, create_mock_backends_app
from gateway.auth import LocalDevKeyManager
from gateway.cli import main as cli_main
from gateway.comparator import audit_catalog_comparison
from gateway.config import GatewaySettings, UpstreamConfig
from gateway.server import GatewayRuntime, create_gateway_app
from gateway.state import SQLiteAuditStore
from gateway.upstream_clients import DiscoveredTool


async def test_dynamic_onboarding_stdio_http_and_swagger_hot_reload(tmp_path) -> None:
    settings = GatewaySettings(
        app_env="sandbox",
        auth_mode="dev",
        sqlite_path=str(tmp_path / "gw.db"),
    )
    dev_keys = LocalDevKeyManager(issuer=settings.oidc_issuer)
    backends_app = create_mock_backends_app(
        public_key_pem=dev_keys.public_pem, expected_issuer=settings.oidc_issuer
    )

    # Mount a lightweight external HTTP MCP + OpenAPI target onto the shared test client
    extra_app = FastAPI()

    @extra_app.post("/extra/mcp")
    async def extra_mcp(request: dict):
        if request.get("method") == "tools/list":
            return {
                "jsonrpc": "2.0",
                "id": request["id"],
                "result": {
                    "tools": [
                        {
                            "name": "query_debt_ledger",  # intentional flat-name collision!
                            "description": "Duplicate named ledger query on extra MCP",
                            "inputSchema": {"type": "object", "properties": {}},
                        }
                    ]
                },
            }
        return {
            "jsonrpc": "2.0",
            "id": request["id"],
            "result": {"content": [{"type": "text", "text": json.dumps({"extra": True})}]},
        }

    @extra_app.get("/orders/{order_id}")
    async def get_order(order_id: str, include_items: bool = False):
        return {"order_id": order_id, "include_items": include_items, "total": 199.5}

    combined = FastAPI()
    combined.mount("/extra-srv", extra_app)
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
    token = dev_keys.issue_user_token(persona="finance_analyst")

    swagger_spec = {
        "openapi": "3.1.0",
        "info": {"title": "Orders API", "version": "1.0.0"},
        "paths": {
            "/orders/{order_id}": {
                "get": {
                    "operationId": "get_order",
                    "summary": "Fetch order details",
                    "parameters": [
                        {
                            "name": "order_id",
                            "in": "path",
                            "required": True,
                            "schema": {"type": "string"},
                        },
                        {
                            "name": "include_items",
                            "in": "query",
                            "required": False,
                            "schema": {"type": "boolean"},
                        },
                    ],
                }
            }
        },
    }

    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://gw"
        ) as gw:
            # 1. Inspect & register inline Swagger/OpenAPI spec -> creates Classic + Code Mode
            inspect_resp = await gw.post(
                "/admin/upstreams/inspect",
                json={
                    "name": "orders_swagger",
                    "type": "openapi",
                    "url": "http://127.0.0.1:8001/extra-srv",
                    "openapi_spec": swagger_spec,
                    "auth_type": "none",
                },
            )
            assert inspect_resp.status_code == 200
            ins_body = inspect_resp.json()
            assert ins_body["inspection"]["tool_count"] == 1
            assert "upstreams:" in ins_body["inspection"]["yaml_snippet"]

            add_sw = await gw.post(
                "/admin/upstreams",
                json={
                    "name": "orders_swagger",
                    "type": "openapi",
                    "url": "http://127.0.0.1:8001/extra-srv",
                    "openapi_spec": swagger_spec,
                    "auth_type": "none",
                },
            )
            assert add_sw.status_code == 200
            assert add_sw.json()["registered"]

            # 2. Register an extra mcp_http with colliding flat tool name
            add_http = await gw.post(
                "/admin/upstreams",
                json={
                    "name": "extra_http",
                    "type": "mcp_http",
                    "url": "http://127.0.0.1:8001/extra-srv/extra/mcp",
                    "auth_type": "none",
                },
            )
            assert add_http.status_code == 200
            collisions = add_http.json()["onboarding_audit"]["classic_mcp"]["flat_name_collisions"]
            assert any(c["tool_name"] == "query_debt_ledger" for c in collisions)
            assert add_http.json()["onboarding_audit"]["code_mode"]["flat_name_collisions"] == []

            # 3. Register a real mcp_stdio server (examples/stdio_echo_mcp.py)
            stdio_script = str(Path("examples/stdio_echo_mcp.py").resolve())
            add_stdio = await gw.post(
                "/admin/upstreams",
                json={
                    "name": "stdio_echo",
                    "type": "mcp_stdio",
                    "command": sys.executable,
                    "args": [stdio_script],
                    "auth_type": "none",
                },
            )
            assert add_stdio.status_code == 200
            assert add_stdio.json()["inspection"]["tools"][0]["name"] == "echo_upper"

            # Verify hot-reload into BOTH traditional tools/list and codemode search+execute
            trad_tools = await gw.post(
                "/mcp?mode=traditional",
                headers={"Authorization": f"Bearer {token}"},
                json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"},
            )
            trad_names = {t["name"] for t in trad_tools.json()["result"]["tools"]}
            assert "orders_swagger__get_order" in trad_names
            assert "stdio_echo__echo_upper" in trad_names

            cm_exec = await gw.post(
                "/mcp?mode=codemode",
                headers={"Authorization": f"Bearer {token}"},
                json={
                    "jsonrpc": "2.0",
                    "id": 2,
                    "method": "tools/call",
                    "params": {
                        "name": "execute_code",
                        "arguments": {
                            "code": (
                                "o = await orders_swagger__get_order("
                                "order_id='ORD-42', include_items=True)\n"
                                "s = await stdio_echo__echo_upper(text='hello')\n"
                                "{'order': o, 'stdio': s}"
                            )
                        },
                    },
                },
            )
            exec_sc = cm_exec.json()["result"]["structuredContent"]
            assert exec_sc["ok"], exec_sc
            assert exec_sc["result"]["order"]["order_id"] == "ORD-42"
            assert exec_sc["result"]["stdio"]["upper"] == "HELLO"

            # 4. Side-by-side comparison playground endpoint
            cmp_reconcile = await gw.post(
                "/compare/run",
                json={"persona": "finance_analyst", "scenario": "reconcile_overdue"},
            )
            assert cmp_reconcile.status_code == 200
            assert cmp_reconcile.json()["winner"] == "code_mode"
            assert cmp_reconcile.json()["deltas"]["round_trips_saved"] >= 2

            cmp_single = await gw.post(
                "/compare/run",
                json={"persona": "finance_analyst", "scenario": "single_lookup"},
            )
            assert cmp_single.status_code == 200
            assert cmp_single.json()["winner"] == "classic_or_tie"

            # 5. Dashboard HTML serves cleanly
            dash = await gw.get("/dashboard")
            assert dash.status_code == 200
            assert "MCPcodeaway" in dash.text

            # 6. Delete dynamic upstream cleans catalog
            deleted = await gw.delete("/admin/upstreams/stdio_echo")
            assert deleted.status_code == 200
            assert deleted.json()["deleted"]

    await backend_client.aclose()


async def test_prod_environment_locks_dynamic_upstreams_and_warns_at_scale(tmp_path) -> None:
    prod_settings = GatewaySettings(
        app_env="prod",
        read_only_prod=True,
        auth_mode="dev",
        sqlite_path=str(tmp_path / "prod.db"),
    )
    assert prod_settings.dynamic_upstreams_permitted is False
    with pytest.raises(ValueError, match="auth_mode must be 'oidc'"):
        create_gateway_app(
            settings=prod_settings,
            store=SQLiteAuditStore(prod_settings.sqlite_path),
        )

    # Verify tool_limit_status thresholds (>30 warning, >100 critical)
    runtime = GatewayRuntime(settings=GatewaySettings(sqlite_path=str(tmp_path / "cmp.db")))
    syn_120 = [
        DiscoveredTool(
            server="huge_erp",
            name=t["name"],
            qualified_name=f"huge_erp__{t['name']}",
            description=t["description"],
            input_schema=t["inputSchema"],
        )
        for t in build_synthetic_tools(120)
    ]
    audit = audit_catalog_comparison(
        runtime.catalog,
        runtime.registry,
        candidate_name="huge_erp",
        candidate_tools=syn_120,
    )
    assert audit["classic_mcp"]["tool_limit_status"] == "critical"
    assert audit["code_mode"]["exposed_tool_count"] == 3
    assert audit["code_mode"]["schema_token_reduction_pct"] > 90.0


def test_cli_add_mcp_exports_yaml(tmp_path, capsys) -> None:
    spec_file = tmp_path / "spec.json"
    spec_file.write_text(
        json.dumps(
            {
                "openapi": "3.1.0",
                "paths": {"/ping": {"get": {"operationId": "ping_service", "summary": "Ping"}}},
            }
        ),
        encoding="utf-8",
    )
    code = cli_main(
        [
            "add-mcp",
            "--name",
            "cli_swagger",
            "--type",
            "openapi",
            "--url",
            "http://example.internal",
            "--openapi-spec-file",
            str(spec_file),
            "--export-yaml",
        ]
    )
    assert code == 0
    out = capsys.readouterr().out
    assert "name: cli_swagger" in out
    assert "pinned_schema_digest:" in out
    assert UpstreamConfig(name="cli_swagger", type="openapi").name == "cli_swagger"
