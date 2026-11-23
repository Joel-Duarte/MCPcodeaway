"""Tests for passive workflow correlation, readback diff, discovery, and System-1 guardrails."""

from __future__ import annotations

import json
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
from gateway.upstream_clients import DiscoveredTool
from gateway.workflow_detector import (
    SyntheticWorkflowClassifier,
    mine_workflows_from_runs,
    synthesize_workflows_from_catalog,
)


def _setup_hr_config(tmp_path: Path) -> tuple[Path, Path]:
    upstream_config = {
        "upstreams": [
            {
                "name": "hr_directory",
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
    upstreams_path = tmp_path / "upstreams.json"
    upstreams_path.write_text(json.dumps(upstream_config), encoding="utf-8")
    return upstreams_path, tmp_path / "shared-gateway.db"


async def _mcp_call(
    client: httpx.AsyncClient,
    token: str,
    *,
    req_id: str,
    tool_name: str,
    arguments: dict[str, Any],
    intent: str | None = None,
    mode: str = "traditional",
) -> dict[str, Any]:
    headers = {
        "Authorization": f"Bearer {token}",
        **request_headers("tools/call", tool_name=tool_name),
    }
    if intent:
        headers["X-Intent"] = intent
    resp = await client.post(
        f"/mcp?mode={mode}",
        headers=headers,
        json={
            "jsonrpc": "2.0",
            "id": req_id,
            "method": "tools/call",
            "params": {"_meta": request_meta(), "name": tool_name, "arguments": arguments},
        },
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert "error" not in body, body
    return body["result"]["structuredContent"]


@pytest.mark.asyncio
async def test_multi_replica_passive_correlation_readback_and_strict_baseline_gate(
    tmp_path: Path,
) -> None:
    """Calls split across two Gateway replicas share DB episodes without crediting ROI."""
    upstreams_path, db_path = _setup_hr_config(tmp_path)
    mock_app = create_mock_backends_app()
    upstream_client_a = httpx.AsyncClient(
        transport=httpx.ASGITransport(app=mock_app), base_url="http://mock-backends"
    )
    upstream_client_b = httpx.AsyncClient(
        transport=httpx.ASGITransport(app=mock_app), base_url="http://mock-backends"
    )
    settings = GatewaySettings(
        app_env="sandbox",
        auth_mode="dev",
        profile="demo",
        upstreams_config_path=str(upstreams_path),
        sqlite_path=str(db_path),
        provenance_signing_secret="passive-multi-replica-signing-secret-1",
    )
    keys = LocalDevKeyManager(issuer=settings.oidc_issuer)
    token = keys.issue_user_token(
        persona="finance_analyst",
        overrides={
            "sub": "hr-operator-1",
            "roles": ["hr_ops", "hr_writer"],
            "groups": ["hr"],
            "scopes": ["mcp.tools", "mcp.write"],
        },
    )
    app_a = create_gateway_app(
        settings=settings,
        store=SQLiteAuditStore(db_path),
        dev_keys=keys,
        http_client=upstream_client_a,
    )
    app_b = create_gateway_app(
        settings=settings,
        store=SQLiteAuditStore(db_path),
        dev_keys=keys,
        http_client=upstream_client_b,
    )

    intent = "Update employee EMP-01 role to principal_auditor for alice@example.com and verify"
    async with app_a.router.lifespan_context(app_a), app_b.router.lifespan_context(app_b):
        async with (
            httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app_a), base_url="http://replica-a"
            ) as client_a,
            httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app_b), base_url="http://replica-b"
            ) as client_b,
        ):
            r1 = await _mcp_call(
                client_a,
                token,
                req_id="step-1",
                tool_name="hr_directory__get_hr_employee",
                arguments={"employee_id": "EMP-01"},
                intent=intent,
            )
            ep_id = r1["workflow_detection"]["episode_id"]
            assert r1["workflow_detection"]["status"] == "open"

            r2 = await _mcp_call(
                client_b,
                token,
                req_id="step-2",
                tool_name="hr_directory__update_hr_employee",
                arguments={"employee_id": "EMP-01", "role": "principal_auditor"},
                intent=intent,
            )
            assert r2["workflow_detection"]["episode_id"] == ep_id

            r3 = await _mcp_call(
                client_a,
                token,
                req_id="step-3",
                tool_name="hr_directory__get_hr_employee",
                arguments={"employee_id": "EMP-01"},
                intent=intent,
            )
            assert r3["workflow_detection"]["episode_id"] == ep_id
            assert r3["workflow_detection"]["status"] == "completed_verified"
            assert r3["workflow_detection"]["completion_tier"] == "verified_readback"
            assert r3["workflow_detection"]["confidence"] >= 0.9

        episodes = await app_a.state.runtime.store.list_workflow_episodes("tenant-acme-dev")
        assert len(episodes) == 1
        assert "alice@example.com" not in (episodes[0].get("redacted_intent") or "")
        assert "EMP-01" not in json.dumps(episodes[0].get("entity_fingerprints"))

        events = await app_a.state.runtime.store.list_business_value_events("tenant-acme-dev")
        assert events == []

        candidates = await app_a.state.runtime.store.list_discovered_workflow_candidates(
            "tenant-acme-dev", upstream_name="hr_directory"
        )
        assert len(candidates) == 1
        assert candidates[0]["status"] == "pending_review"
        assert candidates[0]["verified_count"] == 1

    await upstream_client_a.aclose()
    await upstream_client_b.aclose()


