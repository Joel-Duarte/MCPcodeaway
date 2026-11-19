"""Structured JSON event logging, request/trace context, and Prometheus metrics."""

from __future__ import annotations

import json
import logging
import sys
import threading
from contextvars import ContextVar
from datetime import UTC, datetime
from typing import Any

from opentelemetry import trace

_SENSITIVE_LOG_KEYS = {
    "authorization",
    "token",
    "access_token",
    "raw_token",
    "secret",
    "api_key",
    "client_secret",
    "worker_secret",
    "signing_secret",
}

_REQUEST_CONTEXT_VAR: ContextVar[dict[str, str] | None] = ContextVar(
    "gateway_request_context", default=None
)

_DEFAULT_HISTOGRAM_BUCKETS: tuple[float, ...] = (
    0.005,
    0.01,
    0.025,
    0.05,
    0.1,
    0.25,
    0.5,
    1.0,
    2.5,
    5.0,
    10.0,
)

_METRIC_METADATA: dict[str, tuple[str, str]] = {
    "gateway_mcp_requests_total": ("counter", "Total /mcp JSON-RPC requests processed."),
    "gateway_upstream_calls_total": ("counter", "Total downstream tool invocations dispatched."),
    "gateway_execution_duration_seconds": (
        "histogram",
        "Wall-clock duration of gateway /mcp tool executions in seconds.",
    ),
    "gateway_tokens_saved_total": (
        "counter",
        "Estimated prompt tokens saved via payload filtering and schema compaction.",
    ),
    "gateway_payload_bytes_in_total": (
        "counter",
        "Total raw payload bytes received from downstream servers.",
    ),
    "gateway_payload_bytes_out_total": (
        "counter",
        "Total filtered payload bytes returned to LLM callers.",
    ),
    "gateway_turns_avoided_total": (
        "counter",
        "Estimated extra LLM tool-call round-trips eliminated by Code Mode.",
    ),
    "gateway_obo_token_cache_total": (
        "counter",
        "Delegated downstream token exchange lookups by cache outcome.",
    ),
    "gateway_auth_denials_total": (
        "counter",
        "Total rejected authentication or tenant/RBAC authorization requests.",
    ),
    "gateway_policy_denials_total": (
        "counter",
        "Total downstream tool policy or scope denials enforced by HostDispatcher.",
    ),
    "gateway_abac_rows_filtered_total": (
        "counter",
        "Total response rows redacted/dropped by gateway host-side ABAC rules.",
    ),
    "gateway_redactions_total": (
        "counter",
        "Total PII, JWT, or secret tokens masked by OutputRedactor.",
    ),
    "gateway_grounding_rejections_total": (
        "counter",
        "Total Code Mode responses blocked by provenance grounding enforcement.",
    ),
}


def set_request_context(
    *,
    request_id: str,
    tenant_id: str | None = None,
    user_sub: str | None = None,
    profile: str | None = None,
) -> dict[str, str]:
    """Bind request correlation fields for downstream structured log records."""
    ctx: dict[str, str] = {"request_id": request_id}
    if tenant_id:
        ctx["tenant_id"] = tenant_id
    if user_sub:
        ctx["user_sub"] = user_sub
    if profile:
        ctx["profile"] = profile
    _REQUEST_CONTEXT_VAR.set(ctx)
    return ctx


def update_request_context(**fields: str | None) -> dict[str, str]:
    """Merge additional correlation attributes into the active request context."""
    ctx = dict(_REQUEST_CONTEXT_VAR.get() or {})
    for key, val in fields.items():
        if val:
            ctx[key] = val
    _REQUEST_CONTEXT_VAR.set(ctx)
    return ctx


def get_request_context() -> dict[str, str]:
    """Return a copy of the active request correlation context."""
    return dict(_REQUEST_CONTEXT_VAR.get() or {})


def clear_request_context() -> None:
    """Clear request correlation attributes at request completion."""
    _REQUEST_CONTEXT_VAR.set(None)


def _sanitize_log_fields(fields: dict[str, Any]) -> dict[str, Any]:
    sanitized: dict[str, Any] = {}
    for key, val in fields.items():
        if key.lower() in _SENSITIVE_LOG_KEYS:
            sanitized[key] = "[REDACTED]"
        else:
            sanitized[key] = val
    return sanitized


def _active_trace_ids() -> dict[str, str]:
    span = trace.get_current_span()
    ctx = span.get_span_context() if span is not None else None
    if ctx is not None and ctx.is_valid:
        return {
            "trace_id": format(ctx.trace_id, "032x"),
            "span_id": format(ctx.span_id, "016x"),
        }
    return {}


class JsonLogFormatter(logging.Formatter):
    """Single-line JSON log formatter enriched with request context and OTel trace IDs."""

    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "timestamp": datetime.now(UTC).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "event": getattr(record, "event", record.getMessage()),
            "message": record.getMessage(),
        }
        payload.update(get_request_context())
        payload.update(_active_trace_ids())
        extra_fields = getattr(record, "fields", None)
        if isinstance(extra_fields, dict) and extra_fields:
            payload.update(_sanitize_log_fields(extra_fields))
        return json.dumps(payload, default=str)


