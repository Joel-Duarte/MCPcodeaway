"""Gateway Self-Admin MCP JSON-RPC endpoint for AI-assisted upstream onboarding & ops."""

from __future__ import annotations

import json
from collections.abc import Awaitable, Callable
from typing import TYPE_CHECKING, Any

from fastapi.responses import JSONResponse

from gateway import __version__
from gateway.ab_testing import OnboardingAbTestRequest, run_onboarding_ab_test
from gateway.auth import CallerIdentity
from gateway.config import UpstreamConfig
from gateway.upstream_clients import validate_upstream_url

if TYPE_CHECKING:
    from gateway.server import GatewayRuntime

_ADMIN_MCP_TOOLS: list[dict[str, Any]] = [
    {
        "name": "admin_list_upstreams",
        "description": "List configured upstreams, catalog version, and dynamic edit policy.",
        "inputSchema": {"type": "object", "properties": {}},
        "annotations": {"readOnlyHint": True, "destructiveHint": False},
    },
    {
        "name": "admin_inspect_upstream",
        "description": "Introspect a candidate OpenAPI/MCP upstream and emit onboarding guidance.",
        "inputSchema": {
            "type": "object",
            "properties": {"upstream": {"type": "object"}},
            "required": ["upstream"],
        },
        "annotations": {"readOnlyHint": True, "destructiveHint": False},
    },
    {
        "name": "admin_run_ab_test",
        "description": "Benchmark Classic vs Code Mode on an upstream and recommend mode/split.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "upstream_name": {"type": "string"},
                "upstream": {"type": "object"},
                "intent": {"type": "string"},
            },
        },
        "annotations": {"readOnlyHint": True, "destructiveHint": False},
    },
    {
        "name": "admin_save_upstream",
        "description": "Register or update an upstream configuration and refresh the catalog.",
        "inputSchema": {
            "type": "object",
            "properties": {"upstream": {"type": "object"}},
            "required": ["upstream"],
        },
        "annotations": {"readOnlyHint": False, "destructiveHint": False},
    },
    {
        "name": "admin_get_runtime_telemetry",
        "description": "Read gateway CPU, RSS, active executions, worker health, and store state.",
        "inputSchema": {"type": "object", "properties": {}},
        "annotations": {"readOnlyHint": True, "destructiveHint": False},
    },
    {
        "name": "admin_get_analytics",
        "description": "Read token savings, group/user spend, and Classic vs Code Mode analytics.",
        "inputSchema": {
            "type": "object",
            "properties": {"model": {"type": "string"}},
        },
        "annotations": {"readOnlyHint": True, "destructiveHint": False},
    },
    {
        "name": "admin_demo_traffic_burst",
        "description": "Trigger a live demo /mcp traffic burst across personas and custom servers.",
        "inputSchema": {
            "type": "object",
            "properties": {"count": {"type": "integer", "minimum": 1, "maximum": 24}},
        },
        "annotations": {"readOnlyHint": False, "destructiveHint": False},
    },
]


def _validate_ssrf(runtime: GatewayRuntime, cfg: UpstreamConfig) -> None:
    if not runtime.settings.ssrf_protection_enabled:
        return
    for candidate_url in (cfg.url, cfg.openapi_spec_url):
        validate_upstream_url(
            candidate_url,
            app_env=runtime.settings.app_env,
            block_private_ips_in_prod=runtime.settings.block_private_ips_in_prod,
            allowed_hosts=runtime.settings.allowed_upstream_hosts,
        )


