"""Phase 6 tests: Classic discoverability, multi-MCP scoping, BM25, and X-Intent."""

from __future__ import annotations

from typing import Any

import httpx

from benchmarks.discovery_benchmark import run_discovery_benchmark
from examples.mock_backends import build_synthetic_tools, create_mock_backends_app
from gateway.auth import LocalDevKeyManager
from gateway.config import GatewaySettings, UpstreamConfig
from gateway.mcp_protocol import request_headers, request_meta
from gateway.server import create_gateway_app
from gateway.state import SQLiteAuditStore
from gateway.upstream_clients import DiscoveredTool


async def _mcp_post(
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
    if query:
        for part in query.split("&"):
            if part.startswith("mode="):
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


def _setup_gateway(
    tmp_path,
    *,
    discovery_client: httpx.AsyncClient | None = None,
    settings_overrides: dict[str, Any] | None = None,
):
    cfg_kwargs: dict[str, Any] = {
        "auth_mode": "dev",
        "sqlite_path": str(tmp_path / "gw.db"),
    }
    if settings_overrides:
        cfg_kwargs.update(settings_overrides)
    settings = GatewaySettings(**cfg_kwargs)
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
        discovery_http_client=discovery_client,
    )
    return app, dev_keys


async def test_tool_guidance_reaches_discovery_and_classic_tools_for_one_tenant(tmp_path) -> None:
    app, keys = _setup_gateway(
        tmp_path, settings_overrides={"allowed_tenants": ["tenant-acme-dev", "tenant-other"]}
    )
    admin = keys.issue_user_token(persona="gateway_admin")
    viewer = keys.issue_user_token(overrides={"roles": ["operator_viewer"]})
    finance = keys.issue_user_token(persona="finance_analyst")
    other = keys.issue_user_token(
        persona="finance_analyst", overrides={"tenant_id": "tenant-other"}
    )
    tool = "snowflake_ledger__get_customer_balance"
    endpoint = f"/admin/upstreams/snowflake_ledger/tools/{tool}/guidance"
    admin_headers = {"Authorization": f"Bearer {admin}"}
    viewer_headers = {"Authorization": f"Bearer {viewer}"}
    edits = {
        "description": "Read the customer's accountsreceivable balance.",
        "parameter_hints": {"customer_id": "Use the stable customer ID from the ledger."},
    }
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://testserver"
        ) as client:
            original = (await client.get(endpoint, headers=viewer_headers)).json()
            assert original["can_edit"] is False
            assert original["guidance"] is None
            assert (
                await client.put(endpoint, headers=viewer_headers, json=edits)
            ).status_code == 403
            invalid = await client.put(
                endpoint, headers=admin_headers, json={"parameter_hints": {"unknown": "bad"}}
            )
            assert invalid.status_code == 422
            saved = await client.put(endpoint, headers=admin_headers, json=edits)
            assert saved.json()["guidance"] == edits
            updated = (await client.get(endpoint, headers=viewer_headers)).json()
            assert updated["guidance"] == edits
            assert updated["input_schema"] == original["input_schema"]
            assert updated["output_schema"] == original["output_schema"]

            args = {"name": "search_tools", "arguments": {"query": "accountsreceivable"}}
            search = await _mcp_post(client, finance, "tools/call", args, query="mode=codemode")
            discovered = search["result"]["structuredContent"]["tools"]
            assert discovered[0]["qualified_name"] == tool
            assert edits["parameter_hints"]["customer_id"] in discovered[0]["stub"]
            preloaded = await _mcp_post(
                client,
                finance,
                "tools/list",
                query="mode=codemode",
                headers={"X-Intent": "accountsreceivable"},
            )
            execute = next(t for t in preloaded["result"]["tools"] if t["name"] == "execute_code")
            assert edits["parameter_hints"]["customer_id"] in execute["description"]
            isolated = await _mcp_post(client, other, "tools/call", args, query="mode=codemode")
            assert isolated["result"]["structuredContent"]["tools"] == []

            classic = await _mcp_post(client, finance, "tools/list", query="mode=traditional")
            definition = next(t for t in classic["result"]["tools"] if t["name"] == tool)
            assert definition["description"] == edits["description"]
            parameter = definition["inputSchema"]["properties"]["customer_id"]
            assert edits["parameter_hints"]["customer_id"] in parameter["description"]
            reset = await client.put(endpoint, headers=admin_headers, json={})
            assert reset.json()["guidance"] is None
            classic = await _mcp_post(client, finance, "tools/list", query="mode=traditional")
            definition = next(t for t in classic["result"]["tools"] if t["name"] == tool)
            assert definition["description"] == original["description"]


