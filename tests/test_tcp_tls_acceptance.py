"""Opt-in local TCP/TLS acceptance tests for OIDC/OBO and HTTP MCP upstreams."""

from __future__ import annotations

import asyncio
import os
import socket
import ssl
from datetime import UTC, datetime, timedelta
from pathlib import Path
from urllib.parse import parse_qs

import httpx
import pytest
import uvicorn
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.x509.oid import NameOID
from fastapi import FastAPI, Request
from mcp.client.session import ClientSession
from mcp.client.streamable_http import streamable_http_client
from mcp.server.lowlevel.server import ServerRequestContext
from mcp.server.mcpserver import MCPServer
from mcp.types import DiscoverResult, RequestParams, ServerCapabilities

from gateway.auth import (
    AuthError,
    CallerIdentity,
    DelegatedTokenExchanger,
    LocalDevKeyManager,
    OidcTokenValidator,
)
from gateway.config import GatewaySettings, ToolApprovalPolicy, UpstreamConfig
from gateway.server import create_gateway_app
from gateway.state import InMemoryCacheBackend, SQLiteAuditStore
from gateway.upstream_clients import UpstreamClientManager

pytestmark = [
    pytest.mark.integration,
    pytest.mark.tcp_tls,
    pytest.mark.skipif(
        os.getenv("MCP_RUN_TCP_TLS_TESTS") != "1",
        reason="set MCP_RUN_TCP_TLS_TESTS=1 to run local TCP/TLS acceptance tests",
    ),
]


def _certificate_files(directory: Path) -> tuple[Path, Path, Path]:
    ca_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    ca_name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "MCPcodeaway Test CA")])
    now = datetime.now(UTC)
    ca_cert = (
        x509.CertificateBuilder()
        .subject_name(ca_name)
        .issuer_name(ca_name)
        .public_key(ca_key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - timedelta(minutes=1))
        .not_valid_after(now + timedelta(days=2))
        .add_extension(x509.BasicConstraints(ca=True, path_length=0), critical=True)
        .add_extension(
            x509.KeyUsage(
                digital_signature=True,
                content_commitment=False,
                key_encipherment=False,
                data_encipherment=False,
                key_agreement=False,
                key_cert_sign=True,
                crl_sign=True,
                encipher_only=None,
                decipher_only=None,
            ),
            critical=True,
        )
        .add_extension(x509.SubjectKeyIdentifier.from_public_key(ca_key.public_key()), False)
        .add_extension(
            x509.AuthorityKeyIdentifier.from_issuer_public_key(ca_key.public_key()), False
        )
        .sign(ca_key, hashes.SHA256())
    )
    server_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    server_name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "localhost")])
    server_cert = (
        x509.CertificateBuilder()
        .subject_name(server_name)
        .issuer_name(ca_name)
        .public_key(server_key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - timedelta(minutes=1))
        .not_valid_after(now + timedelta(days=2))
        .add_extension(x509.SubjectAlternativeName([x509.DNSName("localhost")]), critical=False)
        .add_extension(
            x509.KeyUsage(
                digital_signature=True,
                content_commitment=False,
                key_encipherment=True,
                data_encipherment=False,
                key_agreement=False,
                key_cert_sign=False,
                crl_sign=False,
                encipher_only=None,
                decipher_only=None,
            ),
            critical=True,
        )
        .add_extension(x509.SubjectKeyIdentifier.from_public_key(server_key.public_key()), False)
        .add_extension(
            x509.AuthorityKeyIdentifier.from_issuer_public_key(ca_key.public_key()), False
        )
        .sign(ca_key, hashes.SHA256())
    )
    ca_path = directory / "test-ca.pem"
    cert_path = directory / "server-cert.pem"
    key_path = directory / "server-key.pem"
    ca_path.write_bytes(ca_cert.public_bytes(serialization.Encoding.PEM))
    cert_path.write_bytes(server_cert.public_bytes(serialization.Encoding.PEM))
    key_path.write_bytes(
        server_key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption(),
        )
    )
    return ca_path, cert_path, key_path


