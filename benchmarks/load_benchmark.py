"""Concurrent multi-user load benchmark measuring throughput, latency, and token cache hits."""

from __future__ import annotations

import argparse
import asyncio
import json
import math
import sys
import tempfile
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import httpx

_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from examples.mock_backends import create_mock_backends_app  # noqa: E402
from gateway.auth import LocalDevKeyManager
from gateway.comparator import _scenario_plan
from gateway.config import GatewaySettings
from gateway.server import GatewayRuntime
from gateway.state import SQLiteAuditStore


def _percentile(sorted_values: list[float], pct: float) -> float:
    if not sorted_values:
        return 0.0
    idx = min(len(sorted_values) - 1, max(0, math.ceil((pct / 100.0) * len(sorted_values)) - 1))
    return round(sorted_values[idx], 3)


async def run_load_benchmark(
    *,
    num_users: int = 40,
    concurrency: int = 20,
    output_path: Path | None = Path("benchmarks/load_results.json"),
) -> dict[str, Any]:
    """Run parallel multi-user workloads on Traditional vs. Code Mode gateway paths."""
    classic_calls, codemode_script = _scenario_plan("reconcile_overdue")

    with tempfile.TemporaryDirectory() as tmpdir:
        settings = GatewaySettings(
            auth_mode="dev",
            rate_limit_per_minute=10000,
            monty_pool_checkout_timeout_seconds=5.0,
            sqlite_path=f"{tmpdir}/load.db",
        )
        dev_keys = LocalDevKeyManager(issuer=settings.oidc_issuer)
        backends_app = create_mock_backends_app(
            public_key_pem=dev_keys.public_pem, expected_issuer=settings.oidc_issuer
        )
        backend_client = httpx.AsyncClient(
            transport=httpx.ASGITransport(app=backends_app), base_url="http://127.0.0.1:8001"
        )
        runtime = GatewayRuntime(
            settings=settings,
            store=SQLiteAuditStore(settings.sqlite_path),
            dev_keys=dev_keys,
            http_client=backend_client,
        )
        await runtime.startup()

        unique_count = max(4, num_users // 3)
        identities = []
        for idx in range(unique_count):
            tok = dev_keys.issue_user_token(
                persona="finance_analyst",
                overrides={"sub": f"user-load-{idx:02d}", "email": f"user{idx}@acme.example"},
            )
            identities.append(await runtime.validator.validate_token(tok))

        await runtime.ensure_catalog_for_identity(identities[0])
        sem = asyncio.Semaphore(concurrency)

        async def _bench_mode(mode: str) -> dict[str, Any]:
            for ident in identities:
                for aud, scp in (
                    ("api://snowflake-ledger", "snowflake.read"),
                    ("api://billing-eu", "billing.read"),
                    ("api://billing-us", "billing.read"),
                ):
                    await runtime.cache.delete(
                        f"delegated:{ident.tenant_id}:{ident.sub}:{aud}:{scp}"
                    )

            latencies: list[float] = []
            errors = 0
            run_ids: list[str] = []

            async def _one(req_idx: int) -> None:
                nonlocal errors
                ident = identities[req_idx % len(identities)]
                async with sem:
                    t0 = time.perf_counter()
                    try:
                        if mode == "traditional":
                            for call in classic_calls:
                                res = await runtime.call_direct_tool_for_identity(
                                    ident,
                                    call["tool"],
                                    call["arguments"],
                                    mode="traditional",
                                )
                                run_ids.append(res["run_id"])
                        else:
                            res = await runtime.execute_code_for_identity(
                                ident, codemode_script, mode="codemode"
                            )
                            if not res["ok"]:
                                errors += 1
                            run_ids.append(res["run_id"])
                    except Exception:
                        errors += 1
                    latencies.append((time.perf_counter() - t0) * 1000.0)

            wall_start = time.perf_counter()
            await asyncio.gather(*[_one(i) for i in range(num_users)])
            wall_secs = max(time.perf_counter() - wall_start, 1e-6)

            subcall_total = 0
            cache_hits = 0
            for rid in run_ids:
                doc = await runtime.store.get_run(rid)
                if not doc:
                    continue
                for sc in doc.get("subcalls", []):
                    if sc.get("auth_type") == "delegated_token":
                        subcall_total += 1
                        if sc.get("token_cache_hit"):
                            cache_hits += 1

            sorted_lat = sorted(latencies)
            return {
                "mode": mode,
                "requests": num_users,
                "unique_users": unique_count,
                "concurrency": concurrency,
                "wall_seconds": round(wall_secs, 3),
                "throughput_rps": round(num_users / wall_secs, 2),
                "p50_ms": _percentile(sorted_lat, 50),
                "p95_ms": _percentile(sorted_lat, 95),
                "max_ms": round(max(sorted_lat) if sorted_lat else 0.0, 3),
                "success_rate_pct": round(((num_users - errors) / num_users) * 100.0, 2),
                "delegated_subcalls": subcall_total,
                "token_cache_hits": cache_hits,
                "token_cache_hit_rate_pct": round((cache_hits / subcall_total) * 100.0, 2)
                if subcall_total
                else 0.0,
            }

        try:
            traditional_stats = await _bench_mode("traditional")
            codemode_stats = await _bench_mode("codemode")
        finally:
            await runtime.shutdown()
            await backend_client.aclose()

    report = {
        "generated_at": datetime.now(UTC).isoformat(),
        "traditional": traditional_stats,
        "codemode": codemode_stats,
    }
    if output_path is not None:
        output_path.parent.mkdir(parents=True, exist_ok=True)
        output_path.write_text(json.dumps(report, indent=2), encoding="utf-8")
    return report


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Run concurrent multi-user load benchmark")
    parser.add_argument("--users", type=int, default=40)
    parser.add_argument("--concurrency", type=int, default=20)
    parser.add_argument("--output", default="benchmarks/load_results.json")
    args = parser.parse_args(argv)
    report = asyncio.run(
        run_load_benchmark(
            num_users=args.users,
            concurrency=args.concurrency,
            output_path=Path(args.output),
        )
    )
    print(json.dumps(report, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
