"""Opt-in real PostgreSQL interruption and cross-process write-journal tests."""

from __future__ import annotations

import asyncio
import multiprocessing
import os
import secrets
import socket
import subprocess
import time
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

import asyncpg
import pytest
import uvicorn
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from gateway.state import PostgresAuditStore

pytestmark = [
    pytest.mark.integration,
    pytest.mark.postgres,
    pytest.mark.skipif(
        os.getenv("MCP_RUN_POSTGRES_PROCESS_TESTS") != "1",
        reason="set MCP_RUN_POSTGRES_PROCESS_TESTS=1 to run cross-process PostgreSQL tests",
    ),
]
ROOT = Path(__file__).resolve().parents[1]
COMPOSE_FILE = ROOT / "docker-compose.acceptance.yml"
COMPOSE_PROJECT = os.getenv("MCP_TEST_POSTGRES_COMPOSE_PROJECT", "mcpcodeaway-local-acceptance")
TIMEOUT = 20


class ClaimBarrierStore:
    """Coordinate contenders, then delegate every journal operation to PostgreSQL."""

    def __init__(self, store: PostgresAuditStore, messages: Any, release: Any) -> None:
        self.store = store
        self.messages = messages
        self.release = release

    def __getattr__(self, name: str) -> Any:
        return getattr(self.store, name)

    async def prepare_sensitive_write_audit(self, operation: dict[str, Any]):
        row, created = await self.store.prepare_sensitive_write_audit(operation)
        self.messages.put({"kind": "prepared", "operation_id": row["operation_id"]})
        return row, created

    async def claim_sensitive_write_audit(self, operation_id: str, started_at: str) -> bool:
        self.messages.put({"kind": "claim_ready", "operation_id": operation_id})
        if not await asyncio.to_thread(self.release.wait, TIMEOUT):
            raise TimeoutError("parent did not release the deterministic claim barrier")
        return await self.store.claim_sensitive_write_audit(operation_id, started_at)


def dispatch_worker(
    dsn: str,
    upstream_url: str,
    correlation_id: str,
    messages: Any,
    invocation_gate: Any,
    claim_gate: Any,
    result_queue: Any,
    worker_id: str,
) -> None:
    """Run one real GatewayRuntime dispatcher in an independently spawned OS process."""

    async def run() -> None:
        from gateway.auth import CallerIdentity
        from gateway.config import GatewaySettings, UpstreamConfig
        from gateway.observability import clear_request_context, set_request_context
        from gateway.server import GatewayRuntime
        from gateway.upstream_clients import DiscoveredTool

        settings = GatewaySettings(
            auth_mode="dev",
            app_env="sandbox",
            postgres_dsn=dsn,
            sqlite_path=f"/tmp/mcpcodeaway-unused-{worker_id}.db",
            ssrf_protection_enabled=True,
            upstreams_config_path="/tmp/mcpcodeaway-unused-upstreams.yaml",
        )
        runtime = GatewayRuntime(settings=settings)
        try:
            await runtime.store.init()
            runtime.dispatcher.audit_store = ClaimBarrierStore(runtime.store, messages, claim_gate)
            actual_call_tool = runtime.client_manager.call_tool

            async def record_upstream_failure(*args: Any, **kwargs: Any) -> Any:
                try:
                    return await actual_call_tool(*args, **kwargs)
                except Exception as exc:
                    messages.put(
                        {"kind": "upstream_error", "type": type(exc).__name__, "text": str(exc)}
                    )
                    raise

            runtime.client_manager.call_tool = record_upstream_failure
            upstream = UpstreamConfig(
                name="acceptance_upstream",
                type="mcp_http",
                url=upstream_url,
                auth_type="none",
                allow_all_callers=True,
                allowed_operations=["write_item"],
                tool_effects={"write_item": "write"},
                mutating_tools=["write_item"],
                request_timeout_seconds=8,
            )
            tool = DiscoveredTool(
                server=upstream.name,
                name="write_item",
                qualified_name="acceptance_upstream__write_item",
                input_schema={
                    "type": "object",
                    "properties": {"item": {"type": "string"}},
                    "required": ["item"],
                },
                mutating=True,
            )
            identity = CallerIdentity(
                sub="acceptance-caller",
                tenant_id="acceptance-tenant",
                roles=["admin"],
                scopes=["mcp.write"],
            )
            messages.put({"kind": "ready", "pid": os.getpid()})
            if not await asyncio.to_thread(invocation_gate.wait, TIMEOUT):
                raise TimeoutError("parent did not release invocation barrier")
            set_request_context(request_id=correlation_id)
            try:
                result, record = await runtime.dispatcher.invoke_tool(
                    identity, tool, upstream, {"item": "same-payload"}
                )
                result_queue.put(
                    {
                        "pid": os.getpid(),
                        "result": result,
                        "status": record.status,
                        "outcome_unknown": record.outcome_unknown,
                        "error_type": record.error_type,
                    }
                )
            except Exception as exc:
                result_queue.put(
                    {"pid": os.getpid(), "exception": type(exc).__name__, "message": str(exc)}
                )
            finally:
                clear_request_context()
        finally:
            await runtime.client_manager.close()
            await runtime.store.close()

    asyncio.run(run())