class LocalTlsService:
    def __init__(self, app: FastAPI, cert_path: Path, key_path: Path) -> None:
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        sock.bind(("127.0.0.1", 0))
        sock.listen(128)
        self.port = int(sock.getsockname()[1])
        self.socket = sock
        self.server = uvicorn.Server(
            uvicorn.Config(
                app,
                log_level="error",
                ssl_certfile=str(cert_path),
                ssl_keyfile=str(key_path),
                lifespan="on",
            )
        )
        self.task: asyncio.Task[None] | None = None

    async def __aenter__(self) -> LocalTlsService:
        self.task = asyncio.create_task(self.server.serve(sockets=[self.socket]))
        for _ in range(100):
            if self.server.started:
                return self
            if self.task.done():
                await self.task
            await asyncio.sleep(0.02)
        raise TimeoutError("local HTTPS server did not start")

    async def __aexit__(self, *_exc: object) -> None:
        if self.task is not None and not self.task.done():
            self.server.should_exit = True
            try:
                await asyncio.wait_for(self.task, timeout=3)
            except TimeoutError:
                self.task.cancel()
                await asyncio.gather(self.task, return_exceptions=True)
        self.socket.close()


def _oidc_app(state: dict[str, object]) -> FastAPI:
    app = FastAPI()

    @app.get("/{tenant_id}/.well-known/openid-configuration")
    async def discovery(tenant_id: str) -> dict[str, object]:
        issuer = str(state["issuer"])
        return {
            "issuer": issuer,
            "jwks_uri": f"{issuer}/keys",
            "token_endpoint": f"{issuer}/oauth2/v2.0/token",
            "response_types_supported": ["code"],
            "id_token_signing_alg_values_supported": ["RS256"],
        }

    @app.get("/{tenant_id}/keys")
    async def jwks() -> dict[str, object]:
        state["jwks_requests"] = int(state.get("jwks_requests", 0)) + 1
        return {"keys": state["keys"]}

    @app.post("/{tenant_id}/oauth2/v2.0/token")
    async def token(tenant_id: str, request: Request) -> object:
        fields = parse_qs((await request.body()).decode("utf-8"))
        requests = state.setdefault("token_requests", [])
        assert isinstance(requests, list)
        requests.append(fields)
        tenant_requests = state.setdefault("token_tenants", [])
        assert isinstance(tenant_requests, list)
        tenant_requests.append(tenant_id)
        delay = float(state.get("token_delay", 0))
        if delay:
            await asyncio.sleep(delay)
        status = int(state.get("token_status", 400 if state.get("token_error") else 200))
        error = state.get("token_error")
        if error:
            from fastapi.responses import JSONResponse

            return JSONResponse(
                status_code=status,
                content={"error": error, "claims": state.get("claims_challenge")},
            )
        if status >= 400:
            from fastapi.responses import JSONResponse

            return JSONResponse(status_code=status, content={"error": "server_error"})
        return {
            "access_token": "local-downstream-token",
            "token_type": "Bearer",
            "expires_in": 300,
        }

    return app


@pytest.fixture
async def oidc_tls(tmp_path: Path):
    ca_path, cert_path, key_path = _certificate_files(tmp_path)
    state: dict[str, object] = {"token_requests": [], "jwks_requests": 0}
    app = _oidc_app(state)
    service = LocalTlsService(app, cert_path, key_path)
    issuer = f"https://localhost:{service.port}/tenant"
    state["issuer"] = issuer
    await service.__aenter__()
    yield service, state, ca_path, issuer
    await service.__aexit__(None, None, None)


