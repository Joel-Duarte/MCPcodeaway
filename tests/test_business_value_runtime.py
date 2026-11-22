from __future__ import annotations

import pytest

from gateway.business_value_runtime import (
    build_workflow_call_key,
    canonical_json_digest,
    required_evidence_satisfied,
    result_matches_predicate,
    terminal_completion_satisfied,
)
from gateway.integration_definition import ResultPredicate, WorkflowDefinition


def _workflow() -> WorkflowDefinition:
    return WorkflowDefinition.model_validate(
        {
            "id": "support_ticket",
            "name": "Handle a support request",
            "description": "Review customer context and create a ticket.",
            "participating_tools": ["support__find_customer", "support__create_ticket"],
            "required_evidence": [
                {
                    "tool": "support__find_customer",
                    "result_when": {
                        "path": "/customer/id",
                        "type": "non_empty_string",
                    },
                }
            ],
            "completion": {
                "tool": "support__create_ticket",
                "success_when": {"path": "/ticket/id", "type": "non_empty_string"},
                "require_sensitive_write_journal_status": "succeeded",
            },
            "business_value": {
                "manual_baseline_minutes": 5,
                "baseline_source": "administrator_estimate",
                "confidence": "unvalidated",
                "effective_from": "2026-11-22",
            },
        }
    )


def test_canonical_digest_and_call_key_are_stable_and_body_bound() -> None:
    assert canonical_json_digest({"a": 1, "b": 2}) == canonical_json_digest({"b": 2, "a": 1})
    base = {
        "outer_request_id": "req-1",
        "request_digest": "a" * 64,
        "upstream_name": "support",
        "tool_name": "find_customer",
        "arguments": {"email": "person@example.test"},
        "callback_ordinal": 0,
    }
    key = build_workflow_call_key(**base)
    assert key == build_workflow_call_key(**base)
    assert key != build_workflow_call_key(**{**base, "request_digest": "b" * 64})
    assert key != build_workflow_call_key(**{**base, "callback_ordinal": 1})


def test_call_key_rejects_invalid_json_rpc_ids_and_ordinals() -> None:
    base = {
        "outer_request_id": True,
        "request_digest": "a" * 64,
        "upstream_name": "support",
        "tool_name": "find_customer",
        "arguments": {},
        "callback_ordinal": 0,
    }
    with pytest.raises(ValueError, match="JSON-RPC ID"):
        build_workflow_call_key(**base)

    with pytest.raises(ValueError, match="non-negative"):
        build_workflow_call_key(**{**base, "outer_request_id": "req-1", "callback_ordinal": -1})


def test_predicates_and_workflow_completion_use_persisted_boolean_evidence() -> None:
    workflow = _workflow()
    customer_result = {"customer": {"id": "C-100"}}
    assert result_matches_predicate(
        customer_result,
        ResultPredicate.model_validate({"path": "/customer/id", "type": "non_empty_string"}),
    )
    assert not result_matches_predicate(
        {"customer": {"id": " "}},
        ResultPredicate.model_validate({"path": "/customer/id", "type": "non_empty_string"}),
    )

    calls = [
        {
            "call_key": "customer-call",
            "upstream_name": "support",
            "tool_qualified_name": "support__find_customer",
            "status": "succeeded",
            "evidence_predicate_matched": True,
        },
        {
            "call_key": "ticket-call",
            "upstream_name": "support",
            "tool_qualified_name": "support__create_ticket",
            "status": "succeeded",
            "completion_predicate_matched": True,
            "sensitive_write_journal_status": "succeeded",
        },
    ]
    assert required_evidence_satisfied(workflow, calls)
    assert terminal_completion_satisfied(workflow, calls)
    assert not required_evidence_satisfied(workflow, [calls[1]])
    assert not terminal_completion_satisfied(workflow, [calls[0]])


def test_failed_or_missing_predicate_evidence_does_not_complete() -> None:
    workflow = _workflow()
    calls = [
        {
            "call_key": "customer-call",
            "upstream_name": "support",
            "tool_qualified_name": "support__find_customer",
            "status": "succeeded",
            "evidence_predicate_matched": False,
        },
        {
            "call_key": "ticket-call",
            "upstream_name": "support",
            "tool_qualified_name": "support__create_ticket",
            "status": "succeeded",
            "completion_predicate_matched": True,
            "sensitive_write_journal_status": "succeeded",
        },
    ]
    assert not required_evidence_satisfied(workflow, calls)
    assert terminal_completion_satisfied(workflow, calls)

    calls[0]["evidence_predicate_matched"] = True
    calls[0]["status"] = "failed"
    assert not required_evidence_satisfied(workflow, calls)