class TextLogFormatter(logging.Formatter):
    """Human-readable log formatter for local troubleshooting."""

    def format(self, record: logging.LogRecord) -> str:
        ctx = get_request_context()
        req = ctx.get("request_id", "-")
        event = getattr(record, "event", "-")
        fields = getattr(record, "fields", None) or {}
        suffix = f" {json.dumps(_sanitize_log_fields(fields), default=str)}" if fields else ""
        return (
            f"{datetime.now(UTC).isoformat()} {record.levelname} "
            f"[{req}] {event}: {record.getMessage()}{suffix}"
        )


def configure_logging(log_format: str = "json", log_level: str = "INFO") -> logging.Logger:
    """Configure the 'mcpcodeaway' logger idempotently."""
    logger = logging.getLogger("mcpcodeaway")
    level = getattr(logging, log_level.upper(), logging.INFO)
    logger.setLevel(level)
    formatter: logging.Formatter = (
        TextLogFormatter() if log_format == "text" else JsonLogFormatter()
    )

    tagged = [h for h in logger.handlers if getattr(h, "_mcpcodeaway_handler", False)]
    if tagged:
        for handler in tagged:
            handler.setFormatter(formatter)
    else:
        handler = logging.StreamHandler(sys.stdout)
        handler._mcpcodeaway_handler = True  # type: ignore[attr-defined]
        handler.setFormatter(formatter)
        logger.addHandler(handler)
    return logger


def log_event(
    event: str,
    *,
    level: int = logging.INFO,
    message: str = "",
    **fields: Any,
) -> None:
    """Emit a structured event on the 'mcpcodeaway' logger."""
    logger = logging.getLogger("mcpcodeaway")
    logger.log(
        level,
        message or event,
        extra={
            "event": event,
            "fields": _sanitize_log_fields(fields),
        },
    )


def _format_labels(labels: tuple[tuple[str, str], ...]) -> str:
    if not labels:
        return ""
    parts: list[str] = []
    for k, v in labels:
        escaped = str(v).replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n")
        parts.append(f'{k}="{escaped}"')
    return "{" + ",".join(parts) + "}"


class MetricsRegistry:
    """Thread-safe in-process Prometheus/OpenMetrics counter & histogram registry."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._counters: dict[str, dict[tuple[tuple[str, str], ...], float]] = {}
        self._histograms: dict[
            str, dict[tuple[tuple[str, str], ...], dict[str, Any]]
        ] = {}

    @staticmethod
    def _normalize_labels(labels: dict[str, Any] | None) -> tuple[tuple[str, str], ...]:
        if not labels:
            return ()
        return tuple(sorted((str(k), str(v)) for k, v in labels.items()))

    def inc_counter(
        self, name: str, amount: float = 1.0, labels: dict[str, Any] | None = None
    ) -> None:
        """Increment a named counter metric."""
        key = self._normalize_labels(labels)
        with self._lock:
            series = self._counters.setdefault(name, {})
            series[key] = series.get(key, 0.0) + float(amount)

    def observe_histogram(
        self,
        name: str,
        value: float,
        labels: dict[str, Any] | None = None,
        buckets: tuple[float, ...] = _DEFAULT_HISTOGRAM_BUCKETS,
    ) -> None:
        """Record an observation into a named Prometheus histogram."""
        key = self._normalize_labels(labels)
        val = float(value)
        with self._lock:
            series = self._histograms.setdefault(name, {})
            state = series.get(key)
            if state is None:
                state = {
                    "buckets": list(buckets),
                    "counts": [0 for _ in buckets],
                    "inf": 0,
                    "sum": 0.0,
                    "count": 0,
                }
                series[key] = state
            state["sum"] += val
            state["count"] += 1
            state["inf"] += 1
            for idx, upper in enumerate(state["buckets"]):
                if val <= upper:
                    state["counts"][idx] += 1

    def render_prometheus(self) -> str:
        """Render all recorded metrics in Prometheus text exposition format 0.0.4."""
        lines: list[str] = []
        with self._lock:
            for name in sorted(self._counters):
                metric_type, help_text = _METRIC_METADATA.get(name, ("counter", name))
                lines.append(f"# HELP {name} {help_text}")
                lines.append(f"# TYPE {name} {metric_type}")
                for lbl_tuple, val in sorted(self._counters[name].items()):
                    lines.append(f"{name}{_format_labels(lbl_tuple)} {val}")

            for name in sorted(self._histograms):
                metric_type, help_text = _METRIC_METADATA.get(name, ("histogram", name))
                lines.append(f"# HELP {name} {help_text}")
                lines.append(f"# TYPE {name} {metric_type}")
                for lbl_tuple, state in sorted(self._histograms[name].items()):
                    base_labels = dict(lbl_tuple)
                    for upper, bcount in zip(state["buckets"], state["counts"], strict=True):
                        bucket_lbls = self._normalize_labels({**base_labels, "le": upper})
                        lines.append(f"{name}_bucket{_format_labels(bucket_lbls)} {bcount}")
                    inf_lbls = self._normalize_labels({**base_labels, "le": "+Inf"})
                    lines.append(f"{name}_bucket{_format_labels(inf_lbls)} {state['inf']}")
                    lbl_str = _format_labels(lbl_tuple)
                    lines.append(f"{name}_sum{lbl_str} {round(state['sum'], 6)}")
                    lines.append(f"{name}_count{lbl_str} {state['count']}")

        return "\n".join(lines) + ("\n" if lines else "")
