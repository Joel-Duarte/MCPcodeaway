"""Exercise the local mock OIDC issuer, gateway RBAC, and REST-backed MCP pilot."""

from __future__ import annotations

import asyncio
import base64
import hashlib
import importlib
import os
import secrets
import sys
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlparse

import httpx

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

_mcp_protocol = importlib.import_module("gateway.mcp_protocol")
request_headers = _mcp_protocol.request_headers
request_meta = _mcp_protocol.request_meta


def _gateway_url() -> str:
    return os.environ.get("READINESS_GATEWAY_URL", "http://localhost:8080").rstrip("/")


async def _token(
    client: httpx.AsyncClient,
    gateway_url: str,
    *,
    sub: str,
    roles: list[str],
    groups: list[str],
    scopes: list[str],
    tenant_id: str = "tenant-acme-dev",
) -> str:
    idp_url = os.environ.get("READINESS_IDP_URL", "http://localhost:8081/oidc").rstrip("/")
    code_verifier = secrets.token_urlsafe(48)
    challenge = (
        base64.urlsafe_b64encode(hashlib.sha256(code_verifier.encode("ascii")).digest())
        .rstrip(b"=")
        .decode("ascii")
    )
    state = secrets.token_urlsafe(24)
    response = await client.get(
        f"{idp_url}/authorize",
        params={
            "response_type": "code",
            "client_id": "mcp-readiness",
            "redirect_uri": "http://localhost:8080/auth/admin/callback",
            "scope": " ".join(sorted(set(scopes) | {"openid"})),
            "state": state,
            "code_challenge": challenge,
            "code_challenge_method": "S256",
            "persona": sub,
        },
        follow_redirects=False,
    )
    if response.status_code != httpx.codes.FOUND:
        raise RuntimeError(
            f"Local mock OIDC authorization for {sub!r} failed ({response.status_code})"
        )
    callback = urlparse(response.headers.get("location", ""))
    query = parse_qs(callback.query)
    code = (query.get("code") or [""])[0]
    if not code or (query.get("state") or [None])[0] != state:
        raise RuntimeError("Local mock OIDC authorization response had invalid code or state")
    token_response = await client.post(
        f"{idp_url}/token",
        data={
            "grant_type": "authorization_code",
            "code": code,
            "redirect_uri": "http://localhost:8080/auth/admin/callback",
            "client_id": "mcp-readiness",
            "code_verifier": code_verifier,
        },
    )
    if token_response.status_code != httpx.codes.OK:
        raise RuntimeError(
            f"Local mock OIDC code exchange for {sub!r} failed ({token_response.status_code})"
        )
    access_token = token_response.json().get("access_token")
    if not isinstance(access_token, str) or not access_token:
        raise RuntimeError("Local mock OIDC response did not contain an access token")
    if tenant_id == "tenant-acme-dev":
        identity = await client.get(
            f"{gateway_url}/auth/me", headers={"Authorization": f"Bearer {access_token}"}
        )
        if identity.status_code != httpx.codes.OK:
            raise RuntimeError(f"Mock OIDC token for {sub!r} was rejected by the gateway")
        mapped = identity.json()
        if mapped.get("roles") != roles or mapped.get("groups") != groups:
            raise RuntimeError(f"Mock OIDC fixture claims for {sub!r} did not match the scenario")
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


