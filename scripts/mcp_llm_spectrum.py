"""Exercise a real Streamable HTTP MCP client, mock OIDC PKCE, and local LM Studio model."""

from __future__ import annotations

import argparse
import asyncio
import base64
import hashlib
import json
import secrets
import sys
from typing import Any
from urllib.parse import parse_qs, urlsplit

import httpx
import httpx2
from mcp.client.session import ClientSession
from mcp.client.streamable_http import streamable_http_client
from mcp.shared.exceptions import MCPError

_PROTOCOL_VERSION = "2026-07-28"
_SDK_PROTOCOL_VERSION = "2025-11-25"
_PROTOCOL_META = "io.modelcontextprotocol/protocolVersion"
_CAPABILITIES_META = "io.modelcontextprotocol/clientCapabilities"
_CLIENT_INFO_META = "io.modelcontextprotocol/clientInfo"


class _GatewayContractTransport(httpx2.AsyncBaseTransport):
    """Translate standard SDK messages into the gateway's explicit metadata contract."""

    def __init__(self) -> None:
        self._inner = httpx2.AsyncHTTPTransport()

    async def handle_async_request(self, request: httpx2.Request) -> httpx2.Response:
        if request.method != "POST":
            return await self._inner.handle_async_request(request)
        payload = json.loads(await request.aread())
        method = str(payload.get("method") or "")
        if not method:
            return await self._inner.handle_async_request(request)
        if method == "initialize":
            body = {
                "jsonrpc": "2.0",
                "id": payload.get("id"),
                "result": {
                    "protocolVersion": _SDK_PROTOCOL_VERSION,
                    "capabilities": {"tools": {"listChanged": False}},
                    "serverInfo": {"name": "mcpcodeaway-gateway", "version": "mvp"},
                },
            }
            return httpx2.Response(
                200,
                headers={"content-type": "application/json"},
                content=json.dumps(body, separators=(",", ":")).encode("utf-8"),
                request=request,
            )
        if method == "notifications/initialized":
            return httpx2.Response(202, request=request)
        params = payload.setdefault("params", {})
        meta = params.setdefault("_meta", {})
        meta[_PROTOCOL_META] = _PROTOCOL_VERSION
        meta.setdefault(_CAPABILITIES_META, params.get("capabilities") or {})
        meta.setdefault(_CLIENT_INFO_META, {"name": "mcp-llm-spectrum", "version": "0.1.0"})
        headers = dict(request.headers)
        headers.pop("content-length", None)
        headers.pop("mcp-protocol-version", None)
        headers.pop("mcp-method", None)
        headers.pop("mcp-name", None)
        headers["MCP-Protocol-Version"] = meta[_PROTOCOL_META]
        headers["Mcp-Method"] = method
        if isinstance(params.get("name"), str):
            headers["Mcp-Name"] = params["name"]
        stamped = httpx2.Request(
            request.method,
            request.url,
            headers=headers,
            content=json.dumps(payload, separators=(",", ":")).encode("utf-8"),
            extensions=request.extensions,
        )
        response = await self._inner.handle_async_request(stamped)
        if method == "initialize" and "application/json" in response.headers.get(
            "content-type", ""
        ):
            response_body = await response.aread()
            result = json.loads(response_body)
            if isinstance(result.get("result"), dict):
                result["result"]["protocolVersion"] = _SDK_PROTOCOL_VERSION
            await response.aclose()
            response_headers = dict(response.headers)
            response_headers.pop("content-length", None)
            response_headers.pop("transfer-encoding", None)
            return httpx2.Response(
                response.status_code,
                headers=response_headers,
                content=json.dumps(result, separators=(",", ":")).encode("utf-8"),
                request=stamped,
            )
        return response

    async def aclose(self) -> None:
        await self._inner.aclose()


async def _oidc_token(
    client: httpx.AsyncClient,
    idp_url: str,
    *,
    persona: str,
    scopes: str,
    client_id: str,
    redirect_uri: str,
) -> str:
    verifier = secrets.token_urlsafe(64)
    digest = hashlib.sha256(verifier.encode("ascii")).digest()
    challenge = base64.urlsafe_b64encode(digest).rstrip(b"=").decode("ascii")
    state = secrets.token_urlsafe(24)
    authorize = await client.get(
        f"{idp_url.rstrip('/')}/authorize",
        params={
            "response_type": "code",
            "client_id": client_id,
            "redirect_uri": redirect_uri,
            "scope": scopes,
            "state": state,
            "code_challenge": challenge,
            "code_challenge_method": "S256",
            "persona": persona,
        },
        follow_redirects=False,
    )
    if authorize.status_code != httpx.codes.FOUND:
        raise RuntimeError(
            f"Mock IdP authorization failed ({authorize.status_code}): {authorize.text}"
        )
    callback = urlsplit(authorize.headers["location"])
    params = parse_qs(callback.query)
    if params.get("state", [None])[0] != state or not params.get("code"):
        raise RuntimeError("Mock IdP returned an invalid authorization state or no code")
    token = await client.post(
        f"{idp_url.rstrip('/')}/token",
        data={
            "grant_type": "authorization_code",
            "client_id": client_id,
            "redirect_uri": redirect_uri,
            "code": params["code"][0],
            "code_verifier": verifier,
        },
    )
    if token.status_code != httpx.codes.OK:
        raise RuntimeError(f"Mock IdP token exchange failed ({token.status_code}): {token.text}")
    return str(token.json()["access_token"])


