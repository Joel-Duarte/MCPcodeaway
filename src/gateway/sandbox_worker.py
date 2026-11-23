"""Standalone egress-isolated Monty Sandbox Worker service (WebSocket reverse-RPC)."""

from __future__ import annotations

import asyncio
import hmac
import math
import os
import socket
import uuid
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from typing import Any

from fastapi import FastAPI, WebSocket, WebSocketDisconnect

from gateway.runtime_stats import ProcessStatsCollector
from gateway.sandbox import LocalMontyBackend


def _parse_worker_secrets(expected_secret: str) -> list[str]:
    return [s.strip() for s in expected_secret.split(",") if s.strip()]


def _positive_float_env(name: str, default: float) -> float:
    raw = os.getenv(name)
    value = default if raw is None else float(raw)
    if not math.isfinite(value) or value <= 0:
        raise ValueError(f"{name} must be a finite positive number")
    return value


def _authorized_worker_ws(
    ws: WebSocket, expected_secret: str, *, allow_unauthenticated: bool = False
) -> bool:
    keys = _parse_worker_secrets(expected_secret)
    if not keys:
        return allow_unauthenticated
    auth_hdr = ws.headers.get("authorization", "")
    token = auth_hdr[7:].strip() if auth_hdr.lower().startswith("bearer ") else ""
    hdr_secret = ws.headers.get("x-worker-secret", "")
    presented = token or hdr_secret
    return bool(presented) and any(hmac.compare_digest(presented, key) for key in keys)