async def test_list_servers_ceiling_and_execute_code_scoping(tmp_path) -> None:
    app, dev_keys = _setup_gateway(tmp_path)
    fin_token = dev_keys.issue_user_token(persona="finance_analyst")

    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://testserver"
        ) as client:
            # 1. list_servers respects RBAC (finance_analyst sees 3 finance servers, not ops)
            ls_res = await _mcp_post(
                client,
                fin_token,
                "tools/call",
                {"name": "list_servers", "arguments": {}},
                query="mode=codemode",
            )
            ls_data = ls_res["result"]["structuredContent"]
            srv_names = {s["server"] for s in ls_data["servers"]}
            assert {"snowflake_ledger", "billing_legacy_eu", "billing_legacy_us"} <= srv_names
            assert "ops_ticketing" not in srv_names

            # 2. Request ceiling ?servers=snowflake_ledger hides billing_legacy_eu everywhere
            ceiled_ls = await _mcp_post(
                client,
                fin_token,
                "tools/call",
                {"name": "list_servers", "arguments": {}},
                query="mode=codemode&servers=snowflake_ledger",
            )
            assert [s["server"] for s in ceiled_ls["result"]["structuredContent"]["servers"]] == [
                "snowflake_ledger"
            ]

            ceiled_search = await _mcp_post(
                client,
                fin_token,
                "tools/call",
                {"name": "search_tools", "arguments": {"query": "eu invoices", "limit": 10}},
                query="mode=codemode&servers=snowflake_ledger",
            )
            returned_servers = {
                t["server"] for t in ceiled_search["result"]["structuredContent"]["tools"]
            }
            assert "billing_legacy_eu" not in returned_servers

            # 3. execute_code cannot bypass request ceiling even when requesting billing_legacy_eu
            blocked_exec = await _mcp_post(
                client,
                fin_token,
                "tools/call",
                {
                    "name": "execute_code",
                    "arguments": {
                        "servers": ["snowflake_ledger", "billing_legacy_eu"],
                        "code": "return await billing_legacy_eu__list_eu_invoices()",
                    },
                },
                query="mode=codemode&servers=snowflake_ledger",
            )
            assert blocked_exec["result"]["isError"] is True

            # 4. execute_code optional `servers=[...]` narrows tools when no URL ceiling is set
            narrow_fail = await _mcp_post(
                client,
                fin_token,
                "tools/call",
                {
                    "name": "execute_code",
                    "arguments": {
                        "servers": ["snowflake_ledger"],
                        "code": "return await billing_legacy_eu__list_eu_invoices()",
                    },
                },
                query="mode=codemode",
            )
            assert narrow_fail["result"]["isError"] is True

            narrow_ok = await _mcp_post(
                client,
                fin_token,
                "tools/call",
                {
                    "name": "execute_code",
                    "arguments": {
                        "servers": ["snowflake_ledger"],
                        "code": (
                            "bal = await snowflake_ledger__get_customer_balance("
                            'customer_id="CUST-1003")\n'
                            "return bal"
                        ),
                    },
                },
                query="mode=codemode",
            )
            ok_data = narrow_ok["result"]["structuredContent"]
            assert ok_data["ok"] is True
            assert ok_data["result"]["customer_id"] == "CUST-1003"


