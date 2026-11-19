"""Phase 6 tests: Classic discoverability, multi-MCP scoping, System 1, and X-Intent."""

from __future__ import annotations

import json
from typing import Any

import httpx

from benchmarks.discovery_benchmark import run_discovery_benchmark
from examples.mock_backends import build_synthetic_tools, create_mock_backends_app
from gateway.auth import LocalDevKeyManager
from gateway.config import GatewaySettings, UpstreamConfig
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
    req_headers = {"Authorization": f"Bearer {token}"}
    if headers:
        req_headers.update(headers)
    resp = await client.post(
        url,
        headers=req_headers,
        json={"jsonrpc": "2.0", "id": req_id, "method": method, "params": params or {}},
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
                            'bal = await snowflake_ledger__get_customer_balance('
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
            assert disc_res["supportedVersions"] == ["2025-03-26"]
            assert disc_res["cacheScope"] == "private"
            assert disc_res["ttlMs"] > 0

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


async def test_bm25_embedding_cache_system1_and_fallback(tmp_path) -> None:
    embed_calls: list[list[str]] = []
    system1_calls: list[dict[str, Any]] = []
    system1_fail_mode = {"fail": False}

    def _mock_handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content.decode("utf-8"))
        if request.url.path == "/v1/embeddings":
            inputs = list(body["input"])
            embed_calls.append(inputs)
            data = []
            for idx, text in enumerate(inputs):
                low = text.lower()
                # Orthogonal signal: [eu_invoice_dim, debt_ledger_dim, noise_dim]
                vec = [
                    1.0 if ("eu_invoice" in low or "european" in low) else 0.0,
                    1.0 if ("debt_ledger" in low or "debt" in low) else 0.0,
                    0.05,
                ]
                data.append({"index": idx, "embedding": vec})
            return httpx.Response(200, json={"data": data})

        if request.url.path == "/v1/systemone":
            system1_calls.append(body)
            if system1_fail_mode["fail"]:
                return httpx.Response(
                    400,
                    json={
                        "error": {"code": "jitLoadFailed", "message": "Failed to load model"}
                    },
                )
            questions = body["questions"]
            answers: dict[str, Any] = {}
            for qid, qspec in questions.items():
                instr = qspec["instructions"].lower()
                if qid.startswith("srv_"):
                    answers[qid] = {
                        "type": "noul",
                        "noul": 0.94 if "snowflake_ledger" in instr else 0.06,
                    }
                else:
                    answers[qid] = {"probability": 0.91 if "query_debt_ledger" in instr else 0.15}
            return httpx.Response(200, json={"answers": answers})

        return httpx.Response(404, json={"error": "not found"})

    mock_disc_client = httpx.AsyncClient(
        transport=httpx.MockTransport(_mock_handler), base_url="http://mock.discovery"
    )
    app, dev_keys = _setup_gateway(
        tmp_path,
        discovery_client=mock_disc_client,
        settings_overrides={
            "embedding_url": "http://mock.discovery/v1/embeddings",
            "system1_url": "http://mock.discovery/v1/systemone",
            "system1_model": "laya",
            "system1_threshold": 0.35,
        },
    )
    fin_token = dev_keys.issue_user_token(persona="finance_analyst")
    ops_token = dev_keys.issue_user_token(persona="ops_engineer")

    async with app.router.lifespan_context(app):
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

            # 2. Embedding backend embeds catalog once, then caches vectors by catalog_version
            emb1 = await _mcp_post(
                client,
                fin_token,
                "tools/call",
                {"name": "search_tools", "arguments": {"query": "european merchant invoices"}},
                query="mode=codemode&discovery=embedding",
            )
            emb1_sc = emb1["result"]["structuredContent"]
            assert emb1_sc["backend_used"] == "embedding"
            assert emb1_sc["tools"][0]["server"] == "billing_legacy_eu"
            assert len(embed_calls[0]) > 1  # query + tool cards

            emb2 = await _mcp_post(
                client,
                fin_token,
                "tools/call",
                {"name": "search_tools", "arguments": {"query": "debt balance lookup"}},
                query="mode=codemode&discovery=embedding",
            )
            assert emb2["result"]["structuredContent"]["tools"][0]["server"] == "snowflake_ledger"
            assert len(embed_calls[1]) == 1  # ONLY the new query was embedded!

            # 3. System 1 enforces RBAC BEFORE Stage 1: ops_engineer only sends ops_ticketing
            system1_calls.clear()
            await _mcp_post(
                client,
                ops_token,
                "tools/call",
                {"name": "search_tools", "arguments": {"query": "customer debt ledger"}},
                query="mode=codemode&discovery=system1",
            )
            assert len(system1_calls) == 2
            stage1_qkeys = set(system1_calls[0]["questions"].keys())
            assert stage1_qkeys == {"srv_ops_ticketing"}

            # 4. System 1 Stage 1 prunes billing servers -> Stage 2 scores only snowflake
            system1_calls.clear()
            s1_fin = await _mcp_post(
                client,
                fin_token,
                "tools/call",
                {"name": "search_tools", "arguments": {"query": "overdue balances"}},
                query="mode=codemode&discovery=system1",
            )
            s1_sc = s1_fin["result"]["structuredContent"]
            assert s1_sc["backend_used"] == "system1"
            assert s1_sc["fallback_triggered"] is False
            assert s1_sc["tools"][0]["qualified_name"] == "snowflake_ledger__query_debt_ledger"
            assert s1_sc["tools"][0]["stage1_server_score"] == 0.94
            assert len(system1_calls) == 2
            stage2_instrs = " ".join(
                q["instructions"] for q in system1_calls[1]["questions"].values()
            )
            assert "billing_legacy_eu__" not in stage2_instrs

            # 5. BM25 fallback when /v1/systemone returns HTTP 400 jitLoadFailed
            system1_fail_mode["fail"] = True
            fb_res = await _mcp_post(
                client,
                fin_token,
                "tools/call",
                {"name": "search_tools", "arguments": {"query": "debt ledger"}},
                query="mode=codemode&discovery=system1",
            )
            fb_sc = fb_res["result"]["structuredContent"]
            assert fb_sc["backend_requested"] == "system1"
            assert fb_sc["backend_used"] == "bm25"
            assert fb_sc["fallback_triggered"] is True
            assert "400" in (fb_sc["fallback_reason"] or "")
            assert len(fb_sc["tools"]) > 0


