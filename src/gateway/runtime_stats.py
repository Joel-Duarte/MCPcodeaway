"""Zero-dependency process CPU/RAM runtime telemetry and worker health probing."""

from __future__ import annotations

import asyncio
import ipaddress
import os
import resource
import socket
import sys
import time
from typing import TYPE_CHECKING, Any
from urllib.parse import urlparse, urlunparse

from gateway.upstream_clients import _PINNED_DNS, validate_upstream_url

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


def _needs_dns_resolution(hostname: str | None) -> bool:
    if not hostname:
        return False
    if hostname.lower() in {"localhost", "testserver", "localhost.localdomain"}:
        return False
    try:
        ipaddress.ip_address(hostname)
        return False
    except ValueError:
        return True


def _format_netloc(
    host: str, port: int | None, username: str | None = None, password: str | None = None
) -> str:
    formatted_host = f"[{host}]" if ":" in host else host
    netloc = f"{formatted_host}:{port}" if port is not None else formatted_host
    if username:
        auth = f"{username}:{password}" if password else username
        netloc = f"{auth}@{netloc}"
    return netloc


def _worker_health_url(ws_url: str) -> str:
    parsed = urlparse(ws_url)
    scheme = "https" if parsed.scheme == "wss" else "http"
    return urlunparse((scheme, parsed.netloc, "/health", "", "", ""))


async def _discover_worker_health_urls(raw_worker_url: str) -> list[str]:
    urls: list[str] = []
    loop = asyncio.get_running_loop()
    raw_items = [u.strip() for u in raw_worker_url.split(",") if u.strip()]
    for ws_url in raw_items:
        parsed = urlparse(ws_url)
        scheme = "https" if parsed.scheme == "wss" else "http"
        host = parsed.hostname
        if not _needs_dns_resolution(host) or host is None:
            urls.append(urlunparse((scheme, parsed.netloc, "/health", "", "", "")))
            continue
        port = parsed.port or (443 if scheme == "https" else 80)
        try:
            infos = await loop.getaddrinfo(host, port, type=socket.SOCK_STREAM)
            ips = sorted({str(info[4][0]) for info in infos if info[4]})
        except OSError:
            ips = []
        if not ips:
            urls.append(urlunparse((scheme, parsed.netloc, "/health", "", "", "")))
            continue
        for ip in ips:
            netloc = _format_netloc(ip, parsed.port, parsed.username, parsed.password)
            urls.append(urlunparse((scheme, netloc, "/health", "", "", "")))
    return list(dict.fromkeys(urls))


def _aggregate_worker_stats(stats_list: list[dict[str, Any]]) -> dict[str, Any]:
    if not stats_list:
        return {}
    if len(stats_list) == 1:
        return dict(stats_list[0])
    count = len(stats_list)
    merged: dict[str, Any] = dict(stats_list[0])
    merged.update(
        {
            "cpu_percent": round(
                sum(float(s.get("cpu_percent") or 0.0) for s in stats_list) / count, 2
            ),
            "rss_mb": round(sum(float(s.get("rss_mb") or 0.0) for s in stats_list), 2),
            "load_avg_1m": round(
                sum(float(s.get("load_avg_1m") or 0.0) for s in stats_list) / count, 2
            ),
            "cpu_count": sum(int(s.get("cpu_count") or 0) for s in stats_list),
            "uptime_seconds": max(
                (float(s.get("uptime_seconds") or 0.0) for s in stats_list), default=0.0
            ),
            "active_executions": sum(int(s.get("active_executions") or 0) for s in stats_list),
            "total_executions": sum(int(s.get("total_executions") or 0) for s in stats_list),
        }
    )
    for pool_key in ("min_processes", "max_processes", "max_concurrent_executions"):
        if any(pool_key in s and s[pool_key] is not None for s in stats_list):
            merged[pool_key] = sum(int(s.get(pool_key) or 0) for s in stats_list)
    return merged


