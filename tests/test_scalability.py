"""Phase 5 tests: Remote WS sandbox worker, multi-user cache isolation, rate limits, and Redis."""

from __future__ import annotations

import asyncio
import json
import socket
from typing import Any

import httpx
import uvicorn
from fastapi import FastAPI, Header, HTTPException

from examples.mock_backends import create_mock_backends_app
from gateway.auth import LocalDevKeyManager
from gateway.config import GatewaySettings
from gateway.sandbox_worker import create_sandbox_worker_app
from gateway.server import create_gateway_app
from gateway.state import RedisCacheBackend, SQLiteAuditStore


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


async def test_remote_monty_worker_reverse_rpc_isolation(tmp_path) -> None:
    port = _free_port()
    worker_app = create_sandbox_worker_app(min_processes=1, max_processes=2)
    config = uvicorn.Config(worker_app, host="127.0.0.1", port=port, log_level="warning")
    server = uvicorn.Server(config)
    server_task = asyncio.create_task(server.serve())

    try:
        async with httpx.AsyncClient(timeout=2.0) as probe:
            for _ in range(40):
                try:
                    r = await probe.get(f"http://127.0.0.1:{port}/health")
                    if r.status_code == 200:
                        break
                except httpx.HTTPError:
                    await asyncio.sleep(0.05)

        settings = GatewaySettings(
            auth_mode="dev",
            sandbox_backend="remote",
            sandbox_worker_url=f"ws://127.0.0.1:{port}/ws/execute",
            sqlite_path=str(tmp_path / "remote.db"),
        )
        dev_keys = LocalDevKeyManager(issuer=settings.oidc_issuer)
        backends_app = create_mock_backends_app(
            public_key_pem=dev_keys.public_pem, expected_issuer=settings.oidc_issuer
        )
        backend_client = httpx.AsyncClient(
            transport=httpx.ASGITransport(app=backends_app), base_url="http://127.0.0.1:8001"
        )
        gw_app = create_gateway_app(
            settings=settings,
            store=SQLiteAuditStore(settings.sqlite_path),
            dev_keys=dev_keys,
            http_client=backend_client,
        )
        fin_token = dev_keys.issue_user_token(persona="finance_analyst")

        async with gw_app.router.lifespan_context(gw_app):
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=gw_app), base_url="http://gw"
            ) as gw:
                # 1. Parallel reconciliation runs inside isolated remote worker via WS reverse-RPC
                script = (
                    "import asyncio\n"
                    "ledger, eu, us = await asyncio.gather(\n"
                    "    snowflake_ledger__query_debt_ledger(status='overdue'),\n"
                    "    billing_legacy_eu__list_eu_invoices(status='overdue'),\n"
                    "    billing_legacy_us__list_us_invoices(status='overdue'),\n"
                    ")\n"
                    "inv_map = {i['customer_id']: i['amount'] "
                    "for i in eu['invoices'] + us['invoices']}\n"
                    "[\n"
                    "    {'customer_id': r['customer_id'], "
                    "'delta': round(r['ledger_balance'] - inv_map[r['customer_id']], 2)}\n"
                    "    for r in ledger['rows']\n"
                    "    if r['customer_id'] in inv_map "
                    "and r['ledger_balance'] != inv_map[r['customer_id']]\n"
                    "]"
                )
                res = await gw.post(
                    "/mcp?mode=codemode",
                    headers={"Authorization": f"Bearer {fin_token}"},
                    json={
                        "jsonrpc": "2.0",
                        "id": 1,
                        "method": "tools/call",
                        "params": {"name": "execute_code", "arguments": {"code": script}},
                    },
                )
                assert res.status_code == 200
                sc = res.json()["result"]["structuredContent"]
                assert sc["ok"], sc
                assert sc["subcall_count"] == 3
                assert sc["result"] == [
                    {"customer_id": "CUST-1003", "delta": 450.0},
                    {"customer_id": "CUST-1024", "delta": -320.0},
                ]
                assert fin_token not in str(sc)
                assert "eyJ" not in str(sc)

                # 2. Unauthorized tool is never bound into remote worker
                denied = await gw.post(
                    "/mcp?mode=codemode",
                    headers={"Authorization": f"Bearer {fin_token}"},
                    json={
                        "jsonrpc": "2.0",
                        "id": 2,
                        "method": "tools/call",
                        "params": {
                            "name": "execute_code",
                            "arguments": {"code": "await ops_ticketing__list_terminal_incidents()"},
                        },
                    },
                )
                assert not denied.json()["result"]["structuredContent"]["ok"]

        await backend_client.aclose()
    finally:
        server.should_exit = True
        await server_task


