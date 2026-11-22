"""Opt-in hard process-death tests for sensitive-write restart safety."""

from __future__ import annotations

import asyncio
import multiprocessing
import os
import secrets
import signal
import socket
import time
from typing import Any
from urllib.parse import urlsplit

import pytest
import uvicorn
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from gateway.state import PostgresAuditStore

pytestmark = [
    pytest.mark.integration,
    pytest.mark.postgres,
    pytest.mark.skipif(
        os.getenv("MCP_RUN_POSTGRES_CRASH_TESTS") != "1",
        reason="set MCP_RUN_POSTGRES_CRASH_TESTS=1 to run hard process-death acceptance tests",
    ),
]


TIMEOUT = 25


class CrashableWriteServer:
    """Local JSON-RPC endpoint with parent-owned durable request/result queues."""

    def __init__(self, received: Any, completed: Any, response_gate: Any | None = None) -> None:
        self.app = FastAPI()

        @self.app.post("/mcp")
        async def write(request: Request) -> JSONResponse:
            payload = await request.json()
            params = payload.get("params") or {}
            item = (params.get("arguments") or {}).get("item", "")
            event = {"item": item, "pid": os.getpid()}
            received.put(event)
            completed.put(event)
            if response_gate is not None and not await asyncio.to_thread(
                response_gate.wait, TIMEOUT
            ):
                raise TimeoutError("parent did not release the upstream response")
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

        listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        listener.bind(("127.0.0.1", 0))
        listener.listen(128)
        self.socket = listener
        self.port = int(listener.getsockname()[1])
        self.server = uvicorn.Server(uvicorn.Config(self.app, log_level="error", lifespan="on"))
        self.task: asyncio.Task[None] | None = None

    async def start(self) -> None:
        self.task = asyncio.create_task(self.server.serve(sockets=[self.socket]))
        deadline = asyncio.get_running_loop().time() + 5
        while not self.server.started:
            if self.task.done():
                await self.task
            if asyncio.get_running_loop().time() >= deadline:
                raise TimeoutError("local crash-test upstream did not start")
            await asyncio.sleep(0.01)

    async def close(self) -> None:
        if self.task is not None and not self.task.done():
            self.server.should_exit = True
            try:
                await asyncio.wait_for(self.task, timeout=3)
            except TimeoutError:
                self.task.cancel()
                await asyncio.gather(self.task, return_exceptions=True)
        self.socket.close()


class PreparationBarrier:
    def __init__(self, store: PostgresAuditStore, stage: str, messages: Any, hold: Any) -> None:
        self.store = store
        self.stage = stage
        self.messages = messages
        self.hold = hold

    def __getattr__(self, name: str) -> Any:
        return getattr(self.store, name)

    async def prepare_sensitive_write_audit(self, operation: dict[str, Any]):
        return await self.store.prepare_sensitive_write_audit(operation)

    async def claim_sensitive_write_audit(self, operation_id: str, started_at: str) -> bool:
        if self.stage == "after_prepare_before_claim":
            self.messages.put(
                {
                    "kind": "barrier",
                    "stage": self.stage,
                    "pid": os.getpid(),
                    "operation_id": operation_id,
                }
            )
            if not await asyncio.to_thread(self.hold.wait, TIMEOUT):
                raise TimeoutError("claim barrier was not released")
        return await self.store.claim_sensitive_write_audit(operation_id, started_at)


