"""Phase 1 tests: OIDC AuthN, team/agent RBAC, delegated token exchange, and mock backends."""

from __future__ import annotations

import json
import time
from urllib.parse import parse_qs

import httpx
import jwt
import pytest
from fastapi import FastAPI

from examples.mock_backends import build_synthetic_tools, create_mock_backends_app
from gateway.auth import (
    AuthError,
    DelegatedTokenExchanger,
    EntraGroupMembershipResolver,
    LocalDevKeyManager,
    OidcTokenValidator,
    PolicyEvaluator,
    claims_to_identity,
)
from gateway.config import ClaimMappingConfig, GatewaySettings, UpstreamConfig, UpstreamRegistry
from gateway.oauth_discovery import create_oauth_discovery_router
from gateway.state import InMemoryCacheBackend, SQLiteAuditStore


def test_local_dev_key_manager_accepts_shared_rsa_private_key() -> None:
    issuer = "http://gateway.test/dev/oidc"
    issuer_keys = LocalDevKeyManager(issuer=issuer)
    replica_keys = LocalDevKeyManager(issuer=issuer, private_key_pem=issuer_keys.private_pem)

    token = issuer_keys.issue_user_token(persona="finance_analyst")
    assert replica_keys.jwks() == issuer_keys.jwks()
    assert (
        jwt.decode(
            token,
            replica_keys.public_pem,
            algorithms=["RS256"],
            audience="api://mcp-code-gateway",
            issuer=issuer,
        )["sub"]
        == "user-fin-01"
    )


@pytest.mark.parametrize(
    ("claims", "mapping", "expected"),
    [
        (
            {
                "sub": "entra-user",
                "tid": "tenant-a",
                "roles": ["finance_reader"],
                "groups": ["finance"],
                "azp": "gateway",
            },
            ClaimMappingConfig(),
            ("entra-user", "tenant-a", ["finance_reader"], ["finance"]),
        ),
        (
            {
                "sub": "okta-user",
                "org_id": "tenant-b",
                "app_roles": ["ticket_writer"],
                "okta_groups": ["operations"],
                "cid": "gateway",
            },
            ClaimMappingConfig(
                tenant="org_id", roles="app_roles", groups="okta_groups", client_id="cid"
            ),
            ("okta-user", "tenant-b", ["ticket_writer"], ["operations"]),
        ),
        (
            {
                "sub": "keycloak-user",
                "tenant": "tenant-c",
                "gateway_roles": ["tool_approver"],
                "gateway_groups": ["operations-approvers"],
                "client_id": "gateway",
            },
            ClaimMappingConfig(
                tenant="tenant",
                roles="gateway_roles",
                groups="gateway_groups",
                client_id="client_id",
            ),
            ("keycloak-user", "tenant-c", ["tool_approver"], ["operations-approvers"]),
        ),
    ],
    ids=["entra-standard-claims", "okta-mapped-claims", "keycloak-mapped-claims"],
)
def test_oidc_provider_claim_fixtures_map_roles_and_groups(claims, mapping, expected) -> None:
    identity = claims_to_identity(claims, mapping)
    assert (identity.sub, identity.tenant_id, identity.roles, identity.groups) == expected


@pytest.mark.parametrize(
    "claims",
    [
        {"sub": "user", "tid": "tenant", "hasgroups": True},
        {
            "sub": "user",
            "tid": "tenant",
            "_claim_names": {"groups": "src1"},
            "_claim_sources": {"src1": {"endpoint": "https://graph.example.test"}},
        },
    ],
)
def test_entra_group_overage_is_detected_as_incomplete(claims) -> None:
    identity = claims_to_identity(claims, ClaimMappingConfig())
    assert identity.groups == []
    assert identity.group_membership_complete is False