async def test_traditional_discover_server_discover_and_pagination(tmp_path) -> None:
    app, dev_keys = _setup_gateway(tmp_path)
    fin_token = dev_keys.issue_user_token(persona="finance_analyst")

    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://testserver"
        ) as client:
            # MCP v2 server/discover metadata
            disc = await _mcp_post(client, fin_token, "server/discover")
            disc_res = disc["result"]
            assert disc_res["supportedVersions"] == ["2026-07-28"]
            assert disc_res["cacheScope"] == "private"

            assert disc_res["ttlMs"] > 0
            assert disc_res["instructions"].startswith(
                "For deferred discovery, start with search_tools"
            )
            assert "list_servers for optional browsing" in disc_res["instructions"]

            # traditional_discover exposes ONLY list_servers + search_tools upfront
            td_list = await _mcp_post(
                client, fin_token, "tools/list", query="mode=traditional_discover"
            )
            td_names = [t["name"] for t in td_list["result"]["tools"]]
            assert td_names == ["list_servers", "search_tools"]

            # search_tools in traditional_discover returns hydrated_tools with JSON schemas
            td_search = await _mcp_post(
                client,
                fin_token,
                "tools/call",
                {"name": "search_tools", "arguments": {"query": "eu invoices", "limit": 5}},
                query="mode=traditional_discover",
            )
            sc = td_search["result"]["structuredContent"]
            hydrated = {t["name"]: t for t in sc["hydrated_tools"]}
            assert "billing_legacy_eu__list_eu_invoices" in hydrated
            assert "inputSchema" in hydrated["billing_legacy_eu__list_eu_invoices"]

            # Direct execution of hydrated tool works in traditional_discover mode
            call_res = await _mcp_post(
                client,
                fin_token,
                "tools/call",
                {"name": "billing_legacy_eu__list_eu_invoices", "arguments": {}},
                query="mode=traditional_discover",
            )
            assert call_res["result"]["isError"] is False
            assert call_res["result"]["structuredContent"]["ok"] is True

            # Pagination + server filtering on traditional tools/list
            p1 = await _mcp_post(
                client, fin_token, "tools/list", {"limit": 3}, query="mode=traditional"
            )
            p1_names = [t["name"] for t in p1["result"]["tools"]]
            assert len(p1_names) == 3
            assert "nextCursor" in p1["result"]

            p2 = await _mcp_post(
                client,
                fin_token,
                "tools/list",
                {"limit": 3, "cursor": p1["result"]["nextCursor"]},
                query="mode=traditional",
            )
            p2_names = [t["name"] for t in p2["result"]["tools"]]
            assert len(p2_names) == 3
            assert set(p1_names).isdisjoint(set(p2_names))


