"""Phase 1 tests: OIDC AuthN, team/agent RBAC, delegated token exchange, and mock backends."""

from __future__ import annotations

import json
from urllib.parse import parse_qs

import httpx
import jwt
import pytest
from fastapi import FastAPI

from examples.mock_backends import build_synthetic_tools, create_mock_backends_app
from gateway.auth import (
    DelegatedTokenExchanger,
    LocalDevKeyManager,
    OidcTokenValidator,
    PolicyEvaluator,
)
from gateway.config import GatewaySettings, UpstreamConfig, UpstreamRegistry
from gateway.oauth_discovery import create_oauth_discovery_router
from gateway.state import InMemoryCacheBackend, SQLiteAuditStore


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
