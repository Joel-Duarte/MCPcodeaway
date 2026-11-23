"""Constrained MCP Apps renderer and product-owned host bridge tests."""

from __future__ import annotations

import pytest

from gateway.mcp_app_renderer import (
    MCP_APP_MIME_TYPE,
    describe_mcp_app_resource,
    render_mcp_app_resource,
    validate_experience,
)


def _experience() -> dict:
    return {
        "id": "order_detail",
        "resource_uri": "ui://mcpcodeaway/orders/order-detail/r2",
        "renderer_schema": "mcpcodeaway-ui/1",
        "tool": "orders_api__get_order",
        "fallback": "Use the order lookup tool to read order details.",
        "input_mapping": {"order_id": "/form/order_id"},
        "components": [
            {"type": "heading", "text": "Order lookup"},
            {
                "type": "text_input",
                "id": "order_id",
                "label": "Order ID",
                "required": True,
                "max_length": 64,
            },
            {"type": "button", "label": "Look up order", "action": "invoke_tool"},
            {"type": "record", "data": "tool_result"},
        ],
        "csp": {"connect_domains": [], "resource_domains": [], "frame_domains": []},
    }


def _input_schema() -> dict:
    return {
        "type": "object",
        "properties": {"order_id": {"type": "string", "maxLength": 64}},
        "required": ["order_id"],
        "additionalProperties": False,
    }


def test_renderer_emits_safe_mcp_app_resource_with_product_host_bridge() -> None:
    resource = render_mcp_app_resource(_experience(), tool_input_schema=_input_schema())
    descriptor = describe_mcp_app_resource(_experience(), tool_input_schema=_input_schema())

    assert resource["uri"] == descriptor["uri"] == "ui://mcpcodeaway/orders/order-detail/r2"
    assert resource["mimeType"] == descriptor["mimeType"] == MCP_APP_MIME_TYPE
    assert resource["_meta"]["ui"]["csp"] == {
        "connectDomains": [],
        "resourceDomains": [],
        "frameDomains": [],
        "baseUriDomains": [],
    }
    assert resource["_meta"]["ui"]["permissions"] == {}
    body = resource["text"]
    assert body.startswith("<!doctype html>")
    assert "window.parent.postMessage" in body
    assert "ui/initialize" in body
    assert "ui/notifications/tool-input" in body
    assert "tools/call" in body
    assert "orders_api__get_order" in body
    assert "mca_attr__" not in body
    assert "textContent" in body
    assert "onload=" not in body.lower()


def test_plain_text_is_escaped_and_actions_are_not_inert() -> None:
    spec = _experience()
    spec["components"][0]["text"] = "Order & customer"
    resource = render_mcp_app_resource(spec, tool_input_schema=_input_schema())
    assert "Order &amp; customer" in resource["text"]

    no_action = {**spec, "components": [{"type": "text_input", "id": "order_id", "label": "Order"}]}
    with pytest.raises(ValueError, match="inert controls"):
        validate_experience(no_action)


@pytest.mark.parametrize(
    ("mutate", "message"),
    [
        (
            lambda value: value["components"].__setitem__(
                0, {"type": "text", "text": "<script>alert(1)</script>"}
            ),
            "HTML, JavaScript",
        ),
        (
            lambda value: value["components"][0].update({"onclick": "alert(1)"}),
            "unsupported node or property",
        ),
        (
            lambda value: value["components"][0].update(
                {"text": "Open https://attacker.example/resource"}
            ),
            "HTML, JavaScript",
        ),
        (
            lambda value: value.update({"resource_uri": "https://attacker.example/app"}),
            "local ui:// URI",
        ),
        (
            lambda value: value["csp"].update({"resource_domains": ["https://cdn.example"]}),
            "domains must be empty",
        ),
        (
            lambda value: value["input_mapping"].update({"password": "/form/order_id"}),
            "secret-like field",
        ),
    ],
)
def test_renderer_rejects_active_markup_events_remote_origins_and_secret_fields(
    mutate, message: str
) -> None:
    spec = _experience()
    mutate(spec)
    schema = {
        "type": "object",
        "properties": {
            "order_id": {"type": "string"},
            "password": {"type": "string", "writeOnly": True, "format": "password"},
        },
        "required": ["order_id"],
    }
    with pytest.raises(ValueError, match=message):
        validate_experience(spec, tool_input_schema=schema)


def test_renderer_rejects_unknown_style_fields_and_oversized_specs() -> None:
    styled = _experience()
    styled["components"][0]["style"] = "color:red"
    with pytest.raises(ValueError, match="unsupported node or property"):
        validate_experience(styled, tool_input_schema=_input_schema())

    oversized = _experience()
    oversized["components"][0]["text"] = "x" * 2100
    with pytest.raises(ValueError, match="text is invalid or too long"):
        validate_experience(oversized, tool_input_schema=_input_schema())

    too_many = _experience()
    too_many["components"] = [{"type": "text", "text": "x"}] * 41
    with pytest.raises(ValueError, match="at most 40"):
        validate_experience(too_many)


def test_interactive_experience_requires_schema_and_complete_required_mapping() -> None:
    with pytest.raises(ValueError, match="require the catalog tool input schema"):
        validate_experience(_experience())

    spec = _experience()
    spec["input_mapping"] = {}
    schema = {
        "type": "object",
        "properties": {"order_id": {"type": "string"}, "region": {"type": "string"}},
        "required": ["order_id", "region"],
    }
    with pytest.raises(ValueError, match="every required tool field"):
        validate_experience(spec, tool_input_schema=schema)

    spec = _experience()
    schema["properties"]["order_id"] = {"type": "integer"}
    with pytest.raises(ValueError, match="declared string property"):
        validate_experience(spec, tool_input_schema=schema)


def test_renderer_styles_structured_dom_and_select_component() -> None:
    spec = {
        "id": "ticket_filter",
        "resource_uri": "ui://mcpcodeaway/global_ticketing/filter/r1",
        "renderer_schema": "mcpcodeaway-ui/1",
        "tool": "global_ticketing__list_tickets",
        "fallback": "List tickets filtered by region.",
        "input_mapping": {"region": "/form/region"},
        "components": [
            {"type": "heading", "text": "Regional Queue"},
            {"type": "text", "text": "Pick a region to filter open tickets."},
            {
                "type": "select",
                "id": "region",
                "label": "Region",
                "required": True,
                "options": [
                    {"value": "EU", "label": "Europe (EU)"},
                    {"value": "US", "label": "United States (US)"},
                ],
            },
            {"type": "button", "label": "Load Queue", "action": "invoke_tool"},
            {"type": "record", "data": "tool_result"},
        ],
        "csp": {"connect_domains": [], "resource_domains": [], "frame_domains": []},
    }
    schema = {
        "type": "object",
        "properties": {"region": {"type": "string"}},
        "required": ["region"],
        "additionalProperties": False,
    }
    resource = render_mcp_app_resource(spec, tool_input_schema=schema)
    body = resource["text"]
    assert "<style>" in body
    assert ".mca-card" in body
    assert "mca-kv" in body
    assert "mca-table" in body
    assert '<select class="mca-select" id="region" name="region" required>' in body
    assert '<option value="EU">Europe (EU)</option>' in body
    assert "innerHTML" not in body

    bad_select = {
        **spec,
        "components": [
            spec["components"][0],
            {
                "type": "select",
                "id": "region",
                "label": "Region",
                "options": [{"value": "<script>", "label": "Bad"}],
            },
            spec["components"][3],
        ],
    }
    with pytest.raises(ValueError, match="HTML, JavaScript"):
        validate_experience(bad_select, tool_input_schema=schema)