@pytest.mark.asyncio
async def test_concurrent_entities_stay_isolated_and_codemode_single_turn_verifies(
    tmp_path: Path,
) -> None:
    """Interleaved entity IDs stay in separate episodes; Code Mode completes in one turn."""
    upstreams_path, db_path = _setup_hr_config(tmp_path)
    mock_app = create_mock_backends_app()
    upstream_client = httpx.AsyncClient(
        transport=httpx.ASGITransport(app=mock_app), base_url="http://mock-backends"
    )
    settings = GatewaySettings(
        app_env="sandbox",
        auth_mode="dev",
        profile="demo",
        upstreams_config_path=str(upstreams_path),
        sqlite_path=str(db_path),
        provenance_signing_secret="concurrent-entity-isolation-secret-key",
    )
    keys = LocalDevKeyManager(issuer=settings.oidc_issuer)
    token = keys.issue_user_token(
        persona="finance_analyst",
        overrides={
            "sub": "hr-operator-2",
            "roles": ["hr_ops", "hr_writer"],
            "groups": ["hr"],
            "scopes": ["mcp.tools", "mcp.write"],
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
            e1 = await _mcp_call(
                client,
                token,
                req_id="emp1-read",
                tool_name="hr_directory__get_hr_employee",
                arguments={"employee_id": "EMP-01"},
                intent="Update employee profile",
            )
            e2 = await _mcp_call(
                client,
                token,
                req_id="emp2-read",
                tool_name="hr_directory__get_hr_employee",
                arguments={"employee_id": "EMP-02"},
                intent="Update employee profile",
            )
            assert e1["workflow_detection"]["episode_id"] != e2["workflow_detection"]["episode_id"]

            cm_code = (
                "await hr_directory__get_hr_employee(employee_id='EMP-03')\n"
                "await hr_directory__update_hr_employee(employee_id='EMP-03', role='lead')\n"
                "after = await hr_directory__get_hr_employee(employee_id='EMP-03')\n"
                "{'role': after['role'], 'version': after['version']}"
            )
            cm_res = await _mcp_call(
                client,
                token,
                req_id="cm-update-verify",
                tool_name="execute_code",
                arguments={"code": cm_code},
                intent="Update employee role and verify",
                mode="codemode_upfront",
            )
            assert cm_res["ok"] is True
            assert cm_res["workflow_detection"]["status"] == "completed_verified"
            assert cm_res["workflow_detection"]["completion_tier"] == "verified_readback"

    await upstream_client.aclose()


def test_schema_synthesizer_process_miner_and_system1_guardrail() -> None:
    """Synthesizer, miner, and System-1 classifier produce valid calibrated outputs."""
    from gateway.upstream_clients import DiscoveredTool

    tools = [
        DiscoveredTool(
            server="hr_directory",
            name="list_hr_employees",
            qualified_name="hr_directory__list_hr_employees",
            description="List employees",
            input_schema={"type": "object", "properties": {"limit": {"type": "integer"}}},
            output_schema={"type": "object", "properties": {"items": {"type": "array"}}},
            mutating=False,
        ),
        DiscoveredTool(
            server="hr_directory",
            name="get_hr_employee",
            qualified_name="hr_directory__get_hr_employee",
            description="Get employee by ID",
            input_schema={"type": "object", "properties": {"employee_id": {"type": "string"}}},
            output_schema={
                "type": "object",
                "properties": {
                    "employee_id": {"type": "string"},
                    "version": {"type": "integer"},
                },
            },
            mutating=False,
        ),
        DiscoveredTool(
            server="hr_directory",
            name="update_hr_employee",
            qualified_name="hr_directory__update_hr_employee",
            description="Update employee role",
            input_schema={
                "type": "object",
                "properties": {"employee_id": {"type": "string"}, "role": {"type": "string"}},
            },
            output_schema={
                "type": "object",
                "properties": {"employee_id": {"type": "string"}, "version": {"type": "integer"}},
            },
            mutating=True,
        ),
    ]
    synthesized = synthesize_workflows_from_catalog("hr_directory", tools)
    assert len(synthesized) == 1
    assert synthesized[0]["completion"]["tool"] == "hr_directory__get_hr_employee"
    assert synthesized[0]["completion"]["success_when"] == {"path": "/version", "type": "integer"}

    runs = [
        {
            "ok": True,
            "intent": "Update HR employee role and verify",
            "subcalls": [
                {"tool": "hr_directory__get_hr_employee"},
                {"tool": "hr_directory__update_hr_employee"},
                {"tool": "hr_directory__get_hr_employee"},
            ],
        },
        {
            "ok": True,
            "intent": "Change employee role and readback",
            "subcalls": [
                {"tool": "hr_directory__get_hr_employee"},
                {"tool": "hr_directory__update_hr_employee"},
                {"tool": "hr_directory__get_hr_employee"},
            ],
        },
    ]
    mined = mine_workflows_from_runs("hr_directory", runs, min_support=2)
    assert len(mined) == 1
    assert mined[0]["observed_count"] == 2

    classifier = SyntheticWorkflowClassifier()
    partial = classifier.judge_episode(
        redacted_intent="Update all 3 employees and verify",
        steps=[
            {
                "tool_qualified_name": "hr_directory__update_hr_employee",
                "effect": "write",
                "status": "succeeded",
                "shape_diff": {"readback_verified": False},
            }
        ],
    )
    assert partial.completed is False
    assert partial.verdict == "partial_or_incomplete"
    assert "multi_target_mutation_incomplete" in partial.missing_goals


@pytest.mark.asyncio
async def test_system1_judge_guardrail_downgrades_partial_and_never_upgrades_failed_writes(
    tmp_path: Path,
) -> None:
    """LM Studio/OpenAI-compatible judge can downgrade unverified writes, never upgrade errors."""

    def _lm_studio_transport(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content.decode("utf-8"))
        user_msg = json.loads(body["messages"][1]["content"])
        intent = user_msg.get("intent", "")
        if "downgrade" in intent:
            reply = {
                "completed": False,
                "confidence": 0.35,
                "completion_pct": 35,
                "verdict": "partial_or_incomplete",
                "missing_goals": ["only_one_of_two_records_updated"],
            }
        else:
            reply = {
                "completed": True,
                "confidence": 0.99,
                "completion_pct": 99,
                "verdict": "completed_verified",
                "missing_goals": [],
            }
        return httpx.Response(
            200,
            json={"choices": [{"message": {"content": json.dumps(reply)}}]},
        )

    store = SQLiteAuditStore(tmp_path / "guardrail.db")
    await store.init()
    settings = GatewaySettings(
        app_env="sandbox",
        auth_mode="dev",
        workflow_classifier_backend="openai_compatible",
        workflow_classifier_url="http://127.0.0.1:1234/v1",
        workflow_classifier_model="laya",
    )
    from gateway.catalog import ToolCatalog
    from gateway.workflow_detector import WorkflowEpisodeTracker

    http_client = httpx.AsyncClient(transport=httpx.MockTransport(_lm_studio_transport))
    tracker = WorkflowEpisodeTracker(settings, store, ToolCatalog(), http_client=http_client)

    ep_downgrade = await tracker.observe_execution(
        tenant_id="tenant-acme-dev",
        user_sub="u1",
        client_id="c1",
        session_key="s1",
        intent="Please downgrade check update all 2 employees",
        run_id="run-1",
        calls=[
            {
                "upstream_name": "hr_directory",
                "tool_qualified_name": "hr_directory__update_hr_employee",
                "effect": "write",
                "status": "succeeded",
                "sensitive_write_journal_status": "succeeded",
                "arguments": {"employee_id": "EMP-01", "role": "lead"},
                "result": {"employee_id": "EMP-01", "role": "lead"},
            }
        ],
        is_single_turn_batch=True,
    )
    assert ep_downgrade is not None
    assert ep_downgrade["status"] == "partial_or_incomplete"
    assert ep_downgrade["completion_tier"] == "judge_downgraded"
    assert ep_downgrade["completion_pct"] == 35

    ep_failed = await tracker.observe_execution(
        tenant_id="tenant-acme-dev",
        user_sub="u1",
        client_id="c1",
        session_key="s2",
        intent="Update employee EMP-02",
        run_id="run-2",
        calls=[
            {
                "upstream_name": "hr_directory",
                "tool_qualified_name": "hr_directory__update_hr_employee",
                "effect": "write",
                "status": "failed",
                "arguments": {"employee_id": "EMP-02", "role": "lead"},
                "result": None,
            }
        ],
        is_single_turn_batch=True,
    )
    assert ep_failed is not None
    assert ep_failed["status"] == "partial_or_incomplete"
    assert ep_failed["completion_tier"] == "failed_or_rework"
    await http_client.aclose()
    await store.close()


@pytest.mark.asyncio
async def test_read_then_act_merge_late_evidence_atomic_claim_and_ssrf_guards(
    tmp_path: Path,
) -> None:
    """Read-then-act shares one episode, preserves Call 1 evidence, and enforces SSRF/privacy."""
    from gateway.catalog import ToolCatalog
    from gateway.dispatcher import OutputRedactor
    from gateway.integration_definition import IntegrationDefinition
    from gateway.workflow_detector import WorkflowEpisodeTracker

    assert (
        OutputRedactor.redact_intent("Contact bob.smith@corp.example about sk-secret1234567890")
        == "Contact [REDACTED_EMAIL] about [REDACTED_SECRET]"
    )

    store = SQLiteAuditStore(tmp_path / "detector_regressions.db")
    await store.init()
    definition = IntegrationDefinition.model_validate(
        {
            "manifest_version": "1.0",
            "integration": {
                "id": "support",
                "display_name": "Support",
                "upstream_name": "support",
            },
            "tool_refs": [
                {"name": "support__find_customer", "schema_sha256": "0" * 64},
                {"name": "support__close_ticket", "schema_sha256": "0" * 64},
            ],
            "workflows": [
                {
                    "id": "resolve_customer_ticket",
                    "name": "Resolve customer ticket",
                    "description": "Lookup customer and close their support ticket.",
                    "participating_tools": [
                        "support__find_customer",
                        "support__close_ticket",
                    ],
                    "required_evidence": [
                        {
                            "tool": "support__find_customer",
                            "result_when": {"path": "/verified", "type": "boolean", "equals": True},
                        }
                    ],
                    "completion": {
                        "kind": "terminal_tool_result",
                        "tool": "support__close_ticket",
                        "success_when": {"path": "/status", "type": "non_empty_string"},
                    },
                    "business_value": {
                        "manual_baseline_minutes": 10,
                        "baseline_source": "administrator_estimate",
                        "confidence": "unvalidated",
                        "effective_from": "2026-11-23",
                    },
                }
            ],
        }
    )
    saved_def = await store.save_integration_definition_version(
        {
            "tenant_id": "tenant-acme-dev",
            "upstream_name": "support",
            "definition_digest": "digest-support-v1",
            "definition": definition.model_dump(mode="json", exclude_none=True),
        }
    )
    await store.append_integration_definition_publication(
        {
            "event_id": "pub-support-1",
            "tenant_id": "tenant-acme-dev",
            "upstream_name": "support",
            "action": "publish",
            "revision": saved_def["revision"],
            "actor_sub": "admin",
            "created_at": "2026-11-23T00:00:00Z",
            "evidence": {},
        }
    )

    settings = GatewaySettings(
        app_env="sandbox",
        auth_mode="dev",
        workflow_classifier_backend="synthetic_heuristic",
    )
    tracker = WorkflowEpisodeTracker(settings, store, ToolCatalog())

    ep1 = await tracker.observe_execution(
        tenant_id="tenant-acme-dev",
        user_sub="agent-user",
        client_id="client-a",
        session_key="stateless",
        intent="Handle account alice@example.com inquiry",
        run_id="run-1",
        calls=[
            {
                "upstream_name": "support",
                "tool_qualified_name": "support__find_customer",
                "effect": "read",
                "status": "succeeded",
                "arguments": {"email": "alice@example.com"},
                "result": {"customer_id": "CUST-100", "verified": True},
            }
        ],
    )
    assert ep1 is not None

    ep2 = await tracker.observe_execution(
        tenant_id="tenant-acme-dev",
        user_sub="agent-user",
        client_id="client-a",
        session_key="stateless",
        intent="Finish ticket update for CUST-100",
        run_id="run-2",
        calls=[
            {
                "upstream_name": "support",
                "tool_qualified_name": "support__close_ticket",
                "effect": "write",
                "status": "succeeded",
                "sensitive_write_journal_status": "succeeded",
                "arguments": {"customer_id": "CUST-100"},
                "result": {"customer_id": "CUST-100", "status": "closed"},
            }
        ],
    )
    assert ep2 is not None
    assert ep2["episode_id"] == ep1["episode_id"]
    assert ep2["matched_workflow_id"] == "support/resolve_customer_ticket"
    assert ep2["status"] == "completed_verified"
    assert ep2["completion_tier"] == "predicate_matched"
    assert "CUST-100" not in json.dumps(ep2.get("entity_fingerprints"))
    assert "alice@example.com" not in (ep2.get("redacted_intent") or "")

    assert await store.get_workflow_episode("tenant-acme-dev", ep2["episode_id"]) is not None
    assert (
        await store.claim_open_workflow_episode(
            "tenant-acme-dev",
            ep2["episode_id"],
            next_status="abandoned",
            closed_at="2026-11-23T01:00:00Z",
        )
        is None
    )

    open_ep = await tracker.observe_execution(
        tenant_id="tenant-acme-dev",
        user_sub="agent-user",
        client_id="client-a",
        session_key="stateless",
        intent="Start ticket lookup for claim test",
        run_id="run-claim-1",
        calls=[
            {
                "upstream_name": "support",
                "tool_qualified_name": "support__find_customer",
                "effect": "read",
                "status": "succeeded",
                "arguments": {"customer_id": "CUST-200"},
                "result": {"customer_id": "CUST-200", "verified": False},
            }
        ],
    )
    assert open_ep is not None
    first_claim = await store.claim_open_workflow_episode(
        "tenant-acme-dev",
        open_ep["episode_id"],
        next_status="abandoned",
        closed_at="2026-11-23T01:01:00Z",
    )
    second_claim = await store.claim_open_workflow_episode(
        "tenant-acme-dev",
        open_ep["episode_id"],
        next_status="abandoned",
        closed_at="2026-11-23T01:01:05Z",
    )
    assert first_claim is not None
    assert second_claim is None

    prod_bad_settings = GatewaySettings(
        app_env="prod",
        auth_mode="oidc",
        token_exchange_dialect="rfc8693",
        require_admin_auth=True,
        metrics_require_auth=True,
        allowed_tenants=["tenant-prod"],
        postgres_dsn="postgresql://user:pass@db:5432/gw",
        redis_url="redis://redis:6379/0",
        oidc_issuer="https://93.184.216.34",
        oidc_audience="api://prod-gw",
        oidc_jwks_uri="https://93.184.216.34/jwks",
        oidc_client_id="prod-gw-client",
        gateway_public_url="https://gw.example.com",
        admin_oidc_redirect_uri="https://gw.example.com/auth/admin/callback",
        oidc_client_secret="prod-oidc-secret",
        oidc_token_endpoint="https://93.184.216.34/token",
        admin_oidc_authorization_endpoint="https://93.184.216.34/authorize",
        admin_oidc_api_scope="api://prod-gw/admin",
        provenance_signing_secret="prod-provenance-signing-secret-value-012345",
        admin_session_secret="prod-admin-session-secret-value-01234567",
        workflow_classifier_backend="local_http",
        workflow_classifier_url="http://169.254.169.254/latest/meta-data",
        workflow_classifier_secret="prod-classifier-secret-value-01234567890",
    )
    with pytest.raises(ValueError, match="workflow_classifier_url"):
        prod_bad_settings.validate_production_secrets()

    called_urls: list[str] = []

    def _blocked_transport(request: httpx.Request) -> httpx.Response:
        called_urls.append(str(request.url))
        return httpx.Response(200, json={})

    prod_judge_settings = prod_bad_settings.model_copy(
        update={
            "app_env": "prod",
            "workflow_classifier_backend": "local_http",
            "workflow_classifier_url": "http://169.254.169.254:8003",
        }
    )
    ssrf_client = httpx.AsyncClient(transport=httpx.MockTransport(_blocked_transport))
    prod_tracker = WorkflowEpisodeTracker(
        prod_judge_settings, store, ToolCatalog(), http_client=ssrf_client
    )
    verdict = await prod_tracker._run_tier2_judge(
        {"episode_id": "ep-ssrf", "upstream_name": "support", "redacted_intent": "test"},
        calls=[],
    )
    assert verdict is not None
    assert verdict.backend == "synthetic_heuristic"
    assert called_urls == []

    mined_filtered = mine_workflows_from_runs(
        "hr_directory",
        [
            {"upstream_name": "hr_directory", "tool_sequence": ["other__t1", "other__t2"]},
            {"upstream_name": "hr_directory", "tool_sequence": ["other__t1", "other__t2"]},
        ],
        min_support=2,
    )
    assert mined_filtered == []

    await ssrf_client.aclose()
    await store.close()


@pytest.mark.asyncio
async def test_canonical_fingerprint_dedup_discover_filtering_and_lifecycle_api(
    tmp_path: Path,
) -> None:
    """Canonical fingerprints dedup across sources; PATCH/DELETE toggle and remove workflows."""
    from gateway.integration_definition import compute_workflow_fingerprint
    from gateway.upstream_clients import DiscoveredTool

    tools = [
        DiscoveredTool(
            server="hr_directory",
            name="get_hr_employee",
            qualified_name="hr_directory__get_hr_employee",
            description="Get employee",
            input_schema={"type": "object", "properties": {"employee_id": {"type": "string"}}},
            output_schema={
                "type": "object",
                "properties": {"employee_id": {"type": "string"}, "version": {"type": "integer"}},
            },
            mutating=False,
        ),
        DiscoveredTool(
            server="hr_directory",
            name="update_hr_employee",
            qualified_name="hr_directory__update_hr_employee",
            description="Update employee",
            input_schema={
                "type": "object",
                "properties": {"employee_id": {"type": "string"}, "role": {"type": "string"}},
            },
            output_schema={
                "type": "object",
                "properties": {"employee_id": {"type": "string"}, "version": {"type": "integer"}},
            },
            mutating=True,
        ),
    ]
    synthesized = synthesize_workflows_from_catalog("hr_directory", tools)
    assert len(synthesized) == 1
    schema_fp = synthesized[0]["fingerprint"]
    reordered_fp = compute_workflow_fingerprint(
        upstream_name="hr_directory",
        participating_tools=[
            "hr_directory__update_hr_employee",
            "hr_directory__get_hr_employee",
        ],
        completion_tool="hr_directory__get_hr_employee",
        completion_kind="verified_followup",
    )
    assert schema_fp == reordered_fp

    store = SQLiteAuditStore(tmp_path / "dedup-lifecycle.db")
    await store.init()
    first = await store.upsert_discovered_workflow_candidate(
        {
            "tenant_id": "tenant-acme-dev",
            "upstream_name": "hr_directory",
            "workflow_slug": "auto_schema_hr_directory_employee",
            "fingerprint": schema_fp,
            "origin": "auto_schema",
            "status": "pending_review",
            "observed_count": 1,
            "verified_count": 0,
            "sample_intents": ["Schema readback"],
            "draft_definition": synthesized[0],
        }
    )
    second = await store.upsert_discovered_workflow_candidate(
        {
            "tenant_id": "tenant-acme-dev",
            "upstream_name": "hr_directory",
            "workflow_slug": "auto_mined_hr_directory_employee",
            "fingerprint": reordered_fp,
            "origin": "auto_mined",
            "status": "pending_review",
            "observed_count": 3,
            "verified_count": 2,
            "sample_intents": ["Update HR role"],
            "draft_definition": synthesized[0],
        }
    )
    assert second["candidate_id"] == first["candidate_id"]
    assert second["verified_count"] == 2
    assert len(await store.list_discovered_workflow_candidates("tenant-acme-dev")) == 1
    await store.close()

    upstreams_path, db_path = _setup_hr_config(tmp_path)
    mock_app = create_mock_backends_app()
    upstream_client = httpx.AsyncClient(
        transport=httpx.ASGITransport(app=mock_app), base_url="http://mock-backends"
    )
    settings = GatewaySettings(
        app_env="sandbox",
        auth_mode="dev",
        profile="demo",
        upstreams_config_path=str(upstreams_path),
        sqlite_path=str(db_path),
        provenance_signing_secret="lifecycle-secret-key-for-testing-only-123",
    )
    keys = LocalDevKeyManager(issuer=settings.oidc_issuer)
    admin_token = keys.issue_user_token(
        persona="gateway_admin",
        overrides={
            "roles": ["gateway_admin", "hr_ops", "hr_writer"],
            "groups": ["hr"],
            "scopes": ["mcp.tools", "mcp.write"],
        },
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
            disc = await client.post(
                "/admin/integration-definitions/hr_directory/discover",
                headers={"Authorization": f"Bearer {admin_token}"},
                json={},
            )
            assert disc.status_code == 200
            cands = disc.json()["candidates"]
            assert len(cands) >= 1
            cand_id = cands[0]["candidate_id"]

            promoted = await client.post(
                f"/admin/workflows/discovered/{cand_id}/promote",
                headers={"Authorization": f"Bearer {admin_token}"},
                json={"manual_baseline_minutes": 15, "publish": True},
            )
            assert promoted.status_code == 200

            rediscover = await client.post(
                "/admin/integration-definitions/hr_directory/discover",
                headers={"Authorization": f"Bearer {admin_token}"},
                json={},
            )
            assert rediscover.status_code == 200
            assert all(c["candidate_id"] != cand_id for c in rediscover.json()["candidates"])

            catalog = await client.get(
                "/admin/workflows/catalog",
                headers={"Authorization": f"Bearer {admin_token}"},
            )
            assert catalog.status_code == 200
            wf_entries = [
                w for w in catalog.json()["workflows"] if w["upstream_name"] == "hr_directory"
            ]
            assert len(wf_entries) >= 1
            wf_id = wf_entries[0]["workflow"]["id"]

            paused = await client.patch(
                f"/admin/integration-definitions/hr_directory/workflows/{wf_id}",
                headers={"Authorization": f"Bearer {admin_token}"},
                json={
                    "enabled": False,
                    "manual_baseline_minutes": 22.5,
                    "intent_examples": ["Update HR staff member"],
                },
            )
            assert paused.status_code == 200
            assert paused.json()["workflow"]["enabled"] is False
            assert paused.json()["workflow"]["business_value"]["manual_baseline_minutes"] == 22.5
            assert paused.json()["workflow"]["intent_examples"] == ["Update HR staff member"]

            deleted = await client.delete(
                f"/admin/integration-definitions/hr_directory/workflows/{wf_id}",
                headers={"Authorization": f"Bearer {admin_token}"},
            )
            assert deleted.status_code == 200
            assert deleted.json()["deleted"] is True

            after_catalog = await client.get(
                "/admin/workflows/catalog",
                headers={"Authorization": f"Bearer {admin_token}"},
            )
            assert all(
                w["workflow"]["id"] != wf_id
                for w in after_catalog.json()["workflows"]
                if w["upstream_name"] == "hr_directory"
            )
    await upstream_client.aclose()


def test_system1_judge_required_evidence_and_schema_synthesis_cap() -> None:
    classifier = SyntheticWorkflowClassifier()
    verdict = classifier.judge_episode(
        redacted_intent="Triage and resolve billing incident",
        steps=[
            {
                "tool_qualified_name": "global_ticketing__get_ticket",
                "effect": "read",
                "status": "succeeded",
            },
            {
                "tool_qualified_name": "global_ticketing__add_ticket_comment",
                "effect": "write",
                "status": "succeeded",
                "completion_matched": True,
            },
        ],
        matched_workflow_id="resolve_ticket",
        required_evidence_tools=["global_ticketing__resolve_ticket"],
    )
    assert verdict.completed is False
    assert "missing_required_evidence:global_ticketing__resolve_ticket" in verdict.missing_goals

    reads = [
        DiscoveredTool(
            server="global_ticketing",
            name=name,
            qualified_name=f"global_ticketing__{name}",
            mutating=False,
            input_schema={"type": "object", "properties": {"ticket_id": {"type": "string"}}},
            output_schema={"type": "object", "properties": {"ticket_id": {"type": "string"}}},
        )
        for name in [
            "list_tickets",
            "get_ticket",
            "get_ticket_comments",
            "get_ticket_sla",
            "list_queues",
            "list_agents",
            "get_customer",
            "list_articles",
        ]
    ]
    writes = [
        DiscoveredTool(
            server="global_ticketing",
            name=name,
            qualified_name=f"global_ticketing__{name}",
            mutating=True,
            input_schema={"type": "object", "properties": {"ticket_id": {"type": "string"}}},
            output_schema={
                "type": "object",
                "properties": {
                    "ticket_id": {"type": "string"},
                    "status": {"type": "string"},
                },
            },
        )
        for name in [
            "create_ticket",
            "resolve_ticket",
            "escalate_ticket",
            "update_ticket",
            "assign_ticket",
            "add_ticket_comment",
            "add_ticket_tag",
            "add_ticket_watcher",
            "reopen_ticket",
            "merge_tickets",
        ]
    ]
    synthesized = synthesize_workflows_from_catalog("global_ticketing", [*reads, *writes])
    assert len(synthesized) == 5
    for cand in synthesized:
        assert len(cand["participating_tools"]) <= 3


@pytest.mark.asyncio
async def test_auto_discovery_toggle_dismiss_schema_and_mcp_ui_simulate(
    tmp_path: Path,
) -> None:
    upstreams_path, db_path = _setup_hr_config(tmp_path)
    mock_app = create_mock_backends_app()
    upstream_client = httpx.AsyncClient(
        transport=httpx.ASGITransport(app=mock_app), base_url="http://mock-backends"
    )
    settings = GatewaySettings(
        app_env="sandbox",
        auth_mode="dev",
        profile="demo",
        upstreams_config_path=str(upstreams_path),
        sqlite_path=str(db_path),
        provenance_signing_secret="auto-discovery-toggle-signing-secret-1",
    )
    keys = LocalDevKeyManager(issuer=settings.oidc_issuer)
    admin_token = keys.issue_user_token(
        persona="gateway_admin",
        overrides={
            "roles": ["gateway_admin", "hr_ops", "hr_writer"],
            "groups": ["hr"],
            "scopes": ["mcp.tools", "mcp.write"],
        },
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
            disc = await client.post(
                "/admin/integration-definitions/hr_directory/discover",
                headers={"Authorization": f"Bearer {admin_token}"},
                json={},
            )
            assert disc.status_code == 200
            assert len(disc.json()["candidates"]) >= 1

            swept = await client.post(
                "/admin/workflows/discovered/dismiss-schema",
                headers={"Authorization": f"Bearer {admin_token}"},
                json={"upstream_name": "hr_directory"},
            )
            assert swept.status_code == 200
            assert swept.json()["dismissed_count"] >= 1

            toggled = await client.patch(
                "/admin/upstreams/hr_directory/auto-discovery",
                headers={"Authorization": f"Bearer {admin_token}"},
                json={"enabled": False},
            )
            assert toggled.status_code == 200
            assert toggled.json()["auto_discovery_enabled"] is False

            await _mcp_call(
                client,
                admin_token,
                req_id="no-disc-1",
                tool_name="hr_directory__get_hr_employee",
                arguments={"employee_id": "EMP-01"},
                intent="Update employee EMP-01 and verify",
            )
            await _mcp_call(
                client,
                admin_token,
                req_id="no-disc-2",
                tool_name="hr_directory__update_hr_employee",
                arguments={"employee_id": "EMP-01", "role": "staff_engineer"},
                intent="Update employee EMP-01 and verify",
            )
            await _mcp_call(
                client,
                admin_token,
                req_id="no-disc-3",
                tool_name="hr_directory__get_hr_employee",
                arguments={"employee_id": "EMP-01"},
                intent="Update employee EMP-01 and verify",
            )
            episodes = await app.state.runtime.store.list_workflow_episodes("tenant-acme-dev")
            assert len(episodes) >= 1
            for episode in episodes:
                await app.state.runtime.workflow_detector.close_and_finalize_episode(
                    episode, close_reason="explicit_end", run_judge=True
                )

            catalog = await client.get(
                "/admin/workflows/catalog",
                headers={"Authorization": f"Bearer {admin_token}"},
            )
            assert catalog.status_code == 200
            pending = [
                c
                for c in catalog.json()["candidates"]
                if c["upstream_name"] == "hr_directory" and c["status"] == "pending_review"
            ]
            assert pending == []

            sim = await client.post(
                "/admin/mcp-ui/simulate",
                headers={"Authorization": f"Bearer {admin_token}"},
                json={
                    "tool": "hr_directory__get_hr_employee",
                    "arguments": {"employee_id": "EMP-01"},
                },
            )
            assert sim.status_code == 200
            assert sim.json()["simulated"] is True
            assert sim.json()["tool"] == "hr_directory__get_hr_employee"
            assert isinstance(sim.json()["structuredContent"], dict)
    await upstream_client.aclose()
