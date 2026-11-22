"""Tests for per-upstream OBO, client_credentials, user_oauth/user_api_key, and /admin/secrets."""

from __future__ import annotations

import base64
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

import httpx
import pytest
from fastapi import FastAPI, Header, HTTPException
from fastapi.testclient import TestClient

from gateway.auth import (
    CallerIdentity,
    DelegatedTokenExchanger,
    LocalDevKeyManager,
    UserCredentialRequiredError,
)
from gateway.config import GatewaySettings, UpstreamConfig, UpstreamOAuthConfig
from gateway.dispatcher import HostDispatcher
from gateway.mcp_protocol import (
    MCP_CLIENT_CAPABILITIES_META,
    MCP_PROTOCOL_VERSION,
    MCP_PROTOCOL_VERSION_META,
    MCP_SERVER_INFO_META,
)
from gateway.server import create_gateway_app
from gateway.state import InMemoryCacheBackend


def _b64_key(seed: bytes) -> str:
    return base64.urlsafe_b64encode(seed * 2).decode("ascii")


def _mcp_user_call(
    client: TestClient, token: str | None, tool_name: str, arguments: dict[str, str]
) -> httpx.Response:
    headers = {
        "Content-Type": "application/json",
        "Accept": "application/json, text/event-stream",
        "MCP-Protocol-Version": MCP_PROTOCOL_VERSION,
        "Mcp-Method": "tools/call",
        "Mcp-Name": tool_name,
    }
    if token:
        headers["Authorization"] = f"Bearer {token}"
    return client.post(
        "/mcp",
        headers=headers,
        json={
            "jsonrpc": "2.0",
            "id": 1,
            "method": "tools/call",
            "params": {
                "name": tool_name,
                "arguments": arguments,
                "_meta": {
                    MCP_PROTOCOL_VERSION_META: MCP_PROTOCOL_VERSION,
                    MCP_CLIENT_CAPABILITIES_META: {},
                    MCP_SERVER_INFO_META: {"name": "personal-key-test", "version": "1.0"},
                },
            },
        },
    )


def test_user_api_key_authz_flow_uses_personal_credential_without_upstream_oidc(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    owners = {"personal-key-a": "user-a", "personal-key-b": "user-b"}
    observed_keys: list[str | None] = []
    api = FastAPI(title="Personal Key API", version="1.0.0")

    @api.get("/notes/{note_id}", operation_id="read_personal_note")
    def read_personal_note(
        note_id: str, x_personal_token: str | None = Header(default=None)
    ) -> dict[str, str]:
        observed_keys.append(x_personal_token)
        owner = owners.get(x_personal_token or "")
        if owner is None:
            raise HTTPException(status_code=401, detail="Invalid personal key")
        if note_id == "restricted" and owner != "user-b":
            raise HTTPException(status_code=403, detail="This note belongs to another user")
        return {"owner": owner, "note_id": note_id}

    async def dispatch(request: httpx.Request) -> httpx.Response:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=api), base_url="https://personal-api.test"
        ) as upstream_client:
            return await upstream_client.request(
                request.method,
                str(request.url),
                headers=dict(request.headers),
                content=request.content,
            )

    upstream_http = httpx.AsyncClient(transport=httpx.MockTransport(dispatch))
    monkeypatch.setenv("SHARED_PERSONAL_KEY", "must-not-be-used-as-fallback")
    cfg = UpstreamConfig(
        name="personal_notes",
        type="openapi",
        url="https://personal-api.test",
        openapi_spec_url="https://personal-api.test/openapi.json",
        auth_type="user_api_key",
        authorization_mode="upstream",
        api_key_header="X-Personal-Token",
        allowed_operations=["read_personal_note"],
        tool_effects={"read_personal_note": "read"},
    )
    upstreams_path = tmp_path / "upstreams.yaml"
    upstreams_path.write_text(
        "upstreams:\n"
        "  - name: personal_notes\n"
        "    type: openapi\n"
        "    url: https://personal-api.test\n"
        "    openapi_spec_url: https://personal-api.test/openapi.json\n"
        "    auth_type: user_api_key\n"
        "    authorization_mode: upstream\n"
        "    api_key_header: X-Personal-Token\n"
        "    allowed_operations: [read_personal_note]\n"
        "    tool_effects: {read_personal_note: read}\n",
        encoding="utf-8",
    )
    settings = GatewaySettings(
        app_env="sandbox",
        auth_mode="dev",
        allowed_tenants=["tenant-test"],
        sqlite_path=str(tmp_path / "gateway.db"),
        upstreams_config_path=str(upstreams_path),
        credential_encryption_key=f"k1:{_b64_key(b'0123456789abcdef')}",
        ssrf_protection_enabled=False,
    )
    dev_keys = LocalDevKeyManager(issuer=settings.oidc_issuer)
    app = create_gateway_app(settings=settings, dev_keys=dev_keys, http_client=upstream_http)
    token_a = dev_keys.issue_user_token(
        persona="finance_analyst",
        overrides={"sub": "user-a", "tenant_id": "tenant-test", "groups": [], "roles": []},
    )
    token_b = dev_keys.issue_user_token(
        persona="finance_analyst",
        overrides={"sub": "user-b", "tenant_id": "tenant-test", "groups": [], "roles": []},
    )
    token_unmapped = dev_keys.issue_user_token(
        persona="finance_analyst",
        overrides={"sub": "user-c", "tenant_id": "tenant-test", "groups": [], "roles": []},
    )
    tool_name = "personal_notes__read_personal_note"

    assert cfg.authorization_mode == "upstream"
    with TestClient(app) as client:
        unauthenticated = _mcp_user_call(client, None, tool_name, {"note_id": "private"})
        assert unauthenticated.status_code == 401

        missing = _mcp_user_call(client, token_unmapped, tool_name, {"note_id": "private"})
        assert "/auth/connect/personal_notes?ticket=" in missing.text
        assert observed_keys == []

        for token, key in ((token_a, "personal-key-a"), (token_b, "personal-key-b")):
            connected = client.post(
                "/me/connections/personal_notes/secret",
                headers={"Authorization": f"Bearer {token}"},
                json={"api_key": key},
            )
            assert connected.status_code == 200

        private_a = _mcp_user_call(
            client,
            token_a,
            tool_name,
            {"note_id": "private", "api_key": "personal-key-b"},
        )
        assert private_a.status_code == 200
        assert "user-a" in private_a.text
        assert "personal-key-b" not in private_a.text
        assert observed_keys[-1] == "personal-key-a"

        forbidden_a = _mcp_user_call(client, token_a, tool_name, {"note_id": "restricted"})
        assert forbidden_a.status_code == 200
        assert "not authorized for this operation" in forbidden_a.text
        assert "another user" not in forbidden_a.text

        allowed_b = _mcp_user_call(client, token_b, tool_name, {"note_id": "restricted"})
        assert allowed_b.status_code == 200
        assert "user-b" in allowed_b.text
        assert observed_keys[-1] == "personal-key-b"

        revoked = client.post(
            "/me/connections/personal_notes/secret",
            headers={"Authorization": f"Bearer {token_a}"},
            json={"api_key": "revoked-personal-key"},
        )
        assert revoked.status_code == 200
        rejected = _mcp_user_call(client, token_a, tool_name, {"note_id": "private"})
        assert rejected.status_code == 200
        assert "connected credential" in rejected.text
        assert "must-not-be-used-as-fallback" not in rejected.text
        assert observed_keys[-1] == "revoked-personal-key"

    assert "must-not-be-used-as-fallback" not in observed_keys


