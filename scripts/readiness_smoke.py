"""Exercise the local Keycloak, gateway RBAC, and REST-backed MCP pilot."""

from __future__ import annotations

import asyncio
import importlib
import os
import sys
from pathlib import Path
from typing import Any

import httpx

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

_mcp_protocol = importlib.import_module("gateway.mcp_protocol")
request_headers = _mcp_protocol.request_headers
request_meta = _mcp_protocol.request_meta

REALM = "mcp-readiness"
CLIENT_ID = "mcp-readiness"


def _default_password() -> str:
    return "readiness-local-only"


def _readiness_settings() -> tuple[str, str]:
    return (
        os.environ.get("READINESS_IDP_URL", "http://localhost:8081").rstrip("/"),
        os.environ.get("READINESS_GATEWAY_URL", "http://localhost:8080").rstrip("/"),
    )


async def _token(client: httpx.AsyncClient, idp_url: str, username: str) -> str:
    response = await client.post(
        f"{idp_url}/realms/{REALM}/protocol/openid-connect/token",
        data={
            "grant_type": "password",
            "client_id": CLIENT_ID,
            "scope": "gateway.access",
            "username": username,
            "password": os.environ.get("READINESS_PASSWORD", _default_password()),
        },
    )
    if response.status_code != httpx.codes.OK:
        error = response.json().get(
            "error_description", response.json().get("error", "unknown error")
        )
        raise RuntimeError(
            f"Keycloak token request for {username!r} failed ({response.status_code}): {error}"
        )
    access_token = response.json().get("access_token")
    if not isinstance(access_token, str) or not access_token:
        raise RuntimeError("Keycloak token response did not contain an access token")
    return access_token


async def _wait_for_gateway(client: httpx.AsyncClient, base_url: str) -> None:
    for _ in range(30):
        try:
            response = await client.get(f"{base_url}/ready", timeout=2.0)
            if response.status_code == httpx.codes.OK:
                return
        except httpx.HTTPError:
            pass
        await asyncio.sleep(2)
    raise RuntimeError("Gateway did not become ready within 60 seconds")


async def _wait_for_idp(client: httpx.AsyncClient, idp_url: str) -> None:
    discovery_url = f"{idp_url}/realms/{REALM}/.well-known/openid-configuration"
    for _ in range(30):
        try:
            response = await client.get(discovery_url, timeout=2.0)
            if response.status_code == httpx.codes.OK:
                return
        except httpx.HTTPError:
            pass
        await asyncio.sleep(2)
    raise RuntimeError("Local Keycloak did not become ready within 60 seconds")


async def _require_status(
    client: httpx.AsyncClient,
    method: str,
    url: str,
    token: str,
    expected_status: int,
    *,
    json: dict[str, Any] | None = None,
) -> httpx.Response:
    response = await client.request(
        method,
        url,
        headers={"Authorization": f"Bearer {token}"},
        json=json,
    )
    if response.status_code != expected_status:
        raise RuntimeError(
            f"{method} {url} returned {response.status_code}; expected {expected_status}"
        )
    return response