@pytest.mark.asyncio
async def test_local_https_oidc_discovery_jwks_and_rotation(oidc_tls) -> None:
    service, state, ca_path, issuer = oidc_tls
    state["keys"] = []
    old_signer = LocalDevKeyManager(issuer=issuer)
    new_signer = LocalDevKeyManager(issuer=issuer)
    state["keys"] = old_signer.jwks()["keys"]
    settings = GatewaySettings(
        app_env="sandbox",
        auth_mode="oidc",
        oidc_issuer=issuer,
        oidc_jwks_uri=f"{issuer}/keys",
        oidc_audience="api://mcp-code-gateway",
        allowed_tenants=["tenant-acme-dev"],
    )
    async with httpx.AsyncClient(
        verify=ssl.create_default_context(cafile=str(ca_path)), timeout=2
    ) as client:
        metadata = (await client.get(f"{issuer}/.well-known/openid-configuration")).json()
        assert metadata["issuer"] == issuer
        assert metadata["jwks_uri"] == settings.oidc_jwks_uri
        validator = OidcTokenValidator(settings, http_client=client)
        old_identity = await validator.validate_token(
            old_signer.issue_user_token(persona="finance_analyst")
        )
        assert old_identity.sub == "user-fin-01"

        state["keys"] = new_signer.jwks()["keys"]
        new_identity = await validator.validate_token(
            new_signer.issue_user_token(persona="finance_analyst")
        )
        assert new_identity.sub == "user-fin-01"
        unknown = LocalDevKeyManager(issuer=issuer).issue_user_token(persona="finance_analyst")
        with pytest.raises(AuthError, match="Signing key not found"):
            await validator.validate_token(unknown)
    assert int(state["jwks_requests"]) >= 3


@pytest.mark.asyncio
async def test_local_https_tls_verification_rejects_untrusted_and_wrong_host(oidc_tls) -> None:
    service, _state, ca_path, issuer = oidc_tls
    async with httpx.AsyncClient(timeout=2) as untrusted:
        with pytest.raises(httpx.ConnectError):
            await untrusted.get(f"{issuer}/.well-known/openid-configuration")
    async with httpx.AsyncClient(
        verify=ssl.create_default_context(cafile=str(ca_path)), timeout=2
    ) as trusted:
        with pytest.raises(httpx.ConnectError):
            await trusted.get(f"https://127.0.0.1:{service.port}/.well-known/openid-configuration")


