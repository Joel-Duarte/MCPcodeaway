from __future__ import annotations

import pytest
from pydantic import ValidationError

from gateway.integration_definition import (
    IntegrationDefinition,
    ResultPredicate,
    StandaloneToolValueDefinition,
    WorkflowDefinition,
    validate_definition_output_schemas,
    validate_result_predicate_schema,
)

HASH = "a" * 64


def baseline() -> dict[str, object]:
    return {
        "manual_baseline_minutes": 5,
        "baseline_source": "administrator_estimate",
        "confidence": "unvalidated",
        "effective_from": "2026-11-22",
    }


def workflow() -> dict[str, object]:
    return {
        "id": "support_ticket",
        "name": "Handle a support request",
        "description": "Review customer and order context, then create a ticket.",
        "participating_tools": [
            "support__find_customer",
            "support__get_orders",
            "support__create_ticket",
        ],
        "required_evidence": [
            {
                "tool": "support__find_customer",
                "min_successful_calls": 1,
                "result_when": {"path": "/customer/id", "type": "non_empty_string"},
            },
            {
                "tool": "support__get_orders",
                "min_successful_calls": 1,
                "result_when": {"path": "/orders", "type": "array"},
            },
        ],
        "completion": {
            "kind": "terminal_tool_result",
            "tool": "support__create_ticket",
            "success_when": {"path": "/ticket/id", "type": "non_empty_string"},
            "require_sensitive_write_journal_status": "succeeded",
        },
        "business_value": baseline(),
    }


def test_workflow_separates_participating_tools_required_evidence_and_completion() -> None:
    parsed = WorkflowDefinition.model_validate(workflow())

    assert set(parsed.participating_tools) == {
        "support__find_customer",
        "support__get_orders",
        "support__create_ticket",
    }
    assert [item.tool for item in parsed.required_evidence] == [
        "support__find_customer",
        "support__get_orders",
    ]
    assert parsed.completion.tool == "support__create_ticket"


def test_required_evidence_is_an_unordered_set_of_unique_participating_tools() -> None:
    data = workflow()
    data["required_evidence"] = [
        {"tool": "support__create_ticket"},
        {"tool": "support__create_ticket"},
    ]
    with pytest.raises(ValidationError, match="one entry per tool"):
        WorkflowDefinition.model_validate(data)

    data["required_evidence"] = [{"tool": "support__not_in_workflow"}]
    with pytest.raises(ValidationError, match="in participating_tools"):
        WorkflowDefinition.model_validate(data)


def test_completion_tool_must_participate() -> None:
    data = workflow()
    data["completion"] = {
        "kind": "terminal_tool_result",
        "tool": "support__outside_workflow",
        "success_when": {"path": "/id", "type": "string"},
    }
    with pytest.raises(ValidationError, match="completion tool must be in participating_tools"):
        WorkflowDefinition.model_validate(data)


def test_result_predicate_rejects_malformed_json_pointers() -> None:
    for pointer in ("id", "/bad~escape", "/bad~2escape"):
        with pytest.raises(ValidationError):
            ResultPredicate.model_validate({"path": pointer, "type": "string"})


def test_unknown_fields_are_rejected_at_nested_levels() -> None:
    data = workflow()
    data["unexpected"] = "not accepted"
    with pytest.raises(ValidationError, match="Extra inputs are not permitted"):
        WorkflowDefinition.model_validate(data)

    data = workflow()
    data["required_evidence"] = [{"tool": "support__find_customer", "step_order": 1}]
    with pytest.raises(ValidationError, match="Extra inputs are not permitted"):
        WorkflowDefinition.model_validate(data)


def test_standalone_definition_is_exactly_one_tool_with_matching_terminal_rule() -> None:
    definition = StandaloneToolValueDefinition.model_validate(
        {
            "id": "create_ticket",
            "name": "Create support ticket",
            "description": "Create one support ticket.",
            "tool": "support__create_ticket",
            "completion": {
                "tool": "support__create_ticket",
                "success_when": {"path": "/ticket/id", "type": "non_empty_string"},
            },
            "business_value": baseline(),
        }
    )
    assert definition.tool == definition.completion.tool

    data = definition.model_dump()
    data["completion"]["tool"] = "support__close_ticket"
    with pytest.raises(ValidationError, match="must match selected tool"):
        StandaloneToolValueDefinition.model_validate(data)


def test_manifest_checks_tool_refs_and_duplicate_ids() -> None:
    payload = {
        "integration": {
            "id": "support",
            "display_name": "Support",
            "upstream_name": "support_api",
        },
        "tool_refs": [
            {"name": name, "schema_sha256": HASH}
            for name in ["support__find_customer", "support__get_orders", "support__create_ticket"]
        ],
        "workflows": [workflow()],
    }
    assert IntegrationDefinition.model_validate(payload).workflows[0].id == "support_ticket"

    payload["tool_refs"] = [*payload["tool_refs"], payload["tool_refs"][0]]
    with pytest.raises(ValidationError, match="tool_refs must be unique"):
        IntegrationDefinition.model_validate(payload)


