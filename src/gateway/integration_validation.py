"""Catalog-aware checks and tool definitions for published integrations."""

from __future__ import annotations

import copy
from typing import Any

from jsonschema.validators import validator_for

from gateway.catalog import ToolCatalog, to_mcp_tool_definition
from gateway.integration_definition import (
    IntegrationDefinition,
    ResultPredicate,
    validate_result_predicate_schema,
)
from gateway.mcp_app_renderer import validate_experience
from gateway.upstream_clients import compute_schema_digest

ATTRIBUTED_TOOL_PREFIX = "mca_attr__"
START_BUSINESS_EXECUTION_TOOL = "mcpcodeaway_start_business_execution"
SUPPORTED_SCHEMA_DIALECTS = {
    "http://json-schema.org/draft-07/schema#",
    "https://json-schema.org/draft/2019-09/schema",
    "https://json-schema.org/draft/2020-12/schema",
}


def validate_definition_against_catalog(
    definition: IntegrationDefinition,
    catalog: ToolCatalog,
    *,
    reserved_tool_names: set[str] | None = None,
) -> None:
    """Fail closed on stale, cross-upstream, or unwrappable tool references."""

    tools: dict[str, Any] = {}
    for reference in definition.tool_refs:
        tool = catalog.get_tool(reference.name)
        if tool is None:
            raise ValueError(f"unknown tool reference: {reference.name}")
        if tool.server != definition.integration.upstream_name:
            raise ValueError(
                f"tool {reference.name} does not belong to integration upstream "
                f"{definition.integration.upstream_name}"
            )
        actual_digest = compute_schema_digest([tool])
        if actual_digest.lower() != reference.schema_sha256.lower():
            raise ValueError(f"schema digest is stale for {reference.name}")
        validate_attributed_input_schema(tool.input_schema)
        tools[reference.name] = tool

    synthetic_names = {START_BUSINESS_EXECUTION_TOOL}
    for name in tools:
        wrapper_name = attributed_tool_name(name)
        if wrapper_name in synthetic_names:
            raise ValueError(f"reserved attributed tool name collision: {wrapper_name}")
        synthetic_names.add(wrapper_name)
    collisions = synthetic_names & (reserved_tool_names or set())
    if collisions:
        raise ValueError("reserved tool name collision: " + ", ".join(sorted(collisions)))

    for workflow in definition.workflows:
        for evidence in workflow.required_evidence:
            tool = tools[evidence.tool]
            _validate_predicate(evidence.result_when, tool.output_schema, evidence.tool)
        completion_tool = tools[workflow.completion.tool]
        _validate_predicate(
            workflow.completion.success_when,
            completion_tool.output_schema,
            workflow.completion.tool,
        )
        if workflow.completion.require_sensitive_write_journal_status == "succeeded":
            if not completion_tool.mutating:
                raise ValueError(
                    "completion requires a sensitive-write journal, but the terminal tool "
                    "is not classified as mutating"
                )
        if workflow.dedupe is not None:
            for identity_part in workflow.dedupe.identity_parts:
                _validate_predicate_path_only(
                    identity_part.path, completion_tool.output_schema, workflow.completion.tool
                )

    for standalone in definition.standalone_tool_values:
        tool = tools[standalone.tool]
        _validate_predicate(
            standalone.completion.success_when,
            tool.output_schema,
            standalone.tool,
        )
        if standalone.completion.require_sensitive_write_journal_status == "succeeded":
            if not tool.mutating:
                raise ValueError(
                    "completion requires a sensitive-write journal, but the selected tool "
                    "is not classified as mutating"
                )

    for experience in definition.experiences:
        tool = tools[experience.tool]
        validate_experience(
            experience.model_dump(mode="json"),
            tool_input_schema=tool.input_schema,
        )


def _validate_predicate(
    predicate: ResultPredicate | None, output_schema: dict[str, Any] | None, tool_name: str
) -> None:
    if predicate is None:
        return
    if not isinstance(output_schema, dict):
        raise ValueError(f"tool {tool_name} has no output schema for its result predicate")
    validate_result_predicate_schema(predicate, output_schema)


def _validate_predicate_path_only(
    path: str, output_schema: dict[str, Any] | None, tool_name: str
) -> None:
    if not isinstance(output_schema, dict):
        raise ValueError(f"tool {tool_name} has no output schema for its identity path")
    validate_result_predicate_schema(
        ResultPredicate.model_validate({"path": path, "type": "string"}), output_schema
    )


def validate_attributed_input_schema(schema: dict[str, Any]) -> None:
    """Check an upstream input schema can be safely nested without rewriting it."""

    dialect = schema.get("$schema")
    if dialect is not None and dialect not in SUPPORTED_SCHEMA_DIALECTS:
        raise ValueError(f"unsupported JSON Schema dialect for attributed wrapper: {dialect}")
    validator = validator_for(schema)
    validator.check_schema(schema)
    _reject_unresolved_refs(schema)


def validate_attributed_arguments(schema: dict[str, Any], arguments: dict[str, Any]) -> None:
    """Validate wrapper arguments against the unchanged original tool schema."""

    validate_attributed_input_schema(schema)
    validator = validator_for(schema)(schema)
    error = next(validator.iter_errors(arguments), None)
    if error is not None:
        location = "/".join(str(part) for part in error.absolute_path) or "/"
        raise ValueError(f"arguments do not match the current tool schema at {location}")


