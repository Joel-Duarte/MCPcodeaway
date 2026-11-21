"""Self-hosted mock OIDC/OAuth 2.1 IdP for Entra v2 OBO, Keycloak RFC 8693, and External OAuth."""

from __future__ import annotations

import base64
import hashlib
import json
import secrets
import time
from typing import Any
from urllib.parse import urlencode

import jwt
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from fastapi import FastAPI, Form, HTTPException, Query
from fastapi.responses import RedirectResponse
from jwt.algorithms import RSAAlgorithm

ENTRA_ISSUER = "https://mock-idp.local/entra/v2.0"
KEYCLOAK_ISSUER = "https://mock-idp.local/keycloak/realms/enterprise"
EXTERNAL_ISSUER = "https://mock-idp.local/external"


class MockIdpState:
    """Holds RS256 signing keys, registered clients, and ephemeral PKCE/token state."""

    def __init__(self) -> None:
        self._private_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        self._public_key = self._private_key.public_key()
        self.private_pem = self._private_key.private_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PrivateFormat.PKCS8,
            encryption_algorithm=serialization.NoEncryption(),
        ).decode("utf-8")
        public_der = self._public_key.public_bytes(
            encoding=serialization.Encoding.DER,
            format=serialization.PublicFormat.SubjectPublicKeyInfo,
        )
        self.public_pem = self._public_key.public_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PublicFormat.SubjectPublicKeyInfo,
        ).decode("utf-8")
        self.kid = hashlib.sha256(public_der).hexdigest()[:16]

        self.auth_codes: dict[str, dict[str, Any]] = {}
        self.external_tokens: dict[str, dict[str, Any]] = {}
        self.refresh_tokens: dict[str, dict[str, Any]] = {}
        self.current_external_user: str = "user-fin-01"

    def jwks(self) -> dict[str, Any]:
        jwk = json.loads(RSAAlgorithm.to_jwk(self._public_key))
        jwk.update({"use": "sig", "alg": "RS256", "kid": self.kid})
        return {"keys": [jwk]}

    def sign_jwt(self, payload: dict[str, Any]) -> str:
        return jwt.encode(
            payload,
            self.private_pem,
            algorithm="RS256",
            headers={"kid": self.kid},
        )

    def verify_jwt(
        self, token: str, *, audience: str | None = None, issuer: str | None = None
    ) -> dict[str, Any]:
        return jwt.decode(
            token,
            key=self.public_pem,
            algorithms=["RS256"],
            audience=audience,
            issuer=issuer,
            options={"verify_aud": audience is not None},
        )

    def issue_entra_token(
        self,
        *,
        sub: str,
        tenant_id: str = "tenant-acme-entra",
        email: str = "alice@acme.example",
        client_id: str = "copilot-finance-agent",
        roles: list[str] | None = None,
        groups: list[str] | None = None,
        scopes: list[str] | None = None,
        client_groups: list[str] | None = None,
        audience: str = "api://mcp-code-gateway",
        ttl_seconds: int = 3600,
    ) -> str:
        now = int(time.time())
        return self.sign_jwt(
            {
                "iss": ENTRA_ISSUER,
                "aud": audience,
                "sub": sub,
                "tid": tenant_id,
                "email": email,
                "azp": client_id,
                "roles": roles or [],
                "groups": groups or [],
                "scp": " ".join(scopes or ["mcp.tools"]),
                "client_groups": client_groups or [],
                "iat": now,
                "exp": now + ttl_seconds,
            }
        )

    def issue_keycloak_token(
        self,
        *,
        sub: str,
        org_id: str = "enterprise",
        email: str = "bob@acme.example",
        client_id: str = "claude-code",
        roles: list[str] | None = None,
        groups: list[str] | None = None,
        scopes: list[str] | None = None,
        client_groups: list[str] | None = None,
        audience: str = "api://mcp-code-gateway",
        ttl_seconds: int = 3600,
    ) -> str:
        now = int(time.time())
        return self.sign_jwt(
            {
                "iss": KEYCLOAK_ISSUER,
                "aud": audience,
                "sub": sub,
                "org_id": org_id,
                "email": email,
                "azp": client_id,
                "roles": roles or [],
                "groups": groups or [],
                "scope": " ".join(scopes or ["mcp.tools"]),
                "client_groups": client_groups or [],
                "iat": now,
                "exp": now + ttl_seconds,
            }
        )