class LocalWriteServer:
    def __init__(self, calls: Any = None, release: Any = None, completed: Any = None) -> None:
        self.calls = calls
        self.release = release
        self.completed = completed
        self.app = FastAPI()

        @self.app.post("/mcp")
        async def call_tool(request: Request) -> JSONResponse:
            payload = await request.json()
            params = payload.get("params") or {}
            item = (params.get("arguments") or {}).get("item", "")
            if self.calls is not None:
                self.calls.put({"item": item, "pid": os.getpid()})
            if self.completed is not None:
                self.completed.put({"item": item, "pid": os.getpid()})
            if self.release is not None and not await asyncio.to_thread(self.release.wait, TIMEOUT):
                raise TimeoutError("parent did not release upstream write")
            return JSONResponse(
                {
                    "jsonrpc": "2.0",
                    "id": payload.get("id"),
                    "result": {
                        "structuredContent": {"written": item},
                        "content": [],
                        "isError": False,
                    },
                }
            )

        self.socket = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self.socket.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self.socket.bind(("127.0.0.1", 0))
        self.socket.listen(128)
        self.port = int(self.socket.getsockname()[1])
        self.uvicorn = uvicorn.Server(uvicorn.Config(self.app, log_level="error", lifespan="on"))
        self.task: asyncio.Task[None] | None = None

    async def start(self) -> None:
        self.task = asyncio.create_task(self.uvicorn.serve(sockets=[self.socket]))
        deadline = asyncio.get_running_loop().time() + 5
        while not self.uvicorn.started:
            if self.task.done():
                await self.task
            if asyncio.get_running_loop().time() >= deadline:
                raise TimeoutError("local MCP write service did not start")
            await asyncio.sleep(0.01)

    async def close(self) -> None:
        if self.task is not None and not self.task.done():
            self.uvicorn.should_exit = True
            try:
                await asyncio.wait_for(self.task, timeout=3)
            except TimeoutError:
                self.task.cancel()
                await asyncio.gather(self.task, return_exceptions=True)
        self.socket.close()


def compose(*args: str) -> None:
    subprocess.run(
        ["docker", "compose", "-f", str(COMPOSE_FILE), "-p", COMPOSE_PROJECT, *args],
        cwd=ROOT,
        text=True,
        capture_output=True,
        timeout=35,
        check=True,
    )


def db_down() -> None:
    compose("stop", "--timeout", "0", "postgres")


def db_up() -> None:
    compose("up", "-d", "--wait", "--wait-timeout", "30", "postgres")


@pytest.fixture
async def pg_env():
    dsn = os.getenv("MCP_TEST_POSTGRES_DSN")
    if not dsn:
        pytest.skip("MCP_TEST_POSTGRES_DSN is not set; use --with-postgres-processes")
    parsed_dsn = urlsplit(dsn)
    if (
        parsed_dsn.scheme not in {"postgres", "postgresql"}
        or parsed_dsn.hostname not in {"127.0.0.1", "localhost", "::1"}
        or parsed_dsn.username != "gateway_test"
        or parsed_dsn.password != "local_acceptance_only"
        or parsed_dsn.path != "/mcpcodeaway_acceptance"
    ):
        pytest.fail("Phase 2B tests only permit the isolated local acceptance PostgreSQL DSN")
    probe = PostgresAuditStore(dsn)
    try:
        await probe.init()
        assert await probe.health_check()
    except Exception as exc:
        await probe.close()
        pytest.skip(f"local PostgreSQL is unavailable: {type(exc).__name__}")
    await probe.close()
    yield dsn
    db_up()


def start_workers(ctx: Any, dsn: str, url: str, correlations: list[str]):
    messages, results, invocation_gate, claim_gate = (
        ctx.Queue(),
        ctx.Queue(),
        ctx.Event(),
        ctx.Event(),
    )
    processes = [
        ctx.Process(
            target=dispatch_worker,
            args=(
                dsn,
                url,
                correlation,
                messages,
                invocation_gate,
                claim_gate,
                results,
                f"worker-{index}-{secrets.token_hex(3)}",
            ),
        )
        for index, correlation in enumerate(correlations)
    ]
    for process in processes:
        process.start()
    return processes, messages, results, invocation_gate, claim_gate