async def run_readiness_smoke() -> dict[str, str]:
    idp_url, gateway_url = _readiness_settings()
    timeout = httpx.Timeout(15.0)
    async with httpx.AsyncClient(timeout=timeout, follow_redirects=False) as client:
        await _wait_for_idp(client, idp_url)
        await _wait_for_gateway(client, gateway_url)

        admin_token = await _token(client, idp_url, "readiness-admin")
        finance_token = await _token(client, idp_url, "readiness-finance")
        ops_token = await _token(client, idp_url, "readiness-ops")
        approver_token = await _token(client, idp_url, "readiness-approver")
        viewer_token = await _token(client, idp_url, "readiness-viewer")
        unassigned_token = await _token(client, idp_url, "readiness-unassigned")
        other_tenant_token = await _token(client, idp_url, "readiness-other-tenant")

        admin_identity = await _require_status(
            client, "GET", f"{gateway_url}/auth/me", admin_token, httpx.codes.OK
        )
        if "gateway_admin" not in admin_identity.json().get("roles", []):
            raise RuntimeError("Keycloak admin role was not mapped into the gateway identity")

        await _require_status(
            client,
            "GET",
            f"{gateway_url}/admin/upstreams",
            admin_token,
            httpx.codes.OK,
        )
        await _require_status(
            client,
            "GET",
            f"{gateway_url}/admin/upstreams",
            viewer_token,
            httpx.codes.OK,
        )
        await _require_status(
            client,
            "DELETE",
            f"{gateway_url}/admin/upstreams/readiness_missing_upstream",
            viewer_token,
            httpx.codes.FORBIDDEN,
        )
        await _require_status(
            client,
            "GET",
            f"{gateway_url}/admin/upstreams",
            unassigned_token,
            httpx.codes.FORBIDDEN,
        )
        await _require_status(
            client,
            "GET",
            f"{gateway_url}/auth/me",
            other_tenant_token,
            httpx.codes.FORBIDDEN,
        )

        tool_name = "crm_accounts__list_crm_accounts"
        call_response = await client.post(
            f"{gateway_url}/mcp",
            headers={
                **request_headers("tools/call", tool_name=tool_name),
                "Authorization": f"Bearer {finance_token}",
                "X-Admin-Force-Mode": "traditional",
            },
            json={
                "jsonrpc": "2.0",
                "id": 1,
                "method": "tools/call",
                "params": {
                    "name": tool_name,
                    "arguments": {"limit": 5},
                    "_meta": request_meta(client_name="readiness-smoke"),
                },
            },
        )
        if call_response.status_code != httpx.codes.OK:
            raise RuntimeError(f"REST-backed MCP call returned HTTP {call_response.status_code}")
        body = call_response.json()
        result = body.get("result")
        if not isinstance(result, dict) or result.get("isError"):
            raise RuntimeError("REST-backed MCP call did not return a successful tool result")

        approval_name = "readiness_approvals__create_approval_case"
        approval_arguments = {"customer_id": "CUST-READINESS", "reason": "local smoke"}
        approval_headers = {
            **request_headers("tools/call", tool_name=approval_name),
            "Authorization": f"Bearer {ops_token}",
        }
        approval_params: dict[str, Any] = {
            "name": approval_name,
            "arguments": approval_arguments,
            "_meta": {
                **request_meta(client_name="readiness-approval-smoke"),
                _mcp_protocol.MCP_CLIENT_CAPABILITIES_META: {"elicitation": {"form": {}}},
            },
        }
        prompt_response = await client.post(
            f"{gateway_url}/mcp",
            headers=approval_headers,
            json={"jsonrpc": "2.0", "id": 2, "method": "tools/call", "params": approval_params},
        )
        prompt_result = prompt_response.json().get("result", {})
        if (
            prompt_response.status_code != httpx.codes.OK
            or prompt_result.get("resultType") != "input_required"
        ):
            raise RuntimeError(
                "Approval fixture did not request requester confirmation "
                f"(HTTP {prompt_response.status_code}; {prompt_response.text[:500]})"
            )

        approval_params["requestState"] = prompt_result["requestState"]
        approval_params["inputResponses"] = {
            "confirm": {"action": "accept", "content": {"confirm": True}}
        }
        pending_response = await client.post(
            f"{gateway_url}/mcp",
            headers=approval_headers,
            json={"jsonrpc": "2.0", "id": 3, "method": "tools/call", "params": approval_params},
        )
        pending_result = pending_response.json().get("result", {}).get("structuredContent", {})
        approval_id = pending_result.get("approval_id")
        if (
            pending_response.status_code != httpx.codes.OK
            or pending_result.get("approval_status") != "pending"
            or not approval_id
        ):
            raise RuntimeError("Requester confirmation did not persist a pending approval")

        approver_headers = {"Authorization": f"Bearer {approver_token}"}
        inbox_response = await client.get(
            f"{gateway_url}/admin/approvals?status=pending", headers=approver_headers
        )
        if inbox_response.status_code != httpx.codes.OK or not any(
            item.get("approval_id") == approval_id
            for item in inbox_response.json().get("approvals", [])
        ):
            raise RuntimeError(
                "Approver could not read the pending approval through the load balancer"
            )
        decision_url = f"{gateway_url}/admin/approvals/{approval_id}/decision"
        self_decision = await client.post(
            decision_url,
            headers={"Authorization": f"Bearer {ops_token}"},
            json={"decision": "approve"},
        )
        if self_decision.status_code != httpx.codes.FORBIDDEN:
            raise RuntimeError("Requester was able to approve their own readiness action")
        wrong_group_token = finance_token
        wrong_group_decision = await client.post(
            decision_url,
            headers={"Authorization": f"Bearer {wrong_group_token}"},
            json={"decision": "approve"},
        )
        if wrong_group_decision.status_code != httpx.codes.FORBIDDEN:
            raise RuntimeError("Non-approver identity was able to decide the readiness action")
        decision_response = await client.post(
            decision_url,
            headers=approver_headers,
            json={"decision": "approve"},
        )
        if (
            decision_response.status_code != httpx.codes.OK
            or decision_response.json().get("approval", {}).get("status") != "approved"
        ):
            raise RuntimeError("Eligible approver could not approve the readiness action")

        approval_params.pop("requestState")
        approval_params.pop("inputResponses")
        replay_response = await client.post(
            f"{gateway_url}/mcp",
            headers=approval_headers,
            json={"jsonrpc": "2.0", "id": 4, "method": "tools/call", "params": approval_params},
        )
        replay_output = replay_response.json().get("result", {}).get("structuredContent", {})
        replay_result = replay_output.get("result", {})
        if (
            replay_response.status_code != httpx.codes.OK
            or not replay_output.get("ok")
            or not replay_result.get("case_id")
        ):
            raise RuntimeError(
                "Approved readiness action did not execute on requester replay "
                f"(HTTP {replay_response.status_code}; {replay_response.text[:500]})"
            )

        return {
            "gateway": gateway_url,
            "issuer": f"{idp_url}/realms/{REALM}",
            "authenticated_roles": ",".join(admin_identity.json().get("roles", [])),
            "rest_pilot": tool_name,
            "rbac": "admin allowed; viewer read-only; unassigned and other tenant denied",
            "approval": (
                "requester confirmation, shared record, self/wrong-group denial, "
                "eligible approval and replay"
            ),
        }


def main() -> int:
    try:
        report = asyncio.run(run_readiness_smoke())
    except (httpx.HTTPError, RuntimeError) as exc:
        print(f"Readiness smoke failed: {exc}", file=sys.stderr)
        return 1
    for name, value in report.items():
        print(f"{name}: {value}")
    print("Local Keycloak and REST-backed gateway smoke passed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
