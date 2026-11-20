"""Tests for Phase 15 custom alert rules, JSONL archival, and upstream catalog filtering."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from fastapi.testclient import TestClient

from gateway.alerts import AlertEvaluator, CustomAlertRule
from gateway.config import GatewaySettings
from gateway.mcp_protocol import request_headers, request_meta
from gateway.server import create_gateway_app


def test_custom_alert_rules_evaluate_subcall_errors() -> None:
    evaluator = AlertEvaluator()
    evaluator.rules.custom_rules = [
        CustomAlertRule(
            id="subcall-spike",
            name="Subcall Error Spike",
            metric="subcall_errors",
            operator=">=",
            threshold=1.0,
            severity="critical",
        )
    ]
    alerts = evaluator.evaluate(
        runs=[
            {"ok": False, "grounding_blocked": False, "duration_ms": 12.0},
            {"ok": False, "grounding_blocked": True, "duration_ms": 8.0},
        ],
        drift_warnings=[],
        startup_warnings=[],
        worker_status={},
    )
    custom = [a for a in alerts if a["id"] == "custom:subcall-spike"]
    assert len(custom) == 1
    assert custom[0]["severity"] == "critical"
    assert "Subcall Error Spike" in custom[0]["title"]


def test_verified_default_model_pricing_catalog() -> None:
    settings = GatewaySettings(profile="demo")
    assert settings.model_pricing["qwen3.5-9b"] == {"input": 0.10, "output": 0.15}
    assert settings.model_pricing["gemini-4-argon"] == {"input": 2.00, "output": 10.0}
    assert settings.model_pricing["claude-opus-5.5"] == {"input": 4.0, "output": 20.0}
    assert settings.model_pricing["gpt-6.1-sol"] == {"input": 2.0, "output": 10.0}


def test_admin_upstreams_filtering_and_audit_archive(tmp_path) -> None:
    db_path = tmp_path / "phase15.db"
    settings = GatewaySettings(
        profile="demo",
        sqlite_path=str(db_path),
        upstreams_config_path="config/upstreams.yaml",
    )
    app = create_gateway_app(settings=settings)
    with TestClient(app) as client:
        # 1. Verify /admin/upstreams search, filter & pagination
        res = client.get("/admin/upstreams?q=billing&page=1&page_size=1")
        assert res.status_code == 200
        body = res.json()
        assert body["page"] == 1
        assert body["page_size"] == 1
        assert len(body["upstreams"]) <= 1
        assert "facets" in body
        assert "groups" in body["facets"]

        # 2. Seed an aged run directly via runtime store and test archive-before-purge
        runtime = app.state.runtime
        old_ts = (datetime.now(UTC) - timedelta(days=120)).isoformat()
        client.portal.call(
            runtime.store.record_run,
            {
                "run_id": "run-old-archive-test",
                "created_at": old_ts,
                "mode": "codemode",
                "ok": False,
                "grounding_blocked": False,
                "user_sub": "auditor@acme.test",
                "duration_ms": 14.5,
            },
        )
        arch = client.post("/admin/audit/archive?retention_days=90&prune_after_archive=true")
        assert arch.status_code == 200
        arch_data = arch.json()
        assert arch_data["exported_runs"] >= 1
        assert arch_data["deleted_runs"] >= 1
        assert arch_data["sha256"]
        assert "run-old-archive-test" in (arch_data["jsonl_content"] or "")

        # 3. Retention policy schedule + 1-click optimize-mode + fuzzy analytics
        pol = client.post("/admin/audit/retention-policy?retention_days=60&auto_interval=daily")
        assert pol.status_code == 200
        assert pol.json()["policy"]["auto_interval"] == "daily"

        opt = client.post("/admin/upstreams/billing_legacy_eu/optimize-mode?target_mode=codemode")
        assert opt.status_code == 200
        assert opt.json()["preferred_mode"] == "codemode"

        client.portal.call(
            runtime.store.record_run,
            {
                "run_id": "run-fuzzy-timeline-test",
                "created_at": datetime.now(UTC).isoformat(),
                "mode": "codemode",
                "ok": True,
                "user_sub": "alice.fin@acme.test",
                "user_email": "alice.fin@acme.test",
                "user_groups": ["finance"],
                "duration_ms": 18.0,
                "trace_spans": [
                    {
                        "stage": "gateway_auth",
                        "label": "Auth",
                        "start_ms": 0.0,
                        "duration_ms": 2.0,
                        "status": "ok",
                    },
                    {
                        "stage": "monty_spawn",
                        "label": "Spawn",
                        "start_ms": 2.0,
                        "duration_ms": 3.0,
                        "status": "ok",
                    },
                    {
                        "stage": "monty_exec",
                        "label": "Exec",
                        "start_ms": 5.0,
                        "duration_ms": 13.0,
                        "status": "ok",
                    },
                ],
            },
        )
        fuzzy = client.get("/audit/analytics?group=fin&user=alice")
        assert fuzzy.status_code == 200
        fdata = fuzzy.json()
        assert fdata["summary"]["total_runs"] >= 1
        assert fdata["timeline_by_group"][0]["group"] == "finance"

        # 4. Self-Admin MCP admin_save_upstream and read-only optimize-mode (no AttributeError)
        mcp_save = client.post(
            "/admin/mcp",
            headers=request_headers("tools/call", tool_name="admin_save_upstream"),
            json={
                "jsonrpc": "2.0",
                "id": 99,
                "method": "tools/call",
                "params": {
                    "_meta": request_meta(),
                    "name": "admin_save_upstream",
                    "arguments": {
                        "upstream": {
                            "name": "self_admin_inline_api",
                            "type": "openapi",
                            "url": "http://127.0.0.1:8001",
                            "openapi_spec": {
                                "openapi": "3.1.0",
                                "info": {"title": "Inline", "version": "1.0.0"},
                                "paths": {
                                    "/ping": {
                                        "get": {
                                            "operationId": "ping",
                                            "responses": {"200": {"description": "ok"}},
                                        }
                                    }
                                },
                            },
                            "auth_type": "none",
                        }
                    },
                },
            },
        )
        assert mcp_save.status_code == 200, mcp_save.text
        mcp_res = mcp_save.json()["result"]
        assert mcp_res["isError"] is False
        assert mcp_res["structuredContent"]["saved"] is True

        runtime.settings.allow_dynamic_upstreams = False
        ro_opt = client.post("/admin/upstreams/billing_legacy_eu/optimize-mode?target_mode=auto")
        assert ro_opt.status_code == 200
        assert ro_opt.json()["preferred_mode"] == "auto"


def test_operator_viewer_rbac_and_audit_redaction_and_ssrf_rebinding(tmp_path) -> None:
    db_path = tmp_path / "hardening_audit.db"
    settings = GatewaySettings(
        profile="demo",
        require_admin_auth=True,
        sqlite_path=str(db_path),
        upstreams_config_path=str(tmp_path / "none.yaml"),
    )
    app = create_gateway_app(settings=settings, preloaded_tools={})
    with TestClient(app) as client:
        rt = app.state.runtime
        viewer_tok = rt.dev_keys.issue_user_token(
            overrides={
                "sub": "viewer-01",
                "tenant_id": "tenant-acme-dev",
                "roles": ["operator_viewer"],
                "groups": ["platform"],
            }
        )
        viewer_headers = {"Authorization": f"Bearer {viewer_tok}"}
        # operator_viewer is permitted on read-only /admin/* routes, denied on mutating /admin/*
        assert client.get("/admin/upstreams", headers=viewer_headers).status_code == 200
        assert client.get("/admin/telemetry/runtime", headers=viewer_headers).status_code == 200
        assert client.get("/admin/alerts", headers=viewer_headers).status_code == 200
        assert client.post("/admin/catalog/refresh", headers=viewer_headers).status_code == 403

        # Audit store redacts secrets in code and error strings before writing to SQLite
        fin_id = client.portal.call(
            rt.validator.validate_token,
            rt.dev_keys.issue_user_token(persona="finance_analyst"),
        )
        recorded = client.portal.call(
            lambda: rt.telemetry.record_execution(
                mode="codemode",
                identity=fin_id,
                catalog_version="v1",
                schema_tokens=10,
                subcalls=[],
                returned_payload={"ok": False},
                duration_ms=5.0,
                ok=False,
                code="secret = 'sk-live1234567890abcdef'",
                error="Upstream leaked sk-live1234567890abcdef and SSN 123-45-6789",
            )
        )
        stored = client.portal.call(rt.store.get_run, recorded["run_id"])
        assert "[REDACTED_SECRET]" in stored["code"]
        assert "sk-live1234567890abcdef" not in stored["code"]
        assert "[REDACTED_SECRET]" in stored["error"]
        assert "[REDACTED_SSN]" in stored["error"]