def crash_worker(
    dsn: str,
    upstream_url: str,
    correlation_id: str,
    stage: str,
    messages: Any,
    results: Any,
    invocation_gate: Any,
    hold: Any,
) -> None:
    """One isolated GatewayRuntime that can be hard-killed at a named lifecycle stage."""

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
            sqlite_path=f"/tmp/mcpcodeaway-crash-unused-{os.getpid()}.db",
            ssrf_protection_enabled=True,
            upstreams_config_path="/tmp/mcpcodeaway-crash-unused-upstreams.yaml",
        )
        runtime = GatewayRuntime(settings=settings)
        try:
            await runtime.store.init()
            runtime.dispatcher.audit_store = PreparationBarrier(
                runtime.store, stage, messages, hold
            )
            call_tool = runtime.client_manager.call_tool

            async def paused_call(*args: Any, **kwargs: Any) -> Any:
                if stage == "after_claim_before_upstream":
                    messages.put({"kind": "barrier", "stage": stage, "pid": os.getpid()})
                    if not await asyncio.to_thread(hold.wait, TIMEOUT):
                        raise TimeoutError("pre-upstream barrier was not released")
                return await call_tool(*args, **kwargs)

            runtime.client_manager.call_tool = paused_call
            finish = runtime.dispatcher._finish_sensitive_write_audit

            async def paused_finish(operation_id: str, status: str) -> bool:
                if stage == "after_upstream_before_terminal" and status == "succeeded":
                    messages.put({"kind": "barrier", "stage": stage, "pid": os.getpid()})
                    if not await asyncio.to_thread(hold.wait, TIMEOUT):
                        raise TimeoutError("pre-terminal barrier was not released")
                saved = await finish(operation_id, status)
                if stage == "after_terminal_before_result" and status == "succeeded":
                    messages.put(
                        {"kind": "barrier", "stage": stage, "pid": os.getpid(), "saved": saved}
                    )
                    if not await asyncio.to_thread(hold.wait, TIMEOUT):
                        raise TimeoutError("post-terminal barrier was not released")
                return saved

            runtime.dispatcher._finish_sensitive_write_audit = paused_finish
            upstream = UpstreamConfig(
                name="crash_acceptance_upstream",
                type="mcp_http",
                url=upstream_url,
                auth_type="none",
                allow_all_callers=True,
                allowed_operations=["write_item"],
                tool_effects={"write_item": "write"},
                mutating_tools=["write_item"],
                request_timeout_seconds=10,
            )
            tool = DiscoveredTool(
                server=upstream.name,
                name="write_item",
                qualified_name="crash_acceptance_upstream__write_item",
                input_schema={
                    "type": "object",
                    "properties": {"item": {"type": "string"}},
                    "required": ["item"],
                },
                mutating=True,
            )
            identity = CallerIdentity(
                sub="crash-acceptance-caller",
                tenant_id="crash-acceptance-tenant",
                roles=["admin"],
                scopes=["mcp.write"],
            )
            messages.put({"kind": "ready", "pid": os.getpid()})
            if not await asyncio.to_thread(invocation_gate.wait, TIMEOUT):
                raise TimeoutError("invocation was not released")
            set_request_context(request_id=correlation_id)
            try:
                result, record = await runtime.dispatcher.invoke_tool(
                    identity, tool, upstream, {"item": "crash-payload"}
                )
                results.put(
                    {
                        "pid": os.getpid(),
                        "result": result,
                        "status": record.status,
                        "outcome_unknown": record.outcome_unknown,
                        "error_type": record.error_type,
                    }
                )
            except Exception as exc:
                results.put(
                    {"pid": os.getpid(), "exception": type(exc).__name__, "message": str(exc)}
                )
            finally:
                clear_request_context()
        finally:
            await runtime.client_manager.close()
            await runtime.store.close()

    asyncio.run(run())


@pytest.fixture
async def crash_postgres():
    dsn = os.getenv("MCP_TEST_POSTGRES_DSN")
    if not dsn:
        pytest.skip("MCP_TEST_POSTGRES_DSN is not set; use --with-process-crash")
    parsed = urlsplit(dsn)
    if (
        parsed.scheme not in {"postgres", "postgresql"}
        or parsed.hostname not in {"127.0.0.1", "localhost", "::1"}
        or parsed.username != "gateway_test"
        or parsed.password != "local_acceptance_only"
        or parsed.path != "/mcpcodeaway_acceptance"
    ):
        pytest.fail("crash tests only permit the isolated local acceptance PostgreSQL DSN")
    store = PostgresAuditStore(dsn)
    try:
        await store.init()
        assert await store.health_check()
    except Exception as exc:
        await store.close()
        pytest.skip(f"local PostgreSQL is unavailable: {type(exc).__name__}")
    await store.close()
    yield dsn


def start_worker(
    ctx: Any,
    dsn: str,
    upstream_url: str,
    correlation_id: str,
    stage: str = "normal",
):
    messages, results, invocation_gate, hold = ctx.Queue(), ctx.Queue(), ctx.Event(), ctx.Event()
    process = ctx.Process(
        target=crash_worker,
        args=(dsn, upstream_url, correlation_id, stage, messages, results, invocation_gate, hold),
    )
    process.start()
    return process, messages, results, invocation_gate, hold


def wait_message(queue: Any, kind: str, stage: str | None = None) -> dict[str, Any]:
    deadline = time.monotonic() + TIMEOUT
    while True:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise TimeoutError(f"timed out waiting for process message {kind}/{stage}")
        message = queue.get(timeout=remaining)
        if message["kind"] == kind and (stage is None or message.get("stage") == stage):
            return message


def kill_abruptly(process: Any, pid: int) -> None:
    assert process.pid == pid
    assert process.is_alive()
    process.kill()
    process.join(TIMEOUT)
    assert not process.is_alive()
    assert process.exitcode is not None and process.exitcode != 0
    if os.name == "posix":
        assert process.exitcode == -signal.SIGKILL


async def row_for(dsn: str, correlation_id: str) -> dict[str, Any] | None:
    store = PostgresAuditStore(dsn)
    await store.init()
    row = await (await store._get_pool()).fetchrow(
        "SELECT * FROM sensitive_write_audits WHERE correlation_id=$1", correlation_id
    )
    await store.close()
    return dict(row) if row is not None else None


