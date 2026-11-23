"""Gateway acceptance coverage for attributed multi-tool support workflows."""

from __future__ import annotations

import hashlib
import json
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import httpx
import pytest

from examples.mock_support_workflow import MockSupportState, create_mock_support_workflow_app
from gateway.auth import LocalDevKeyManager
from gateway.config import GatewaySettings
from gateway.integration_validation import ATTRIBUTED_TOOL_PREFIX, START_BUSINESS_EXECUTION_TOOL
from gateway.mcp_app_renderer import MCP_APP_EXTENSION, MCP_APP_MIME_TYPE
from gateway.mcp_protocol import MCP_CLIENT_CAPABILITIES_META, request_headers, request_meta
from gateway.server import create_gateway_app
from gateway.state import SQLiteAuditStore
from gateway.upstream_clients import compute_schema_digest

_TENANT = "tenant-acme-dev"
_UPSTREAM = "support_api"
_WORKFLOW_ID = "support_api/handle_customer_support_request"
_STANDALONE_ID = "support_api/customer_lookup"
_BUSINESS_VALUE_GOLDENS = {
    flow["id"]: flow
    for flow in json.loads(
        (Path(__file__).parent / "golden" / "flows.json").read_text(encoding="utf-8")
    )["business_value_flows"]
}


@dataclass
class SupportGateway:
    app: Any
    client: httpx.AsyncClient
    runtime: Any
    store: SQLiteAuditStore
    support_state: MockSupportState
    token: str


@asynccontextmanager
async def _support_gateway(tmp_path: Path):
    mock_state = MockSupportState()
    mock_app = create_mock_support_workflow_app(mock_state)
    upstream_config = {
        "name": _UPSTREAM,
        "type": "mcp_http",
        "url": "http://support-mock/mcp",
        "auth_type": "none",
        "allowed_roles": ["support_agent"],
        "mutating_tools": ["create_ticket"],
        "tool_effects": {"create_ticket": "write"},
    }
    upstreams_path = tmp_path / "upstreams.json"
    upstreams_path.write_text(json.dumps({"upstreams": [upstream_config]}), encoding="utf-8")
    settings = GatewaySettings(
        auth_mode="dev",
        gateway_mode="traditional",
        upstreams_config_path=str(upstreams_path),
        sqlite_path=str(tmp_path / "gateway.db"),
        provenance_signing_secret="support-workflow-acceptance-signing-secret",
        credential_encryption_key="12" * 32,
    )
    keys = LocalDevKeyManager(issuer=settings.oidc_issuer)
    token = keys.issue_user_token(
        persona="support_agent",
        overrides={
            "roles": ["support_agent", "support_writer", "gateway_admin"],
            "scopes": ["mcp.tools", "mcp.write"],
        },
    )
    upstream_client = httpx.AsyncClient(
        transport=httpx.ASGITransport(app=mock_app), base_url="http://support-mock"
    )
    store = SQLiteAuditStore(settings.sqlite_path)
    app = create_gateway_app(
        settings=settings,
        store=store,
        dev_keys=keys,
        http_client=upstream_client,
    )
    gateway = SupportGateway(
        app=app,
        client=httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://gateway"),
        runtime=app.state.runtime,
        store=store,
        support_state=mock_state,
        token=token,
    )
    async with app.router.lifespan_context(app):
        try:
            await _request(
                gateway.client,
                gateway.token,
                "tools/list",
                request_id="fixture-tools-list",
                mode="traditional",
            )
            await _publish_support_definition(gateway.runtime, store)
            yield gateway
        finally:
            await gateway.client.aclose()
            await upstream_client.aclose()


