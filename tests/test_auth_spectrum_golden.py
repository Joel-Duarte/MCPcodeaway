"""End-to-end Golden Auth & Authz Spectrum suite across mock Entra, Keycloak, and OpenAPI APIs."""

from __future__ import annotations

import base64
import json
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlsplit

import httpx
import pytest
from fastapi.testclient import TestClient

from examples.auth_spectrum.mock_idp import (
    ENTRA_ISSUER,
    KEYCLOAK_ISSUER,
    MockIdpState,
    create_mock_idp_app,
)
from examples.auth_spectrum.services import (
    SHARED_API_KEY,
    SHARED_BEARER_SECRET,
    create_auth_spectrum_services,
)
from gateway.config import AbacRuleConfig, GatewaySettings, UpstreamConfig, UpstreamOAuthConfig
from gateway.mcp_protocol import (
    MCP_CLIENT_CAPABILITIES_META,
    MCP_PROTOCOL_VERSION,
    MCP_PROTOCOL_VERSION_META,
    MCP_SERVER_INFO_META,
)
from gateway.server import create_gateway_app


def _b64_key(seed: bytes) -> str:
    return base64.urlsafe_b64encode(seed * 2).decode("ascii")


def _mcp_call(
    client: TestClient,
    token: str | None,
    method: str,
    params: dict[str, Any] | None = None,
    *,
    tool_name: str | None = None,
) -> Any:
    headers: dict[str, str] = {
        "Content-Type": "application/json",
        "Accept": "application/json, text/event-stream",
        "MCP-Protocol-Version": MCP_PROTOCOL_VERSION,
        "Mcp-Method": method,
        "X-Admin-Force-Mode": "traditional",
    }
    if token is not None:
        headers["Authorization"] = f"Bearer {token}"
    if tool_name:
        headers["Mcp-Name"] = tool_name
    merged_params = dict(params or {})
    merged_params["_meta"] = {
        MCP_PROTOCOL_VERSION_META: MCP_PROTOCOL_VERSION,
        MCP_CLIENT_CAPABILITIES_META: {},
        MCP_SERVER_INFO_META: {"name": "golden-agent", "version": "1.0"},
    }
    return client.post(
        "/mcp",
        headers=headers,
        json={"jsonrpc": "2.0", "id": 1, "method": method, "params": merged_params},
    )


def _extract_tool_json(resp: Any) -> dict[str, Any]:
    body = resp.json()
    assert "result" in body, f"Expected result, got: {body}"
    text = body["result"]["content"][0]["text"]
    parsed = json.loads(text)
    return parsed["result"] if isinstance(parsed, dict) and "result" in parsed else parsed


