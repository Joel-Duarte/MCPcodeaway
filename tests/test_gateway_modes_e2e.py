"""Phase 2 tests: End-to-end stateless /mcp across codemode, traditional, and hybrid modes."""

from __future__ import annotations

import httpx

from examples.mock_backends import create_mock_backends_app
from gateway.auth import LocalDevKeyManager
from gateway.config import GatewaySettings
from gateway.server import create_gateway_app
from gateway.state import SQLiteAuditStore


async def test_gateway_modes_e2e_reconciliation_and_audit(tmp_path) -> None:
    settings = GatewaySettings(auth_mode="dev", sqlite_path=str(tmp_path / "gw.db"))
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

    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://gw"
        ) as gw:
            # 1. Code Mode tools/list returns ONLY list_servers, search_tools, and execute_code
            cm_list = await gw.post(
                "/mcp",
                headers={"X-Admin-Force-Mode": "codemode", "Authorization": f"Bearer {fin_token}"},
                json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"},
            )
            assert cm_list.status_code == 200
            cm_names = [t["name"] for t in cm_list.json()["result"]["tools"]]
            assert cm_names == ["list_servers", "search_tools", "execute_code"]

            # 2. RBAC-filtered search_tools hides ops_ticketing from finance_analyst
            search = await gw.post(
                "/mcp",
                headers={"X-Admin-Force-Mode": "codemode", "Authorization": f"Bearer {fin_token}"},
                json={
                    "jsonrpc": "2.0",
                    "id": 2,
                    "method": "tools/call",
                    "params": {"name": "search_tools", "arguments": {"query": ""}},
                },
            )
            search_res = search.json()["result"]["structuredContent"]
            servers = {t["server"] for t in search_res["tools"]}
            assert servers == {"snowflake_ledger", "billing_legacy_eu", "billing_legacy_us"}
            assert "ops_ticketing" not in servers

            # 3. execute_code runs parallel asyncio.gather reconciliation inside Monty
            script = (
                "import asyncio\n"
                "ledger, eu, us = await asyncio.gather(\n"
                "    snowflake_ledger__query_debt_ledger(status='overdue'),\n"
                "    billing_legacy_eu__list_eu_invoices(status='overdue'),\n"
                "    billing_legacy_us__list_us_invoices(status='overdue'),\n"
                ")\n"
                "inv_map = {i['customer_id']: i['amount'] "
                "for i in eu['invoices'] + us['invoices']}\n"
                "mismatches = [\n"
                "    {\n"
                "        'customer_id': r['customer_id'],\n"
                "        'delta': round(r['ledger_balance'] - inv_map[r['customer_id']], 2),\n"
                "    }\n"
                "    for r in ledger['rows']\n"
                "    if r['customer_id'] in inv_map "
                "and r['ledger_balance'] != inv_map[r['customer_id']]\n"
                "]\n"
                "mismatches"
            )
            cm_exec = await gw.post(
                "/mcp",
                headers={"X-Admin-Force-Mode": "codemode", "Authorization": f"Bearer {fin_token}"},
                json={
                    "jsonrpc": "2.0",
                    "id": 3,
                    "method": "tools/call",
                    "params": {"name": "execute_code", "arguments": {"code": script}},
                },
            )
            assert cm_exec.status_code == 200
            exec_sc = cm_exec.json()["result"]["structuredContent"]
            assert exec_sc["ok"]
            assert exec_sc["subcall_count"] == 3
            assert exec_sc["result"] == [
                {"customer_id": "CUST-1003", "delta": 450.0},
                {"customer_id": "CUST-1024", "delta": -320.0},
            ]
            assert exec_sc["raw_upstream_bytes"] > exec_sc["returned_bytes"]

            # Inspect persisted audit run & token hops
            audit = await gw.get(f"/audit/runs/{exec_sc['run_id']}")
            assert audit.status_code == 200
            run_doc = audit.json()
            assert run_doc["subcall_count"] == 3
            assert len(run_doc["token_hops"]) == 3

            # 4. Traditional mode exposes all accessible tools directly and rejects execute_code
            trad_list = await gw.post(
                "/mcp",
                headers={
                    "X-Admin-Force-Mode": "traditional",
                    "Authorization": f"Bearer {fin_token}",
                },
                json={"jsonrpc": "2.0", "id": 4, "method": "tools/list"},
            )
            trad_names = {t["name"] for t in trad_list.json()["result"]["tools"]}
            assert "snowflake_ledger__query_debt_ledger" in trad_names
            assert "billing_legacy_eu__list_eu_invoices" in trad_names
            assert "execute_code" not in trad_names

            trad_direct = await gw.post(
                "/mcp",
                headers={
                    "Authorization": f"Bearer {fin_token}",
                    "X-Admin-Force-Mode": "traditional",
                },
                json={
                    "jsonrpc": "2.0",
                    "id": 5,
                    "method": "tools/call",
                    "params": {
                        "name": "billing_legacy_eu__get_eu_customer",
                        "arguments": {"customer_id": "CUST-1003"},
                    },
                },
            )
            assert trad_direct.status_code == 200
            assert trad_direct.json()["result"]["structuredContent"]["result"]["region"] == "EU"

            # 5. Hybrid mode exposes mutating tool directly and keeps it out of execute_code
            hyb_list = await gw.post(
                "/mcp",
                headers={"X-Admin-Force-Mode": "hybrid", "Authorization": f"Bearer {ops_token}"},
                json={"jsonrpc": "2.0", "id": 6, "method": "tools/list"},
            )
            hyb_names = [t["name"] for t in hyb_list.json()["result"]["tools"]]
            assert hyb_names == [
                "list_servers",
                "search_tools",
                "execute_code",
                "ops_ticketing__create_collection_case",
            ]

            hyb_sandbox_write = await gw.post(
                "/mcp",
                headers={"X-Admin-Force-Mode": "hybrid", "Authorization": f"Bearer {ops_token}"},
                json={
                    "jsonrpc": "2.0",
                    "id": 7,
                    "method": "tools/call",
                    "params": {
                        "name": "execute_code",
                        "arguments": {
                            "code": (
                                "await ops_ticketing__create_collection_case("
                                "customer_id='CUST-1003', reason='Test')"
                            )
                        },
                    },
                },
            )
            assert not hyb_sandbox_write.json()["result"]["structuredContent"]["ok"]

            hyb_direct_write = await gw.post(
                "/mcp",
                headers={"X-Admin-Force-Mode": "hybrid", "Authorization": f"Bearer {ops_token}"},
                json={
                    "jsonrpc": "2.0",
                    "id": 8,
                    "method": "tools/call",
                    "params": {
                        "name": "ops_ticketing__create_collection_case",
                        "arguments": {"customer_id": "CUST-1003", "reason": "Overdue variance"},
                    },
                },
            )
            write_sc = hyb_direct_write.json()["result"]["structuredContent"]
            assert write_sc["ok"]
            assert write_sc["result"]["case_id"] == "CASE-0001"

    await backend_client.aclose()
