"""Test client defaults for requests that represent compliant MCP clients."""

from __future__ import annotations

from urllib.parse import urlsplit

import httpx
import pytest

from gateway.mcp_protocol import request_headers, request_meta


@pytest.fixture(autouse=True)
def frame_unannotated_mcp_client_posts(monkeypatch: pytest.MonkeyPatch) -> None:
    """Give ordinary test calls the current wire framing unless testing errors.

    Protocol contract tests that supply any MCP framing header retain their
    exact bytes and can assert malformed/unsupported requests are rejected.
    This lets broader business-flow tests focus on the behavior behind a
    current MCP client without repeating protocol boilerplate at each call.
    """
    original_post = httpx.AsyncClient.post
    original_sync_post = httpx.Client.post

    def frame_payload_and_headers(
        url: str, kwargs: dict[str, object]
    ) -> tuple[dict[str, object], bool]:
        path = urlsplit(url).path
        payload = kwargs.get("json")
        if not (path.endswith("/mcp") or path == "/admin/mcp") or not isinstance(payload, dict):
            return kwargs, False
        method = payload.get("method")
        if method not in {"server/discover", "tools/list", "tools/call"}:
            return kwargs, False
        params = payload.get("params")
        if params is None:
            params = {}
            payload["params"] = params
        if not isinstance(params, dict):
            return kwargs, False
        headers = dict(kwargs.get("headers") or {})
        if any(
            key.lower() in {"mcp-protocol-version", "mcp-method", "mcp-name"} for key in headers
        ):
            return kwargs, False
        params.setdefault("_meta", request_meta())
        headers.update(request_headers(str(method), tool_name=params.get("name")))
        kwargs["headers"] = headers
        kwargs["json"] = payload
        return kwargs, True

    async def post_with_current_mcp_framing(
        client: httpx.AsyncClient,
        url: str,
        *args: object,
        **kwargs: object,
    ) -> httpx.Response:
        kwargs, _ = frame_payload_and_headers(url, kwargs)
        return await original_post(client, url, *args, **kwargs)

    def post_with_current_sync_mcp_framing(
        client: httpx.Client,
        url: str,
        *args: object,
        **kwargs: object,
    ) -> httpx.Response:
        updated, _ = frame_payload_and_headers(url, kwargs)
        return original_sync_post(client, url, *args, **updated)

    monkeypatch.setattr(httpx.AsyncClient, "post", post_with_current_mcp_framing)
    monkeypatch.setattr(httpx.Client, "post", post_with_current_sync_mcp_framing)
