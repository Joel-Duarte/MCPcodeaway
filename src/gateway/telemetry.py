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
from opentelemetry.sdk.trace.export import BatchSpanProcessor, SimpleSpanProcessor

from gateway.auth import CallerIdentity
from gateway.dispatcher import SubcallRecord
from gateway.state import AuditStore

_OTEL_CONFIGURED = False
_ATTACHED_EXPORTERS: set[int] = set()


def configure_telemetry(
    service_name: str = "mcpcodeaway-gateway",
    otlp_endpoint: str | None = None,
    span_exporter: Any | None = None,
) -> trace.Tracer:
    """Initialize OpenTelemetry TracerProvider once and optionally attach an exporter."""
    global _OTEL_CONFIGURED
    if not _OTEL_CONFIGURED:
        provider = TracerProvider(resource=Resource.create({"service.name": service_name}))
        trace.set_tracer_provider(provider)
        _OTEL_CONFIGURED = True

    active_provider = trace.get_tracer_provider()
    exporter = span_exporter
    use_simple_processor = span_exporter is not None
    if exporter is None and otlp_endpoint:
        try:
            from opentelemetry.exporter.otlp.proto.http.trace_exporter import (
                OTLPSpanExporter,
            )
        except ImportError as exc:
            raise RuntimeError(
                "otlp_endpoint requires optional dependency 'mcpcodeaway[otlp]' "
                "(opentelemetry-exporter-otlp-proto-http)."
            ) from exc
        exporter = OTLPSpanExporter(endpoint=otlp_endpoint)

    if (
        exporter is not None
        and isinstance(active_provider, TracerProvider)
        and id(exporter) not in _ATTACHED_EXPORTERS
    ):
        processor = (
            SimpleSpanProcessor(exporter)
            if use_simple_processor
            else BatchSpanProcessor(exporter)
        )
        active_provider.add_span_processor(processor)
        _ATTACHED_EXPORTERS.add(id(exporter))

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
        provenance: dict[str, Any] | None = None,
        classic_schema_tokens: int | None = None,
        grounding_blocked: bool = False,
    ) -> dict[str, Any]:
        raw_upstream_bytes = sum(sc.raw_bytes for sc in subcalls)
        returned_bytes = len(json.dumps(returned_payload, default=str).encode("utf-8"))
        upstream_tokens = max(1, math.ceil(raw_upstream_bytes / 4)) if raw_upstream_bytes else 0
        returned_tokens = _approx_tokens(returned_payload)
        estimated_tokens_saved = max(0, upstream_tokens - returned_tokens)
        schema_tokens_saved = (
            max(0, classic_schema_tokens - schema_tokens)
            if classic_schema_tokens is not None
            else 0
        )
        turns_avoided = max(0, len(subcalls) - 1) if tool_name == "execute_code" else 0
        abac_rows_filtered = sum(sc.abac_rows_filtered for sc in subcalls)
        redactions_count = sum(sc.redactions_count for sc in subcalls)

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
            "classic_schema_tokens": classic_schema_tokens,
            "schema_tokens_saved": schema_tokens_saved,
            "turns_avoided": turns_avoided,
            "abac_rows_filtered": abac_rows_filtered,
            "redactions_count": redactions_count,
            "grounding_blocked": grounding_blocked,
            "raw_upstream_bytes": raw_upstream_bytes,
            "returned_bytes": returned_bytes,
            "returned_tokens": returned_tokens,
            "estimated_tokens_saved": estimated_tokens_saved,
            "duration_ms": round(duration_ms, 3),
        }
        if provenance is not None:
            run["provenance"] = provenance
        await self.store.record_run(run)
        return run

    async def summary(self, limit: int = 200, tenant_id: str | None = None) -> dict[str, Any]:
        runs = await self.store.list_runs(limit=limit, tenant_id=tenant_id)
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

    async def value_report(
        self,
        *,
        limit: int = 500,
        tenant_id: str | None = None,
        cost_per_1m_input_tokens_usd: float = 3.0,
        context_overflow_threshold_tokens: int = 32768,
    ) -> dict[str, Any]:
        """Compute ROI, token/cost savings, overflow preventions, and governance impact."""
        runs = await self.store.list_runs(limit=limit, tenant_id=tenant_id)

        total_subcalls = 0
        raw_bytes_total = 0
        returned_bytes_total = 0
        payload_tokens_saved = 0
        schema_tokens_saved = 0
        turns_avoided = 0
        overflows_prevented = 0
        obo_hits = 0
        obo_misses = 0
        abac_filtered = 0
        redactions_total = 0
        grounded_verified = 0
        grounding_blocked = 0
        failed_or_blocked = 0
        by_upstream: dict[str, dict[str, Any]] = {}

        for run in runs:
            mode = str(run.get("mode", "unknown"))
            raw_b = int(run.get("raw_upstream_bytes", 0))
            ret_b = int(run.get("returned_bytes", 0))
            ret_tok = int(
                run.get("returned_tokens")
                or (max(1, math.ceil(ret_b / 4)) if ret_b else 0)
            )
            raw_tok = max(1, math.ceil(raw_b / 4)) if raw_b else 0

            total_subcalls += int(run.get("subcall_count", 0))
            raw_bytes_total += raw_b
            returned_bytes_total += ret_b
            payload_tokens_saved += int(run.get("estimated_tokens_saved", 0))
            schema_tokens_saved += int(run.get("schema_tokens_saved", 0))
            turns_avoided += int(run.get("turns_avoided", 0))
            abac_filtered += int(run.get("abac_rows_filtered", 0))
            redactions_total += int(run.get("redactions_count", 0))

            if (
                (mode.startswith("codemode") or mode == "hybrid")
                and raw_tok >= context_overflow_threshold_tokens
                and ret_tok < context_overflow_threshold_tokens
            ):
                overflows_prevented += 1

            if run.get("grounding_blocked"):
                grounding_blocked += 1
            if not run.get("ok"):
                failed_or_blocked += 1
            prov = run.get("provenance")
            if isinstance(prov, dict) and prov.get("grounded") and run.get("ok"):
                grounded_verified += 1

            subcalls = run.get("subcalls") or []
            for sc in subcalls:
                srv = str(sc.get("server") or "unknown")
                entry = by_upstream.setdefault(
                    srv,
                    {
                        "calls": 0,
                        "errors": 0,
                        "raw_bytes": 0,
                        "abac_rows_filtered": 0,
                        "redactions_count": 0,
                        "classic_calls": 0,
                        "codemode_calls": 0,
                        "_total_ms": 0.0,
                    },
                )
                entry["calls"] += 1
                if sc.get("status") != "ok":
                    entry["errors"] += 1
                entry["raw_bytes"] += int(sc.get("raw_bytes", 0))
                entry["abac_rows_filtered"] += int(sc.get("abac_rows_filtered", 0))
                entry["redactions_count"] += int(sc.get("redactions_count", 0))
                entry["_total_ms"] += float(sc.get("duration_ms", 0.0))
                if mode.startswith("codemode") or mode == "hybrid":
                    entry["codemode_calls"] += 1
                else:
                    entry["classic_calls"] += 1

                if sc.get("auth_type") == "delegated_token":
                    if sc.get("token_cache_hit"):
                        obo_hits += 1
                    else:
                        obo_misses += 1

        recommendations: list[dict[str, Any]] = []
        for srv, entry in by_upstream.items():
            calls = entry["calls"]
            total_ms = entry.pop("_total_ms")
            entry["avg_duration_ms"] = round(total_ms / calls, 3) if calls else 0.0
            classic_calls = entry["classic_calls"]
            if classic_calls > 0 and (entry["raw_bytes"] / classic_calls) >= 8000:
                recommendations.append(
                    {
                        "server": srv,
                        "reason": (
                            f"{classic_calls} classic calls averaged "
                            f"{round(entry['raw_bytes'] / classic_calls)} bytes/call; "
                            "switch callers to codemode or set preferred_mode: codemode."
                        ),
                    }
                )

        total_tokens_saved = payload_tokens_saved + schema_tokens_saved
        est_usd = round((total_tokens_saved / 1_000_000.0) * cost_per_1m_input_tokens_usd, 6)
        payload_reduction_pct = (
            round(max(0.0, (raw_bytes_total - returned_bytes_total) / raw_bytes_total) * 100, 2)
            if raw_bytes_total > 0
            else 0.0
        )
        obo_total = obo_hits + obo_misses
        obo_hit_rate = round((obo_hits / obo_total) * 100, 2) if obo_total > 0 else 0.0

        return {
            "window_runs": len(runs),
            "tenant_scope": tenant_id or "all_tenants",
            "pricing_assumptions": {
                "cost_per_1m_input_tokens_usd": cost_per_1m_input_tokens_usd,
                "context_overflow_threshold_tokens": context_overflow_threshold_tokens,
            },
            "efficiency": {
                "total_subcalls": total_subcalls,
                "total_raw_upstream_bytes": raw_bytes_total,
                "total_returned_bytes": returned_bytes_total,
                "payload_reduction_pct": payload_reduction_pct,
                "payload_tokens_saved": payload_tokens_saved,
                "schema_tokens_saved": schema_tokens_saved,
                "total_tokens_saved": total_tokens_saved,
                "estimated_llm_cost_saved_usd": est_usd,
                "llm_turns_avoided": turns_avoided,
                "context_overflows_prevented": overflows_prevented,
                "obo_token_cache_hits": obo_hits,
                "obo_token_cache_misses": obo_misses,
                "obo_cache_hit_rate_pct": obo_hit_rate,
            },
            "security_and_governance": {
                "abac_rows_filtered": abac_filtered,
                "pii_secrets_redacted": redactions_total,
                "grounded_runs_verified": grounded_verified,
                "grounding_violations_blocked": grounding_blocked,
                "failed_or_blocked_runs": failed_or_blocked,
            },
            "by_upstream": by_upstream,
            "recommendations": recommendations,
        }
