"""OpenTelemetry setup and structured execution audit recording."""

from __future__ import annotations

import json
import math
import uuid
from datetime import UTC, datetime
from typing import Any

from opentelemetry import trace
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import TracerProvider

from gateway.auth import CallerIdentity
from gateway.dispatcher import SubcallRecord
from gateway.state import AuditStore

_OTEL_CONFIGURED = False


def configure_telemetry(service_name: str = "mcpcodeaway-gateway") -> trace.Tracer:
    """Initialize OpenTelemetry TracerProvider once and return the gateway tracer."""
    global _OTEL_CONFIGURED
    if not _OTEL_CONFIGURED:
        provider = TracerProvider(resource=Resource.create({"service.name": service_name}))
        trace.set_tracer_provider(provider)
        _OTEL_CONFIGURED = True
    return trace.get_tracer(service_name)


def _approx_tokens(value: Any) -> int:
    text = value if isinstance(value, str) else json.dumps(value, default=str)
    return max(1, math.ceil(len(text) / 4)) if text else 0


class TelemetryRecorder:
    """Persists execution audit runs and computes summary metrics."""

    def __init__(self, store: AuditStore) -> None:
        self.store = store

    async def record_execution(
        self,
        *,
        mode: str,
        identity: CallerIdentity,
        catalog_version: str,
        schema_tokens: int,
        subcalls: list[SubcallRecord],
        returned_payload: Any,
        duration_ms: float,
        ok: bool,
        code: str | None = None,
        stdout: str = "",
        error: str | None = None,
        tool_name: str | None = None,
    ) -> dict[str, Any]:
        raw_upstream_bytes = sum(sc.raw_bytes for sc in subcalls)
        returned_bytes = len(json.dumps(returned_payload, default=str).encode("utf-8"))
        upstream_tokens = max(1, math.ceil(raw_upstream_bytes / 4)) if raw_upstream_bytes else 0
        returned_tokens = _approx_tokens(returned_payload)
        estimated_tokens_saved = max(0, upstream_tokens - returned_tokens)

        token_hops: list[dict[str, Any]] = []
        seen_hops: set[tuple[str | None, str, bool]] = set()
        for sc in subcalls:
            if sc.auth_type == "delegated_token":
                hop_key = (sc.downstream_audience, sc.server, sc.token_cache_hit)
                if hop_key not in seen_hops:
                    seen_hops.add(hop_key)
                    token_hops.append(
                        {
                            "server": sc.server,
                            "audience": sc.downstream_audience,
                            "cache_hit": sc.token_cache_hit,
                        }
                    )

        run = {
            "run_id": f"run_{uuid.uuid4().hex[:12]}",
            "created_at": datetime.now(UTC).isoformat(),
            "mode": mode,
            "ok": ok,
            "tenant_id": identity.tenant_id,
            "user_sub": identity.sub,
            "user_email": identity.email,
            "user_roles": identity.roles,
            "client_id": identity.client_id,
            "catalog_version": catalog_version,
            "tool_name": tool_name,
            "code": code,
            "stdout": stdout,
            "error": error,
            "subcalls": [sc.model_dump() for sc in subcalls],
            "subcall_count": len(subcalls),
            "token_hops": token_hops,
            "schema_tokens": schema_tokens,
            "raw_upstream_bytes": raw_upstream_bytes,
            "returned_bytes": returned_bytes,
            "estimated_tokens_saved": estimated_tokens_saved,
            "duration_ms": round(duration_ms, 3),
        }
        await self.store.record_run(run)
        return run

    async def summary(self, limit: int = 200) -> dict[str, Any]:
        runs = await self.store.list_runs(limit=limit)
        by_mode: dict[str, dict[str, Any]] = {}
        for run in runs:
            mode = str(run.get("mode", "unknown"))
            bucket = by_mode.setdefault(
                mode,
                {
                    "runs": 0,
                    "subcalls": 0,
                    "raw_upstream_bytes": 0,
                    "returned_bytes": 0,
                    "estimated_tokens_saved": 0,
                    "avg_duration_ms": 0.0,
                    "_total_ms": 0.0,
                },
            )
            bucket["runs"] += 1
            bucket["subcalls"] += int(run.get("subcall_count", 0))
            bucket["raw_upstream_bytes"] += int(run.get("raw_upstream_bytes", 0))
            bucket["returned_bytes"] += int(run.get("returned_bytes", 0))
            bucket["estimated_tokens_saved"] += int(run.get("estimated_tokens_saved", 0))
            bucket["_total_ms"] += float(run.get("duration_ms", 0.0))

        for bucket in by_mode.values():
            total_ms = bucket.pop("_total_ms")
            runs_cnt = bucket["runs"]
            bucket["avg_duration_ms"] = round(total_ms / runs_cnt, 3) if runs_cnt else 0.0

        return {"total_runs": len(runs), "by_mode": by_mode}
