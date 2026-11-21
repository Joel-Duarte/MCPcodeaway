"""Phase 2 tests: Monty sandbox isolation, resource caps, call budget, and credential safety."""

from __future__ import annotations

import asyncio
import json
import time

import httpx
import pytest

from examples.mock_backends import create_mock_backends_app
from gateway.auth import CallerIdentity, LocalDevKeyManager
from gateway.config import GatewaySettings
from gateway.dispatcher import SubcallRecord
from gateway.sandbox import LocalMontyBackend, SandboxResult
from gateway.server import create_gateway_app
from gateway.state import SQLiteAuditStore
from gateway.upstream_clients import DiscoveredTool


async def test_gateway_wall_timeout_cancels_the_sandbox_backend(tmp_path) -> None:
    class SlowSandboxBackend:
        def __init__(self) -> None:
            self.cancelled = asyncio.Event()

        async def execute(
            self,
            code: str,
            *,
            external_functions: dict,
            timeout_seconds: float,
            max_output_bytes: int = 32768,
        ) -> SandboxResult:
            _ = (code, external_functions, timeout_seconds, max_output_bytes)
            try:
                await asyncio.sleep(2.0)
            except asyncio.CancelledError:
                self.cancelled.set()
                raise
            return SandboxResult(ok=True, result="late")

        async def close(self) -> None:
            return None

    backend = SlowSandboxBackend()
    settings = GatewaySettings(
        auth_mode="dev",
        execution_wall_timeout_seconds=0.05,
        sqlite_path=str(tmp_path / "wall-timeout.db"),
    )
    dev_keys = LocalDevKeyManager(issuer=settings.oidc_issuer)
    app = create_gateway_app(
        settings=settings,
        store=SQLiteAuditStore(settings.sqlite_path),
        dev_keys=dev_keys,
        sandbox_backend=backend,
        preloaded_tools={},
    )
    token = dev_keys.issue_user_token(persona="finance_analyst")

    async with app.router.lifespan_context(app):
        identity = await app.state.runtime.validator.validate_authorization_header(
            f"Bearer {token}"
        )
        result = await app.state.runtime.execute_code_for_identity(identity, "1 + 1")
        assert not result["ok"]
        assert result["error_type"] == "ExecutionTimeoutError"
        assert "wall-clock limit" in result["error"]
        assert backend.cancelled.is_set()