@pytest.mark.asyncio
async def test_local_https_obo_success_challenge_validation_and_timeout(oidc_tls) -> None:
    service, state, ca_path, issuer = oidc_tls
    endpoint = f"https://localhost:{service.port}/{{tenant_id}}/oauth2/v2.0/token"
    settings = GatewaySettings(
        app_env="sandbox",
        auth_mode="oidc",
        oidc_issuer=issuer,
        oidc_jwks_uri=f"{issuer}/keys",
        oidc_token_endpoint=endpoint,
        oidc_client_id="local-gateway-client",
        oidc_client_secret="test-only-client-secret",
        token_exchange_dialect="entra_v2_obo",
        ssrf_protection_enabled=False,
    )
    identity = CallerIdentity(
        sub="local-user",
        tenant_id="tenant-a",
        raw_token="local-rs256-assertion",
        claims={"exp": 4_000_000_000},
    )
    async with httpx.AsyncClient(
        verify=ssl.create_default_context(cafile=str(ca_path)), timeout=0.15
    ) as client:
        exchanger = DelegatedTokenExchanger(settings, InMemoryCacheBackend(), http_client=client)
        result = await exchanger.exchange(
            identity,
            audience="api://downstream",
            scope="api://downstream/.default",
        )
        assert result.access_token == "local-downstream-token"
        request = state["token_requests"][-1]
        assert request["assertion"] == [identity.raw_token]
        assert request["client_id"] == ["local-gateway-client"]
        assert request["scope"] == ["api://downstream/.default"]
        assert state["token_tenants"][-1] == "tenant-a"

        state["token_error"] = "invalid_scope"
        with pytest.raises(AuthError, match="Token exchange failed"):
            await exchanger.exchange(
                identity,
                audience="api://downstream",
                scope="api://downstream/invalid",
            )

        state["token_error"] = "interaction_required"
        state["claims_challenge"] = "eyJjaGFsbGVuZ2UiOiJsb2NhbCJ9"
        with pytest.raises(AuthError) as challenge_error:
            await exchanger.exchange(
                identity,
                audience="api://downstream",
                scope="api://downstream/challenged",
            )
        assert challenge_error.value.claims_challenge == state["claims_challenge"]

        state["token_error"] = "invalid_target"
        with pytest.raises(AuthError, match="Token exchange failed"):
            await exchanger.exchange(
                identity,
                audience="api://wrong-audience",
                scope="api://wrong-audience/.default",
            )

        with pytest.raises(AuthError, match="absolute HTTPS URL"):
            await exchanger.exchange(
                identity,
                audience="api://downstream",
                scope="api://downstream/.default",
                token_endpoint_override="http://localhost/token",
            )

        production_exchanger = DelegatedTokenExchanger(
            settings.model_copy(
                update={
                    "app_env": "prod",
                    "oidc_issuer": f"https://localhost:{service.port}/tenant-a/v2.0",
                }
            ),
            InMemoryCacheBackend(),
            http_client=client,
        )
        request_count = len(state["token_requests"])
        with pytest.raises(AuthError, match="verified caller tenant"):
            await production_exchanger.exchange(
                identity,
                audience="api://downstream",
                scope="api://downstream/.default",
                token_endpoint_override="https://elsewhere.invalid/tenant-a/oauth2/v2.0/token",
            )
        assert len(state["token_requests"]) == request_count

        state["token_error"] = None
        state["token_delay"] = 0.4
        with pytest.raises(httpx.TimeoutException):
            await exchanger.exchange(
                identity,
                audience="api://downstream",
                scope="api://downstream/slow",
            )

        state["token_delay"] = 0
        state["token_status"] = 503
        with pytest.raises(AuthError, match=r"Token exchange failed \(503\)"):
            await exchanger.exchange(
                identity,
                audience="api://downstream",
                scope="api://downstream/unavailable",
            )

    assert all(
        request["client_secret"] == ["test-only-client-secret"]
        for request in state["token_requests"]
    )


@pytest.mark.asyncio
async def test_local_https_obo_connection_failure(oidc_tls) -> None:
    service, _state, ca_path, issuer = oidc_tls
    settings = GatewaySettings(
        app_env="sandbox",
        auth_mode="oidc",
        oidc_issuer=issuer,
        oidc_jwks_uri=f"{issuer}/keys",
        oidc_token_endpoint=f"https://localhost:{service.port}/{{tenant_id}}/oauth2/v2.0/token",
        oidc_client_id="local-gateway-client",
        oidc_client_secret="test-only-client-secret",
        token_exchange_dialect="entra_v2_obo",
        ssrf_protection_enabled=False,
    )
    identity = CallerIdentity(
        sub="local-user",
        tenant_id="tenant-a",
        raw_token="local-assertion",
        claims={"exp": 4_000_000_000},
    )
    await service.__aexit__(None, None, None)
    async with httpx.AsyncClient(
        verify=ssl.create_default_context(cafile=str(ca_path)), timeout=1
    ) as client:
        exchanger = DelegatedTokenExchanger(settings, InMemoryCacheBackend(), http_client=client)
        with pytest.raises(httpx.ConnectError):
            await exchanger.exchange(
                identity,
                audience="api://downstream",
                scope="api://downstream/.default",
            )


