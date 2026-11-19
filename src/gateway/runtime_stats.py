"""Zero-dependency process CPU/RAM runtime telemetry and worker health probing."""

from __future__ import annotations

import os
import resource
import sys
import time
from typing import TYPE_CHECKING, Any
from urllib.parse import urlparse, urlunparse

if TYPE_CHECKING:
    from gateway.server import GatewayRuntime


def _read_rss_mb() -> float:
    """Return current process Resident Set Size in MiB using /proc or resource fallback."""
    proc_status = "/proc/self/status"
    try:
        with open(proc_status, encoding="utf-8") as fh:
            for line in fh:
                if line.startswith("VmRSS:"):
                    parts = line.split()
                    if len(parts) >= 2:
                        return round(int(parts[1]) / 1024.0, 2)
    except OSError:
        pass
    usage = resource.getrusage(resource.RUSAGE_SELF)
    # Linux ru_maxrss is in KiB; macOS ru_maxrss is in bytes.
    divisor = (1024.0 * 1024.0) if sys.platform == "darwin" else 1024.0
    return round(usage.ru_maxrss / divisor, 2)


class ProcessStatsCollector:
    """Tracks incremental process CPU utilization, memory RSS, load average, and uptime."""

    def __init__(self) -> None:
        self._started_mono = time.monotonic()
        self._last_wall = self._started_mono
        times = os.times()
        self._last_cpu = float(times.user + times.system)
        self.active_executions = 0
        self.total_executions = 0

    def snapshot(self) -> dict[str, Any]:
        now = time.monotonic()
        times = os.times()
        cur_cpu = float(times.user + times.system)
        wall_delta = max(now - self._last_wall, 1e-6)
        cpu_delta = max(0.0, cur_cpu - self._last_cpu)
        cpu_count = os.cpu_count() or 1
        cpu_pct = round(min(100.0 * cpu_count, (cpu_delta / wall_delta) * 100.0), 2)
        self._last_wall = now
        self._last_cpu = cur_cpu

        try:
            load_1m = round(os.getloadavg()[0], 2)
        except OSError:
            load_1m = 0.0

        return {
            "cpu_percent": cpu_pct,
            "rss_mb": _read_rss_mb(),
            "load_avg_1m": load_1m,
            "cpu_count": cpu_count,
            "uptime_seconds": round(now - self._started_mono, 1),
            "active_executions": self.active_executions,
            "total_executions": self.total_executions,
        }


def _worker_health_url(ws_url: str) -> str:
    parsed = urlparse(ws_url)
    scheme = "https" if parsed.scheme == "wss" else "http"
    return urlunparse((scheme, parsed.netloc, "/health", "", "", ""))


async def collect_runtime_telemetry(runtime: GatewayRuntime) -> dict[str, Any]:
    """Collect gateway process telemetry, backend adapters, and remote worker stats."""
    gw_stats = runtime.process_stats.snapshot()
    worker_info: dict[str, Any] = {"mode": runtime.settings.sandbox_backend}

    if runtime.settings.sandbox_backend == "remote" and runtime.settings.sandbox_worker_url:
        health_url = _worker_health_url(runtime.settings.sandbox_worker_url)
        worker_info["health_url"] = health_url
        try:
            client = runtime.client_manager._get_client()
            resp = await client.get(health_url, timeout=1.5)
            resp.raise_for_status()
            worker_info["status"] = "ok"
            worker_info["stats"] = resp.json()
        except Exception as exc:
            worker_info["status"] = "unreachable"
            worker_info["error"] = str(exc)
    else:
        worker_info["status"] = "in_process"
        worker_info["stats"] = gw_stats

    recent_runs = await runtime.store.list_runs(limit=100)
    spawns = sorted(
        float(r.get("monty_spawn_ms") or 0.0)
        for r in recent_runs
        if float(r.get("monty_spawn_ms") or 0.0) > 0
    )
    execs = sorted(
        float(r.get("monty_exec_ms") or 0.0)
        for r in recent_runs
        if float(r.get("monty_exec_ms") or 0.0) > 0
    )

    def _pct(vals: list[float], q: float) -> float:
        if not vals:
            return 0.0
        idx = min(len(vals) - 1, max(0, int(q * len(vals)) - 1))
        return round(vals[idx], 3)

    monty_telemetry = {
        "ephemeral": True,
        "codemode_runs": sum(
            1 for r in recent_runs if str(r.get("mode", "")).startswith("codemode")
        ),
        "grounding_blocked_runs": sum(1 for r in recent_runs if r.get("grounding_blocked")),
        "spawn_p50_ms": _pct(spawns, 0.50),
        "spawn_p95_ms": _pct(spawns, 0.95),
        "exec_p50_ms": _pct(execs, 0.50),
        "exec_p95_ms": _pct(execs, 0.95),
    }

    return {
        "gateway": {
            "profile": runtime.settings.effective_profile_name,
            "app_env": runtime.settings.app_env,
            "auth_mode": runtime.settings.auth_mode,
            "catalog_version": runtime.catalog.catalog_version,
            "upstreams_count": len(runtime.registry.list_upstreams()),
            "cache_backend": "redis" if runtime.settings.redis_url else "memory",
            "store_backend": "postgres" if runtime.settings.postgres_dsn else "sqlite",
            **gw_stats,
        },
        "sandbox_worker": worker_info,
        "monty_telemetry": monty_telemetry,
    }
