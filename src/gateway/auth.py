"""Vendor-agnostic OIDC validation, team/agent RBAC policy engine, and delegated token exchange."""

from __future__ import annotations

import hashlib
import json
import time
import uuid
from typing import Any

import httpx
import jwt
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from jwt.algorithms import RSAAlgorithm
from pydantic import BaseModel, Field

from gateway.config import ClaimMappingConfig, GatewaySettings, UpstreamConfig
from gateway.state import CacheBackend


class AuthError(Exception):
    """Authentication or authorization error mapped cleanly to HTTP responses."""

    def __init__(
        self, status_code: int, detail: str, www_authenticate: str | None = None
    ) -> None:
        super().__init__(detail)
        self.status_code = status_code
        self.detail = detail
        self.www_authenticate = www_authenticate


class CallerIdentity(BaseModel):
    """Verified identity extracted from the incoming OIDC Bearer JWT."""

    sub: str
    tenant_id: str
    email: str | None = None
    client_id: str | None = None
    groups: list[str] = Field(default_factory=list)
    roles: list[str] = Field(default_factory=list)
    scopes: list[str] = Field(default_factory=list)
    client_groups: list[str] = Field(default_factory=list)
    raw_token: str = Field(default="", exclude=True)
    claims: dict[str, Any] = Field(default_factory=dict)

    def public_profile(self) -> dict[str, Any]:
        """Return identity metadata without raw bearer assertion."""
        return {
            "sub": self.sub,
            "tenant_id": self.tenant_id,
            "email": self.email,
            "client_id": self.client_id,
            "groups": self.groups,
            "roles": self.roles,
            "scopes": self.scopes,
            "client_groups": self.client_groups,
        }


DEV_PERSONAS: dict[str, dict[str, Any]] = {
    "finance_analyst": {
        "sub": "user-fin-01",
        "tenant_id": "tenant-acme-dev",
        "email": "alice.finance@acme.example",
        "client_id": "copilot-finance-agent",
        "roles": ["finance_analyst", "snowflake_reader", "billing_reader"],
        "groups": ["finance"],
        "scopes": ["mcp.tools", "finance.read"],
    },
    "ops_engineer": {
        "sub": "user-ops-01",
        "tenant_id": "tenant-acme-dev",
        "email": "bob.ops@acme.example",
        "client_id": "ops-remediation-agent",
        "roles": ["ops_engineer", "ticketing_writer"],
        "groups": ["operations"],
        "scopes": ["mcp.tools", "ops.read", "ops.write"],
    },
    "readonly_auditor": {
        "sub": "user-aud-01",
        "tenant_id": "tenant-acme-dev",
        "email": "carol.audit@acme.example",
        "client_id": "audit-copilot",
        "roles": ["readonly_auditor", "snowflake_reader", "billing_reader"],
        "groups": ["audit"],
        "scopes": ["mcp.tools", "finance.read"],
    },
    "support_agent": {
        "sub": "user-sup-01",
        "tenant_id": "tenant-acme-dev",
        "email": "dave.support@acme.example",
        "client_id": "support-triage-agent",
        "roles": ["support_agent"],
        "groups": ["support"],
        "scopes": ["mcp.tools", "ops.read"],
    },
    "eu_accountant": {
        "sub": "user-fin-eu-01",
        "tenant_id": "tenant-acme-dev",
        "email": "elena.eu@acme.example",
        "client_id": "copilot-finance-agent",
        "roles": ["finance_analyst", "snowflake_reader", "billing_reader"],
        "groups": ["finance"],
        "scopes": ["mcp.tools", "finance.read"],
        "client_groups": ["EU"],
    },
    "us_accountant": {
        "sub": "user-fin-us-01",
        "tenant_id": "tenant-acme-dev",
        "email": "frank.us@acme.example",
        "client_id": "copilot-finance-agent",
        "roles": ["finance_analyst", "snowflake_reader", "billing_reader"],
        "groups": ["finance"],
        "scopes": ["mcp.tools", "finance.read"],
        "client_groups": ["US"],
    },
    "gateway_admin": {
        "sub": "user-admin-01",
        "tenant_id": "tenant-acme-dev",
        "email": "grace.admin@acme.example",
        "client_id": "platform-control-plane",
        "roles": ["gateway_admin", "platform_admin"],
        "groups": ["platform-admins"],
        "scopes": ["mcp.tools", "mcp.admin", "mcp.audit"],
        "client_groups": ["ALL"],
    },
}