@pytest.mark.asyncio
async def test_audit_and_live_telemetry_keep_owner_for_all_execution_outcomes(tmp_path) -> None:
    class OutcomeSandbox:
        def __init__(self) -> None:
            self.cancel_started = asyncio.Event()

        async def execute(
            self,
            code: str,
            *,
            external_functions: dict,
            timeout_seconds: float,
            max_output_bytes: int = 32768,
        ) -> SandboxResult:
            _ = (external_functions, timeout_seconds, max_output_bytes)
            if code == "success":
                return SandboxResult(ok=True, result={"completed": True})
            if code == "failure":
                return SandboxResult(
                    ok=False, error="deliberate failure", error_type="ExecutionError"
                )
            if code == "timeout":
                await asyncio.sleep(1.0)
            if code == "cancel":
                self.cancel_started.set()
                await asyncio.Event().wait()
            raise AssertionError(f"unexpected sandbox input: {code}")

        async def close(self) -> None:
            return None

    settings = GatewaySettings(
        auth_mode="dev",
        execution_wall_timeout_seconds=0.03,
        sqlite_path=str(tmp_path / "outcome-ownership.db"),
        upstreams_config_path=str(tmp_path / "missing.yaml"),
    )
    backend = OutcomeSandbox()
    app = create_gateway_app(
        settings=settings,
        store=SQLiteAuditStore(settings.sqlite_path),
        sandbox_backend=backend,
        preloaded_tools={},
    )
    runtime = app.state.runtime
    events = runtime.cache.subscribe_events("mcpcodeaway:telemetry")

    async def _next_event() -> asyncio.Task[str]:
        task = asyncio.create_task(events.__anext__())
        # Let the async generator register its subscriber before execution.
        await asyncio.sleep(0)
        return task

    async with app.router.lifespan_context(app):
        owners = {
            outcome: CallerIdentity(sub=f"user-{outcome}", tenant_id=f"tenant-{outcome}")
            for outcome in ("success", "failure", "timeout", "cancel")
        }
        observed: dict[str, dict] = {}

        for outcome in ("success", "failure", "timeout"):
            event_task = await _next_event()
            result = await runtime.execute_code_for_identity(owners[outcome], outcome)
            observed[outcome] = json.loads(await event_task)
            assert result["ok"] is (outcome == "success")

        event_task = await _next_event()
        cancelled = asyncio.create_task(
            runtime.execute_code_for_identity(owners["cancel"], "cancel")
        )
        await asyncio.wait_for(backend.cancel_started.wait(), timeout=1.0)
        cancelled.cancel()
        with pytest.raises(asyncio.CancelledError):
            await cancelled
        observed["cancel"] = json.loads(await event_task)

        expected_errors = {
            "success": None,
            "failure": "deliberate failure",
            "timeout": "gateway wall-clock limit",
            "cancel": "cancelled before completion",
        }
        runs = await runtime.store.list_runs(limit=20)
        assert len(runs) == len(expected_errors)
        by_owner = {(row["tenant_id"], row["user_sub"]): row for row in runs}

        for outcome, owner in owners.items():
            row = by_owner[(owner.tenant_id, owner.sub)]
            assert row["ok"] is (outcome == "success")
            if expected_errors[outcome] is None:
                assert row["error"] is None
            else:
                assert expected_errors[outcome] in row["error"]
            event = observed[outcome]
            assert event["tenant_id"] == owner.tenant_id
            assert event["user_sub"] == owner.sub
            assert event["run_id"] == row["run_id"]
            assert event["ok"] is (outcome == "success")
            summary = await runtime.telemetry.summary(tenant_id=owner.tenant_id)
            assert summary["total_runs"] == 1

    await events.aclose()


async def test_oversized_code_is_rejected_before_sandbox_execution(tmp_path) -> None:
    class RecordingSandboxBackend:
        def __init__(self) -> None:
            self.called = False

        async def execute(
            self,
            code: str,
            *,
            external_functions: dict,
            timeout_seconds: float,
            max_output_bytes: int = 32768,
        ) -> SandboxResult:
            _ = (code, external_functions, timeout_seconds, max_output_bytes)
            self.called = True
            return SandboxResult(ok=True, result="unexpected")

        async def close(self) -> None:
            return None

    backend = RecordingSandboxBackend()
    settings = GatewaySettings(
        auth_mode="dev",
        max_sandbox_code_bytes=8,
        sqlite_path=str(tmp_path / "code-size.db"),
    )
    dev_keys = LocalDevKeyManager(issuer=settings.oidc_issuer)
    app = create_gateway_app(
        settings=settings,
        store=SQLiteAuditStore(settings.sqlite_path),
        dev_keys=dev_keys,
        sandbox_backend=backend,
        preloaded_tools={},
    )
    token = dev_keys.issue_user_token(persona="finance_analyst")

    async with app.router.lifespan_context(app):
        identity = await app.state.runtime.validator.validate_authorization_header(
            f"Bearer {token}"
        )
        result = await app.state.runtime.execute_code_for_identity(identity, "123456789")

    assert not result["ok"]
    assert result["error_type"] == "SandboxInputTooLarge"
    assert not backend.called