def test_entra_group_resolution_requires_graph_obo_configuration() -> None:
    with pytest.raises(ValueError, match="requires an OBO token endpoint"):
        GatewaySettings(entra_group_overage_resolution_enabled=True)
    configured = GatewaySettings(
        entra_group_overage_resolution_enabled=True,
        oidc_token_endpoint="https://login.microsoftonline.com/{tenant_id}/oauth2/v2.0/token",
        oidc_client_id="gateway-client",
        oidc_client_secret="configured-secret",
    )
    assert configured.entra_group_overage_resolution_enabled


def test_unresolved_group_overage_denies_group_dependent_upstream_access() -> None:
    policy = PolicyEvaluator(GatewaySettings(auth_mode="dev"))
    identity = claims_to_identity(
        {"sub": "user", "tid": "tenant", "hasgroups": True, "roles": ["admin"]},
        ClaimMappingConfig(),
    )
    upstream = UpstreamConfig(
        name="group_api",
        type="mcp_http",
        url="https://api.example.test/mcp",
        allowed_groups=["allowed-group-id"],
    )
    allowed, reason = policy.can_access_upstream(identity, upstream)
    assert not allowed
    assert reason == (
        "Caller group membership could not be resolved for 'group_api'; "
        "group-dependent access is denied."
    )


def test_upstream_authorization_uses_user_credential_without_gateway_group_checks() -> None:
    policy = PolicyEvaluator(
        GatewaySettings(app_env="sandbox", read_only_prod=False, auth_mode="dev")
    )
    identity = claims_to_identity({"sub": "user", "tid": "tenant"}, ClaimMappingConfig())
    upstream = UpstreamConfig(
        name="personal_api",
        type="openapi",
        url="https://api.example.test",
        auth_type="user_api_key",
        authorization_mode="upstream",
        mutating_tools=["write_record"],
    )

    assert policy.can_access_upstream(identity, upstream) == (True, "allowed")
    assert policy.can_execute_tool(identity, upstream, "write_record", is_mutating=True) == (
        True,
        "allowed",
    )


def test_shared_access_requires_explicit_shared_credential_mode() -> None:
    identity = claims_to_identity({"sub": "user", "tid": "tenant"}, ClaimMappingConfig())
    policy = PolicyEvaluator(GatewaySettings(app_env="sandbox", auth_mode="dev"))
    upstream = UpstreamConfig(
        name="shared_api",
        type="openapi",
        url="https://api.example.test",
        auth_type="api_key",
        authorization_mode="shared_access",
        allow_all_callers=True,
        mutating_tools=["write_record"],
    )

    assert policy.can_access_upstream(identity, upstream) == (True, "allowed")
    with pytest.raises(ValueError, match="allow_all_callers=true"):
        UpstreamConfig(
            name="implicit_shared_api",
            type="openapi",
            url="https://api.example.test",
            auth_type="api_key",
            authorization_mode="shared_access",
        )
    with pytest.raises(ValueError, match="requires delegated_token"):
        UpstreamConfig(
            name="wrong_upstream_mode",
            type="openapi",
            url="https://api.example.test",
            auth_type="api_key",
            authorization_mode="upstream",
        )
    with pytest.raises(ValueError, match="cannot configure a shared secret_ref"):
        UpstreamConfig(
            name="user_key_with_shared_fallback",
            type="openapi",
            url="https://api.example.test",
            auth_type="user_api_key",
            authorization_mode="upstream",
            secret_ref="env://SHARED_KEY",
        )


