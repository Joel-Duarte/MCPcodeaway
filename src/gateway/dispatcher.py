"""Trusted host dispatcher: RBAC, rate limits, schema validation, OBO creds, and redaction."""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import logging
import random
import re
import time
import uuid
from datetime import UTC, datetime
from typing import Any
from urllib.parse import quote

import httpx
import jsonschema
from opentelemetry import trace
from pydantic import BaseModel

from gateway.auth import (
    CallerIdentity,
    ClientCredentialsProvider,
    DelegatedTokenExchanger,
    PolicyEvaluator,
    UserCredentialRequiredError,
)
from gateway.catalog import ToolCatalog, render_python_stub
from gateway.config import GatewaySettings, ToolApprovalPolicy, UpstreamConfig, UpstreamRegistry
from gateway.crypto import sign_connect_ticket
from gateway.errors import (
    UpstreamCapacityExceeded,
    UpstreamCircuitOpen,
    UpstreamFailure,
    classify_upstream_error,
)
from gateway.observability import MetricsRegistry, get_request_context, log_event
from gateway.secrets import CredentialVault, SecretResolver
from gateway.state import CacheBackend
from gateway.upstream_clients import DiscoveredTool, UpstreamClientManager, validate_upstream_url

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


def _mask_cards_with_count(text: str) -> tuple[str, int]:
    count = 0

    def _replace(match: re.Match[str]) -> str:
        nonlocal count
        digits = re.sub(r"\D", "", match.group(0))
        if 13 <= len(digits) <= 19 and _luhn_valid(digits):
            count += 1
            return "[REDACTED_CARD]"
        return match.group(0)

    return _CARD_CANDIDATE_RE.sub(_replace, text), count


class OutputRedactor:
    """Redacts leaked bearer JWTs, API keys, SSNs, and Luhn-valid card numbers."""

    @classmethod
    def redact_text_with_count(cls, text: str) -> tuple[str, int]:
        total = 0
        masked, n_jwt = _JWT_RE.subn("[REDACTED_JWT]", text)
        total += n_jwt
        masked, n_key = _API_KEY_RE.subn("[REDACTED_SECRET]", masked)
        total += n_key
        masked, n_ssn = _SSN_RE.subn("[REDACTED_SSN]", masked)
        total += n_ssn
        masked, n_card = _mask_cards_with_count(masked)
        return masked, total + n_card

    @classmethod
    def redact_text(cls, text: str) -> str:
        return cls.redact_text_with_count(text)[0]

    @classmethod
    def redact_with_count(cls, value: Any) -> tuple[Any, int]:
        if isinstance(value, str):
            return cls.redact_text_with_count(value)
        if isinstance(value, list):
            items: list[Any] = []
            total = 0
            for item in value:
                redacted_item, n = cls.redact_with_count(item)
                items.append(redacted_item)
                total += n
            return items, total
        if isinstance(value, tuple):
            items_t: list[Any] = []
            total = 0
            for item in value:
                redacted_item, n = cls.redact_with_count(item)
                items_t.append(redacted_item)
                total += n
            return items_t, total
        if isinstance(value, dict):
            out: dict[Any, Any] = {}
            total = 0
            for k, v in value.items():
                redacted_val, n = cls.redact_with_count(v)
                out[k] = redacted_val
                total += n
            return out, total
        return value, 0

    @classmethod
    def redact(cls, value: Any) -> Any:
        return cls.redact_with_count(value)[0]


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
    abac_rows_filtered: int = 0
    redactions_count: int = 0
    error: str | None = None
    error_type: str | None = None
    retryable: bool = False
    retry_attempts: int = 0
    outcome_unknown: bool = False
    upstream_status_code: int | None = None
    schema_covered: bool | None = None
    is_mutating: bool | None = None
    invalid_arguments: int | None = None


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
        for k, v in value.items():
            found.update(extract_entity_identifiers(k))
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
            str(k): summarize_value_shape(v, depth=depth + 1) for k, v in list(value.items())[:15]
        }
    if isinstance(value, list | tuple):
        return [summarize_value_shape(value[0], depth=depth + 1)] if value else []
    return type(value).__name__


def _parse_signing_secrets(signing_secrets: str | list[str]) -> list[str]:
    if isinstance(signing_secrets, str):
        keys = [s.strip() for s in signing_secrets.split(",") if s.strip()]
    else:
        keys = [str(s).strip() for s in signing_secrets if str(s).strip()]
    return keys or ["dev-provenance-secret"]