async def _request(
    client: httpx.AsyncClient,
    token: str,
    method: str,
    *,
    request_id: str | int,
    name: str | None = None,
    arguments: dict[str, Any] | None = None,
    extra_params: dict[str, Any] | None = None,
    mode: str,
    supports_apps: bool = False,
) -> httpx.Response:
    headers = {
        **request_headers(method, tool_name=name),
        "Authorization": f"Bearer {token}",
        "X-Admin-Force-Mode": mode,
    }
    params: dict[str, Any] = {"_meta": request_meta(client_name="support-workflow-acceptance")}
    if supports_apps:
        capabilities = params["_meta"][MCP_CLIENT_CAPABILITIES_META]
        capabilities["extensions"] = {MCP_APP_EXTENSION: {"mimeTypes": [MCP_APP_MIME_TYPE]}}
    if name is not None:
        params.update({"name": name, "arguments": arguments or {}})
    params.update(extra_params or {})
    return await client.post(
        "/mcp",
        headers=headers,
        json={"jsonrpc": "2.0", "id": request_id, "method": method, "params": params},
    )


async def _publish_support_definition(runtime: Any, store: SQLiteAuditStore) -> None:
    await runtime.ensure_catalog_for_identity(runtime._dev_discovery_identity())
    tools = runtime.catalog.get_server_tools(_UPSTREAM)
    tool_names = [tool.qualified_name for tool in tools]
    refs = [
        {"name": tool.qualified_name, "schema_sha256": compute_schema_digest([tool])}
        for tool in tools
    ]
    tenant_id = runtime.settings.allowed_tenants[0]
    definition: dict[str, Any] = {
        "manifest_version": "1.0",
        "integration": {
            "id": "support_desk",
            "display_name": "Support Desk",
            "upstream_name": _UPSTREAM,
            "status": "published",
            "revision": 1,
        },
        "tool_refs": refs,
        "experiences": [],
        "workflows": [
            {
                "id": "handle_customer_support_request",
                "name": "Handle a customer support request",
                "description": "Review customer, account and order history, then create a ticket.",
                "participating_tools": tool_names,
                "required_evidence": [
                    {
                        "tool": f"{_UPSTREAM}__find_customer",
                        "min_successful_calls": 1,
                        "result_when": {"path": "/customer/id", "type": "non_empty_string"},
                    },
                    {
                        "tool": f"{_UPSTREAM}__get_account",
                        "min_successful_calls": 1,
                        "result_when": {"path": "/account/id", "type": "non_empty_string"},
                    },
                    {
                        "tool": f"{_UPSTREAM}__get_order_history",
                        "min_successful_calls": 1,
                        "result_when": {"path": "/orders", "type": "array"},
                    },
                ],
                "completion": {
                    "kind": "terminal_tool_result",
                    "tool": f"{_UPSTREAM}__create_ticket",
                    "success_when": {"path": "/ticket/id", "type": "non_empty_string"},
                    "require_sensitive_write_journal_status": "succeeded",
                },
                "dedupe": {
                    "namespace": "support.create_ticket.v1",
                    "identity_parts": [{"source": "terminal_result", "path": "/ticket/id"}],
                },
                "business_value": {
                    "manual_baseline_minutes": 5,
                    "baseline_source": "administrator_estimate",
                    "confidence": "unvalidated",
                    "loaded_labor_cost_per_hour": 25,
                    "currency": "EUR",
                    "effective_from": "2026-11-22",
                },
            }
        ],
        "standalone_tool_values": [
            {
                "id": "customer_lookup",
                "name": "Look up a customer",
                "description": "Find one customer by reference.",
                "tool": f"{_UPSTREAM}__find_customer",
                "completion": {
                    "kind": "terminal_tool_result",
                    "tool": f"{_UPSTREAM}__find_customer",
                    "success_when": {"path": "/customer/id", "type": "non_empty_string"},
                },
                "business_value": {
                    "manual_baseline_minutes": 1,
                    "baseline_source": "administrator_estimate",
                    "confidence": "unvalidated",
                    "effective_from": "2026-11-22",
                },
            }
        ],
        "publication": {
            "validator_version": "support-workflow-acceptance-1",
            "content_sha256": "0" * 64,
            "published_by": "support-workflow-test-admin",
            "published_at": datetime.now(UTC).isoformat(),
        },
    }
    canonical = json.dumps(definition, sort_keys=True, separators=(",", ":")).encode()
    definition["publication"]["content_sha256"] = hashlib.sha256(canonical).hexdigest()
    canonical = json.dumps(definition, sort_keys=True, separators=(",", ":")).encode()
    digest = hashlib.sha256(canonical).hexdigest()
    saved = await store.save_integration_definition_version(
        {
            "tenant_id": tenant_id,
            "upstream_name": _UPSTREAM,
            "definition_digest": digest,
            "definition": definition,
        }
    )
    await store.append_integration_definition_publication(
        {
            "event_id": "support-workflow-acceptance-publication",
            "tenant_id": tenant_id,
            "upstream_name": _UPSTREAM,
            "action": "publish",
            "revision": saved["revision"],
            "actor_sub": "support-workflow-test-admin",
            "created_at": datetime.now(UTC).isoformat(),
        }
    )


