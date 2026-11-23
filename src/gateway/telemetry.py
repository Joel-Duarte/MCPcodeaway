"""OpenTelemetry setup and structured execution audit recording."""

from __future__ import annotations

import json
import math
import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

from opentelemetry import trace
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor, SimpleSpanProcessor

from gateway.auth import CallerIdentity
from gateway.dispatcher import OutputRedactor, SubcallRecord
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
            SimpleSpanProcessor(exporter) if use_simple_processor else BatchSpanProcessor(exporter)
        )
        active_provider.add_span_processor(processor)
        _ATTACHED_EXPORTERS.add(id(exporter))

    return trace.get_tracer(service_name)


def _approx_tokens(value: Any) -> int:
    text = value if isinstance(value, str) else json.dumps(value, default=str)
    return max(1, math.ceil(len(text) / 4)) if text else 0


def _expected_average_minutes(assumption: Any) -> float | None:
    if not isinstance(assumption, dict) or assumption.get("kind") != "expected_average":
        return None
    value = assumption.get("minutes")
    if isinstance(value, bool) or not isinstance(value, int | float):
        return None
    return float(value)


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
        mode_decision_reason: str | None = None,
        mode_decision_score: float | None = None,
        experiment_id: str | None = None,
        ab_variant: str | None = None,
        model_name: str | None = None,
        monty_spawn_ms: float = 0.0,
        monty_exec_ms: float = 0.0,
        trace_spans: list[dict[str, Any]] | None = None,
        intent: str | None = None,
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
        is_cm = mode.startswith("codemode") or mode == "hybrid"
        tokens_used_est = schema_tokens + (
            returned_tokens if is_cm else upstream_tokens or returned_tokens
        )
        if is_cm and (estimated_tokens_saved > 0 or schema_tokens_saved > 0):
            parts: list[str] = ["Saved vs. Traditional MCP via Code Mode"]
            if schema_tokens_saved > 0:
                parts.append("Deferred Schema Search")
            tokens_saved_by = " + ".join(parts)
        elif is_cm:
            tokens_saved_by = "Saved vs. Traditional MCP via Code Mode (minimal payload)"
        else:
            tokens_saved_by = "Traditional MCP (no Code Mode batching savings)"

        if trace_spans is None:
            subcall_total_ms = round(sum(sc.duration_ms for sc in subcalls), 3)
            known_ms = monty_spawn_ms + monty_exec_ms + subcall_total_ms
            overhead_ms = max(0.2, round(duration_ms - known_ms, 3))
            auth_ms = round(overhead_ms * 0.35, 3)
            route_ms = round(overhead_ms * 0.25, 3)
            sec_ms = round(max(0.1, overhead_ms - auth_ms - route_ms), 3)
            cursor = 0.0
            spans: list[dict[str, Any]] = [
                {
                    "stage": "gateway_auth",
                    "label": "Gateway Auth & RBAC",
                    "start_ms": cursor,
                    "duration_ms": auth_ms,
                    "status": "ok",
                }
            ]
            cursor = round(cursor + auth_ms, 3)
            spans.append(
                {
                    "stage": "mode_route",
                    "label": f"Mode Routing ({mode})",
                    "start_ms": cursor,
                    "duration_ms": route_ms,
                    "status": "ok",
                }
            )
            cursor = round(cursor + route_ms, 3)
            if is_cm and (monty_spawn_ms > 0 or monty_exec_ms > 0):
                spans.append(
                    {
                        "stage": "monty_spawn",
                        "label": "Monty Sandbox Spawn (Ephemeral)",
                        "start_ms": cursor,
                        "duration_ms": round(monty_spawn_ms, 3),
                        "status": "ok",
                    }
                )
                cursor = round(cursor + monty_spawn_ms, 3)
                spans.append(
                    {
                        "stage": "monty_exec",
                        "label": "Monty Python Execution",
                        "start_ms": cursor,
                        "duration_ms": round(monty_exec_ms, 3),
                        "status": "ok" if ok else "error",
                    }
                )
                cursor = round(cursor + monty_exec_ms, 3)
            for sc in subcalls:
                spans.append(
                    {
                        "stage": "upstream_subcall",
                        "label": f"{sc.server}.{sc.tool}",
                        "start_ms": cursor,
                        "duration_ms": round(sc.duration_ms, 3),
                        "status": "ok" if sc.status == "ok" else "error",
                    }
                )
                cursor = round(cursor + sc.duration_ms, 3)
            spans.append(
                {
                    "stage": "security_guard",
                    "label": "ABAC / PII / Grounding Check",
                    "start_ms": cursor,
                    "duration_ms": sec_ms,
                    "status": "blocked" if grounding_blocked else ("ok" if ok else "error"),
                }
            )
            trace_spans = spans
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

        inferred_server = (
            subcalls[0].server
            if subcalls
            else (tool_name.split("__", 1)[0] if "__" in (tool_name or "") else "gateway")
        )
        eff_input_tokens = max(0, int(tokens_used_est))
        eff_output_tokens = max(0, int(returned_tokens))
        eff_total_tokens = eff_input_tokens + eff_output_tokens
        adaptive_marker = next(
            (
                part.strip()
                for part in str(mode_decision_reason or "").split(";")
                if part.strip().startswith(
                    (
                        "adaptive_recommendation:",
                        "adaptive_exact:",
                        "adaptive_workload_family:",
                        "adaptive_model_wide:",
                    )
                )
            ),
            "",
        )
        recommended_candidate = adaptive_marker.split(":")[-1] if adaptive_marker else None
        recommended_mode = (
            recommended_candidate if recommended_candidate in {"traditional", "codemode"} else None
        )
        run = {
            "run_id": f"run_{uuid.uuid4().hex[:12]}",
            "created_at": datetime.now(UTC).isoformat(),
            "mode": mode,
            "applied_mode": mode,
            "recommended_mode": recommended_mode,
            "ok": ok,
            "tenant_id": identity.tenant_id,
            "user_sub": identity.sub,
            "user_email": identity.email,
            "user_roles": identity.roles,
            "user_groups": list(identity.groups),
            "user_client_groups": list(identity.client_groups),
            "client_id": identity.client_id,
            "catalog_version": catalog_version,
            "tool_name": tool_name,
            "server_name": inferred_server,
            "input_tokens": eff_input_tokens,
            "output_tokens": eff_output_tokens,
            "total_tokens": eff_total_tokens,
            "token_source": "estimated",
            "code": OutputRedactor.redact_text(code) if code else None,
            "stdout": OutputRedactor.redact_text(stdout) if stdout else "",
            "error": OutputRedactor.redact_text(error) if error else None,
            "subcalls": [OutputRedactor.redact(sc.model_dump()) for sc in subcalls],
            "subcall_count": len(subcalls),
            "token_hops": token_hops,
            "schema_tokens": schema_tokens,
            "classic_schema_tokens": classic_schema_tokens,
            "schema_tokens_saved": schema_tokens_saved,
            "turns_avoided": turns_avoided,
            "abac_rows_filtered": abac_rows_filtered,
            "redactions_count": redactions_count,
            "grounding_blocked": grounding_blocked,
            "mode_decision_reason": mode_decision_reason,
            "mode_decision_score": mode_decision_score,
            "experiment_id": experiment_id,
            "ab_variant": ab_variant,
            "model_name": model_name,
            "raw_upstream_bytes": raw_upstream_bytes,
            "returned_bytes": returned_bytes,
            "returned_tokens": returned_tokens,
            "tokens_used_est": tokens_used_est,
            "tokens_saved_by": tokens_saved_by,
            "estimated_tokens_saved": estimated_tokens_saved,
            "monty_spawn_ms": round(monty_spawn_ms, 3),
            "monty_exec_ms": round(monty_exec_ms, 3),
            "trace_spans": trace_spans,
            "duration_ms": round(duration_ms, 3),
            "intent": OutputRedactor.redact_intent(intent),
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

    async def business_value_report(
        self, *, tenant_id: str, since: str | None = None, limit: int = 1000
    ) -> dict[str, Any]:
        """Aggregate durable outcomes by integration, definition, revision, and UTC day.

        Gross manual baseline is not presented as net savings. Net estimate is only
        calculated when both review and correction assumptions are explicit expected
        averages. Actual review/rework capture and automation-cost inputs are not yet
        in the ledger, so validated net savings and ROI remain unavailable.
        """
        if not tenant_id:
            raise ValueError("business-value reports require an explicit tenant scope")
        events = await self.store.list_business_value_events(tenant_id, since=since, limit=limit)
        executions = await self.store.list_business_executions(tenant_id, since=since, limit=limit)
        execution_status_counts = {
            status: 0 for status in ("active", "completed", "outcome_unknown", "failed", "expired")
        }
        report_now = datetime.now(UTC)
        for execution in executions:
            status = str(execution.get("status") or "unknown")
            if status == "active":
                expires_at = execution.get("expires_at")
                if isinstance(expires_at, str):
                    try:
                        expiry = datetime.fromisoformat(expires_at.replace("Z", "+00:00"))
                        if expiry.tzinfo is None:
                            expiry = expiry.replace(tzinfo=UTC)
                        if expiry <= report_now:
                            status = "expired"
                    except ValueError:
                        pass
            execution_status_counts[status] = execution_status_counts.get(status, 0) + 1
        grouped: dict[tuple[str, ...], dict[str, Any]] = {}
        for event in events:
            if not bool(event.get("credited")):
                continue
            evidence = event.get("evidence")
            evidence = evidence if isinstance(evidence, dict) else {}
            baseline = evidence.get("baseline_snapshot")
            if not isinstance(baseline, dict):
                continue
            upstream = str(event.get("upstream_name") or "unknown")
            integration_id = str(evidence.get("integration_id") or upstream)
            integration_display_name = evidence.get("integration_display_name")
            integration_display_name = (
                integration_display_name if isinstance(integration_display_name, str) else None
            )
            selected_kind = str(evidence.get("selected_kind") or "unknown")
            selected_id = str(evidence.get("selected_id") or "unknown")
            revision = str(evidence.get("definition_revision") or "unknown")
            created_at = str(event.get("created_at") or "")
            try:
                instant = datetime.fromisoformat(created_at.replace("Z", "+00:00"))
                if instant.tzinfo is None:
                    instant = instant.replace(tzinfo=UTC)
                utc_day = instant.astimezone(UTC).date().isoformat()
            except ValueError:
                utc_day = "unknown"
            source = str(baseline.get("baseline_source") or "unknown")
            confidence = str(baseline.get("confidence") or "unknown")
            currency = baseline.get("currency")
            currency = str(currency) if isinstance(currency, str) else None
            key = (
                integration_id,
                upstream,
                selected_kind,
                selected_id,
                revision,
                source,
                confidence,
                currency or "",
                utc_day,
            )
            bucket = grouped.setdefault(
                key,
                {
                    "integration_id": integration_id,
                    "integration_display_name": integration_display_name,
                    "upstream_name": upstream,
                    "integration_upstream_name": upstream,
                    "selected_kind": selected_kind,
                    "selected_id": selected_id,
                    "definition_revision": revision,
                    "period_utc": utc_day,
                    "baseline_source": source,
                    "baseline_confidence": confidence,
                    "currency": currency,
                    "completed_count": 0,
                    "estimated_manual_baseline_minutes": 0.0,
                    "validated_manual_baseline_minutes": 0.0,
                    "estimated_minutes_saved": 0.0,
                    "_estimated_savings_complete": True,
                    "estimated_labor_value": 0.0,
                    "_labor_value_complete": True,
                },
            )
            bucket["completed_count"] += 1
            try:
                manual_minutes = float(baseline["manual_baseline_minutes"])
            except (KeyError, TypeError, ValueError):
                bucket["_estimated_savings_complete"] = False
                bucket["_labor_value_complete"] = False
                continue
            bucket["estimated_manual_baseline_minutes"] += manual_minutes
            if confidence in {"sampled", "validated"}:
                bucket["validated_manual_baseline_minutes"] += manual_minutes
            review = _expected_average_minutes(baseline.get("review_minutes"))
            correction = _expected_average_minutes(baseline.get("correction_minutes"))
            if review is None or correction is None:
                bucket["_estimated_savings_complete"] = False
                bucket["_labor_value_complete"] = False
                continue
            estimated_saved = manual_minutes - review - correction
            bucket["estimated_minutes_saved"] += estimated_saved
            hourly_cost = baseline.get("loaded_labor_cost_per_hour")
            if not isinstance(hourly_cost, (int, float)) or isinstance(hourly_cost, bool):
                bucket["_labor_value_complete"] = False
                continue
            bucket["estimated_labor_value"] += estimated_saved * float(hourly_cost) / 60.0

        by_period: list[dict[str, Any]] = []
        totals_by_currency: dict[str, dict[str, Any]] = {}
        for key in sorted(grouped):
            bucket = grouped[key]
            currency_key = str(bucket["currency"] or "unpriced")
            if not bucket.pop("_estimated_savings_complete"):
                bucket["estimated_minutes_saved"] = None
            else:
                bucket["estimated_minutes_saved"] = round(bucket["estimated_minutes_saved"], 3)
            if not bucket.pop("_labor_value_complete"):
                bucket["estimated_labor_value"] = None
            else:
                bucket["estimated_labor_value"] = {
                    "amount": round(bucket["estimated_labor_value"], 4),
                    "currency": currency_key,
                }
            for metric in (
                "estimated_manual_baseline_minutes",
                "validated_manual_baseline_minutes",
            ):
                bucket[metric] = round(bucket[metric], 3)
            totals = totals_by_currency.setdefault(
                currency_key,
                {
                    "completed_count": 0,
                    "estimated_manual_baseline_minutes": 0.0,
                    "validated_manual_baseline_minutes": 0.0,
                    "estimated_minutes_saved": 0.0,
                    "_estimated_savings_complete": True,
                },
            )
            totals["completed_count"] += bucket["completed_count"]
            totals["estimated_manual_baseline_minutes"] += bucket[
                "estimated_manual_baseline_minutes"
            ]
            totals["validated_manual_baseline_minutes"] += bucket[
                "validated_manual_baseline_minutes"
            ]
            if bucket["estimated_minutes_saved"] is None:
                totals["_estimated_savings_complete"] = False
            elif totals["_estimated_savings_complete"]:
                totals["estimated_minutes_saved"] += bucket["estimated_minutes_saved"]
            by_period.append(bucket)

        for totals in totals_by_currency.values():
            totals["estimated_manual_baseline_minutes"] = round(
                totals["estimated_manual_baseline_minutes"], 3
            )
            totals["validated_manual_baseline_minutes"] = round(
                totals["validated_manual_baseline_minutes"], 3
            )
            if totals.pop("_estimated_savings_complete"):
                totals["estimated_minutes_saved"] = round(totals["estimated_minutes_saved"], 3)
            else:
                totals["estimated_minutes_saved"] = None

        episodes = (
            await self.store.list_workflow_episodes(tenant_id, limit=limit)
            if hasattr(self.store, "list_workflow_episodes")
            else []
        )
        episode_status_counts: dict[str, int] = {
            status: 0
            for status in (
                "open",
                "completed_verified",
                "completed_unverified",
                "partial_or_incomplete",
                "abandoned",
                "read_only",
            )
        }
        attribution_mode_counts: dict[str, int] = {"passive_matched": 0, "explicit_legacy": 0}
        for ep in episodes:
            st = str(ep.get("status") or "unknown")
            episode_status_counts[st] = episode_status_counts.get(st, 0) + 1
        for event in events:
            if not bool(event.get("credited")):
                continue
            ev = event.get("evidence") if isinstance(event.get("evidence"), dict) else {}
            am = str(ev.get("attribution_mode") or "explicit_legacy")
            attribution_mode_counts[am] = attribution_mode_counts.get(am, 0) + 1
        pending_candidates = (
            await self.store.list_discovered_workflow_candidates(
                tenant_id, status="pending_review", limit=200
            )
            if hasattr(self.store, "list_discovered_workflow_candidates")
            else []
        )

        return {
            "tenant_scope": tenant_id,
            "since": since,
            "events_returned": len(events),
            "truncated": len(events) >= limit,
            "executions_returned": len(executions),
            "executions_truncated": len(executions) >= limit,
            "completed_count": sum(item["completed_count"] for item in by_period),
            "execution_status_counts": execution_status_counts,
            "attribution_mode_counts": attribution_mode_counts,
            "episode_summary": {
                "total": len(episodes),
                "status_counts": episode_status_counts,
                "pending_baseline_candidates": len(pending_candidates),
            },
            "by_integration_workflow_day": by_period,
            "totals_by_currency": totals_by_currency,
            "validated_minutes_saved": None,
            "cost_coverage": {
                "captured_categories": [],
                "missing_categories": [
                    "llm_inference",
                    "gateway_operations",
                    "upstream_or_vendor_costs",
                ],
                "roi_available": False,
            },
        }

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
                run.get("returned_tokens") or (max(1, math.ceil(ret_b / 4)) if ret_b else 0)
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

    async def analytics_report(
        self,
        *,
        limit: int = 1000,
        tenant_id: str | None = None,
        group_filter: str | None = None,
        user_filter: str | None = None,
        upstream_filter: str | None = None,
        model_name: str = "default",
        model_pricing: dict[str, dict[str, float]] | None = None,
        window_hours: int | None = None,
    ) -> dict[str, Any]:
        """Aggregate usage, USD cost, mode decisions, and A/B experiments by group & user."""
        pricing_table = model_pricing or {"default": {"input": 3.0, "output": 15.0}}
        active_price = (
            pricing_table.get(model_name)
            or pricing_table.get("default")
            or {
                "input": 3.0,
                "output": 15.0,
            }
        )
        in_rate = float(active_price.get("input", 3.0))
        out_rate = float(active_price.get("output", 15.0))

        all_runs = await self.store.list_runs(limit=limit, tenant_id=tenant_id)
        gf = (group_filter or "").strip().lower()
        uf = (user_filter or "").strip().lower()
        sf = (upstream_filter or "").strip().lower()
        runs: list[dict[str, Any]] = []
        cutoff_iso: str | None = None
        if window_hours is not None and window_hours > 0:
            cutoff_iso = (datetime.now(UTC) - timedelta(hours=window_hours)).isoformat()
        for run in all_runs:
            if cutoff_iso is not None and str(run.get("created_at") or "") < cutoff_iso:
                continue
            groups = [str(g) for g in (run.get("user_groups") or [])] or ["unassigned"]
            user_sub = str(run.get("user_sub") or "unknown")
            user_email = str(run.get("user_email") or "")
            subcalls = run.get("subcalls") or []
            servers = {str(sc.get("server") or "unknown") for sc in subcalls}
            if gf and not any(gf in g.lower() for g in groups):
                continue
            if uf and uf not in user_sub.lower() and uf not in user_email.lower():
                continue
            if sf and not any(sf in s.lower() for s in servers):
                continue
            runs.append(run)

        group_buckets: dict[str, dict[str, Any]] = {}
        client_group_buckets: dict[str, dict[str, Any]] = {}
        user_buckets: dict[str, dict[str, Any]] = {}
        decision_buckets: dict[tuple[str, str], dict[str, Any]] = {}
        exp_buckets: dict[str, dict[str, dict[str, Any]]] = {}

        total_classic = 0
        total_codemode = 0
        total_tokens_saved = 0
        total_cost_usd = 0.0
        total_cost_saved_usd = 0.0

        for run in runs:
            mode = str(run.get("mode") or "unknown")
            is_cm = mode.startswith("codemode") or mode == "hybrid"
            if is_cm:
                total_codemode += 1
            else:
                total_classic += 1

            schema_tok = int(run.get("schema_tokens", 0))
            ret_tok = int(run.get("returned_tokens") or _approx_tokens(run.get("returned_bytes")))
            raw_b = int(run.get("raw_upstream_bytes", 0))
            in_tok = schema_tok + (
                ret_tok if is_cm else (max(1, math.ceil(raw_b / 4)) if raw_b else ret_tok)
            )
            out_tok = ret_tok if not is_cm else max(60, ret_tok // 2)
            tok_saved = int(run.get("estimated_tokens_saved", 0)) + int(
                run.get("schema_tokens_saved", 0)
            )
            run_cost = ((in_tok / 1_000_000.0) * in_rate) + ((out_tok / 1_000_000.0) * out_rate)
            run_saved_usd = (tok_saved / 1_000_000.0) * in_rate
            total_tokens_saved += tok_saved
            total_cost_usd += run_cost
            total_cost_saved_usd += run_saved_usd

            subcalls = run.get("subcalls") or []
            subcall_count = int(run.get("subcall_count", 0))
            abac_n = int(run.get("abac_rows_filtered", 0))
            red_n = int(run.get("redactions_count", 0))
            dur_ms = float(run.get("duration_ms", 0.0))
            user_sub = str(run.get("user_sub") or "unknown")
            groups = [str(g) for g in (run.get("user_groups") or [])] or ["unassigned"]
            cgroups = [str(g) for g in (run.get("user_client_groups") or [])] or ["default"]

            for grp in groups:
                gb = group_buckets.setdefault(
                    grp,
                    {
                        "group": grp,
                        "runs": 0,
                        "_users": set(),
                        "subcalls": 0,
                        "classic_runs": 0,
                        "codemode_runs": 0,
                        "tokens_saved": 0,
                        "estimated_cost_usd": 0.0,
                        "estimated_cost_saved_usd": 0.0,
                        "abac_rows_filtered": 0,
                        "pii_redactions": 0,
                    },
                )
                gb["runs"] += 1
                gb["_users"].add(user_sub)
                gb["subcalls"] += subcall_count
                gb["codemode_runs" if is_cm else "classic_runs"] += 1
                gb["tokens_saved"] += tok_saved
                gb["estimated_cost_usd"] += run_cost
                gb["estimated_cost_saved_usd"] += run_saved_usd
                gb["abac_rows_filtered"] += abac_n
                gb["pii_redactions"] += red_n

            for cg in cgroups:
                cb = client_group_buckets.setdefault(
                    cg,
                    {"client_group": cg, "runs": 0, "tokens_saved": 0, "abac_rows_filtered": 0},
                )
                cb["runs"] += 1
                cb["tokens_saved"] += tok_saved
                cb["abac_rows_filtered"] += abac_n

            ub = user_buckets.setdefault(
                user_sub,
                {
                    "user_sub": user_sub,
                    "user_email": run.get("user_email"),
                    "groups": groups,
                    "runs": 0,
                    "subcalls": 0,
                    "classic_runs": 0,
                    "codemode_runs": 0,
                    "_upstreams": {},
                    "tokens_saved": 0,
                    "estimated_cost_usd": 0.0,
                    "estimated_cost_saved_usd": 0.0,
                },
            )
            ub["runs"] += 1
            ub["subcalls"] += subcall_count
            ub["codemode_runs" if is_cm else "classic_runs"] += 1
            ub["tokens_saved"] += tok_saved
            ub["estimated_cost_usd"] += run_cost
            ub["estimated_cost_saved_usd"] += run_saved_usd
            for sc in subcalls:
                srv = str(sc.get("server") or "unknown")
                ub["_upstreams"][srv] = ub["_upstreams"].get(srv, 0) + 1

            reason = str(run.get("mode_decision_reason") or f"explicit:{mode}")
            db = decision_buckets.setdefault(
                (mode, reason),
                {
                    "mode": mode,
                    "reason": reason,
                    "runs": 0,
                    "_score_sum": 0.0,
                    "_score_n": 0,
                    "_saved_sum": 0,
                    "_dur_sum": 0.0,
                },
            )
            db["runs"] += 1
            if run.get("mode_decision_score") is not None:
                db["_score_sum"] += float(run["mode_decision_score"])
                db["_score_n"] += 1
            db["_saved_sum"] += tok_saved
            db["_dur_sum"] += dur_ms

            exp_id = run.get("experiment_id")
            if exp_id:
                variant = str(run.get("ab_variant") or ("codemode" if is_cm else "classic"))
                exp = exp_buckets.setdefault(str(exp_id), {})
                arm = exp.setdefault(
                    variant,
                    {
                        "runs": 0,
                        "errors": 0,
                        "_dur_sum": 0.0,
                        "total_tokens_saved": 0,
                    },
                )
                arm["runs"] += 1
                if not run.get("ok"):
                    arm["errors"] += 1
                arm["_dur_sum"] += dur_ms
                arm["total_tokens_saved"] += tok_saved

        by_company_group: list[dict[str, Any]] = []
        for item in group_buckets.values():
            users_set = item.pop("_users")
            runs_n = item["runs"]
            by_company_group.append(
                {
                    **item,
                    "unique_users": len(users_set),
                    "codemode_share_pct": (
                        round((item["codemode_runs"] / runs_n) * 100, 1) if runs_n else 0.0
                    ),
                    "estimated_cost_usd": round(item["estimated_cost_usd"], 6),
                    "estimated_cost_saved_usd": round(item["estimated_cost_saved_usd"], 6),
                }
            )
        by_company_group.sort(key=lambda x: x["runs"], reverse=True)

        by_user: list[dict[str, Any]] = []
        for item in user_buckets.values():
            upstreams_map = item.pop("_upstreams")
            top_srvs = sorted(upstreams_map.items(), key=lambda kv: kv[1], reverse=True)[:5]
            by_user.append(
                {
                    **item,
                    "top_upstreams": [{"server": s, "calls": c} for s, c in top_srvs],
                    "estimated_cost_usd": round(item["estimated_cost_usd"], 6),
                    "estimated_cost_saved_usd": round(item["estimated_cost_saved_usd"], 6),
                }
            )
        by_user.sort(key=lambda x: x["runs"], reverse=True)

        mode_decisions: list[dict[str, Any]] = []
        for item in decision_buckets.values():
            n = item["runs"]
            score_n = item.pop("_score_n")
            score_sum = item.pop("_score_sum")
            saved_sum = item.pop("_saved_sum")
            dur_sum = item.pop("_dur_sum")
            mode_decisions.append(
                {
                    **item,
                    "avg_score": round(score_sum / score_n, 1) if score_n else None,
                    "avg_tokens_saved": round(saved_sum / n, 1) if n else 0.0,
                    "avg_duration_ms": round(dur_sum / n, 2) if n else 0.0,
                }
            )
        mode_decisions.sort(key=lambda x: x["runs"], reverse=True)

        ab_experiments: dict[str, Any] = {}
        for exp_id, arms in exp_buckets.items():
            formatted_arms: dict[str, Any] = {}
            for variant, stats in arms.items():
                n = stats["runs"]
                formatted_arms[variant] = {
                    "runs": n,
                    "error_rate_pct": round((stats["errors"] / n) * 100, 1) if n else 0.0,
                    "avg_duration_ms": round(stats["_dur_sum"] / n, 2) if n else 0.0,
                    "avg_tokens_saved": round(stats["total_tokens_saved"] / n, 1) if n else 0.0,
                    "total_tokens_saved": stats["total_tokens_saved"],
                }
            cm_arm = formatted_arms.get("codemode")
            cl_arm = formatted_arms.get("classic")
            if cm_arm and cl_arm:
                winner = (
                    "codemode"
                    if cm_arm["total_tokens_saved"] >= cl_arm["total_tokens_saved"]
                    and cm_arm["error_rate_pct"] <= cl_arm["error_rate_pct"] + 5.0
                    else "classic"
                )
            else:
                winner = "codemode" if cm_arm else "classic"
            ab_experiments[exp_id] = {"arms": formatted_arms, "recommended_winner": winner}

        timeline_buckets: dict[str, dict[str, float]] = {}
        for run in runs:
            groups = [str(g) for g in (run.get("user_groups") or [])] or ["unassigned"]
            spans = run.get("trace_spans") or []
            stage_sums = {
                "gateway": 0.0,
                "monty_spawn": 0.0,
                "monty_exec": 0.0,
                "upstream": 0.0,
                "security": 0.0,
            }
            for sp in spans:
                st = str(sp.get("stage") or "")
                dur = float(sp.get("duration_ms") or 0.0)
                if st in {"gateway_auth", "mode_route"}:
                    stage_sums["gateway"] += dur
                elif st == "monty_spawn":
                    stage_sums["monty_spawn"] += dur
                elif st == "monty_exec":
                    stage_sums["monty_exec"] += dur
                elif st == "upstream_subcall":
                    stage_sums["upstream"] += dur
                elif st == "security_guard":
                    stage_sums["security"] += dur
            total_dur = float(run.get("duration_ms") or 0.0)
            for grp in groups:
                tb = timeline_buckets.setdefault(
                    grp,
                    {
                        "runs": 0.0,
                        "total_ms": 0.0,
                        "gateway": 0.0,
                        "monty_spawn": 0.0,
                        "monty_exec": 0.0,
                        "upstream": 0.0,
                        "security": 0.0,
                    },
                )
                tb["runs"] += 1.0
                tb["total_ms"] += total_dur
                for k, v in stage_sums.items():
                    tb[k] += v

        timeline_by_group = [
            {
                "group": grp,
                "runs": int(tb["runs"]),
                "avg_total_ms": round(tb["total_ms"] / tb["runs"], 2),
                "avg_gateway_ms": round(tb["gateway"] / tb["runs"], 2),
                "avg_monty_spawn_ms": round(tb["monty_spawn"] / tb["runs"], 2),
                "avg_monty_exec_ms": round(tb["monty_exec"] / tb["runs"], 2),
                "avg_upstream_ms": round(tb["upstream"] / tb["runs"], 2),
                "avg_security_ms": round(tb["security"] / tb["runs"], 2),
            }
            for grp, tb in sorted(
                timeline_buckets.items(), key=lambda kv: kv[1]["runs"], reverse=True
            )
        ]

        server_buckets: dict[str, dict[str, int]] = {}
        tool_buckets: dict[str, dict[str, int]] = {}
        overall_in = 0
        overall_out = 0
        overall_tot = 0
        for r in runs:
            r_in = int(r.get("input_tokens") or r.get("tokens_used_est") or 0)
            r_out = int(r.get("output_tokens") or r.get("returned_tokens") or 0)
            r_tot = int(r.get("total_tokens") or (r_in + r_out))
            r_sav = int(r.get("estimated_tokens_saved") or 0)
            overall_in += r_in
            overall_out += r_out
            overall_tot += r_tot
            srv = str(r.get("server_name") or "gateway")
            sb = server_buckets.setdefault(
                srv,
                {
                    "runs": 0,
                    "input_tokens": 0,
                    "output_tokens": 0,
                    "total_tokens": 0,
                    "tokens_saved": 0,
                },
            )
            sb["runs"] += 1
            sb["input_tokens"] += r_in
            sb["output_tokens"] += r_out
            sb["total_tokens"] += r_tot
            sb["tokens_saved"] += r_sav
            tl = str(r.get("tool_name") or "unknown")
            tb_item = tool_buckets.setdefault(
                tl,
                {
                    "runs": 0,
                    "input_tokens": 0,
                    "output_tokens": 0,
                    "total_tokens": 0,
                    "tokens_saved": 0,
                },
            )
            tb_item["runs"] += 1
            tb_item["input_tokens"] += r_in
            tb_item["output_tokens"] += r_out
            tb_item["total_tokens"] += r_tot
            tb_item["tokens_saved"] += r_sav

        by_server = [
            {"server": k, **v}
            for k, v in sorted(
                server_buckets.items(), key=lambda kv: kv[1]["total_tokens"], reverse=True
            )
        ]
        by_tool = [
            {"tool": k, **v}
            for k, v in sorted(
                tool_buckets.items(), key=lambda kv: kv[1]["total_tokens"], reverse=True
            )
        ]

        total_n = len(runs)
        return {
            "window_runs": total_n,
            "tenant_scope": tenant_id or "all_tenants",
            "pricing": {
                "model": model_name,
                "input_per_1m_usd": in_rate,
                "output_per_1m_usd": out_rate,
                "available_models": sorted(pricing_table),
            },
            "summary": {
                "total_runs": total_n,
                "classic_runs": total_classic,
                "codemode_runs": total_codemode,
                "codemode_share_pct": (
                    round((total_codemode / total_n) * 100, 1) if total_n else 0.0
                ),
                "total_tokens_saved": total_tokens_saved,
                "input_tokens": overall_in,
                "output_tokens": overall_out,
                "total_tokens": overall_tot,
                "estimated_cost_usd": round(total_cost_usd, 6),
                "estimated_cost_saved_usd": round(total_cost_saved_usd, 6),
            },
            "by_server": by_server,
            "by_tool": by_tool,
            "by_company_group": by_company_group,
            "by_client_group": sorted(
                client_group_buckets.values(), key=lambda x: x["runs"], reverse=True
            ),
            "by_user": by_user,
            "mode_decisions": mode_decisions,
            "ab_experiments": ab_experiments,
            "timeline_by_group": timeline_by_group,
        }