@pytest.mark.asyncio
async def test_entra_group_resolver_uses_tenant_user_scoped_cache() -> None:
    settings = GatewaySettings(entra_graph_scope="https://graph.microsoft.com/.default")
    cache = InMemoryCacheBackend()
    exchanged_for: list[tuple[str, str, str]] = []

    class Exchanger:
        async def exchange(self, identity, audience, scope, **_kwargs):
            exchanged_for.append((identity.tenant_id, audience, scope))
            return type("Token", (), {"access_token": f"token-{identity.tenant_id}"})()

    class Client:
        async def _request(self, method, url, *, headers, **_kwargs):
            assert method == "GET"
            assert headers["Authorization"].startswith("Bearer token-")
            return httpx.Response(200, json={"value": [{"id": "group-1"}]})

    resolver = EntraGroupMembershipResolver(
        settings=settings,
        exchanger=Exchanger(),
        cache=cache,
        upstream_client=Client(),
    )
    first = claims_to_identity(
        {"sub": "s", "tid": "tenant-a", "oid": "object-a"}, ClaimMappingConfig()
    )
    second = claims_to_identity(
        {"sub": "s", "tid": "tenant-b", "oid": "object-a"}, ClaimMappingConfig()
    )
    assert await resolver(first) == ["group-1"]
    assert await resolver(first) == ["group-1"]
    assert await resolver(second) == ["group-1"]
    assert exchanged_for == [
        ("tenant-a", "https://graph.microsoft.com", settings.entra_graph_scope),
        ("tenant-b", "https://graph.microsoft.com", settings.entra_graph_scope),
    ]


@pytest.mark.asyncio
async def test_untrusted_tenant_is_rejected_before_group_resolution() -> None:
    settings = GatewaySettings(auth_mode="dev", allowed_tenants=["tenant-allowed"])
    dev_keys = LocalDevKeyManager(issuer=settings.oidc_issuer)
    resolved = False

    async def group_resolver(_identity):
        nonlocal resolved
        resolved = True
        return ["group"]

    validator = OidcTokenValidator(settings, dev_keys=dev_keys, group_resolver=group_resolver)
    now = int(time.time())
    token = jwt.encode(
        {
            "iss": settings.oidc_issuer,
            "aud": settings.oidc_audience,
            "sub": "user",
            "tid": "tenant-untrusted",
            "hasgroups": True,
            "iat": now,
            "exp": now + 300,
        },
        dev_keys.private_pem,
        algorithm="RS256",
        headers={"kid": dev_keys.kid},
    )
    with pytest.raises(AuthError, match="not permitted"):
        await validator.validate_token(token)
    assert not resolved