async def test_modern_mcp_metadata_headers_and_discovery_contract(tmp_path) -> None:
    app, dev_keys = _setup_gateway(tmp_path)
    token = dev_keys.issue_user_token(persona="finance_analyst")
    metadata = {
        "io.modelcontextprotocol/protocolVersion": "2026-07-28",
        "io.modelcontextprotocol/clientInfo": {"name": "test-client", "version": "1.0"},
        "io.modelcontextprotocol/clientCapabilities": {},
    }
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://testserver"
        ) as client:
            headers = {
                "Authorization": f"Bearer {token}",
                "MCP-Protocol-Version": "2026-07-28",
                "Mcp-Method": "server/discover",
            }
            discover = await client.post(
                "/mcp",
                headers=headers,
                json={
                    "jsonrpc": "2.0",
                    "id": "modern-discover",
                    "method": "server/discover",
                    "params": {"_meta": metadata},
                },
            )
            assert discover.status_code == 200
            discover_result = discover.json()["result"]
            assert discover_result["resultType"] == "complete"
            assert discover_result["supportedVersions"] == ["2026-07-28"]
            assert (
                discover_result["_meta"]["io.modelcontextprotocol/serverInfo"]["name"]
                == "mcpcodeaway"
            )

            modern_tool_list = await client.post(
                "/mcp",
                headers={**headers, "Mcp-Method": "tools/list"},
                json={
                    "jsonrpc": "2.0",
                    "id": "modern-tools-list",
                    "method": "tools/list",
                    "params": {"_meta": metadata},
                },
            )
            assert modern_tool_list.status_code == 200
            modern_tool_list_result = modern_tool_list.json()["result"]
            assert modern_tool_list_result["ttlMs"] == 0
            assert modern_tool_list_result["cacheScope"] == "private"

            tool_headers = {**headers, "Mcp-Method": "tools/call", "Mcp-Name": "list_servers"}
            tool_response = await client.post(
                "/mcp?mode=codemode",
                headers=tool_headers,
                json={
                    "jsonrpc": "2.0",
                    "id": 2,
                    "method": "tools/call",
                    "params": {"_meta": metadata, "name": "list_servers", "arguments": {}},
                },
            )
            assert tool_response.status_code == 200
            assert tool_response.json()["result"]["resultType"] == "complete"

            mismatch = await client.post(
                "/mcp",
                headers={**headers, "Mcp-Method": "tools/call", "Mcp-Name": "wrong_name"},
                json={
                    "jsonrpc": "2.0",
                    "id": 3,
                    "method": "tools/call",
                    "params": {"_meta": metadata, "name": "list_servers", "arguments": {}},
                },
            )
            assert mismatch.status_code == 400
            assert mismatch.json()["error"]["code"] == -32020

            with_session_id = await client.post(
                "/mcp",
                headers={**headers, "Mcp-Session-Id": "unexpected-session"},
                json={
                    "jsonrpc": "2.0",
                    "id": 30,
                    "method": "server/discover",
                    "params": {"_meta": metadata},
                },
            )
            assert with_session_id.status_code == 400
            assert (
                "protocol sessions are not supported" in with_session_id.json()["error"]["message"]
            )

            unsupported = await client.post(
                "/mcp",
                headers={**headers, "MCP-Protocol-Version": "2025-03-26"},
                json={
                    "jsonrpc": "2.0",
                    "id": 4,
                    "method": "server/discover",
                    "params": {
                        "_meta": {
                            **metadata,
                            "io.modelcontextprotocol/protocolVersion": "2025-03-26",
                        }
                    },
                },
            )
            assert unsupported.status_code == 400
            assert unsupported.json()["error"]["code"] == -32022
            assert unsupported.json()["error"]["data"]["supportedVersions"] == ["2026-07-28"]

            mismatched_version = await client.post(
                "/mcp",
                headers={**headers, "MCP-Protocol-Version": "2025-11-25"},
                json={
                    "jsonrpc": "2.0",
                    "id": 5,
                    "method": "server/discover",
                    "params": {"_meta": metadata},
                },
            )
            assert mismatched_version.status_code == 400
            assert mismatched_version.json()["error"]["code"] == -32020

            initialize = await client.post(
                "/mcp",
                headers={"Authorization": f"Bearer {token}"},
                json={"jsonrpc": "2.0", "id": 6, "method": "initialize", "params": {}},
            )
            assert initialize.status_code == 400

            legacy_followup = await client.post(
                "/mcp",
                headers={
                    "Authorization": f"Bearer {token}",
                    "MCP-Protocol-Version": "2025-11-25",
                },
                json={"jsonrpc": "2.0", "id": 8, "method": "tools/list", "params": {}},
            )
            assert legacy_followup.status_code == 400
            assert legacy_followup.json()["error"]["code"] == -32020

            unknown_method = await client.post(
                "/mcp",
                headers={**headers, "Mcp-Method": "resources/list"},
                json={
                    "jsonrpc": "2.0",
                    "id": 7,
                    "method": "resources/list",
                    "params": {"_meta": metadata},
                },
            )
            assert unknown_method.status_code == 404
            assert unknown_method.json()["error"]["code"] == -32601

            modern_notification = await client.post(
                "/mcp",
                headers={**headers, "Mcp-Method": "notifications/initialized"},
                json={
                    "jsonrpc": "2.0",
                    "method": "notifications/initialized",
                    "params": {"_meta": metadata},
                },
            )
            assert modern_notification.status_code == 400

            rejected_origin = await client.post(
                "/mcp",
                headers={**headers, "Origin": "https://attacker.example"},
                json={
                    "jsonrpc": "2.0",
                    "id": 9,
                    "method": "server/discover",
                    "params": {"_meta": metadata},
                },
            )
            assert rejected_origin.status_code == 403