async def remove_row(dsn: str, correlation_id: str) -> None:
    store = PostgresAuditStore(dsn)
    await store.init()
    await (await store._get_pool()).execute(
        "DELETE FROM sensitive_write_audits WHERE correlation_id=$1", correlation_id
    )
    await store.close()


async def drain(queue: Any) -> list[dict[str, Any]]:
    values = []
    while True:
        try:
            values.append(queue.get_nowait())
        except Exception:
            return values


def assert_safe_journal_row(row: dict[str, Any]) -> None:
    assert "crash-payload" not in str(row)
    assert "access_token" not in row
    assert "arguments" not in row


async def retry_worker(
    ctx: Any, dsn: str, url: str, correlation_id: str
) -> tuple[dict[str, Any], list[dict[str, Any]], list[dict[str, Any]]]:
    process, messages, results, invocation_gate, _hold = start_worker(ctx, dsn, url, correlation_id)
    await asyncio.to_thread(wait_message, messages, "ready")
    invocation_gate.set()
    result = await asyncio.to_thread(results.get, True, TIMEOUT)
    await asyncio.to_thread(process.join, TIMEOUT)
    assert not process.is_alive() and process.exitcode == 0
    return result, await drain(messages), await drain(results)


@pytest.mark.asyncio
async def test_hard_kill_after_preparation_allows_safe_claim_and_single_dispatch(crash_postgres):
    ctx = multiprocessing.get_context("spawn")
    received, completed = ctx.Queue(), ctx.Queue()
    upstream = CrashableWriteServer(received, completed)
    await upstream.start()
    correlation = f"crash-prepared-{secrets.token_hex(8)}"
    process, messages, results, invocation_gate, hold = start_worker(
        ctx,
        crash_postgres,
        f"http://127.0.0.1:{upstream.port}/mcp",
        correlation,
        "after_prepare_before_claim",
    )
    try:
        ready = await asyncio.to_thread(wait_message, messages, "ready")
        invocation_gate.set()
        barrier = await asyncio.to_thread(
            wait_message, messages, "barrier", "after_prepare_before_claim"
        )
        before = await row_for(crash_postgres, correlation)
        assert before is not None and before["status"] == "prepared"
        assert before["correlation_id"] == correlation
        kill_abruptly(process, ready["pid"])
        after = await row_for(crash_postgres, correlation)
        assert after is not None and after["status"] == "prepared"
        assert after["operation_id"] == barrier["operation_id"]
        assert await drain(received) == []

        retry, retry_messages, _ = await retry_worker(
            ctx, crash_postgres, f"http://127.0.0.1:{upstream.port}/mcp", correlation
        )
        assert retry["status"] == "ok"
        assert len(await drain(received)) == 1
        assert len(await drain(completed)) == 1
        final = await row_for(crash_postgres, correlation)
        assert final is not None and final["status"] == "succeeded"
        assert retry["pid"] != ready["pid"]
        assert_safe_journal_row(final)
    finally:
        if process.is_alive():
            process.kill()
            process.join(3)
        await remove_row(crash_postgres, correlation)
        await upstream.close()


@pytest.mark.asyncio
async def test_hard_kill_after_claim_before_upstream_preserves_unresolved_claim(crash_postgres):
    ctx = multiprocessing.get_context("spawn")
    received, completed = ctx.Queue(), ctx.Queue()
    upstream = CrashableWriteServer(received, completed)
    await upstream.start()
    correlation = f"crash-claimed-{secrets.token_hex(8)}"
    process, messages, _results, invocation_gate, hold = start_worker(
        ctx,
        crash_postgres,
        f"http://127.0.0.1:{upstream.port}/mcp",
        correlation,
        "after_claim_before_upstream",
    )
    try:
        ready = await asyncio.to_thread(wait_message, messages, "ready")
        invocation_gate.set()
        await asyncio.to_thread(wait_message, messages, "barrier", "after_claim_before_upstream")
        before = await row_for(crash_postgres, correlation)
        assert before is not None and before["status"] == "executing"
        kill_abruptly(process, ready["pid"])
        after = await row_for(crash_postgres, correlation)
        assert after is not None and after["status"] == "executing"
        assert await drain(received) == []
        retry, _retry_messages, _ = await retry_worker(
            ctx, crash_postgres, f"http://127.0.0.1:{upstream.port}/mcp", correlation
        )
        assert retry["pid"] != ready["pid"]
        assert retry["status"] == "error"
        assert retry["outcome_unknown"] is False
        assert await drain(received) == []
        assert await drain(completed) == []
        assert_safe_journal_row(after)
    finally:
        if process.is_alive():
            process.kill()
            process.join(3)
        await remove_row(crash_postgres, correlation)
        await upstream.close()


