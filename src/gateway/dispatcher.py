"""Trusted host dispatcher: RBAC, rate limits, schema validation, OBO creds, and redaction."""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import re
import time
from typing import Any

import jsonschema
from opentelemetry import trace
from pydantic import BaseModel

from gateway.auth import CallerIdentity, DelegatedTokenExchanger, PolicyEvaluator
from gateway.catalog import ToolCatalog, render_python_stub
from gateway.config import GatewaySettings, UpstreamConfig
from gateway.state import CacheBackend
from gateway.upstream_clients import DiscoveredTool, UpstreamClientManager

_TRACER = trace.get_tracer("mcpcodeaway.dispatcher")

_JWT_RE = re.compile(r"\beyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\b")
_API_KEY_RE = re.compile(r"\b(?:sk-[A-Za-z0-9_-]{12,}|ghp_[A-Za-z0-9]{16,}|AKIA[0-9A-Z]{16})\b")
_SSN_RE = re.compile(r"\b\d{3}-\d{2}-\d{4}\b")
_CARD_CANDIDATE_RE = re.compile(r"\b(?:\d[ -]*?){13,19}\b")


def _luhn_valid(digits: str) -> bool:
    total = 0
    reverse = digits[::-1]
    for idx, ch in enumerate(reverse):
        num = int(ch)
        if idx % 2 == 1:
            num *= 2
            if num > 9:
                num -= 9
        total += num
    return total % 10 == 0


def _mask_cards(text: str) -> str:
    def _replace(match: re.Match[str]) -> str:
        digits = re.sub(r"\D", "", match.group(0))
        if 13 <= len(digits) <= 19 and _luhn_valid(digits):
            return "[REDACTED_CARD]"
        return match.group(0)

    return _CARD_CANDIDATE_RE.sub(_replace, text)


class OutputRedactor:
    """Redacts leaked bearer JWTs, API keys, SSNs, and Luhn-valid card numbers."""

    @classmethod
    def redact_text(cls, text: str) -> str:
        masked = _JWT_RE.sub("[REDACTED_JWT]", text)
        masked = _API_KEY_RE.sub("[REDACTED_SECRET]", masked)
        masked = _SSN_RE.sub("[REDACTED_SSN]", masked)
        return _mask_cards(masked)

    @classmethod
    def redact(cls, value: Any) -> Any:
        if isinstance(value, str):
            return cls.redact_text(value)
        if isinstance(value, list):
            return [cls.redact(item) for item in value]
        if isinstance(value, tuple):
            return [cls.redact(item) for item in value]
        if isinstance(value, dict):
            return {k: cls.redact(v) for k, v in value.items()}
        return value


class SubcallRecord(BaseModel):
    """Telemetry record for a single downstream tool invocation."""

    tool: str
    server: str
    duration_ms: float
    raw_bytes: int
    status: str
    auth_type: str
    token_cache_hit: bool = False
    downstream_audience: str | None = None
    output_sha256: str | None = None
    error: str | None = None


_ENTITY_ID_RE = re.compile(r"\b[A-Z]{2,6}-[A-Z0-9-]{2,}\b")
_EMAIL_RE = re.compile(r"\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}\b")


def extract_entity_identifiers(value: Any) -> set[str]:
    """Extract business entity IDs (e.g. CUST-1003, INV-EU-2003) and emails from JSON values."""
    found: set[str] = set()
    if isinstance(value, str):
        found.update(_ENTITY_ID_RE.findall(value))
        found.update(m.lower() for m in _EMAIL_RE.findall(value))
    elif isinstance(value, list | tuple):
        for item in value:
            found.update(extract_entity_identifiers(item))
    elif isinstance(value, dict):
        for v in value.values():
            found.update(extract_entity_identifiers(v))
    return found


