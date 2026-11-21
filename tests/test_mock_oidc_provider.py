"""Protocol-level checks for the local OIDC provider used by the readiness lab."""

from __future__ import annotations

import base64
import hashlib
from urllib.parse import parse_qs, urlparse

import httpx
import jwt
import pytest
from jwt.algorithms import RSAAlgorithm

from examples.mock_backends import create_mock_backends_app


@pytest.mark.asyncio
async def test_mock_oidc_authorization_code_pkce_and_one_time_use(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    issuer = "http://127.0.0.1:8001/oidc"
    monkeypatch.setenv("MOCK_OIDC_ISSUER", issuer)
    monkeypatch.delenv("MOCK_OIDC_PRIVATE_KEY_PATH", raising=False)
    monkeypatch.setenv("MOCK_OIDC_AUDIENCE", "api://mcp-code-gateway")
    monkeypatch.setenv("MOCK_OIDC_CLIENT_ID", "mcp-readiness")
    monkeypatch.setenv("MOCK_OIDC_REDIRECT_URI", "http://localhost:8080/auth/admin/callback")
    app = create_mock_backends_app()
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://mock-idp.test") as client:
        metadata_response = await client.get("/oidc/.well-known/openid-configuration")
        metadata = metadata_response.json()
        assert metadata_response.status_code == httpx.codes.OK
        assert metadata["issuer"] == issuer
        assert "S256" in metadata["code_challenge_methods_supported"]

        verifier = "readiness-test-verifier-that-is-long-enough-for-pkce-123456"
        challenge = (
            base64.urlsafe_b64encode(hashlib.sha256(verifier.encode("ascii")).digest())
            .rstrip(b"=")
            .decode("ascii")
        )
        auth_params = {
            "response_type": "code",
            "client_id": "mcp-readiness",
            "redirect_uri": "http://localhost:8080/auth/admin/callback",
            "scope": "openid mcp.tools",
            "state": "test-state",
            "nonce": "test-nonce",
            "code_challenge": challenge,
            "code_challenge_method": "S256",
            "persona": "readiness-finance",
        }
        authorization = await client.get("/oidc/authorize", params=auth_params)
        assert authorization.status_code == httpx.codes.FOUND
        callback = parse_qs(urlparse(authorization.headers["location"]).query)
        assert callback["state"] == ["test-state"]
        code = callback["code"][0]

        bad_pkce = await client.post(
            "/oidc/token",
            data={
                "grant_type": "authorization_code",
                "code": code,
                "client_id": "mcp-readiness",
                "redirect_uri": auth_params["redirect_uri"],
                "code_verifier": "wrong-verifier-that-is-long-enough-for-pkce-123456",
            },
        )
        assert bad_pkce.status_code == httpx.codes.BAD_REQUEST

        authorization = await client.get("/oidc/authorize", params=auth_params)
        code = parse_qs(urlparse(authorization.headers["location"]).query)["code"][0]
        token_response = await client.post(
            "/oidc/token",
            data={
                "grant_type": "authorization_code",
                "code": code,
                "client_id": "mcp-readiness",
                "redirect_uri": auth_params["redirect_uri"],
                "code_verifier": verifier,
            },
        )
        assert token_response.status_code == httpx.codes.OK
        tokens = token_response.json()
        jwks = (await client.get("/oidc/jwks")).json()
        public_key = RSAAlgorithm.from_jwk(jwks["keys"][0])
        access_claims = jwt.decode(
            tokens["access_token"],
            public_key,
            algorithms=["RS256"],
            audience="api://mcp-code-gateway",
            issuer=issuer,
        )
        id_claims = jwt.decode(
            tokens["id_token"],
            public_key,
            algorithms=["RS256"],
            audience="mcp-readiness",
            issuer=issuer,
        )
        assert access_claims["sub"] == "readiness-finance"
        assert access_claims["groups"] == ["finance"]
        assert access_claims["roles"] == ["finance_analyst"]
        assert id_claims["nonce"] == "test-nonce"
        missing_userinfo_token = await client.get("/oidc/userinfo")
        assert missing_userinfo_token.status_code == httpx.codes.UNAUTHORIZED
        userinfo = await client.get(
            "/oidc/userinfo",
            headers={"Authorization": f"Bearer {tokens['access_token']}"},
        )
        assert userinfo.status_code == httpx.codes.OK
        assert userinfo.json()["groups"] == ["finance"]

        replay = await client.post(
            "/oidc/token",
            data={
                "grant_type": "authorization_code",
                "code": code,
                "client_id": "mcp-readiness",
                "redirect_uri": auth_params["redirect_uri"],
                "code_verifier": verifier,
            },
        )
        assert replay.status_code == httpx.codes.BAD_REQUEST

        invalid_redirect = await client.get(
            "/oidc/authorize",
            params={**auth_params, "redirect_uri": "https://attacker.example/callback"},
        )
        assert invalid_redirect.status_code == httpx.codes.BAD_REQUEST
