"""In-gateway SLA/security AlertEvaluator with SSRF-guarded webhook dispatch."""

from __future__ import annotations

import time
from datetime import UTC, datetime
from typing import Any

import httpx
from pydantic import BaseModel, Field

from gateway.upstream_clients import validate_upstream_url


class AlertRulesConfig(BaseModel):
    """Configurable alert thresholds persisted in AuditStore."""

    error_rate_pct_max: float = Field(default=15.0, ge=0.1, le=100.0)
    p95_latency_ms_max: float = Field(default=1500.0, ge=10.0, le=60000.0)
    grounding_blocks_max: int = Field(default=5, ge=1, le=1000)
    webhook_url: str | None = None
    webhook_cooldown_seconds: int = Field(default=60, ge=5, le=3600)


class AlertEvaluator:
    """Evaluates live error rate, p95 latency, grounding blocks, drift, and worker health."""

    def __init__(self) -> None:
        self._last_sent: dict[str, float] = {}
        self.rules = AlertRulesConfig()

    def evaluate(
        self,
        *,
        runs: list[dict[str, Any]],
        drift_warnings: list[dict[str, str]],
        startup_warnings: list[str],
        worker_status: dict[str, Any],
    ) -> list[dict[str, Any]]:
        now_iso = datetime.now(UTC).isoformat()
        alerts: list[dict[str, Any]] = []

        if len(runs) >= 5:
            errors = sum(1 for r in runs if not r.get("ok"))
            err_pct = round((errors / len(runs)) * 100.0, 2)
            if err_pct > self.rules.error_rate_pct_max:
                alerts.append(
                    {
                        "id": "high_error_rate",
                        "severity": "critical",
                        "title": (
                            f"High MCP error rate ({err_pct}% > {self.rules.error_rate_pct_max}%)"
                        ),
                        "detail": f"{errors} failed/blocked calls in last {len(runs)} runs.",
                        "observed_at": now_iso,
                    }
                )

            durations = sorted(float(r.get("duration_ms") or 0.0) for r in runs)
            p95_idx = min(len(durations) - 1, max(0, int(0.95 * len(durations)) - 1))
            p95_ms = round(durations[p95_idx], 2)
            if p95_ms > self.rules.p95_latency_ms_max:
                alerts.append(
                    {
                        "id": "high_p95_latency",
                        "severity": "warning",
                        "title": (
                            f"p95 latency SLA breach ({p95_ms} ms > "
                            f"{self.rules.p95_latency_ms_max} ms)"
                        ),
                        "detail": f"Measured across last {len(runs)} runs.",
                        "observed_at": now_iso,
                    }
                )

        grounding_blocks = sum(1 for r in runs if r.get("grounding_blocked"))
        if grounding_blocks >= self.rules.grounding_blocks_max:
            alerts.append(
                {
                    "id": "grounding_violations",
                    "severity": "warning",
                    "title": f"Grounding violation spike ({grounding_blocks} blocked)",
                    "detail": f"Threshold is {self.rules.grounding_blocks_max} in recent window.",
                    "observed_at": now_iso,
                }
            )

        if drift_warnings:
            alerts.append(
                {
                    "id": "schema_drift",
                    "severity": "warning",
                    "title": f"Schema drift detected on {len(drift_warnings)} upstream(s)",
                    "detail": drift_warnings[0].get("message", "Pinned schema digest mismatch"),
                    "observed_at": now_iso,
                }
            )

        if startup_warnings:
            alerts.append(
                {
                    "id": "upstream_unreachable",
                    "severity": "warning",
                    "title": f"{len(startup_warnings)} upstream(s) unreachable at discovery",
                    "detail": startup_warnings[0],
                    "observed_at": now_iso,
                }
            )

        w_stat = str(worker_status.get("status") or "")
        if worker_status.get("mode") == "remote" and w_stat not in {"ok", "healthy", "connected"}:
            alerts.append(
                {
                    "id": "sandbox_worker_unhealthy",
                    "severity": "critical",
                    "title": f"Remote sandbox worker status: {w_stat or 'unknown'}",
                    "detail": str(worker_status.get("error") or "Worker health probe failed"),
                    "observed_at": now_iso,
                }
            )

        return alerts

    async def notify_webhook_if_needed(
        self,
        alerts: list[dict[str, Any]],
        *,
        app_env: str,
        block_private_ips_in_prod: bool,
        allowed_hosts: list[str],
    ) -> list[str]:
        url = (self.rules.webhook_url or "").strip()
        if not url or not alerts:
            return []
        validate_upstream_url(
            url,
            app_env=app_env,
            block_private_ips_in_prod=block_private_ips_in_prod,
            allowed_hosts=allowed_hosts,
        )
        now = time.monotonic()
        due = [
            a
            for a in alerts
            if (now - self._last_sent.get(a["id"], 0.0)) >= self.rules.webhook_cooldown_seconds
        ]
        if not due:
            return []
        try:
            async with httpx.AsyncClient(timeout=5.0) as client:
                resp = await client.post(url, json={"source": "mcpcodeaway", "alerts": due})
                resp.raise_for_status()
            for a in due:
                self._last_sent[a["id"]] = now
            return [a["id"] for a in due]
        except Exception:
            return []
