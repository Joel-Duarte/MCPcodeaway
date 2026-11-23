"""Golden E2E onboarding, multi-upstream detection X/Y/Z, and MCP Apps UI delivery."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import httpx
import pytest

from examples.mock_backends import create_mock_backends_app
from examples.mock_support_workflow import create_mock_support_workflow_app
from gateway.auth import LocalDevKeyManager
from gateway.config import GatewaySettings
from gateway.mcp_app_renderer import MCP_APP_EXTENSION, MCP_APP_MIME_TYPE
from gateway.mcp_protocol import MCP_CLIENT_CAPABILITIES_META, request_headers, request_meta
from gateway.server import create_gateway_app
from gateway.state import SQLiteAuditStore

_GOLDEN_PATH = Path(__file__).parent / "golden" / "flows.json"
_GOLDEN_FLOWS = {
    flow["id"]: flow
    for flow in json.loads(_GOLDEN_PATH.read_text(encoding="utf-8"))[
        "auto_workflow_detection_flows"
    ]
}


async def _mcp_rpc(
    client: httpx.AsyncClient,
    token: str,
    *,
    req_id: str,
    method: str,
    params: dict[str, Any] | None = None,
    intent: str | None = None,
    workflow_run_id: str | None = None,
    mode: str = "traditional",
    supports_apps: bool = False,
) -> dict[str, Any]:
    meta = request_meta(client_name="regional-ui-client" if supports_apps else "cli-agent")
    meta[MCP_CLIENT_CAPABILITIES_META]["elicitation"] = {"form": {}}
    if supports_apps:
        meta[MCP_CLIENT_CAPABILITIES_META]["extensions"] = {
            MCP_APP_EXTENSION: {"mimeTypes": [MCP_APP_MIME_TYPE]}
        }
    tool_name = (params or {}).get("name")
    headers = {
        "Authorization": f"Bearer {token}",
        **request_headers(method, tool_name=tool_name),
    }
    if intent:
        headers["X-Intent"] = intent
    if workflow_run_id:
        headers["X-Workflow-Run-Id"] = workflow_run_id
    resp = await client.post(
        f"/mcp?mode={mode}",
        headers=headers,
        json={
            "jsonrpc": "2.0",
            "id": req_id,
            "method": method,
            "params": {"_meta": meta, **(params or {})},
        },
    )
    assert resp.status_code == 200, resp.text
    return resp.json()


async def _call_tool_with_confirm(
    client: httpx.AsyncClient,
    token: str,
    *,
    req_id: str,
    tool_name: str,
    arguments: dict[str, Any],
    intent: str,
    workflow_run_id: str | None = None,
) -> dict[str, Any]:
    resp = await _mcp_rpc(
        client,
        token,
        req_id=req_id,
        method="tools/call",
        params={"name": tool_name, "arguments": arguments},
        intent=intent,
        workflow_run_id=workflow_run_id,
    )
    assert "error" not in resp, resp
    result_obj = resp["result"]
    if result_obj.get("resultType") == "input_required" and isinstance(
        result_obj.get("requestState"), str
    ):
        resp = await _mcp_rpc(
            client,
            token,
            req_id=f"{req_id}-confirmed",
            method="tools/call",
            params={
                "name": tool_name,
                "arguments": arguments,
                "requestState": result_obj["requestState"],
                "inputResponses": {"confirm": {"action": "accept", "content": {"confirm": True}}},
            },
            intent=intent,
            workflow_run_id=workflow_run_id,
        )
        assert "error" not in resp, resp
        result_obj = resp["result"]
    return result_obj.get("structuredContent", result_obj)


def _admin_headers(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


@pytest.mark.asyncio
async def test_onboarding_multi_api_and_mcp_golden_detection_x_y_z_and_ui(
    tmp_path: Path,
) -> None:
    """Onboard fake OpenAPI + MCP servers, drive X/Y/Z + negatives, and verify UI."""
    upstreams_path = tmp_path / "upstreams.json"
    upstreams_path.write_text(json.dumps({"upstreams": []}), encoding="utf-8")
    db_path = tmp_path / "golden-auto-workflow.db"

    settings = GatewaySettings(
        app_env="sandbox",
        auth_mode="dev",
        profile="demo",
        allow_dynamic_upstreams=True,
        upstreams_config_path=str(upstreams_path),
        sqlite_path=str(db_path),
        provenance_signing_secret="golden-auto-workflow-signing-secret-key-01",
    )
    keys = LocalDevKeyManager(issuer=settings.oidc_issuer)
    mock_app = create_mock_backends_app(
        public_key_pem=keys.public_pem,
        expected_issuer=settings.oidc_issuer,
    )
    mock_app.mount("/support", create_mock_support_workflow_app())
    upstream_client = httpx.AsyncClient(
        transport=httpx.ASGITransport(app=mock_app), base_url="http://mock-backends"
    )
    admin_token = keys.issue_user_token(
        persona="gateway_admin",
        overrides={
            "sub": "golden-onboarding-admin",
            "roles": [
                "gateway_admin",
                "platform_admin",
                "hr_ops",
                "hr_writer",
                "ops_engineer",
                "ticketing_writer",
            ],
            "groups": ["hr", "ops"],
            "scopes": ["mcp.tools", "mcp.write", "mcp.admin"],
        },
    )

    app = create_gateway_app(
        settings=settings,
        store=SQLiteAuditStore(db_path),
        dev_keys=keys,
        http_client=upstream_client,
    )

    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://gateway"
        ) as client:
            pack_resp = await client.get(
                "/admin/onboarding/prompt-pack",
                headers=_admin_headers(admin_token),
            )
            assert pack_resp.status_code == 200
            assert "prompt" in pack_resp.json()

            bundles: list[dict[str, Any]] = [
                {
                    "upstream": {
                        "name": "hr_directory",
                        "type": "openapi",
                        "url": "http://mock-backends/hr",
                        "openapi_spec_url": "http://mock-backends/hr/openapi.json",
                        "auth_type": "none",
                        "allowed_roles": ["hr_ops", "hr_writer", "gateway_admin"],
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
                        "tool_approval_policies": {
                            "list_hr_employees": {"mode": "none"},
                            "get_hr_employee": {"mode": "none"},
                            "update_hr_employee": {"mode": "requester_confirmation"},
                        },
                        "mutating_tools": ["update_hr_employee"],
                    },
                    "integration_definition": {
                        "manifest_version": "1.0",
                        "integration": {
                            "id": "hr_directory",
                            "display_name": "HR Directory API",
                            "upstream_name": "hr_directory",
                        },
                        "experiences": [
                            {
                                "id": "hr_profile_ui",
                                "resource_uri": "ui://mcpcodeaway/hr/profile/r1",
                                "renderer_schema": "mcpcodeaway-ui/1",
                                "tool": "hr_directory__get_hr_employee",
                                "fallback": "Use hr_directory__get_hr_employee.",
                                "input_mapping": {"employee_id": "/form/employee_id"},
                                "components": [
                                    {"type": "heading", "text": "Employee Profile"},
                                    {
                                        "type": "text_input",
                                        "id": "employee_id",
                                        "label": "Employee ID",
                                        "required": True,
                                        "max_length": 64,
                                    },
                                    {"type": "button", "label": "Inspect", "action": "invoke_tool"},
                                    {"type": "record", "data": "tool_result"},
                                ],
                                "csp": {
                                    "connect_domains": [],
                                    "resource_domains": [],
                                    "frame_domains": [],
                                },
                            }
                        ],
                        "workflows": [],
                    },
                    "publish": True,
                },
                {
                    "upstream": {
                        "name": "support_mcp",
                        "type": "mcp_http",
                        "url": "http://mock-backends/support/mcp",
                        "auth_type": "none",
                        "allowed_roles": ["gateway_admin", "hr_ops"],
                        "allowed_operations": [
                            "find_customer",
                            "get_account",
                            "get_order_history",
                            "create_ticket",
                        ],
                        "tool_effects": {
                            "find_customer": "read",
                            "get_account": "read",
                            "get_order_history": "read",
                            "create_ticket": "write",
                        },
                        "tool_approval_policies": {
                            "find_customer": {"mode": "none"},
                            "get_account": {"mode": "none"},
                            "get_order_history": {"mode": "none"},
                            "create_ticket": {"mode": "requester_confirmation"},
                        },
                        "mutating_tools": ["create_ticket"],
                    },
                    "integration_definition": {
                        "manifest_version": "1.0",
                        "integration": {
                            "id": "support_mcp",
                            "display_name": "Support MCP",
                            "upstream_name": "support_mcp",
                        },
                        "experiences": [
                            {
                                "id": "support_customer_ui",
                                "resource_uri": "ui://mcpcodeaway/support/customer/r1",
                                "renderer_schema": "mcpcodeaway-ui/1",
                                "tool": "support_mcp__find_customer",
                                "fallback": "Use support_mcp__find_customer.",
                                "input_mapping": {"customer_reference": "/form/customer_reference"},
                                "components": [
                                    {"type": "heading", "text": "Customer Lookup"},
                                    {
                                        "type": "text_input",
                                        "id": "customer_reference",
                                        "label": "Customer Reference",
                                        "required": True,
                                        "max_length": 64,
                                    },
                                    {"type": "button", "label": "Find", "action": "invoke_tool"},
                                    {"type": "record", "data": "tool_result"},
                                ],
                                "csp": {
                                    "connect_domains": [],
                                    "resource_domains": [],
                                    "frame_domains": [],
                                },
                            }
                        ],
                        "workflows": [],
                    },
                    "publish": True,
                },
                {
                    "upstream": {
                        "name": "ops_mcp",
                        "type": "mcp_http",
                        "url": "http://mock-backends/ops/mcp",
                        "auth_type": "delegated_token",
                        "downstream_audience": "api://ops-ticketing",
                        "allowed_roles": ["ops_engineer", "ticketing_writer", "gateway_admin"],
                        "allowed_operations": [
                            "list_terminal_incidents",
                            "get_terminal_health",
                            "search_sentry_errors",
                            "get_github_release_commit",
                            "create_collection_case",
                        ],
                        "tool_effects": {
                            "list_terminal_incidents": "read",
                            "get_terminal_health": "read",
                            "search_sentry_errors": "read",
                            "get_github_release_commit": "read",
                            "create_collection_case": "write",
                        },
                        "tool_approval_policies": {
                            "list_terminal_incidents": {"mode": "none"},
                            "get_terminal_health": {"mode": "none"},
                            "search_sentry_errors": {"mode": "none"},
                            "get_github_release_commit": {"mode": "none"},
                            "create_collection_case": {"mode": "requester_confirmation"},
                        },
                        "mutating_tools": ["create_collection_case"],
                    },
                    "publish": False,
                },
            ]

            for bundle in bundles:
                val = await client.post(
                    "/admin/onboarding/bundle/validate",
                    headers=_admin_headers(admin_token),
                    json=bundle,
                )
                assert val.status_code == 200, val.text
                app_res = await client.post(
                    "/admin/onboarding/bundle/apply",
                    headers=_admin_headers(admin_token),
                    json=bundle,
                )
                assert app_res.status_code == 200, app_res.text

            ui_list = await _mcp_rpc(
                client,
                admin_token,
                req_id="ui-tools-list",
                method="tools/list",
                supports_apps=True,
            )
            ui_tools = {t["name"]: t for t in ui_list["result"]["tools"]}
            assert (
                ui_tools["hr_directory__get_hr_employee"]["_meta"]["ui"]["resourceUri"]
                == "ui://mcpcodeaway/hr/profile/r1"
            )
            assert (
                ui_tools["support_mcp__find_customer"]["_meta"]["ui"]["resourceUri"]
                == "ui://mcpcodeaway/support/customer/r1"
            )
            assert "mcpcodeaway_start_business_execution" not in ui_tools
            assert not any(name.startswith("mca_attr__") for name in ui_tools)

            res_list = await _mcp_rpc(
                client,
                admin_token,
                req_id="ui-res-list",
                method="resources/list",
                supports_apps=True,
            )
            res_uris = {item["uri"]: item for item in res_list["result"]["resources"]}
            assert "ui://mcpcodeaway/hr/profile/r1" in res_uris
            assert "ui://mcpcodeaway/support/customer/r1" in res_uris

            for uri, expected_tool in (
                ("ui://mcpcodeaway/hr/profile/r1", "hr_directory__get_hr_employee"),
                ("ui://mcpcodeaway/support/customer/r1", "support_mcp__find_customer"),
            ):
                read_res = await _mcp_rpc(
                    client,
                    admin_token,
                    req_id=f"read-{expected_tool}",
                    method="resources/read",
                    params={"uri": uri},
                    supports_apps=True,
                )
                content = read_res["result"]["contents"][0]
                assert content["mimeType"] == MCP_APP_MIME_TYPE
                assert content["_meta"]["ui"]["csp"]["connectDomains"] == []
                assert content["_meta"]["ui"]["permissions"] == {}
                assert expected_tool in content["text"]
                assert "mca_attr__" not in content["text"]

            plain_list = await _mcp_rpc(
                client,
                admin_token,
                req_id="plain-tools-list",
                method="tools/list",
                supports_apps=False,
            )
            plain_tools = {t["name"]: t for t in plain_list["result"]["tools"]}
            assert "_meta" not in plain_tools["hr_directory__get_hr_employee"]
            plain_res = await _mcp_rpc(
                client,
                admin_token,
                req_id="plain-res-list",
                method="resources/list",
                supports_apps=False,
            )
            assert plain_res["error"]["code"] == -32021

            flow_x = _GOLDEN_FLOWS["workflow_x_hr_openapi_verified_readback"]
            intent_x = "Update employee EMP-01 role to principal_controller and verify profile"
            await _call_tool_with_confirm(
                client,
                admin_token,
                req_id="x-1",
                tool_name="hr_directory__list_hr_employees",
                arguments={"limit": 10},
                intent=intent_x,
            )
            await _call_tool_with_confirm(
                client,
                admin_token,
                req_id="x-2",
                tool_name="hr_directory__get_hr_employee",
                arguments={"employee_id": "EMP-01"},
                intent=intent_x,
            )
            await _call_tool_with_confirm(
                client,
                admin_token,
                req_id="x-3",
                tool_name="hr_directory__update_hr_employee",
                arguments={"employee_id": "EMP-01", "role": "principal_controller"},
                intent=intent_x,
            )
            x_final = await _call_tool_with_confirm(
                client,
                admin_token,
                req_id="x-4",
                tool_name="hr_directory__get_hr_employee",
                arguments={"employee_id": "EMP-01"},
                intent=intent_x,
            )
            x_det = x_final["workflow_detection"]
            assert x_det["status"] == flow_x["expected_status"]
            assert x_det["completion_tier"] == flow_x["expected_tier"]
            assert x_det["confidence"] >= 0.9

            flow_y = _GOLDEN_FLOWS["workflow_y_support_mcp_ticket_created"]
            intent_y = "Find customer CUST-1001 account and create a billing support ticket"
            await _call_tool_with_confirm(
                client,
                admin_token,
                req_id="y-1",
                tool_name="support_mcp__find_customer",
                arguments={"customer_reference": "CUST-1001"},
                intent=intent_y,
            )
            await _call_tool_with_confirm(
                client,
                admin_token,
                req_id="y-2",
                tool_name="support_mcp__get_account",
                arguments={"customer_id": "CUST-1001"},
                intent=intent_y,
            )
            y_last = await _call_tool_with_confirm(
                client,
                admin_token,
                req_id="y-3",
                tool_name="support_mcp__create_ticket",
                arguments={
                    "customer_id": "CUST-1001",
                    "summary": "Investigate invoice settlement hold",
                },
                intent=intent_y,
            )
            y_ep_id = y_last["workflow_detection"]["episode_id"]

            flow_z = _GOLDEN_FLOWS["workflow_z_ops_mcp_codemode_triage"]
            intent_z = "Triage terminal TERM-EU-44 error and create collection case"
            ops_code = (
                "health = await ops_mcp__get_terminal_health(terminal_id='TERM-EU-44')\n"
                "err = await ops_mcp__search_sentry_errors(terminal_id='TERM-EU-44')\n"
                "pr = await ops_mcp__get_github_release_commit(release_sha=err['release_sha'])\n"
                "{'online': health['online'], 'pr_number': pr['pr_number']}"
            )
            z_cm_res = await _mcp_rpc(
                client,
                admin_token,
                req_id="z-codemode",
                method="tools/call",
                params={"name": "execute_code", "arguments": {"code": ops_code}},
                intent=intent_z,
                workflow_run_id="ops-triage-run-1",
                mode=flow_z["mode"],
            )
            assert "error" not in z_cm_res, z_cm_res
            z_case_res = await _call_tool_with_confirm(
                client,
                admin_token,
                req_id="z-write",
                tool_name="ops_mcp__create_collection_case",
                arguments={
                    "customer_id": "CUST-EU-44",
                    "reason": "Batch timeout recovery",
                },
                intent=intent_z,
                workflow_run_id="ops-triage-run-1",
            )
            z_ep_id = z_case_res["workflow_detection"]["episode_id"]

            tracker = app.state.runtime.workflow_detector
            await tracker.close_and_finalize_episode(
                await app.state.runtime.store.get_workflow_episode("tenant-acme-dev", y_ep_id),
                close_reason="turn_idle",
                run_judge=True,
            )
            await tracker.close_and_finalize_episode(
                await app.state.runtime.store.get_workflow_episode("tenant-acme-dev", z_ep_id),
                close_reason="turn_idle",
                run_judge=True,
            )
            await tracker._judge_queue.join()
            y_closed = await app.state.runtime.store.get_workflow_episode(
                "tenant-acme-dev", y_ep_id
            )
            assert y_closed is not None
            assert y_closed["status"] == flow_y["expected_status"]
            z_closed = await app.state.runtime.store.get_workflow_episode(
                "tenant-acme-dev", z_ep_id
            )
            assert z_closed is not None
            assert z_closed["status"] == flow_z["expected_status"]

            neg_browse = _GOLDEN_FLOWS["negative_hr_browse_only_not_credited"]
            browse_code = (
                "await hr_directory__list_hr_employees(limit=5)\n"
                "emp = await hr_directory__get_hr_employee(employee_id='EMP-04')\n"
                "{'id': emp['employee_id']}"
            )
            browse_res = await _mcp_rpc(
                client,
                admin_token,
                req_id="neg-browse",
                method="tools/call",
                params={"name": "execute_code", "arguments": {"code": browse_code}},
                intent="Inspect people ops directory entries",
                mode="codemode_upfront",
            )
            assert (
                browse_res["result"]["structuredContent"]["workflow_detection"]["status"]
                == neg_browse["expected_status"]
            )

            neg_partial = _GOLDEN_FLOWS["negative_hr_partial_missing_requested_verification"]
            partial_structured = await _call_tool_with_confirm(
                client,
                admin_token,
                req_id="neg-partial",
                tool_name="hr_directory__update_hr_employee",
                arguments={"employee_id": "EMP-02", "role": "interim_controller"},
                intent="Update employee EMP-02 role and verify the new version",
                workflow_run_id="partial-run-1",
            )
            partial_ep_id = partial_structured["workflow_detection"]["episode_id"]
            await tracker.close_and_finalize_episode(
                await app.state.runtime.store.get_workflow_episode(
                    "tenant-acme-dev", partial_ep_id
                ),
                close_reason="turn_idle",
                run_judge=True,
            )
            await tracker._judge_queue.join()
            partial_closed = await app.state.runtime.store.get_workflow_episode(
                "tenant-acme-dev", partial_ep_id
            )
            assert partial_closed is not None
            assert partial_closed["status"] == neg_partial["expected_status"]

            neg_abandoned = _GOLDEN_FLOWS["negative_support_mcp_abandoned_error"]
            abandoned_code = "await support_mcp__find_customer(customer_reference='UNKNOWN-999')\n"
            abandoned_res = await _mcp_rpc(
                client,
                admin_token,
                req_id="neg-abandoned",
                method="tools/call",
                params={"name": "execute_code", "arguments": {"code": abandoned_code}},
                intent="Check customer UNKNOWN-999 record",
                mode="codemode_upfront",
            )
            assert (
                abandoned_res["result"]["structuredContent"]["workflow_detection"]["status"]
                == neg_abandoned["expected_status"]
            )

            pre_events = await app.state.runtime.store.list_business_value_events("tenant-acme-dev")
            assert pre_events == []

            discovered_resp = await client.get(
                "/admin/workflows/discovered",
                headers=_admin_headers(admin_token),
                params={"status": "pending_review"},
            )
            assert discovered_resp.status_code == 200
            discovered_by_upstream: dict[str, list[dict[str, Any]]] = {}
            for cand in discovered_resp.json()["candidates"]:
                discovered_by_upstream.setdefault(cand["upstream_name"], []).append(cand)
            assert "hr_directory" in discovered_by_upstream
            assert "support_mcp" in discovered_by_upstream
            assert "ops_mcp" in discovered_by_upstream

            for upstream, minutes in (
                ("hr_directory", flow_x["manual_baseline_minutes"]),
                ("support_mcp", flow_y["manual_baseline_minutes"]),
                ("ops_mcp", flow_z["manual_baseline_minutes"]),
            ):
                cand = discovered_by_upstream[upstream][0]
                promo = await client.post(
                    f"/admin/workflows/discovered/{cand['candidate_id']}/promote",
                    headers=_admin_headers(admin_token),
                    json={"manual_baseline_minutes": minutes, "publish": True},
                )
                assert promo.status_code == 200, promo.text
                assert promo.json()["published"] is True

            await _call_tool_with_confirm(
                client,
                admin_token,
                req_id="x-replay-1",
                tool_name="hr_directory__list_hr_employees",
                arguments={"limit": 5},
                intent=intent_x,
            )
            await _call_tool_with_confirm(
                client,
                admin_token,
                req_id="x-replay-2",
                tool_name="hr_directory__get_hr_employee",
                arguments={"employee_id": "EMP-03"},
                intent=intent_x,
            )
            await _call_tool_with_confirm(
                client,
                admin_token,
                req_id="x-replay-3",
                tool_name="hr_directory__update_hr_employee",
                arguments={"employee_id": "EMP-03", "role": "director"},
                intent=intent_x,
            )
            replay_final = await _call_tool_with_confirm(
                client,
                admin_token,
                req_id="x-replay-4",
                tool_name="hr_directory__get_hr_employee",
                arguments={"employee_id": "EMP-03"},
                intent=intent_x,
            )
            assert replay_final["workflow_detection"]["status"] == "completed_verified"

            report = await client.get(
                "/admin/business-value/report",
                headers=_admin_headers(admin_token),
            )
            assert report.status_code == 200
            body = report.json()
            assert body["attribution_mode_counts"]["passive_matched"] >= 1
            assert body["episode_summary"]["total"] >= 7

    await upstream_client.aclose()
