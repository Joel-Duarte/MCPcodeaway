"""Phase 3 tests: Plug-and-play MCP/OpenAPI onboarding, prod locks, comparator, and dashboard."""

from __future__ import annotations

import json
import sys
from pathlib import Path

import httpx
import pytest
from fastapi import FastAPI
from fastapi.responses import JSONResponse

from examples.mock_backends import build_synthetic_tools, create_mock_backends_app
from gateway.auth import LocalDevKeyManager
from gateway.cli import main as cli_main
from gateway.comparator import audit_catalog_comparison
from gateway.config import GatewaySettings, UpstreamConfig, UpstreamRegistry
from gateway.mcp_protocol import request_headers, request_meta
from gateway.server import GatewayRuntime, create_gateway_app
from gateway.state import SQLiteAuditStore
from gateway.upstream_clients import DiscoveredTool, build_onboarding_guidance


def test_demo_upstream_tool_details_and_approval_policies_are_complete() -> None:
    config_path = Path(__file__).parents[1] / "config" / "upstreams.yaml"
    registry = UpstreamRegistry(
        GatewaySettings(app_env="sandbox", auth_mode="dev", upstreams_config_path=str(config_path))
    )
    demo_names = {
        "snowflake_ledger",
        "billing_legacy_eu",
        "billing_legacy_us",
        "ops_ticketing",
        "recovery_workbench",
    }

    for upstream in registry.list_upstreams():
        if upstream.name not in demo_names:
            continue
        assert upstream.allowed_operations is not None
        assert set(upstream.tool_guidance) == set(upstream.allowed_operations)
        assert set(upstream.tool_approval_policies) == set(upstream.allowed_operations)
        for tool_name in upstream.allowed_operations:
            guidance = upstream.tool_guidance[tool_name]
            assert guidance.description
            assert guidance.parameter_hints
            expected_mode = {
                "read": "none",
                "write": "requester_confirmation",
                "high_impact": "manager_approval",
            }[upstream.tool_effects[tool_name]]
            assert upstream.tool_approval_policies[tool_name].mode == expected_mode


def test_production_upstreams_require_access_and_operation_allowlists(tmp_path: Path) -> None:
    path = tmp_path / "upstreams.yaml"
    settings = GatewaySettings(
        app_env="prod",
        auth_mode="oidc",
        upstreams_config_path=str(path),
    )
    path.write_text(
        "upstreams:\n  - name: open_service\n    type: mcp_http\n    url: https://api.example/mcp\n",
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="allowed_roles or allowed_groups"):
        UpstreamRegistry(settings)


def test_production_accepts_explicit_upstream_authorization_without_gateway_groups(
    tmp_path: Path,
) -> None:
    path = tmp_path / "upstreams.yaml"
    path.write_text(
        "upstreams:\n"
        "  - name: user_api\n"
        "    type: openapi\n"
        "    url: https://api.example.test\n"
        "    auth_type: user_api_key\n"
        "    authorization_mode: upstream\n"
        "    allowed_operations: [read_profile]\n"
        "    tool_effects: {read_profile: read}\n"
        "    mutating_tools: []\n",
        encoding="utf-8",
    )
    settings = GatewaySettings(app_env="prod", auth_mode="oidc", upstreams_config_path=str(path))

    registry = UpstreamRegistry(settings)

    assert registry.get("user_api") is not None


def test_onboarding_treats_api_key_scheme_as_ambiguous_and_explains_auth_owner() -> None:
    upstream = UpstreamConfig(
        name="personal_api",
        type="openapi",
        url="https://api.example.test",
        auth_type="user_api_key",
        authorization_mode="upstream",
    )
    security_schemes = [{"name": "personalKey", "type": "apiKey", "header_name": "X-Personal-Key"}]

    guidance = build_onboarding_guidance(upstream, [], security_schemes)

    assert guidance["auth_discovery"]["status"] == "declared"
    assert guidance["auth_discovery"]["requires_operator_confirmation"] is True
    assert any("Confirm Who Owns the API Key" == item["title"] for item in guidance["checklist"])
    assert any(item["field"] == "authorization_mode" for item in guidance["checklist"])