def _reject_unresolved_refs(value: Any) -> None:
    if isinstance(value, dict):
        ref = value.get("$ref")
        if ref is not None and (not isinstance(ref, str) or not ref.startswith("#/")):
            raise ValueError("attributed wrappers support local JSON Schema references only")
        for child in value.values():
            _reject_unresolved_refs(child)
    elif isinstance(value, list):
        for child in value:
            _reject_unresolved_refs(child)


def attributed_tool_name(qualified_tool_name: str) -> str:
    return f"{ATTRIBUTED_TOOL_PREFIX}{qualified_tool_name}"


def build_attributed_tool_definition(
    tool: Any, *, resource_uri: str | None = None
) -> dict[str, Any]:
    """Build a gateway-owned envelope while preserving the original nested schema."""

    original = to_mcp_tool_definition(tool)
    input_schema = copy.deepcopy(original["inputSchema"])
    properties: dict[str, Any] = {
        "business_execution_id": {
            "type": "string",
            "minLength": 1,
            "maxLength": 128,
            "description": "Gateway-issued execution ID returned by the start tool.",
        },
        "arguments": input_schema,
    }
    wrapper_schema: dict[str, Any] = {
        "type": "object",
        "properties": properties,
        "required": ["business_execution_id", "arguments"],
        "additionalProperties": False,
    }
    if "$schema" in input_schema:
        wrapper_schema["$schema"] = input_schema["$schema"]
    for defs_key in ("$defs", "definitions"):
        if isinstance(input_schema.get(defs_key), dict):
            wrapper_schema[defs_key] = copy.deepcopy(input_schema[defs_key])

    metadata: dict[str, Any] = {"visibility": ["model", "app"]}
    if resource_uri:
        metadata["resourceUri"] = resource_uri
    result: dict[str, Any] = {
        "name": attributed_tool_name(tool.qualified_name),
        "description": (
            f"Attributed wrapper for {tool.qualified_name}. Provide the original tool input "
            "unchanged under arguments and the business_execution_id returned by "
            "mcpcodeaway_start_business_execution. The ID does not grant tool access."
        ),
        "inputSchema": wrapper_schema,
        "annotations": original.get("annotations", {"readOnlyHint": not tool.mutating}),
        "_meta": {"ui": metadata},
    }
    if "outputSchema" in original:
        result["outputSchema"] = {
            "type": "object",
            "properties": {
                "ok": {"type": "boolean"},
                "result": {
                    "anyOf": [original["outputSchema"], {"type": "null"}],
                },
                "business_execution_id": {"type": "string"},
                "workflow_call_id": {"type": "string"},
                "completion_recorded": {"type": "boolean"},
            },
            "required": [
                "ok",
                "result",
                "business_execution_id",
                "workflow_call_id",
                "completion_recorded",
            ],
            "additionalProperties": False,
        }
    return result


def build_start_business_execution_definition(
    published_definitions: list[dict[str, Any]],
) -> dict[str, Any] | None:
    """Create the explicit model/App start tool, or hide it when nothing is eligible."""

    workflows: dict[str, str] = {}
    standalone_values: dict[str, str] = {}
    for row in published_definitions:
        upstream_name = str(row["upstream_name"])
        definition = row["definition"]
        for workflow in definition.get("workflows", []):
            workflow_id = f"{upstream_name}/{workflow['id']}"
            if workflow_id in workflows:
                raise ValueError(f"duplicate published workflow ID: {workflow_id}")
            workflows[workflow_id] = f"{workflow['name']}: {workflow['description']}"
        for standalone in definition.get("standalone_tool_values", []):
            value_id = f"{upstream_name}/{standalone['id']}"
            if value_id in standalone_values:
                raise ValueError(f"duplicate published standalone ID: {value_id}")
            standalone_values[value_id] = f"{standalone['name']}: {standalone['description']}"
    if not workflows and not standalone_values:
        return None

    variants: list[dict[str, Any]] = []
    if workflows:
        variants.append(
            {
                "type": "object",
                "properties": {
                    "kind": {"const": "workflow"},
                    "workflow_id": {
                        "type": "string",
                        "enum": sorted(workflows),
                        "description": "Published workflow ID: "
                        + "; ".join(f"{key} — {workflows[key]}" for key in sorted(workflows)),
                    },
                },
                "required": ["kind", "workflow_id"],
                "additionalProperties": False,
            }
        )
    if standalone_values:
        variants.append(
            {
                "type": "object",
                "properties": {
                    "kind": {"const": "standalone_tool"},
                    "tool_value_id": {
                        "type": "string",
                        "enum": sorted(standalone_values),
                        "description": "Published standalone outcome: "
                        + "; ".join(
                            f"{key} — {standalone_values[key]}" for key in sorted(standalone_values)
                        ),
                    },
                },
                "required": ["kind", "tool_value_id"],
                "additionalProperties": False,
            }
        )
    return {
        "name": START_BUSINESS_EXECUTION_TOOL,
        "description": (
            "Start an explicit business-value execution for an eligible published workflow "
            "or one-tool outcome. This does not grant access to any tool; all calls still use "
            "the gateway's normal authorization and approval checks."
        ),
        "inputSchema": {"oneOf": variants},
        "annotations": {"readOnlyHint": True},
        "_meta": {"ui": {"visibility": ["model", "app"]}},
    }
