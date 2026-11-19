"""RFC 9728 / RFC 8414 OAuth discovery and local dev OIDC token endpoints."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Header, HTTPException, Response
from pydantic import BaseModel, Field

from gateway.auth import (
    DEV_PERSONAS,
    AuthError,
    LocalDevKeyManager,
    OidcTokenValidator,
)
from gateway.config import GatewaySettings


class DevTokenRequest(BaseModel):
    """Request body for minting local dev OIDC caller JWTs."""

    persona: str | None = "finance_analyst"
    sub: str | None = None
    tenant_id: str | None = None
    email: str | None = None
    client_id: str | None = None
    roles: list[str] | None = None
    groups: list[str] | None = None
    scopes: list[str] | None = None
    audience: str | None = None
    ttl_seconds: int = Field(default=3600, ge=1, le=86400)


def create_oauth_discovery_router(
    settings: GatewaySettings,
    dev_keys: LocalDevKeyManager | None,
    validator: OidcTokenValidator,
) -> APIRouter:
    """Build vendor-neutral OAuth 2.1 discovery and dev OIDC token routes."""
    router = APIRouter(tags=["oauth-discovery"])

    @router.get("/.well-known/oauth-protected-resource")
    async def oauth_protected_resource() -> dict[str, Any]:
        return {
            "resource": settings.oidc_audience,
            "authorization_servers": [settings.oidc_issuer.rstrip("/")],
            "scopes_supported": [
                "mcp.tools",
                "finance.read",
                "ops.read",
                "ops.write",
            ],
            "bearer_methods_supported": ["header"],
        }

    @router.get("/.well-known/oauth-authorization-server")
    async def oauth_authorization_server() -> dict[str, Any]:
        base = settings.gateway_public_url.rstrip("/")
        issuer = settings.oidc_issuer.rstrip("/")
        jwks_uri = (
            f"{base}/dev/oidc/jwks"
            if settings.auth_mode == "dev"
            else (settings.oidc_jwks_uri or f"{issuer}/.well-known/jwks.json")
        )
        token_endpoint = (
            f"{base}/dev/auth/token"
            if settings.auth_mode == "dev"
            else (settings.oidc_token_endpoint or f"{issuer}/token")
        )
        return {
            "issuer": issuer,
            "jwks_uri": jwks_uri,
            "token_endpoint": token_endpoint,
            "grant_types_supported": [
                "authorization_code",
                "refresh_token",
                "urn:ietf:params:oauth:grant-type:token-exchange",
            ],
            "response_types_supported": ["code"],
            "token_endpoint_auth_methods_supported": ["client_secret_post"],
        }

    if settings.auth_mode == "dev" and settings.active_features.dev_auth:

        @router.get("/dev/oidc/jwks")
        async def dev_oidc_jwks() -> dict[str, Any]:
            if dev_keys is None:
                raise HTTPException(status_code=404, detail="Dev OIDC endpoints disabled")
            return dev_keys.jwks()

        @router.post("/dev/auth/token")
        async def dev_issue_token(body: DevTokenRequest) -> dict[str, Any]:
            if dev_keys is None:
                raise HTTPException(status_code=404, detail="Dev OIDC endpoints disabled")
            if body.persona and body.persona not in DEV_PERSONAS:
                raise HTTPException(
                    status_code=400,
                    detail=f"Unknown dev persona '{body.persona}'. "
                    f"Choose one of: {', '.join(sorted(DEV_PERSONAS))}",
                )
            overrides: dict[str, Any] = {}
            for field_name in (
                "sub",
                "tenant_id",
                "email",
                "client_id",
                "roles",
                "groups",
                "scopes",
            ):
                val = getattr(body, field_name)
                if val is not None:
                    overrides[field_name] = val

            token = dev_keys.issue_user_token(
                persona=body.persona,
                audience=body.audience or settings.oidc_audience,
                ttl_seconds=body.ttl_seconds,
                overrides=overrides or None,
            )
            return {
                "access_token": token,
                "token_type": "Bearer",
                "expires_in": body.ttl_seconds,
                "persona": body.persona,
            }

    @router.get("/auth/me")
    async def auth_me(
        response: Response,
        authorization: str | None = Header(default=None),
    ) -> dict[str, Any]:
        try:
            identity = await validator.validate_authorization_header(authorization)
        except AuthError as exc:
            headers = {"WWW-Authenticate": exc.www_authenticate} if exc.www_authenticate else None
            raise HTTPException(
                status_code=exc.status_code, detail=exc.detail, headers=headers
            ) from exc
        response.headers["Cache-Control"] = "no-store"
        return identity.public_profile()

    return router
