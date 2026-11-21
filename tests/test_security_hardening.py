"""Tests for Phase 9 production hardening: control-plane RBAC, tenant isolation, SSRF, OTLP."""

from __future__ import annotations

import builtins
import sys
import tomllib
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest
import yaml
from opentelemetry import trace
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

from gateway.auth import DEV_PERSONAS, CallerIdentity, LocalDevKeyManager
from gateway.config import GatewaySettings
from gateway.server import create_gateway_app
from gateway.state import SQLiteAuditStore
from gateway.telemetry import TelemetryRecorder
from gateway.upstream_clients import validate_upstream_url


def _make_app(
    tmp_path: Path,
    *,
    app_env: str = "sandbox",
    auth_mode: str = "dev",
    require_admin_auth: bool | None = None,
    allowed_upstream_hosts: list[str] | None = None,
    http_client: httpx.AsyncClient | None = None,
) -> tuple[LocalDevKeyManager, TelemetryRecorder, httpx.ASGITransport]:
    settings = GatewaySettings(
        app_env=app_env,
        auth_mode=auth_mode,
        require_admin_auth=require_admin_auth,
        allowed_upstream_hosts=allowed_upstream_hosts or [],
        allowed_tenants=["tenant-acme-dev", "tenant-other"],
        sqlite_path=str(tmp_path / "gateway.db"),
        upstreams_config_path=str(tmp_path / "missing_upstreams.yaml"),
    )
    dev_keys = LocalDevKeyManager(issuer=settings.oidc_issuer)
    store = SQLiteAuditStore(settings.sqlite_path)
    telemetry = TelemetryRecorder(store)
    app = create_gateway_app(
        settings=settings,
        store=store,
        dev_keys=dev_keys,
        preloaded_tools={},
        http_client=http_client,
    )
    return dev_keys, telemetry, httpx.ASGITransport(app=app)


def test_admin_auth_required_defaults_by_auth_mode() -> None:
    assert "gateway_admin" in DEV_PERSONAS
    assert GatewaySettings(auth_mode="dev").admin_auth_required is False
    assert GatewaySettings(auth_mode="oidc").admin_auth_required is True
    assert GatewaySettings(auth_mode="dev", require_admin_auth=True).admin_auth_required is True
    assert GatewaySettings(auth_mode="oidc", require_admin_auth=False).admin_auth_required is False


@pytest.mark.asyncio
async def test_control_plane_admin_rbac_enforced(tmp_path: Path) -> None:
    dev_keys, _, transport = _make_app(tmp_path, app_env="uat", require_admin_auth=True)
    non_admin_tok = dev_keys.issue_user_token(persona="finance_analyst")
    admin_tok = dev_keys.issue_user_token(persona="gateway_admin")

    candidate = {
        "name": "partner_api",
        "type": "openapi",
        "openapi_spec": {
            "openapi": "3.0.0",
            "info": {"title": "Partner", "version": "1.0.0"},
            "paths": {
                "/items": {
                    "get": {
                        "operationId": "list_items",
                        "responses": {"200": {"description": "ok"}},
                    }
                }
            },
        },
        "auth_type": "none",
    }

    async with httpx.AsyncClient(transport=transport, base_url="http://gateway") as gw:
        for method, path, body in (
            ("GET", "/admin/upstreams", None),
            ("GET", "/admin/upstreams/agent-prompt", None),
            ("POST", "/admin/upstreams/inspect", candidate),
            ("POST", "/admin/upstreams", candidate),
            ("DELETE", "/admin/upstreams/partner_api", None),
            ("GET", "/audit/runs", None),
            ("GET", "/audit/runs/run-123", None),
            ("GET", "/audit/summary", None),
        ):
            unauth = await gw.request(method, path, json=body)
            assert unauth.status_code == 401, (method, path, unauth.text)
            assert unauth.headers.get("www-authenticate") == "Bearer"

        non_admin_headers = {"Authorization": f"Bearer {non_admin_tok}"}
        for method, path, body in (
            ("GET", "/admin/upstreams", None),
            ("GET", "/admin/upstreams/agent-prompt", None),
            ("POST", "/admin/upstreams/inspect", candidate),
            ("POST", "/admin/upstreams", candidate),
            ("DELETE", "/admin/upstreams/partner_api", None),
        ):
            forbidden = await gw.request(method, path, headers=non_admin_headers, json=body)
            assert forbidden.status_code == 403, (method, path, forbidden.text)

        admin_headers = {"Authorization": f"Bearer {admin_tok}"}
        listed = await gw.get("/admin/upstreams", headers=admin_headers)
        assert listed.status_code == 200
        prompt_res = await gw.get("/admin/upstreams/agent-prompt", headers=admin_headers)
        assert prompt_res.status_code == 200
        assert "prompt" in prompt_res.json()

        inspected = await gw.post("/admin/upstreams/inspect", headers=admin_headers, json=candidate)
        assert inspected.status_code == 200
        added = await gw.post("/admin/upstreams", headers=admin_headers, json=candidate)
        assert added.status_code == 200
        assert added.json()["registered"] is True
        deleted = await gw.delete("/admin/upstreams/partner_api", headers=admin_headers)
        assert deleted.status_code == 200
        assert deleted.json()["deleted"] is True