async def _chat_for_tool(
    *,
    model_base_url: str,
    model: str,
    tools: list[Any],
    prompt: str,
) -> dict[str, Any]:
    openai_tools = [
        {
            "type": "function",
            "function": {
                "name": tool.name,
                "description": tool.description or "",
                "parameters": tool.inputSchema
                if hasattr(tool, "inputSchema")
                else tool.input_schema,
            },
        }
        for tool in tools
    ]
    async with httpx.AsyncClient(timeout=120.0) as client:
        response = await client.post(
            f"{model_base_url.rstrip('/')}/chat/completions",
            headers={"Authorization": "Bearer lm-studio"},
            json={
                "model": model,
                "temperature": 0,
                "max_tokens": 512,
                "messages": [
                    {
                        "role": "system",
                        "content": (
                            "Use the available MCP tool to answer from the API. Do not invent data."
                        ),
                    },
                    {"role": "user", "content": prompt},
                ],
                "tools": openai_tools,
                "tool_choice": "auto",
            },
        )
        response.raise_for_status()
        return response.json()


async def _run(args: argparse.Namespace) -> dict[str, Any]:
    async with httpx.AsyncClient(timeout=20.0) as oidc:
        finance_token = await _oidc_token(
            oidc,
            args.idp_url,
            persona="readiness-finance",
            scopes="openid mcp.tools finance.read",
            client_id=args.client_id,
            redirect_uri=args.redirect_uri,
        )
        support_token = await _oidc_token(
            oidc,
            args.idp_url,
            persona="support_agent",
            scopes="openid mcp.tools ops.read",
            client_id=args.client_id,
            redirect_uri=args.redirect_uri,
        )

    target = f"{args.gateway_url.rstrip('/')}/mcp?mode=traditional&servers=crm_accounts"
    authorized: dict[str, Any]
    async with httpx2.AsyncClient(
        headers={"Authorization": f"Bearer {finance_token}"},
        timeout=120.0,
        transport=_GatewayContractTransport(),
    ) as client:
        async with streamable_http_client(target, http_client=client) as (read, write):
            async with ClientSession(read, write, read_timeout_seconds=120) as session:
                await session.initialize()
                listed = await session.list_tools()
                crm_tools = [
                    tool for tool in listed.tools if tool.name.endswith("list_crm_accounts")
                ]
                if not crm_tools:
                    raise RuntimeError(
                        "Finance persona was not given the CRM list tool; visible tools: "
                        + ", ".join(tool.name for tool in listed.tools)
                    )
                completion = await _chat_for_tool(
                    model_base_url=args.model_base_url,
                    model=args.model,
                    tools=crm_tools,
                    prompt=(
                        "Find the Acme Retail EU account and report its account ID and ARR in USD."
                    ),
                )
                message = completion["choices"][0]["message"]
                calls = message.get("tool_calls") or []
                if len(calls) != 1:
                    raise RuntimeError(f"Expected one model tool call; received {len(calls)}")
                call = calls[0]
                tool_name = str(call["function"]["name"])
                arguments = json.loads(call["function"].get("arguments") or "{}")
                result = await session.call_tool(tool_name, arguments)
                if result.is_error:
                    raise RuntimeError(f"Authorized MCP API call failed: {result.content}")
                result_data = result.structured_content
                if not isinstance(result_data, dict) or "ACC-101" not in json.dumps(result_data):
                    raise RuntimeError(
                        f"CRM response did not contain the seeded Acme record: {result_data}"
                    )
                authorized = {
                    "subject": "readiness-finance",
                    "tool": tool_name,
                    "arguments": arguments,
                    "api_result": result_data,
                }

    denied: dict[str, Any]
    async with httpx2.AsyncClient(
        headers={"Authorization": f"Bearer {support_token}"},
        timeout=120.0,
        transport=_GatewayContractTransport(),
    ) as client:
        async with streamable_http_client(target, http_client=client) as (read, write):
            async with ClientSession(read, write, read_timeout_seconds=120) as session:
                await session.initialize()
                listed = await session.list_tools()
                crm_visible = any(tool.name.endswith("list_crm_accounts") for tool in listed.tools)
                denied_by_gateway = False
                try:
                    result = await session.call_tool("crm_accounts__list_crm_accounts", {})
                    denied_by_gateway = result.is_error
                    error_blocks = [getattr(block, "text", "") for block in result.content]
                except MCPError as exc:
                    denied_by_gateway = True
                    error_blocks = [str(exc)]
                if crm_visible or not denied_by_gateway:
                    raise RuntimeError("Support persona unexpectedly accessed finance CRM data")
                denied = {
                    "subject": "support_agent",
                    "crm_tool_visible": crm_visible,
                    "direct_call_denied": denied_by_gateway,
                    "error": error_blocks,
                }

    return {
        "mcp_sdk": "streamable-http",
        "model": args.model,
        "authorized_flow": authorized,
        "denied_flow": denied,
        "passed": True,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--gateway-url", default="http://127.0.0.1:8080")
    parser.add_argument("--idp-url", default="http://127.0.0.1:8081/oidc")
    parser.add_argument("--model-base-url", default="http://127.0.0.1:1234/v1")
    parser.add_argument("--model", default="qwen/qwen3.5-9b")
    parser.add_argument("--client-id", default="mcp-readiness")
    parser.add_argument("--redirect-uri", default="http://localhost:8080/auth/admin/callback")
    args = parser.parse_args()
    try:
        print(json.dumps(asyncio.run(_run(args)), indent=2))
    except Exception as exc:
        print(json.dumps({"passed": False, "error": str(exc)}, indent=2), file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