@pytest.mark.asyncio
async def test_entra_obo_uses_verified_tenant_endpoint_and_propagates_claims_challenge() -> None:
    identity = claims_to_identity(
        {"sub": "user", "tid": "tenant-a", "oid": "object-a"}, ClaimMappingConfig()
    )
    seen: list[httpx.Request] = []

    def challenged(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(400, json={"error": "interaction_required", "claims": "eyJ4IjoxfQ"})

    settings = GatewaySettings(
        token_exchange_dialect="entra_v2_obo",
        oidc_token_endpoint="https://login.microsoftonline.com/{tenant_id}/oauth2/v2.0/token",
        oidc_client_id="gateway",
        oidc_client_secret="secret",
    )
    async with httpx.AsyncClient(transport=httpx.MockTransport(challenged)) as client:
        exchanger = DelegatedTokenExchanger(settings, InMemoryCacheBackend(), http_client=client)
        with pytest.raises(AuthError) as caught:
            await exchanger.exchange(
                identity,
                audience="api://downstream",
                scope="api://downstream/.default",
            )
    assert seen[0].url.path == "/tenant-a/oauth2/v2.0/token"
    assert caught.value.claims_challenge == "eyJ4IjoxfQ"
    assert caught.value.www_authenticate == 'Bearer claims="eyJ4IjoxfQ"'


def _build_auth_app(
    settings: GatewaySettings, dev_keys: LocalDevKeyManager | None = None
) -> tuple[FastAPI, OidcTokenValidator]:
    validator = OidcTokenValidator(settings=settings, dev_keys=dev_keys)
    app = FastAPI()
    app.include_router(create_oauth_discovery_router(settings, dev_keys, validator))
    return app, validator


async def test_unauthenticated_and_invalid_tokens_return_401_with_www_authenticate() -> None:
    settings = GatewaySettings(auth_mode="dev")
    dev_keys = LocalDevKeyManager(issuer=settings.oidc_issuer)
    app, _ = _build_auth_app(settings, dev_keys)

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        resp = await client.get("/auth/me")
        assert resp.status_code == 401
        assert (
            resp.headers["www-authenticate"]
            == 'Bearer resource_metadata="http://127.0.0.1:8000/.well-known/oauth-protected-resource"'
        )

        resp = await client.get("/auth/me", headers={"Authorization": "Basic abc123"})
        assert resp.status_code == 401
        assert "resource_metadata=" in resp.headers["www-authenticate"]

        expired = dev_keys.issue_user_token(persona="finance_analyst", ttl_seconds=-10)
        resp = await client.get("/auth/me", headers={"Authorization": f"Bearer {expired}"})
        assert resp.status_code == 401

        wrong_aud = dev_keys.issue_user_token(
            persona="finance_analyst", audience="api://some-other-service"
        )
        resp = await client.get("/auth/me", headers={"Authorization": f"Bearer {wrong_aud}"})
        assert resp.status_code == 401

        valid = dev_keys.issue_user_token(persona="finance_analyst")
        resp = await client.get("/auth/me", headers={"Authorization": f"Bearer {valid}"})
        assert resp.status_code == 200
        body = resp.json()
        assert body["sub"] == "user-fin-01"
        assert body["tenant_id"] == "tenant-acme-dev"
        assert "raw_token" not in body


async def test_unapproved_tenant_returns_403() -> None:
    settings = GatewaySettings(auth_mode="dev", allowed_tenants=["tenant-acme-dev"])
    dev_keys = LocalDevKeyManager(issuer=settings.oidc_issuer)
    app, _ = _build_auth_app(settings, dev_keys)

    rogue_token = dev_keys.issue_user_token(
        persona="finance_analyst",
        overrides={"tenant_id": "tenant-untrusted-99"},
    )
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        resp = await client.get("/auth/me", headers={"Authorization": f"Bearer {rogue_token}"})
        assert resp.status_code == 403


async def test_oidc_jwks_validation_with_real_rsa_keys() -> None:
    idp_keys = LocalDevKeyManager(issuer="https://idp.example.com/v2.0")
    jwks_payload = idp_keys.jwks()

    def handler(request: httpx.Request) -> httpx.Response:
        if str(request.url) == "https://idp.example.com/.well-known/jwks.json":
            return httpx.Response(200, json=jwks_payload)
        return httpx.Response(404)

    settings = GatewaySettings(
        auth_mode="oidc",
        oidc_issuer="https://idp.example.com/v2.0",
        oidc_jwks_uri="https://idp.example.com/.well-known/jwks.json",
        oidc_audience="api://mcp-code-gateway",
        allowed_tenants=["tenant-acme-dev"],
    )
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as mock_http:
        validator = OidcTokenValidator(settings=settings, http_client=mock_http)
        token = idp_keys.issue_user_token(persona="ops_engineer")
        identity = await validator.validate_authorization_header(f"Bearer {token}")
        assert identity.sub == "user-ops-01"
        assert "ops_engineer" in identity.roles


async def test_oauth_discovery_and_dev_token_endpoints() -> None:
    dev_settings = GatewaySettings(auth_mode="dev")
    dev_keys = LocalDevKeyManager(issuer=dev_settings.oidc_issuer)
    dev_app, _ = _build_auth_app(dev_settings, dev_keys)

    transport = httpx.ASGITransport(app=dev_app)
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        prm = await client.get("/.well-known/oauth-protected-resource")
        assert prm.status_code == 200
        assert prm.json() == {
            "resource": "api://mcp-code-gateway",
            "authorization_servers": ["http://127.0.0.1:8000/dev/oidc"],
            "scopes_supported": ["mcp.tools", "finance.read", "ops.read", "ops.write"],
            "bearer_methods_supported": ["header"],
        }

        asm = await client.get("/.well-known/oauth-authorization-server")
        assert asm.status_code == 200
        assert asm.json()["jwks_uri"] == "http://127.0.0.1:8000/dev/oidc/jwks"
        assert (
            "urn:ietf:params:oauth:grant-type:token-exchange" in asm.json()["grant_types_supported"]
        )

        minted = await client.post("/dev/auth/token", json={"persona": "support_agent"})
        assert minted.status_code == 200
        token = minted.json()["access_token"]
        me = await client.get("/auth/me", headers={"Authorization": f"Bearer {token}"})
        assert me.status_code == 200
        assert me.json()["sub"] == "user-sup-01"

    oidc_settings = GatewaySettings(
        auth_mode="oidc",
        oidc_issuer="https://idp.example.com",
        oidc_jwks_uri="https://idp.example.com/jwks",
    )
    oidc_app, _ = _build_auth_app(oidc_settings, None)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=oidc_app), base_url="http://testserver"
    ) as client:
        assert (await client.get("/dev/oidc/jwks")).status_code == 404
        assert (
            await client.post("/dev/auth/token", json={"persona": "finance_analyst"})
        ).status_code == 404


