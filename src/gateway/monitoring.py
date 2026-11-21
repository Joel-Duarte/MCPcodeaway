"""Opt-in upstream probes and compact operational status snapshots."""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any

import httpx

from gateway.config import UpstreamConfig
from gateway.errors import classify_upstream_error
from gateway.upstream_clients import UpstreamClientManager


class UpstreamHealthMonitor:
    """Runs configured unauthenticated probes and applies consecutive-result thresholds."""

    def __init__(self, client_manager: UpstreamClientManager) -> None:
        self.client_manager = client_manager
        self.statuses: dict[str, dict[str, Any]] = {}
        self._last_checked: dict[str, float] = {}
        self._probe_urls: dict[str, str] = {}

    async def check(self, upstream: UpstreamConfig) -> dict[str, Any] | None:
        config = upstream.health_check
        if not config.enabled or not config.url:
            self.statuses.pop(upstream.name, None)
            return None

        if self._probe_urls.get(upstream.name) != config.url:
            self.statuses.pop(upstream.name, None)
            self._probe_urls[upstream.name] = config.url
        previous = self.statuses.get(upstream.name, {})
        try:
            response = await self.client_manager.probe_health(
                config.url, timeout_seconds=config.timeout_seconds
            )
            if not 200 <= response.status_code < 300:
                raise httpx.HTTPStatusError(
                    f"Health endpoint returned HTTP {response.status_code}",
                    request=response.request,
                    response=response,
                )
            success = True
            error_type = None
            message = None
            status_code = response.status_code
        except Exception as exc:
            success = False
            failure = classify_upstream_error(exc)
            error_type = failure.error_type
            message = failure.message
            status_code = failure.status_code

        successes = int(previous.get("consecutive_successes", 0)) + 1 if success else 0
        failures = int(previous.get("consecutive_failures", 0)) + 1 if not success else 0
        state = str(previous.get("status", "unknown"))
        if success:
            if successes >= config.success_threshold or state == "unknown":
                state = "healthy"
            elif state not in {"healthy", "unhealthy"}:
                state = "degraded"
        else:
            state = "unhealthy" if failures >= config.failure_threshold else "degraded"
        snapshot = {
            "server": upstream.name,
            "status": state,
            "enabled": True,
            "url": config.url,
            "checked_at": datetime.now(UTC).isoformat(),
            "last_success_at": datetime.now(UTC).isoformat()
            if success
            else previous.get("last_success_at"),
            "consecutive_failures": failures,
            "consecutive_successes": successes,
            "error_type": error_type,
            "error": message,
            "status_code": status_code,
        }
        self.statuses[upstream.name] = snapshot
        return snapshot

    async def run(self, upstreams: Callable[[], list[UpstreamConfig]]) -> None:
        """Poll enabled endpoints on their configured cadence until cancelled."""
        while True:
            now = asyncio.get_running_loop().time()
            for upstream in upstreams():
                if not upstream.health_check.enabled:
                    self.statuses.pop(upstream.name, None)
                    self._probe_urls.pop(upstream.name, None)
                    self._last_checked.pop(upstream.name, None)
                    continue
                if self._probe_urls.get(upstream.name) != upstream.health_check.url:
                    self._last_checked.pop(upstream.name, None)
                due = (
                    self._last_checked.get(upstream.name, 0.0)
                    + upstream.health_check.interval_seconds
                )
                if now < due:
                    continue
                self._last_checked[upstream.name] = now
                try:
                    await self.check(upstream)
                except asyncio.CancelledError:
                    raise
                except Exception:
                    continue
            await asyncio.sleep(1.0)

    def list_statuses(self) -> list[dict[str, Any]]:
        return [self.statuses[name] for name in sorted(self.statuses)]

    def is_circuit_open(self, upstream_name: str) -> bool:
        """Only explicitly configured probes can pause traffic to their upstream."""
        return self.statuses.get(upstream_name, {}).get("status") == "unhealthy"