def _sdk_mcp_app(calls: list[str]) -> FastAPI:
    server = MCPServer(name="tcp-tls-acceptance", version="1.0.0")

    @server.tool(description="Return a read-only value", structured_output=True)
    async def read_value(value: str) -> dict[str, str]:
        calls.append("read")
        return {"value": value}

    @server.tool(description="Write a value", structured_output=True)
    async def write_value(value: str) -> dict[str, str]:
        calls.append("write")
        return {"written": value}

    @server.tool(description="Return after a fixed delay")
    async def slow_value() -> str:
        calls.append("slow")
        await asyncio.sleep(0.6)
        return "slow"

    async def discover(_context: ServerRequestContext[None], _params: RequestParams):
        return DiscoverResult(
            supportedVersions=["2026-07-28"],
            capabilities=ServerCapabilities(tools={}),
        )

    server._lowlevel_server.add_request_handler("server/discover", RequestParams, discover)
    return server.streamable_http_app(
        streamable_http_path="/mcp",
        json_response=True,
        stateless_http=True,
        host="localhost",
    )


@pytest.fixture
async def mcp_tls(tmp_path: Path):
    ca_path, cert_path, key_path = _certificate_files(tmp_path)
    calls: list[str] = []
    app = _sdk_mcp_app(calls)
    service = LocalTlsService(app, cert_path, key_path)
    await service.__aenter__()
    yield service, calls, ca_path
    await service.__aexit__(None, None, None)


@pytest.mark.asyncio
async def test_gateway_upstream_client_uses_tcp_tls_mcp_sdk_boundary(
    mcp_tls, tmp_path: Path
) -> None:
    service, calls, ca_path = mcp_tls
    client = httpx.AsyncClient(verify=ssl.create_default_context(cafile=str(ca_path)), timeout=2)
    settings = GatewaySettings(
        auth_mode="dev",
        app_env="sandbox",
        ssrf_protection_enabled=False,
        sqlite_path=str(tmp_path / "gateway.db"),
        upstreams_config_path=str(tmp_path / "not-configured.yaml"),
    )
    upstream = UpstreamConfig(
        name="local_sdk",
        type="mcp_http",
        url=f"https://localhost:{service.port}/mcp",
        auth_type="none",
        allow_all_callers=True,
        allowed_operations=["read_value", "write_value"],
        tool_effects={"read_value": "read", "write_value": "write"},
        mutating_tools=["write_value"],
        tool_approval_policies={"write_value": ToolApprovalPolicy(mode="requester_confirmation")},
    )
    discoverer = UpstreamClientManager(http_client=client, ssrf_protection_enabled=False)
    tools = await discoverer.discover_tools(upstream)
    assert {tool.name for tool in tools} == {"read_value", "write_value"}
    await discoverer.close()
    app = create_gateway_app(
        settings=settings,
        store=SQLiteAuditStore(settings.sqlite_path),
        http_client=client,
        preloaded_tools={"local_sdk": tools},
    )
    dev_keys = app.state.runtime.dev_keys
    token = dev_keys.issue_user_token(persona="finance_analyst")
    try:
        async with app.router.lifespan_context(app):
            app.state.runtime.registry.upsert(upstream)
            app.state.runtime.catalog.set_server_tools(upstream, tools)
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app), base_url="http://gateway"
            ) as gateway:
                response = await gateway.post(
                    "/mcp?execution=classic",
                    headers={
                        "Authorization": f"Bearer {token}",
                        "X-Admin-Force-Mode": "traditional",
                    },
                    json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"},
                )
                assert response.status_code == 200, response.text
                names = {tool["name"] for tool in response.json()["result"]["tools"]}
                assert "local_sdk__read_value" in names
                result = await gateway.post(
                    "/mcp?execution=classic",
                    headers={"Authorization": f"Bearer {token}"},
                    json={
                        "jsonrpc": "2.0",
                        "id": 2,
                        "method": "tools/call",
                        "params": {
                            "name": "local_sdk__read_value",
                            "arguments": {"value": "over-tls"},
                        },
                    },
                )
                assert result.status_code == 200, result.text
                assert result.json()["result"]["structuredContent"]["result"] == {
                    "value": "over-tls"
                }
                assert calls == ["read"]

                for mode in ("codemode", "hybrid", "auto"):
                    code_mode = mode in {"codemode", "hybrid"}
                    tool_name = "execute_code" if code_mode else "local_sdk__read_value"
                    arguments = (
                        {"code": "await local_sdk__read_value(value='over-tls')"}
                        if code_mode
                        else {"value": "over-tls"}
                    )
                    mode_response = await gateway.post(
                        "/mcp?execution=auto" if mode == "auto" else "/mcp",
                        headers={
                            "Authorization": f"Bearer {token}",
                            "X-Admin-Force-Mode": mode,
                        },
                        json={
                            "jsonrpc": "2.0",
                            "id": mode,
                            "method": "tools/call",
                            "params": {"name": tool_name, "arguments": arguments},
                        },
                    )
                    assert mode_response.status_code == 200, mode_response.text
                    assert calls.count("read") == 2 + ("codemode", "hybrid", "auto").index(mode)

                writer_token = dev_keys.issue_user_token(
                    persona="finance_analyst",
                    overrides={"roles": ["ticketing_writer"], "scopes": ["mcp.write"]},
                )
                write_response = await gateway.post(
                    "/mcp",
                    headers={
                        "Authorization": f"Bearer {writer_token}",
                        "X-Admin-Force-Mode": "hybrid",
                    },
                    json={
                        "jsonrpc": "2.0",
                        "id": 10,
                        "method": "tools/call",
                        "params": {
                            "name": "local_sdk__write_value",
                            "arguments": {"value": "approval-required"},
                        },
                    },
                )
                assert write_response.status_code == 200
                assert write_response.json().get("error", {}).get("code") == -32021
                assert "write" not in calls
    finally:
        await client.aclose()