async def test_policy_evaluator_and_prod_restrictions(tmp_path) -> None:
    sandbox_settings = GatewaySettings(app_env="sandbox", auth_mode="dev")
    dev_keys = LocalDevKeyManager(issuer=sandbox_settings.oidc_issuer)
    validator = OidcTokenValidator(sandbox_settings, dev_keys=dev_keys)
    registry = UpstreamRegistry(sandbox_settings)
    upstreams = {u.name: u for u in registry.list_upstreams()}

    identities = {}
    for persona in ("finance_analyst", "ops_engineer", "readonly_auditor", "support_agent"):
        tok = dev_keys.issue_user_token(persona=persona)
        identities[persona] = await validator.validate_token(tok)

    policy = PolicyEvaluator(sandbox_settings)

    for p in ("finance_analyst", "readonly_auditor"):
        assert policy.can_access_upstream(identities[p], upstreams["snowflake_ledger"])[0]
        assert policy.can_access_upstream(identities[p], upstreams["billing_legacy_eu"])[0]
        assert not policy.can_access_upstream(identities[p], upstreams["ops_ticketing"])[0]

    assert policy.can_execute_tool(
        identities["ops_engineer"], upstreams["ops_ticketing"], "create_collection_case", True
    )[0]

    assert policy.can_execute_tool(
        identities["support_agent"], upstreams["ops_ticketing"], "list_terminal_incidents", False
    )[0]
    assert not policy.can_execute_tool(
        identities["support_agent"], upstreams["ops_ticketing"], "create_collection_case", True
    )[0]

    prod_settings = GatewaySettings(app_env="prod", read_only_prod=True, auth_mode="dev")
    prod_policy = PolicyEvaluator(prod_settings)
    assert not prod_policy.can_execute_tool(
        identities["ops_engineer"], upstreams["ops_ticketing"], "create_collection_case", True
    )[0]

    store = SQLiteAuditStore(str(tmp_path / "test.db"))
    await store.init()
    await store.save_dynamic_upstream(
        "local_stdio",
        UpstreamConfig(
            name="local_stdio", type="mcp_stdio", command="uvx", args=["mcp-server"]
        ).model_dump(),
    )
    prod_registry = UpstreamRegistry(prod_settings, store=store)
    await prod_registry.refresh()
    assert prod_registry.get("local_stdio") is None
    await store.close()