class LocalDevKeyManager:
    """Generates and manages an ephemeral RS256 keypair for offline OIDC & delegation testing."""

    def __init__(self, issuer: str = "http://127.0.0.1:8000/dev/oidc") -> None:
        self.issuer = issuer
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

    def jwks(self) -> dict[str, Any]:
        """Export public key in standard JWKS format."""
        jwk = json.loads(RSAAlgorithm.to_jwk(self._public_key))
        jwk.update({"use": "sig", "alg": "RS256", "kid": self.kid})
        return {"keys": [jwk]}

    def issue_user_token(
        self,
        *,
        persona: str | None = None,
        audience: str = "api://mcp-code-gateway",
        ttl_seconds: int = 3600,
        overrides: dict[str, Any] | None = None,
    ) -> str:
        """Sign an RS256 caller JWT matching a named persona or custom claims."""
        base = dict(DEV_PERSONAS[persona]) if persona else dict(DEV_PERSONAS["finance_analyst"])
        if overrides:
            base.update(overrides)
        now = int(time.time())
        payload: dict[str, Any] = {
            "iss": self.issuer,
            "aud": audience,
            "sub": base["sub"],
            "tid": base["tenant_id"],
            "email": base.get("email"),
            "azp": base.get("client_id"),
            "roles": list(base.get("roles", [])),
            "groups": list(base.get("groups", [])),
            "client_groups": list(base.get("client_groups", [])),
            "scp": " ".join(base.get("scopes", [])),
            "iat": now,
            "exp": now + ttl_seconds,
            "jti": str(uuid.uuid4()),
        }
        return jwt.encode(
            payload,
            self.private_pem,
            algorithm="RS256",
            headers={"kid": self.kid},
        )

    def issue_delegated_token(
        self,
        *,
        identity: CallerIdentity,
        audience: str,
        scope: str,
        actor_client_id: str,
        ttl_seconds: int = 300,
    ) -> str:
        """Sign a scoped downstream RS256 JWT preserving user identity and gateway actor claim."""
        now = int(time.time())
        payload: dict[str, Any] = {
            "iss": self.issuer,
            "aud": audience,
            "sub": identity.sub,
            "tid": identity.tenant_id,
            "email": identity.email,
            "roles": list(identity.roles),
            "groups": list(identity.groups),
            "client_groups": list(identity.client_groups),
            "scp": scope,
            "azp": identity.client_id,
            "act": {"sub": actor_client_id},
            "iat": now,
            "exp": now + ttl_seconds,
            "jti": str(uuid.uuid4()),
        }
        return jwt.encode(
            payload,
            self.private_pem,
            algorithm="RS256",
            headers={"kid": self.kid},
        )


def _extract_str_list(claims: dict[str, Any], primary: str, *fallbacks: str) -> list[str]:
    for key in (primary, *fallbacks):
        value = claims.get(key)
        if value is None:
            continue
        if isinstance(value, str):
            return [item for item in value.split() if item]
        if isinstance(value, (list, tuple)):
            return [str(item) for item in value if str(item)]
    return []


def _extract_first_str(claims: dict[str, Any], primary: str, *fallbacks: str) -> str | None:
    for key in (primary, *fallbacks):
        value = claims.get(key)
        if isinstance(value, str) and value:
            return value
    return None


