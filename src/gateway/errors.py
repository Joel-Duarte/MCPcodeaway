"""Stable upstream failure categories used in agent responses and operations telemetry."""

from __future__ import annotations

from dataclasses import dataclass

import httpx


class UpstreamCircuitOpen(RuntimeError):
    """Raised when an administrator-configured probe has marked an upstream unhealthy."""


class UpstreamCapacityExceeded(RuntimeError):
    """The upstream's configured request or in-flight call budget is exhausted."""


@dataclass(frozen=True)
class UpstreamFailure:
    error_type: str
    message: str
    retryable: bool = False
    outcome_unknown: bool = False
    status_code: int | None = None


def classify_upstream_error(exc: Exception, *, operation_is_read: bool = False) -> UpstreamFailure:
    """Map transport failures to a stable, non-sensitive contract for agents/admins."""
    if isinstance(exc, UpstreamCircuitOpen):
        return UpstreamFailure(
            "upstream_unhealthy",
            "The upstream health check is failing; calls are paused until the service recovers.",
        )
    if isinstance(exc, UpstreamCapacityExceeded):
        return UpstreamFailure("upstream_capacity_exceeded", str(exc), retryable=True)
    if isinstance(exc, (httpx.TimeoutException, TimeoutError)):
        return UpstreamFailure(
            "upstream_timeout",
            "The upstream did not respond before the gateway timeout.",
            retryable=operation_is_read,
            outcome_unknown=not operation_is_read,
        )
    if isinstance(exc, httpx.ConnectError | httpx.NetworkError):
        return UpstreamFailure(
            "upstream_unreachable",
            "The gateway could not connect to the upstream service.",
            retryable=operation_is_read,
        )
    if isinstance(exc, httpx.HTTPStatusError):
        status = exc.response.status_code
        if status == 429:
            category, message = "upstream_rate_limited", "The upstream is rate limiting requests."
        elif status in {401, 403}:
            category, message = (
                "upstream_auth",
                f"The upstream rejected the configured credentials (HTTP {status}).",
            )
        elif status in {502, 503, 504}:
            category, message = "upstream_unavailable", "The upstream is temporarily unavailable."
        else:
            category, message = "upstream_http_error", f"The upstream returned HTTP {status}."
        return UpstreamFailure(
            category,
            message,
            retryable=operation_is_read and status in {429, 502, 503, 504},
            outcome_unknown=not operation_is_read and status >= 500,
            status_code=status,
        )
    if isinstance(exc, httpx.DecodingError):
        return UpstreamFailure(
            "upstream_invalid_response", "The upstream returned an invalid response."
        )
    message = str(exc)
    if message.startswith("Downstream MCP tool error:"):
        detail = message.partition(":")[2].strip()[:1000]
        return UpstreamFailure(
            "upstream_tool_error", f"The upstream tool returned an error: {detail}"
        )
    if message.startswith("Upstream tool error"):
        detail = message.partition(":")[2].strip()[:1000]
        return UpstreamFailure(
            "upstream_tool_error", f"The upstream tool returned an error: {detail}"
        )
    return UpstreamFailure("upstream_error", "The upstream call failed.")