async def collect_runtime_telemetry(runtime: GatewayRuntime) -> dict[str, Any]:
    """Collect gateway process telemetry, backend adapters, and remote worker stats."""
    gw_stats = runtime.process_stats.snapshot()
    worker_info: dict[str, Any] = {"mode": runtime.settings.sandbox_backend}
    sandbox_workers: list[dict[str, Any]] = []

    if runtime.settings.sandbox_backend == "remote" and runtime.settings.sandbox_worker_url:
        health_urls = await _discover_worker_health_urls(runtime.settings.sandbox_worker_url)
        client = runtime.client_manager._get_client()

        use_pinning = runtime.client_manager._http_client is None and bool(
            getattr(runtime.client_manager, "_pinning_supported", False)
        )

        async def _probe_worker(health_url: str) -> dict[str, Any]:
            fallback_id = str(urlparse(health_url).netloc or health_url)
            pinned_token = None
            try:
                if use_pinning:
                    addresses = validate_upstream_url(
                        health_url,
                        app_env=runtime.settings.app_env,
                        block_private_ips_in_prod=False,
                        allowed_hosts=runtime.settings.allowed_upstream_hosts,
                        resolve_dns=True,
                    )
                    host = (urlparse(health_url).hostname or "").strip("[]").lower().rstrip(".")
                    if addresses and host:
                        pinned_token = _PINNED_DNS.set({host: addresses})
                resp = await client.get(health_url, timeout=1.5)
                resp.raise_for_status()
                payload = resp.json()
                if not isinstance(payload, dict):
                    payload = {}
                worker_id = str(payload.get("worker_id") or fallback_id)
                return {
                    "worker_id": worker_id,
                    "mode": "remote",
                    "health_url": health_url,
                    "status": "ok",
                    "pool": {
                        "min_processes": payload.get("min_processes"),
                        "max_processes": payload.get("max_processes"),
                        "max_concurrent_executions": payload.get("max_concurrent_executions"),
                    },
                    "stats": payload,
                }
            except Exception as exc:
                return {
                    "worker_id": fallback_id,
                    "mode": "remote",
                    "health_url": health_url,
                    "status": "unreachable",
                    "error": str(exc),
                    "pool": {
                        "min_processes": None,
                        "max_processes": None,
                        "max_concurrent_executions": None,
                    },
                    "stats": None,
                }
            finally:
                if pinned_token is not None:
                    _PINNED_DNS.reset(pinned_token)

        raw_results = await asyncio.gather(
            *(_probe_worker(url) for url in health_urls),
            return_exceptions=True,
        )
        for url, item in zip(health_urls, raw_results, strict=False):
            if isinstance(item, BaseException):
                sandbox_workers.append(
                    {
                        "worker_id": str(urlparse(url).netloc or url),
                        "mode": "remote",
                        "health_url": url,
                        "status": "unreachable",
                        "error": str(item),
                        "pool": {
                            "min_processes": None,
                            "max_processes": None,
                            "max_concurrent_executions": None,
                        },
                        "stats": None,
                    }
                )
            else:
                sandbox_workers.append(item)

        sandbox_workers.sort(key=lambda entry: (str(entry["worker_id"]), str(entry["health_url"])))
        healthy = [entry for entry in sandbox_workers if entry["status"] == "ok"]
        unhealthy = [entry for entry in sandbox_workers if entry["status"] != "ok"]
        worker_count = len(sandbox_workers)
        healthy_count = len(healthy)
        worker_info["worker_count"] = worker_count
        worker_info["healthy_count"] = healthy_count
        if health_urls:
            worker_info["health_url"] = health_urls[0]
        if worker_count > 0 and healthy_count == worker_count:
            worker_info["status"] = "ok"
            worker_info["stats"] = _aggregate_worker_stats(
                [entry["stats"] for entry in healthy if isinstance(entry.get("stats"), dict)]
            )
        elif healthy_count > 0:
            worker_info["status"] = "degraded"
            worker_info["error"] = "; ".join(
                f"{entry['worker_id']}: {entry.get('error', 'unreachable')}" for entry in unhealthy
            )
            worker_info["stats"] = _aggregate_worker_stats(
                [entry["stats"] for entry in healthy if isinstance(entry.get("stats"), dict)]
            )
        else:
            worker_info["status"] = "unreachable"
            worker_info["error"] = (
                "; ".join(str(entry.get("error") or "unreachable") for entry in unhealthy)
                or "No remote sandbox workers configured"
            )
    else:
        local_worker_id = f"{os.getenv('HOSTNAME', socket.gethostname())}-local"
        local_pool = {
            "min_processes": getattr(runtime.sandbox, "min_processes", 1),
            "max_processes": getattr(runtime.sandbox, "max_processes", 4),
        }
        sandbox_workers = [
            {
                "worker_id": local_worker_id,
                "mode": runtime.settings.sandbox_backend,
                "status": "in_process",
                "pool": local_pool,
                "stats": gw_stats,
            }
        ]
        worker_info["status"] = "in_process"
        worker_info["worker_count"] = 1
        worker_info["healthy_count"] = 1
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
        "sandbox_workers": sandbox_workers,
        "monty_telemetry": monty_telemetry,
    }
