"""Tests for prompt-driven agent onboarding, bundle apply, candidate promotion, and MCP UI."""

from __future__ import annotations

import json
from pathlib import Path

import httpx
import pytest

from examples.mock_backends import create_mock_backends_app
from gateway.auth import LocalDevKeyManager
from gateway.config import GatewaySettings
from gateway.mcp_app_renderer import MCP_APP_EXTENSION, MCP_APP_MIME_TYPE
from gateway.mcp_protocol import MCP_CLIENT_CAPABILITIES_META, request_headers, request_meta
from gateway.server import create_gateway_app
from gateway.state import SQLiteAuditStore


@pytest.mark.asyncio
async def test_prompt_pack_bundle_apply_and_native_mcp_app_ui_delivery(tmp_path: Path) -> None:
    """Admin copies prompt pack, agent submits bundle JSON with workflow + UI, and tools work."""
    mock_app = create_mock_backends_app()
    upstream_client = httpx.AsyncClient(
        transport=httpx.ASGITransport(app=mock_app), base_url="http://mock-backends"
    )
    upstreams_path = tmp_path / "upstreams.json"
    upstreams_path.write_text(json.dumps({"upstreams": []}), encoding="utf-8")
    settings = GatewaySettings(
        app_env="sandbox",
        auth_mode="dev",
        profile="demo",
        upstreams_config_path=str(upstreams_path),
        sqlite_path=str(tmp_path / "onboarding-bundle.db"),
        provenance_signing_secret="prompt-onboarding-test-signing-key-123",
    )
    keys = LocalDevKeyManager(issuer=settings.oidc_issuer)
    admin_token = keys.issue_user_token(
        persona="gateway_admin",
        overrides={"roles": ["gateway_admin", "hr_ops", "hr_writer"], "scopes": ["mcp.tools"]},
    )
    viewer_token = keys.issue_user_token(
        persona="finance_analyst",
        overrides={"roles": ["operator_viewer"], "groups": ["audit"]},
    )
    app = create_gateway_app(
        settings=settings,
        store=SQLiteAuditStore(settings.sqlite_path),
        dev_keys=keys,
        http_client=upstream_client,
    )

    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://gateway"
        ) as client:
            prompt_resp = await client.get(
                "/admin/onboarding/prompt-pack",
                headers={"Authorization": f"Bearer {admin_token}"},
            )
            assert prompt_resp.status_code == 200
            prompt_data = prompt_resp.json()
            assert "OnboardingBundle" in prompt_data["prompt"]
            assert prompt_data["apply_endpoint"] == "/admin/onboarding/bundle/apply"

            bundle = {
                "upstream": {
                    "name": "hr_directory",
                    "type": "openapi",
                    "url": "http://mock-backends/hr",
                    "openapi_spec_url": "http://mock-backends/hr/openapi.json",
                    "auth_type": "none",
                    "allowed_roles": ["hr_ops", "gateway_admin"],
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
                        "update_hr_employee": {"mode": "requester_confirmation"}
                    },
                },
                "integration_definition": {
                    "manifest_version": "1.0",
                    "integration": {
                        "id": "hr_directory",
                        "display_name": "HR Directory",
                        "upstream_name": "hr_directory",
                    },
                    "experiences": [
                        {
                            "id": "employee_lookup_ui",
                            "resource_uri": "ui://mcpcodeaway/hr/lookup/r1",
                            "renderer_schema": "mcpcodeaway-ui/1",
                            "tool": "hr_directory__get_hr_employee",
                            "fallback": "Use hr_directory__get_hr_employee.",
                            "input_mapping": {"employee_id": "/form/employee_id"},
                            "components": [
                                {"type": "heading", "text": "Employee Lookup"},
                                {
                                    "type": "text_input",
                                    "id": "employee_id",
                                    "label": "Employee ID",
                                    "required": True,
                                    "max_length": 64,
                                },
                                {"type": "button", "label": "Load", "action": "invoke_tool"},
                                {"type": "record", "data": "tool_result"},
                            ],
                            "csp": {
                                "connect_domains": [],
                                "resource_domains": [],
                                "frame_domains": [],
                            },
                        }
                    ],
                    "workflows": [
                        {
                            "id": "employee_role_update",
                            "name": "Employee role update",
                            "description": "Read, update, and verify employee profile.",
                            "participating_tools": [
                                "hr_directory__get_hr_employee",
                                "hr_directory__update_hr_employee",
                            ],
                            "required_evidence": [
                                {
                                    "tool": "hr_directory__update_hr_employee",
                                    "result_when": {
                                        "path": "/employee_id",
                                        "type": "non_empty_string",
                                    },
                                }
                            ],
                            "completion": {
                                "kind": "terminal_tool_result",
                                "tool": "hr_directory__get_hr_employee",
                                "success_when": {"path": "/version", "type": "integer"},
                            },
                            "business_value": {
                                "manual_baseline_minutes": 10,
                                "baseline_source": "administrator_estimate",
                                "confidence": "unvalidated",
                                "effective_from": "2026-11-23",
                            },
                        }
                    ],
                },
                "publish": True,
            }

            denied = await client.post(
                "/admin/onboarding/bundle/apply",
                headers={"Authorization": f"Bearer {viewer_token}"},
                json=bundle,
            )
            assert denied.status_code == 403

            applied = await client.post(
                "/admin/onboarding/bundle/apply",
                headers={"Authorization": f"Bearer {admin_token}"},
                json=bundle,
            )
            assert applied.status_code == 200, applied.text
            assert applied.json()["integration_definition"]["published"] is True

            meta = request_meta(client_name="regional-ui-client")
            meta[MCP_CLIENT_CAPABILITIES_META]["extensions"] = {
                MCP_APP_EXTENSION: {"mimeTypes": [MCP_APP_MIME_TYPE]}
            }
            listed = await client.post(
                "/mcp",
                headers={"Authorization": f"Bearer {admin_token}", **request_headers("tools/list")},
                json={
                    "jsonrpc": "2.0",
                    "id": "ui-tools-list",
                    "method": "tools/list",
                    "params": {"_meta": meta},
                },
            )
            tools = {t["name"]: t for t in listed.json()["result"]["tools"]}
            assert "hr_directory__get_hr_employee" in tools
            assert (
                tools["hr_directory__get_hr_employee"]["_meta"]["ui"]["resourceUri"]
                == "ui://mcpcodeaway/hr/lookup/r1"
            )

            res_read = await client.post(
                "/mcp",
                headers={
                    "Authorization": f"Bearer {admin_token}",
                    **request_headers("resources/read"),
                },
                json={
                    "jsonrpc": "2.0",
                    "id": "ui-res-read",
                    "method": "resources/read",
                    "params": {"_meta": meta, "uri": "ui://mcpcodeaway/hr/lookup/r1"},
                },
            )
            html_text = res_read.json()["result"]["contents"][0]["text"]
            assert "hr_directory__get_hr_employee" in html_text
            assert "mca_attr__" not in html_text

            discovered = await client.post(
                "/admin/integration-definitions/hr_directory/discover",
                headers={"Authorization": f"Bearer {admin_token}"},
                json={},
            )
            assert discovered.status_code == 200
            cands = discovered.json()["candidates"]
            assert len(cands) >= 1
            cand_id = cands[0]["candidate_id"]

            promoted = await client.post(
                f"/admin/workflows/discovered/{cand_id}/promote",
                headers={"Authorization": f"Bearer {admin_token}"},
                json={"manual_baseline_minutes": 12, "publish": False},
            )
            assert promoted.status_code == 200
            assert promoted.json()["promoted"] is True

    await upstream_client.aclose()


