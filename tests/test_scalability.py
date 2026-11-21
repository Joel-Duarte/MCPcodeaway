"""Phase 5 tests: Remote WS sandbox worker, multi-user cache isolation, rate limits, and Redis."""

from __future__ import annotations

import asyncio
import json
import random
import socket
from typing import Any

import httpx
import pytest
import uvicorn
from fastapi import FastAPI, Header, HTTPException

from examples.mock_backends import create_mock_backends_app
from gateway.auth import LocalDevKeyManager
from gateway.config import GatewaySettings
from gateway.mcp_protocol import request_headers, request_meta
from gateway.sandbox import RemoteMontyWorkerBackend
from gateway.sandbox_worker import create_sandbox_worker_app
from gateway.server import create_gateway_app
from gateway.state import InMemoryCacheBackend, RedisCacheBackend, SQLiteAuditStore


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


async def test_remote_monty_worker_reverse_rpc_isolation(tmp_path) -> None:
    port = _free_port()
    worker_app = create_sandbox_worker_app(
        min_processes=1,
        max_processes=2,
        max_concurrent_executions=2,
    )
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

                remote_backend = RemoteMontyWorkerBackend(f"ws://127.0.0.1:{port}/ws/execute")
                callbacks_entered = 0
                callbacks_lock = asyncio.Lock()
                callbacks_ready = asyncio.Event()

                def _identity_probe(marker: str):
                    async def _probe() -> str:
                        nonlocal callbacks_entered
                        async with callbacks_lock:
                            callbacks_entered += 1
                            if callbacks_entered == 2:
                                callbacks_ready.set()
                        await asyncio.wait_for(callbacks_ready.wait(), timeout=5.0)
                        return marker

                    return _probe

                result_a, result_b = await asyncio.gather(
                    remote_backend.execute(
                        "await identity_probe()",
                        external_functions={"identity_probe": _identity_probe("tenant-a")},
                        timeout_seconds=2.0,
                    ),
                    remote_backend.execute(
                        "await identity_probe()",
                        external_functions={"identity_probe": _identity_probe("tenant-b")},
                        timeout_seconds=2.0,
                    ),
                )
                assert result_a.ok and result_a.result == "tenant-a"
                assert result_b.ok and result_b.result == "tenant-b"

                callback_started = asyncio.Event()
                callback_cancelled = asyncio.Event()

                async def _wait_until_cancelled() -> str:
                    callback_started.set()
                    try:
                        await asyncio.Event().wait()
                    except asyncio.CancelledError:
                        callback_cancelled.set()
                        raise
                    return "unreachable"

                cancelled_execution = asyncio.create_task(
                    remote_backend.execute(
                        "await wait_until_cancelled()",
                        external_functions={"wait_until_cancelled": _wait_until_cancelled},
                        timeout_seconds=4.0,
                    )
                )
                await asyncio.wait_for(callback_started.wait(), timeout=3.0)
                cancelled_execution.cancel()
                with pytest.raises(asyncio.CancelledError):
                    await cancelled_execution
                await asyncio.wait_for(callback_cancelled.wait(), timeout=1.0)

                holds_started = 0
                holds_lock = asyncio.Lock()
                both_slots_held = asyncio.Event()
                release_holds = asyncio.Event()

                async def _hold_slot() -> str:
                    nonlocal holds_started
                    async with holds_lock:
                        holds_started += 1
                        if holds_started == 2:
                            both_slots_held.set()
                    await asyncio.wait_for(release_holds.wait(), timeout=5.0)
                    return "released"

                holding_executions = asyncio.gather(
                    *[
                        remote_backend.execute(
                            "await hold_slot()",
                            external_functions={"hold_slot": _hold_slot},
                            timeout_seconds=4.0,
                        )
                        for _ in range(2)
                    ]
                )
                try:
                    await asyncio.wait_for(both_slots_held.wait(), timeout=5.0)
                    overloaded = await remote_backend.execute(
                        "1", external_functions={}, timeout_seconds=2.0
                    )
                    assert not overloaded.ok
                    assert "capacity" in (overloaded.error or "").lower()
                finally:
                    release_holds.set()
                held_results = await holding_executions
                assert all(result.ok and result.result == "released" for result in held_results)

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


