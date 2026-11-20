"""Deterministic cross-replica concurrent tenant & subject isolation proof."""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

import httpx
import pytest
from fastapi import FastAPI, Header

from gateway.auth import LocalDevKeyManager
from gateway.config import GatewaySettings
from gateway.mcp_protocol import request_headers, request_meta
from gateway.server import create_gateway_app
from gateway.state import SQLiteAuditStore


def _build_barrier_backend(barrier: asyncio.Barrier) -> FastAPI:
    """Upstream OpenAPI/HTTP backend that forces two requests to overlap in flight."""
    app = FastAPI()

    @app.get("/sentinel")
    async def get_sentinel(
        x_tenant_id: str | None = Header(default=None),
        x_forwarded_sub: str | None = Header(default=None),
    ) -> dict[str, Any]:
        await barrier.wait()
        return {
            "tenant_id": x_tenant_id,
            "user_sub": x_forwarded_sub,
            "sentinel": f"secret-{x_tenant_id}-{x_forwarded_sub}",
        }

    return app


async def test_cross_replica_concurrent_tenant_and_subject_isolation(tmp_path: Path) -> None:
    """Two independent gateway replicas sharing a store isolate overlapping tenant requests."""
    shared_db = str(tmp_path / "shared_audit.db")
    cfg_path = tmp_path / "upstreams.yaml"
    cfg_path.write_text(
        """
upstreams:
  - name: sentinel_api
    type: openapi
    url: http://127.0.0.1:8999
    auth_type: none
    allowed_roles: ["finance_analyst", "support_agent"]
    openapi_spec:
      openapi: "3.0.3"
      info:
        title: Sentinel API
        version: "1.0.0"
      paths:
        /sentinel:
          get:
            operationId: get_sentinel
            responses:
              "200":
                description: ok
""",
        encoding="utf-8",
    )

    barrier = asyncio.Barrier(2)
    backend_app = _build_barrier_backend(barrier)
    backend_client_a = httpx.AsyncClient(
        transport=httpx.ASGITransport(app=backend_app), base_url="http://127.0.0.1:8999"
    )
    backend_client_b = httpx.AsyncClient(
        transport=httpx.ASGITransport(app=backend_app), base_url="http://127.0.0.1:8999"
    )

    settings_a = GatewaySettings(
        auth_mode="dev",
        sqlite_path=shared_db,
        upstreams_config_path=str(cfg_path),
        allowed_tenants=["tenant-acme-dev", "tenant-beta-dev"],
    )
    settings_b = GatewaySettings(
        auth_mode="dev",
        sqlite_path=shared_db,
        upstreams_config_path=str(cfg_path),
        allowed_tenants=["tenant-acme-dev", "tenant-beta-dev"],
    )
    dev_keys = LocalDevKeyManager(issuer=settings_a.oidc_issuer)
    shared_store = SQLiteAuditStore(shared_db)

    replica_a = create_gateway_app(
        settings=settings_a, store=shared_store, dev_keys=dev_keys, http_client=backend_client_a
    )
    replica_b = create_gateway_app(
        settings=settings_b, store=shared_store, dev_keys=dev_keys, http_client=backend_client_b
    )

    tok_a = dev_keys.issue_user_token(persona="finance_analyst")
    tok_b = dev_keys.issue_user_token(persona="support_agent")

    async def _call_replica(app: FastAPI, token: str, code: str) -> httpx.Response:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://gw"
        ) as gw:
            return await gw.post(
                "/mcp",
                headers={
                    "Authorization": f"Bearer {token}",
                    **request_headers("tools/call", tool_name="execute_code"),
                },
                json={
                    "jsonrpc": "2.0",
                    "id": 1,
                    "method": "tools/call",
                    "params": {
                        "_meta": request_meta(),
                        "name": "execute_code",
                        "arguments": {"code": code},
                    },
                },
            )

    async with replica_a.router.lifespan_context(replica_a):
        async with replica_b.router.lifespan_context(replica_b):
            resp_a, resp_b = await asyncio.gather(
                _call_replica(replica_a, tok_a, "await sentinel_api__get_sentinel()"),
                _call_replica(replica_b, tok_b, "await sentinel_api__get_sentinel()"),
            )

    assert resp_a.status_code == 200, resp_a.text
    assert resp_b.status_code == 200, resp_b.text
    assert "mcp-session-id" not in {k.lower() for k in resp_a.headers}
    assert "set-cookie" not in {k.lower() for k in resp_a.headers}

    sc_a = resp_a.json()["result"]["structuredContent"]
    sc_b = resp_b.json()["result"]["structuredContent"]
    assert sc_a["ok"] is True
    assert sc_b["ok"] is True

    # Verify audit runs in shared store are strictly isolated by subject
    runs = await shared_store.list_runs(limit=20, tenant_id="tenant-acme-dev")
    subs = {r["user_sub"] for r in runs}
    assert subs == {"user-fin-01", "user-sup-01"}