@pytest.mark.asyncio
async def test_hard_kill_in_flight_distinguishes_received_from_unsent_and_blocks_retry(
    crash_postgres,
):
    ctx = multiprocessing.get_context("spawn")
    received, completed, response_gate = ctx.Queue(), ctx.Queue(), ctx.Event()
    upstream = CrashableWriteServer(received, completed, response_gate)
    await upstream.start()
    correlation = f"crash-inflight-{secrets.token_hex(8)}"
    process, messages, _results, invocation_gate, hold = start_worker(
        ctx, crash_postgres, f"http://127.0.0.1:{upstream.port}/mcp", correlation
    )
    try:
        ready = await asyncio.to_thread(wait_message, messages, "ready")
        invocation_gate.set()
        request = await asyncio.to_thread(received.get, True, TIMEOUT)
        assert request["item"] == "crash-payload"
        committed = await asyncio.to_thread(completed.get, True, TIMEOUT)
        assert committed["item"] == "crash-payload"
        before = await row_for(crash_postgres, correlation)
        assert before is not None and before["status"] == "executing"
        kill_abruptly(process, ready["pid"])
        response_gate.set()
        after = await row_for(crash_postgres, correlation)
        assert after is not None and after["status"] == "executing"
        retry, _retry_messages, _ = await retry_worker(
            ctx, crash_postgres, f"http://127.0.0.1:{upstream.port}/mcp", correlation
        )
        assert retry["pid"] != ready["pid"]
        assert retry["status"] == "error"
        assert len(await drain(received)) == 0
        assert len(await drain(completed)) == 0

        other_correlation = f"crash-inflight-distinct-{secrets.token_hex(8)}"
        distinct, _distinct_messages, _ = await retry_worker(
            ctx, crash_postgres, f"http://127.0.0.1:{upstream.port}/mcp", other_correlation
        )
        assert distinct["status"] == "ok"
        assert len(await drain(received)) == 1
        assert len(await drain(completed)) == 1
        assert_safe_journal_row(after)
    finally:
        if process.is_alive():
            process.kill()
            process.join(3)
        await remove_row(crash_postgres, correlation)
        if "other_correlation" in locals():
            await remove_row(crash_postgres, other_correlation)
        await upstream.close()


@pytest.mark.parametrize(
    "stage,expected_retry,expected_final",
    [
        ("after_upstream_before_terminal", "error", "executing"),
        ("after_terminal_before_result", "error", "succeeded"),
    ],
)
async def test_hard_kill_around_terminal_persistence_never_replays(
    crash_postgres, stage: str, expected_retry: str, expected_final: str
):
    ctx = multiprocessing.get_context("spawn")
    received, completed = ctx.Queue(), ctx.Queue()
    upstream = CrashableWriteServer(received, completed)
    await upstream.start()
    correlation = f"crash-terminal-{stage}-{secrets.token_hex(8)}"
    process, messages, _results, invocation_gate, hold = start_worker(
        ctx, crash_postgres, f"http://127.0.0.1:{upstream.port}/mcp", correlation, stage
    )
    try:
        ready = await asyncio.to_thread(wait_message, messages, "ready")
        invocation_gate.set()
        barrier = await asyncio.to_thread(wait_message, messages, "barrier", stage)
        assert barrier["pid"] == ready["pid"]
        if stage == "after_terminal_before_result":
            assert barrier["saved"] is True
        assert len(await drain(received)) == 1
        assert len(await drain(completed)) == 1
        before = await row_for(crash_postgres, correlation)
        expected_before = "executing" if stage == "after_upstream_before_terminal" else "succeeded"
        assert before is not None and before["status"] == expected_before
        kill_abruptly(process, ready["pid"])
        after = await row_for(crash_postgres, correlation)
        assert after is not None and after["status"] == expected_final
        retry, _retry_messages, _ = await retry_worker(
            ctx, crash_postgres, f"http://127.0.0.1:{upstream.port}/mcp", correlation
        )
        assert retry["pid"] != ready["pid"]
        assert retry["status"] == expected_retry
        assert await drain(received) == []
        assert await drain(completed) == []
        distinct_correlation = f"crash-terminal-distinct-{secrets.token_hex(8)}"
        distinct, _distinct_messages, _ = await retry_worker(
            ctx,
            crash_postgres,
            f"http://127.0.0.1:{upstream.port}/mcp",
            distinct_correlation,
        )
        assert distinct["status"] == "ok"
        assert distinct["pid"] != ready["pid"]
        assert len(await drain(received)) == 1
        assert len(await drain(completed)) == 1
        assert_safe_journal_row(after)
    finally:
        if process.is_alive():
            process.kill()
            process.join(3)
        if "distinct_correlation" in locals():
            await remove_row(crash_postgres, distinct_correlation)
        await remove_row(crash_postgres, correlation)
        await upstream.close()