@pytest.mark.asyncio
async def test_mcp_sdk_client_initializes_lists_and_invokes_over_tcp_tls(mcp_tls) -> None:
    service, calls, ca_path = mcp_tls
    import httpx2

    trusted = ssl.create_default_context(cafile=str(ca_path))
    async with httpx2.AsyncClient(verify=trusted) as http_client:
        async with streamable_http_client(
            f"https://localhost:{service.port}/mcp", http_client=http_client
        ) as (read_stream, write_stream):
            async with ClientSession(read_stream, write_stream) as session:
                initialized = await session.initialize()
                assert initialized.server_info.name == "tcp-tls-acceptance"
                listed = await session.list_tools()
                assert {tool.name for tool in listed.tools} == {
                    "read_value",
                    "write_value",
                    "slow_value",
                }
                called = await session.call_tool("read_value", {"value": "sdk-client"})
                assert called.structured_content == {"value": "sdk-client"}
                assert calls == ["read"]
                failed = await session.call_tool("unknown_tool", {})
                assert failed.is_error


@pytest.mark.asyncio
async def test_gateway_mcp_tcp_tls_timeout_tls_verification_and_disconnect(mcp_tls) -> None:
    service, _calls, ca_path = mcp_tls
    upstream = UpstreamConfig(
        name="local_sdk",
        type="mcp_http",
        url=f"https://localhost:{service.port}/mcp",
        auth_type="none",
        request_timeout_seconds=0.2,
    )
    untrusted_client = httpx.AsyncClient(timeout=2)
    untrusted = UpstreamClientManager(http_client=untrusted_client, ssrf_protection_enabled=False)
    try:
        with pytest.raises(httpx.ConnectError):
            await untrusted.discover_tools(upstream)
    finally:
        await untrusted_client.aclose()

    client = httpx.AsyncClient(verify=ssl.create_default_context(cafile=str(ca_path)), timeout=2)
    manager = UpstreamClientManager(http_client=client, ssrf_protection_enabled=False)
    tools = await manager.discover_tools(upstream)
    slow_tool = next(tool for tool in tools if tool.name == "slow_value")
    with pytest.raises(TimeoutError):
        await manager.call_tool(upstream, slow_tool, {}, None)

    await service.__aexit__(None, None, None)
    with pytest.raises(httpx.ConnectError):
        await manager.discover_tools(upstream)
    await manager.close()
    await client.aclose()
