"""Golden end-to-end test validating mock IDP ticketing API + agent bundle onboarding."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import httpx
import pytest

from examples.mock_backends import create_mock_backends_app
from gateway.auth import LocalDevKeyManager
from gateway.config import GatewaySettings
from gateway.mcp_app_renderer import MCP_APP_EXTENSION, MCP_APP_MIME_TYPE
from gateway.mcp_protocol import MCP_CLIENT_CAPABILITIES_META, request_headers, request_meta
from gateway.server import create_gateway_app
from gateway.state import SQLiteAuditStore

_GOLDEN_DIR = Path(__file__).resolve().parent / "golden"


@pytest.mark.asyncio
async def test_golden_end_to_end_ticketing_agent_onboarding(tmp_path: Path) -> None:
    """Onboard global_ticketing via agent prompt & bundle, enforce IDP OBO, run workflows & UI."""
    flows = json.loads((_GOLDEN_DIR / "flows.json").read_text(encoding="utf-8"))
    flow_spec = flows["agent_onboarding_golden_flows"][0]
    bundle = json.loads((_GOLDEN_DIR / flow_spec["bundle_fixture"]).read_text(encoding="utf-8"))

    upstreams_path = tmp_path / "upstreams.json"
    upstreams_path.write_text(json.dumps({"upstreams": []}), encoding="utf-8")
    settings = GatewaySettings(
        app_env="sandbox",
        auth_mode="dev",
        profile="demo",
        upstreams_config_path=str(upstreams_path),
        sqlite_path=str(tmp_path / "golden-onboarding.db"),
        provenance_signing_secret="golden-agent-onboarding-signing-key-1234",
    )
    keys = LocalDevKeyManager(issuer=settings.oidc_issuer)
    mock_app = create_mock_backends_app(
        public_key_pem=keys.public_pem,
        expected_issuer=keys.issuer,
    )
    upstream_client = httpx.AsyncClient(
        transport=httpx.ASGITransport(app=mock_app),
        base_url="http://mock-backends:8001",
    )
    app = create_gateway_app(
        settings=settings,
        store=SQLiteAuditStore(settings.sqlite_path),
        dev_keys=keys,
        http_client=upstream_client,
    )

    admin_token = keys.issue_user_token(
        persona="gateway_admin",
        overrides={
            "roles": ["gateway_admin", "ops_engineer", "ticketing_writer"],
            "groups": ["operations", "platform-admins"],
            "scopes": ["mcp.tools", "mcp.admin", "ops.write"],
        },
    )
    ops_token = keys.issue_user_token(
        persona="finance_analyst",
        overrides={
            "sub": "sre-elena",
            "roles": ["ops_engineer", "ticketing_reader", "ticketing_writer"],
            "groups": ["operations"],
            "scopes": ["mcp.tools", "ops.write"],
        },
    )
    unauthorized_token = keys.issue_user_token(
        persona="finance_analyst",
        overrides={
            "sub": "viewer-no-ops",
            "roles": ["operator_viewer"],
            "groups": ["finance"],
            "scopes": ["mcp.tools"],
        },
    )

    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url="http://gateway",
        ) as client:
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=mock_app),
                base_url="http://mock-backends:8001",
            ) as raw_mock:
                unauth_tick = await raw_mock.get("/ticketing/tickets")
                assert unauth_tick.status_code == 401
                spec_resp = await raw_mock.get("/ticketing/openapi.json")
                assert spec_resp.status_code == 200
                spec = spec_resp.json()
                assert "HTTPBearer" in (spec.get("components", {}).get("securitySchemes", {}))
                assert len(spec["paths"]) == 15

            prompt_resp = await client.get(
                "/admin/onboarding/prompt-pack",
                params={"upstream_name": flow_spec["upstream_name"]},
                headers={"Authorization": f"Bearer {admin_token}"},
            )
            assert prompt_resp.status_code == 200
            assert "clarifying question" in prompt_resp.json()["prompt"].lower()

            inspect_resp = await client.post(
                "/admin/upstreams/inspect",
                headers={"Authorization": f"Bearer {admin_token}"},
                json={
                    "name": flow_spec["upstream_name"],
                    "type": "openapi",
                    "url": "http://mock-backends:8001/ticketing",
                    "openapi_spec_url": "http://mock-backends:8001/ticketing/openapi.json",
                    "auth_type": "delegated_token",
                    "downstream_audience": flow_spec["expected_audience"],
                },
            )
            assert inspect_resp.status_code == 200, inspect_resp.text
            inspection = inspect_resp.json()["inspection"]
            assert inspection["tool_count"] == flow_spec["expected_tool_count"]

            bad_upstream = {
                k: v for k, v in bundle["upstream"].items() if k != "tool_approval_policies"
            }
            missing_policy_resp = await client.post(
                "/admin/onboarding/bundle/validate",
                headers={"Authorization": f"Bearer {admin_token}"},
                json={**bundle, "upstream": bad_upstream},
            )
            assert missing_policy_resp.status_code == 422

            validate_resp = await client.post(
                "/admin/onboarding/bundle/validate",
                headers={"Authorization": f"Bearer {admin_token}"},
                json=bundle,
            )
            assert validate_resp.status_code == 200, validate_resp.text
            assert validate_resp.json()["valid"] is True

            for exp in bundle["integration_definition"]["experiences"]:
                preview = await client.post(
                    "/admin/mcp-ui/preview",
                    headers={"Authorization": f"Bearer {admin_token}"},
                    json={"experience": exp},
                )
                assert preview.status_code == 200, preview.text
                assert preview.json()["valid"] is True

            apply_resp = await client.post(
                "/admin/onboarding/bundle/apply",
                headers={"Authorization": f"Bearer {admin_token}"},
                json=bundle,
            )
            assert apply_resp.status_code == 200, apply_resp.text
            applied = apply_resp.json()
            assert applied["applied"] is True
            assert applied["upstream"]["registered"] is True
            assert applied["integration_definition"]["published"] is True

            wf_catalog = await client.get(
                "/admin/workflows/catalog",
                headers={"Authorization": f"Bearer {admin_token}"},
            )
            assert wf_catalog.status_code == 200
            wf_ids = {
                item["workflow"]["id"]
                for item in wf_catalog.json()["workflows"]
                if item["upstream_name"] == flow_spec["upstream_name"]
            }
            assert set(flow_spec["expected_workflows"]).issubset(wf_ids)

            ui_catalog = await client.get(
                "/admin/mcp-ui/experiences",
                headers={"Authorization": f"Bearer {admin_token}"},
            )
            assert ui_catalog.status_code == 200
            ui_uris = {
                item["experience"]["resource_uri"]
                for item in ui_catalog.json()["experiences"]
                if item["upstream_name"] == flow_spec["upstream_name"]
            }
            assert ui_uris == set(flow_spec["expected_mcp_ui_uris"])

            denied_list = await client.post(
                "/mcp",
                headers={
                    "Authorization": f"Bearer {unauthorized_token}",
                    **request_headers("tools/list"),
                },
                json={
                    "jsonrpc": "2.0",
                    "id": "tick-unauth-list",
                    "method": "tools/list",
                    "params": {"_meta": request_meta()},
                },
            )
            assert denied_list.status_code == 200
            unauth_names = [
                t["name"]
                for t in denied_list.json()["result"]["tools"]
                if t["name"].startswith("global_ticketing__")
            ]
            assert unauth_names == []

            ui_meta = request_meta(client_name="global-ops-ui")
            ui_meta[MCP_CLIENT_CAPABILITIES_META]["extensions"] = {
                MCP_APP_EXTENSION: {"mimeTypes": [MCP_APP_MIME_TYPE]}
            }

            auto_list = await client.post(
                "/mcp",
                headers={
                    "Authorization": f"Bearer {ops_token}",
                    **request_headers("tools/list"),
                },
                json={
                    "jsonrpc": "2.0",
                    "id": "tick-ops-auto-list",
                    "method": "tools/list",
                    "params": {"_meta": ui_meta},
                },
            )
            assert auto_list.status_code == 200
            assert auto_list.json()["result"]["tools"][0]["name"] == "execute_code"

            tools_list = await client.post(
                "/mcp?mode=traditional",
                headers={
                    "Authorization": f"Bearer {ops_token}",
                    **request_headers("tools/list"),
                },
                json={
                    "jsonrpc": "2.0",
                    "id": "tick-ops-list",
                    "method": "tools/list",
                    "params": {"_meta": ui_meta},
                },
            )
            assert tools_list.status_code == 200
            listed = {
                t["name"]: t
                for t in tools_list.json()["result"]["tools"]
                if t["name"].startswith("global_ticketing__")
            }
            assert len(listed) == flow_spec["expected_tool_count"]
            get_meta = listed["global_ticketing__get_ticket"].get("_meta", {}).get("ui", {})
            assert (
                get_meta.get("resourceUri")
                == "ui://mcpcodeaway/global_ticketing/ticket_inspector/r1"
            )

            async def _call_tool(
                req_id: str, name: str, arguments: dict[str, Any], intent: str
            ) -> dict[str, Any]:
                meta = request_meta(client_name="global-ops-ui")
                meta[MCP_CLIENT_CAPABILITIES_META]["elicitation"] = {"form": {}}
                resp = await client.post(
                    "/mcp?mode=traditional",
                    headers={
                        "Authorization": f"Bearer {ops_token}",
                        "X-Intent": intent,
                        **request_headers("tools/call", tool_name=name),
                    },
                    json={
                        "jsonrpc": "2.0",
                        "id": req_id,
                        "method": "tools/call",
                        "params": {
                            "_meta": meta,
                            "name": name,
                            "arguments": arguments,
                        },
                    },
                )
                assert resp.status_code == 200, resp.text
                body = resp.json()
                assert "error" not in body, body
                result = body["result"]
                if result.get("resultType") == "input_required":
                    confirmed = await client.post(
                        "/mcp?mode=traditional",
                        headers={
                            "Authorization": f"Bearer {ops_token}",
                            "X-Intent": intent,
                            **request_headers("tools/call", tool_name=name),
                        },
                        json={
                            "jsonrpc": "2.0",
                            "id": f"{req_id}-confirm",
                            "method": "tools/call",
                            "params": {
                                "_meta": meta,
                                "name": name,
                                "arguments": arguments,
                                "requestState": result["requestState"],
                                "inputResponses": {
                                    "confirm": {"action": "accept", "content": {"confirm": True}}
                                },
                            },
                        },
                    )
                    assert confirmed.status_code == 200, confirmed.text
                    cbody = confirmed.json()
                    assert "error" not in cbody, cbody
                    return cbody["result"]["structuredContent"]
                return result["structuredContent"]

            ui_read = await client.post(
                "/mcp",
                headers={
                    "Authorization": f"Bearer {ops_token}",
                    **request_headers("resources/read"),
                },
                json={
                    "jsonrpc": "2.0",
                    "id": "tick-ui-read",
                    "method": "resources/read",
                    "params": {
                        "_meta": ui_meta,
                        "uri": "ui://mcpcodeaway/global_ticketing/ticket_inspector/r1",
                    },
                },
            )
            assert ui_read.status_code == 200
            html_content = ui_read.json()["result"]["contents"][0]["text"]
            assert "Global Ticket Inspector" in html_content
            assert "global_ticketing__get_ticket" in html_content

            intent = "Triage open ticket TCK-1001, assign engineer, comment, and resolve it"
            listed_open = await _call_tool(
                "wf-1-list",
                "global_ticketing__list_tickets",
                {"region": "EU", "status": "open"},
                intent,
            )
            assert listed_open["ok"] is True
            assert listed_open["result"]["count"] >= 1
            await _call_tool(
                "wf-1-assign",
                "global_ticketing__assign_ticket",
                {"ticket_id": "TCK-1001", "assignee": "elena.rostova@example.test"},
                intent,
            )
            await _call_tool(
                "wf-1-comment",
                "global_ticketing__add_ticket_comment",
                {"ticket_id": "TCK-1001", "body": "Restarted webhook relay.", "internal": False},
                intent,
            )
            resolved = await _call_tool(
                "wf-1-resolve",
                "global_ticketing__resolve_ticket",
                {
                    "ticket_id": "TCK-1001",
                    "resolution_summary": "Rotated webhook gateway pool",
                    "root_cause": "connection_pool_exhaustion",
                },
                intent,
            )
            assert resolved["ok"] is True
            assert resolved["result"]["status"] == "resolved"

            verified = await _call_tool(
                "wf-1-readback",
                "global_ticketing__get_ticket",
                {"ticket_id": "TCK-1001"},
                intent,
            )
            assert verified["ok"] is True
            assert verified["result"]["status"] == "resolved"
            assert verified["workflow_detection"]["status"] == "completed_verified"
            assert (
                verified["workflow_detection"]["matched_workflow_id"]
                == "global_ticketing/triage_and_resolve_ticket"
            )

            cm_code = (
                "queues = await global_ticketing__list_ticketing_queues()\n"
                "open_tickets = await global_ticketing__list_tickets(status='open')\n"
                "metrics = await global_ticketing__get_ticketing_metrics()\n"
                "{'queue_count': queues['count'], 'open_count': open_tickets['count'], "
                "'total_tickets': metrics['total_tickets']}"
            )
            cm_resp = await client.post(
                "/mcp?mode=codemode_upfront",
                headers={
                    "Authorization": f"Bearer {ops_token}",
                    "X-Intent": "Aggregate global queues, open backlog, and regional summary",
                    **request_headers("tools/call", tool_name="execute_code"),
                },
                json={
                    "jsonrpc": "2.0",
                    "id": "wf-2-codemode",
                    "method": "tools/call",
                    "params": {
                        "_meta": request_meta(),
                        "name": "execute_code",
                        "arguments": {"code": cm_code},
                    },
                },
            )
            assert cm_resp.status_code == 200, cm_resp.text
            cm_struct = cm_resp.json()["result"]["structuredContent"]
            assert cm_struct["ok"] is True
            assert cm_struct["result"]["queue_count"] == 4
            assert cm_struct["result"]["total_tickets"] >= 4

    await upstream_client.aclose()