def create_sandbox_worker_app(
    *,
    min_processes: int = 1,
    max_processes: int = 4,
    worker_secret: str | None = None,
    allow_unauthenticated: bool | None = None,
    max_code_bytes: int = 256 * 1024,
    max_concurrent_executions: int = 4,
    checkout_timeout_seconds: float | None = None,
    request_timeout_seconds: float | None = None,
) -> FastAPI:
    """Create a standalone Monty worker service with zero token or network knowledge."""
    resolved_secret = (
        worker_secret
        if worker_secret is not None
        else os.getenv("GATEWAY_SANDBOX_WORKER_SECRET", "")
    )
    app_env = os.getenv("GATEWAY_APP_ENV", "sandbox").lower()
    unauthenticated_dev_mode = (
        app_env in {"sandbox", "dev"} if allow_unauthenticated is None else allow_unauthenticated
    )
    worker_keys = _parse_worker_secrets(resolved_secret)
    if not worker_keys and not unauthenticated_dev_mode:
        raise ValueError("GATEWAY_SANDBOX_WORKER_SECRET is required outside local development")
    if app_env == "prod" and any(len(key) < 32 for key in worker_keys):
        raise ValueError(
            "GATEWAY_SANDBOX_WORKER_SECRET must contain keys of at least 32 characters in prod"
        )
    checkout_timeout = (
        _positive_float_env("GATEWAY_MONTY_POOL_CHECKOUT_TIMEOUT_SECONDS", 0.25)
        if checkout_timeout_seconds is None
        else checkout_timeout_seconds
    )
    request_timeout = (
        _positive_float_env("GATEWAY_MONTY_WORKER_REQUEST_TIMEOUT_SECONDS", 15.0)
        if request_timeout_seconds is None
        else request_timeout_seconds
    )
    if (
        not math.isfinite(checkout_timeout)
        or checkout_timeout <= 0
        or not math.isfinite(request_timeout)
        or request_timeout <= 0
    ):
        raise ValueError("Monty checkout and request timeouts must be finite and positive")
    monty = LocalMontyBackend(
        min_processes=min_processes,
        max_processes=max_processes,
        checkout_timeout_seconds=checkout_timeout,
        request_timeout_seconds=request_timeout,
    )
    stats = ProcessStatsCollector()
    execution_slots = asyncio.Semaphore(max(1, max_concurrent_executions))

    @asynccontextmanager
    async def lifespan(_: FastAPI) -> AsyncIterator[None]:
        try:
            yield
        finally:
            await monty.close()

    app = FastAPI(title="MCPcodeaway Sandbox Worker", version="0.1.0", lifespan=lifespan)

    @app.get("/health")
    async def health() -> dict[str, Any]:
        return {
            "status": "ok",
            "service": "sandbox-worker",
            "worker_id": os.getenv("HOSTNAME", socket.gethostname()),
            "min_processes": min_processes,
            "max_processes": max_processes,
            "max_concurrent_executions": max_concurrent_executions,
            **stats.snapshot(),
        }

    @app.websocket("/ws/execute")
    async def ws_execute(ws: WebSocket) -> None:
        if not _authorized_worker_ws(
            ws, resolved_secret, allow_unauthenticated=unauthenticated_dev_mode
        ):
            await ws.close(code=1008, reason="Unauthorized sandbox worker caller")
            return
        await ws.accept()
        reader_task: asyncio.Task[None] | None = None
        pending: dict[str, asyncio.Future[Any]] = {}
        try:
            init_msg = await ws.receive_json()
            if not isinstance(init_msg, dict):
                await ws.send_json({"type": "error", "error": "Run message must be an object"})
                return
            if init_msg.get("type") != "run":
                await ws.send_json({"type": "error", "error": "Expected initial 'run' message"})
                return

            code_value = init_msg.get("code")
            if not isinstance(code_value, str):
                await ws.send_json({"type": "error", "error": "Code must be a string"})
                return
            code = code_value
            if len(code.encode("utf-8")) > max_code_bytes:
                await ws.send_json({"type": "error", "error": "Code payload is too large"})
                return
            raw_tool_names = init_msg.get("tool_names", [])
            if not isinstance(raw_tool_names, list) or any(
                not isinstance(name, str) or not name or len(name) > 256 for name in raw_tool_names
            ):
                await ws.send_json({"type": "error", "error": "Invalid tool bindings"})
                return
            tool_names = raw_tool_names
            if len(tool_names) > 200:
                await ws.send_json({"type": "error", "error": "Too many tool bindings"})
                return
            try:
                timeout_seconds = float(init_msg.get("timeout_seconds", 10.0))
                max_output_bytes = int(init_msg.get("max_output_bytes", 32768))
            except (TypeError, ValueError, OverflowError):
                await ws.send_json({"type": "error", "error": "Invalid execution limits"})
                return
            if not math.isfinite(timeout_seconds) or timeout_seconds <= 0:
                await ws.send_json({"type": "error", "error": "Invalid execution timeout"})
                return
            timeout_seconds = min(timeout_seconds, 30.0)
            max_output_bytes = max(1024, min(max_output_bytes, 256 * 1024))

            try:
                await asyncio.wait_for(execution_slots.acquire(), timeout=0.1)
            except TimeoutError:
                await ws.send_json({"type": "error", "error": "Sandbox worker is at capacity"})
                await ws.close(code=1013, reason="Sandbox worker is at capacity")
                return

            loop = asyncio.get_running_loop()
            send_lock = asyncio.Lock()

            async def _read_loop() -> None:
                try:
                    while True:
                        msg = await ws.receive_json()
                        if not isinstance(msg, dict):
                            for fut in pending.values():
                                if not fut.done():
                                    fut.set_exception(
                                        RuntimeError("Gateway sent an invalid worker message")
                                    )
                            return
                        if msg.get("type") == "call_result":
                            cid = str(msg.get("call_id", ""))
                            fut = pending.pop(cid, None)
                            if fut is not None and not fut.done():
                                if msg.get("ok"):
                                    fut.set_result(msg.get("result"))
                                else:
                                    fut.set_exception(
                                        RuntimeError(str(msg.get("error", "Remote call error")))
                                    )
                except WebSocketDisconnect:
                    for fut in pending.values():
                        if not fut.done():
                            fut.set_exception(RuntimeError("Gateway disconnected"))
                except asyncio.CancelledError:
                    return

            reader_task = asyncio.create_task(_read_loop())

            external_functions: dict[str, Callable[..., Any]] = {}
            for tool_name in tool_names:

                def _make_proxy(bound_name: str) -> Callable[..., Any]:
                    async def _proxy(*args: Any, **kwargs: Any) -> Any:
                        call_id = uuid.uuid4().hex
                        fut = loop.create_future()
                        pending[call_id] = fut
                        async with send_lock:
                            await ws.send_json(
                                {
                                    "type": "call",
                                    "call_id": call_id,
                                    "tool": bound_name,
                                    "args": list(args),
                                    "kwargs": kwargs,
                                }
                            )
                        return await fut

                    return _proxy

                external_functions[tool_name] = _make_proxy(tool_name)

            stats.active_executions += 1
            stats.total_executions += 1
            try:
                res = await monty.execute(
                    code,
                    external_functions=external_functions,
                    timeout_seconds=timeout_seconds,
                    max_output_bytes=max_output_bytes,
                )
            finally:
                stats.active_executions = max(0, stats.active_executions - 1)
                execution_slots.release()
            async with send_lock:
                await ws.send_json({"type": "done", "sandbox_result": res.model_dump()})
        except WebSocketDisconnect:
            return
        finally:
            if reader_task is not None and not reader_task.done():
                reader_task.cancel()
                await asyncio.gather(reader_task, return_exceptions=True)

    return app


app = create_sandbox_worker_app()