async def test_delegated_token_exchanger_dev_cache_isolation_and_dialects() -> None:
    settings = GatewaySettings(
        auth_mode="dev",
        token_exchange_dialect="dev_local",
        allowed_tenants=["tenant-acme-dev", "tenant-beta"],
    )
    dev_keys = LocalDevKeyManager(issuer=settings.oidc_issuer)
    validator = OidcTokenValidator(settings, dev_keys=dev_keys)
    cache = InMemoryCacheBackend()
    exchanger = DelegatedTokenExchanger(settings=settings, cache=cache, dev_keys=dev_keys)

    fin_id = await validator.validate_token(dev_keys.issue_user_token(persona="finance_analyst"))
    aud_id = await validator.validate_token(dev_keys.issue_user_token(persona="readonly_auditor"))
    beta_id = await validator.validate_token(
        dev_keys.issue_user_token(persona="finance_analyst", overrides={"tenant_id": "tenant-beta"})
    )

    first = await exchanger.exchange(
        fin_id, audience="api://snowflake-ledger", scope="snowflake.read"
    )
    assert not first.cache_hit
    decoded = jwt.decode(
        first.access_token,
        key=dev_keys.public_pem,
        algorithms=["RS256"],
        audience="api://snowflake-ledger",
        issuer=settings.oidc_issuer,
    )
    assert decoded["sub"] == "user-fin-01"
    assert decoded["act"] == {"sub": settings.oidc_client_id}

    second = await exchanger.exchange(
        fin_id, audience="api://snowflake-ledger", scope="snowflake.read"
    )
    assert second.cache_hit
    assert second.access_token == first.access_token

    aud_res = await exchanger.exchange(
        aud_id, audience="api://snowflake-ledger", scope="snowflake.read"
    )
    assert not aud_res.cache_hit
    assert aud_res.access_token != first.access_token

    beta_res = await exchanger.exchange(
        beta_id, audience="api://snowflake-ledger", scope="snowflake.read"
    )
    assert not beta_res.cache_hit
    assert beta_res.access_token != first.access_token

    captured: list[dict[str, str]] = []

    def token_handler(request: httpx.Request) -> httpx.Response:
        parsed = {k: v[0] for k, v in parse_qs(request.content.decode("utf-8")).items()}
        captured.append(parsed)
        return httpx.Response(200, json={"access_token": f"tok-{len(captured)}", "expires_in": 600})

    async with httpx.AsyncClient(transport=httpx.MockTransport(token_handler)) as mock_http:
        rfc_settings = GatewaySettings(
            token_exchange_dialect="rfc8693",
            oidc_token_endpoint="https://idp.example.com/oauth/token",
            oidc_client_id="gw-client",
            oidc_client_secret="gw-secret",
        )
        rfc_exchanger = DelegatedTokenExchanger(
            settings=rfc_settings, cache=InMemoryCacheBackend(), http_client=mock_http
        )
        res_rfc = await rfc_exchanger.exchange(
            fin_id, audience="api://billing-eu", scope="billing.read"
        )
        assert res_rfc.access_token == "tok-1"
        assert captured[0]["grant_type"] == "urn:ietf:params:oauth:grant-type:token-exchange"
        assert captured[0]["subject_token"] == fin_id.raw_token
        assert captured[0]["audience"] == "api://billing-eu"

        entra_settings = GatewaySettings(
            token_exchange_dialect="entra_v2_obo",
            oidc_token_endpoint="https://login.microsoftonline.com/tid/oauth2/v2.0/token",
            oidc_client_id="gw-client",
            oidc_client_secret="gw-secret",
        )
        entra_exchanger = DelegatedTokenExchanger(
            settings=entra_settings, cache=InMemoryCacheBackend(), http_client=mock_http
        )
        res_entra = await entra_exchanger.exchange(
            fin_id, audience="api://billing-eu", scope="api://billing-eu/.default"
        )
        assert res_entra.access_token == "tok-2"
        assert captured[1]["grant_type"] == "urn:ietf:params:oauth:grant-type:jwt-bearer"
        assert captured[1]["requested_token_use"] == "on_behalf_of"
        assert captured[1]["assertion"] == fin_id.raw_token