def test_full_auth_and_authz_spectrum_golden_flows(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    idp = MockIdpState()
    idp_app = create_mock_idp_app(idp)
    services = create_auth_spectrum_services(idp)

    secret_dir = tmp_path / "run_secrets"
    secret_dir.mkdir()
    bearer_file = secret_dir / "tax_bearer"
    bearer_file.write_text(SHARED_BEARER_SECRET, encoding="utf-8")
    monkeypatch.setenv("SPECTRUM_M2M_SECRET", "m2m-secret-xyz")

    async def _asgi_dispatch(request: httpx.Request) -> httpx.Response:
        host = request.url.host or ""
        target_app = idp_app if host == "mock-idp.local" else services.get(host.split(".")[0])
        if target_app is None:
            return httpx.Response(404, json={"detail": f"Unknown host {host}"})
        transport = httpx.ASGITransport(app=target_app)
        async with httpx.AsyncClient(transport=transport, base_url=f"https://{host}") as sub:
            resp = await sub.request(
                request.method,
                str(request.url),
                headers=dict(request.headers),
                content=request.content,
                follow_redirects=False,
            )
            return resp

    mock_http = httpx.AsyncClient(transport=httpx.MockTransport(_asgi_dispatch))
    settings = GatewaySettings(
        app_env="sandbox",
        auth_mode="oidc",
        token_exchange_dialect="entra_v2_obo",
        oidc_issuer=ENTRA_ISSUER,
        oidc_jwks_uri=f"{ENTRA_ISSUER}/jwks",
        oidc_token_endpoint=f"{ENTRA_ISSUER}/token",
        oidc_audience="api://mcp-code-gateway",
        oidc_client_id="mcp-code-gateway",
        oidc_client_secret="gw-entra-secret",
        allowed_tenants=["tenant-acme-entra", "enterprise"],
        allowed_secret_dirs=[str(secret_dir)],
        credential_encryption_key=f"k1:{_b64_key(b'0123456789abcdef')}",
        sqlite_path=str(tmp_path / "gw.db"),
        upstreams_config_path=str(tmp_path / "upstreams.yaml"),
        gateway_mode="traditional",
        execution_mode="classic",
        ssrf_protection_enabled=False,
        require_admin_auth=True,
    )
    (tmp_path / "upstreams.yaml").write_text("upstreams: []\n", encoding="utf-8")

    app = create_gateway_app(settings=settings, http_client=mock_http)

    with TestClient(app) as client:
        admin_tok = idp.issue_entra_token(
            sub="admin-01",
            roles=["gateway_admin"],
            groups=["platform"],
            scopes=["mcp.tools", "mcp.write"],
        )
        finance_eu_tok = idp.issue_entra_token(
            sub="user-fin-01",
            roles=["finance_analyst", "tax_writer"],
            groups=["finance"],
            scopes=["mcp.tools", "mcp.write"],
            client_groups=["EU"],
        )
        auditor_tok = idp.issue_entra_token(
            sub="user-aud-01",
            roles=["readonly_auditor"],
            groups=["audit"],
            scopes=["mcp.tools"],
        )
        wrong_tenant_tok = idp.issue_entra_token(
            sub="user-evil-01",
            tenant_id="tenant-unapproved",
            roles=["finance_analyst"],
            groups=["finance"],
        )
        headless_agent_tok = idp.issue_entra_token(
            sub="sp-nightly-bot",
            client_id="nightly-reconcile-bot",
            roles=["finance_bot_role"],
            groups=[],
            scopes=["mcp.tools"],
        )

        r_sec = client.put(
            "/admin/secrets/inv_api_key",
            headers={"Authorization": f"Bearer {admin_tok}"},
            json={"value": SHARED_API_KEY},
        )
        assert r_sec.status_code == 200

        upstreams = [
            UpstreamConfig(
                name="inventory_obo",
                type="openapi",
                url="https://inventory_obo.internal",
                openapi_spec_url="https://inventory_obo.internal/openapi.json",
                auth_type="delegated_token",
                downstream_audience="api://inventory_obo",
                downstream_scope="api://inventory_obo/mcp.tools",
                allowed_groups=["finance"],
                abac_rules=[
                    AbacRuleConfig(
                        claim="client_groups",
                        response_list_key="items",
                        row_field="region",
                        enforce_on_args=False,
                        filter_response_rows=True,
                    )
                ],
            ),
            UpstreamConfig(
                name="tax_keycloak",
                type="openapi",
                url="https://tax_keycloak.internal",
                openapi_spec_url="https://tax_keycloak.internal/openapi.json",
                auth_type="delegated_token",
                downstream_audience="api://tax_keycloak",
                downstream_scope="mcp.tools",
                allowed_groups=["finance"],
                mutating_tools=["calculate_tax"],
                oauth=UpstreamOAuthConfig(
                    token_exchange_dialect="rfc8693",
                    token_endpoint=f"{KEYCLOAK_ISSUER}/protocol/openid-connect/token",
                    client_id="kc-gw",
                    client_secret_ref="env://SPECTRUM_M2M_SECRET",
                ),
            ),
            UpstreamConfig(
                name="metrics_m2m",
                type="openapi",
                url="https://metrics_m2m.internal",
                openapi_spec_url="https://metrics_m2m.internal/openapi.json",
                auth_type="client_credentials",
                allowed_roles=["finance_analyst", "finance_bot_role"],
                allowed_clients=["copilot-finance-agent", "nightly-reconcile-bot"],
                oauth=UpstreamOAuthConfig(
                    token_endpoint=f"{ENTRA_ISSUER}/token",
                    client_id="gw-m2m-client",
                    client_secret_ref="env://SPECTRUM_M2M_SECRET",
                    scopes=["m2m.read"],
                ),
            ),
            UpstreamConfig(
                name="inventory_apikey",
                type="openapi",
                url="https://inventory_apikey.internal",
                openapi_spec_url="https://inventory_apikey.internal/openapi.json",
                auth_type="api_key",
                secret_ref="vault://inv_api_key",
                api_key_header="X-API-Key",
                allowed_groups=["finance"],
            ),
            UpstreamConfig(
                name="tax_bearer",
                type="openapi",
                url="https://tax_bearer.internal",
                openapi_spec_url="https://tax_bearer.internal/openapi.json",
                auth_type="bearer_secret",
                secret_ref=f"file://{bearer_file}",
                allowed_groups=["finance", "audit"],
            ),
            UpstreamConfig(
                name="external_oauth",
                type="openapi",
                url="https://external_oauth.internal",
                openapi_spec_url="https://external_oauth.internal/openapi.json",
                auth_type="user_oauth",
                allowed_groups=["finance"],
                oauth=UpstreamOAuthConfig(
                    authorize_endpoint="https://mock-idp.local/external/oauth/authorize",
                    token_endpoint="https://mock-idp.local/external/oauth/token",
                    client_id="ext-oauth-client",
                    scopes=["profile"],
                ),
            ),
            UpstreamConfig(
                name="external_pat",
                type="openapi",
                url="https://external_pat.internal",
                openapi_spec_url="https://external_pat.internal/openapi.json",
                auth_type="user_api_key",
                api_key_header="X-Personal-Token",
                allowed_groups=["finance"],
            ),
            UpstreamConfig(
                name="sandbox_open",
                type="openapi",
                url="https://sandbox_open.internal",
                openapi_spec_url="https://sandbox_open.internal/openapi.json",
                auth_type="none",
                allowed_groups=["finance", "audit"],
            ),
        ]
        for cfg in upstreams:
            r_add = client.post(
                "/admin/upstreams",
                headers={"Authorization": f"Bearer {admin_tok}"},
                json=cfg.model_dump(exclude_none=True),
            )
            assert r_add.status_code == 200, r_add.text

        assert _mcp_call(client, None, "server/discover").status_code == 401
        assert _mcp_call(client, wrong_tenant_tok, "server/discover").status_code == 403

        r_obo = _mcp_call(
            client,
            finance_eu_tok,
            "tools/call",
            {"name": "inventory_obo__list_inventory_items", "arguments": {}},
            tool_name="inventory_obo__list_inventory_items",
        )
        d_obo = _extract_tool_json(r_obo)
        assert d_obo.get("auth_verified") == "entra_v2_obo", f"Got {d_obo}"
        assert d_obo["caller_sub"] == "user-fin-01"
        assert [i["region"] for i in d_obo["items"]] == ["EU"]

        r_kc = _mcp_call(
            client,
            finance_eu_tok,
            "tools/call",
            {"name": "tax_keycloak__calculate_tax", "arguments": {"region": "eu", "subtotal": 100}},
            tool_name="tax_keycloak__calculate_tax",
        )
        d_kc = _extract_tool_json(r_kc)
        assert d_kc["auth_verified"] == "rfc8693"
        assert d_kc["total"] == 120.0

        r_m2m = _mcp_call(
            client,
            headless_agent_tok,
            "tools/call",
            {"name": "metrics_m2m__get_metrics_summary", "arguments": {}},
            tool_name="metrics_m2m__get_metrics_summary",
        )
        d_m2m = _extract_tool_json(r_m2m)
        assert d_m2m["auth_verified"] == "client_credentials"
        assert d_m2m["status"] == "ok"

        r_deny_bot = _mcp_call(
            client,
            headless_agent_tok,
            "tools/call",
            {"name": "inventory_obo__list_inventory_items", "arguments": {}},
            tool_name="inventory_obo__list_inventory_items",
        )
        deny_body = r_deny_bot.json()
        assert "error" in deny_body or deny_body.get("result", {}).get("isError") is True

        assert (
            _extract_tool_json(
                _mcp_call(
                    client,
                    finance_eu_tok,
                    "tools/call",
                    {
                        "name": "inventory_apikey__get_item_value",
                        "arguments": {"item_id": "sku-100"},
                    },
                    tool_name="inventory_apikey__get_item_value",
                )
            )["auth_verified"]
            == "api_key"
        )
        assert (
            _extract_tool_json(
                _mcp_call(
                    client,
                    auditor_tok,
                    "tools/call",
                    {"name": "tax_bearer__get_tax_summary", "arguments": {}},
                    tool_name="tax_bearer__get_tax_summary",
                )
            )["auth_verified"]
            == "bearer_secret"
        )
        assert (
            _extract_tool_json(
                _mcp_call(
                    client,
                    auditor_tok,
                    "tools/call",
                    {"name": "sandbox_open__ping_sandbox", "arguments": {}},
                    tool_name="sandbox_open__ping_sandbox",
                )
            )["auth_verified"]
            == "none"
        )

        r_unlinked = _mcp_call(
            client,
            finance_eu_tok,
            "tools/call",
            {"name": "external_oauth__get_external_profile", "arguments": {}},
            tool_name="external_oauth__get_external_profile",
        )
        err_text = r_unlinked.json()["result"]["content"][0]["text"]
        assert "/auth/connect/external_oauth?ticket=" in err_text
        ticket = parse_qs(urlsplit(err_text.split("open ", 1)[1].split(" ", 1)[0]).query)["ticket"][
            0
        ]

        r_auth_redir = client.get(
            f"/auth/connect/external_oauth?ticket={ticket}", follow_redirects=False
        )
        assert r_auth_redir.status_code == 302
        with TestClient(idp_app) as idp_client:
            ext_loc = r_auth_redir.headers["location"]
            r_ext_auth = idp_client.get(
                urlsplit(ext_loc).path + "?" + urlsplit(ext_loc).query,
                follow_redirects=False,
            )
            assert r_ext_auth.status_code == 302
            cb_loc = r_ext_auth.headers["location"]
        r_cb = client.get(urlsplit(cb_loc).path + "?" + urlsplit(cb_loc).query)
        assert r_cb.status_code == 200

        d_ext = _extract_tool_json(
            _mcp_call(
                client,
                finance_eu_tok,
                "tools/call",
                {"name": "external_oauth__get_external_profile", "arguments": {}},
                tool_name="external_oauth__get_external_profile",
            )
        )
        assert d_ext["auth_verified"] == "user_oauth"
        assert d_ext["external_user"] == "user-fin-01"

        r_pat_save = client.post(
            "/me/connections/external_pat/secret",
            headers={"Authorization": f"Bearer {finance_eu_tok}"},
            json={"api_key": "pat-alice-111"},
        )
        assert r_pat_save.status_code == 200
        d_pat = _extract_tool_json(
            _mcp_call(
                client,
                finance_eu_tok,
                "tools/call",
                {"name": "external_pat__list_personal_notes", "arguments": {}},
                tool_name="external_pat__list_personal_notes",
            )
        )
        assert d_pat["owner"] == "user-fin-01"

        other_tenant_same_sub_tok = idp.issue_entra_token(
            sub="user-fin-01",
            tenant_id="enterprise",
            roles=["finance_analyst"],
            groups=["finance"],
            scopes=["mcp.tools"],
        )
        r_other_tenant_conns = client.get(
            "/me/connections",
            headers={"Authorization": f"Bearer {other_tenant_same_sub_tok}"},
        )
        assert r_other_tenant_conns.status_code == 200
        for conn in r_other_tenant_conns.json()["connections"]:
            assert conn["status"] == "not_connected"

        r_other_tenant_pat = _mcp_call(
            client,
            other_tenant_same_sub_tok,
            "tools/call",
            {"name": "external_pat__list_personal_notes", "arguments": {}},
            tool_name="external_pat__list_personal_notes",
        )
        assert "/auth/connect/external_pat?ticket=" in r_other_tenant_pat.text

        other_user_same_tenant_tok = idp.issue_entra_token(
            sub="user-fin-eu-01",
            tenant_id="tenant-acme-entra",
            roles=["finance_analyst"],
            groups=["finance"],
            scopes=["mcp.tools"],
        )
        r_other_user_pat = _mcp_call(
            client,
            other_user_same_tenant_tok,
            "tools/call",
            {"name": "external_pat__list_personal_notes", "arguments": {}},
            tool_name="external_pat__list_personal_notes",
        )
        assert "/auth/connect/external_pat?ticket=" in r_other_user_pat.text

        r_replay = client.post(
            "/auth/connect/external_pat/secret",
            data={"ticket": ticket, "api_key": "stolen-attempt"},
        )
        assert r_replay.status_code == 401

        r_cm = client.post(
            "/mcp",
            headers={
                "Authorization": f"Bearer {finance_eu_tok}",
                "Content-Type": "application/json",
                "Accept": "application/json, text/event-stream",
                "MCP-Protocol-Version": MCP_PROTOCOL_VERSION,
                "Mcp-Method": "tools/call",
                "Mcp-Name": "execute_code",
                "X-Admin-Force-Mode": "codemode",
            },
            json={
                "jsonrpc": "2.0",
                "id": 99,
                "method": "tools/call",
                "params": {
                    "name": "execute_code",
                    "arguments": {
                        "code": (
                            "a = await inventory_apikey__get_item_value(item_id='sku-100')\n"
                            "b = await external_pat__list_personal_notes()\n"
                            "return {'a': a['auth_verified'], 'b': b['owner']}"
                        )
                    },
                    "_meta": {
                        MCP_PROTOCOL_VERSION_META: MCP_PROTOCOL_VERSION,
                        MCP_CLIENT_CAPABILITIES_META: {},
                        MCP_SERVER_INFO_META: {"name": "golden-agent", "version": "1.0"},
                    },
                },
            },
        )
        assert r_cm.status_code == 200
        cm_text = r_cm.text
        assert "api_key" in cm_text and "user-fin-01" in cm_text
        for secret_val in (
            SHARED_API_KEY,
            SHARED_BEARER_SECRET,
            "pat-alice-111",
            "gw-entra-secret",
            "m2m-secret-xyz",
        ):
            assert secret_val not in cm_text

        r_audit = client.get(
            "/audit/runs",
            headers={"Authorization": f"Bearer {admin_tok}"},
        )
        assert r_audit.status_code == 200
        for secret_val in (
            SHARED_API_KEY,
            SHARED_BEARER_SECRET,
            "pat-alice-111",
            "gw-entra-secret",
            "m2m-secret-xyz",
        ):
            assert secret_val not in r_audit.text