async def handle_admin_mcp_request(
    runtime: GatewayRuntime,
    caller: CallerIdentity | None,
    payload: dict[str, Any],
    *,
    inspect_fn: Callable[[UpstreamConfig], Awaitable[tuple[dict[str, Any], dict[str, Any]]]],
    analytics_fn: Callable[[str | None, str | None], Awaitable[dict[str, Any]]],
    telemetry_fn: Callable[[], Awaitable[dict[str, Any]]],
) -> JSONResponse:
    """Handle JSON-RPC 2.0 requests for the gateway_admin Self-Admin MCP endpoint."""
    req_id = payload.get("id")
    method = payload.get("method")
    params = payload.get("params") or {}

    if method == "initialize":
        return JSONResponse(
            {
                "jsonrpc": "2.0",
                "id": req_id,
                "result": {
                    "protocolVersion": "2025-03-26",
                    "serverInfo": {"name": "mcpcodeaway-self-admin", "version": __version__},
                    "capabilities": {"tools": {}},
                },
            }
        )

    if method == "tools/list":
        return JSONResponse({"jsonrpc": "2.0", "id": req_id, "result": {"tools": _ADMIN_MCP_TOOLS}})

    if method != "tools/call":
        return JSONResponse(
            {
                "jsonrpc": "2.0",
                "id": req_id,
                "error": {"code": -32601, "message": f"Method '{method}' not found"},
            }
        )

    name = params.get("name")
    args = params.get("arguments") or {}

    try:
        out = await _dispatch_admin_tool(
            runtime,
            caller,
            str(name or ""),
            args,
            inspect_fn=inspect_fn,
            analytics_fn=analytics_fn,
            telemetry_fn=telemetry_fn,
        )
    except PermissionError as exc:
        return JSONResponse(
            {"jsonrpc": "2.0", "id": req_id, "error": {"code": -32003, "message": str(exc)}}
        )
    except Exception as exc:
        return JSONResponse(
            {
                "jsonrpc": "2.0",
                "id": req_id,
                "result": {
                    "isError": True,
                    "content": [{"type": "text", "text": str(exc)}],
                    "structuredContent": {"ok": False, "error": str(exc)},
                },
            }
        )

    return JSONResponse(
        {
            "jsonrpc": "2.0",
            "id": req_id,
            "result": {
                "isError": False,
                "content": [{"type": "text", "text": json.dumps(out, default=str)}],
                "structuredContent": out,
            },
        }
    )


async def _dispatch_admin_tool(
    runtime: GatewayRuntime,
    caller: CallerIdentity | None,
    name: str,
    args: dict[str, Any],
    *,
    inspect_fn: Callable[[UpstreamConfig], Awaitable[tuple[dict[str, Any], dict[str, Any]]]],
    analytics_fn: Callable[[str | None, str | None], Awaitable[dict[str, Any]]],
    telemetry_fn: Callable[[], Awaitable[dict[str, Any]]],
) -> dict[str, Any]:
    if name == "admin_list_upstreams":
        return {
            "catalog_version": runtime.catalog.catalog_version,
            "dynamic_upstreams_permitted": runtime.settings.dynamic_upstreams_permitted,
            "upstreams": [u.model_dump(mode="json") for u in runtime.registry.list_upstreams()],
        }

    if name == "admin_inspect_upstream":
        cfg = UpstreamConfig.model_validate(args["upstream"])
        _validate_ssrf(runtime, cfg)
        inspection, audit = await inspect_fn(cfg)
        return {"inspection": inspection, "onboarding_audit": audit}

    if name == "admin_run_ab_test":
        body = OnboardingAbTestRequest.model_validate(args)
        if body.upstream is not None:
            _validate_ssrf(runtime, body.upstream)
        eval_id = (
            caller
            or runtime._dev_discovery_identity()
            or CallerIdentity(
                sub="admin-mcp-ab",
                tenant_id=(
                    runtime.settings.allowed_tenants[0]
                    if runtime.settings.allowed_tenants
                    else "tenant-acme-dev"
                ),
                roles=["gateway_admin", "finance_analyst", "ops_engineer"],
                groups=["platform"],
            )
        )
        return await run_onboarding_ab_test(runtime, eval_id, body)

    if name == "admin_save_upstream":
        if not runtime.settings.dynamic_upstreams_permitted:
            raise PermissionError("Dynamic upstream registration is disabled in prod")
        cfg = UpstreamConfig.model_validate(args["upstream"])
        if cfg.type == "mcp_stdio" and runtime.settings.app_env == "prod":
            raise PermissionError("mcp_stdio upstreams are disabled in prod")
        _validate_ssrf(runtime, cfg)
        inspection, audit = await inspect_fn(cfg)
        await runtime.registry.save_upstream(cfg)
        warnings = await runtime.refresh_catalog()
        epoch = await runtime.bump_catalog_epoch()
        return {
            "saved": True,
            "upstream": cfg.name,
            "catalog_version": runtime.catalog.catalog_version,
            "catalog_epoch": epoch,
            "warnings": warnings,
            "inspection": inspection,
            "onboarding_audit": audit,
        }

    if name == "admin_get_runtime_telemetry":
        return await telemetry_fn()

    if name == "admin_get_analytics":
        return await analytics_fn(args.get("model"), None)

    if name == "admin_demo_traffic_burst":
        if (
            runtime.settings.app_env == "prod"
            or runtime.settings.effective_profile_name == "production"
        ):
            raise PermissionError("Demo traffic generator is disabled in production")
        count = int(args.get("count", 6))
        return await runtime.demo_traffic.burst(count=count)

    raise KeyError(f"Unknown admin MCP tool '{name}'")
