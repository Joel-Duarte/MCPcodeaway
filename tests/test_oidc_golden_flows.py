"""Golden gateway journeys against the local OIDC provider contract."""

from __future__ import annotations

from urllib.parse import parse_qs, urlencode, urlparse, urlunparse

import httpx
import pytest
from fastapi import FastAPI

from examples.mock_backends import create_mock_backends_app
from gateway.config import GatewaySettings
from gateway.server import create_gateway_app


def _provider_transport(provider: FastAPI) -> httpx.MockTransport:
    async def dispatch(request: httpx.Request) -> httpx.Response:
        if request.url.host != "mock-idp.local":
            return httpx.Response(404, json={"error": "unknown_test_host"})
        transport = httpx.ASGITransport(app=provider)
        async with httpx.AsyncClient(
            transport=transport,
            base_url="https://mock-idp.local",
            follow_redirects=False,
        ) as client:
            return await client.request(
                request.method,
                str(request.url),
                headers=dict(request.headers),
                content=request.content,
                follow_redirects=False,
            )

    return httpx.MockTransport(dispatch)


async def _run_oidc_login(
    gateway: httpx.AsyncClient,
    provider: httpx.AsyncClient,
    *,
    persona: str | None = None,
) -> tuple[httpx.Response, str]:
    login = await gateway.get("/auth/admin/login?next_url=/dashboard/", follow_redirects=False)
    assert login.status_code == httpx.codes.FOUND
    authorization_url = urlparse(login.headers["location"])
    authorization_query = parse_qs(authorization_url.query)
    assert authorization_query["response_type"] == ["code"]
    assert authorization_query["client_id"] == ["mcp-readiness"]
    assert authorization_query["redirect_uri"] == ["https://gateway.test/auth/admin/callback"]
    assert authorization_query["code_challenge_method"] == ["S256"]
    state = authorization_query["state"][0]
    if persona:
        authorization_query["persona"] = [persona]
    authorization_url = authorization_url._replace(query=urlencode(authorization_query, doseq=True))
    authorization = await provider.get(urlunparse(authorization_url), follow_redirects=False)
    assert authorization.status_code == httpx.codes.FOUND
    callback_url = urlparse(authorization.headers["location"])
    callback_query = parse_qs(callback_url.query)
    assert callback_query["state"] == [state]
    callback = await gateway.get(
        f"{callback_url.path}?{callback_url.query}", follow_redirects=False
    )
    return callback, f"{callback_url.path}?{callback_url.query}"


@pytest.mark.asyncio
async def test_oidc_auth_code_pkce_admin_session_golden(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    issuer = "https://mock-idp.local/oidc"
    redirect_uri = "https://gateway.test/auth/admin/callback"
    monkeypatch.setenv("MOCK_OIDC_ISSUER", issuer)
    monkeypatch.delenv("MOCK_OIDC_PRIVATE_KEY_PATH", raising=False)
    monkeypatch.setenv("MOCK_OIDC_AUDIENCE", "api://mcp-readiness")
    monkeypatch.setenv("MOCK_OIDC_CLIENT_ID", "mcp-readiness")
    monkeypatch.setenv("MOCK_OIDC_REDIRECT_URI", redirect_uri)
    provider_app = create_mock_backends_app()
    outbound = httpx.AsyncClient(transport=_provider_transport(provider_app))
    settings = GatewaySettings(
        app_env="sandbox",
        auth_mode="oidc",
        gateway_public_url="https://gateway.test",
        oidc_issuer=issuer,
        oidc_jwks_uri=f"{issuer}/jwks",
        oidc_audience="api://mcp-readiness",
        oidc_token_endpoint=f"{issuer}/token",
        oidc_client_id="mcp-readiness",
        admin_oidc_authorization_endpoint=f"{issuer}/authorize",
        admin_oidc_redirect_uri=redirect_uri,
        admin_oidc_scopes="openid mcp.admin",
        admin_oidc_api_scope="mcp.admin",
        allowed_tenants=["tenant-acme-dev"],
        require_admin_auth=True,
        admin_session_secret="local-oidc-golden-session-key",
        sqlite_path=str(tmp_path / "oidc-golden.db"),
        upstreams_config_path=str(tmp_path / "unconfigured-upstreams.yaml"),
    )
    app = create_gateway_app(settings=settings, http_client=outbound, preloaded_tools={})
    runtime = app.state.runtime
    await runtime.startup()
    try:
        async with (
            httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app), base_url="https://gateway.test"
            ) as gateway,
            httpx.AsyncClient(
                transport=httpx.ASGITransport(app=provider_app),
                base_url="https://mock-idp.local",
                follow_redirects=False,
            ) as provider,
        ):
            admin_callback, callback_path = await _run_oidc_login(gateway, provider)
            assert admin_callback.status_code == httpx.codes.FOUND
            assert admin_callback.headers["location"] == "/dashboard/"
            session = await gateway.get("/auth/admin/session")
            assert session.status_code == httpx.codes.OK
            assert session.json()["authenticated"] is True
            assert session.json()["is_admin"] is True
            assert session.json()["user"]["groups"] == ["platform-admins"]
            assert (await gateway.get("/admin/upstreams")).status_code == httpx.codes.OK

            replay = await gateway.get(callback_path, follow_redirects=False)
            assert replay.status_code == httpx.codes.BAD_REQUEST

            await gateway.post("/auth/admin/logout")
            denied_callback, _ = await _run_oidc_login(
                gateway, provider, persona="readiness-finance"
            )
            assert denied_callback.status_code == httpx.codes.SEE_OTHER
            assert denied_callback.headers["location"] == "/dashboard/?auth_error=not_authorized"
            denied_session = await gateway.get("/auth/admin/session")
            assert denied_session.json()["authenticated"] is False
    finally:
        await runtime.shutdown()
        await outbound.aclose()