def claims_to_identity(
    claims: dict[str, Any], mapping: ClaimMappingConfig, raw_token: str = ""
) -> CallerIdentity:
    """Map raw JWT claims to a vendor-agnostic CallerIdentity."""
    sub = _extract_first_str(claims, mapping.subject, "sub")
    if not sub:
        raise AuthError(401, "Token missing subject claim")
    tenant_id = _extract_first_str(claims, mapping.tenant, "tid", "tenant_id") or ""
    email = _extract_first_str(claims, mapping.email, "email", "upn", "preferred_username")
    client_id = _extract_first_str(claims, mapping.client_id, "azp", "client_id", "appid")
    roles = _extract_str_list(claims, mapping.roles, "roles")
    groups = _extract_str_list(claims, mapping.groups, "groups")
    scopes = _extract_str_list(claims, mapping.scopes, "scp", "scope")
    client_groups = _extract_str_list(
        claims, mapping.client_groups, "client_groups", "allowed_regions", "regions"
    )
    return CallerIdentity(
        sub=sub,
        tenant_id=tenant_id,
        email=email,
        client_id=client_id,
        groups=groups,
        roles=roles,
        scopes=scopes,
        client_groups=client_groups,
        raw_token=raw_token,
        claims=claims,
    )


class OidcTokenValidator:
    """Validates OAuth 2.1 / OIDC Bearer JWTs using JWKS or local dev keys."""

    def __init__(
        self,
        settings: GatewaySettings,
        dev_keys: LocalDevKeyManager | None = None,
        http_client: httpx.AsyncClient | None = None,
    ) -> None:
        self.settings = settings
        self.dev_keys = dev_keys
        self._http_client = http_client
        self._jwks_cache: dict[str, Any] | None = None
        self._jwks_expires_at: float = 0.0

    def www_authenticate_header(self) -> str:
        """Build RFC 9728 WWW-Authenticate header pointing to resource metadata."""
        base = self.settings.gateway_public_url.rstrip("/")
        return f'Bearer resource_metadata="{base}/.well-known/oauth-protected-resource"'

    async def _fetch_jwks(self, force_refresh: bool = False) -> dict[str, Any]:
        if (
            not force_refresh
            and self._jwks_cache is not None
            and time.monotonic() < self._jwks_expires_at
        ):
            return self._jwks_cache
        if not self.settings.oidc_jwks_uri:
            raise AuthError(500, "OIDC JWKS URI is not configured")
        if self._http_client is not None:
            resp = await self._http_client.get(self.settings.oidc_jwks_uri)
        else:
            async with httpx.AsyncClient(timeout=10.0) as client:
                resp = await client.get(self.settings.oidc_jwks_uri)
        resp.raise_for_status()
        self._jwks_cache = resp.json()
        self._jwks_expires_at = time.monotonic() + 300
        return self._jwks_cache

    async def _resolve_public_key(self, token: str) -> Any:
        if self.settings.auth_mode == "dev":
            if self.dev_keys is None:
                raise AuthError(500, "Dev key manager not initialized")
            return self.dev_keys.public_pem

        header = jwt.get_unverified_header(token)
        kid = header.get("kid")
        jwks = await self._fetch_jwks()
        key = self._match_jwk(jwks, kid)
        if key is None:
            jwks = await self._fetch_jwks(force_refresh=True)
            key = self._match_jwk(jwks, kid)
        if key is None:
            raise AuthError(
                401,
                "Signing key not found in JWKS",
                www_authenticate=self.www_authenticate_header(),
            )
        return RSAAlgorithm.from_jwk(json.dumps(key))

    @staticmethod
    def _match_jwk(jwks: dict[str, Any], kid: str | None) -> dict[str, Any] | None:
        keys = jwks.get("keys", [])
        if kid is None and len(keys) == 1:
            return keys[0]
        for entry in keys:
            if entry.get("kid") == kid:
                return entry
        return None

    async def validate_token(self, token: str) -> CallerIdentity:
        """Decode and validate an OIDC JWT and enforce tenant allowlisting."""
        www_auth = self.www_authenticate_header()
        try:
            key = await self._resolve_public_key(token)
            claims = jwt.decode(
                token,
                key=key,
                algorithms=["RS256"],
                audience=self.settings.oidc_audience,
                issuer=self.settings.oidc_issuer,
                options={"require": ["exp", "sub"]},
            )
        except AuthError:
            raise
        except jwt.PyJWTError as exc:
            raise AuthError(401, f"Invalid bearer token: {exc}", www_authenticate=www_auth) from exc

        identity = claims_to_identity(claims, self.settings.claim_mapping, raw_token=token)
        if (
            self.settings.allowed_tenants
            and identity.tenant_id not in self.settings.allowed_tenants
        ):
            raise AuthError(403, f"Tenant '{identity.tenant_id}' is not permitted")
        return identity

    async def validate_authorization_header(self, auth_header: str | None) -> CallerIdentity:
        """Parse and validate an HTTP Authorization: Bearer <jwt> header."""
        www_auth = self.www_authenticate_header()
        if not auth_header:
            raise AuthError(401, "Missing Authorization header", www_authenticate=www_auth)
        parts = auth_header.split(" ", 1)
        if len(parts) != 2 or parts[0].lower() != "bearer" or not parts[1].strip():
            raise AuthError(
                401, "Authorization header must use Bearer token", www_authenticate=www_auth
            )
        return await self.validate_token(parts[1].strip())