@pytest.mark.asyncio
async def test_audit_routes_enforce_tenant_isolation(tmp_path: Path) -> None:
    dev_keys, telemetry, transport = _make_app(tmp_path, require_admin_auth=True)

    acme_run = await telemetry.record_execution(
        mode="codemode",
        identity=CallerIdentity(sub="user-acme", tenant_id="tenant-acme-dev"),
        catalog_version="v1",
        schema_tokens=100,
        subcalls=[],
        returned_payload={"ok": True},
        duration_ms=12.0,
        ok=True,
    )
    other_run = await telemetry.record_execution(
        mode="traditional",
        identity=CallerIdentity(sub="user-other", tenant_id="tenant-other"),
        catalog_version="v1",
        schema_tokens=200,
        subcalls=[],
        returned_payload={"ok": True},
        duration_ms=15.0,
        ok=True,
    )

    acme_tok = dev_keys.issue_user_token(persona="finance_analyst")
    admin_tok = dev_keys.issue_user_token(persona="gateway_admin")

    async with httpx.AsyncClient(transport=transport, base_url="http://gateway") as gw:
        acme_h = {"Authorization": f"Bearer {acme_tok}"}
        runs_res = await gw.get("/audit/runs", headers=acme_h)
        assert runs_res.status_code == 200
        assert [r["run_id"] for r in runs_res.json()["runs"]] == [acme_run["run_id"]]

        cross_list = await gw.get("/audit/runs?tenant_id=tenant-other", headers=acme_h)
        assert cross_list.status_code == 403

        own_detail = await gw.get(f"/audit/runs/{acme_run['run_id']}", headers=acme_h)
        assert own_detail.status_code == 200
        cross_detail = await gw.get(f"/audit/runs/{other_run['run_id']}", headers=acme_h)
        assert cross_detail.status_code == 403

        own_summary = await gw.get("/audit/summary", headers=acme_h)
        assert own_summary.status_code == 200
        assert own_summary.json()["total_runs"] == 1
        cross_summary = await gw.get("/audit/summary?tenant_id=tenant-other", headers=acme_h)
        assert cross_summary.status_code == 403

        admin_h = {"Authorization": f"Bearer {admin_tok}"}
        all_runs = await gw.get("/audit/runs", headers=admin_h)
        assert all_runs.status_code == 200
        assert all_runs.json()["count"] == 2
        other_filtered = await gw.get("/audit/runs?tenant_id=tenant-other", headers=admin_h)
        assert other_filtered.status_code == 200
        assert [r["run_id"] for r in other_filtered.json()["runs"]] == [other_run["run_id"]]
        admin_detail = await gw.get(f"/audit/runs/{other_run['run_id']}", headers=admin_h)
        assert admin_detail.status_code == 200
        all_summary = await gw.get("/audit/summary", headers=admin_h)
        assert all_summary.status_code == 200
        assert all_summary.json()["total_runs"] == 2


def test_validate_upstream_url_ssrf_rules() -> None:
    for bad in (
        "file:///etc/passwd",
        "ftp://example.com/spec.json",
        "http://169.254.169.254/latest/meta-data/",
        "http://metadata.google.internal/computeMetadata/v1/",
        "http://[::ffff:169.254.169.254]/latest/meta-data/",
        "http://100.100.100.200/latest/meta-data/",
    ):
        for env in ("sandbox", "prod"):
            with pytest.raises(ValueError):
                validate_upstream_url(bad, app_env=env)

    validate_upstream_url("http://127.0.0.1:8080/mcp", app_env="sandbox")
    validate_upstream_url("http://localhost:8001/openapi.json", app_env="uat")

    for prod_bad in (
        "http://127.0.0.1:8080/mcp",
        "http://10.0.0.15:8080/mcp",
        "http://[::1]:8080/mcp",
        "http://localhost:8001/mcp",
        "http://billing.internal/openapi.json",
    ):
        with pytest.raises(ValueError):
            validate_upstream_url(prod_bad, app_env="prod")

    validate_upstream_url("https://api.stripe.com/v1", app_env="prod")
    validate_upstream_url(
        "http://billing.internal/openapi.json",
        app_env="prod",
        allowed_hosts=["billing.internal"],
    )