async def test_mock_backends_enforce_delegated_audience_and_roles() -> None:
    settings = GatewaySettings(auth_mode="dev")
    dev_keys = LocalDevKeyManager(issuer=settings.oidc_issuer)
    validator = OidcTokenValidator(settings, dev_keys=dev_keys)
    exchanger = DelegatedTokenExchanger(
        settings=settings, cache=InMemoryCacheBackend(), dev_keys=dev_keys
    )

    fin_id = await validator.validate_token(dev_keys.issue_user_token(persona="finance_analyst"))
    sup_id = await validator.validate_token(dev_keys.issue_user_token(persona="support_agent"))
    ops_id = await validator.validate_token(dev_keys.issue_user_token(persona="ops_engineer"))

    sf_tok = (
        await exchanger.exchange(fin_id, "api://snowflake-ledger", "snowflake.read")
    ).access_token
    eu_tok = (await exchanger.exchange(fin_id, "api://billing-eu", "billing.read")).access_token
    sup_ops_tok = (await exchanger.exchange(sup_id, "api://ops-ticketing", "ops.read")).access_token
    eng_ops_tok = (
        await exchanger.exchange(ops_id, "api://ops-ticketing", "ops.write")
    ).access_token

    backends_app = create_mock_backends_app(
        public_key_pem=dev_keys.public_pem, expected_issuer=settings.oidc_issuer
    )
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=backends_app), base_url="http://backends"
    ) as client:
        wrong = await client.post(
            "/snowflake/mcp",
            headers={"Authorization": f"Bearer {eu_tok}"},
            json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"},
        )
        assert wrong.status_code == 401

        ops_sf_tok = (
            await exchanger.exchange(ops_id, "api://snowflake-ledger", "snowflake.read")
        ).access_token
        forbidden = await client.post(
            "/snowflake/mcp",
            headers={"Authorization": f"Bearer {ops_sf_tok}"},
            json={"jsonrpc": "2.0", "id": 2, "method": "tools/list"},
        )
        assert forbidden.status_code == 403

        sf_call = await client.post(
            "/snowflake/mcp",
            headers={"Authorization": f"Bearer {sf_tok}"},
            json={
                "jsonrpc": "2.0",
                "id": 3,
                "method": "tools/call",
                "params": {"name": "query_debt_ledger", "arguments": {"status": "overdue"}},
            },
        )
        assert sf_call.status_code == 200
        rows = json.loads(sf_call.json()["result"]["content"][0]["text"])["rows"]
        assert len(rows) == 10

        spec = await client.get("/billing-eu/openapi.json")
        assert spec.status_code == 200
        assert "/invoices" in spec.json()["paths"]

        eu_invoices = await client.get(
            "/billing-eu/invoices?status=overdue",
            headers={"Authorization": f"Bearer {eu_tok}"},
        )
        assert eu_invoices.status_code == 200
        assert eu_invoices.json()["count"] == 5

        inc = await client.post(
            "/ops/mcp",
            headers={"Authorization": f"Bearer {sup_ops_tok}"},
            json={
                "jsonrpc": "2.0",
                "id": 4,
                "method": "tools/call",
                "params": {"name": "list_terminal_incidents", "arguments": {}},
            },
        )
        assert inc.status_code == 200

        denied_write = await client.post(
            "/ops/mcp",
            headers={"Authorization": f"Bearer {sup_ops_tok}"},
            json={
                "jsonrpc": "2.0",
                "id": 5,
                "method": "tools/call",
                "params": {
                    "name": "create_collection_case",
                    "arguments": {"customer_id": "CUST-1003", "reason": "Overdue"},
                },
            },
        )
        assert denied_write.status_code == 403

        allowed_write = await client.post(
            "/ops/mcp",
            headers={"Authorization": f"Bearer {eng_ops_tok}"},
            json={
                "jsonrpc": "2.0",
                "id": 6,
                "method": "tools/call",
                "params": {
                    "name": "create_collection_case",
                    "arguments": {"customer_id": "CUST-1003", "reason": "Overdue"},
                },
            },
        )
        assert allowed_write.status_code == 200
        assert (
            json.loads(allowed_write.json()["result"]["content"][0]["text"])["case_id"]
            == "CASE-0001"
        )

    assert len(build_synthetic_tools(25)) == 25
    with pytest.raises(ValueError):
        UpstreamConfig(name="Invalid-Name", type="mcp_http")