def create_mock_idp_app(state: MockIdpState | None = None) -> FastAPI:
    """Create a FastAPI app simulating Entra v2, Keycloak RFC 8693, and External OAuth."""
    idp = state or MockIdpState()
    app = FastAPI(title="Mock Multi-IdP Server", version="1.0.0")
    app.state.idp = idp

    @app.get("/entra/v2.0/.well-known/openid-configuration")
    def entra_discovery() -> dict[str, Any]:
        return {
            "issuer": ENTRA_ISSUER,
            "jwks_uri": f"{ENTRA_ISSUER}/jwks",
            "token_endpoint": f"{ENTRA_ISSUER}/token",
        }

    @app.get("/entra/v2.0/jwks")
    @app.get("/keycloak/realms/enterprise/protocol/openid-connect/certs")
    def jwks() -> dict[str, Any]:
        return idp.jwks()

    @app.get("/keycloak/realms/enterprise/.well-known/openid-configuration")
    def keycloak_discovery() -> dict[str, Any]:
        return {
            "issuer": KEYCLOAK_ISSUER,
            "jwks_uri": f"{KEYCLOAK_ISSUER}/protocol/openid-connect/certs",
            "token_endpoint": f"{KEYCLOAK_ISSUER}/protocol/openid-connect/token",
        }

    @app.post("/entra/v2.0/token")
    def entra_token(
        grant_type: str = Form(...),
        assertion: str | None = Form(default=None),
        requested_token_use: str | None = Form(default=None),
        scope: str | None = Form(default=None),
        client_id: str = Form(...),
        client_secret: str | None = Form(default=None),
    ) -> dict[str, Any]:
        if grant_type == "client_credentials":
            if not client_secret:
                raise HTTPException(status_code=401, detail="client_secret required")
            token = idp.issue_entra_token(
                sub=f"sp-{client_id}",
                client_id=client_id,
                roles=["m2m_reader"],
                scopes=(scope or "m2m.read").split(),
                audience="api://metrics_m2m",
                ttl_seconds=300,
            )
            return {"access_token": token, "token_type": "Bearer", "expires_in": 300}

        if (
            grant_type == "urn:ietf:params:oauth:grant-type:jwt-bearer"
            and requested_token_use == "on_behalf_of"
        ):
            if not assertion:
                raise HTTPException(status_code=400, detail="assertion required")
            try:
                claims = idp.verify_jwt(assertion, issuer=ENTRA_ISSUER)
            except jwt.PyJWTError as exc:
                raise HTTPException(status_code=401, detail=f"Invalid assertion: {exc}") from exc
            target_aud = "api://inventory_obo"
            if scope and "/" in scope:
                target_aud = scope.rsplit("/", 1)[0]
            down_token = idp.issue_entra_token(
                sub=str(claims["sub"]),
                tenant_id=str(claims.get("tid", "")),
                email=str(claims.get("email", "")),
                client_id=client_id,
                roles=list(claims.get("roles", [])),
                groups=list(claims.get("groups", [])),
                scopes=(scope or "mcp.tools").split(),
                client_groups=list(claims.get("client_groups", [])),
                audience=target_aud,
                ttl_seconds=300,
            )
            return {"access_token": down_token, "token_type": "Bearer", "expires_in": 300}

        raise HTTPException(status_code=400, detail=f"Unsupported Entra grant_type '{grant_type}'")

    @app.post("/keycloak/realms/enterprise/protocol/openid-connect/token")
    def keycloak_token(
        grant_type: str = Form(...),
        subject_token: str | None = Form(default=None),
        audience: str | None = Form(default=None),
        scope: str | None = Form(default=None),
        client_id: str = Form(...),
        client_secret: str | None = Form(default=None),
    ) -> dict[str, Any]:
        if grant_type == "client_credentials":
            if not client_secret:
                raise HTTPException(status_code=401, detail="client_secret required")
            token = idp.issue_keycloak_token(
                sub=f"sa-{client_id}",
                client_id=client_id,
                roles=["m2m_reader"],
                scopes=(scope or "m2m.read").split(),
                audience=audience or "api://metrics_m2m",
                ttl_seconds=300,
            )
            return {"access_token": token, "token_type": "Bearer", "expires_in": 300}

        if grant_type == "urn:ietf:params:oauth:grant-type:token-exchange":
            if not subject_token:
                raise HTTPException(status_code=400, detail="subject_token required")
            claims: dict[str, Any] | None = None
            for iss in (KEYCLOAK_ISSUER, ENTRA_ISSUER):
                try:
                    claims = idp.verify_jwt(subject_token, issuer=iss)
                    break
                except jwt.PyJWTError:
                    continue
            if claims is None:
                raise HTTPException(status_code=401, detail="Invalid subject_token")
            down_token = idp.issue_keycloak_token(
                sub=str(claims["sub"]),
                org_id=str(claims.get("org_id") or claims.get("tid") or "enterprise"),
                email=str(claims.get("email", "")),
                client_id=client_id,
                roles=list(claims.get("roles", [])),
                groups=list(claims.get("groups", [])),
                scopes=(scope or "mcp.tools").split(),
                client_groups=list(claims.get("client_groups", [])),
                audience=audience or "api://tax_keycloak",
                ttl_seconds=300,
            )
            return {"access_token": down_token, "token_type": "Bearer", "expires_in": 300}

        raise HTTPException(
            status_code=400, detail=f"Unsupported Keycloak grant_type '{grant_type}'"
        )

    @app.get("/external/oauth/authorize")
    def external_authorize(
        response_type: str = Query(...),
        client_id: str = Query(...),
        redirect_uri: str = Query(...),
        state: str = Query(...),
        code_challenge: str | None = Query(default=None),
        code_challenge_method: str | None = Query(default=None),
    ) -> RedirectResponse:
        if response_type != "code":
            raise HTTPException(status_code=400, detail="response_type must be code")
        code = f"extcode_{secrets.token_urlsafe(12)}"
        idp.auth_codes[code] = {
            "client_id": client_id,
            "redirect_uri": redirect_uri,
            "code_challenge": code_challenge,
            "code_challenge_method": code_challenge_method,
            "user": idp.current_external_user,
        }
        sep = "&" if "?" in redirect_uri else "?"
        return RedirectResponse(
            url=f"{redirect_uri}{sep}{urlencode({'code': code, 'state': state})}",
            status_code=302,
        )

    @app.post("/external/oauth/token")
    def external_token(
        grant_type: str = Form(...),
        code: str | None = Form(default=None),
        code_verifier: str | None = Form(default=None),
        refresh_token: str | None = Form(default=None),
        client_id: str = Form(...),
    ) -> dict[str, Any]:
        if grant_type == "authorization_code":
            record = idp.auth_codes.pop(code or "", None)
            if record is None or record["client_id"] != client_id:
                raise HTTPException(status_code=400, detail="Invalid authorization code")
            challenge = record.get("code_challenge")
            if challenge:
                if not code_verifier:
                    raise HTTPException(status_code=400, detail="code_verifier required")
                digest = hashlib.sha256(code_verifier.encode("ascii")).digest()
                computed = base64.urlsafe_b64encode(digest).rstrip(b"=").decode("ascii")
                if computed != challenge:
                    raise HTTPException(status_code=400, detail="PKCE code_verifier mismatch")
            user = str(record["user"])
            access = f"ext_acc_{secrets.token_urlsafe(12)}"
            refresh = f"ext_ref_{secrets.token_urlsafe(12)}"
            idp.external_tokens[access] = {"user": user}
            idp.refresh_tokens[refresh] = {"user": user, "client_id": client_id}
            return {
                "access_token": access,
                "refresh_token": refresh,
                "token_type": "Bearer",
                "expires_in": 10,
            }

        if grant_type == "refresh_token":
            rec = idp.refresh_tokens.pop(refresh_token or "", None)
            if rec is None or rec["client_id"] != client_id:
                raise HTTPException(status_code=400, detail="Invalid refresh_token")
            user = str(rec["user"])
            access = f"ext_acc_rot_{secrets.token_urlsafe(12)}"
            new_refresh = f"ext_ref_rot_{secrets.token_urlsafe(12)}"
            idp.external_tokens[access] = {"user": user}
            idp.refresh_tokens[new_refresh] = {"user": user, "client_id": client_id}
            return {
                "access_token": access,
                "refresh_token": new_refresh,
                "token_type": "Bearer",
                "expires_in": 3600,
            }

        raise HTTPException(status_code=400, detail=f"Unsupported external grant '{grant_type}'")

    return app