async def test_bm25_and_x_intent_header_discovery(tmp_path) -> None:
    app, dev_keys = _setup_gateway(tmp_path)
    fin_token = dev_keys.issue_user_token(persona="finance_analyst")

    async with app.router.lifespan_context(app):
        syn_tools = [
            DiscoveredTool(
                server="billing_legacy_us",
                name=t["name"],
                qualified_name=f"billing_legacy_us__{t['name']}",
                description=t.get("description", ""),
                input_schema=t["inputSchema"],
                mutating=False,
                annotations=t.get("annotations", {}),
            )
            for t in build_synthetic_tools(50)
        ]
        rt = app.state.runtime
        us_cfg = UpstreamConfig(
            name="billing_legacy_us", type="mcp_http", url="http://127.0.0.1:8001"
        )
        rt.catalog.set_server_tools(
            us_cfg, rt.catalog.get_server_tools("billing_legacy_us") + syn_tools
        )

        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://testserver"
        ) as client:
            # 1. BM25 semantic ranking
            bm25_res = await _mcp_post(
                client,
                fin_token,
                "tools/call",
                {"name": "search_tools", "arguments": {"query": "past due debt ledger balance"}},
                query="mode=codemode&discovery=bm25",
            )
            bm25_sc = bm25_res["result"]["structuredContent"]
            assert bm25_sc["backend_used"] == "bm25"
            assert bm25_sc["tools"][0]["qualified_name"].startswith("snowflake_ledger__")

            # 2. X-Intent in traditional_discover pre-hydrates matching tools with full JSON schemas
            td_intent = await _mcp_post(
                client,
                fin_token,
                "tools/list",
                query="mode=traditional_discover",
                headers={"X-Intent": "list eu invoices"},
            )
            td_tool_names = [t["name"] for t in td_intent["result"]["tools"]]
            assert "list_servers" in td_tool_names
            assert "search_tools" in td_tool_names
            assert "billing_legacy_eu__list_eu_invoices" in td_tool_names

            # 3. X-Intent in codemode keeps 3-tool surface & inlines Python stub in execute_code
            cm_intent = await _mcp_post(
                client,
                fin_token,
                "tools/list",
                query="mode=codemode",
                headers={"X-Intent": "query debt ledger in snowflake"},
            )
            cm_tools = {t["name"]: t for t in cm_intent["result"]["tools"]}
            assert list(cm_tools.keys()) == ["list_servers", "search_tools", "execute_code"]
            assert "snowflake_ledger__query_debt_ledger" in cm_tools["execute_code"]["description"]


async def test_discovery_benchmark_smoke(tmp_path) -> None:
    out_file = tmp_path / "discovery_smoke.json"
    report = await run_discovery_benchmark(
        backends=["keyword", "bm25"],
        extra_counts=(0, 50),
        output_path=out_file,
    )
    assert out_file.exists()
    assert report["query_count"] == 20
    assert len(report["results"]) == 4

    by_key = {(r["tool_count"], r["backend_requested"]): r for r in report["results"]}
    bm25_large = by_key[max(k[0] for k in by_key if k[1] == "bm25"), "bm25"]
    kw_large = by_key[max(k[0] for k in by_key if k[1] == "keyword"), "keyword"]
    assert bm25_large["semantic_recall_at_5"] >= kw_large["semantic_recall_at_5"]
    assert bm25_large["recall_at_5"] >= 0.7


