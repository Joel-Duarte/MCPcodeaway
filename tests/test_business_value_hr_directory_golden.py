"""Golden business workflow using fictional employees in the local HR mock API."""

from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import httpx
import pytest

from examples.mock_backends import create_mock_backends_app
from gateway.auth import LocalDevKeyManager
from gateway.config import GatewaySettings
from gateway.mcp_protocol import request_headers, request_meta
from gateway.server import create_gateway_app
from gateway.state import SQLiteAuditStore
from gateway.upstream_clients import compute_schema_digest

_UPSTREAM = "hr_directory"
_TENANT = "tenant-acme-dev"
_WORKFLOW_ID = "hr_directory/update_fictional_employee_profile"
_GOLDEN_PATH = Path(__file__).parent / "golden" / "flows.json"
_GOLDEN = next(
    flow
    for flow in json.loads(_GOLDEN_PATH.read_text(encoding="utf-8"))["business_value_flows"]
    if flow["id"] == "hr_fictional_user_update_verified_by_readback"
)


async def _request(
    client: httpx.AsyncClient,
    token: str,
    *,
    request_id: str,
    name: str,
    arguments: dict[str, Any],
    intent: str | None = None,
    session_id: str | None = None,
) -> dict[str, Any]:
    headers = {
        "Authorization": f"Bearer {token}",
        **request_headers("tools/call", tool_name=name),
    }
    if intent:
        headers["X-Intent"] = intent
    if session_id:
        headers["Mcp-Session-Id"] = session_id
    response = await client.post(
        "/mcp",
        headers=headers,
        json={
            "jsonrpc": "2.0",
            "id": request_id,
            "method": "tools/call",
            "params": {"_meta": request_meta(), "name": name, "arguments": arguments},
        },
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert "error" not in body, body
    structured = body["result"]["structuredContent"]
    return structured


async def _publish_hr_workflow(runtime: Any, store: SQLiteAuditStore) -> None:
    await runtime.ensure_catalog_for_identity(runtime._dev_discovery_identity())
    tools = runtime.catalog.get_server_tools(_UPSTREAM)
    references = [
        {"name": tool.qualified_name, "schema_sha256": compute_schema_digest([tool])}
        for tool in tools
        if tool.name in {"list_hr_employees", "get_hr_employee", "update_hr_employee"}
    ]
    names = {tool.name: tool.qualified_name for tool in tools}
    definition: dict[str, Any] = {
        "manifest_version": "1.0",
        "integration": {
            "id": _UPSTREAM,
            "display_name": "Fictional HR Directory",
            "upstream_name": _UPSTREAM,
            "status": "published",
            "revision": 1,
        },
        "tool_refs": references,
        "experiences": [],
        "workflows": [
            {
                "id": "update_fictional_employee_profile",
                "name": "Update and verify a fictional employee profile",
                "description": (
                    "Find a fictional employee, review the current profile, update their role, "
                    "then read the profile back to verify the saved change."
                ),
                "participating_tools": [
                    names["list_hr_employees"],
                    names["get_hr_employee"],
                    names["update_hr_employee"],
                ],
                "required_evidence": [
                    {
                        "tool": names["list_hr_employees"],
                        "result_when": {"path": "/items", "type": "non_empty_array"},
                    },
                    {
                        "tool": names["get_hr_employee"],
                        "result_when": {"path": "/employee_id", "type": "non_empty_string"},
                    },
                    {
                        "tool": names["update_hr_employee"],
                        "result_when": {
                            "path": "/role",
                            "type": "string",
                            "equals": _GOLDEN["updated_role"],
                        },
                    },
                ],
                "completion": {
                    "kind": "terminal_tool_result",
                    "tool": names["get_hr_employee"],
                    "success_when": {
                        "path": "/version",
                        "type": "integer",
                        "equals": _GOLDEN["expected_final_version"],
                    },
                },
                "business_value": {
                    "manual_baseline_minutes": _GOLDEN["expected_manual_baseline_minutes"],
                    "baseline_source": "administrator_estimate",
                    "confidence": "unvalidated",
                    "effective_from": "2026-11-22",
                    "evidence_note": (
                        "Golden fixture baseline for the fictional HR profile update flow."
                    ),
                },
            }
        ],
        "standalone_tool_values": [],
        "publication": {
            "validator_version": "hr-directory-golden-1",
            "content_sha256": "0" * 64,
            "published_by": "golden-hr-workflow-admin",
            "published_at": datetime.now(UTC).isoformat(),
        },
    }
    publication_body = json.dumps(definition, sort_keys=True, separators=(",", ":")).encode()
    definition["publication"]["content_sha256"] = hashlib.sha256(publication_body).hexdigest()
    digest_body = json.dumps(definition, sort_keys=True, separators=(",", ":")).encode()
    digest = hashlib.sha256(digest_body).hexdigest()
    tenant_id = runtime.settings.allowed_tenants[0]
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
            "event_id": "hr-directory-golden-publication",
            "tenant_id": tenant_id,
            "upstream_name": _UPSTREAM,
            "action": "publish",
            "revision": saved["revision"],
            "actor_sub": "golden-hr-workflow-admin",
            "created_at": datetime.now(UTC).isoformat(),
        }
    )