@pytest.mark.asyncio
async def test_onboarding_bundle_ssrf_422_and_oidc_session_without_dev_keys(
    tmp_path: Path,
) -> None:
    """Admin onboarding endpoints block health_check SSRF, return 422, and work under OIDC."""
    from gateway.admin_auth import AdminSessionManager
    from gateway.auth import CallerIdentity
    from gateway.catalog import ToolCatalog
    from gateway.onboarding_prompts import validate_onboarding_bundle_payload

    prod_settings = GatewaySettings(app_env="prod", block_private_ips_in_prod=True)
    with pytest.raises(ValueError):
        validate_onboarding_bundle_payload(
            {
                "upstream": {
                    "name": "bad_upstream",
                    "type": "http",
                    "url": "https://api.vendor.example.com/mcp",
                    "health_check": {
                        "enabled": True,
                        "url": "http://169.254.169.254/latest/meta-data",
                    },
                }
            },
            settings=prod_settings,
            catalog=ToolCatalog(),
        )

    mock_app = create_mock_backends_app()
    upstream_client = httpx.AsyncClient(
        transport=httpx.ASGITransport(app=mock_app), base_url="http://mock-backends"
    )
    upstreams_path = tmp_path / "upstreams.json"
    upstreams_path.write_text(
        json.dumps(
            {
                "upstreams": [
                    {
                        "name": "hr_directory",
                        "type": "openapi",
                        "url": "http://mock-backends/hr",
                        "openapi_spec_url": "http://mock-backends/hr/openapi.json",
                        "auth_type": "none",
                        "allowed_roles": ["gateway_admin"],
                    }
                ]
            }
        ),
        encoding="utf-8",
    )
    settings = GatewaySettings(
        app_env="sandbox",
        auth_mode="oidc",
        require_admin_auth=True,
        oidc_jwks_uri="https://idp.example.com/.well-known/jwks.json",
        admin_session_secret="oidc-test-admin-session-secret",
        upstreams_config_path=str(upstreams_path),
        sqlite_path=str(tmp_path / "oidc-onboarding.db"),
        provenance_signing_secret="oidc-test-provenance-secret-value",
    )
    store = SQLiteAuditStore(Path(settings.sqlite_path))
    app = create_gateway_app(
        settings=settings,
        store=store,
        dev_keys=None,
        http_client=upstream_client,
    )
    session_mgr = AdminSessionManager(settings)
    admin_identity = CallerIdentity(
        sub="oidc-admin-1",
        tenant_id="tenant-acme-dev",
        roles=["gateway_admin"],
        groups=["admins"],
        scopes=["mcp.admin"],
        client_id="admin-console",
    )
    session_cookie = session_mgr.create_session_token(admin_identity)

    async with app.router.lifespan_context(app):
        assert app.state.runtime._dev_discovery_identity() is None
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url="http://gateway",
            cookies={"mcpcodeaway_admin_session": session_cookie},
        ) as client:
            pack = await client.get(
                "/admin/onboarding/prompt-pack",
                params={"upstream_name": "hr_directory"},
            )
            assert pack.status_code == 200, pack.text
            assert pack.json()["upstream_name"] == "hr_directory"

            disc = await client.post(
                "/admin/integration-definitions/hr_directory/discover",
                json={},
            )
            assert disc.status_code == 200, disc.text

            bad_bundle = {
                "integration_definition": {
                    "manifest_version": "1.0",
                    "integration": {
                        "id": "hr_directory",
                        "display_name": "HR",
                        "upstream_name": "hr_directory",
                    },
                    "experiences": [
                        {
                            "id": "bad_ui",
                            "resource_uri": "not-a-ui-uri",
                            "renderer_schema": "mcpcodeaway-ui/1",
                            "tool": "hr_directory__get_hr_employee",
                            "components": [{"type": "heading", "text": "Bad"}],
                        }
                    ],
                }
            }
            val_res = await client.post("/admin/onboarding/bundle/validate", json=bad_bundle)
            assert val_res.status_code == 422
            apply_res = await client.post("/admin/onboarding/bundle/apply", json=bad_bundle)
            assert apply_res.status_code == 422

    await upstream_client.aclose()