async def test_tool_callbacks_are_bounded_per_execution(tmp_path) -> None:
    class FanoutSandboxBackend:
        async def execute(
            self,
            code: str,
            *,
            external_functions: dict,
            timeout_seconds: float,
            max_output_bytes: int = 32768,
        ) -> SandboxResult:
            _ = (code, timeout_seconds, max_output_bytes)
            results = await asyncio.gather(
                *(external_functions["mock_srv__echo"](value=i) for i in range(6))
            )
            return SandboxResult(ok=True, result=results)

        async def close(self) -> None:
            return None

    upstreams_path = tmp_path / "upstreams.yaml"
    upstreams_path.write_text(
        "upstreams:\n"
        "  - name: mock_srv\n"
        "    type: mcp_http\n"
        "    url: https://mock.example.test/mcp\n"
        "    auth_type: none\n",
        encoding="utf-8",
    )
    settings = GatewaySettings(
        auth_mode="dev",
        max_concurrent_tool_calls_per_execution=2,
        sqlite_path=str(tmp_path / "callback-limit.db"),
        upstreams_config_path=str(upstreams_path),
    )
    dev_keys = LocalDevKeyManager(issuer=settings.oidc_issuer)
    app = create_gateway_app(
        settings=settings,
        store=SQLiteAuditStore(settings.sqlite_path),
        dev_keys=dev_keys,
        sandbox_backend=FanoutSandboxBackend(),
        preloaded_tools={
            "mock_srv": [
                DiscoveredTool(
                    server="mock_srv",
                    name="echo",
                    qualified_name="mock_srv__echo",
                    input_schema={"type": "object", "properties": {"value": {"type": "integer"}}},
                )
            ]
        },
    )

    async with app.router.lifespan_context(app):
        runtime = app.state.runtime
        active_calls = 0
        peak_calls = 0

        async def _measure_callback_concurrency(
            identity, tool, upstream, arguments, *, budget=None, code_mode_hints=False
        ):
            nonlocal active_calls, peak_calls
            _ = (identity, budget, code_mode_hints)
            active_calls += 1
            peak_calls = max(peak_calls, active_calls)
            try:
                await asyncio.sleep(0.02)
                return arguments, SubcallRecord(
                    tool=tool.qualified_name,
                    server=upstream.name,
                    duration_ms=20.0,
                    raw_bytes=16,
                    status="ok",
                    auth_type="none",
                )
            finally:
                active_calls -= 1

        runtime.dispatcher.invoke_tool = _measure_callback_concurrency
        token = dev_keys.issue_user_token(persona="finance_analyst")
        identity = await runtime.validator.validate_authorization_header(f"Bearer {token}")
        result = await runtime.execute_code_for_identity(identity, "fan out")

    assert result["ok"]
    assert len(result["result"]) == 6
    assert peak_calls == 2


async def test_monty_pool_checkout_timeout_bounds_waiting_executions() -> None:
    sandbox = LocalMontyBackend(
        min_processes=1,
        max_processes=1,
        checkout_timeout_seconds=0.05,
        request_timeout_seconds=2.0,
    )
    callback_started = asyncio.Event()
    release_callback = asyncio.Event()

    async def _hold_worker() -> str:
        callback_started.set()
        await release_callback.wait()
        return "released"

    first_execution = asyncio.create_task(
        sandbox.execute(
            "await hold_worker()",
            external_functions={"hold_worker": _hold_worker},
            timeout_seconds=1.0,
        )
    )
    try:
        await asyncio.wait_for(callback_started.wait(), timeout=2.0)
        waiting_execution = await sandbox.execute(
            "1 + 1", external_functions={}, timeout_seconds=1.0
        )
        assert not waiting_execution.ok
        assert "timeout" in (waiting_execution.error or "").lower()
    finally:
        release_callback.set()
    first_result = await first_execution
    assert first_result.ok and first_result.result == "released"
    await sandbox.close()


async def test_monty_host_request_timeout_kills_a_stuck_worker() -> None:
    sandbox = LocalMontyBackend(
        min_processes=1,
        max_processes=1,
        checkout_timeout_seconds=0.1,
        request_timeout_seconds=0.5,
    )
    started = time.perf_counter()
    try:
        result = await sandbox.execute(
            "while True:\n    pass",
            external_functions={},
            timeout_seconds=5.0,
        )
        elapsed = time.perf_counter() - started
        assert not result.ok
        assert elapsed < 3.0
    finally:
        await sandbox.close()


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
