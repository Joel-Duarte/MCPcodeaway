"""Deterministic authorization cache expiry and replica-freshness checks."""

from __future__ import annotations

import logging
import time

import httpx
import jwt
import pytest

from gateway.auth import (
    AuthError,
    EntraGroupMembershipResolver,
    LocalDevKeyManager,
    OidcTokenValidator,
    PolicyEvaluator,
    claims_to_identity,
)
from gateway.config import ClaimMappingConfig, GatewaySettings, UpstreamConfig
from gateway.server import GatewayRuntime
from gateway.state import InMemoryCacheBackend


def _signed_token(keys: LocalDevKeyManager, settings: GatewaySettings) -> str:
    now = int(time.time())
    return jwt.encode(
        {
            "iss": settings.oidc_issuer,
            "aud": settings.oidc_audience,
            "sub": "freshness-user",
            "tid": "tenant-a",
            "oid": "object-a",
            "iat": now,
            "exp": now + 3600,
            "hasgroups": True,
        },
        keys.private_pem,
        algorithm="RS256",
        headers={"kid": keys.kid},
    )


@pytest.mark.asyncio
async def test_jwks_rotation_is_independent_per_validator_and_expires(monkeypatch) -> None:
    clock = [100.0]
    monkeypatch.setattr("gateway.auth.time.monotonic", lambda: clock[0])
    settings = GatewaySettings(
        auth_mode="oidc",
        oidc_issuer="https://idp.example.com",
        oidc_jwks_uri="https://idp.example.com/jwks",
        allowed_tenants=["tenant-a"],
    )
    original = LocalDevKeyManager(issuer=settings.oidc_issuer)
    rotated = LocalDevKeyManager(issuer=settings.oidc_issuer)
    current_jwks = [original.jwks()]
    requests = [0]

    def handler(_request: httpx.Request) -> httpx.Response:
        requests[0] += 1
        return httpx.Response(200, json=current_jwks[0])

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        first_runtime = GatewayRuntime(settings=settings, http_client=client)
        second_runtime = GatewayRuntime(settings=settings, http_client=client)
        first = first_runtime.validator
        second = second_runtime.validator
        old_token = _signed_token(original, settings)
        await first.validate_token(old_token)
        await second.validate_token(old_token)
        assert requests[0] == 2

        current_jwks[0] = rotated.jwks()
        new_token = _signed_token(rotated, settings)
        await first.validate_token(new_token)
        assert requests[0] == 3
        with pytest.raises(AuthError, match="Signing key not found"):
            await first.validate_token(old_token)
        assert requests[0] == 4

        await second.validate_token(old_token)
        assert requests[0] == 4
        clock[0] = 400.0
        with pytest.raises(AuthError, match="Signing key not found"):
            await second.validate_token(old_token)
        assert requests[0] == 6
        await second.validate_token(new_token)
        assert requests[0] == 6


@pytest.mark.asyncio
async def test_expired_jwks_refresh_outage_fails_closed(monkeypatch, caplog) -> None:
    clock = [1.0]
    monkeypatch.setattr("gateway.auth.time.monotonic", lambda: clock[0])
    settings = GatewaySettings(
        auth_mode="oidc",
        oidc_issuer="https://idp.example.com",
        oidc_jwks_uri="https://idp.example.com/jwks",
        allowed_tenants=["tenant-a"],
    )
    keys = LocalDevKeyManager(issuer=settings.oidc_issuer)
    fail = [False]

    def handler(_request: httpx.Request) -> httpx.Response:
        if fail[0]:
            return httpx.Response(503)
        return httpx.Response(200, json=keys.jwks())

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        validator = OidcTokenValidator(settings, http_client=client)
        token = _signed_token(keys, settings)
        await validator.validate_token(token)
        clock[0] = 301.0
        fail[0] = True
        caplog.set_level(logging.INFO, logger="mcpcodeaway")
        with pytest.raises(httpx.HTTPStatusError):
            await validator.validate_token(token)
    assert "auth.jwks_refresh_failed" in caplog.text
    assert "idp.example.com" not in caplog.text


