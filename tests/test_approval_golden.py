"""Golden end-to-end journeys for requester confirmation policies."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any

import httpx
import pytest
from fastapi import FastAPI

from gateway.auth import LocalDevKeyManager
from gateway.config import GatewaySettings
from gateway.mcp_protocol import MCP_CLIENT_CAPABILITIES_META, request_headers, request_meta
from gateway.server import create_gateway_app
from gateway.state import SQLiteAuditStore

_GOLDEN_PATH = Path(__file__).parent / "golden" / "approval_flows.json"
_FLOWS = json.loads(_GOLDEN_PATH.read_text(encoding="utf-8"))["flows"]
_TOOL = "approval_fixture__write_item"
_ITEM_ID = "item-204"


def _mock_upstream() -> tuple[FastAPI, dict[str, int]]:
    app = FastAPI()
    calls = {"write_item": 0}

    @app.post("/mcp")
    async def handle_mcp(payload: dict[str, Any]) -> dict[str, Any]:
        method = payload["method"]
        result: dict[str, Any]
        if method == "server/discover":
            result = {"supportedVersions": ["2026-07-28"]}
        elif method == "tools/list":
            result = {
                "tools": [
                    {
                        "name": "write_item",
                        "description": "Write a test item.",
                        "inputSchema": {
                            "type": "object",
                            "properties": {"item_id": {"type": "string"}},
                            "required": ["item_id"],
                        },
                    }
                ]
            }
        elif method == "tools/call":
            calls["write_item"] += 1
            result = {
                "content": [{"type": "text", "text": json.dumps({"written": True})}],
                "structuredContent": {"written": True},
                "isError": False,
            }
        else:
            return {
                "jsonrpc": "2.0",
                "id": payload["id"],
                "error": {"code": -32601, "message": "Unknown method"},
            }
        return {"jsonrpc": "2.0", "id": payload["id"], "result": result}

    return app, calls


def _mcp_payload(
    method: str,
    *,
    name: str,
    arguments: dict[str, Any] | None = None,
    capabilities: dict[str, Any] | None = None,
    request_state: str | None = None,
    input_responses: dict[str, Any] | None = None,
) -> dict[str, Any]:
    meta = request_meta()
    meta[MCP_CLIENT_CAPABILITIES_META] = capabilities or {}
    params: dict[str, Any] = {"_meta": meta, "name": name, "arguments": arguments or {}}
    if request_state is not None:
        params["requestState"] = request_state
    if input_responses is not None:
        params["inputResponses"] = input_responses
    return {"jsonrpc": "2.0", "id": 1, "method": method, "params": params}


async def _call(
    client: httpx.AsyncClient,
    token: str,
    *,
    name: str = _TOOL,
    arguments: dict[str, Any] | None = None,
    capabilities: dict[str, Any] | None = None,
    request_state: str | None = None,
    input_responses: dict[str, Any] | None = None,
    mode: str = "traditional",
) -> httpx.Response:
    method = "tools/call"
    if mode == "codemode":
        name = "execute_code"
        arguments = arguments or {}
    return await client.post(
        f"/mcp?mode={mode}",
        headers={
            "Authorization": f"Bearer {token}",
            **request_headers(method, tool_name=name),
        },
        json=_mcp_payload(
            method,
            name=name,
            arguments=arguments,
            capabilities=capabilities,
            request_state=request_state,
            input_responses=input_responses,
        ),
    )


async def _environment(tmp_path: Path):
    backend_app, calls = _mock_upstream()
    backend_client = httpx.AsyncClient(
        transport=httpx.ASGITransport(app=backend_app), base_url="http://127.0.0.1:8001"
    )
    upstream_config = {
        "upstreams": [
            {
                "name": "approval_fixture",
                "type": "mcp_http",
                "url": "http://127.0.0.1:8001/mcp",
                "auth_type": "none",
                "allowed_roles": ["finance_analyst", "admin"],
                "allowed_groups": ["platform-admins"],
                "mutating_tools": ["write_item"],
                "tool_approval_policies": {
                    "write_item": {
                        "mode": "requester_confirmation",
                        "version": 1,
                        "confirmation_message": "Write this item?",
                    }
                },
            }
        ]
    }
    config_path = tmp_path / "upstreams.yaml"
    import yaml

    config_path.write_text(yaml.safe_dump(upstream_config), encoding="utf-8")
    settings = GatewaySettings(
        app_env="sandbox",
        auth_mode="dev",
        require_admin_auth=True,
        upstreams_config_path=str(config_path),
        sqlite_path=str(tmp_path / "gateway.db"),
        provenance_signing_secret="approval-golden-signing-key",
    )
    dev_keys = LocalDevKeyManager(issuer=settings.oidc_issuer)
    app = create_gateway_app(
        settings=settings,
        store=SQLiteAuditStore(settings.sqlite_path),
        dev_keys=dev_keys,
        http_client=backend_client,
    )
    return app, backend_client, dev_keys, calls


def _accepted_response() -> dict[str, Any]:
    return {"confirm": {"action": "accept", "content": {"confirm": True}}}


def _declined_response() -> dict[str, Any]:
    return {"confirm": {"action": "decline"}}


def _capabilities() -> dict[str, Any]:
    return {"elicitation": {"form": {}}}


@pytest.mark.parametrize("flow", _FLOWS, ids=lambda flow: flow["id"])
async def test_requester_approval_golden_journeys(flow: dict[str, str], tmp_path: Path) -> None:
    app, backend_client, dev_keys, calls = await _environment(tmp_path)
    user_token = dev_keys.issue_user_token(
        persona="gateway_admin",
        overrides={"roles": ["admin"], "scopes": ["mcp.tools", "mcp.write"]},
    )
    non_admin_token = dev_keys.issue_user_token(persona="finance_analyst")
    admin_token = dev_keys.issue_user_token(persona="gateway_admin")
    user_headers = {"Authorization": f"Bearer {non_admin_token}"}
    admin_headers = {"Authorization": f"Bearer {admin_token}"}

    try:
        async with app.router.lifespan_context(app):
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app), base_url="http://gateway"
            ) as gateway:
                kind = flow["kind"]
                if kind == "admin_rbac":
                    body = {"mode": "none", "confirmation_message": ""}
                    forbidden = await gateway.put(
                        f"/admin/upstreams/approval_fixture/tools/{_TOOL}/approval-policy",
                        headers=user_headers,
                        json=body,
                    )
                    assert forbidden.status_code == 403
                    saved = await gateway.put(
                        f"/admin/upstreams/approval_fixture/tools/{_TOOL}/approval-policy",
                        headers=admin_headers,
                        json={
                            "mode": "requester_confirmation",
                            "confirmation_message": "Confirm update.",
                        },
                    )
                    assert saved.status_code == 200, saved.text
                    assert saved.json()["approval_policy"]["version"] == 2
                    return

                if kind == "missing_capability":
                    missing = await _call(
                        gateway,
                        user_token,
                        arguments={"item_id": _ITEM_ID},
                    )
                    assert missing.json()["error"]["code"] == -32021
                    assert calls["write_item"] == 0
                    return

                if kind == "code_mode":
                    response = await _call(
                        gateway,
                        user_token,
                        mode="codemode",
                        arguments={
                            "code": "await approval_fixture__write_item(item_id='item-204')"
                        },
                    )
                    assert response.status_code == 200, response.text
                    result = response.json()["result"]["structuredContent"]
                    assert result["ok"] is False
                    assert "Requester confirmation" in result["error"]
                    assert calls["write_item"] == 0
                    return

                prompted = await _call(
                    gateway,
                    user_token,
                    arguments={"item_id": _ITEM_ID},
                    capabilities=_capabilities(),
                )
                assert prompted.status_code == 200 and "result" in prompted.json(), (
                    app.state.runtime.startup_warnings,
                    prompted.text,
                )
                prompt = prompted.json()["result"]
                assert prompt["resultType"] == "input_required"
                assert "Write this item?" in prompt["inputRequests"]["confirm"]["params"]["message"]
                state = prompt["requestState"]
                assert calls["write_item"] == 0

                if kind == "accept":
                    accepted = await _call(
                        gateway,
                        user_token,
                        arguments={"item_id": _ITEM_ID},
                        capabilities=_capabilities(),
                        request_state=state,
                        input_responses=_accepted_response(),
                    )
                    assert accepted.json()["result"]["structuredContent"]["ok"] is True
                    assert calls["write_item"] == 1

                elif kind == "decline_replay":
                    declined = await _call(
                        gateway,
                        user_token,
                        arguments={"item_id": _ITEM_ID},
                        capabilities=_capabilities(),
                        request_state=state,
                        input_responses=_declined_response(),
                    )
                    assert declined.json()["result"]["isError"] is True
                    replay = await _call(
                        gateway,
                        user_token,
                        arguments={"item_id": _ITEM_ID},
                        capabilities=_capabilities(),
                        request_state=state,
                        input_responses=_accepted_response(),
                    )
                    assert replay.json()["error"]["code"] == -32602
                    assert calls["write_item"] == 0

                elif kind == "argument_tamper":
                    changed = await _call(
                        gateway,
                        user_token,
                        arguments={"item_id": "item-205"},
                        capabilities=_capabilities(),
                        request_state=state,
                        input_responses=_accepted_response(),
                    )
                    assert changed.json()["error"]["code"] == -32602
                    assert calls["write_item"] == 0

                elif kind == "identity_mismatch":
                    other_token = dev_keys.issue_user_token(
                        persona="finance_analyst",
                        overrides={
                            "sub": "user-other",
                            "client_id": "different-client",
                            "roles": ["admin"],
                            "groups": ["platform-admins"],
                            "scopes": ["mcp.tools", "mcp.write"],
                        },
                    )
                    changed = await _call(
                        gateway,
                        other_token,
                        arguments={"item_id": _ITEM_ID},
                        capabilities=_capabilities(),
                        request_state=state,
                        input_responses=_accepted_response(),
                    )
                    assert changed.json()["error"]["code"] == -32602
                    assert calls["write_item"] == 0

                elif kind == "policy_change":
                    updated = await gateway.put(
                        f"/admin/upstreams/approval_fixture/tools/{_TOOL}/approval-policy",
                        headers=admin_headers,
                        json={
                            "mode": "requester_confirmation",
                            "confirmation_message": "New policy.",
                        },
                    )
                    assert updated.status_code == 200, updated.text
                    assert updated.json()["approval_policy"]["version"] == 2
                    stale = await _call(
                        gateway,
                        user_token,
                        arguments={"item_id": _ITEM_ID},
                        capabilities=_capabilities(),
                        request_state=state,
                        input_responses=_accepted_response(),
                    )
                    assert stale.json()["error"]["code"] == -32602
                    assert calls["write_item"] == 0

                elif kind == "concurrent_accept":
                    results = await asyncio.gather(
                        *[
                            _call(
                                gateway,
                                user_token,
                                arguments={"item_id": _ITEM_ID},
                                capabilities=_capabilities(),
                                request_state=state,
                                input_responses=_accepted_response(),
                            )
                            for _ in range(2)
                        ]
                    )
                    assert sum("result" in response.json() for response in results) == 1
                    assert sum("error" in response.json() for response in results) == 1
                    assert calls["write_item"] == 1

                else:
                    raise AssertionError(f"Unsupported approval golden flow: {kind}")
    finally:
        await backend_client.aclose()
