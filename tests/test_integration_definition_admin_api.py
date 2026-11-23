"""Admin API acceptance tests for immutable integration-definition revisions."""

from __future__ import annotations

import json
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

import httpx
import pytest

from examples.mock_support_workflow import create_mock_support_workflow_app
from gateway.auth import LocalDevKeyManager
from gateway.config import GatewaySettings
from gateway.server import create_gateway_app
from gateway.state import SQLiteAuditStore
from gateway.upstream_clients import compute_schema_digest


@asynccontextmanager
async def _admin_gateway(tmp_path: Path):
    mock = create_mock_support_workflow_app()
    upstreams_path = tmp_path / "upstreams.json"
    upstreams_path.write_text(
        json.dumps(
            {
                "upstreams": [
                    {
                        "name": "support_api",
                        "type": "mcp_http",
                        "url": "http://support-admin-mock/mcp",
                        "auth_type": "none",
                        "allowed_roles": ["support_agent"],
                        "mutating_tools": ["create_ticket"],
                        "tool_effects": {"create_ticket": "write"},
                    }
                ]
            }
        ),
        encoding="utf-8",
    )
    settings = GatewaySettings(
        profile="demo",
        app_env="sandbox",
        auth_mode="dev",
        upstreams_config_path=str(upstreams_path),
        sqlite_path=str(tmp_path / "integration-definitions.db"),
        provenance_signing_secret="integration-definition-admin-test-signing-key",
        credential_encryption_key="23" * 32,
    )
    keys = LocalDevKeyManager(issuer=settings.oidc_issuer)
    admin_token = keys.issue_user_token(persona="gateway_admin")
    viewer_token = keys.issue_user_token(
        persona="finance_analyst",
        overrides={"roles": ["operator_viewer"], "scopes": ["mcp.tools"]},
    )
    upstream_client = httpx.AsyncClient(
        transport=httpx.ASGITransport(app=mock), base_url="http://support-admin-mock"
    )
    store = SQLiteAuditStore(settings.sqlite_path)
    app = create_gateway_app(
        settings=settings,
        store=store,
        dev_keys=keys,
        http_client=upstream_client,
    )
    gateway_client = httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://gateway"
    )
    async with app.router.lifespan_context(app):
        try:
            yield app.state.runtime, store, gateway_client, admin_token, viewer_token
        finally:
            await gateway_client.aclose()
            await upstream_client.aclose()


def _support_definition(runtime: Any, *, display_name: str = "Support Desk") -> dict[str, Any]:
    tools = runtime.catalog.get_server_tools("support_api")
    return {
        "manifest_version": "1.0",
        "integration": {
            "id": "support_desk",
            "display_name": display_name,
            "upstream_name": "support_api",
            "status": "draft",
            "revision": 1,
        },
        "tool_refs": [
            {"name": tool.qualified_name, "schema_sha256": compute_schema_digest([tool])}
            for tool in tools
        ],
        "experiences": [],
        "workflows": [],
        "standalone_tool_values": [],
    }


