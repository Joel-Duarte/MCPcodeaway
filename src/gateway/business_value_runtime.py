"""Deterministic helpers for workflow evidence and attribution identities.

This module deliberately contains no dispatch or authorization logic. Callers must
continue to use the normal catalog, policy and ``HostDispatcher`` path.
"""

from __future__ import annotations

import copy
import hashlib
import json
import re
from typing import Any

from gateway.integration_definition import ResultPredicate, WorkflowDefinition


def extract_caller_regions(
    *,
    claims: dict[str, Any] | None = None,
    client_groups: list[str] | tuple[str, ...] = (),
) -> tuple[str, ...]:
    """Return ordered uppercase region/country identifiers from verified caller identity."""
    candidates: list[str] = []
    if isinstance(claims, dict):
        for key in ("country", "c", "region", "geo"):
            raw = claims.get(key)
            if isinstance(raw, str) and raw.strip():
                candidates.append(raw.strip().upper())
            elif isinstance(raw, (list, tuple)):
                for item in raw:
                    if isinstance(item, str) and item.strip():
                        candidates.append(item.strip().upper())
    for grp in client_groups:
        if isinstance(grp, str) and grp.strip() and grp.strip().upper() != "ALL":
            candidates.append(grp.strip().upper())
    return tuple(dict.fromkeys(candidates))


def apply_regional_baseline(
    baseline: dict[str, Any], caller_regions: tuple[str, ...] | list[str] = ()
) -> dict[str, Any]:
    """Snapshot baseline with any caller-region rate override in the same gateway currency."""
    snapshot = copy.deepcopy(baseline)
    regional = snapshot.get("regional_labor_rates")
    if isinstance(regional, dict) and caller_regions:
        normalized_map = {str(k).strip().upper(): v for k, v in regional.items() if str(k).strip()}
        for region in caller_regions:
            key = str(region).strip().upper()
            if key in normalized_map:
                snapshot["loaded_labor_cost_per_hour"] = float(normalized_map[key])
                snapshot["resolved_region"] = key
                return snapshot
    snapshot.setdefault("resolved_region", None)
    return snapshot


def canonical_json_digest(value: Any) -> str:
    """Return a stable SHA-256 digest for JSON-compatible values."""

    encoded = json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def encode_jsonrpc_request_id(request_id: str | int) -> str:
    """Preserve JSON-RPC ID type when storing it in a text key."""

    if isinstance(request_id, bool) or not isinstance(request_id, (str, int)):
        raise ValueError("attributed tool requests require a string or integer JSON-RPC ID")
    if isinstance(request_id, str):
        if not request_id:
            raise ValueError("attributed tool requests require a non-empty JSON-RPC ID")
        return "string:" + request_id
    return f"integer:{request_id}"


def build_workflow_call_key(
    *,
    outer_request_id: str | int,
    request_digest: str,
    upstream_name: str,
    tool_name: str,
    arguments: dict[str, Any],
    callback_ordinal: int,
    business_execution_id: str = "",
    tenant_id: str = "",
    caller_sub: str = "",
    client_id: str = "",
) -> str:
    """Build the stable identity of one logical tool callback.

    The JSON-RPC ID alone is not an idempotency key. The request digest fences
    body changes, while the operation identity and Code Mode callback ordinal
    distinguish calls carried by the same outer execution request.
    """

    encode_jsonrpc_request_id(outer_request_id)
    if callback_ordinal < 0:
        raise ValueError("callback_ordinal must be non-negative")
    return canonical_json_digest(
        {
            "outer_request_id": outer_request_id,
            "request_digest": request_digest,
            "business_execution_id": business_execution_id,
            "tenant_id": tenant_id,
            "caller_sub": caller_sub,
            "client_id": client_id,
            "upstream_name": upstream_name,
            "tool_name": tool_name,
            "arguments_digest": canonical_json_digest(arguments),
            "callback_ordinal": callback_ordinal,
        }
    )