def build_provenance_envelope(
    *,
    allowed_servers: set[str] | None,
    subcalls: list[SubcallRecord],
    observed_tool_outputs: list[Any],
    returned_value: Any,
    signing_secret: str | list[str],
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
    primary_key = _parse_signing_secrets(signing_secret)[0]
    canonical = json.dumps(body, sort_keys=True, separators=(",", ":")).encode("utf-8")
    sig = hmac.new(primary_key.encode("utf-8"), canonical, hashlib.sha256).hexdigest()
    body["provenance_hmac"] = sig
    return body


def verify_provenance_envelope(envelope: dict[str, Any], signing_secrets: str | list[str]) -> bool:
    """Verify a provenance envelope HMAC against one or more rotated keys."""
    presented = str(envelope.get("provenance_hmac") or "")
    if not presented:
        return False
    body = {k: v for k, v in envelope.items() if k != "provenance_hmac"}
    canonical = json.dumps(body, sort_keys=True, separators=(",", ":")).encode("utf-8")
    for key in _parse_signing_secrets(signing_secrets):
        expected = hmac.new(key.encode("utf-8"), canonical, hashlib.sha256).hexdigest()
        if hmac.compare_digest(expected, presented):
            return True
    return False


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
        if rule.claim == "groups" and not identity.group_membership_complete:
            raise PermissionError(
                "ABAC policy denied: caller group membership could not be resolved"
            )
        allowed_vals = _resolve_identity_claim_values(identity, rule.claim)
        if "ALL" in allowed_vals or "*" in allowed_vals:
            continue
        if not allowed_vals:
            if rule.require_claim:
                raise PermissionError(
                    f"ABAC policy denied: caller token missing required claim '{rule.claim}'"
                )
            continue
        if rule.arg_name in updated and updated[rule.arg_name] is not None:
            requested = str(updated[rule.arg_name])
            if requested not in allowed_vals:
                raise PermissionError(
                    f"ABAC policy denied {rule.arg_name}='{requested}' "
                    f"for {rule.claim}={sorted(allowed_vals)}"
                )
        elif rule.arg_name in props:
            if len(allowed_vals) == 1:
                updated[rule.arg_name] = next(iter(allowed_vals))
            else:
                raise PermissionError(
                    f"ABAC policy denied: specify '{rule.arg_name}' from the caller's "
                    f"allowed {rule.claim} values {sorted(allowed_vals)}"
                )
    return updated


def _apply_post_call_abac_with_count(
    identity: CallerIdentity,
    upstream: UpstreamConfig,
    result: Any,
) -> tuple[Any, int]:
    current = result
    dropped = 0
    for rule in upstream.abac_rules:
        if not rule.filter_response_rows or not rule.row_field:
            continue
        if rule.claim == "groups" and not identity.group_membership_complete:
            raise PermissionError(
                "ABAC policy denied: caller group membership could not be resolved"
            )
        allowed_vals = _resolve_identity_claim_values(identity, rule.claim)
        if "ALL" in allowed_vals or "*" in allowed_vals:
            continue
        if not allowed_vals:
            if rule.require_claim:
                raise PermissionError(
                    f"ABAC policy denied: caller token missing required claim '{rule.claim}'"
                )
            continue
        field = rule.row_field
        if rule.response_list_key:
            if not (
                isinstance(current, dict) and isinstance(current.get(rule.response_list_key), list)
            ):
                raise PermissionError(
                    "ABAC response policy denied: expected list field "
                    f"'{rule.response_list_key}' was missing or had an invalid shape"
                )
            orig = current[rule.response_list_key]
            filtered = [
                row for row in orig if isinstance(row, dict) and str(row.get(field)) in allowed_vals
            ]
            dropped += len(orig) - len(filtered)
            current = {**current, rule.response_list_key: filtered}
            if "count" in current:
                current["count"] = len(filtered)
        elif isinstance(current, list):
            orig_list = current
            current = [
                row
                for row in orig_list
                if isinstance(row, dict) and str(row.get(field)) in allowed_vals
            ]
            dropped += len(orig_list) - len(current)
        elif isinstance(current, dict) and field in current:
            if str(current[field]) not in allowed_vals:
                raise PermissionError(
                    f"ABAC row policy denied access to record with {field}='{current[field]}'"
                )
    return current, dropped


def _apply_post_call_abac(
    identity: CallerIdentity,
    upstream: UpstreamConfig,
    result: Any,
) -> Any:
    return _apply_post_call_abac_with_count(identity, upstream, result)[0]


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
        registry: UpstreamRegistry,
        metrics: MetricsRegistry | None = None,
        secret_resolver: SecretResolver | None = None,
        credential_vault: CredentialVault | None = None,
        client_credentials: ClientCredentialsProvider | None = None,
        connect_signing_key: bytes | None = None,
        http_client: httpx.AsyncClient | None = None,
        audit_store: Any | None = None,
    ) -> None:
        self.settings = settings
        self.policy = policy
        self.exchanger = exchanger
        self.cache = cache
        self.client_manager = client_manager
        self.health_monitor: Any = None
        self.catalog = catalog
        self.registry = registry
        self.metrics = metrics
        self.secret_resolver = secret_resolver or SecretResolver(settings=settings)
        self.credential_vault = credential_vault
        self.client_credentials = client_credentials or ClientCredentialsProvider(
            settings=settings, cache=cache, http_client=http_client
        )
        self._connect_signing_key = connect_signing_key
        self._http_client = http_client
        self.audit_store = audit_store
        self._refresh_locks: dict[str, asyncio.Lock] = {}

    async def _prepare_sensitive_write_audit(
        self,
        identity: CallerIdentity,
        tool: DiscoveredTool,
        upstream: UpstreamConfig,
        arguments: dict[str, Any],
    ) -> str | None:
        effect = upstream.tool_effects.get(tool.name, "write" if tool.mutating else "read")
        if effect == "read" and not tool.mutating:
            return None
        if self.audit_store is None or not hasattr(
            self.audit_store, "prepare_sensitive_write_audit"
        ):
            raise RuntimeError("Durable sensitive-write audit storage is unavailable")
        correlation_id = get_request_context().get("request_id") or uuid.uuid4().hex
        encoded_args = json.dumps(arguments, sort_keys=True, separators=(",", ":"))
        digest = hashlib.sha256(encoded_args.encode("utf-8")).hexdigest()
        dedupe_key = hashlib.sha256(
            "\0".join(
                (
                    identity.tenant_id,
                    identity.sub,
                    correlation_id,
                    upstream.name,
                    tool.name,
                    digest,
                )
            ).encode("utf-8")
        ).hexdigest()
        now = datetime.now(UTC).isoformat()
        row, _ = await self.audit_store.prepare_sensitive_write_audit(
            {
                "operation_id": uuid.uuid4().hex,
                "tenant_id": identity.tenant_id,
                "caller_sub": identity.sub,
                "upstream_name": upstream.name,
                "tool_name": tool.name,
                "request_digest": digest,
                "correlation_id": correlation_id,
                "dedupe_key": dedupe_key,
                "created_at": now,
            }
        )
        if row.get("request_digest") != digest:
            raise PermissionError(
                "Sensitive write request correlation was reused with new arguments"
            )
        operation_id = str(row["operation_id"])
        if not await self.audit_store.claim_sensitive_write_audit(
            operation_id, datetime.now(UTC).isoformat()
        ):
            log_event(
                "audit.sensitive_write_retry_blocked",
                level=logging.WARNING,
                operation_id=operation_id,
                status=str(row.get("status") or "already_claimed"),
            )
            raise PermissionError(
                f"Duplicate sensitive write blocked; audit operation {operation_id} "
                f"is {row.get('status', 'already claimed')}"
            )
        return operation_id

    async def _finish_sensitive_write_audit(self, operation_id: str, status: str) -> bool:
        if self.audit_store is None:
            return False
        return await self.audit_store.finish_sensitive_write_audit(
            operation_id, status, datetime.now(UTC).isoformat()
        )

    def _build_connect_url(self, identity: CallerIdentity, upstream: UpstreamConfig) -> str:
        base = self.settings.gateway_public_url.rstrip("/")
        path = f"{base}/auth/connect/{quote(upstream.name, safe='')}"
        if self._connect_signing_key is None:
            return path
        ticket = sign_connect_ticket(
            signing_key=self._connect_signing_key,
            sub=identity.sub,
            tenant_id=identity.tenant_id,
            upstream_name=upstream.name,
            ttl_seconds=self.settings.connect_ticket_ttl_seconds,
        )
        return f"{path}?ticket={quote(ticket, safe='')}"

    async def _resolve_oauth_client_secret(self, upstream: UpstreamConfig) -> str | None:
        if upstream.oauth and upstream.oauth.client_secret_ref:
            return await self.secret_resolver.resolve(
                upstream.oauth.client_secret_ref,
                allow_bare_env=True,
                allow_gateway_vault=False,
            )
        return None

    async def _refresh_user_oauth(
        self,
        identity: CallerIdentity,
        upstream: UpstreamConfig,
        payload: dict[str, Any],
    ) -> dict[str, str]:
        oauth = upstream.oauth
        refresh_token = payload.get("refresh_token")
        if not oauth or not oauth.token_endpoint or not oauth.client_id or not refresh_token:
            raise UserCredentialRequiredError(
                upstream_name=upstream.name,
                auth_type=upstream.auth_type,
                connect_url=self._build_connect_url(identity, upstream),
                reason="expired",
            )
        if self.settings.ssrf_protection_enabled:
            validate_upstream_url(
                oauth.token_endpoint,
                app_env=self.settings.app_env,
                block_private_ips_in_prod=self.settings.block_private_ips_in_prod,
                allowed_hosts=self.settings.allowed_upstream_hosts,
            )
        form_data: dict[str, str] = {
            "grant_type": "refresh_token",
            "refresh_token": str(refresh_token),
            "client_id": oauth.client_id,
        }
        client_secret = await self._resolve_oauth_client_secret(upstream)
        if client_secret:
            form_data["client_secret"] = client_secret

        if self._http_client is not None:
            resp = await self._http_client.post(
                oauth.token_endpoint, data=form_data, follow_redirects=False
            )
        else:
            async with httpx.AsyncClient(timeout=10.0) as client:
                resp = await client.post(
                    oauth.token_endpoint, data=form_data, follow_redirects=False
                )
        if resp.status_code >= 400:
            raise UserCredentialRequiredError(
                upstream_name=upstream.name,
                auth_type=upstream.auth_type,
                connect_url=self._build_connect_url(identity, upstream),
                reason="refresh_failed",
            )
        body = resp.json()
        new_access = body.get("access_token")
        if not new_access:
            raise UserCredentialRequiredError(
                upstream_name=upstream.name,
                auth_type=upstream.auth_type,
                connect_url=self._build_connect_url(identity, upstream),
                reason="refresh_failed",
            )
        expires_in = int(body.get("expires_in", 3600))
        expires_at = int(time.time()) + expires_in
        new_payload = {
            "access_token": str(new_access),
            "refresh_token": str(body.get("refresh_token") or refresh_token),
            "token_type": str(body.get("token_type") or "Bearer"),
            "expires_at": expires_at,
        }
        if self.credential_vault is not None:
            await self.credential_vault.save_user_credential(
                tenant_id=identity.tenant_id,
                user_sub=identity.sub,
                upstream_name=upstream.name,
                auth_kind="user_oauth",
                payload=new_payload,
                expires_at=expires_at,
            )
        return new_payload

    async def resolve_upstream_headers(
        self, identity: CallerIdentity, upstream: UpstreamConfig
    ) -> tuple[dict[str, str], bool, str | None]:
        """Resolve downstream auth headers on the host side only."""
        if upstream.auth_type == "none":
            return {}, False, None
        if upstream.auth_type == "delegated_token":
            audience = upstream.downstream_audience or f"api://{upstream.name}"
            scope = upstream.downstream_scope or "mcp.tools"
            oauth = upstream.oauth
            client_secret = await self._resolve_oauth_client_secret(upstream)
            exchanged = await self.exchanger.exchange(
                identity,
                audience=audience,
                scope=scope,
                dialect_override=oauth.token_exchange_dialect if oauth else None,
                token_endpoint_override=oauth.token_endpoint if oauth else None,
                client_id_override=oauth.client_id if oauth else None,
                client_secret_override=client_secret,
            )
            return (
                {"Authorization": f"Bearer {exchanged.access_token}"},
                exchanged.cache_hit,
                audience,
            )
        if upstream.auth_type == "client_credentials":
            oauth = upstream.oauth
            if not oauth or not oauth.token_endpoint or not oauth.client_id:
                raise RuntimeError(
                    f"Upstream '{upstream.name}' requires oauth.token_endpoint and oauth.client_id"
                )
            client_secret = await self._resolve_oauth_client_secret(upstream)
            if not client_secret:
                raise RuntimeError(
                    f"Upstream '{upstream.name}' requires oauth.client_secret_ref "
                    "for client_credentials"
                )
            scopes = oauth.scopes or (
                [upstream.downstream_scope] if upstream.downstream_scope else []
            )
            token, cache_hit = await self.client_credentials.get_token(
                upstream_name=upstream.name,
                token_endpoint=oauth.token_endpoint,
                client_id=oauth.client_id,
                client_secret=client_secret,
                scopes=scopes,
            )
            header = oauth.token_header or "Authorization"
            prefix = f"{oauth.token_prefix} " if oauth.token_prefix else ""
            return {header: f"{prefix}{token}"}, cache_hit, upstream.downstream_audience
        if upstream.auth_type in ("bearer_secret", "api_key"):
            ref = upstream.secret_ref or (
                f"env://{upstream.secret_env_var}" if upstream.secret_env_var else ""
            )
            if not ref:
                raise RuntimeError(
                    f"Upstream '{upstream.name}' requires secret_ref or secret_env_var"
                )
            secret_val = await self.secret_resolver.resolve(ref, allow_bare_env=True)
            if upstream.auth_type == "bearer_secret":
                return {"Authorization": f"Bearer {secret_val}"}, False, None
            return {upstream.api_key_header: secret_val}, False, None
        if upstream.auth_type in ("user_oauth", "user_api_key"):
            if self.credential_vault is None:
                raise RuntimeError("CredentialVault is required for per-user upstream auth")
            record = await self.credential_vault.load_user_credential(
                tenant_id=identity.tenant_id,
                user_sub=identity.sub,
                upstream_name=upstream.name,
            )
            if record is None:
                raise UserCredentialRequiredError(
                    upstream_name=upstream.name,
                    auth_type=upstream.auth_type,
                    connect_url=self._build_connect_url(identity, upstream),
                    reason="not_connected",
                )
            payload = record["payload"]
            if upstream.auth_type == "user_api_key":
                api_key = str(payload.get("api_key") or "")
                if not api_key:
                    raise UserCredentialRequiredError(
                        upstream_name=upstream.name,
                        auth_type=upstream.auth_type,
                        connect_url=self._build_connect_url(identity, upstream),
                        reason="not_connected",
                    )
                header = (
                    upstream.oauth.token_header
                    if upstream.oauth and upstream.oauth.token_header
                    else upstream.api_key_header
                )
                prefix = (
                    f"{upstream.oauth.token_prefix} "
                    if upstream.oauth and upstream.oauth.token_prefix
                    else ""
                )
                return {header: f"{prefix}{api_key}"}, False, None

            expires_at = payload.get("expires_at")
            if isinstance(expires_at, int | float) and int(expires_at) <= int(time.time()) + 30:
                lock_key = f"{identity.tenant_id}:{identity.sub}:{upstream.name}"
                lock = self._refresh_locks.setdefault(lock_key, asyncio.Lock())
                async with lock:
                    latest = await self.credential_vault.load_user_credential(
                        tenant_id=identity.tenant_id,
                        user_sub=identity.sub,
                        upstream_name=upstream.name,
                    )
                    latest_payload = latest["payload"] if latest else payload
                    latest_exp = latest_payload.get("expires_at")
                    if (
                        isinstance(latest_exp, int | float)
                        and int(latest_exp) > int(time.time()) + 30
                    ):
                        payload = latest_payload
                    else:
                        payload = await self._refresh_user_oauth(identity, upstream, latest_payload)
            access_token = str(payload.get("access_token") or "")
            if not access_token:
                raise UserCredentialRequiredError(
                    upstream_name=upstream.name,
                    auth_type=upstream.auth_type,
                    connect_url=self._build_connect_url(identity, upstream),
                    reason="not_connected",
                )
            header = upstream.oauth.token_header if upstream.oauth else "Authorization"
            prefix = (
                f"{upstream.oauth.token_prefix} "
                if upstream.oauth and upstream.oauth.token_prefix
                else "Bearer "
            )
            return {header: f"{prefix}{access_token}"}, False, None
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
        requester_confirmation_policy: ToolApprovalPolicy | None = None,
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
                if self.metrics is not None:
                    self.metrics.inc_counter(
                        "gateway_policy_denials_total",
                        labels={"server": upstream.name, "tool": tool.qualified_name},
                    )
                log_event(
                    "policy.denied",
                    level=logging.WARNING,
                    server=upstream.name,
                    tool=tool.qualified_name,
                    reason=reason,
                )
                raise PermissionError(reason)

            current_upstream = self.registry.get(upstream.name) or upstream
            approval_policy = current_upstream.tool_approval_policies.get(tool.name)
            if (
                approval_policy is not None
                and approval_policy.mode in {"requester_confirmation", "manager_approval"}
                and requester_confirmation_policy != approval_policy
            ):
                reason = "Requester confirmation is required for this tool call."
                log_event(
                    "approval.confirmation_required",
                    level=logging.WARNING,
                    server=upstream.name,
                    tool=tool.qualified_name,
                    execution="codemode" if code_mode_hints else "direct",
                )
                raise PermissionError(reason)

            rl_key = f"rl:{identity.tenant_id}:{identity.sub}"
            count = await self.cache.increment_window(rl_key, window_seconds=60)
            if count > self.settings.rate_limit_per_minute:
                log_event(
                    "policy.denied",
                    level=logging.WARNING,
                    server=upstream.name,
                    tool=tool.qualified_name,
                    reason="rate_limit_exceeded",
                )
                raise RuntimeError(
                    f"Rate limit exceeded ({self.settings.rate_limit_per_minute}/min)"
                )

            if budget is not None:
                budget.consume()

            try:
                scoped_args = _apply_pre_call_abac(identity, tool, upstream, arguments)
            except PermissionError as exc:
                if self.metrics is not None:
                    self.metrics.inc_counter(
                        "gateway_policy_denials_total",
                        labels={"server": upstream.name, "tool": tool.qualified_name},
                    )
                log_event(
                    "policy.denied",
                    level=logging.WARNING,
                    server=upstream.name,
                    tool=tool.qualified_name,
                    reason=str(exc),
                )
                raise

            try:
                jsonschema.validate(instance=scoped_args, schema=tool.input_schema)
            except jsonschema.ValidationError as exc:
                msg = f"Invalid arguments for {tool.qualified_name}: {exc.message}"
                if code_mode_hints:
                    msg = f"{msg}\nExpected signature:\n{render_python_stub(tool)}"
                raise ValueError(msg) from exc

            cache_hit = False
            audience: str | None = None
            retry_attempts = 0
            audit_operation_id: str | None = None
            audit_claimed = False
            audit_completed = False
            audit_terminal_persisted = False
            upstream_execution_succeeded = False
            try:
                if (
                    upstream.health_check.enabled
                    and self.health_monitor is not None
                    and self.health_monitor.is_circuit_open(upstream.name)
                ):
                    raise UpstreamCircuitOpen(
                        f"Configured health check failed for '{upstream.name}'"
                    )
                auth_headers, cache_hit, audience = await self.resolve_upstream_headers(
                    identity, upstream
                )
                span.set_attribute("auth.token_cache_hit", cache_hit)
                if upstream.auth_type == "delegated_token" and self.metrics is not None:
                    self.metrics.inc_counter(
                        "gateway_obo_token_cache_total",
                        labels={
                            "outcome": "hit" if cache_hit else "miss",
                            "server": upstream.name,
                        },
                    )
                latest_upstream = self.registry.get(upstream.name) or upstream
                latest_approval_policy = latest_upstream.tool_approval_policies.get(tool.name)
                if (
                    latest_approval_policy is not None
                    and latest_approval_policy.mode
                    in {"requester_confirmation", "manager_approval"}
                    and requester_confirmation_policy != latest_approval_policy
                ):
                    reason = "Requester confirmation is required for this tool call."
                    log_event(
                        "approval.confirmation_required",
                        level=logging.WARNING,
                        server=upstream.name,
                        tool=tool.qualified_name,
                        execution="codemode" if code_mode_hints else "direct",
                    )
                    raise PermissionError(reason)
                operation_is_read = upstream.tool_effects.get(tool.name) == "read" or (
                    tool.name not in upstream.tool_effects and not tool.mutating
                )
                permit_key = f"upstream_concurrency:{upstream.name}"
                permit_token = uuid.uuid4().hex
                permit_acquired = False
                if current_upstream.max_concurrent_calls is not None:
                    permit_acquired = await self.cache.acquire_permit(
                        permit_key,
                        capacity=current_upstream.max_concurrent_calls,
                        lease_seconds=int(current_upstream.request_timeout_seconds * 2) + 15,
                        token=permit_token,
                    )
                    if not permit_acquired:
                        raise UpstreamCapacityExceeded(
                            f"Upstream '{upstream.name}' reached its concurrent call limit; "
                            "retry later."
                        )

                async def check_upstream_rate() -> None:
                    limit = current_upstream.max_requests_per_minute
                    if limit is None:
                        return
                    request_count = await self.cache.increment_window(
                        f"upstream_rate:{upstream.name}", window_seconds=60
                    )
                    if request_count > limit:
                        raise UpstreamCapacityExceeded(
                            f"Upstream '{upstream.name}' reached its request rate limit; "
                            "retry later."
                        )

                try:
                    await check_upstream_rate()
                    audit_operation_id = await self._prepare_sensitive_write_audit(
                        identity, tool, upstream, scoped_args
                    )
                    audit_claimed = audit_operation_id is not None
                    raw_result = await self.client_manager.call_tool(
                        upstream,
                        tool,
                        scoped_args,
                        auth_headers=auth_headers,
                    )
                    upstream_execution_succeeded = True
                    if audit_operation_id is not None:
                        finalized = await self._finish_sensitive_write_audit(
                            audit_operation_id, "succeeded"
                        )
                        if not finalized:
                            raise RuntimeError(
                                "Sensitive write completed but its audit status could not be saved"
                            )
                        audit_completed = True
                        audit_terminal_persisted = True
                except Exception as first_error:
                    first_failure = classify_upstream_error(
                        first_error, operation_is_read=operation_is_read
                    )
                    if not first_failure.retryable or isinstance(
                        first_error, UpstreamCapacityExceeded
                    ):
                        raise
                    retry_attempts = 1
                    retry_after = 0.0
                    response = getattr(first_error, "response", None)
                    if response is not None:
                        try:
                            retry_after = float(response.headers.get("Retry-After", "0"))
                        except ValueError:
                            retry_after = 0.0
                    await asyncio.sleep(
                        min(max(retry_after, 0.0), 2.0) + random.uniform(0.05, 0.25)
                    )
                    await check_upstream_rate()
                    raw_result = await self.client_manager.call_tool(
                        upstream,
                        tool,
                        scoped_args,
                        auth_headers=auth_headers,
                    )
                finally:
                    if permit_acquired:
                        try:
                            await self.cache.release_permit(permit_key, permit_token)
                        except Exception as release_error:
                            log_event(
                                "upstream.capacity_permit_release_failed",
                                level=logging.ERROR,
                                server=upstream.name,
                                error_type=type(release_error).__name__,
                            )
            except Exception as exc:
                duration_ms = round((time.perf_counter() - started) * 1000, 3)
                operation_is_read = (
                    upstream.tool_effects.get(tool.name, "read" if not tool.mutating else "write")
                    == "read"
                )
                if isinstance(exc, UserCredentialRequiredError):
                    failure = UpstreamFailure(
                        error_type="user_credential_required",
                        message=str(exc),
                    )
                elif isinstance(exc, httpx.HTTPStatusError) and (
                    upstream.auth_type in {"user_api_key", "user_oauth"}
                    or upstream.authorization_mode == "upstream"
                ):
                    status = exc.response.status_code
                    if status == 401 and upstream.auth_type in {"user_api_key", "user_oauth"}:
                        failure = UpstreamFailure(
                            error_type="user_credential_rejected",
                            message=(
                                f"The connected credential for '{upstream.name}' was rejected. "
                                "Reconnect this upstream from Connections and try again."
                            ),
                            status_code=status,
                        )
                    elif status == 403:
                        failure = UpstreamFailure(
                            error_type="upstream_access_denied",
                            message=(
                                f"You are authenticated to '{upstream.name}', but your account "
                                "is not authorized for this operation. Ask the upstream owner "
                                "to grant access."
                            ),
                            status_code=status,
                        )
                    else:
                        failure = classify_upstream_error(exc, operation_is_read=operation_is_read)
                else:
                    failure = classify_upstream_error(exc, operation_is_read=operation_is_read)
                if audit_claimed and not audit_completed and audit_operation_id is not None:
                    final_status = (
                        "outcome_unknown"
                        if failure.outcome_unknown or upstream_execution_succeeded
                        else "failed"
                    )
                    try:
                        audit_terminal_persisted = await self._finish_sensitive_write_audit(
                            audit_operation_id, final_status
                        )
                        if not audit_terminal_persisted:
                            log_event(
                                "audit.sensitive_write_status_failed",
                                level=logging.ERROR,
                                operation_id=audit_operation_id,
                                status=final_status,
                                error_type="journal_status_not_updated",
                            )
                    except Exception as audit_error:
                        log_event(
                            "audit.sensitive_write_status_failed",
                            level=logging.ERROR,
                            operation_id=audit_operation_id,
                            status=final_status,
                            error_type=type(audit_error).__name__,
                        )
                if audit_claimed and upstream_execution_succeeded:
                    failure = UpstreamFailure(
                        error_type="sensitive_write_outcome_unknown",
                        message=(
                            "The upstream write may have completed, but the gateway could not "
                            "durably record its outcome."
                        ),
                        outcome_unknown=True,
                    )
                elif audit_claimed and not audit_terminal_persisted and not failure.outcome_unknown:
                    failure = UpstreamFailure(
                        error_type=failure.error_type,
                        message=(
                            f"{failure.message} The gateway could not durably record the "
                            "journal status."
                        ),
                        retryable=False,
                        outcome_unknown=False,
                        status_code=failure.status_code,
                    )
                safe_error = OutputRedactor.redact_text(failure.message)
                record = SubcallRecord(
                    tool=tool.qualified_name,
                    server=upstream.name,
                    duration_ms=duration_ms,
                    raw_bytes=0,
                    status="error",
                    auth_type=upstream.auth_type,
                    token_cache_hit=cache_hit,
                    downstream_audience=audience,
                    error=safe_error,
                    error_type=failure.error_type,
                    retryable=failure.retryable,
                    retry_attempts=retry_attempts,
                    outcome_unknown=failure.outcome_unknown,
                    upstream_status_code=failure.status_code,
                    schema_covered=tool.input_schema is not None and tool.output_schema is not None,
                    is_mutating=bool(tool.mutating),
                )
                if self.metrics is not None:
                    self.metrics.inc_counter(
                        "gateway_upstream_calls_total",
                        labels={
                            "server": upstream.name,
                            "tool": tool.qualified_name,
                            "status": "error",
                            "error_type": failure.error_type,
                        },
                    )
                log_event(
                    "upstream.failed",
                    level=logging.WARNING,
                    server=upstream.name,
                    tool=tool.qualified_name,
                    duration_ms=duration_ms,
                    error_type=failure.error_type,
                    retry_attempts=record.retry_attempts,
                    outcome_unknown=failure.outcome_unknown,
                    journal_operation_id=(audit_operation_id if audit_claimed else None),
                )
                span.set_attribute("upstream.error_type", failure.error_type)
                span.record_exception(exc)
                return {
                    "_dispatch_error": safe_error,
                    "error_type": failure.error_type,
                    "retryable": failure.retryable,
                    "retry_attempts": record.retry_attempts,
                    "outcome_unknown": failure.outcome_unknown,
                    "upstream_status_code": failure.status_code,
                }, record

            raw_encoded = json.dumps(raw_result, default=str).encode("utf-8")
            raw_bytes = len(raw_encoded)
            output_sha256 = hashlib.sha256(raw_encoded).hexdigest()[:16]
            redacted_raw, redactions_count = OutputRedactor.redact_with_count(raw_result)
            try:
                filtered, abac_rows_filtered = _apply_post_call_abac_with_count(
                    identity, upstream, redacted_raw
                )
            except PermissionError as exc:
                if self.metrics is not None:
                    self.metrics.inc_counter(
                        "gateway_policy_denials_total",
                        labels={"server": upstream.name, "tool": tool.qualified_name},
                    )
                log_event(
                    "policy.denied",
                    level=logging.WARNING,
                    server=upstream.name,
                    tool=tool.qualified_name,
                    reason=str(exc),
                )
                raise

            span.set_attribute("upstream.raw_bytes", raw_bytes)
            span.set_attribute("abac.rows_filtered", abac_rows_filtered)
            span.set_attribute("redaction.count", redactions_count)
            if self.metrics is not None:
                self.metrics.inc_counter(
                    "gateway_upstream_calls_total",
                    labels={
                        "server": upstream.name,
                        "tool": tool.qualified_name,
                        "status": "ok",
                    },
                )
                if abac_rows_filtered > 0:
                    self.metrics.inc_counter(
                        "gateway_abac_rows_filtered_total",
                        amount=abac_rows_filtered,
                        labels={"server": upstream.name},
                    )
                if redactions_count > 0:
                    self.metrics.inc_counter(
                        "gateway_redactions_total",
                        amount=redactions_count,
                        labels={"server": upstream.name},
                    )
            if abac_rows_filtered > 0:
                log_event(
                    "abac.filtered",
                    server=upstream.name,
                    tool=tool.qualified_name,
                    rows_filtered=abac_rows_filtered,
                )
            if redactions_count > 0:
                log_event(
                    "redaction.applied",
                    server=upstream.name,
                    tool=tool.qualified_name,
                    redactions_count=redactions_count,
                )

            duration_ms = round((time.perf_counter() - started) * 1000, 3)
            if retry_attempts and self.metrics is not None:
                self.metrics.inc_counter(
                    "gateway_upstream_retries_total",
                    labels={"server": upstream.name, "tool": tool.qualified_name},
                )
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
                abac_rows_filtered=abac_rows_filtered,
                redactions_count=redactions_count,
                retry_attempts=retry_attempts,
                schema_covered=tool.input_schema is not None and tool.output_schema is not None,
                is_mutating=bool(tool.mutating),
            )
            return filtered, record