async def test_authorized_discovery_budget_observe_active_and_explicit_override(
    tmp_path, caplog
) -> None:
    caplog.set_level("INFO", logger="mcpcodeaway")
    app, keys = _setup_gateway(tmp_path, settings_overrides={"auto_discovery_token_budget": 1})
    token = keys.issue_user_token(persona="finance_analyst")
    runtime = app.state.runtime
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://testserver"
        ) as client:
            observed = await _mcp_post(client, token, "tools/list")
            runtime.settings.auto_discovery_policy = "active"
            deferred = await _mcp_post(client, token, "tools/list")
            assert "search_tools" in {t["name"] for t in deferred["result"]["tools"]}
            hydrated = await _mcp_post(
                client, token, "tools/call", {"name": "search_tools", "arguments": {}}
            )
            assert hydrated["result"]["structuredContent"]["tools"]
            search_event = next(
                record.fields
                for record in caplog.records
                if getattr(record, "fields", {}).get("discovery_operation") == "search"
            )
            assert search_event["definitions_returned"] > 0
            assert search_event["estimated_discovery_tokens"] > 0
            assert search_event["response_bytes"] > 0
            assert search_event["discovery_search_ms"] >= 0

            runtime.settings.auto_discovery_token_budget = 10_000
            scope = "servers=billing_legacy_eu"
            listing = await _mcp_post(client, token, "tools/list", query=scope)
            assert "search_tools" not in {t["name"] for t in listing["result"]["tools"]}
            assert "ops_ticketing" not in str(listing)
            page = await _mcp_post(client, token, "tools/list", {"limit": 1}, query=scope)
            assert page["result"]["tools"] == listing["result"]["tools"][:1]
            assert page["result"].get("nextCursor")
            forced = await _mcp_post(
                client, token, "tools/list", query=f"{scope}&discovery_mode=deferred"
            )
            assert "search_tools" in {t["name"] for t in forced["result"]["tools"]}

            runtime.settings.auto_discovery_policy = "observe"
            rollback = await _mcp_post(client, token, "tools/list")
            assert rollback["result"]["tools"] == observed["result"]["tools"]

            endpoint = "/admin/discovery/report"
            viewer = keys.issue_user_token(overrides={"roles": ["operator_viewer"]})
            response = await client.get(endpoint, headers={"Authorization": f"Bearer {viewer}"})
            assert response.status_code == 200
            report = response.json()
            assert report["policy"] == "observe"
            assert report["token_budget"] == 10_000
            assert {row["operation"] for row in report["samples"]} == {"listing", "search"}
            assert any(row.get("reason") == "over_budget" for row in report["samples"])
            assert all("tenant" not in row for row in report["samples"])
            denied = await client.get(endpoint, headers={"Authorization": f"Bearer {token}"})
            assert denied.status_code == 403


def test_discovery_budget_counts_rendered_guidance_and_stops_on_large_catalog() -> None:
    from gateway.catalog import estimate_tokens
    from gateway.server import _budget_upfront_listing
    from gateway.upstream_clients import ToolGuidance

    tool = DiscoveredTool(server="finance", name="lookup", qualified_name="finance__lookup")
    for mode in ("classic", "codemode"):
        listing, tokens, reason = _budget_upfront_listing([tool], mode, 1024)
        assert reason == "fits_budget"
        assert tokens == estimate_tokens(listing)
        description = "tenant guidance " * 20
        larger = tool.model_copy(
            update={"description": description, "guidance": ToolGuidance(description=description)}
        )
        assert _budget_upfront_listing([larger], mode, tokens)[2] == "over_budget"
        wide = tool.model_copy(
            update={
                "input_schema": {
                    "type": "object",
                    "properties": {"id": {"type": "string", "enum": ["x" * 6000]}},
                }
            }
        )
        if mode == "classic":
            assert _budget_upfront_listing([wide], mode, 1024)[2] == "over_budget"
    assert _budget_upfront_listing([tool] * 101, "codemode", 1024)[2] == "stub_cap"
    assert _budget_upfront_listing([], "classic", 1024)[2] == "empty_catalog"