async def _verify_admin_oidc_login(client: httpx.AsyncClient, gateway_url: str) -> None:
    login = await client.get(
        f"{gateway_url}/auth/admin/login?next_url=/admin", follow_redirects=False
    )
    if login.status_code != httpx.codes.FOUND:
        raise RuntimeError(f"Gateway did not start OIDC login ({login.status_code})")
    authorize = await client.get(login.headers["location"], follow_redirects=False)
    if authorize.status_code != httpx.codes.FOUND:
        raise RuntimeError(f"Mock OIDC provider rejected authorization ({authorize.status_code})")
    callback = await client.get(authorize.headers["location"], follow_redirects=False)
    if callback.status_code != httpx.codes.FOUND or "set-cookie" not in callback.headers:
        raise RuntimeError(
            f"Gateway OIDC callback did not create an admin session ({callback.status_code})"
        )
    session = await client.get(f"{gateway_url}/auth/admin/session")
    if session.status_code != httpx.codes.OK or not session.json().get("authenticated"):
        raise RuntimeError("OIDC callback session was not accepted by the gateway")
    if "gateway_admin" not in session.json().get("user", {}).get("roles", []):
        raise RuntimeError("OIDC admin login returned an identity without the admin role")


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
    gateway_url = _gateway_url()
    timeout = httpx.Timeout(60.0)
    async with httpx.AsyncClient(timeout=timeout, follow_redirects=False) as client:
        await _wait_for_gateway(client, gateway_url)
        idp_url = os.environ.get("READINESS_IDP_URL", "http://localhost:8081/oidc").rstrip("/")
        provider_metadata = await client.get(f"{idp_url}/.well-known/openid-configuration")
        if provider_metadata.status_code != httpx.codes.OK:
            raise RuntimeError("Local OIDC provider metadata endpoint was not available")
        metadata = provider_metadata.json()
        if metadata.get("issuer") != "http://localhost:8081/oidc" or "S256" not in metadata.get(
            "code_challenge_methods_supported", []
        ):
            raise RuntimeError(
                "Local OIDC metadata does not advertise the expected issuer and PKCE"
            )
        await _verify_admin_oidc_login(client, gateway_url)

        admin_token = await _token(
            client,
            gateway_url,
            sub="readiness-admin",
            roles=["gateway_admin"],
            groups=["platform-admins"],
            scopes=["mcp.tools", "mcp.admin", "mcp.audit"],
        )
        finance_token = await _token(
            client,
            gateway_url,
            sub="readiness-finance",
            roles=["finance_analyst"],
            groups=["finance"],
            scopes=["mcp.tools", "finance.read"],
        )
        ops_token = await _token(
            client,
            gateway_url,
            sub="readiness-ops",
            roles=["ops_engineer", "ticketing_writer", "tool_approver"],
            groups=["operations", "operations-approvers"],
            scopes=["mcp.tools", "ops.write"],
        )
        approver_token = await _token(
            client,
            gateway_url,
            sub="readiness-approver",
            roles=["tool_approver"],
            groups=["operations-approvers"],
            scopes=["mcp.tools"],
        )
        viewer_token = await _token(
            client,
            gateway_url,
            sub="readiness-viewer",
            roles=["operator_viewer"],
            groups=["platform"],
            scopes=["mcp.tools"],
        )
        unassigned_token = await _token(
            client,
            gateway_url,
            sub="readiness-unassigned",
            roles=[],
            groups=[],
            scopes=["mcp.tools"],
        )
        other_tenant_token = await _token(
            client,
            gateway_url,
            sub="readiness-other-tenant",
            roles=["finance_analyst"],
            groups=["finance"],
            scopes=["mcp.tools", "finance.read"],
            tenant_id="mcp-other",
        )

        admin_identity = await _require_status(
            client, "GET", f"{gateway_url}/auth/me", admin_token, httpx.codes.OK
        )
        if "gateway_admin" not in admin_identity.json().get("roles", []):
            raise RuntimeError("Mock OIDC admin role was not mapped into the gateway identity")

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
            "X-Admin-Force-Mode": "traditional",
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
        wrong_group_token = await _token(
            client,
            gateway_url,
            sub="readiness-wrong-group-approver",
            roles=["tool_approver"],
            groups=["finance"],
            scopes=["mcp.tools"],
        )
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

        discovery = await client.get(f"{gateway_url}/.well-known/oauth-authorization-server")
        return {
            "gateway": gateway_url,
            "issuer": discovery.json().get("issuer", "local mock OIDC"),
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
    print("Local mock OIDC and REST-backed gateway smoke passed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
