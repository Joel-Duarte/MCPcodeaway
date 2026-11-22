from __future__ import annotations

import copy

import pytest

from examples.mock_support_workflow import _TOOLS
from gateway.catalog import ToolCatalog
from gateway.config import UpstreamConfig
from gateway.integration_definition import IntegrationDefinition
from gateway.integration_validation import (
    ATTRIBUTED_TOOL_PREFIX,
    START_BUSINESS_EXECUTION_TOOL,
    build_attributed_tool_definition,
    build_start_business_execution_definition,
    validate_definition_against_catalog,
)
from gateway.upstream_clients import DiscoveredTool, compute_schema_digest


def _catalog() -> ToolCatalog:
    catalog = ToolCatalog()
    tools = [
        DiscoveredTool(
            server="support_api",
            name=raw["name"],
            qualified_name=f"support_api__{raw['name']}",
            description=raw["description"],
            input_schema=copy.deepcopy(raw["inputSchema"]),
            output_schema=copy.deepcopy(raw["outputSchema"]),
            mutating=raw["name"] == "create_ticket",
            annotations={"readOnlyHint": raw["name"] != "create_ticket"},
        )
        for raw in _TOOLS
    ]
    catalog.set_server_tools(
        UpstreamConfig(
            name="support_api",
            type="mcp_http",
            url="http://support.example.test/mcp",
        ),
        tools,
    )
    return catalog


def _definition(catalog: ToolCatalog) -> IntegrationDefinition:
    tool_names = [tool.qualified_name for tool in catalog.get_server_tools("support_api")]
    refs = [
        {
            "name": tool.qualified_name,
            "schema_sha256": compute_schema_digest([tool]),
        }
        for tool in catalog.get_server_tools("support_api")
    ]
    return IntegrationDefinition.model_validate(
        {
            "integration": {
                "id": "support",
                "display_name": "Support desk",
                "upstream_name": "support_api",
            },
            "tool_refs": refs,
            "workflows": [
                {
                    "id": "support_request",
                    "name": "Handle a support request",
                    "description": (
                        "Review customer, account and order context, then create a ticket."
                    ),
                    "participating_tools": tool_names,
                    "required_evidence": [
                        {
                            "tool": "support_api__find_customer",
                            "result_when": {
                                "path": "/customer/id",
                                "type": "non_empty_string",
                            },
                        },
                        {
                            "tool": "support_api__get_account",
                            "result_when": {
                                "path": "/account/id",
                                "type": "non_empty_string",
                            },
                        },
                        {
                            "tool": "support_api__get_order_history",
                            "result_when": {"path": "/orders", "type": "array"},
                        },
                    ],
                    "completion": {
                        "tool": "support_api__create_ticket",
                        "success_when": {
                            "path": "/ticket/id",
                            "type": "non_empty_string",
                        },
                        "require_sensitive_write_journal_status": "succeeded",
                    },
                    "business_value": {
                        "manual_baseline_minutes": 5,
                        "baseline_source": "administrator_estimate",
                        "confidence": "unvalidated",
                        "effective_from": "2026-11-22",
                    },
                }
            ],
        }
    )


def test_catalog_validation_checks_schema_hashes_output_paths_and_write_journal() -> None:
    catalog = _catalog()
    definition = _definition(catalog)
    validate_definition_against_catalog(definition, catalog)

    stale = definition.model_copy(deep=True)
    stale.tool_refs[0].schema_sha256 = "0" * 64
    with pytest.raises(ValueError, match="schema digest is stale"):
        validate_definition_against_catalog(stale, catalog)

    invalid_path = definition.model_copy(deep=True)
    invalid_path.workflows[0].required_evidence[0].result_when.path = "/customer/missing"
    with pytest.raises(ValueError, match="not declared"):
        validate_definition_against_catalog(invalid_path, catalog)


def test_attributed_wrapper_has_closed_envelope_and_explicit_app_visibility() -> None:
    tool = _catalog().get_tool("support_api__create_ticket")
    original_schema = copy.deepcopy(tool.input_schema)
    wrapper = build_attributed_tool_definition(tool)

    assert wrapper["name"] == f"{ATTRIBUTED_TOOL_PREFIX}{tool.qualified_name}"
    assert wrapper["inputSchema"]["required"] == ["business_execution_id", "arguments"]
    assert wrapper["inputSchema"]["additionalProperties"] is False
    assert wrapper["inputSchema"]["properties"]["arguments"] == original_schema
    assert wrapper["_meta"]["ui"]["visibility"] == ["model", "app"]
    assert "resourceUri" not in wrapper["_meta"]["ui"]
    assert tool.input_schema == original_schema

    linked = build_attributed_tool_definition(tool, resource_uri="ui://support/request")
    assert linked["_meta"]["ui"]["resourceUri"] == "ui://support/request"


def test_start_tool_exposes_only_published_ids_with_model_and_app_visibility() -> None:
    tool = build_start_business_execution_definition(
        [
            {
                "upstream_name": "support_api",
                "definition": {
                    "workflows": [
                        {
                            "id": "support_request",
                            "name": "Handle support request",
                            "description": "Review and create a ticket.",
                        }
                    ],
                    "standalone_tool_values": [],
                },
            }
        ]
    )
    assert tool["name"] == START_BUSINESS_EXECUTION_TOOL
    assert tool["inputSchema"]["oneOf"][0]["properties"]["workflow_id"]["enum"] == [
        "support_api/support_request"
    ]
    assert tool["_meta"]["ui"]["visibility"] == ["model", "app"]
    assert build_start_business_execution_definition([]) is None


def test_unsupported_schema_dialect_fails_validation_but_original_tool_is_untouched() -> None:
    catalog = _catalog()
    definition = _definition(catalog)
    tool = catalog.get_tool("support_api__find_customer")
    tool.input_schema["$schema"] = "https://example.test/unknown-schema"
    next(
        ref for ref in definition.tool_refs if ref.name == tool.qualified_name
    ).schema_sha256 = compute_schema_digest([tool])
    with pytest.raises(ValueError, match="unsupported JSON Schema dialect"):
        validate_definition_against_catalog(definition, catalog)