@pytest.mark.asyncio
async def test_inspect_upstream_blocks_ssrf_before_outbound_http(tmp_path: Path) -> None:
    seen_urls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen_urls.append(str(request.url))
        return httpx.Response(200, json={})

    spy_client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    dev_keys, _, transport = _make_app(tmp_path, require_admin_auth=True, http_client=spy_client)
    admin_tok = dev_keys.issue_user_token(persona="gateway_admin")

    async with httpx.AsyncClient(transport=transport, base_url="http://gateway") as gw:
        res = await gw.post(
            "/admin/upstreams/inspect",
            headers={"Authorization": f"Bearer {admin_tok}"},
            json={
                "name": "ssrf_probe",
                "type": "openapi",
                "url": "https://api.example.com",
                "openapi_spec_url": "http://169.254.169.254/latest/meta-data/iam",
                "auth_type": "none",
            },
        )
        assert res.status_code == 400
        assert "metadata" in res.json()["detail"].lower()

        bad_scheme = await gw.post(
            "/admin/upstreams/inspect",
            headers={"Authorization": f"Bearer {admin_tok}"},
            json={
                "name": "file_probe",
                "type": "mcp_http",
                "url": "file:///etc/passwd",
                "auth_type": "none",
            },
        )
        assert bad_scheme.status_code == 400
        assert "scheme" in bad_scheme.json()["detail"].lower()

    assert seen_urls == []
    await spy_client.aclose()


@pytest.mark.asyncio
async def test_otel_exporter_and_otlp_optional_dependency(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    pyproject = tomllib.loads(Path("pyproject.toml").read_text(encoding="utf-8"))
    assert "otlp" in pyproject["project"]["optional-dependencies"]

    exporter = InMemorySpanExporter()
    from gateway.telemetry import configure_telemetry

    tracer = configure_telemetry(span_exporter=exporter)
    with tracer.start_as_current_span("phase9.test.span") as span:
        span.set_attribute("tenant_id", "tenant-acme-dev")
    spans = exporter.get_finished_spans()
    assert any(s.name == "phase9.test.span" for s in spans)

    captured_endpoints: list[str] = []

    class FakeOtlpExporter:
        def __init__(self, *, endpoint: str) -> None:
            captured_endpoints.append(endpoint)

        def export(self, _spans):  # pragma: no cover - no flush required here
            return 0

        def shutdown(self) -> None:  # pragma: no cover
            return None

        def force_flush(self, _timeout_millis: int = 30000) -> bool:
            return True

    fake_mod = SimpleNamespace(OTLPSpanExporter=FakeOtlpExporter)
    monkeypatch.setitem(
        sys.modules,
        "opentelemetry.exporter.otlp.proto.http.trace_exporter",
        fake_mod,
    )
    configure_telemetry(otlp_endpoint="http://127.0.0.1:4318/v1/traces")
    assert captured_endpoints == ["http://127.0.0.1:4318/v1/traces"]

    real_import = builtins.__import__

    def blocked_import(name, *args, **kwargs):
        if name.startswith("opentelemetry.exporter"):
            raise ImportError("missing otlp")
        return real_import(name, *args, **kwargs)

    monkeypatch.delitem(
        sys.modules,
        "opentelemetry.exporter.otlp.proto.http.trace_exporter",
        raising=False,
    )
    monkeypatch.setattr(builtins, "__import__", blocked_import)
    with pytest.raises(RuntimeError, match="mcpcodeaway\\[otlp\\]"):
        configure_telemetry(otlp_endpoint="http://127.0.0.1:4318/v1/traces")
    assert isinstance(trace.get_tracer_provider(), object)


def test_docker_compose_scaled_profile_wires_shared_secrets() -> None:
    compose = yaml.safe_load(Path("docker-compose.yml").read_text(encoding="utf-8"))
    worker_env = compose["services"]["sandbox-worker"]["environment"]
    scaled_env = compose["services"]["gateway-scaled"]["environment"]

    assert "GATEWAY_SANDBOX_WORKER_SECRET" in worker_env
    assert "GATEWAY_SANDBOX_WORKER_SECRET" in scaled_env
    assert (
        worker_env["GATEWAY_SANDBOX_WORKER_SECRET"] == scaled_env["GATEWAY_SANDBOX_WORKER_SECRET"]
    )
    assert "GATEWAY_PROVENANCE_SIGNING_SECRET" in scaled_env