async def test_upstream_mode_change_survives_catalog_refresh_and_rejects_read_only(
    tmp_path: Path,
) -> None:
    config_path = tmp_path / "upstreams.yaml"
    config_path.write_text(
        "upstreams:\n"
        "  - name: mode_fixture\n"
        "    type: mcp_http\n"
        "    url: http://127.0.0.1:9/mcp\n"
        "    auth_type: none\n"
        "    allowed_operations: [read_fixture]\n"
        "    tool_effects: {read_fixture: read}\n"
        "    preferred_mode: traditional\n",
        encoding="utf-8",
    )
    settings = GatewaySettings(
        app_env="sandbox",
        auth_mode="dev",
        sqlite_path=str(tmp_path / "mode.db"),
        upstreams_config_path=str(config_path),
    )
    app = create_gateway_app(settings=settings)

    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://gw"
        ) as client:
            runtime = app.state.runtime
            await runtime.store.save_dynamic_upstream(
                "mode_fixture",
                {
                    "name": "mode_fixture",
                    "type": "mcp_http",
                    "url": "http://127.0.0.1:9/mcp",
                    "auth_type": "none",
                    "allowed_operations": [],
                    "tool_effects": {},
                    "preferred_mode": "traditional",
                },
            )
            await runtime.registry.refresh()
            changed = await client.patch(
                "/admin/upstreams/mode_fixture/mode",
                json={"preferred_mode": "codemode"},
            )
            assert changed.status_code == 200, changed.text
            assert changed.json()["preferred_mode"] == "codemode"

            await runtime.registry.refresh()
            listed = await client.get("/admin/upstreams?q=mode_fixture")
            assert listed.status_code == 200, listed.text
            assert listed.json()["upstreams"][0]["preferred_mode"] == "codemode"
            assert listed.json()["upstreams"][0]["allowed_operations"] == ["read_fixture"]
            assert listed.json()["upstreams"][0]["tool_effects"] == {"read_fixture": "read"}

            runtime.settings.allow_dynamic_upstreams = False
            read_only = await client.patch(
                "/admin/upstreams/mode_fixture/mode",
                json={"preferred_mode": "traditional"},
            )
            assert read_only.status_code == 403
            assert "config/upstreams.yaml" in read_only.json()["detail"]

            runtime.settings.allow_dynamic_upstreams = True
            invalid = await client.patch(
                "/admin/upstreams/mode_fixture/mode",
                json={"preferred_mode": "made_up_mode"},
            )
            assert invalid.status_code == 422


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

    extra_app = FastAPI()

    @extra_app.post("/extra/mcp")
    async def extra_mcp(request: dict):
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
                            "name": "query_debt_ledger",
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

    @extra_app.post("/protected/mcp")
    async def protected_mcp():
        return JSONResponse(
            {"detail": "authentication required"},
            status_code=401,
            headers={"WWW-Authenticate": 'Bearer realm="private"'},
        )

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

            challenge = await gw.post(
                "/admin/upstreams/inspect",
                json={
                    "name": "protected_mcp",
                    "type": "mcp_http",
                    "url": "http://127.0.0.1:8001/extra-srv/protected/mcp",
                    "auth_type": "none",
                },
            )
            assert challenge.status_code == 400
            assert "Bearer authentication challenge" in challenge.json()["detail"]
            assert "realm" not in challenge.json()["detail"]

            add_sw = await gw.post(
                "/admin/upstreams",
                json={
                    "name": "orders_swagger",
                    "type": "openapi",
                    "url": "http://127.0.0.1:8001/extra-srv",
                    "openapi_spec": swagger_spec,
                    "auth_type": "none",
                    "allowed_operations": ["get_order"],
                    "tool_effects": {"get_order": "read"},
                    "mutating_tools": [],
                    "allow_all_callers": True,
                },
            )
            assert add_sw.status_code == 200
            assert add_sw.json()["registered"]

            add_http = await gw.post(
                "/admin/upstreams",
                json={
                    "name": "extra_http",
                    "type": "mcp_http",
                    "url": "http://127.0.0.1:8001/extra-srv/extra/mcp",
                    "auth_type": "none",
                    "allowed_operations": ["query_debt_ledger"],
                    "tool_effects": {"query_debt_ledger": "read"},
                    "mutating_tools": [],
                    "allow_all_callers": True,
                },
            )
            assert add_http.status_code == 200
            collisions = add_http.json()["onboarding_audit"]["classic_mcp"]["flat_name_collisions"]
            assert any(c["tool_name"] == "query_debt_ledger" for c in collisions)
            assert add_http.json()["onboarding_audit"]["code_mode"]["flat_name_collisions"] == []

            stdio_script = str(Path("examples/stdio_echo_mcp.py").resolve())
            add_stdio = await gw.post(
                "/admin/upstreams",
                json={
                    "name": "stdio_echo",
                    "type": "mcp_stdio",
                    "command": sys.executable,
                    "args": [stdio_script],
                    "auth_type": "none",
                    "allowed_operations": ["echo_upper"],
                    "tool_effects": {"echo_upper": "read"},
                    "mutating_tools": [],
                    "allow_all_callers": True,
                },
            )
            assert add_stdio.status_code == 200
            assert add_stdio.json()["inspection"]["tools"][0]["name"] == "echo_upper"

            trad_tools = await gw.post(
                "/mcp",
                headers={
                    "Authorization": f"Bearer {token}",
                    "X-Admin-Force-Mode": "traditional",
                    **request_headers("tools/list"),
                },
                json={
                    "jsonrpc": "2.0",
                    "id": 1,
                    "method": "tools/list",
                    "params": {"_meta": request_meta()},
                },
            )
            trad_names = {t["name"] for t in trad_tools.json()["result"]["tools"]}
            assert "orders_swagger__get_order" in trad_names
            assert "stdio_echo__echo_upper" in trad_names

            cm_exec = await gw.post(
                "/mcp?mode=codemode",
                headers={
                    "Authorization": f"Bearer {token}",
                    **request_headers("tools/call", tool_name="execute_code"),
                },
                json={
                    "jsonrpc": "2.0",
                    "id": 2,
                    "method": "tools/call",
                    "params": {
                        "_meta": request_meta(),
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

            dash = await gw.get("/dashboard")
            assert dash.status_code == 200
            assert "MCPcodeaway" in dash.text

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