def summarize_value_shape(value: Any, *, depth: int = 0) -> Any:
    """Return a compact schema-only sketch (keys + scalar type names, zero raw data)."""
    if value is None:
        return "None"
    if isinstance(value, bool):
        return "bool"
    if isinstance(value, int | float):
        return type(value).__name__
    if isinstance(value, str):
        return "str"
    if depth >= 3:
        if isinstance(value, dict):
            return "dict"
        if isinstance(value, list | tuple):
            return "list"
        return type(value).__name__
    if isinstance(value, dict):
        return {
            str(k): summarize_value_shape(v, depth=depth + 1)
            for k, v in list(value.items())[:15]
        }
    if isinstance(value, list | tuple):
        return [summarize_value_shape(value[0], depth=depth + 1)] if value else []
    return type(value).__name__


def build_provenance_envelope(
    *,
    allowed_servers: set[str] | None,
    subcalls: list[SubcallRecord],
    observed_tool_outputs: list[Any],
    returned_value: Any,
    signing_secret: str,
) -> dict[str, Any]:
    """Build a tamper-evident host-stamped provenance envelope outside Monty."""
    ok_calls = [sc for sc in subcalls if sc.status == "ok"]
    tools_called = list(dict.fromkeys(sc.tool for sc in ok_calls))
    servers_called = sorted({sc.server for sc in ok_calls})
    digests = [sc.output_sha256 for sc in ok_calls if sc.output_sha256]

    observed_entities = extract_entity_identifiers(observed_tool_outputs)
    returned_entities = extract_entity_identifiers(returned_value)
    unverified_identifiers = sorted(returned_entities - observed_entities)

    grounded = (len(ok_calls) > 0 or returned_value is None) and not unverified_identifiers
    body = {
        "enforced_by": "host_dispatcher",
        "allowed_servers_scope": sorted(allowed_servers) if allowed_servers is not None else None,
        "tools_called": tools_called,
        "servers_called": servers_called,
        "subcall_count": len(ok_calls),
        "tool_output_digests": digests,
        "grounded": grounded,
        "unverified_identifiers": unverified_identifiers,
    }
    canonical = json.dumps(body, sort_keys=True, separators=(",", ":")).encode("utf-8")
    sig = hmac.new(signing_secret.encode("utf-8"), canonical, hashlib.sha256).hexdigest()
    body["provenance_hmac"] = sig
    return body


class ExecutionBudget:
    """Per-execution downstream call counter guarding against runaway loops."""

    def __init__(self, max_calls: int) -> None:
        self.max_calls = max_calls
        self.calls_made = 0

    def consume(self) -> None:
        if self.calls_made >= self.max_calls:
            raise RuntimeError(
                f"Execution call budget exceeded ({self.max_calls} downstream calls max)"
            )
        self.calls_made += 1


def bind_positional_arguments(
    tool: DiscoveredTool, args: tuple[Any, ...], kwargs: dict[str, Any]
) -> dict[str, Any]:
    """Map positional + keyword arguments from Monty into a JSON-Schema dict."""
    if not args:
        return dict(kwargs)
    props = (tool.input_schema or {}).get("properties") or {}
    required = set((tool.input_schema or {}).get("required") or [])
    ordered_names = [p for p in props if p in required] + [p for p in props if p not in required]
    if len(args) > len(ordered_names):
        raise ValueError(
            f"Too many positional arguments for {tool.qualified_name}: "
            f"expected at most {len(ordered_names)}, got {len(args)}.\n"
            f"Expected signature:\n{render_python_stub(tool)}"
        )
    bound = dict(kwargs)
    for name, val in zip(ordered_names, args, strict=False):
        if name in bound:
            raise ValueError(
                f"Multiple values for argument '{name}' in {tool.qualified_name}.\n"
                f"Expected signature:\n{render_python_stub(tool)}"
            )
        bound[name] = val
    return bound


