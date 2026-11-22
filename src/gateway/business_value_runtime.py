"""Deterministic helpers for workflow evidence and attribution identities.

This module deliberately contains no dispatch or authorization logic. Callers must
continue to use the normal catalog, policy and ``HostDispatcher`` path.
"""

from __future__ import annotations

import hashlib
import json
import re
from typing import Any

from gateway.integration_definition import ResultPredicate, WorkflowDefinition


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


def build_workflow_call_key(
    *,
    outer_request_id: str | int,
    request_digest: str,
    upstream_name: str,
    tool_name: str,
    arguments: dict[str, Any],
    callback_ordinal: int,
) -> str:
    """Build the stable identity of one logical tool callback.

    The JSON-RPC ID alone is not an idempotency key. The request digest fences
    body changes, while the operation identity and Code Mode callback ordinal
    distinguish calls carried by the same outer execution request.
    """

    if isinstance(outer_request_id, bool) or not isinstance(outer_request_id, (str, int)):
        raise ValueError("attributed tool requests require a string or integer JSON-RPC ID")
    if isinstance(outer_request_id, str) and not outer_request_id:
        raise ValueError("attributed tool requests require a non-empty JSON-RPC ID")
    if callback_ordinal < 0:
        raise ValueError("callback_ordinal must be non-negative")
    return canonical_json_digest(
        {
            "outer_request_id": outer_request_id,
            "request_digest": request_digest,
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
                and call.get("evidence_predicate_matched") is not True
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
        and call.get("completion_predicate_matched") is True
        and (
            workflow.completion.require_sensitive_write_journal_status is None
            or call.get("sensitive_write_journal_status")
            == workflow.completion.require_sensitive_write_journal_status
        )
        for call in calls
    )
