"""Dual-mode Admin authentication: OIDC PKCE + HttpOnly signed session cookies & dev login."""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import logging
import secrets
import time
from typing import TYPE_CHECKING, Any
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from fastapi import APIRouter, Cookie, Header, HTTPException, Query, Response
from fastapi.responses import RedirectResponse
from pydantic import BaseModel

from gateway.auth import DEV_PERSONAS, AuthError, CallerIdentity
from gateway.config import GatewaySettings
from gateway.observability import log_event

if TYPE_CHECKING:
    from gateway.server import GatewayRuntime

ADMIN_SESSION_COOKIE = "mcpcodeaway_admin_session"


def _b64u_encode(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def _b64u_decode(encoded: str) -> bytes:
    padding = "=" * (-len(encoded) % 4)
    return base64.urlsafe_b64decode(encoded + padding)


def _pkce_challenge(verifier: str) -> str:
    return _b64u_encode(hashlib.sha256(verifier.encode("ascii")).digest())


def _safe_next_url(next_url: str | None) -> str:
    if (
        not next_url
        or not next_url.startswith("/")
        or next_url.startswith("//")
        or "\\" in next_url
        or any(ord(ch) < 32 for ch in next_url)
    ):
        return "/dashboard/"
    parsed = urlsplit(next_url)
    if parsed.scheme or parsed.netloc or not parsed.path.startswith("/"):
        return "/dashboard/"
    return next_url


def _access_denied_url(next_url: str | None) -> str:
    parsed = urlsplit(_safe_next_url(next_url))
    query = [(key, value) for key, value in parse_qsl(parsed.query) if key != "auth_error"]
    query.append(("auth_error", "not_authorized"))
    return urlunsplit(("", "", parsed.path, urlencode(query), parsed.fragment))


def _validate_oidc_url(settings: GatewaySettings, target: str | None) -> None:
    if target and settings.ssrf_protection_enabled:
        from gateway.upstream_clients import validate_upstream_url

        validate_upstream_url(
            target,
            app_env=settings.app_env,
            block_private_ips_in_prod=settings.block_private_ips_in_prod,
            allowed_hosts=settings.allowed_upstream_hosts,
        )


def _admin_oidc_scope_value(settings: GatewaySettings) -> str:
    scopes = [*settings.admin_oidc_scopes.split()]
    if settings.admin_oidc_api_scope:
        scopes.extend(settings.admin_oidc_api_scope.split())
    return " ".join(dict.fromkeys(scopes))


class AdminSessionManager:
    """Signs and verifies stateless HttpOnly admin session cookies across rotated keys."""

    def __init__(self, settings: GatewaySettings) -> None:
        self.settings = settings

    def create_session_token(self, identity: CallerIdentity) -> str:
        now = int(time.time())
        session_exp = now + self.settings.admin_session_ttl_seconds
        token_exp = identity.claims.get("exp")
        if isinstance(token_exp, (int, float)):
            session_exp = min(session_exp, int(token_exp))
        payload = {
            "sub": identity.sub,
            "tenant_id": identity.tenant_id,
            "email": identity.email,
            "client_id": identity.client_id,
            "roles": list(identity.roles),
            "groups": list(identity.groups),
            "scopes": list(identity.scopes),
            "client_groups": list(identity.client_groups),
            "exp": session_exp,
        }
        encoded = _b64u_encode(
            json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
        )
        primary = self.settings.admin_session_secrets[0]
        sig = hmac.new(primary.encode("utf-8"), encoded.encode("ascii"), hashlib.sha256).hexdigest()
        return f"{encoded}.{sig}"

    def verify_session_token(self, token: str | None) -> CallerIdentity | None:
        if not token or "." not in token:
            return None
        try:
            encoded, presented_sig = token.split(".", 1)
            matched = any(
                hmac.compare_digest(
                    hmac.new(
                        key.encode("utf-8"), encoded.encode("ascii"), hashlib.sha256
                    ).hexdigest(),
                    presented_sig,
                )
                for key in self.settings.admin_session_secrets
            )
            if not matched:
                return None
            payload = json.loads(_b64u_decode(encoded).decode("utf-8"))
            if int(payload.get("exp", 0)) <= int(time.time()):
                return None
            return CallerIdentity(
                sub=str(payload["sub"]),
                tenant_id=str(payload.get("tenant_id") or ""),
                email=payload.get("email"),
                client_id=payload.get("client_id"),
                roles=list(payload.get("roles") or []),
                groups=list(payload.get("groups") or []),
                scopes=list(payload.get("scopes") or []),
                client_groups=list(payload.get("client_groups") or []),
                claims=payload,
            )
        except Exception:
            return None


class TokenLoginRequest(BaseModel):
    """Bearer JWT exchange payload for admin cookie login."""

    token: str


class DevAdminLoginRequest(BaseModel):
    """Dev-mode persona login payload for admin UI evaluation."""

    persona: str = "gateway_admin"


def create_admin_auth_router(runtime: GatewayRuntime) -> APIRouter:
    """Create OIDC PKCE + HttpOnly session cookie & dev-login endpoints."""
    router = APIRouter(tags=["admin-auth"])
    settings = runtime.settings

    def _set_cookie(response: Response, session_val: str) -> None:
        secure = settings.app_env == "prod" or settings.admin_cookie_samesite == "none"
        response.set_cookie(
            key=ADMIN_SESSION_COOKIE,
            value=session_val,
            max_age=settings.admin_session_ttl_seconds,
            httponly=True,
            samesite=settings.admin_cookie_samesite,
            secure=secure,
            path="/",
        )

    def _clear_cookie(response: Response) -> None:
        secure = settings.app_env == "prod" or settings.admin_cookie_samesite == "none"
        response.delete_cookie(
            key=ADMIN_SESSION_COOKIE,
            httponly=True,
            samesite=settings.admin_cookie_samesite,
            secure=secure,
            path="/",
        )

    def _require_admin(identity: CallerIdentity) -> None:
        allowed = set(settings.admin_roles) | set(settings.viewer_roles)
        if not (set(identity.roles) & allowed):
            log_event(
                "auth.denied",
                level=logging.WARNING,
                surface="admin_login",
                status_code=403,
                reason="missing_admin_role",
            )
            raise HTTPException(
                status_code=403,
                detail="Admin role required for control-plane UI access.",
            )

    def _effective_oidc_endpoints() -> tuple[str, str, str]:
        dev_enabled = bool(settings.auth_mode == "dev" and settings.active_features.dev_auth)
        mock_oidc_base = "http://127.0.0.1:8001/oidc"
        auth_ep = settings.admin_oidc_authorization_endpoint or (
            f"{mock_oidc_base}/authorize" if dev_enabled else ""
        )
        token_ep = settings.oidc_token_endpoint or (
            f"{mock_oidc_base}/token" if dev_enabled else ""
        )
        issuer = (
            mock_oidc_base
            if dev_enabled and not settings.admin_oidc_authorization_endpoint
            else settings.oidc_issuer
        )
        return auth_ep, token_ep, issuer

    @router.get("/auth/admin/config")
    async def admin_auth_config() -> dict[str, Any]:
        dev_enabled = bool(settings.auth_mode == "dev" and settings.active_features.dev_auth)
        auth_ep, token_ep, issuer = _effective_oidc_endpoints()
        oidc_enabled = bool(auth_ep and token_ep)
        return {
            "auth_mode": settings.auth_mode,
            "profile": settings.effective_profile_name,
            "admin_auth_required": settings.admin_auth_required,
            "oidc_enabled": oidc_enabled,
            "oidc_issuer": issuer,
            "dev_auth_enabled": dev_enabled,
            "token_login_enabled": settings.app_env != "prod",
            "dev_personas": sorted(DEV_PERSONAS) if dev_enabled else [],
        }

    @router.get("/auth/admin/login")
    async def admin_oidc_login(
        next_url: str = Query(default="/dashboard/"),
    ) -> RedirectResponse:
        auth_ep, token_ep, _ = _effective_oidc_endpoints()
        if not (auth_ep and token_ep):
            raise HTTPException(
                status_code=400,
                detail="admin_oidc_authorization_endpoint and oidc_token_endpoint required",
            )
        _validate_oidc_url(settings, auth_ep)
        verifier = secrets.token_urlsafe(48)
        state = secrets.token_urlsafe(24)
        redirect_uri = (
            settings.admin_oidc_redirect_uri
            or f"{settings.gateway_public_url.rstrip('/')}/auth/admin/callback"
        )
        await runtime.cache.set(
            f"pkce:{state}",
            json.dumps({"code_verifier": verifier, "next_url": _safe_next_url(next_url)}),
            ttl_seconds=600,
        )
        params = urlencode(
            {
                "response_type": "code",
                "client_id": settings.oidc_client_id,
                "redirect_uri": redirect_uri,
                "scope": _admin_oidc_scope_value(settings),
                "state": state,
                "code_challenge": _pkce_challenge(verifier),
                "code_challenge_method": "S256",
            }
        )
        sep = "&" if "?" in auth_ep else "?"
        return RedirectResponse(
            url=f"{auth_ep}{sep}{params}",
            status_code=302,
        )

    @router.get("/auth/admin/callback")
    async def admin_oidc_callback(
        code: str = Query(...),
        state: str = Query(...),
    ) -> RedirectResponse:
        raw_state = await runtime.cache.get(f"pkce:{state}")
        await runtime.cache.delete(f"pkce:{state}")
        if not raw_state:
            raise HTTPException(status_code=400, detail="Invalid or expired OIDC state")
        pkce_data = json.loads(raw_state)
        _, token_ep, _ = _effective_oidc_endpoints()
        if not token_ep:
            raise HTTPException(status_code=500, detail="oidc_token_endpoint not configured")
        _validate_oidc_url(settings, token_ep)

        redirect_uri = (
            settings.admin_oidc_redirect_uri
            or f"{settings.gateway_public_url.rstrip('/')}/auth/admin/callback"
        )
        form_data = {
            "grant_type": "authorization_code",
            "code": code,
            "redirect_uri": redirect_uri,
            "client_id": settings.oidc_client_id,
            "scope": _admin_oidc_scope_value(settings),
            "code_verifier": pkce_data["code_verifier"],
        }
        if settings.oidc_client_secret:
            form_data["client_secret"] = settings.oidc_client_secret

        http = runtime.validator._http_client or runtime.client_manager._get_client()
        token_resp = await http.post(
            token_ep,
            data=form_data,
            follow_redirects=False,
        )
        if token_resp.status_code >= 400:
            raise HTTPException(
                status_code=502,
                detail=f"OIDC authorization code exchange failed ({token_resp.status_code})",
            )
        token_body = token_resp.json()
        raw_jwt = token_body.get("access_token")
        if not raw_jwt:
            raise HTTPException(
                status_code=502,
                detail="OIDC token response missing gateway API access token",
            )
        try:
            identity = await runtime.validator.validate_token(str(raw_jwt))
        except AuthError as exc:
            raise HTTPException(status_code=exc.status_code, detail=exc.detail) from exc
        try:
            _require_admin(identity)
        except HTTPException as exc:
            if exc.status_code != 403:
                raise
            redirect = RedirectResponse(
                url=_access_denied_url(pkce_data.get("next_url")),
                status_code=303,
            )
            _clear_cookie(redirect)
            return redirect

        session_tok = runtime.admin_sessions.create_session_token(identity)
        redirect = RedirectResponse(url=_safe_next_url(pkce_data.get("next_url")), status_code=302)
        _set_cookie(redirect, session_tok)
        return redirect

    if settings.app_env != "prod":

        @router.post("/auth/admin/token-login")
        async def admin_token_login(
            body: TokenLoginRequest,
            response: Response,
        ) -> dict[str, Any]:
            try:
                identity = await runtime.validator.validate_token(body.token.strip())
            except AuthError as exc:
                raise HTTPException(status_code=exc.status_code, detail=exc.detail) from exc
            _require_admin(identity)
            session_tok = runtime.admin_sessions.create_session_token(identity)
            _set_cookie(response, session_tok)
            return {"authenticated": True, "user": identity.public_profile()}

    if settings.auth_mode == "dev" and settings.active_features.dev_auth:

        @router.post("/auth/admin/dev-login")
        async def admin_dev_login(
            body: DevAdminLoginRequest,
            response: Response,
        ) -> dict[str, Any]:
            if runtime.dev_keys is None:
                raise HTTPException(status_code=500, detail="Dev key manager unavailable")
            if body.persona not in DEV_PERSONAS:
                raise HTTPException(status_code=400, detail=f"Unknown persona '{body.persona}'")
            token = runtime.dev_keys.issue_user_token(persona=body.persona)
            try:
                identity = await runtime.validator.validate_token(token)
            except AuthError as exc:
                raise HTTPException(status_code=exc.status_code, detail=exc.detail) from exc
            _require_admin(identity)
            session_tok = runtime.admin_sessions.create_session_token(identity)
            _set_cookie(response, session_tok)
            return {
                "authenticated": True,
                "access_token": token,
                "user": identity.public_profile(),
            }

    @router.get("/auth/admin/session")
    async def admin_session_info(
        authorization: str | None = Header(default=None),
        mcpcodeaway_admin_session: str | None = Cookie(default=None),
    ) -> dict[str, Any]:
        identity: CallerIdentity | None = None
        if authorization:
            try:
                identity = await runtime.validator.validate_authorization_header(authorization)
            except AuthError:
                identity = None
        if identity is None and mcpcodeaway_admin_session:
            identity = runtime.admin_sessions.verify_session_token(mcpcodeaway_admin_session)
        if identity is None:
            return {
                "authenticated": False,
                "user": None,
                "is_admin": False,
                "can_access_admin_console": False,
                "admin_auth_required": settings.admin_auth_required,
            }
        is_admin = bool(set(identity.roles) & set(settings.admin_roles))
        can_access_admin_console = is_admin or bool(
            set(identity.roles) & set(settings.viewer_roles)
        )
        return {
            "authenticated": True,
            "user": identity.public_profile(),
            "is_admin": is_admin,
            "can_access_admin_console": can_access_admin_console,
            "admin_auth_required": settings.admin_auth_required,
        }

    @router.post("/auth/admin/logout")
    async def admin_logout(response: Response) -> dict[str, bool]:
        response.delete_cookie(key=ADMIN_SESSION_COOKIE, path="/")
        return {"logged_out": True}

    return router