def messages(queue: Any, count: int, kind: str) -> list[dict[str, Any]]:
    found = []
    deadline = time.monotonic() + TIMEOUT
    while len(found) < count:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise TimeoutError(f"timed out waiting for {count} {kind} messages")
        message = queue.get(timeout=remaining)
        if message["kind"] == kind:
            found.append(message)
    return found


def finish(processes: list[Any], result_queue: Any, count: int) -> list[dict[str, Any]]:
    output = []
    try:
        for _ in range(count):
            output.append(result_queue.get(timeout=TIMEOUT))
        for process in processes:
            process.join(TIMEOUT)
            if process.is_alive():
                process.terminate()
                process.join(3)
            assert process.exitcode == 0
    finally:
        for process in processes:
            if process.is_alive():
                process.terminate()
                process.join(3)
    return output


async def count_calls(queue: Any) -> list[dict[str, Any]]:
    found = []
    while True:
        try:
            found.append(queue.get_nowait())
        except Exception:
            return found


@pytest.mark.parametrize(
    "correlations,dispatch_count",
    [(["same-request", "same-request"], 1), (["request-a", "request-b"], 2)],
)
async def test_real_dispatcher_claims_across_two_os_processes(
    pg_env, correlations: list[str], dispatch_count: int
) -> None:
    ctx = multiprocessing.get_context("spawn")
    calls = ctx.Queue()
    server = LocalWriteServer(calls=calls)
    await server.start()
    prefix = f"process-{secrets.token_hex(8)}-"
    processes, msg_queue, result_queue, invocation_gate, claim_gate = start_workers(
        ctx, pg_env, f"http://127.0.0.1:{server.port}/mcp", [prefix + c for c in correlations]
    )
    store = PostgresAuditStore(pg_env)
    try:
        ready = await asyncio.to_thread(messages, msg_queue, 2, "ready")
        assert len({item["pid"] for item in ready}) == 2
        invocation_gate.set()
        await asyncio.to_thread(messages, msg_queue, 2, "claim_ready")
        claim_gate.set()
        results = await asyncio.to_thread(finish, processes, result_queue, 2)
        errors = []
        while True:
            try:
                message = msg_queue.get_nowait()
            except Exception:
                break
            if message["kind"] == "upstream_error":
                errors.append(message)
        assert len(await count_calls(calls)) == dispatch_count, {
            "results": results,
            "upstream_errors": errors,
        }
        if dispatch_count == 1:
            assert sum(item.get("status") == "ok" for item in results) == 1
            assert sum(item.get("status") == "error" for item in results) == 1
        else:
            assert sum(item.get("status") == "ok" for item in results) == 2
        await store.init()
        pool = await store._get_pool()
        rows = await pool.fetch(
            "SELECT status, correlation_id FROM sensitive_write_audits "
            "WHERE correlation_id LIKE $1 ORDER BY correlation_id",
            f"{prefix}%",
        )
        assert len(rows) == dispatch_count
        assert all(row["status"] == "succeeded" for row in rows)
        assert {row["correlation_id"] for row in rows} == {prefix + c for c in correlations}
        await pool.execute(
            "DELETE FROM sensitive_write_audits WHERE correlation_id LIKE $1", f"{prefix}%"
        )
    finally:
        claim_gate.set()
        for process in processes:
            if process.is_alive():
                process.terminate()
                process.join(3)
        await store.close()
        await server.close()


async def test_database_loss_before_preparation_blocks_dispatch(pg_env) -> None:
    ctx = multiprocessing.get_context("spawn")
    calls = ctx.Queue()
    server = LocalWriteServer(calls=calls)
    await server.start()
    request_id = f"before-{secrets.token_hex(8)}"
    processes, msg_queue, result_queue, invocation_gate, claim_gate = start_workers(
        ctx, pg_env, f"http://127.0.0.1:{server.port}/mcp", [request_id]
    )
    try:
        await asyncio.to_thread(messages, msg_queue, 1, "ready")
        check = await asyncpg.connect(pg_env)
        existing = await check.fetchrow(
            "SELECT status FROM sensitive_write_audits WHERE correlation_id=$1", request_id
        )
        await check.close()
        assert existing is None
        db_down()
        invocation_gate.set()
        result = (await asyncio.to_thread(finish, processes, result_queue, 1))[0]
        assert result.get("status") == "error"
        assert await count_calls(calls) == []
    finally:
        db_up()
        invocation_gate.set()
        claim_gate.set()
        for process in processes:
            if process.is_alive():
                process.terminate()
                process.join(3)
        await server.close()
    store = PostgresAuditStore(pg_env)
    await store.init()
    row = await (await store._get_pool()).fetchrow(
        "SELECT status FROM sensitive_write_audits WHERE correlation_id=$1", request_id
    )
    assert row is None
    await store.close()


