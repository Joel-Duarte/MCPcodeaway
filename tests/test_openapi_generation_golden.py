"""Golden coverage for importing API descriptions into generated gateway tools."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import httpx
import pytest

from gateway.config import UpstreamConfig
from gateway.upstream_clients import UpstreamClientManager

_GOLDEN = Path(__file__).parent / "golden" / "openapi_to_tools.json"


@pytest.mark.asyncio
async def test_openapi_golden_import_creates_expected_tools_and_calls_api() -> None:
    fixture = json.loads(_GOLDEN.read_text(encoding="utf-8"))
    spec = {key: value for key, value in fixture.items() if key != "expected_tools"}
    requests_seen: list[dict[str, Any]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests_seen.append(
            {
                "method": request.method,
                "path": request.url.raw_path.split(b"?", maxsplit=1)[0].decode("ascii"),
                "query": dict(request.url.params),
                "authorization": request.headers.get("authorization"),
                "cookie": request.headers.get("cookie"),
                "body": json.loads(request.content) if request.content else None,
            }
        )
        if request.url.raw_path.split(b"?", maxsplit=1)[0] == b"/api/records/a%2Fb":
            return httpx.Response(
                200,
                json={"id": "a/b", "title": "Example", "owner": "tenant-a"},
                headers={"Set-Cookie": "api_session=must-not-be-reused; HttpOnly"},
            )
        return httpx.Response(201, json={"id": "generated-1", **json.loads(request.content)})

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    manager = UpstreamClientManager(http_client=client)
    upstream = UpstreamConfig(
        name="inventory_api",
        type="openapi",
        url="https://inventory.example/api",
        auth_type="none",
        openapi_spec=spec,
    )
    try:
        tools = await manager.discover_tools(upstream)
        expected = fixture["expected_tools"]
        assert len(tools) == len(expected)
        for generated, golden in zip(tools, expected, strict=True):
            assert generated.name == golden["name"]
            assert generated.qualified_name == golden["qualified_name"]
            assert generated.http_method == golden["method"]
            assert generated.http_path == golden["path"]
            assert generated.path_params == golden["path_params"]
            assert generated.query_params == golden["query_params"]
            assert generated.input_schema.get("required", []) == golden["required"]

        got = await manager.call_tool(
            upstream,
            tools[0],
            {"record_id": "a/b", "include_history": True},
            auth_headers={"Authorization": "Bearer tenant-a-token"},
        )
        created = await manager.call_tool(
            upstream,
            tools[1],
            {"title": "Next", "owner": "tenant-b"},
            auth_headers={"Authorization": "Bearer tenant-b-token"},
        )

        assert got["id"] == "a/b"
        assert created["id"] == "generated-1"
        assert requests_seen[0] == {
            "method": "GET",
            "path": "/api/records/a%2Fb",
            "query": {"include_history": "true"},
            "authorization": "Bearer tenant-a-token",
            "cookie": None,
            "body": None,
        }
        assert requests_seen[1]["authorization"] == "Bearer tenant-b-token"
        assert requests_seen[1]["cookie"] is None
        assert requests_seen[1]["body"] == {"title": "Next", "owner": "tenant-b"}
    finally:
        await client.aclose()


def test_openapi_import_rejects_cookie_session_requirements() -> None:
    manager = UpstreamClientManager()
    upstream = UpstreamConfig(name="cookie_api", type="openapi", url="https://api.example")
    spec = {
        "openapi": "3.1.0",
        "components": {
            "securitySchemes": {
                "browserSession": {"type": "apiKey", "in": "cookie", "name": "session"}
            }
        },
        "security": [{"browserSession": []}],
        "paths": {"/items": {"get": {"operationId": "listItems", "responses": {}}}},
    }
    with pytest.raises(ValueError, match="cookie-based API sessions are unsupported"):
        manager._discover_openapi_from_spec(upstream, spec)


def test_upstream_config_rejects_legacy_mcp_session_setting() -> None:
    with pytest.raises(ValueError, match="stateful_session is not supported"):
        UpstreamConfig(
            name="legacy_server",
            type="mcp_http",
            url="https://mcp.example/mcp",
            stateful_session=True,
        )
