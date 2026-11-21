"""Measure live gateway latency using the local OIDC and REST readiness pilot."""

from __future__ import annotations

import argparse
import asyncio
import importlib
import json
import os
import sys
import time
from collections import Counter
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import httpx

_ROOT = Path(__file__).resolve().parents[1]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

_mcp_protocol = importlib.import_module("gateway.mcp_protocol")
request_headers = _mcp_protocol.request_headers
request_meta = _mcp_protocol.request_meta


def _percentile(samples: list[float], percentile: float) -> float:
    if not samples:
        return 0.0
    ordered = sorted(samples)
    index = max(0, min(len(ordered) - 1, int((percentile / 100.0) * len(ordered) + 0.999) - 1))
    return round(ordered[index], 3)


async def _access_token(
    client: httpx.AsyncClient,
    *,
    token_url: str,
    username: str,
    password: str,
    client_id: str,
) -> str:
    response = await client.post(
        token_url,
        data={
            "grant_type": "password",
            "client_id": client_id,
            "scope": "gateway.access",
            "username": username,
            "password": password,
        },
    )
    response.raise_for_status()
    token = response.json().get("access_token")
    if not isinstance(token, str) or not token:
        raise RuntimeError("OIDC response did not contain an access token")
    return token


def _request_payload(mode: str, tool_name: str) -> tuple[str, dict[str, Any]]:
    if mode == "codemode":
        return "execute_code", {
            "code": "accounts = await crm_accounts__list_crm_accounts(limit=25)\nlen(accounts)",
        }
    return tool_name, {"limit": 25}