@pytest.mark.parametrize("status_code", [400, 503])
@pytest.mark.asyncio
async def test_failed_user_oauth_refresh_fails_closed_without_leaking_tokens(
    status_code: int,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    monkeypatch.setenv("TEST_USER_OAUTH_CLIENT_SECRET", "client-secret-private")
    request_bodies: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        request_bodies.append(request.content.decode("utf-8"))
        return httpx.Response(status_code, json={"error": "temporarily_unavailable"})

    settings = GatewaySettings(app_env="sandbox", auth_mode="dev", ssrf_protection_enabled=False)
    upstream = UpstreamConfig(
        name="external_oauth",
        type="openapi",
        url="https://api.example.test",
        auth_type="user_oauth",
        oauth=UpstreamOAuthConfig(
            token_endpoint="https://auth.example.test/token",
            client_id="public-client-id",
            client_secret_ref="env://TEST_USER_OAUTH_CLIENT_SECRET",
        ),
    )
    identity = CallerIdentity(sub="user-a", tenant_id="tenant-a", raw_token="caller-token")
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http_client:
        dispatcher = HostDispatcher(
            settings=settings,
            policy=None,
            exchanger=None,
            cache=InMemoryCacheBackend(),
            client_manager=None,
            catalog=None,
            registry=None,
            http_client=http_client,
        )
        with pytest.raises(UserCredentialRequiredError) as error:
            await dispatcher._refresh_user_oauth(
                identity,
                upstream,
                {"access_token": "access-private", "refresh_token": "refresh-private"},
            )

    assert error.value.reason == "refresh_failed"
    assert "access-private" not in str(error.value)
    assert "refresh-private" not in str(error.value)
    assert "client-secret-private" not in caplog.text
    assert "refresh-private" in request_bodies[0]
    assert "client-secret-private" in request_bodies[0]


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

        import asyncio

        id_a = asyncio.run(runtime.validator.validate_token(user_a_tok))
        id_b = asyncio.run(runtime.validator.validate_token(user_b_tok))
        h1, hit1, _ = asyncio.run(runtime.dispatcher.resolve_upstream_headers(id_a, m2m_up))
        h2, hit2, _ = asyncio.run(runtime.dispatcher.resolve_upstream_headers(id_a, m2m_up))
        assert h1 == {"Authorization": "Bearer m2m-tok-1"}
        assert hit1 is False
        assert hit2 is True

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

        with pytest.raises(UserCredentialRequiredError):
            asyncio.run(runtime.dispatcher.resolve_upstream_headers(id_b, pat_up))

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
        stored_oauth = asyncio.run(
            runtime.store.get_user_credential(id_a.tenant_id, id_a.sub, "external_github")
        )
        assert stored_oauth is not None
        assert "user-oauth-tok-1" not in stored_oauth["ciphertext"]

        async def resolve_oauth_twice():
            return await asyncio.gather(
                runtime.dispatcher.resolve_upstream_headers(id_a, oauth_up),
                runtime.dispatcher.resolve_upstream_headers(id_a, oauth_up),
            )

        oauth_results = asyncio.run(resolve_oauth_twice())
        assert [result[0] for result in oauth_results] == [
            {"Authorization": "Bearer user-oauth-tok-2"},
            {"Authorization": "Bearer user-oauth-tok-2"},
        ]
        assert "authorization_code" in oauth_calls
        assert oauth_calls.count("refresh_token") == 1

        r_del = client.delete(
            "/me/connections/external_github",
            headers={"Authorization": f"Bearer {user_a_tok}"},
        )
        assert r_del.status_code == 200
        assert r_del.json()["deleted"] is True