def _resolve_identity_claim_values(identity: CallerIdentity, claim: str) -> set[str]:
    """Extract caller claim values (attribute first, then raw JWT claims dict)."""
    val = getattr(identity, claim, None)
    if val is None or val == [] or val == "":
        val = identity.claims.get(claim)
    if val is None:
        return set()
    if isinstance(val, str):
        return {part for part in val.split() if part}
    if isinstance(val, list | tuple | set):
        return {str(item) for item in val if str(item)}
    return {str(val)}


def _apply_pre_call_abac(
    identity: CallerIdentity,
    tool: DiscoveredTool,
    upstream: UpstreamConfig,
    arguments: dict[str, Any],
) -> dict[str, Any]:
    updated = dict(arguments)
    props = (tool.input_schema or {}).get("properties") or {}
    for rule in upstream.abac_rules:
        if not rule.enforce_on_args or not rule.arg_name:
            continue
        allowed_vals = _resolve_identity_claim_values(identity, rule.claim)
        if not allowed_vals:
            continue
        if rule.arg_name in updated and updated[rule.arg_name] is not None:
            requested = str(updated[rule.arg_name])
            if requested not in allowed_vals:
                raise PermissionError(
                    f"ABAC policy denied {rule.arg_name}='{requested}' "
                    f"for {rule.claim}={sorted(allowed_vals)}"
                )
        elif len(allowed_vals) == 1 and rule.arg_name in props:
            updated[rule.arg_name] = next(iter(allowed_vals))
    return updated


def _apply_post_call_abac(
    identity: CallerIdentity,
    upstream: UpstreamConfig,
    result: Any,
) -> Any:
    current = result
    for rule in upstream.abac_rules:
        if not rule.filter_response_rows or not rule.row_field:
            continue
        allowed_vals = _resolve_identity_claim_values(identity, rule.claim)
        if not allowed_vals:
            continue
        field = rule.row_field
        if (
            rule.response_list_key
            and isinstance(current, dict)
            and isinstance(current.get(rule.response_list_key), list)
        ):
            filtered = [
                row
                for row in current[rule.response_list_key]
                if not isinstance(row, dict) or str(row.get(field)) in allowed_vals
            ]
            current = {**current, rule.response_list_key: filtered}
            if "count" in current:
                current["count"] = len(filtered)
        elif isinstance(current, list):
            current = [
                row
                for row in current
                if not isinstance(row, dict) or str(row.get(field)) in allowed_vals
            ]
        elif isinstance(current, dict) and field in current:
            if str(current[field]) not in allowed_vals:
                raise PermissionError(
                    f"ABAC row policy denied access to record with "
                    f"{field}='{current[field]}'"
                )
    return current


