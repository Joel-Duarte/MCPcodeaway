"""Phase 2 tests: Schema validation hints, PII/secret redaction, tenant & auth_type governance."""

from __future__ import annotations

import json

import httpx
import yaml
from fastapi import FastAPI, Header, HTTPException

from examples.mock_backends import create_mock_backends_app
from gateway.auth import LocalDevKeyManager
from gateway.config import GatewaySettings
from gateway.server import create_gateway_app
from gateway.state import SQLiteAuditStore


async def test_validation_hints_redaction_and_tenant_isolation(tmp_path) -> None:
    settings = GatewaySettings(
        auth_mode="dev",
        allowed_tenants=["tenant-acme-dev"],
        sqlite_path=str(tmp_path / "gw.db"),
    )
    dev_keys = LocalDevKeyManager(issuer=settings.oidc_issuer)
    backends_app = create_mock_backends_app(
        public_key_pem=dev_keys.public_pem, expected_issuer=settings.oidc_issuer
    )
    backend_client = httpx.AsyncClient(
        transport=httpx.ASGITransport(app=backends_app), base_url="http://127.0.0.1:8001"
    )
    app = create_gateway_app(
        settings=settings,
        store=SQLiteAuditStore(settings.sqlite_path),
        dev_keys=dev_keys,
        http_client=backend_client,
    )

    fin_token = dev_keys.issue_user_token(persona="finance_analyst")
    bad_tenant_token = dev_keys.issue_user_token(
        persona="finance_analyst", overrides={"tenant_id": "tenant-foreign"}
    )

    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://gw"
        ) as gw:
            # Tenant allowlist rejects unapproved tenant on /mcp
            denied = await gw.post(
                "/mcp",
                headers={"Authorization": f"Bearer {bad_tenant_token}"},
                json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"},
            )
            assert denied.status_code == 403

            # Bad arguments trigger actionable stub-signature self-correction hint in Code Mode
            bad_args = await gw.post(
                "/mcp",
                headers={"Authorization": f"Bearer {fin_token}"},
                json={
                    "jsonrpc": "2.0",
                    "id": 2,
                    "method": "tools/call",
                    "params": {
                        "name": "execute_code",
                        "arguments": {"code": "await snowflake_ledger__get_customer_balance()"},
                    },
                },
            )
            sc_err = bad_args.json()["result"]["structuredContent"]
            assert not sc_err["ok"]
            assert "Expected signature:" in sc_err["error"]
            assert "customer_id: str" in sc_err["error"]

            # OutputRedactor masks SSNs, API keys, and JWTs from sandbox stdout and result
            redact_resp = await gw.post(
                "/mcp",
                headers={"Authorization": f"Bearer {fin_token}"},
                json={
                    "jsonrpc": "2.0",
                    "id": 3,
                    "method": "tools/call",
                    "params": {
                        "name": "execute_code",
                        "arguments": {
                            "code": (
                                "print('leak sk-testsecret1234567890')\n"
                                "tok = 'eyJhbGciOiJSUzI1NiJ9.eyJzdWIiOiIxMjM0NSJ9.sigvalue123'\n"
                                "{\n"
                                "  'ssn': '123-45-6789',\n"
                                "  'jwt': tok,\n"
                                "}"
                            )
                        },
                    },
                },
            )
            redacted_sc = redact_resp.json()["result"]["structuredContent"]
            assert redacted_sc["ok"]
            assert "[REDACTED_SECRET]" in redacted_sc["stdout"]
            assert redacted_sc["result"]["ssn"] == "[REDACTED_SSN]"
            assert redacted_sc["result"]["jwt"] == "[REDACTED_JWT]"

    await backend_client.aclose()