def _auth(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


@pytest.mark.asyncio
async def test_integration_definition_admin_rbac_tenant_scope_and_validation(
    tmp_path: Path,
) -> None:
    async with _admin_gateway(tmp_path) as (_runtime, _store, client, admin, viewer):
        first_tool = _runtime.catalog.get_server_tools("support_api")[0]
        guidance = await client.get(
            f"/admin/upstreams/support_api/tools/{first_tool.qualified_name}/guidance",
            headers=_auth(viewer),
        )
        assert guidance.status_code == 200, guidance.text
        assert guidance.json()["schema_sha256"] == compute_schema_digest([first_tool])
        viewer_read = await client.get(
            "/admin/integration-definitions/support_api", headers=_auth(viewer)
        )
        assert viewer_read.status_code == 200

        tenant_override = await client.get(
            "/admin/integration-definitions/support_api?tenant_id=tenant-other",
            headers=_auth(viewer),
        )
        assert tenant_override.status_code == 400

        viewer_write = await client.post(
            "/admin/integration-definitions/validate",
            headers=_auth(viewer),
            json=_support_definition(_runtime),
        )
        assert viewer_write.status_code == 403

        definition = _support_definition(_runtime)
        valid = await client.post(
            "/admin/integration-definitions/validate",
            headers=_auth(admin),
            json=definition,
        )
        assert valid.status_code == 200, valid.text
        assert valid.json()["valid"] is True
        assert valid.json()["tool_count"] == 4

        stale = json.loads(json.dumps(definition))
        stale["tool_refs"][0]["schema_sha256"] = "0" * 64
        invalid = await client.post(
            "/admin/integration-definitions/validate",
            headers=_auth(admin),
            json=stale,
        )
        assert invalid.status_code == 422
        assert "schema digest is stale" in invalid.json()["detail"]

        body_tenant_override = await client.post(
            "/admin/integration-definitions/drafts",
            headers=_auth(admin),
            json={"definition": definition, "tenant_id": "tenant-other"},
        )
        assert body_tenant_override.status_code == 422
        validate_tenant_override = await client.post(
            "/admin/integration-definitions/validate?tenant_id=tenant-other",
            headers=_auth(admin),
            json=definition,
        )
        assert validate_tenant_override.status_code == 400


@pytest.mark.asyncio
async def test_integration_definition_draft_publish_rollback_and_current_projection(
    tmp_path: Path,
) -> None:
    async with _admin_gateway(tmp_path) as (_runtime, store, client, admin, _viewer):
        first_draft = await client.post(
            "/admin/integration-definitions/drafts",
            headers=_auth(admin),
            json={"definition": _support_definition(_runtime)},
        )
        assert first_draft.status_code == 200, first_draft.text
        revision_1 = first_draft.json()["revision"]
        assert revision_1 == "1"
        assert first_draft.json()["definition"]["integration"]["status"] == "draft"

        published_1 = await client.post(
            "/admin/integration-definitions/support_api/publish",
            headers=_auth(admin),
            json={"revision": revision_1},
        )
        assert published_1.status_code == 200, published_1.text
        assert published_1.json()["previous_revision"] is None

        second_draft = await client.post(
            "/admin/integration-definitions/drafts",
            headers=_auth(admin),
            json={"definition": _support_definition(_runtime, display_name="Support Desk v2")},
        )
        assert second_draft.status_code == 200, second_draft.text
        revision_2 = second_draft.json()["revision"]
        assert revision_2 == "2"
        published_2 = await client.post(
            "/admin/integration-definitions/support_api/publish",
            headers=_auth(admin),
            json={"revision": revision_2},
        )
        assert published_2.status_code == 200, published_2.text
        assert published_2.json()["previous_revision"] == revision_1

        rolled_back = await client.post(
            "/admin/integration-definitions/support_api/rollback",
            headers=_auth(admin),
            json={"revision": revision_1},
        )
        assert rolled_back.status_code == 200, rolled_back.text
        assert rolled_back.json()["previous_revision"] == revision_2

        current = await client.get(
            "/admin/integration-definitions/support_api", headers=_auth(admin)
        )
        assert current.status_code == 200
        current_body = current.json()
        assert current_body["current_revision"] == revision_1
        assert (
            current_body["current"]["definition"]["integration"]["display_name"] == "Support Desk"
        )
        assert current_body["current"]["definition"]["integration"]["status"] == "published"
        assert current_body["current"]["definition"]["integration"]["revision"] == 1
        assert [row["revision"] for row in current_body["versions"]] == [revision_1, revision_2]

        with store._ensure_conn() as connection:
            publications = connection.execute(
                "SELECT action, revision, evidence_json FROM integration_definition_publications "
                "WHERE tenant_id=? AND upstream_name=? ORDER BY rowid",
                ("tenant-acme-dev", "support_api"),
            ).fetchall()
        assert [row["action"] for row in publications] == ["publish", "publish", "rollback"]
        assert json.loads(publications[-1]["evidence_json"]) == {
            "previous_revision": revision_2,
            "target_revision": revision_1,
        }


@pytest.mark.asyncio
async def test_stale_definition_draft_is_retained_but_cannot_be_published(
    tmp_path: Path,
) -> None:
    async with _admin_gateway(tmp_path) as (_runtime, _store, client, admin, _viewer):
        stale = _support_definition(_runtime)
        stale["tool_refs"][0]["schema_sha256"] = "f" * 64
        draft = await client.post(
            "/admin/integration-definitions/drafts",
            headers=_auth(admin),
            json={"definition": stale},
        )
        assert draft.status_code == 200, draft.text

        publish = await client.post(
            "/admin/integration-definitions/support_api/publish",
            headers=_auth(admin),
            json={"revision": draft.json()["revision"]},
        )
        assert publish.status_code == 422
        assert "schema digest is stale" in publish.json()["detail"]


@pytest.mark.asyncio
async def test_business_value_report_is_read_only_and_tenant_scoped(
    tmp_path: Path,
) -> None:
    async with _admin_gateway(tmp_path) as (_runtime, store, client, admin, viewer):
        for tenant_id, selected_id, digest in (
            ("tenant-acme-dev", "own-workflow", "own-outcome"),
            ("tenant-other", "other-workflow", "other-outcome"),
        ):
            await store.record_business_value_event(
                {
                    "event_id": f"event-{digest}",
                    "tenant_id": tenant_id,
                    "business_execution_id": f"execution-{digest}",
                    "completion_key": "business-value-completion-v1",
                    "upstream_name": "support_api",
                    "outcome_namespace": "support.ticket.v1",
                    "outcome_digest": digest,
                    "credited": 1,
                    "created_at": "2026-11-22T10:00:00+00:00",
                    "evidence": {
                        "selected_kind": "workflow",
                        "selected_id": selected_id,
                        "definition_revision": "1",
                        "baseline_snapshot": {
                            "manual_baseline_minutes": 5,
                            "baseline_source": "administrator_estimate",
                            "confidence": "unvalidated",
                        },
                    },
                }
            )

        viewer_report = await client.get(
            "/admin/business-value/report?limit=10", headers=_auth(viewer)
        )
        assert viewer_report.status_code == 200
        assert viewer_report.json()["tenant_scope"] == "tenant-acme-dev"
        assert viewer_report.json()["completed_count"] == 1
        assert viewer_report.json()["by_integration_workflow_day"][0]["selected_id"] == (
            "own-workflow"
        )

        viewer_override = await client.get(
            "/admin/business-value/report?tenant_id=tenant-other", headers=_auth(viewer)
        )
        assert viewer_override.status_code == 403

        admin_report = await client.get(
            "/admin/business-value/report?tenant_id=tenant-other", headers=_auth(admin)
        )
        assert admin_report.status_code == 200
        assert admin_report.json()["tenant_scope"] == "tenant-other"
        assert admin_report.json()["by_integration_workflow_day"][0]["selected_id"] == (
            "other-workflow"
        )