async def run_live_benchmark(
    *,
    base_url: str,
    requests: int,
    concurrency: int,
    warmup: int,
    mode: str,
    timeout_seconds: float,
    output_path: Path | None,
) -> dict[str, Any]:
    if requests < 1 or concurrency < 1 or warmup < 0:
        raise ValueError("requests/concurrency must be positive and warmup must not be negative")
    base_url = base_url.rstrip("/")
    idp_url = os.environ.get("READINESS_IDP_URL", "http://localhost:8081").rstrip("/")
    token_url = os.environ.get(
        "READINESS_TOKEN_URL",
        f"{idp_url}/realms/mcp-readiness/protocol/openid-connect/token",
    )
    username = os.environ.get("READINESS_USERNAME", "readiness-finance")
    password = os.environ.get("READINESS_PASSWORD", "readiness-local-only")
    client_id = os.environ.get("READINESS_CLIENT_ID", "mcp-readiness")
    tool_name = os.environ.get("READINESS_TOOL", "crm_accounts__list_crm_accounts")
    forced_mode = "traditional" if mode == "traditional" else "codemode"
    limits = httpx.Limits(
        max_connections=max(concurrency, 2),
        max_keepalive_connections=max(concurrency, 2),
    )
    timeout = httpx.Timeout(timeout_seconds)
    async with httpx.AsyncClient(timeout=timeout, limits=limits) as client:
        token = await _access_token(
            client,
            token_url=token_url,
            username=username,
            password=password,
            client_id=client_id,
        )
        request_tool, arguments = _request_payload(mode, tool_name)
        headers = {
            **request_headers("tools/call", tool_name=request_tool),
            "Authorization": f"Bearer {token}",
            "X-Admin-Force-Mode": forced_mode,
        }

        async def one_call(request_id: int) -> tuple[float, int, bool, str | None, int]:
            payload = {
                "jsonrpc": "2.0",
                "id": request_id,
                "method": "tools/call",
                "params": {
                    "name": request_tool,
                    "arguments": arguments,
                    "_meta": request_meta(client_name="readiness-live-benchmark"),
                },
            }
            started = time.perf_counter()
            try:
                response = await client.post(f"{base_url}/mcp", headers=headers, json=payload)
                elapsed_ms = (time.perf_counter() - started) * 1000
                successful = False
                error_class: str | None = None
                try:
                    body = response.json()
                    result = body.get("result")
                    successful = (
                        response.status_code == httpx.codes.OK
                        and isinstance(result, dict)
                        and not result.get("isError", False)
                        and not (
                            isinstance(result.get("structuredContent"), dict)
                            and result["structuredContent"].get("ok") is False
                        )
                        and "error" not in body
                    )
                    if not successful:
                        message = str(body.get("error", {}).get("message", ""))
                        if "Rate limit exceeded" in message:
                            error_class = "rate_limit"
                        else:
                            error_class = f"mcp_{body.get('error', {}).get('code', 'tool_error')}"
                except (ValueError, AttributeError):
                    error_class = "invalid_response"
                if response.status_code != httpx.codes.OK:
                    error_class = f"http_{response.status_code}"
                return (
                    elapsed_ms,
                    response.status_code,
                    successful,
                    error_class,
                    len(response.content),
                )
            except httpx.TimeoutException:
                return (time.perf_counter() - started) * 1000, 0, False, "timeout", 0
            except httpx.HTTPError as exc:
                return (
                    (time.perf_counter() - started) * 1000,
                    0,
                    False,
                    f"transport_{type(exc).__name__}",
                    0,
                )

        for warmup_id in range(warmup):
            _, status, successful, error_class, _ = await one_call(-warmup_id - 1)
            if not successful:
                raise RuntimeError(
                    f"Warmup failed with {error_class or f'HTTP {status}'}; no benchmark was run"
                )

        semaphore = asyncio.Semaphore(concurrency)
        latencies: list[float] = []
        response_bytes = 0
        errors: Counter[str] = Counter()
        statuses: Counter[str] = Counter()
        successes = 0

        async def measured(request_id: int) -> None:
            nonlocal response_bytes, successes
            async with semaphore:
                elapsed, status, successful, error_class, body_bytes = await one_call(request_id)
            latencies.append(elapsed)
            response_bytes += body_bytes
            statuses[str(status)] += 1
            if successful:
                successes += 1
            else:
                errors[error_class or "unknown"] += 1

        wall_started = time.perf_counter()
        await asyncio.gather(*(measured(index + 1) for index in range(requests)))
        wall_seconds = max(time.perf_counter() - wall_started, 1e-9)

    report: dict[str, Any] = {
        "generated_at": datetime.now(UTC).isoformat(),
        "base_url": base_url,
        "workload": {
            "requests": requests,
            "concurrency": concurrency,
            "warmup_requests": warmup,
            "mode": mode,
            "tool": tool_name,
            "payload": "limit=25",
            "retries": 0,
        },
        "results": {
            "wall_seconds": round(wall_seconds, 3),
            "throughput_rps": round(requests / wall_seconds, 3),
            "success_rate_pct": round((successes / requests) * 100, 3),
            "successes": successes,
            "errors": dict(errors),
            "http_status_counts": dict(statuses),
            "latency_ms": {
                "p50": _percentile(latencies, 50),
                "p95": _percentile(latencies, 95),
                "p99": _percentile(latencies, 99),
                "max": round(max(latencies, default=0.0), 3),
            },
            "average_response_bytes": round(response_bytes / requests, 1),
        },
        "notes": [
            "This run measures the local deployment and mock REST upstream, not customer capacity.",
            "Run separate read-only runs for each load level; this harness does not retry.",
        ],
    }
    if output_path is not None:
        output_path.parent.mkdir(parents=True, exist_ok=True)
        output_path.write_text(json.dumps(report, indent=2), encoding="utf-8")
    return report


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--base-url", default=os.environ.get("READINESS_GATEWAY_URL", "http://localhost:8080")
    )
    parser.add_argument("--requests", type=int, default=200)
    parser.add_argument("--concurrency", type=int, default=10)
    parser.add_argument("--warmup", type=int, default=10)
    parser.add_argument("--mode", choices=("traditional", "codemode"), default="traditional")
    parser.add_argument("--timeout-seconds", type=float, default=20.0)
    parser.add_argument("--output", default="/tmp/mcpcodeaway-live-readiness.json")
    args = parser.parse_args(argv)
    try:
        report = asyncio.run(
            run_live_benchmark(
                base_url=args.base_url,
                requests=args.requests,
                concurrency=args.concurrency,
                warmup=args.warmup,
                mode=args.mode,
                timeout_seconds=args.timeout_seconds,
                output_path=Path(args.output) if args.output else None,
            )
        )
    except (httpx.HTTPError, RuntimeError, ValueError) as exc:
        print(f"Live benchmark failed: {exc}", file=sys.stderr)
        return 1
    print(json.dumps(report, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