async def test_database_loss_during_preparation_prevents_dispatch(pg_env) -> None:
    ctx = multiprocessing.get_context("spawn")
    calls = ctx.Queue()
    server = LocalWriteServer(calls=calls)
    await server.start()
    request_id = f"during-{secrets.token_hex(8)}"
    processes, msg_queue, result_queue, invocation_gate, claim_gate = start_workers(
        ctx, pg_env, f"http://127.0.0.1:{server.port}/mcp", [request_id]
    )
    connection = await asyncpg.connect(pg_env)
    try:
        await asyncio.to_thread(messages, msg_queue, 1, "ready")
        await connection.execute("BEGIN")
        await connection.execute("LOCK TABLE sensitive_write_audits IN ACCESS EXCLUSIVE MODE")
        invocation_gate.set()
        deadline = asyncio.get_running_loop().time() + TIMEOUT
        while True:
            waiting = await connection.fetchval(
                "SELECT EXISTS (SELECT 1 FROM pg_stat_activity WHERE wait_event_type='Lock' "
                "AND query ILIKE '%INSERT INTO sensitive_write_audits%')"
            )
            if waiting:
                break
            if asyncio.get_running_loop().time() >= deadline:
                raise TimeoutError("write process did not reach the journal preparation insert")
            await asyncio.sleep(0.02)
        db_down()
        result = (await asyncio.to_thread(finish, processes, result_queue, 1))[0]
        assert result.get("status") == "error"
        assert await count_calls(calls) == []
    finally:
        claim_gate.set()
        try:
            await connection.close()
        except Exception:
            pass
        db_up()
        for process in processes:
            if process.is_alive():
                process.terminate()
                process.join(3)
        await server.close()
    store = PostgresAuditStore(pg_env)
    await store.init()
    row = await (await store._get_pool()).fetchrow(
        "SELECT status FROM sensitive_write_audits WHERE correlation_id=$1", request_id
    )
    assert row is None
    await store.close()


async def test_database_loss_after_preparation_blocks_dispatch_and_recovers(pg_env) -> None:
    ctx = multiprocessing.get_context("spawn")
    calls = ctx.Queue()
    server = LocalWriteServer(calls=calls)
    await server.start()
    request_id = f"after-prepare-{secrets.token_hex(8)}"
    processes, msg_queue, result_queue, invocation_gate, claim_gate = start_workers(
        ctx, pg_env, f"http://127.0.0.1:{server.port}/mcp", [request_id]
    )
    try:
        await asyncio.to_thread(messages, msg_queue, 1, "ready")
        invocation_gate.set()
        prepared = await asyncio.to_thread(messages, msg_queue, 1, "prepared")
        assert prepared
        await asyncio.to_thread(messages, msg_queue, 1, "claim_ready")
        check = await asyncpg.connect(pg_env)
        before = await check.fetchrow(
            "SELECT status FROM sensitive_write_audits WHERE correlation_id=$1", request_id
        )
        await check.close()
        assert before is not None and before["status"] == "prepared"
        db_down()
        claim_gate.set()
        result = (await asyncio.to_thread(finish, processes, result_queue, 1))[0]
        assert result.get("status") == "error"
        assert await count_calls(calls) == []
    finally:
        db_up()
        claim_gate.set()
        for process in processes:
            if process.is_alive():
                process.terminate()
                process.join(3)
        await server.close()
    store = PostgresAuditStore(pg_env)
    await store.init()
    pool = await store._get_pool()
    row = await pool.fetchrow(
        "SELECT status FROM sensitive_write_audits WHERE correlation_id=$1", request_id
    )
    assert row is not None and row["status"] == "prepared"
    await pool.execute("DELETE FROM sensitive_write_audits WHERE correlation_id=$1", request_id)
    await store.close()