class PolicyEvaluator:
    """Gateway policy enforcement point for team/role/client/environment RBAC."""

    def __init__(self, settings: GatewaySettings) -> None:
        self.settings = settings

    def can_access_upstream(
        self, identity: CallerIdentity, upstream: UpstreamConfig
    ) -> tuple[bool, str]:
        if not upstream.enabled:
            return False, f"Upstream '{upstream.name}' is disabled"
        if upstream.allowed_roles and not (set(identity.roles) & set(upstream.allowed_roles)):
            return False, f"Caller lacks required role for '{upstream.name}'"
        if upstream.allowed_groups and not (set(identity.groups) & set(upstream.allowed_groups)):
            return False, f"Caller lacks required group for '{upstream.name}'"
        if upstream.allowed_clients and identity.client_id not in upstream.allowed_clients:
            return (
                False,
                f"Calling client '{identity.client_id}' is not allowed for '{upstream.name}'",
            )
        if upstream.allowed_scopes and not (set(identity.scopes) & set(upstream.allowed_scopes)):
            return False, f"Caller lacks required scope for '{upstream.name}'"
        return True, "allowed"

    def can_execute_tool(
        self,
        identity: CallerIdentity,
        upstream: UpstreamConfig,
        tool_name: str,
        is_mutating: bool = False,
    ) -> tuple[bool, str]:
        """Authorize a specific tool call, enforcing write roles/scopes and read-only prod."""
        allowed, reason = self.can_access_upstream(identity, upstream)
        if not allowed:
            return False, reason

        effective_mutating = is_mutating or (tool_name in upstream.mutating_tools)
        if not effective_mutating:
            return True, "allowed"

        if self.settings.app_env == "prod" and self.settings.read_only_prod:
            return False, "Mutating tools are blocked in read-only prod environment"

        has_write_role = any(
            role == "admin" or role.endswith("_writer") for role in identity.roles
        )
        has_write_scope = any(
            scope == "mcp.write" or scope.endswith(".write") for scope in identity.scopes
        )
        if not has_write_role or not has_write_scope:
            return False, f"Caller lacks write role/scope for mutating tool '{tool_name}'"
        return True, "allowed"


class TokenExchangeResult(BaseModel):
    """Metadata and bearer token returned by delegated token exchange."""

    access_token: str
    audience: str
    scope: str
    dialect: str
    cache_hit: bool
    expires_in: int