async def _publish_support_experience(gateway: SupportGateway) -> None:
    current = await gateway.store.get_current_published_integration_definition(_TENANT, _UPSTREAM)
    assert current is not None
    definition = json.loads(json.dumps(current["definition"]))
    definition["integration"]["revision"] = 2
    definition["experiences"] = [
        {
            "id": "customer_lookup_view",
            "resource_uri": "ui://mcpcodeaway/support/customer-lookup/r2",
            "renderer_schema": "mcpcodeaway-ui/1",
            "tool": f"{_UPSTREAM}__find_customer",
            "fallback": "Use the customer lookup tool to find a customer.",
            "input_mapping": {"customer_reference": "/form/customer_reference"},
            "components": [
                {"type": "heading", "text": "Find a customer"},
                {
                    "type": "text_input",
                    "id": "customer_reference",
                    "label": "Customer reference",
                    "required": True,
                    "max_length": 64,
                },
                {"type": "button", "label": "Search", "action": "find_customer"},
                {"type": "record", "data": "tool_result"},
            ],
            "csp": {"connect_domains": [], "resource_domains": [], "frame_domains": []},
        }
    ]
    encoded = json.dumps(definition, sort_keys=True, separators=(",", ":")).encode()
    definition["publication"]["content_sha256"] = hashlib.sha256(encoded).hexdigest()
    digest = hashlib.sha256(
        json.dumps(definition, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    saved = await gateway.store.save_integration_definition_version(
        {
            "tenant_id": _TENANT,
            "upstream_name": _UPSTREAM,
            "revision": "2",
            "definition_digest": digest,
            "definition": definition,
        }
    )
    await gateway.store.append_integration_definition_publication(
        {
            "event_id": "support-app-resource-publication",
            "tenant_id": _TENANT,
            "upstream_name": _UPSTREAM,
            "action": "publish",
            "revision": saved["revision"],
            "actor_sub": "support-workflow-test-admin",
            "created_at": datetime.now(UTC).isoformat(),
        }
    )


async def _start_workflow(gateway: SupportGateway, request_id: str | int) -> str:
    response = await _request(
        gateway.client,
        gateway.token,
        "tools/call",
        request_id=request_id,
        name=START_BUSINESS_EXECUTION_TOOL,
        arguments={"kind": "workflow", "workflow_id": _WORKFLOW_ID},
        mode="traditional",
    )
    assert response.status_code == 200, response.text
    started = response.json()["result"]["structuredContent"]
    assert started["ok"] is True
    return started["business_execution_id"]


async def _call_attributed(
    gateway: SupportGateway,
    execution_id: str,
    tool_name: str,
    arguments: dict[str, Any],
    *,
    request_id: str | int,
    mode: str,
) -> dict[str, Any]:
    response = await _request(
        gateway.client,
        gateway.token,
        "tools/call",
        request_id=request_id,
        name=f"{ATTRIBUTED_TOOL_PREFIX}{_UPSTREAM}__{tool_name}",
        arguments={"business_execution_id": execution_id, "arguments": arguments},
        mode=mode,
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert "error" not in body, body
    return body["result"]["structuredContent"]


async def _workflow_events(gateway: SupportGateway, execution_id: str) -> list[dict[str, Any]]:
    events = await gateway.store.list_business_value_events(_TENANT)
    return [event for event in events if event["business_execution_id"] == execution_id]


@pytest.mark.asyncio
async def test_support_workflow_tools_list_and_direct_wrappers_complete_with_value_credit(
    tmp_path: Path,
) -> None:
    async with _support_gateway(tmp_path) as gateway:
        listing = await _request(
            gateway.client,
            gateway.token,
            "tools/list",
            request_id="list-hybrid",
            mode="hybrid",
            supports_apps=True,
        )
        assert listing.status_code == 200
        tools = {tool["name"]: tool for tool in listing.json()["result"]["tools"]}
        assert START_BUSINESS_EXECUTION_TOOL not in tools
        assert not any(name.startswith(ATTRIBUTED_TOOL_PREFIX) for name in tools)
        assert f"{_UPSTREAM}__create_ticket" in tools, sorted(tools)

        execution_id = await _start_workflow(gateway, "start-direct-success")
        await _call_attributed(
            gateway,
            execution_id,
            "find_customer",
            {"customer_reference": "CUST-1001"},
            request_id="direct-find-customer",
            mode="hybrid",
        )
        await _call_attributed(
            gateway,
            execution_id,
            "get_account",
            {"customer_id": "CUST-1001"},
            request_id="direct-get-account",
            mode="hybrid",
        )
        await _call_attributed(
            gateway,
            execution_id,
            "get_order_history",
            {"customer_id": "CUST-1001"},
            request_id="direct-get-orders",
            mode="hybrid",
        )
        terminal = await _call_attributed(
            gateway,
            execution_id,
            "create_ticket",
            {"customer_id": "CUST-1001", "summary": "Late delivery"},
            request_id="direct-create-ticket",
            mode="hybrid",
        )

        assert terminal["ok"] is True
        assert terminal["result"]["ticket"]["id"] == "TKT-5001"
        assert terminal["completion_recorded"] is True
        calls = await gateway.store.list_workflow_calls(_TENANT, execution_id)
        assert {call["tool_qualified_name"] for call in calls} == {
            f"{_UPSTREAM}__find_customer",
            f"{_UPSTREAM}__get_account",
            f"{_UPSTREAM}__get_order_history",
            f"{_UPSTREAM}__create_ticket",
        }
        assert all(call["status"] == "succeeded" for call in calls)
        events = await _workflow_events(gateway, execution_id)
        golden = _BUSINESS_VALUE_GOLDENS["support_workflow_requires_all_lookups_and_ticket"]
        assert len(events) == golden["expected_credited_completions"]
        assert {call["tool_qualified_name"] for call in calls} >= set(
            golden["expected_required_tools"]
        )
        assert terminal["completion_recorded"] is True
        assert (
            events[0]["evidence"]["baseline_snapshot"]["manual_baseline_minutes"]
            == golden["expected_manual_baseline_minutes"]
        )


@pytest.mark.asyncio
async def test_support_workflow_missing_required_order_lookup_does_not_credit(
    tmp_path: Path,
) -> None:
    async with _support_gateway(tmp_path) as gateway:
        execution_id = await _start_workflow(gateway, "start-direct-missing-evidence")
        for tool_name, arguments, request_id in (
            ("find_customer", {"customer_reference": "CUST-1001"}, "missing-find"),
            ("get_account", {"customer_id": "CUST-1001"}, "missing-account"),
        ):
            await _call_attributed(
                gateway,
                execution_id,
                tool_name,
                arguments,
                request_id=request_id,
                mode="traditional",
            )
        terminal = await _call_attributed(
            gateway,
            execution_id,
            "create_ticket",
            {"customer_id": "CUST-1001", "summary": "Late delivery"},
            request_id="missing-create-ticket",
            mode="traditional",
        )

        assert terminal["ok"] is True
        golden = _BUSINESS_VALUE_GOLDENS[
            "support_workflow_ticket_without_order_lookup_is_incomplete"
        ]
        assert terminal["completion_recorded"] is golden["expected_completion_recorded"]
        assert not await _workflow_events(gateway, execution_id)
        assert {event["tool"] for event in gateway.support_state.events} == {
            "find_customer",
            "get_account",
            "create_ticket",
        }
        assert golden["missing_required_tool"].split("__", maxsplit=1)[1] not in {
            event["tool"] for event in gateway.support_state.events
        }


@pytest.mark.asyncio
async def test_support_workflow_code_mode_attributes_multiple_callbacks_and_completes(
    tmp_path: Path,
) -> None:
    async with _support_gateway(tmp_path) as gateway:
        execution_id = await _start_workflow(gateway, "start-code-mode")
        code = (
            "customer = await support_api__find_customer(customer_reference='CUST-1002')\n"
            "account = await support_api__get_account(customer_id=customer['customer']['id'])\n"
            "orders = await support_api__get_order_history("
            "customer_id=customer['customer']['id'])\n"
            "ticket = await support_api__create_ticket("
            "customer_id=customer['customer']['id'], summary='Returned order')\n"
            "{'account_id': account['account']['id'], 'order_count': len(orders['orders']), "
            "'ticket_id': ticket['ticket']['id']}"
        )
        response = await _request(
            gateway.client,
            gateway.token,
            "tools/call",
            request_id="code-mode-support-request",
            name="execute_code",
            arguments={"code": code, "business_execution_id": execution_id},
            mode="codemode_upfront",
        )

        assert response.status_code == 200, response.text
        run = response.json()["result"]["structuredContent"]
        assert run["ok"] is True, run
        assert run["result"] == {
            "account_id": "ACCT-1002",
            "order_count": 1,
            "ticket_id": "TKT-5001",
        }
        assert run["business_value_completion"]["credited"] == 1
        calls = await gateway.store.list_workflow_calls(_TENANT, execution_id)
        golden = _BUSINESS_VALUE_GOLDENS["support_workflow_code_mode_tracks_each_callback"]
        assert {call["callback_ordinal"] for call in calls} == set(
            golden["expected_callback_ordinals"]
        )
        assert len(calls) == 4
        assert (
            len(await _workflow_events(gateway, execution_id))
            == golden["expected_credited_completions"]
        )


@pytest.mark.asyncio
async def test_standalone_execution_rejects_reused_outer_request_id_with_changed_body(
    tmp_path: Path,
) -> None:
    async with _support_gateway(tmp_path) as gateway:
        start = await _request(
            gateway.client,
            gateway.token,
            "tools/call",
            request_id="start-standalone",
            name=START_BUSINESS_EXECUTION_TOOL,
            arguments={"kind": "standalone_tool", "tool_value_id": _STANDALONE_ID},
            mode="traditional",
        )
        execution_id = start.json()["result"]["structuredContent"]["business_execution_id"]
        first = await _request(
            gateway.client,
            gateway.token,
            "tools/call",
            request_id="standalone-same-outer-id",
            name=f"{ATTRIBUTED_TOOL_PREFIX}{_UPSTREAM}__find_customer",
            arguments={
                "business_execution_id": execution_id,
                "arguments": {"customer_reference": "CUST-1001"},
            },
            mode="traditional",
        )
        assert first.status_code == 200
        changed = await _request(
            gateway.client,
            gateway.token,
            "tools/call",
            request_id="standalone-same-outer-id",
            name=f"{ATTRIBUTED_TOOL_PREFIX}{_UPSTREAM}__find_customer",
            arguments={
                "business_execution_id": execution_id,
                "arguments": {"customer_reference": "CUST-1002"},
            },
            mode="traditional",
        )

        assert changed.status_code == 200
        assert "error" in changed.json()
        second_invocation = await _request(
            gateway.client,
            gateway.token,
            "tools/call",
            request_id="standalone-distinct-outer-id",
            name=f"{ATTRIBUTED_TOOL_PREFIX}{_UPSTREAM}__find_customer",
            arguments={
                "business_execution_id": execution_id,
                "arguments": {"customer_reference": "CUST-1002"},
            },
            mode="traditional",
        )
        assert second_invocation.status_code == 200
        golden = _BUSINESS_VALUE_GOLDENS["standalone_value_rejects_second_logical_invocation"]
        assert "error" in second_invocation.json()
        assert len(gateway.support_state.events) == golden["expected_upstream_calls"]
        assert [event["tool"] for event in gateway.support_state.events] == ["find_customer"]


@pytest.mark.asyncio
async def test_mcp_apps_negotiates_and_reads_constrained_ui_resource(tmp_path: Path) -> None:
    async with _support_gateway(tmp_path) as gateway:
        await _publish_support_experience(gateway)
        discovery = await _request(
            gateway.client,
            gateway.token,
            "server/discover",
            request_id="apps-discover",
            mode="traditional",
            supports_apps=True,
        )
        assert discovery.status_code == 200
        advertised = discovery.json()["result"]["capabilities"]["extensions"]
        assert advertised[MCP_APP_EXTENSION]["mimeTypes"] == [MCP_APP_MIME_TYPE]

        listing = await _request(
            gateway.client,
            gateway.token,
            "tools/list",
            request_id="apps-tools-list",
            mode="traditional",
            supports_apps=True,
        )
        tools = {tool["name"]: tool for tool in listing.json()["result"]["tools"]}
        direct_tool = tools[f"{_UPSTREAM}__find_customer"]
        assert direct_tool["_meta"]["ui"]["visibility"] == ["model", "app"]
        assert direct_tool["_meta"]["ui"]["resourceUri"] == (
            "ui://mcpcodeaway/support/customer-lookup/r2"
        )

        resources = await _request(
            gateway.client,
            gateway.token,
            "resources/list",
            request_id="apps-resource-list",
            mode="traditional",
            supports_apps=True,
        )
        listed_resources = resources.json()["result"]["resources"]
        assert listed_resources == [
            {
                "uri": "ui://mcpcodeaway/support/customer-lookup/r2",
                "name": "customer_lookup_view",
                "description": "Use the customer lookup tool to find a customer.",
                "mimeType": MCP_APP_MIME_TYPE,
            }
        ]

        read = await _request(
            gateway.client,
            gateway.token,
            "resources/read",
            request_id="apps-resource-read",
            mode="traditional",
            supports_apps=True,
            extra_params={"uri": listed_resources[0]["uri"]},
        )
        content = read.json()["result"]["contents"][0]
        assert content["uri"] == listed_resources[0]["uri"]
        assert content["mimeType"] == MCP_APP_MIME_TYPE
        assert "ui/notifications/tool-input" in content["text"]
        assert "tools/call" in content["text"]
        assert content["_meta"]["ui"]["permissions"] == {}
        assert content["_meta"]["ui"]["csp"]["connectDomains"] == []


@pytest.mark.asyncio
async def test_mcp_apps_metadata_and_resources_are_absent_without_negotiation(
    tmp_path: Path,
) -> None:
    async with _support_gateway(tmp_path) as gateway:
        await _publish_support_experience(gateway)
        listing = await _request(
            gateway.client,
            gateway.token,
            "tools/list",
            request_id="no-app-tools-list",
            mode="traditional",
        )
        tools = {tool["name"]: tool for tool in listing.json()["result"]["tools"]}
        assert "_meta" not in tools[f"{_UPSTREAM}__find_customer"]

        discovery = await _request(
            gateway.client,
            gateway.token,
            "server/discover",
            request_id="no-app-discover",
            mode="traditional",
        )
        assert MCP_APP_EXTENSION not in discovery.json()["result"]["capabilities"]["extensions"]

        response = await _request(
            gateway.client,
            gateway.token,
            "resources/list",
            request_id="no-app-resource-list",
            mode="traditional",
        )
        assert response.json()["error"]["code"] == -32021
