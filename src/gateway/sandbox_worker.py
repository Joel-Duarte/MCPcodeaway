"""Standalone egress-isolated Monty Sandbox Worker service (WebSocket reverse-RPC)."""

from __future__ import annotations

import asyncio
import hmac
import os
import uuid
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from typing import Any

from fastapi import FastAPI, WebSocket, WebSocketDisconnect

from gateway.sandbox import LocalMontyBackend


def _authorized_worker_ws(ws: WebSocket, expected_secret: str) -> bool:
    if not expected_secret:
        return True
    auth_hdr = ws.headers.get("authorization", "")
    token = auth_hdr[7:].strip() if auth_hdr.lower().startswith("bearer ") else ""
    hdr_secret = ws.headers.get("x-worker-secret", "")
    presented = token or hdr_secret
    return bool(presented) and hmac.compare_digest(presented, expected_secret)


def create_sandbox_worker_app(
    *,
    min_processes: int = 1,
    max_processes: int = 4,
    worker_secret: str | None = None,
) -> FastAPI:
    """Create a standalone Monty worker service with zero token or network knowledge."""
    resolved_secret = (
        worker_secret
        if worker_secret is not None
        else os.getenv("GATEWAY_SANDBOX_WORKER_SECRET", "")
    )
    monty = LocalMontyBackend(min_processes=min_processes, max_processes=max_processes)

    @asynccontextmanager
    async def lifespan(_: FastAPI) -> AsyncIterator[None]:
        try:
            yield
        finally:
            await monty.close()

    app = FastAPI(title="MCPcodeaway Sandbox Worker", version="0.1.0", lifespan=lifespan)

    @app.get("/health")
    async def health() -> dict[str, str]:
        return {"status": "ok", "service": "sandbox-worker"}

    @app.websocket("/ws/execute")
    async def ws_execute(ws: WebSocket) -> None:
        if not _authorized_worker_ws(ws, resolved_secret):
            await ws.close(code=1008, reason="Unauthorized sandbox worker caller")
            return
        await ws.accept()
        reader_task: asyncio.Task[None] | None = None
        pending: dict[str, asyncio.Future[Any]] = {}
        try:
            init_msg = await ws.receive_json()
            if init_msg.get("type") != "run":
                await ws.send_json({"type": "error", "error": "Expected initial 'run' message"})
                return

            code = str(init_msg.get("code") or "")
            tool_names = [str(name) for name in (init_msg.get("tool_names") or [])]
            timeout_seconds = float(init_msg.get("timeout_seconds", 10.0))
            max_output_bytes = int(init_msg.get("max_output_bytes", 32768))

            loop = asyncio.get_running_loop()
            send_lock = asyncio.Lock()

            async def _read_loop() -> None:
                try:
                    while True:
                        msg = await ws.receive_json()
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

            res = await monty.execute(
                code,
                external_functions=external_functions,
                timeout_seconds=timeout_seconds,
                max_output_bytes=max_output_bytes,
            )
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