@pytest.mark.parametrize("schedule_seed", [7, 29, 101], ids=lambda seed: f"schedule-{seed}")
async def test_concurrent_multi_user_cache_isolation_and_rate_limit(
    tmp_path, schedule_seed: int
) -> None:
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
    simultaneous_calls = 0
    simultaneous_calls_lock = asyncio.Lock()
    first_wave_entered_upstream = asyncio.Event()

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
                            "name": "whoami",
                            "description": "Echo delegated identity",
                            "inputSchema": {"type": "object", "properties": {}},
                        }
                    ]
                },
            }
        data = {"sub": claims["sub"], "tid": claims["tid"]}
        nonlocal simultaneous_calls
        async with simultaneous_calls_lock:
            simultaneous_calls += 1
            if simultaneous_calls >= 4:
                first_wave_entered_upstream.set()
        await asyncio.wait_for(first_wave_entered_upstream.wait(), timeout=5.0)
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
        cache=InMemoryCacheBackend(),
    )
    shared_cache = app.state.runtime.cache
    replica_app = create_gateway_app(
        settings=settings,
        store=SQLiteAuditStore(settings.sqlite_path),
        dev_keys=dev_keys,
        http_client=backend_client,
        cache=shared_cache,
    )
    request_slots = asyncio.Semaphore(4)
    schedule_rng = random.Random(schedule_seed)
    launch_delays = [schedule_rng.uniform(0.0, 0.02) for _ in range(20)]

    async with (
        app.router.lifespan_context(app),
        replica_app.router.lifespan_context(replica_app),
    ):
        async with (
            httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app), base_url="http://gw-a"
            ) as gw_a,
            httpx.AsyncClient(
                transport=httpx.ASGITransport(app=replica_app), base_url="http://gw-b"
            ) as gw_b,
        ):

            async def _run_user(idx: int) -> tuple[str, str, dict[str, Any]]:
                await asyncio.sleep(launch_delays[idx])
                tenant = "tenant-alpha" if idx % 2 == 0 else "tenant-beta"
                sub = "shared-subject" if idx < 2 else f"concurrent-user-{idx:02d}"
                tok = dev_keys.issue_user_token(
                    persona="finance_analyst",
                    overrides={"sub": sub, "tenant_id": tenant},
                )
                async with request_slots:
                    gw = gw_a if idx % 2 == 0 else gw_b
                    resp = await gw.post(
                        "/mcp?mode=codemode",
                        headers={
                            "Authorization": f"Bearer {tok}",
                            **request_headers("tools/call", tool_name="execute_code"),
                        },
                        json={
                            "jsonrpc": "2.0",
                            "id": idx,
                            "method": "tools/call",
                            "params": {
                                "_meta": request_meta(),
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

            rl_tok = dev_keys.issue_user_token(
                persona="finance_analyst",
                overrides={"sub": "rate-limited-user", "tenant_id": "tenant-alpha"},
            )
            rl_resp = await gw_a.post(
                "/mcp?mode=codemode",
                headers={
                    "Authorization": f"Bearer {rl_tok}",
                    **request_headers("tools/call", tool_name="execute_code"),
                },
                json={
                    "jsonrpc": "2.0",
                    "id": 99,
                    "method": "tools/call",
                    "params": {
                        "_meta": request_meta(),
                        "name": "execute_code",
                        "arguments": {
                            "code": ("for _ in range(25):\n    await whoami_srv__whoami()\n")
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
            self.zsets[key] = {m: s for m, s in bucket.items() if not (min_score <= s <= max_score)}

        async def zadd(self, key: str, mapping: dict[str, float]) -> None:
            self.zsets.setdefault(key, {}).update(mapping)

        async def zcard(self, key: str) -> int:
            return len(self.zsets.get(key, {}))

        async def expire(self, key: str, ttl: int) -> None:
            _ = (key, ttl)

        async def eval(self, script: str, numkeys: int, *args: object) -> int:
            _ = script
            assert numkeys == 1
            key, now, expires, capacity, token, ttl = args
            _ = ttl
            bucket = self.zsets.setdefault(str(key), {})
            self.zsets[str(key)] = {
                member: score for member, score in bucket.items() if score > float(now)
            }
            if len(self.zsets[str(key)]) >= int(capacity):
                return 0
            self.zsets[str(key)][str(token)] = float(expires)
            return 1

        async def zrem(self, key: str, token: str) -> None:
            self.zsets.get(key, {}).pop(token, None)

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
    assert await cache.acquire_permit("upstream:crm", capacity=1, lease_seconds=30, token="one")
    assert not await cache.acquire_permit("upstream:crm", capacity=1, lease_seconds=30, token="two")
    await cache.release_permit("upstream:crm", "one")
    assert await cache.acquire_permit("upstream:crm", capacity=1, lease_seconds=30, token="two")

    await cache.close()
    assert fake.closed


async def test_in_memory_upstream_concurrency_permits_are_shared_and_released() -> None:
    from gateway.state import InMemoryCacheBackend

    cache = InMemoryCacheBackend()
    assert await cache.acquire_permit("upstream:crm", capacity=2, lease_seconds=30, token="a")
    assert await cache.acquire_permit("upstream:crm", capacity=2, lease_seconds=30, token="b")
    assert not await cache.acquire_permit("upstream:crm", capacity=2, lease_seconds=30, token="c")
    await cache.release_permit("upstream:crm", "a")
    assert await cache.acquire_permit("upstream:crm", capacity=2, lease_seconds=30, token="c")


async def test_redis_rate_limiter_reconnects_after_a_lost_connection(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import sys
    from types import ModuleType

    class RedisConnectionError(Exception):
        pass

    redis_module = ModuleType("redis")
    exceptions_module = ModuleType("redis.exceptions")
    exceptions_module.ConnectionError = RedisConnectionError
    redis_module.exceptions = exceptions_module
    monkeypatch.setitem(sys.modules, "redis", redis_module)
    monkeypatch.setitem(sys.modules, "redis.exceptions", exceptions_module)

    class _FlakyRedis:
        def __init__(self) -> None:
            self.zsets: dict[str, dict[str, float]] = {}
            self.fail_next_command = True
            self.reconnects = 0
            self.connection_pool = self

        async def disconnect(self, *, inuse_connections: bool) -> None:
            assert inuse_connections is True
            self.fail_next_command = False
            self.reconnects += 1

        async def zremrangebyscore(self, key: str, min_score: float, max_score: float) -> None:
            if self.fail_next_command:
                raise RedisConnectionError("stale Redis connection")
            bucket = self.zsets.get(key, {})
            self.zsets[key] = {
                member: score
                for member, score in bucket.items()
                if not min_score <= score <= max_score
            }

        async def zadd(self, key: str, mapping: dict[str, float]) -> None:
            self.zsets.setdefault(key, {}).update(mapping)

        async def zcard(self, key: str) -> int:
            return len(self.zsets.get(key, {}))

        async def expire(self, key: str, ttl: int) -> None:
            _ = (key, ttl)

    client = _FlakyRedis()
    cache = RedisCacheBackend(client=client)
    assert await cache.increment_window("rl:user", 60) == 1
    assert client.reconnects == 1
    assert len(client.zsets["rl:user"]) == 1
