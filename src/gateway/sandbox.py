"""Pluggable Code Mode sandbox backends: local Monty pool and remote WS worker."""

from __future__ import annotations

import asyncio
import inspect
import json
import time
from collections.abc import Callable
from typing import Any, Protocol

import pydantic_monty
import websockets
from pydantic import BaseModel

from gateway.dispatcher import OutputRedactor


class SandboxResult(BaseModel):
    """Structured outcome returned by a SandboxBackend execution."""

    ok: bool
    result: Any = None
    stdout: str = ""
    error: str | None = None
    error_type: str | None = None
    truncated: bool = False
    duration_ms: float = 0.0


class SandboxBackend(Protocol):
    """Protocol for isolated Code Mode execution engines (local or remote worker)."""

    async def execute(
        self,
        code: str,
        *,
        external_functions: dict[str, Callable[..., Any]],
        timeout_seconds: float,
        max_output_bytes: int = 32768,
    ) -> SandboxResult: ...

    async def close(self) -> None: ...


def truncate_and_redact_output(
    value: Any, stdout: str, max_output_bytes: int
) -> tuple[Any, str, bool]:
    """Redact sensitive data from output/stdout and bound payload size."""
    clean_stdout = OutputRedactor.redact_text(stdout)
    clean_value = OutputRedactor.redact(value)
    encoded = json.dumps(clean_value, default=str).encode("utf-8")
    if len(encoded) <= max_output_bytes:
        return clean_value, clean_stdout, False
    preview = encoded[: max_output_bytes // 2].decode("utf-8", errors="ignore")
    truncated_obj = {
        "_truncated": True,
        "original_bytes": len(encoded),
        "max_output_bytes": max_output_bytes,
        "preview": preview,
    }
    return truncated_obj, clean_stdout, True


class LocalMontyBackend:
    """In-process Rust-isolated Python execution pool using pydantic-monty."""

    def __init__(
        self,
        *,
        min_processes: int = 1,
        max_processes: int = 4,
        max_memory_bytes: int = 64 * 1024 * 1024,
        max_recursion_depth: int = 200,
    ) -> None:
        self.min_processes = min_processes
        self.max_processes = max_processes
        self.max_memory_bytes = max_memory_bytes
        self.max_recursion_depth = max_recursion_depth
        self._pool: pydantic_monty.AsyncMonty | None = None
        self._lock = asyncio.Lock()

    async def _get_pool(self) -> pydantic_monty.AsyncMonty:
        if self._pool is not None:
            return self._pool
        async with self._lock:
            if self._pool is None:
                pool = pydantic_monty.AsyncMonty(
                    min_processes=self.min_processes,
                    max_processes=self.max_processes,
                )
                await pool.__aenter__()
                self._pool = pool
            return self._pool

    async def close(self) -> None:
        async with self._lock:
            if self._pool is not None:
                await self._pool.__aexit__(None, None, None)
                self._pool = None

    async def execute(
        self,
        code: str,
        *,
        external_functions: dict[str, Callable[..., Any]],
        timeout_seconds: float,
        max_output_bytes: int = 32768,
    ) -> SandboxResult:
        started = time.perf_counter()
        pool = await self._get_pool()
        collector = pydantic_monty.CollectString(max_bytes=min(16384, max_output_bytes))
        limits: pydantic_monty.ResourceLimits = {
            "max_feed_duration_secs": timeout_seconds,
            "max_memory": self.max_memory_bytes,
            "max_recursion_depth": self.max_recursion_depth,
        }
        try:
            async with pool.checkout(limits=limits) as session:
                raw_result = await session.feed_run(
                    code,
                    external_lookup=external_functions,
                    print_callback=collector,
                )
            clean_res, clean_stdout, truncated = truncate_and_redact_output(
                raw_result, collector.output, max_output_bytes
            )
            return SandboxResult(
                ok=True,
                result=clean_res,
                stdout=clean_stdout,
                truncated=truncated,
                duration_ms=round((time.perf_counter() - started) * 1000, 3),
            )
        except pydantic_monty.MontyError as exc:
            return SandboxResult(
                ok=False,
                stdout=OutputRedactor.redact_text(collector.output),
                error=str(exc),
                error_type=type(exc).__name__,
                duration_ms=round((time.perf_counter() - started) * 1000, 3),
            )
        except Exception as exc:
            return SandboxResult(
                ok=False,
                stdout=OutputRedactor.redact_text(collector.output),
                error=str(exc),
                error_type=type(exc).__name__,
                duration_ms=round((time.perf_counter() - started) * 1000, 3),
            )


class RemoteMontyWorkerBackend:
    """Executes Code Mode scripts on an egress-isolated sandbox worker over WebSocket RPC."""

    def __init__(self, worker_ws_url: str, *, worker_secret: str = "") -> None:
        self.worker_ws_url = worker_ws_url
        self.worker_secret = worker_secret
        self._primary_secret = next(
            (s.strip() for s in worker_secret.split(",") if s.strip()), ""
        )

    async def close(self) -> None:
        return None

    async def execute(
        self,
        code: str,
        *,
        external_functions: dict[str, Callable[..., Any]],
        timeout_seconds: float,
        max_output_bytes: int = 32768,
    ) -> SandboxResult:
        started = time.perf_counter()
        connect_kwargs: dict[str, Any] = {"max_size": 4 * 1024 * 1024}
        if self._primary_secret:
            connect_kwargs["additional_headers"] = {
                "Authorization": f"Bearer {self._primary_secret}",
                "X-Worker-Secret": self._primary_secret,
            }
        try:
            async with websockets.connect(self.worker_ws_url, **connect_kwargs) as ws:
                await ws.send(
                    json.dumps(
                        {
                            "type": "run",
                            "code": code,
                            "tool_names": list(external_functions.keys()),
                            "timeout_seconds": timeout_seconds,
                            "max_output_bytes": max_output_bytes,
                        }
                    )
                )
                send_lock = asyncio.Lock()
                in_flight: set[asyncio.Task[None]] = set()

                async def _dispatch_call(msg: dict[str, Any]) -> None:
                    call_id = msg["call_id"]
                    tool_name = msg["tool"]
                    args = msg.get("args") or []
                    kwargs = msg.get("kwargs") or {}
                    fn = external_functions.get(tool_name)
                    if fn is None:
                        reply = {
                            "type": "call_result",
                            "call_id": call_id,
                            "ok": False,
                            "error": f"Tool '{tool_name}' is not bound in caller scope",
                        }
                    else:
                        try:
                            val = fn(*args, **kwargs)
                            if inspect.isawaitable(val):
                                val = await val
                            reply = {
                                "type": "call_result",
                                "call_id": call_id,
                                "ok": True,
                                "result": val,
                            }
                        except Exception as exc:
                            reply = {
                                "type": "call_result",
                                "call_id": call_id,
                                "ok": False,
                                "error": str(exc),
                            }
                    async with send_lock:
                        await ws.send(json.dumps(reply, default=str))

                while True:
                    raw_msg = await ws.recv()
                    msg = json.loads(raw_msg)
                    msg_type = msg.get("type")
                    if msg_type == "call":
                        task = asyncio.create_task(_dispatch_call(msg))
                        in_flight.add(task)
                        task.add_done_callback(in_flight.discard)
                    elif msg_type == "done":
                        if in_flight:
                            await asyncio.gather(*in_flight, return_exceptions=True)
                        res = SandboxResult.model_validate(msg["sandbox_result"])
                        clean_res, clean_out, trunc = truncate_and_redact_output(
                            res.result, res.stdout, max_output_bytes
                        )
                        res.result = clean_res
                        res.stdout = clean_out
                        res.truncated = res.truncated or trunc
                        res.duration_ms = round((time.perf_counter() - started) * 1000, 3)
                        return res
                    elif msg_type == "error":
                        return SandboxResult(
                            ok=False,
                            error=str(msg.get("error", "Remote worker error")),
                            error_type="RemoteWorkerError",
                            duration_ms=round((time.perf_counter() - started) * 1000, 3),
                        )
        except Exception as exc:
            return SandboxResult(
                ok=False,
                error=str(exc),
                error_type=type(exc).__name__,
                duration_ms=round((time.perf_counter() - started) * 1000, 3),
            )
