"""Self-service per-user OAuth 2.0 PKCE and API-key credential linking routes."""

from __future__ import annotations

import base64
import hashlib
import html
import json
import secrets
import time
from typing import Any
from urllib.parse import quote, urlencode

import httpx
from fastapi import APIRouter, Form, Header, HTTPException, Query, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from pydantic import BaseModel

from gateway.auth import AuthError, CallerIdentity, OidcTokenValidator, PolicyEvaluator
from gateway.config import GatewaySettings, UpstreamConfig, UpstreamRegistry
from gateway.crypto import CryptoError, sign_connect_ticket, verify_connect_ticket
from gateway.secrets import CredentialVault, SecretResolver
from gateway.state import CacheBackend
from gateway.upstream_clients import validate_upstream_url


class UserSecretPayload(BaseModel):
    """JSON payload for saving a per-user API key / PAT."""

    api_key: str
    ticket: str | None = None


def _pkce_challenge(verifier: str) -> str:
    digest = hashlib.sha256(verifier.encode("ascii")).digest()
    return base64.urlsafe_b64encode(digest).rstrip(b"=").decode("ascii")


def _render_html(title: str, body_html: str) -> HTMLResponse:
    page = f"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>{html.escape(title)} — MCPcodeaway</title>
<style>
  body {{
    font-family: system-ui, -apple-system, sans-serif;
    background: #f8fafc; color: #0f172a; margin: 0; padding: 32px 16px;
  }}
  .card {{
    max-width: 460px; margin: 40px auto; background: #fff;
    border: 1px solid #e2e8f0; border-radius: 10px; padding: 24px;
  }}
  h1 {{ font-size: 18px; margin: 0 0 8px; }}
  p {{ font-size: 14px; color: #475569; line-height: 1.5; margin: 0 0 16px; }}
  label {{ display: block; font-size: 13px; font-weight: 600; margin-bottom: 6px; }}
  input[type=password] {{
    width: 100%; box-sizing: border-box; padding: 9px 11px;
    border: 1px solid #cbd5e1; border-radius: 6px; font-size: 14px; margin-bottom: 14px;
  }}
  button {{
    background: #0f172a; color: #fff; border: 0; border-radius: 6px;
    padding: 9px 16px; font-size: 14px; font-weight: 600; cursor: pointer;
  }}
</style>
</head>
<body><div class="card">{body_html}</div></body>
</html>"""
    return HTMLResponse(
        content=page,
        headers={
            "Cache-Control": "no-store",
            "Content-Security-Policy": (
                "default-src 'none'; style-src 'unsafe-inline'; form-action 'self'"
            ),
        },
    )


def create_user_connect_router(
    *,
    settings: GatewaySettings,
    registry: UpstreamRegistry,
    validator: OidcTokenValidator,
    policy: PolicyEvaluator,
    cache: CacheBackend,
    vault: CredentialVault,
    secret_resolver: SecretResolver,
    connect_signing_key: bytes,
    http_client: httpx.AsyncClient | None = None,
    session_resolver: Any | None = None,
) -> APIRouter:
    """Create routes for per-user OAuth2 PKCE linking and personal API key storage."""
    router = APIRouter(tags=["user-connections"])

    async def _resolve_caller(
        request: Request,
        authorization: str | None,
    ) -> CallerIdentity:
        if authorization:
            try:
                return await validator.validate_authorization_header(authorization)
            except AuthError as exc:
                raise HTTPException(status_code=exc.status_code, detail=exc.detail) from exc
        if session_resolver is not None:
            session_identity = await session_resolver(request)
            if session_identity is not None:
                return session_identity
        raise HTTPException(status_code=401, detail="Authentication required")

    def _get_per_user_upstream(name: str) -> UpstreamConfig:
        upstream = registry.get(name)
        if upstream is None or not upstream.enabled:
            raise HTTPException(status_code=404, detail=f"Upstream '{name}' not found")
        if upstream.auth_type not in ("user_oauth", "user_api_key"):
            raise HTTPException(
                status_code=400,
                detail=f"Upstream '{name}' does not use per-user credentials",
            )
        return upstream

    def _verify_ticket_or_401(ticket: str, upstream_name: str) -> tuple[str, str]:
        try:
            payload = verify_connect_ticket(
                signing_key=connect_signing_key,
                ticket=ticket,
                expected_upstream=upstream_name,
            )
        except CryptoError as exc:
            raise HTTPException(status_code=401, detail=str(exc)) from exc
        return str(payload["tid"]), str(payload["sub"])

    @router.get("/auth/connect/{upstream_name}")
    async def start_user_connect(
        upstream_name: str,
        ticket: str = Query(..., min_length=10),
    ) -> Any:
        tenant_id, user_sub = _verify_ticket_or_401(ticket, upstream_name)
        upstream = _get_per_user_upstream(upstream_name)

        if upstream.auth_type == "user_api_key":
            esc_up = html.escape(upstream.name, quote=True)
            esc_ticket = html.escape(ticket, quote=True)
            action_url = html.escape(
                f"/auth/connect/{quote(upstream.name, safe='')}/secret", quote=True
            )
            return _render_html(
                f"Connect {upstream.name}",
                f"<h1>Connect {esc_up}</h1>"
                "<p>Enter your personal API key or Personal Access Token. "
                "It will be encrypted at rest and never exposed to the AI agent.</p>"
                f'<form method="post" action="{action_url}">'
                f'<input type="hidden" name="ticket" value="{esc_ticket}">'
                '<label for="api_key">Personal API Key / Token</label>'
                '<input id="api_key" name="api_key" type="password" required autocomplete="off">'
                '<button type="submit">Save Encrypted Credential</button>'
                "</form>",
            )

        oauth = upstream.oauth
        if (
            not oauth
            or not oauth.authorize_endpoint
            or not oauth.token_endpoint
            or not oauth.client_id
        ):
            raise HTTPException(
                status_code=500,
                detail=f"Upstream '{upstream_name}' OAuth endpoints are incomplete",
            )
        if settings.ssrf_protection_enabled:
            validate_upstream_url(
                oauth.authorize_endpoint,
                app_env=settings.app_env,
                block_private_ips_in_prod=settings.block_private_ips_in_prod,
                allowed_hosts=settings.allowed_upstream_hosts,
            )

        verifier = secrets.token_urlsafe(48)
        state = secrets.token_urlsafe(24)
        await cache.set(
            f"oauth_pkce:{state}",
            json.dumps(
                {"tid": tenant_id, "sub": user_sub, "up": upstream_name, "verifier": verifier}
            ),
            ttl_seconds=600,
        )
        redirect_uri = (
            f"{settings.gateway_public_url.rstrip('/')}/auth/connect/"
            f"{quote(upstream_name, safe='')}/callback"
        )
        params = {
            "response_type": "code",
            "client_id": oauth.client_id,
            "redirect_uri": redirect_uri,
            "state": state,
            "code_challenge": _pkce_challenge(verifier),
            "code_challenge_method": "S256",
        }
        if oauth.scopes:
            params["scope"] = " ".join(oauth.scopes)
        sep = "&" if "?" in oauth.authorize_endpoint else "?"
        return RedirectResponse(
            url=f"{oauth.authorize_endpoint}{sep}{urlencode(params)}", status_code=302
        )

    @router.post("/auth/connect/{upstream_name}/secret")
    async def submit_user_secret_form(
        upstream_name: str,
        ticket: str = Form(...),
        api_key: str = Form(...),
    ) -> HTMLResponse:
        tenant_id, user_sub = _verify_ticket_or_401(ticket, upstream_name)
        upstream = _get_per_user_upstream(upstream_name)
        cleaned = api_key.strip()
        if not cleaned:
            raise HTTPException(status_code=400, detail="API key cannot be empty")
        await vault.save_user_credential(
            tenant_id=tenant_id,
            user_sub=user_sub,
            upstream_name=upstream.name,
            auth_kind="user_api_key",
            payload={"api_key": cleaned},
        )
        return _render_html(
            "Connected",
            f"<h1>{html.escape(upstream.name)} Connected</h1>"
            "<p>Your credential has been encrypted and stored. "
            "You can close this tab and retry your agent request.</p>",
        )

    @router.get("/auth/connect/{upstream_name}/callback")
    async def oauth_connect_callback(
        upstream_name: str,
        code: str = Query(..., min_length=1),
        state: str = Query(..., min_length=8),
    ) -> HTMLResponse:
        cache_key = f"oauth_pkce:{state}"
        raw_state = await cache.get(cache_key)
        if raw_state is None:
            raise HTTPException(status_code=400, detail="Invalid or expired OAuth state")
        await cache.delete(cache_key)
        state_data = json.loads(raw_state)
        if state_data.get("up") != upstream_name:
            raise HTTPException(status_code=400, detail="OAuth state upstream mismatch")

        upstream = _get_per_user_upstream(upstream_name)
        oauth = upstream.oauth
        if not oauth or not oauth.token_endpoint or not oauth.client_id:
            raise HTTPException(status_code=500, detail="Upstream OAuth config incomplete")
        if settings.ssrf_protection_enabled:
            validate_upstream_url(
                oauth.token_endpoint,
                app_env=settings.app_env,
                block_private_ips_in_prod=settings.block_private_ips_in_prod,
                allowed_hosts=settings.allowed_upstream_hosts,
            )

        redirect_uri = (
            f"{settings.gateway_public_url.rstrip('/')}/auth/connect/"
            f"{quote(upstream_name, safe='')}/callback"
        )
        form_data: dict[str, str] = {
            "grant_type": "authorization_code",
            "code": code,
            "redirect_uri": redirect_uri,
            "client_id": oauth.client_id,
            "code_verifier": str(state_data["verifier"]),
        }
        if oauth.client_secret_ref:
            form_data["client_secret"] = await secret_resolver.resolve(
                oauth.client_secret_ref,
                allow_bare_env=True,
                allow_gateway_vault=False,
            )

        if http_client is not None:
            resp = await http_client.post(
                oauth.token_endpoint, data=form_data, follow_redirects=False
            )
        else:
            async with httpx.AsyncClient(timeout=10.0) as client:
                resp = await client.post(
                    oauth.token_endpoint, data=form_data, follow_redirects=False
                )

        if resp.status_code >= 400:
            raise HTTPException(
                status_code=502, detail=f"OAuth token exchange failed ({resp.status_code})"
            )
        body = resp.json()
        access_token = body.get("access_token")
        if not access_token:
            raise HTTPException(
                status_code=502, detail="OAuth provider did not return access_token"
            )

        expires_in = int(body.get("expires_in", 3600))
        expires_at = int(time.time()) + expires_in
        await vault.save_user_credential(
            tenant_id=str(state_data["tid"]),
            user_sub=str(state_data["sub"]),
            upstream_name=upstream.name,
            auth_kind="user_oauth",
            payload={
                "access_token": str(access_token),
                "refresh_token": str(body.get("refresh_token") or ""),
                "token_type": str(body.get("token_type") or "Bearer"),
                "expires_at": expires_at,
            },
            expires_at=expires_at,
        )
        return _render_html(
            "Connected",
            f"<h1>{html.escape(upstream.name)} Connected</h1>"
            "<p>Your OAuth authorization succeeded. You can close this tab and "
            "retry your agent request. Reconnect if this gateway process restarts "
            "or a different replica handles the request.</p>",
        )

    @router.get("/me/connections")
    async def list_my_connections(
        request: Request,
        authorization: str | None = Header(default=None),
    ) -> dict[str, Any]:
        identity = await _resolve_caller(request, authorization)
        stored = {
            item["upstream_name"]: item
            for item in await vault.list_user_credentials(
                tenant_id=identity.tenant_id, user_sub=identity.sub
            )
        }
        now = int(time.time())
        connections: list[dict[str, Any]] = []
        for upstream in registry.list_upstreams():
            if not upstream.enabled or upstream.auth_type not in ("user_oauth", "user_api_key"):
                continue
            allowed, _ = policy.can_access_upstream(identity, upstream)
            if not allowed:
                continue
            rec = stored.get(upstream.name)
            status = "not_connected"
            if rec is not None:
                exp = rec.get("expires_at")
                status = "expired" if isinstance(exp, int) and exp <= now else "connected"
            ticket = sign_connect_ticket(
                signing_key=connect_signing_key,
                sub=identity.sub,
                tenant_id=identity.tenant_id,
                upstream_name=upstream.name,
                ttl_seconds=settings.connect_ticket_ttl_seconds,
            )
            connections.append(
                {
                    "upstream_name": upstream.name,
                    "auth_type": upstream.auth_type,
                    "status": status,
                    "expires_at": rec.get("expires_at") if rec else None,
                    "updated_at": rec.get("updated_at") if rec else None,
                    "connect_url": (
                        f"{settings.gateway_public_url.rstrip('/')}/auth/connect/"
                        f"{quote(upstream.name, safe='')}?ticket={quote(ticket, safe='')}"
                    ),
                }
            )
        return {"connections": connections}

    @router.post("/me/connections/{upstream_name}/secret")
    async def save_my_connection_secret(
        upstream_name: str,
        body: UserSecretPayload,
        request: Request,
        authorization: str | None = Header(default=None),
    ) -> dict[str, Any]:
        if body.ticket:
            tenant_id, user_sub = _verify_ticket_or_401(body.ticket, upstream_name)
        else:
            identity = await _resolve_caller(request, authorization)
            tenant_id, user_sub = identity.tenant_id, identity.sub
        upstream = _get_per_user_upstream(upstream_name)
        cleaned = body.api_key.strip()
        if not cleaned:
            raise HTTPException(status_code=400, detail="api_key cannot be empty")
        await vault.save_user_credential(
            tenant_id=tenant_id,
            user_sub=user_sub,
            upstream_name=upstream.name,
            auth_kind="user_api_key",
            payload={"api_key": cleaned},
        )
        return {"status": "connected", "upstream_name": upstream.name}

    @router.delete("/me/connections/{upstream_name}")
    async def delete_my_connection(
        upstream_name: str,
        request: Request,
        authorization: str | None = Header(default=None),
    ) -> dict[str, Any]:
        identity = await _resolve_caller(request, authorization)
        deleted = await vault.delete_user_credential(
            tenant_id=identity.tenant_id,
            user_sub=identity.sub,
            upstream_name=upstream_name,
        )
        return {"deleted": deleted, "upstream_name": upstream_name}

    return router