class HostDispatcher:
    """Enforces policy, validates schemas, resolves credentials, and calls upstreams."""

    def __init__(
        self,
        *,
        settings: GatewaySettings,
        policy: PolicyEvaluator,
        exchanger: DelegatedTokenExchanger,
        cache: CacheBackend,
        client_manager: UpstreamClientManager,
        catalog: ToolCatalog,
    ) -> None:
        self.settings = settings
        self.policy = policy
        self.exchanger = exchanger
        self.cache = cache
        self.client_manager = client_manager
        self.catalog = catalog

    async def resolve_upstream_headers(
        self, identity: CallerIdentity, upstream: UpstreamConfig
    ) -> tuple[dict[str, str], bool, str | None]:
        """Resolve downstream auth headers on the host side only."""
        if upstream.auth_type == "none":
            return {}, False, None
        if upstream.auth_type == "delegated_token":
            audience = upstream.downstream_audience or f"api://{upstream.name}"
            scope = upstream.downstream_scope or "mcp.tools"
            exchanged = await self.exchanger.exchange(identity, audience=audience, scope=scope)
            return (
                {"Authorization": f"Bearer {exchanged.access_token}"},
                exchanged.cache_hit,
                audience,
            )
        secret_env = upstream.secret_env_var or ""
        secret_val = os.getenv(secret_env, "") if secret_env else ""
        if not secret_val:
            raise RuntimeError(
                f"Upstream '{upstream.name}' requires secret env var '{secret_env}'"
            )
        if upstream.auth_type == "bearer_secret":
            return {"Authorization": f"Bearer {secret_val}"}, False, None
        if upstream.auth_type == "api_key":
            return {upstream.api_key_header: secret_val}, False, None
        raise ValueError(f"Unsupported upstream auth_type '{upstream.auth_type}'")

    async def invoke_tool(
        self,
        identity: CallerIdentity,
        tool: DiscoveredTool,
        upstream: UpstreamConfig,
        arguments: dict[str, Any],
        *,
        budget: ExecutionBudget | None = None,
        code_mode_hints: bool = False,
    ) -> tuple[Any, SubcallRecord]:
        started = time.perf_counter()
        with _TRACER.start_as_current_span("gateway.downstream.call") as span:
            span.set_attribute("mcp.tool", tool.qualified_name)
            span.set_attribute("mcp.server", upstream.name)
            span.set_attribute("auth.type", upstream.auth_type)
            span.set_attribute("tenant.id", identity.tenant_id)
            span.set_attribute("catalog.version", self.catalog.catalog_version)

            allowed, reason = self.policy.can_execute_tool(
                identity, upstream, tool.name, tool.mutating
            )
            if not allowed:
                raise PermissionError(reason)

            rl_key = f"rl:{identity.tenant_id}:{identity.sub}"
            count = await self.cache.increment_window(rl_key, window_seconds=60)
            if count > self.settings.rate_limit_per_minute:
                raise RuntimeError(
                    f"Rate limit exceeded ({self.settings.rate_limit_per_minute}/min)"
                )

            if budget is not None:
                budget.consume()

            scoped_args = _apply_pre_call_abac(identity, tool, upstream, arguments)

            try:
                jsonschema.validate(instance=scoped_args, schema=tool.input_schema)
            except jsonschema.ValidationError as exc:
                msg = f"Invalid arguments for {tool.qualified_name}: {exc.message}"
                if code_mode_hints:
                    msg = f"{msg}\nExpected signature:\n{render_python_stub(tool)}"
                raise ValueError(msg) from exc

            cache_hit = False
            audience: str | None = None
            try:
                auth_headers, cache_hit, audience = await self.resolve_upstream_headers(
                    identity, upstream
                )
                span.set_attribute("auth.token_cache_hit", cache_hit)
                raw_result = await self.client_manager.call_tool(
                    upstream, tool, scoped_args, auth_headers=auth_headers
                )
            except Exception as exc:
                duration_ms = round((time.perf_counter() - started) * 1000, 3)
                record = SubcallRecord(
                    tool=tool.qualified_name,
                    server=upstream.name,
                    duration_ms=duration_ms,
                    raw_bytes=0,
                    status="error",
                    auth_type=upstream.auth_type,
                    token_cache_hit=cache_hit,
                    downstream_audience=audience,
                    error=str(exc),
                )
                span.record_exception(exc)
                return {"_dispatch_error": str(exc)}, record

            raw_encoded = json.dumps(raw_result, default=str).encode("utf-8")
            raw_bytes = len(raw_encoded)
            output_sha256 = hashlib.sha256(raw_encoded).hexdigest()[:16]
            redacted = _apply_post_call_abac(identity, upstream, OutputRedactor.redact(raw_result))
            duration_ms = round((time.perf_counter() - started) * 1000, 3)
            record = SubcallRecord(
                tool=tool.qualified_name,
                server=upstream.name,
                duration_ms=duration_ms,
                raw_bytes=raw_bytes,
                status="ok",
                auth_type=upstream.auth_type,
                token_cache_hit=cache_hit,
                downstream_audience=audience,
                output_sha256=output_sha256,
            )
            return redacted, record