@pytest.mark.asyncio
async def test_fictional_employee_update_is_verified_by_terminal_readback(tmp_path: Path) -> None:
    mock_app = create_mock_backends_app()
    upstream_client = httpx.AsyncClient(
        transport=httpx.ASGITransport(app=mock_app), base_url="http://mock-backends"
    )
    upstream_config = {
        "upstreams": [
            {
                "name": _UPSTREAM,
                "type": "openapi",
                "url": "http://mock-backends/hr",
                "openapi_spec_url": "http://mock-backends/hr/openapi.json",
                "auth_type": "none",
                "allowed_roles": ["hr_ops"],
                "allowed_operations": [
                    "list_hr_employees",
                    "get_hr_employee",
                    "update_hr_employee",
                ],
                "tool_effects": {
                    "list_hr_employees": "read",
                    "get_hr_employee": "read",
                    "update_hr_employee": "write",
                },
                "mutating_tools": ["update_hr_employee"],
            }
        ]
    }
    upstreams_path = tmp_path / "hr-upstreams.json"
    upstreams_path.write_text(json.dumps(upstream_config), encoding="utf-8")
    settings = GatewaySettings(
        app_env="sandbox",
        auth_mode="dev",
        profile="demo",
        upstreams_config_path=str(upstreams_path),
        sqlite_path=str(tmp_path / "hr-gateway.db"),
        provenance_signing_secret="fictional-hr-workflow-golden-signing-key",
    )
    keys = LocalDevKeyManager(issuer=settings.oidc_issuer)
    store = SQLiteAuditStore(settings.sqlite_path)
    app = create_gateway_app(
        settings=settings,
        store=store,
        dev_keys=keys,
        http_client=upstream_client,
    )
    token = keys.issue_user_token(
        persona="finance_analyst",
        overrides={
            "sub": "golden-hr-operator",
            "roles": [_GOLDEN["persona"], "hr_writer"],
            "groups": ["hr"],
            "scopes": ["mcp.tools", "mcp.write"],
        },
    )

    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://gateway"
        ) as gateway:
            tool_listing = await gateway.post(
                "/mcp",
                headers={"Authorization": f"Bearer {token}", **request_headers("tools/list")},
                json={
                    "jsonrpc": "2.0",
                    "id": "hr-list-tools",
                    "method": "tools/list",
                    "params": {"_meta": request_meta()},
                },
            )
            assert tool_listing.status_code == 200, tool_listing.text
            listed_names = {tool["name"] for tool in tool_listing.json()["result"]["tools"]}
            assert {
                "hr_directory__list_hr_employees",
                "hr_directory__get_hr_employee",
                "hr_directory__update_hr_employee",
            } <= listed_names, sorted(listed_names)
            await _publish_hr_workflow(app.state.runtime, store)
            refreshed_listing = await gateway.post(
                "/mcp",
                headers={"Authorization": f"Bearer {token}", **request_headers("tools/list")},
                json={
                    "jsonrpc": "2.0",
                    "id": "hr-list-tools-after-publish",
                    "method": "tools/list",
                    "params": {"_meta": request_meta()},
                },
            )
            refreshed_names = {tool["name"] for tool in refreshed_listing.json()["result"]["tools"]}
            assert "mcpcodeaway_start_business_execution" not in refreshed_names
            assert not any(name.startswith("mca_attr__") for name in refreshed_names)

            intent = "Update fictional employee profile role and verify saved profile"
            listing_resp = await _request(
                gateway,
                token,
                request_id="hr-list-employees",
                name=f"{_UPSTREAM}__list_hr_employees",
                arguments={"limit": 10},
                intent=intent,
            )
            before_resp = await _request(
                gateway,
                token,
                request_id="hr-read-before-update",
                name=f"{_UPSTREAM}__get_hr_employee",
                arguments={"employee_id": _GOLDEN["employee_id"]},
                intent=intent,
            )
            updated_resp = await _request(
                gateway,
                token,
                request_id="hr-update-role",
                name=f"{_UPSTREAM}__update_hr_employee",
                arguments={
                    "employee_id": _GOLDEN["employee_id"],
                    "role": _GOLDEN["updated_role"],
                },
                intent=intent,
            )
            verified_resp = await _request(
                gateway,
                token,
                request_id="hr-read-after-update",
                name=f"{_UPSTREAM}__get_hr_employee",
                arguments={"employee_id": _GOLDEN["employee_id"]},
                intent=intent,
            )

            listing = listing_resp.get("result", listing_resp)
            before = before_resp.get("result", before_resp)
            updated = updated_resp.get("result", updated_resp)
            verified = verified_resp.get("result", verified_resp)
            assert listing["count"] >= 3
            assert before["name"] == _GOLDEN["expected_name"]
            assert before["role"] == _GOLDEN["expected_initial_role"]
            assert updated["role"] == _GOLDEN["updated_role"]
            assert verified["role"] == _GOLDEN["updated_role"]
            assert verified["version"] == _GOLDEN["expected_final_version"]
            assert verified_resp["workflow_detection"]["status"] == "completed_verified"
            episode_id = verified_resp["workflow_detection"]["episode_id"]

            events = await store.list_business_value_events(_TENANT)
            credited = [event for event in events if event["business_execution_id"] == episode_id]
            assert len(credited) == _GOLDEN["expected_credited_completions"]
            assert credited[0]["evidence"]["attribution_mode"] == "passive_matched"
            assert credited[0]["evidence"]["selected_id"] == _WORKFLOW_ID
            assert (
                credited[0]["evidence"]["baseline_snapshot"]["manual_baseline_minutes"]
                == _GOLDEN["expected_manual_baseline_minutes"]
            )

    await upstream_client.aclose()