def json_pointer_value(value: Any, pointer: str) -> Any:
    """Resolve a validated RFC 6901 JSON Pointer against an upstream result."""

    if pointer == "":
        return value
    if not pointer.startswith("/"):
        raise ValueError("JSON Pointer must start with '/'")
    current = value
    for raw_token in pointer[1:].split("/"):
        if re.search(r"~(?![01])", raw_token):
            raise ValueError("JSON Pointer has an invalid '~' escape")
        token = raw_token.replace("~1", "/").replace("~0", "~")
        if isinstance(current, dict):
            if token not in current:
                return _MISSING
            current = current[token]
        elif isinstance(current, list) and token.isdigit():
            index = int(token)
            if index >= len(current):
                return _MISSING
            current = current[index]
        else:
            return _MISSING
    return current


def resolve_json_pointer(value: Any, pointer: str) -> tuple[bool, Any]:
    resolved = json_pointer_value(value, pointer)
    return (resolved is not _MISSING, None if resolved is _MISSING else resolved)


_MISSING = object()


def result_matches_predicate(value: Any, predicate: ResultPredicate) -> bool:
    """Evaluate a manifest's bounded structural result predicate."""

    found = json_pointer_value(value, predicate.path)
    if found is _MISSING:
        return False
    kind = predicate.type
    if kind == "string":
        type_matches = isinstance(found, str)
    elif kind == "non_empty_string":
        type_matches = isinstance(found, str) and bool(found.strip())
    elif kind == "object":
        type_matches = isinstance(found, dict)
    elif kind == "non_empty_object":
        type_matches = isinstance(found, dict) and bool(found)
    elif kind == "array":
        type_matches = isinstance(found, list)
    elif kind == "non_empty_array":
        type_matches = isinstance(found, list) and bool(found)
    elif kind == "boolean":
        type_matches = isinstance(found, bool)
    elif kind == "number":
        type_matches = isinstance(found, (int, float)) and not isinstance(found, bool)
    elif kind == "integer":
        type_matches = isinstance(found, int) and not isinstance(found, bool)
    elif kind == "null":
        type_matches = found is None
    else:
        return False
    if not type_matches:
        return False
    if predicate.equals is not None and found != predicate.equals:
        return False
    if predicate.one_of is not None and found not in predicate.one_of:
        return False
    return True


def required_evidence_satisfied(workflow: WorkflowDefinition, calls: list[dict[str, Any]]) -> bool:
    """Check unordered required evidence over successful logical calls.

    The store must return one row per ``call_key``. Retries with the same key do
    not increase the successful-call count.
    """

    unique_calls: dict[str, dict[str, Any]] = {}
    for call in calls:
        call_key = call.get("call_key")
        if isinstance(call_key, str):
            unique_calls.setdefault(call_key, call)
    for requirement in workflow.required_evidence:
        count = 0
        for call in unique_calls.values():
            qualified_name = call.get("tool_qualified_name")
            if not isinstance(qualified_name, str):
                upstream_name = call.get("upstream_name")
                tool_name = call.get("tool_name")
                qualified_name = (
                    f"{upstream_name}__{tool_name}"
                    if isinstance(upstream_name, str) and isinstance(tool_name, str)
                    else None
                )
            if qualified_name != requirement.tool:
                continue
            if call.get("status") != "succeeded":
                continue
            if (
                requirement.result_when is not None
                and _call_evidence_value(call, "evidence_predicate_matched") is not True
            ):
                continue
            count += 1
        if count < requirement.min_successful_calls:
            return False
    return True


def terminal_completion_satisfied(
    workflow: WorkflowDefinition, calls: list[dict[str, Any]]
) -> bool:
    """Check whether a successful terminal call satisfies its result predicate."""

    return any(
        (
            call.get("tool_qualified_name")
            or (
                f"{call.get('upstream_name')}__{call.get('tool_name')}"
                if isinstance(call.get("upstream_name"), str)
                and isinstance(call.get("tool_name"), str)
                else None
            )
        )
        == workflow.completion.tool
        and call.get("status") == "succeeded"
        and _call_evidence_value(call, "completion_predicate_matched") is True
        and (
            workflow.completion.require_sensitive_write_journal_status is None
            or _call_evidence_value(call, "sensitive_write_journal_status")
            == workflow.completion.require_sensitive_write_journal_status
        )
        for call in calls
    )


def _call_evidence_value(call: dict[str, Any], key: str) -> Any:
    nested = call.get("evidence")
    if isinstance(nested, dict) and key in nested:
        return nested[key]
    return call.get(key)