@pytest.mark.asyncio
async def test_prompt_clarifying_questions_and_mcp_ui_management_api(
    tmp_path: Path,
) -> None:
    """Prompt asks clarifying questions first; MCP UI catalog/preview/PUT/DELETE work cleanly."""
    mock_app = create_mock_backends_app()
    upstream_client = httpx.AsyncClient(
        transport=httpx.ASGITransport(app=mock_app), base_url="http://mock-backends"
    )
    upstreams_path = tmp_path / "upstreams.json"
    upstreams_path.write_text(json.dumps({"upstreams": []}), encoding="utf-8")
    settings = GatewaySettings(
        app_env="sandbox",
        auth_mode="dev",
        profile="demo",
        upstreams_config_path=str(upstreams_path),
        sqlite_path=str(tmp_path / "mcp-ui-mgmt.db"),
        provenance_signing_secret="mcp-ui-mgmt-secret-key-for-tests-12345",
    )
    keys = LocalDevKeyManager(issuer=settings.oidc_issuer)
    admin_token = keys.issue_user_token(
        persona="gateway_admin",
        overrides={"roles": ["gateway_admin", "hr_ops", "hr_writer"], "scopes": ["mcp.tools"]},
    )
    app = create_gateway_app(
        settings=settings,
        store=SQLiteAuditStore(settings.sqlite_path),
        dev_keys=keys,
        http_client=upstream_client,
    )
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://gateway"
        ) as client:
            prompt_res = await client.get(
                "/admin/onboarding/prompt-pack",
                headers={"Authorization": f"Bearer {admin_token}"},
            )
            assert prompt_res.status_code == 200
            assert "clarifying question" in prompt_res.json()["prompt"].lower()

            bundle_res = await client.post(
                "/admin/onboarding/bundle/apply",
                headers={"Authorization": f"Bearer {admin_token}"},
                json={
                    "publish": True,
                    "upstream": {
                        "name": "hr_directory",
                        "type": "openapi",
                        "url": "http://mock-backends/hr",
                        "openapi_spec_url": "http://mock-backends/hr/openapi.json",
                        "auth_type": "none",
                        "authorization_mode": "gateway",
                        "allow_all_callers": True,
                        "allowed_operations": ["get_hr_employee"],
                        "tool_effects": {"get_hr_employee": "read"},
                    },
                    "integration_definition": {
                        "manifest_version": "1.0",
                        "integration": {
                            "id": "hr_directory",
                            "display_name": "HR Directory",
                            "upstream_name": "hr_directory",
                        },
                        "experiences": [
                            {
                                "id": "emp_view",
                                "resource_uri": "ui://mcpcodeaway/hr/emp/r1",
                                "renderer_schema": "mcpcodeaway-ui/1",
                                "tool": "hr_directory__get_hr_employee",
                                "fallback": "Display employee lookup card for operator.",
                                "components": [
                                    {"type": "heading", "text": "Employee Inspector"},
                                    {"type": "record", "data": "tool_result"},
                                ],
                            }
                        ],
                    },
                },
            )
            assert bundle_res.status_code == 200, bundle_res.text

            ui_list = await client.get(
                "/admin/mcp-ui/experiences",
                headers={"Authorization": f"Bearer {admin_token}"},
            )
            assert ui_list.status_code == 200
            items = ui_list.json()["experiences"]
            assert len(items) == 1
            assert items[0]["experience"]["id"] == "emp_view"

            preview_ok = await client.post(
                "/admin/mcp-ui/preview",
                headers={"Authorization": f"Bearer {admin_token}"},
                json={"experience": items[0]["experience"]},
            )
            assert preview_ok.status_code == 200
            assert "Employee Inspector" in preview_ok.json()["html"]

            preview_bad = await client.post(
                "/admin/mcp-ui/preview",
                headers={"Authorization": f"Bearer {admin_token}"},
                json={
                    "experience": {
                        **items[0]["experience"],
                        "resource_uri": "https://invalid.example.com",
                    }
                },
            )
            assert preview_bad.status_code == 422

            updated_exp = {
                **items[0]["experience"],
                "fallback": "Updated employee card fallback.",
                "components": [
                    {"type": "heading", "text": "Updated Employee Card"},
                    {"type": "record", "data": "tool_result"},
                ],
            }
            put_res = await client.put(
                "/admin/integration-definitions/hr_directory/experiences/emp_view",
                headers={"Authorization": f"Bearer {admin_token}"},
                json={"experience": updated_exp},
            )
            assert put_res.status_code == 200
            assert put_res.json()["experience"]["fallback"] == "Updated employee card fallback."

            del_res = await client.delete(
                "/admin/integration-definitions/hr_directory/experiences/emp_view",
                headers={"Authorization": f"Bearer {admin_token}"},
            )
            assert del_res.status_code == 200
            after_list = await client.get(
                "/admin/mcp-ui/experiences",
                headers={"Authorization": f"Bearer {admin_token}"},
            )
            assert after_list.json()["experiences"] == []

    await upstream_client.aclose()
