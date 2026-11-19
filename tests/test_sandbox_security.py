"""Phase 2 tests: Monty sandbox isolation, resource caps, call budget, and credential safety."""

from __future__ import annotations

import httpx

from examples.mock_backends import create_mock_backends_app
from gateway.auth import LocalDevKeyManager
from gateway.config import GatewaySettings
from gateway.sandbox import LocalMontyBackend
from gateway.server import create_gateway_app
from gateway.state import SQLiteAuditStore


async def test_monty_blocks_fs_env_network_and_timeouts() -> None:
    sandbox = LocalMontyBackend(min_processes=1, max_processes=2)
    try:
        fs_res = await sandbox.execute(
            'open("/etc/passwd").read()',
            external_functions={},
            timeout_seconds=2.0,
        )
        assert not fs_res.ok
        assert fs_res.error_type == "MontyRuntimeError"

        env_res = await sandbox.execute(
            "import os\nos.environ",
            external_functions={},
            timeout_seconds=2.0,
        )
        assert not env_res.ok

        net_res = await sandbox.execute(
            "import socket\nsocket.socket()",
            external_functions={},
            timeout_seconds=2.0,
        )
        assert not net_res.ok

        loop_res = await sandbox.execute(
            "while True:\n    pass",
            external_functions={},
            timeout_seconds=0.5,
        )
        assert not loop_res.ok
        assert "timeout" in (loop_res.error or "").lower()
    finally:
        await sandbox.close()


async def test_call_budget_exhaustion_and_token_unreachability(tmp_path) -> None:
    settings = GatewaySettings(
        auth_mode="dev",
        max_calls_per_execution=3,
        sqlite_path=str(tmp_path / "gw.db"),
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

    caller_token = dev_keys.issue_user_token(persona="finance_analyst")
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://gw"
        ) as gw_client:
            # Exceeding max_calls_per_execution=3 must abort cleanly
            budget_resp = await gw_client.post(
                "/mcp",
                headers={"Authorization": f"Bearer {caller_token}"},
                json={
                    "jsonrpc": "2.0",
                    "id": 1,
                    "method": "tools/call",
                    "params": {
                        "name": "execute_code",
                        "arguments": {
                            "code": (
                                "rows = []\n"
                                "for _ in range(5):\n"
                                "    rows.append(await snowflake_ledger__query_debt_ledger())\n"
                                "len(rows)"
                            )
                        },
                    },
                },
            )
            assert budget_resp.status_code == 200
            sc = budget_resp.json()["result"]["structuredContent"]
            assert not sc["ok"]
            assert "budget" in sc["error"].lower()

            # Raw bearer tokens and host credentials are unreachable from sandbox scope
            probe_resp = await gw_client.post(
                "/mcp",
                headers={"Authorization": f"Bearer {caller_token}"},
                json={
                    "jsonrpc": "2.0",
                    "id": 2,
                    "method": "tools/call",
                    "params": {
                        "name": "execute_code",
                        "arguments": {
                            "code": (
                                "bal = await snowflake_ledger__get_customer_balance("
                                "customer_id='CUST-1003')\n"
                                "{'locals': list(locals().keys()), 'customer': bal['customer_id']}"
                            )
                        },
                    },
                },
            )
            assert probe_resp.status_code == 200
            probe_sc = probe_resp.json()["result"]["structuredContent"]
            assert probe_sc["ok"]
            assert probe_sc["result"]["customer"] == "CUST-1003"
            serialized = str(probe_sc)
            assert caller_token not in serialized
            assert "eyJ" not in serialized

            # Introspection builtins like globals() are also blocked by Monty
            globals_resp = await gw_client.post(
                "/mcp",
                headers={"Authorization": f"Bearer {caller_token}"},
                json={
                    "jsonrpc": "2.0",
                    "id": 3,
                    "method": "tools/call",
                    "params": {"name": "execute_code", "arguments": {"code": "globals()"}},
                },
            )
            assert not globals_resp.json()["result"]["structuredContent"]["ok"]
    await backend_client.aclose()