async def test_concurrent_multi_user_cache_isolation_and_rate_limit(tmp_path) -> None:
    import jwt
    import yaml

    settings = GatewaySettings(
        auth_mode="dev",
        allowed_tenants=["tenant-alpha", "tenant-beta"],
        rate_limit_per_minute=20,
        sqlite_path=str(tmp_path / "iso.db"),
    )
    dev_keys = LocalDevKeyManager(issuer=settings.oidc_issuer)

    whoami_app = FastAPI()

    @whoami_app.post("/whoami/mcp")
    async def whoami_mcp(request: dict, authorization: str | None = Header(default=None)):
        if not authorization or not authorization.startswith("Bearer "):
            raise HTTPException(status_code=401, detail="Missing token")
        tok = authorization.split(" ", 1)[1]
        claims = jwt.decode(
            tok,
            key=dev_keys.public_pem,
            algorithms=["RS256"],
            audience="api://whoami-srv",
            issuer=settings.oidc_issuer,
        )
        if request.get("method") == "tools/list":
            return {
                "jsonrpc": "2.0",
                "id": request["id"],
                "result": {
                    "tools": [
                        {
                            "name": "whoami",
                            "description": "Echo delegated identity",
                            "inputSchema": {"type": "object", "properties": {}},
                        }
                    ]
                },
            }
        data = {"sub": claims["sub"], "tid": claims["tid"]}
        return {
            "jsonrpc": "2.0",
            "id": request["id"],
            "result": {"content": [{"type": "text", "text": json.dumps(data)}]},
        }

    cfg_file = tmp_path / "upstreams.yaml"
    cfg_file.write_text(
        yaml.safe_dump(
            {
                "upstreams": [
                    {
                        "name": "whoami_srv",
                        "type": "mcp_http",
                        "url": "http://whoami/whoami/mcp",
                        "auth_type": "delegated_token",
                        "downstream_audience": "api://whoami-srv",
                        "downstream_scope": "whoami.read",
                    }
                ]
            }
        ),
        encoding="utf-8",
    )
    settings.upstreams_config_path = str(cfg_file)
    backend_client = httpx.AsyncClient(
        transport=httpx.ASGITransport(app=whoami_app), base_url="http://whoami"
    )
    app = create_gateway_app(
        settings=settings,
        store=SQLiteAuditStore(settings.sqlite_path),
        dev_keys=dev_keys,
        http_client=backend_client,
    )

    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://gw"
        ) as gw:
            async def _run_user(idx: int) -> tuple[str, str, dict[str, Any]]:
                tenant = "tenant-alpha" if idx % 2 == 0 else "tenant-beta"
                sub = f"concurrent-user-{idx:02d}"
                tok = dev_keys.issue_user_token(
                    persona="finance_analyst",
                    overrides={"sub": sub, "tenant_id": tenant},
                )
                resp = await gw.post(
                    "/mcp?mode=codemode",
                    headers={"Authorization": f"Bearer {tok}"},
                    json={
                        "jsonrpc": "2.0",
                        "id": idx,
                        "method": "tools/call",
                        "params": {
                            "name": "execute_code",
                            "arguments": {"code": "await whoami_srv__whoami()"},
                        },
                    },
                )
                return sub, tenant, resp.json()["result"]["structuredContent"]

            results = await asyncio.gather(*[_run_user(i) for i in range(20)])
            for expected_sub, expected_tid, sc in results:
                assert sc["ok"]
                assert sc["result"] == {"sub": expected_sub, "tid": expected_tid}

            # Verify rate_limit_per_minute=20 blocks 21st call for same user within 60s
            rl_tok = dev_keys.issue_user_token(
                persona="finance_analyst",
                overrides={"sub": "rate-limited-user", "tenant_id": "tenant-alpha"},
            )
            rl_resp = await gw.post(
                "/mcp?mode=codemode",
                headers={"Authorization": f"Bearer {rl_tok}"},
                json={
                    "jsonrpc": "2.0",
                    "id": 99,
                    "method": "tools/call",
                    "params": {
                        "name": "execute_code",
                        "arguments": {
                            "code": (
                                "for _ in range(25):\n"
                                "    await whoami_srv__whoami()\n"
                            )
                        },
                    },
                },
            )
            rl_sc = rl_resp.json()["result"]["structuredContent"]
            assert not rl_sc["ok"]
            assert "rate limit" in rl_sc["error"].lower()

    await backend_client.aclose()


async def test_redis_cache_backend_with_fake_client() -> None:
    class _FakeRedis:
        def __init__(self) -> None:
            self.kv: dict[str, bytes] = {}
            self.zsets: dict[str, dict[str, float]] = {}
            self.closed = False

        async def get(self, key: str) -> bytes | None:
            return self.kv.get(key)

        async def set(self, key: str, value: str, ex: int | None = None) -> None:
            _ = ex
            self.kv[key] = value.encode("utf-8")

        async def delete(self, key: str) -> None:
            self.kv.pop(key, None)

        async def zremrangebyscore(self, key: str, min_score: float, max_score: float) -> None:
            bucket = self.zsets.get(key, {})
            self.zsets[key] = {
                m: s for m, s in bucket.items() if not (min_score <= s <= max_score)
            }

        async def zadd(self, key: str, mapping: dict[str, float]) -> None:
            self.zsets.setdefault(key, {}).update(mapping)

        async def zcard(self, key: str) -> int:
            return len(self.zsets.get(key, {}))

        async def expire(self, key: str, ttl: int) -> None:
            _ = (key, ttl)

        async def aclose(self) -> None:
            self.closed = True

    fake = _FakeRedis()
    cache = RedisCacheBackend(client=fake)
    await cache.set("k1", "tok-val", ttl_seconds=60)
    assert await cache.get("k1") == "tok-val"
    await cache.delete("k1")
    assert await cache.get("k1") is None

    assert await cache.increment_window("rl:u1", 60) == 1
    assert await cache.increment_window("rl:u1", 60) == 2
    assert await cache.increment_window("rl:u1", 60) == 3

    await cache.close()
    assert fake.closed