def test_dedupe_identity_and_baseline_constraints() -> None:
    data = workflow()
    data["dedupe"] = {
        "namespace": "support.create_ticket.v1",
        "identity_parts": [{"source": "terminal_result", "path": "/ticket/id"}],
    }
    parsed = WorkflowDefinition.model_validate(data)
    assert parsed.dedupe is not None

    data["dedupe"]["identity_parts"] = [{"source": "terminal_result", "path": "ticket/id"}]
    with pytest.raises(ValidationError):
        WorkflowDefinition.model_validate(data)

    invalid_baseline = baseline()
    invalid_baseline["loaded_labor_cost_per_hour"] = 25
    with pytest.raises(ValidationError, match="must be supplied together"):
        WorkflowDefinition.model_validate({**workflow(), "business_value": invalid_baseline})


def test_result_predicate_path_is_checked_against_output_schema() -> None:
    predicate = ResultPredicate.model_validate({"path": "/customer/id", "type": "non_empty_string"})
    schema = {
        "$schema": "https://json-schema.org/draft/2020-12/schema",
        "type": "object",
        "properties": {
            "customer": {
                "type": "object",
                "properties": {"id": {"type": "string"}},
                "required": ["id"],
            }
        },
    }
    validate_result_predicate_schema(predicate, schema)

    with pytest.raises(ValueError, match="not declared"):
        validate_result_predicate_schema(
            ResultPredicate.model_validate({"path": "/customer/name", "type": "string"}),
            schema,
        )
    with pytest.raises(ValueError, match="incompatible"):
        validate_result_predicate_schema(
            ResultPredicate.model_validate({"path": "/customer/id", "type": "array"}),
            schema,
        )


def test_manifest_rejects_overlapping_workflow_and_standalone_terminal_tool() -> None:
    workflow_data = workflow()
    standalone = {
        "id": "standalone_ticket",
        "name": "Create one ticket",
        "description": "Create one support ticket.",
        "tool": "support__create_ticket",
        "completion": {
            "tool": "support__create_ticket",
            "success_when": {"path": "/ticket/id", "type": "non_empty_string"},
        },
        "business_value": baseline(),
    }
    tool_names = [*workflow_data["participating_tools"]]
    payload = {
        "integration": {
            "id": "support",
            "display_name": "Support",
            "upstream_name": "support_api",
        },
        "tool_refs": [{"name": name, "schema_sha256": HASH} for name in tool_names],
        "workflows": [workflow_data],
        "standalone_tool_values": [standalone],
    }
    with pytest.raises(ValidationError, match="cannot share terminal tools"):
        IntegrationDefinition.model_validate(payload)


def test_result_predicate_schema_validation_handles_defs_refs_and_composition() -> None:
    predicate = ResultPredicate.model_validate({"path": "/customer/id", "type": "non_empty_string"})
    schema = {
        "$schema": "https://json-schema.org/draft/2020-12/schema",
        "$defs": {
            "customer": {
                "type": "object",
                "properties": {"id": {"type": ["string", "null"]}},
            }
        },
        "allOf": [
            {"$ref": "#/$defs/customer"},
            {"properties": {"customer": {"type": "object"}}},
        ],
        "properties": {
            "customer": {"$ref": "#/$defs/customer"},
        },
    }
    validate_result_predicate_schema(predicate, schema)

    unsupported = {"$schema": "https://example.invalid/schema", "type": "object"}
    with pytest.raises(ValueError, match="unsupported JSON Schema dialect"):
        validate_result_predicate_schema(predicate, unsupported)


def test_definition_predicates_are_validated_with_each_catalog_output_schema() -> None:
    payload = {
        "integration": {
            "id": "support",
            "display_name": "Support",
            "upstream_name": "support_api",
        },
        "tool_refs": [
            {"name": name, "schema_sha256": HASH}
            for name in ["support__find_customer", "support__get_orders", "support__create_ticket"]
        ],
        "workflows": [workflow()],
    }
    definition = IntegrationDefinition.model_validate(payload)
    output_schemas = {
        "support__find_customer": {
            "type": "object",
            "properties": {
                "customer": {"type": "object", "properties": {"id": {"type": "string"}}}
            },
        },
        "support__get_orders": {
            "type": "object",
            "properties": {"orders": {"type": "array"}},
        },
        "support__create_ticket": {
            "type": "object",
            "properties": {"ticket": {"type": "object", "properties": {"id": {"type": "string"}}}},
        },
    }
    validate_definition_output_schemas(definition, output_schemas)

    del output_schemas["support__get_orders"]
    with pytest.raises(ValueError, match="output schema is unavailable"):
        validate_definition_output_schemas(definition, output_schemas)
