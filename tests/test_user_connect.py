"""Tests for per-upstream OBO, client_credentials, user_oauth/user_api_key, and /admin/secrets."""

from __future__ import annotations

import base64
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

import httpx
import pytest
from fastapi.testclient import TestClient

from gateway.auth import (
    CallerIdentity,
    DelegatedTokenExchanger,
    LocalDevKeyManager,
    UserCredentialRequiredError,
)
from gateway.config import GatewaySettings, UpstreamConfig, UpstreamOAuthConfig
from gateway.server import create_gateway_app
from gateway.state import InMemoryCacheBackend


def _b64_key(seed: bytes) -> str:
    return base64.urlsafe_b64encode(seed * 2).decode("ascii")


async def test_per_upstream_obo_dialect_override_and_cache_isolation() -> None:
    calls: list[dict[str, str]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        body = parse_qs(request.content.decode("utf-8"))
        grant = body.get("grant_type", [""])[0]
        calls.append({"url": str(request.url), "grant_type": grant})
        if grant == "urn:ietf:params:oauth:grant-type:jwt-bearer":
            return httpx.Response(200, json={"access_token": "entra-obo-tok", "expires_in": 300})
        return httpx.Response(200, json={"access_token": "keycloak-8693-tok", "expires_in": 300})

    settings = GatewaySettings(
        app_env="sandbox",
        auth_mode="dev",
        token_exchange_dialect="entra_v2_obo",
        oidc_token_endpoint="https://login.microsoftonline.com/t1/oauth2/v2.0/token",
        oidc_client_id="gw-client",
        oidc_client_secret="gw-secret",
        ssrf_protection_enabled=False,
    )
    identity = CallerIdentity(
        sub="user-1",
        tenant_id="tenant-acme-dev",
        raw_token="caller-jwt",
    )
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        exchanger = DelegatedTokenExchanger(settings, InMemoryCacheBackend(), http_client=client)
        r_entra = await exchanger.exchange(identity, audience="api://svc1", scope="s1")
        r_kc = await exchanger.exchange(
            identity,
            audience="api://svc1",
            scope="s1",
            dialect_override="rfc8693",
            token_endpoint_override="https://sso.company.internal/realms/ent/protocol/openid-connect/token",
            client_id_override="kc-gw",
            client_secret_override="kc-sec",
        )
        assert r_entra.access_token == "entra-obo-tok"
        assert r_kc.access_token == "keycloak-8693-tok"
        assert len(calls) == 2
        assert calls[0]["grant_type"] == "urn:ietf:params:oauth:grant-type:jwt-bearer"
        assert calls[1]["grant_type"] == "urn:ietf:params:oauth:grant-type:token-exchange"


def test_user_api_key_user_oauth_client_credentials_and_admin_secrets(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("M2M_SECRET", "m2m-client-secret")
    monkeypatch.setenv("OAUTH_CLIENT_SECRET", "oauth-client-secret")

    oauth_calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        body = parse_qs(request.content.decode("utf-8"))
        grant = body.get("grant_type", [""])[0]
        oauth_calls.append(grant)
        if grant == "client_credentials":
            return httpx.Response(200, json={"access_token": "m2m-tok-1", "expires_in": 300})
        if grant == "authorization_code":
            return httpx.Response(
                200,
                json={
                    "access_token": "user-oauth-tok-1",
                    "refresh_token": "user-refresh-1",
                    "expires_in": 10,
                },
            )
        if grant == "refresh_token":
            return httpx.Response(
                200,
                json={
                    "access_token": "user-oauth-tok-2",
                    "refresh_token": "user-refresh-2",
                    "expires_in": 3600,
                },
            )
        return httpx.Response(400, json={"error": "unsupported_grant_type"})

    mock_http = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    settings = GatewaySettings(
        app_env="sandbox",
        auth_mode="dev",
        sqlite_path=str(tmp_path / "gw.db"),
        upstreams_config_path=str(tmp_path / "upstreams.yaml"),
        credential_encryption_key=f"k1:{_b64_key(b'0123456789abcdef')}",
        ssrf_protection_enabled=False,
        require_admin_auth=True,
    )
    (tmp_path / "upstreams.yaml").write_text("upstreams: []\n", encoding="utf-8")
    app = create_gateway_app(
        settings=settings,
        dev_keys=LocalDevKeyManager(issuer=settings.oidc_issuer),
        http_client=mock_http,
    )
    runtime = app.state.runtime

    with TestClient(app) as client:
        assert runtime.dev_keys is not None
        admin_tok = runtime.dev_keys.issue_user_token(
            persona="ops_engineer",
            overrides={"roles": ["gateway_admin"]},
        )
        viewer_tok = runtime.dev_keys.issue_user_token(
            persona="readonly_auditor",
            overrides={"roles": ["operator_viewer"]},
        )
        user_a_tok = runtime.dev_keys.issue_user_token(persona="finance_analyst")
        user_b_tok = runtime.dev_keys.issue_user_token(persona="eu_accountant")

        # 1. /admin/secrets RBAC & vault:// storage
        r_forbid = client.put(
            "/admin/secrets/jira_shared",
            headers={"Authorization": f"Bearer {viewer_tok}"},
            json={"value": "secret-val"},
        )
        assert r_forbid.status_code == 403

        r_put = client.put(
            "/admin/secrets/jira_shared",
            headers={"Authorization": f"Bearer {admin_tok}"},
            json={"value": "secret-val"},
        )
        assert r_put.status_code == 200
        r_list = client.get(
            "/admin/secrets",
            headers={"Authorization": f"Bearer {viewer_tok}"},
        )
        assert r_list.status_code == 200
        assert r_list.json()["secrets"][0]["name"] == "jira_shared"
        assert "secret-val" not in r_list.text

        # 2. Register upstreams for user_api_key, user_oauth, and client_credentials
        pat_up = UpstreamConfig(
            name="personal_jira",
            type="openapi",
            url="https://jira.example.com",
            auth_type="user_api_key",
            api_key_header="X-Jira-Token",
        )
        oauth_up = UpstreamConfig(
            name="external_github",
            type="openapi",
            url="https://api.github.example.com",
            auth_type="user_oauth",
            oauth=UpstreamOAuthConfig(
                authorize_endpoint="https://github.example.com/login/oauth/authorize",
                token_endpoint="https://github.example.com/login/oauth/access_token",
                client_id="gh-client",
                client_secret_ref="env://OAUTH_CLIENT_SECRET",
                scopes=["repo"],
            ),
        )
        m2m_up = UpstreamConfig(
            name="internal_m2m",
            type="openapi",
            url="https://m2m.example.com",
            auth_type="client_credentials",
            oauth=UpstreamOAuthConfig(
                token_endpoint="https://auth.example.com/token",
                client_id="m2m-client",
                client_secret_ref="env://M2M_SECRET",
                scopes=["read"],
            ),
        )
        for cfg in (pat_up, oauth_up, m2m_up):
            runtime.registry.upsert(cfg)

        # 3. client_credentials resolves and caches M2M token
        import asyncio

        id_a = asyncio.run(runtime.validator.validate_token(user_a_tok))
        id_b = asyncio.run(runtime.validator.validate_token(user_b_tok))
        h1, hit1, _ = asyncio.run(runtime.dispatcher.resolve_upstream_headers(id_a, m2m_up))
        h2, hit2, _ = asyncio.run(runtime.dispatcher.resolve_upstream_headers(id_a, m2m_up))
        assert h1 == {"Authorization": "Bearer m2m-tok-1"}
        assert hit1 is False
        assert hit2 is True

        # 4. user_api_key: missing -> UserCredentialRequiredError -> connect -> works
        with pytest.raises(UserCredentialRequiredError) as exc_info:
            asyncio.run(runtime.dispatcher.resolve_upstream_headers(id_a, pat_up))
        connect_url = exc_info.value.connect_url
        ticket = parse_qs(urlsplit(connect_url).query)["ticket"][0]

        r_form = client.get(f"/auth/connect/personal_jira?ticket={ticket}")
        assert r_form.status_code == 200
        assert "Personal API Key" in r_form.text

        r_save = client.post(
            "/auth/connect/personal_jira/secret",
            data={"ticket": ticket, "api_key": "pat-alice-999"},
        )
        assert r_save.status_code == 200

        h_pat, _, _ = asyncio.run(runtime.dispatcher.resolve_upstream_headers(id_a, pat_up))
        assert h_pat == {"X-Jira-Token": "pat-alice-999"}

        # User B is isolated from User A's PAT
        with pytest.raises(UserCredentialRequiredError):
            asyncio.run(runtime.dispatcher.resolve_upstream_headers(id_b, pat_up))

        # 5. user_oauth: PKCE redirect -> callback -> resolve -> refresh when near expiry
        r_me = client.get("/me/connections", headers={"Authorization": f"Bearer {user_a_tok}"})
        assert r_me.status_code == 200
        gh_conn = [
            c for c in r_me.json()["connections"] if c["upstream_name"] == "external_github"
        ][0]
        assert gh_conn["status"] == "not_connected"

        r_redir = client.get(
            urlsplit(gh_conn["connect_url"]).path + "?" + urlsplit(gh_conn["connect_url"]).query,
            follow_redirects=False,
        )
        assert r_redir.status_code == 302
        state = parse_qs(urlsplit(r_redir.headers["location"]).query)["state"][0]

        r_cb = client.get(f"/auth/connect/external_github/callback?code=auth-code-1&state={state}")
        assert r_cb.status_code == 200

        # expires_in=10 is within 30s refresh window, so this triggers refresh_token
        h_oauth, _, _ = asyncio.run(runtime.dispatcher.resolve_upstream_headers(id_a, oauth_up))
        assert h_oauth == {"Authorization": "Bearer user-oauth-tok-2"}
        assert "authorization_code" in oauth_calls
        assert "refresh_token" in oauth_calls

        # Delete connection
        r_del = client.delete(
            "/me/connections/external_github",
            headers={"Authorization": f"Bearer {user_a_tok}"},
        )
        assert r_del.status_code == 200
        assert r_del.json()["deleted"] is True