async def test_database_loss_after_upstream_dispatch_is_not_replayed_after_recovery(
    pg_env, capfd
) -> None:
    ctx = multiprocessing.get_context("spawn")
    calls, upstream_gate = ctx.Queue(), ctx.Event()
    server = LocalWriteServer(calls=calls, release=upstream_gate)
    await server.start()
    request_id = f"after-dispatch-{secrets.token_hex(8)}"
    processes, msg_queue, result_queue, invocation_gate, claim_gate = start_workers(
        ctx, pg_env, f"http://127.0.0.1:{server.port}/mcp", [request_id]
    )
    try:
        await asyncio.to_thread(messages, msg_queue, 1, "ready")
        invocation_gate.set()
        await asyncio.to_thread(messages, msg_queue, 1, "claim_ready")
        claim_gate.set()
        dispatched = await asyncio.to_thread(calls.get, True, TIMEOUT)
        assert dispatched["item"] == "same-payload"
        check = await asyncpg.connect(pg_env)
        before = await check.fetchrow(
            "SELECT status FROM sensitive_write_audits WHERE correlation_id=$1", request_id
        )
        await check.close()
        assert before is not None and before["status"] == "executing"
        db_down()
        upstream_gate.set()
        first_result = (await asyncio.to_thread(finish, processes, result_queue, 1))[0]
        assert first_result.get("status") == "error"
        assert first_result.get("outcome_unknown") is True
        assert first_result.get("result", {}).get("_dispatch_error")
        assert "may have completed" in first_result["result"]["_dispatch_error"]
        logs = capfd.readouterr().out
        assert "audit.sensitive_write_status_failed" in logs
        assert '"status": "outcome_unknown"' in logs
    finally:
        upstream_gate.set()
        db_up()
        claim_gate.set()
        for process in processes:
            if process.is_alive():
                process.terminate()
                process.join(3)
        await server.close()

    store = PostgresAuditStore(pg_env)
    await store.init()
    pool = await store._get_pool()
    row = await pool.fetchrow(
        "SELECT status, request_digest, caller_sub, tenant_id, "
        "to_jsonb(sensitive_write_audits)::text AS journal_json "
        "FROM sensitive_write_audits WHERE correlation_id=$1",
        request_id,
    )
    assert row is not None
    assert row["status"] == "executing"
    assert row["tenant_id"] == "acceptance-tenant"
    assert row["caller_sub"] == "acceptance-caller"
    assert "same-payload" not in row["journal_json"]
    assert "access_token" not in row["journal_json"]

    recovery_server = LocalWriteServer(calls=calls, release=upstream_gate)
    await recovery_server.start()
    retry_processes, retry_messages, retry_results, retry_invocation, retry_gate = start_workers(
        ctx, pg_env, f"http://127.0.0.1:{recovery_server.port}/mcp", [request_id]
    )
    try:
        await asyncio.to_thread(messages, retry_messages, 1, "ready")
        retry_invocation.set()
        await asyncio.to_thread(messages, retry_messages, 1, "claim_ready")
        retry_gate.set()
        retry_result = (await asyncio.to_thread(finish, retry_processes, retry_results, 1))[0]
        assert retry_result.get("status") == "error"
        assert await count_calls(calls) == []
        persisted = await pool.fetchrow(
            "SELECT status FROM sensitive_write_audits WHERE correlation_id=$1", request_id
        )
        assert persisted["status"] == "executing"
    finally:
        retry_gate.set()
        for process in retry_processes:
            if process.is_alive():
                process.terminate()
                process.join(3)

    fresh_id = f"after-recovery-{secrets.token_hex(8)}"
    fresh_processes, fresh_messages, fresh_results, fresh_invocation, fresh_gate = start_workers(
        ctx, pg_env, f"http://127.0.0.1:{recovery_server.port}/mcp", [fresh_id]
    )
    try:
        await asyncio.to_thread(messages, fresh_messages, 1, "ready")
        fresh_invocation.set()
        await asyncio.to_thread(messages, fresh_messages, 1, "claim_ready")
        fresh_gate.set()
        fresh_result = (await asyncio.to_thread(finish, fresh_processes, fresh_results, 1))[0]
        assert fresh_result.get("status") == "ok"
        assert len(await count_calls(calls)) == 1
        fresh_row = await pool.fetchrow(
            "SELECT status FROM sensitive_write_audits WHERE correlation_id=$1", fresh_id
        )
        assert fresh_row is not None and fresh_row["status"] == "succeeded"
    finally:
        fresh_gate.set()
        for process in fresh_processes:
            if process.is_alive():
                process.terminate()
                process.join(3)
    await pool.execute(
        "DELETE FROM sensitive_write_audits WHERE correlation_id = ANY($1::text[])",
        [request_id, fresh_id],
    )
    await store.close()
    await recovery_server.close()