async def test_pluggable_auth_types_and_schema_drift_detection(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("TEST_PARTNER_API_KEY", "partner-key-999")
    monkeypatch.setenv("TEST_PARTNER_BEARER", "partner-bearer-777")

    custom_app = FastAPI()

    @custom_app.post("/apikey/mcp")
    async def apikey_mcp(request: dict, x_partner_key: str | None = Header(default=None)):
        if x_partner_key != "partner-key-999":
            raise HTTPException(status_code=401, detail="Invalid API key")
        method = request.get("method")
        if method == "tools/list":
            return {
                "jsonrpc": "2.0",
                "id": request["id"],
                "result": {
                    "tools": [
                        {
                            "name": "ping",
                            "description": "Ping partner service",
                            "inputSchema": {"type": "object", "properties": {}},
                        }
                    ]
                },
            }
        return {
            "jsonrpc": "2.0",
            "id": request["id"],
            "result": {"content": [{"type": "text", "text": json.dumps({"pong": True})}]},
        }

    @custom_app.post("/bearer/mcp")
    async def bearer_mcp(request: dict, authorization: str | None = Header(default=None)):
        if authorization != "Bearer partner-bearer-777":
            raise HTTPException(status_code=401, detail="Invalid bearer secret")
        if request.get("method") == "tools/list":
            return {
                "jsonrpc": "2.0",
                "id": request["id"],
                "result": {
                    "tools": [
                        {
                            "name": "status",
                            "description": "Service status",
                            "inputSchema": {"type": "object", "properties": {}},
                        }
                    ]
                },
            }
        return {
            "jsonrpc": "2.0",
            "id": request["id"],
            "result": {"content": [{"type": "text", "text": json.dumps({"healthy": True})}]},
        }

    @custom_app.post("/open/mcp")
    async def open_mcp(request: dict):
        if request.get("method") == "tools/list":
            return {
                "jsonrpc": "2.0",
                "id": request["id"],
                "result": {
                    "tools": [
                        {
                            "name": "echo",
                            "description": "Echo message",
                            "inputSchema": {
                                "type": "object",
                                "properties": {"msg": {"type": "string"}},
                                "required": ["msg"],
                            },
                        }
                    ]
                },
            }
        msg = request["params"]["arguments"]["msg"]
        return {
            "jsonrpc": "2.0",
            "id": request["id"],
            "result": {"content": [{"type": "text", "text": json.dumps({"echo": msg})}]},
        }

    cfg_path = tmp_path / "upstreams.yaml"
    cfg_path.write_text(
        yaml.safe_dump(
            {
                "upstreams": [
                    {
                        "name": "partner_apikey",
                        "type": "mcp_http",
                        "url": "http://custom/apikey/mcp",
                        "auth_type": "api_key",
                        "secret_env_var": "TEST_PARTNER_API_KEY",
                        "api_key_header": "X-Partner-Key",
                        "pinned_schema_digest": "0000000000000000",  # intentional drift
                    },
                    {
                        "name": "partner_bearer",
                        "type": "mcp_http",
                        "url": "http://custom/bearer/mcp",
                        "auth_type": "bearer_secret",
                        "secret_env_var": "TEST_PARTNER_BEARER",
                    },
                    {
                        "name": "public_echo",
                        "type": "mcp_http",
                        "url": "http://custom/open/mcp",
                        "auth_type": "none",
                    },
                ]
            }
        ),
        encoding="utf-8",
    )

    settings = GatewaySettings(
        auth_mode="dev",
        sqlite_path=str(tmp_path / "gw.db"),
        upstreams_config_path=str(cfg_path),
    )
    dev_keys = LocalDevKeyManager(issuer=settings.oidc_issuer)
    custom_client = httpx.AsyncClient(
        transport=httpx.ASGITransport(app=custom_app), base_url="http://custom"
    )
    app = create_gateway_app(
        settings=settings,
        store=SQLiteAuditStore(settings.sqlite_path),
        dev_keys=dev_keys,
        http_client=custom_client,
    )
    token = dev_keys.issue_user_token(persona="finance_analyst")

    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://gw"
        ) as gw:
            health = await gw.get("/health")
            assert health.status_code == 200
            warnings = health.json()["drift_warnings"]
            assert len(warnings) == 1
            assert warnings[0]["server"] == "partner_apikey"

            res = await gw.post(
                "/mcp",
                headers={"Authorization": f"Bearer {token}"},
                json={
                    "jsonrpc": "2.0",
                    "id": 1,
                    "method": "tools/call",
                    "params": {
                        "name": "execute_code",
                        "arguments": {
                            "code": (
                                "a = await partner_apikey__ping()\n"
                                "b = await partner_bearer__status()\n"
                                "c = await public_echo__echo('hi')\n"
                                "{'a': a, 'b': b, 'c': c}"
                            )
                        },
                    },
                },
            )
            sc = res.json()["result"]["structuredContent"]
            assert sc["ok"]
            assert sc["result"] == {
                "a": {"pong": True},
                "b": {"healthy": True},
                "c": {"echo": "hi"},
            }

    await custom_client.aclose()