async def test_hybrid_system1_prefilter_and_x_intent_header(tmp_path) -> None:
    stage2_counts: list[int] = []

    def _s1_handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content.decode("utf-8"))
        qkeys = list(body["questions"].keys())
        if any(k.startswith("tool_") for k in qkeys):
            stage2_counts.append(len(qkeys))
        answers = {
            k: {"probability": 0.9 if "snowflake" in v["instructions"].lower() else 0.7}
            for k, v in body["questions"].items()
        }
        return httpx.Response(200, json={"answers": answers})

    mock_client = httpx.AsyncClient(
        transport=httpx.MockTransport(_s1_handler), base_url="http://mock.s1"
    )
    app, dev_keys = _setup_gateway(
        tmp_path,
        discovery_client=mock_client,
        settings_overrides={"system1_url": "http://mock.s1/v1/systemone"},
    )
    fin_token = dev_keys.issue_user_token(persona="finance_analyst")

    async with app.router.lifespan_context(app):
        # Add 50 synthetic tools directly to billing_legacy_us for <=25 prefilter test
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
            # hybrid_system1 caps Stage 2 tool questions to <= 25 even across >50 accessible tools
            await _mcp_post(
                client,
                fin_token,
                "tools/call",
                {"name": "search_tools", "arguments": {"query": "invoice reconciliation"}},
                query="mode=codemode&discovery=hybrid_system1",
            )
            assert stage2_counts and stage2_counts[-1] <= 25

            # X-Intent in traditional_discover pre-hydrates matching tools with full JSON schemas
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

            # X-Intent in codemode keeps 3-tool surface & inlines Python stub in execute_code
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