class DelegatedTokenExchanger:
    """Exchanges user assertions for scoped downstream tokens via RFC 8693, Entra OBO, or dev."""

    def __init__(
        self,
        settings: GatewaySettings,
        cache: CacheBackend,
        dev_keys: LocalDevKeyManager | None = None,
        http_client: httpx.AsyncClient | None = None,
    ) -> None:
        self.settings = settings
        self.cache = cache
        self.dev_keys = dev_keys
        self._http_client = http_client

    async def exchange(
        self,
        identity: CallerIdentity,
        audience: str,
        scope: str,
    ) -> TokenExchangeResult:
        dialect = self.settings.token_exchange_dialect
        cache_key = f"delegated:{identity.tenant_id}:{identity.sub}:{audience}:{scope}"
        cached = await self.cache.get(cache_key)
        if cached is not None:
            return TokenExchangeResult(
                access_token=cached,
                audience=audience,
                scope=scope,
                dialect=dialect,
                cache_hit=True,
                expires_in=self.settings.token_cache_ttl_seconds,
            )

        if dialect == "dev_local":
            if self.dev_keys is None:
                raise AuthError(500, "Dev key manager required for dev_local token exchange")
            ttl = self.settings.token_cache_ttl_seconds
            access_token = self.dev_keys.issue_delegated_token(
                identity=identity,
                audience=audience,
                scope=scope,
                actor_client_id=self.settings.oidc_client_id,
                ttl_seconds=ttl,
            )
            expires_in = ttl
        else:
            access_token, expires_in = await self._exchange_http(
                dialect=dialect,
                identity=identity,
                audience=audience,
                scope=scope,
            )

        cache_ttl = min(max(expires_in - 30, 1), self.settings.token_cache_ttl_seconds)
        await self.cache.set(cache_key, access_token, ttl_seconds=cache_ttl)
        return TokenExchangeResult(
            access_token=access_token,
            audience=audience,
            scope=scope,
            dialect=dialect,
            cache_hit=False,
            expires_in=expires_in,
        )

    async def _exchange_http(
        self,
        *,
        dialect: str,
        identity: CallerIdentity,
        audience: str,
        scope: str,
    ) -> tuple[str, int]:
        if not self.settings.oidc_token_endpoint:
            raise AuthError(500, "oidc_token_endpoint must be set for HTTP token exchange")
        if dialect == "rfc8693":
            form_data = {
                "grant_type": "urn:ietf:params:oauth:grant-type:token-exchange",
                "subject_token": identity.raw_token,
                "subject_token_type": "urn:ietf:params:oauth:token-type:access_token",
                "audience": audience,
                "scope": scope,
                "client_id": self.settings.oidc_client_id,
                "client_secret": self.settings.oidc_client_secret,
            }
        elif dialect == "entra_v2_obo":
            form_data = {
                "grant_type": "urn:ietf:params:oauth:grant-type:jwt-bearer",
                "assertion": identity.raw_token,
                "requested_token_use": "on_behalf_of",
                "scope": scope,
                "client_id": self.settings.oidc_client_id,
                "client_secret": self.settings.oidc_client_secret,
            }
        else:
            raise AuthError(500, f"Unsupported token exchange dialect '{dialect}'")

        if self._http_client is not None:
            resp = await self._http_client.post(self.settings.oidc_token_endpoint, data=form_data)
        else:
            async with httpx.AsyncClient(timeout=10.0) as client:
                resp = await client.post(self.settings.oidc_token_endpoint, data=form_data)

        if resp.status_code >= 400:
            raise AuthError(502, f"Token exchange failed ({resp.status_code}): {resp.text}")
        body = resp.json()
        token = body.get("access_token")
        if not token:
            raise AuthError(502, "Token exchange response missing access_token")
        expires_in = int(body.get("expires_in", self.settings.token_cache_ttl_seconds))
        return str(token), expires_in