@pytest.mark.asyncio
async def test_validator_suppresses_graph_challenge_and_denies_incomplete_identity() -> None:
    settings = GatewaySettings(
        auth_mode="oidc",
        oidc_issuer="https://idp.example.com",
        oidc_jwks_uri="https://idp.example.com/jwks",
        allowed_tenants=["tenant-a"],
    )
    keys = LocalDevKeyManager(issuer=settings.oidc_issuer)

    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=keys.jwks())

    async def failed_group_resolution(_identity):
        raise AuthError(
            403,
            "Microsoft Graph group resolution failed (401)",
            www_authenticate='Bearer claims="challenge"',
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        validator = OidcTokenValidator(
            settings,
            http_client=client,
            group_resolver=failed_group_resolution,
        )
        identity = await validator.validate_token(_signed_token(keys, settings))

    assert identity.groups == []
    assert not identity.group_membership_complete
    allowed, _ = PolicyEvaluator(settings).can_access_upstream(
        identity,
        UpstreamConfig(
            name="restricted",
            type="mcp_http",
            url="https://api.example.test/mcp",
            allowed_groups=["approved-group"],
        ),
    )
    assert not allowed


@pytest.mark.asyncio
async def test_group_membership_removal_converges_per_replica_at_cache_expiry(
    monkeypatch,
) -> None:
    clock = [100.0]
    monkeypatch.setattr("gateway.state.time.monotonic", lambda: clock[0])
    settings = GatewaySettings(
        entra_group_overage_resolution_enabled=True,
        entra_group_cache_ttl_seconds=30,
        entra_graph_scope="https://graph.microsoft.com/.default",
        oidc_token_endpoint="https://idp.example.com/token",
        oidc_client_id="freshness-test-client",
        oidc_client_secret="freshness-test-secret",
    )
    groups: list[list[str]] = [[]]
    fetches = [0]

    class Exchanger:
        async def exchange(self, *_args, **_kwargs):
            return type("Token", (), {"access_token": "fixture-token"})()

    class Graph:
        async def _request(self, *_args, **_kwargs):
            fetches[0] += 1
            return httpx.Response(200, json={"value": [{"id": item} for item in groups[0]]})

    runtimes = [GatewayRuntime(settings=settings), GatewayRuntime(settings=settings)]
    resolvers = [
        EntraGroupMembershipResolver(
            settings=settings,
            exchanger=Exchanger(),
            cache=runtime.cache,
            upstream_client=Graph(),
        )
        for runtime in runtimes
    ]
    for runtime, resolver in zip(runtimes, resolvers, strict=True):
        runtime.validator.group_resolver = resolver
    identities = [
        claims_to_identity({"sub": "u", "tid": "tenant-a", "oid": "object-a"}, ClaimMappingConfig())
        for _ in runtimes
    ]
    upstream = UpstreamConfig(
        name="restricted",
        type="mcp_http",
        url="https://api.example.test/mcp",
        allowed_groups=["approved-group"],
    )
    for resolver, identity in zip(resolvers, identities, strict=True):
        identity.groups = await resolver(identity)
        assert not PolicyEvaluator(settings).can_access_upstream(identity, upstream)[0]
    assert fetches[0] == 2

    groups[0] = ["approved-group"]
    clock[0] = 129.0
    for resolver, identity in zip(resolvers, identities, strict=True):
        identity.groups = await resolver(identity)
        assert not PolicyEvaluator(settings).can_access_upstream(identity, upstream)[0]
    assert fetches[0] == 2

    clock[0] = 130.0
    for resolver, identity in zip(resolvers, identities, strict=True):
        identity.groups = await resolver(identity)
        assert PolicyEvaluator(settings).can_access_upstream(identity, upstream)[0]
    assert fetches[0] == 4

    groups[0] = []
    clock[0] = 159.0
    for resolver, identity in zip(resolvers, identities, strict=True):
        identity.groups = await resolver(identity)
        assert PolicyEvaluator(settings).can_access_upstream(identity, upstream)[0]
    assert fetches[0] == 4

    clock[0] = 160.0
    for resolver, identity in zip(resolvers, identities, strict=True):
        identity.groups = await resolver(identity)
        assert not PolicyEvaluator(settings).can_access_upstream(identity, upstream)[0]
    assert fetches[0] == 6


@pytest.mark.asyncio
@pytest.mark.parametrize("status", [401, 403, 429, 500])
async def test_graph_failures_leave_group_overage_incomplete_and_denied(status: int) -> None:
    settings = GatewaySettings(
        entra_graph_scope="https://graph.microsoft.com/.default",
    )

    class Exchanger:
        async def exchange(self, *_args, **_kwargs):
            return type("Token", (), {"access_token": "fixture-token"})()

    class Graph:
        async def _request(self, *_args, **_kwargs):
            headers = {"WWW-Authenticate": 'Bearer claims="challenge"'} if status == 401 else {}
            return httpx.Response(status, headers=headers)

    resolver = EntraGroupMembershipResolver(
        settings=settings,
        exchanger=Exchanger(),
        cache=InMemoryCacheBackend(),
        upstream_client=Graph(),
    )
    identity = claims_to_identity(
        {"sub": "u", "tid": "tenant-a", "oid": "object-a", "hasgroups": True},
        ClaimMappingConfig(),
    )
    with pytest.raises(AuthError) as caught:
        await resolver(identity)
    if status == 401:
        assert caught.value.www_authenticate == 'Bearer claims="challenge"'
    identity.group_membership_complete = False
    assert not PolicyEvaluator(settings).can_access_upstream(
        identity,
        UpstreamConfig(
            name="restricted",
            type="mcp_http",
            url="https://api.example.test/mcp",
            allowed_groups=["approved-group"],
        ),
    )[0]


@pytest.mark.asyncio
async def test_graph_malformed_response_and_timeout_do_not_return_stale_groups() -> None:
    settings = GatewaySettings(entra_graph_scope="https://graph.microsoft.com/.default")

    class Exchanger:
        async def exchange(self, *_args, **_kwargs):
            return type("Token", (), {"access_token": "fixture-token"})()

    identity = claims_to_identity(
        {"sub": "u", "tid": "tenant-a", "oid": "object-a"}, ClaimMappingConfig()
    )

    class MalformedGraph:
        async def _request(self, *_args, **_kwargs):
            return httpx.Response(200, json={"value": "invalid"})

    resolver = EntraGroupMembershipResolver(
        settings=settings,
        exchanger=Exchanger(),
        cache=InMemoryCacheBackend(),
        upstream_client=MalformedGraph(),
    )
    with pytest.raises(AuthError, match="malformed"):
        await resolver(identity)

    class TimeoutGraph:
        async def _request(self, *_args, **_kwargs):
            raise httpx.ReadTimeout("fixture timeout")

    resolver = EntraGroupMembershipResolver(
        settings=settings,
        exchanger=Exchanger(),
        cache=InMemoryCacheBackend(),
        upstream_client=TimeoutGraph(),
    )
    with pytest.raises(httpx.ReadTimeout):
        await resolver(identity)


@pytest.mark.asyncio
async def test_expired_group_cache_refresh_failure_does_not_use_stale_membership(
    monkeypatch,
) -> None:
    clock = [100.0]
    monkeypatch.setattr("gateway.state.time.monotonic", lambda: clock[0])
    settings = GatewaySettings(entra_group_cache_ttl_seconds=30)
    cache = InMemoryCacheBackend()
    await cache.set("entra_groups:tenant-a:object-a", '["approved-group"]', 30)

    class Exchanger:
        async def exchange(self, *_args, **_kwargs):
            return type("Token", (), {"access_token": "fixture-token"})()

    class Graph:
        async def _request(self, *_args, **_kwargs):
            return httpx.Response(503)

    resolver = EntraGroupMembershipResolver(
        settings=settings,
        exchanger=Exchanger(),
        cache=cache,
        upstream_client=Graph(),
    )
    identity = claims_to_identity(
        {"sub": "u", "tid": "tenant-a", "oid": "object-a"}, ClaimMappingConfig()
    )
    clock[0] = 130.0
    with pytest.raises(AuthError, match="503"):
        await resolver(identity)


def test_admin_session_claims_and_source_expiry_hold_across_gateway_runtimes(monkeypatch) -> None:
    clock = [1_000]
    monkeypatch.setattr("gateway.admin_auth.time.time", lambda: clock[0])
    settings = GatewaySettings(admin_session_secret="session-key-a", admin_session_ttl_seconds=600)
    runtimes = [GatewayRuntime(settings=settings), GatewayRuntime(settings=settings)]
    identity = claims_to_identity(
        {
            "sub": "admin",
            "tid": "tenant-a",
            "roles": ["gateway_admin"],
            "groups": ["sensitive-operators"],
            "exp": 1_040,
        },
        ClaimMappingConfig(),
    )
    token = runtimes[0].admin_sessions.create_session_token(identity)
    identity.roles = ["viewer"]
    identity.groups = []
    changed = runtimes[1].admin_sessions.verify_session_token(token)
    assert changed is not None and changed.roles == ["gateway_admin"]
    assert changed.groups == ["sensitive-operators"]

    rotated_runtime = GatewayRuntime(
        settings=GatewaySettings(
            admin_session_secret="session-key-b", admin_session_ttl_seconds=600
        )
    )
    assert rotated_runtime.admin_sessions.verify_session_token(token) is None
    overlapping_runtime = GatewayRuntime(
        settings=GatewaySettings(
            admin_session_secret="session-key-b,session-key-a", admin_session_ttl_seconds=600
        )
    )
    assert overlapping_runtime.admin_sessions.verify_session_token(token) is not None
    clock[0] = 1_040
    assert runtimes[0].admin_sessions.verify_session_token(token) is None
    assert runtimes[1].admin_sessions.verify_session_token(token) is None
