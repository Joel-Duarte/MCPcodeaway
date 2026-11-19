"""Stateless Streamable HTTP Code-Mode MCP Gateway FastAPI application."""

from __future__ import annotations

import json
import time
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

import httpx
from fastapi import FastAPI, Header, HTTPException, Query, Request
from fastapi.responses import HTMLResponse, JSONResponse
from pydantic import BaseModel

from gateway import __version__
from gateway.auth import (
    DEV_PERSONAS,
    AuthError,
    CallerIdentity,
    DelegatedTokenExchanger,
    LocalDevKeyManager,
    OidcTokenValidator,
    PolicyEvaluator,
)
from gateway.catalog import ToolCatalog, estimate_tokens, to_mcp_tool_definition
from gateway.comparator import audit_catalog_comparison, run_side_by_side_comparison
from gateway.config import (
    DiscoveryBackendMode,
    GatewayMode,
    GatewaySettings,
    UpstreamConfig,
    UpstreamRegistry,
)
from gateway.discovery import DiscoveryEngine
from gateway.dispatcher import (
    ExecutionBudget,
    HostDispatcher,
    SubcallRecord,
    bind_positional_arguments,
)
from gateway.oauth_discovery import create_oauth_discovery_router
from gateway.sandbox import LocalMontyBackend, RemoteMontyWorkerBackend, SandboxBackend
from gateway.state import (
    AuditStore,
    CacheBackend,
    InMemoryCacheBackend,
    RedisCacheBackend,
    SQLiteAuditStore,
)
from gateway.telemetry import TelemetryRecorder, configure_telemetry
from gateway.upstream_clients import DiscoveredTool, UpstreamClientManager, inspect_upstream

_VALID_MODES: set[str] = {"codemode", "traditional", "traditional_discover", "hybrid"}
_VALID_DISCOVERY: set[str] = {"keyword", "bm25", "embedding", "system1", "hybrid_system1"}

LIST_SERVERS_DEF: dict[str, Any] = {
    "name": "list_servers",
    "description": (
        "List RBAC-authorized upstream MCP servers and OpenAPI services available to you, "
        "including tool counts and sample tool names."
    ),
    "inputSchema": {
        "type": "object",
        "properties": {
            "query": {
                "type": "string",
                "default": "",
                "description": "Optional keyword filter across server names and tools.",
            }
        },
    },
    "annotations": {"readOnlyHint": True},
}

SEARCH_TOOLS_DEF: dict[str, Any] = {
    "name": "search_tools",
    "description": (
        "Search authorized upstream MCP tools and OpenAPI endpoints across one or more "
        "servers, returning compact typed Python stubs (or full JSON Schemas in "
        "traditional_discover mode)."
    ),
    "inputSchema": {
        "type": "object",
        "properties": {
            "query": {
                "type": "string",
                "default": "",
                "description": "Natural-language or keyword query (e.g. 'overdue invoices').",
            },
            "server": {
                "type": "string",
                "description": "Optional single upstream server slug filter.",
            },
            "servers": {
                "type": "array",
                "items": {"type": "string"},
                "description": "Optional list of upstream server slugs to search across.",
            },
            "limit": {
                "type": "integer",
                "default": 15,
                "minimum": 1,
                "maximum": 50,
            },
        },
    },
    "annotations": {"readOnlyHint": True},
}

EXECUTE_CODE_DEF: dict[str, Any] = {
    "name": "execute_code",
    "description": (
        "Execute sandboxed Python inside Pydantic Monty (Rust Python) with async access to "
        "authorized upstream tools discovered via search_tools. Top-level await and "
        "'import asyncio' / asyncio.gather work; do not import external modules like 'json' "
        "or 'requests'; make your final expression the compact result dict/list."
    ),
    "inputSchema": {
        "type": "object",
        "properties": {
            "code": {
                "type": "string",
                "description": "Python script to execute inside the Monty sandbox.",
            },
            "servers": {
                "type": "array",
                "items": {"type": "string"},
                "description": (
                    "Optional least-privilege server allowlist for this run "
                    "(bounded by any request-level server ceiling)."
                ),
            },
            "timeout_ms": {
                "type": "integer",
                "default": 10000,
                "minimum": 100,
                "maximum": 30000,
            },
        },
        "required": ["code"],
    },
    "annotations": {"readOnlyHint": False},
}


class GatewayRuntime:
    """Assembled gateway dependencies shared across HTTP routes, comparator, and benchmarks."""

    def __init__(
        self,
        *,
        settings: GatewaySettings | None = None,
        cache: CacheBackend | None = None,
        store: AuditStore | None = None,
        dev_keys: LocalDevKeyManager | None = None,
        http_client: httpx.AsyncClient | None = None,
        discovery_http_client: httpx.AsyncClient | None = None,
        sandbox_backend: SandboxBackend | None = None,
        preloaded_tools: dict[str, list[DiscoveredTool]] | None = None,
    ) -> None:
        self.settings = settings or GatewaySettings()
        configure_telemetry()
        if cache is not None:
            self.cache: CacheBackend = cache
        elif self.settings.redis_url:
            self.cache = RedisCacheBackend(self.settings.redis_url)
        else:
            self.cache = InMemoryCacheBackend()
        self.store = store or SQLiteAuditStore(self.settings.sqlite_path)
        self.dev_keys = dev_keys
        if self.dev_keys is None and self.settings.auth_mode == "dev":
            self.dev_keys = LocalDevKeyManager(issuer=self.settings.oidc_issuer)
        self.validator = OidcTokenValidator(
            settings=self.settings, dev_keys=self.dev_keys, http_client=http_client
        )
        self.policy = PolicyEvaluator(self.settings)
        self.exchanger = DelegatedTokenExchanger(
            settings=self.settings,
            cache=self.cache,
            dev_keys=self.dev_keys,
            http_client=http_client,
        )
        self.registry = UpstreamRegistry(self.settings, store=self.store)
        self.client_manager = UpstreamClientManager(http_client=http_client)
        self.catalog = ToolCatalog()
        self.discovery_engine = DiscoveryEngine(
            self.settings, http_client=discovery_http_client
        )
        self.dispatcher = HostDispatcher(
            settings=self.settings,
            policy=self.policy,
            exchanger=self.exchanger,
            cache=self.cache,
            client_manager=self.client_manager,
            catalog=self.catalog,
        )
        if sandbox_backend is not None:
            self.sandbox: SandboxBackend = sandbox_backend
        elif self.settings.sandbox_backend == "remote":
            if not self.settings.sandbox_worker_url:
                raise ValueError(
                    "sandbox_worker_url must be configured when sandbox_backend='remote'"
                )
            self.sandbox = RemoteMontyWorkerBackend(self.settings.sandbox_worker_url)
        else:
            self.sandbox = LocalMontyBackend()
        self.telemetry = TelemetryRecorder(self.store)
        self.preloaded_tools = preloaded_tools or {}
        self.startup_warnings: list[str] = []

    def _dev_discovery_identity(self) -> CallerIdentity | None:
        if self.dev_keys is None:
            return None
        tenant = (
            self.settings.allowed_tenants[0]
            if self.settings.allowed_tenants
            else "tenant-acme-dev"
        )
        token = self.dev_keys.issue_user_token(
            overrides={
                "sub": "gateway-discovery",
                "tenant_id": tenant,
                "roles": [
                    "admin",
                    "finance_analyst",
                    "ops_engineer",
                    "snowflake_reader",
                    "billing_reader",
                    "ticketing_writer",
                ],
                "groups": ["finance", "operations", "audit", "support", "platform"],
                "scopes": ["mcp.tools", "finance.read", "ops.read", "ops.write"],
                "client_id": self.settings.oidc_client_id,
            }
        )
        return CallerIdentity(
            sub="gateway-discovery",
            tenant_id=tenant,
            client_id=self.settings.oidc_client_id,
            roles=[
                "admin",
                "finance_analyst",
                "ops_engineer",
                "snowflake_reader",
                "billing_reader",
                "ticketing_writer",
            ],
            groups=["finance", "operations", "audit", "support", "platform"],
            scopes=["mcp.tools", "finance.read", "ops.read", "ops.write"],
            raw_token=token,
        )

    async def refresh_catalog(self, identity: CallerIdentity | None = None) -> list[str]:
        await self.registry.refresh()
        upstreams = self.registry.list_upstreams()
        active_names = {u.name for u in upstreams}
        for existing in list(self.catalog.digests_by_server):
            if existing not in active_names and existing not in self.preloaded_tools:
                self.catalog.remove_server(existing)

        for srv_name, tools in self.preloaded_tools.items():
            cfg = self.registry.get(srv_name) or UpstreamConfig(
                name=srv_name, type="mcp_http", url="http://preloaded.local", auth_type="none"
            )
            self.catalog.set_server_tools(cfg, tools)

        effective_identity = identity or self._dev_discovery_identity()
        warnings: list[str] = []
        for upstream in upstreams:
            if upstream.name in self.preloaded_tools:
                continue
            headers: dict[str, str] = {}
            try:
                if upstream.type == "openapi" and upstream.openapi_spec_url:
                    headers = {}
                elif upstream.auth_type != "delegated_token":
                    headers, _, _ = await self.dispatcher.resolve_upstream_headers(
                        effective_identity
                        or CallerIdentity(sub="gateway-discovery", tenant_id="system"),
                        upstream,
                    )
                elif effective_identity is not None:
                    headers, _, _ = await self.dispatcher.resolve_upstream_headers(
                        effective_identity, upstream
                    )
                else:
                    continue
                tools = await self.client_manager.discover_tools(upstream, auth_headers=headers)
                self.catalog.set_server_tools(upstream, tools)
            except Exception as exc:
                warnings.append(f"Upstream '{upstream.name}' discovery failed: {exc}")
        self.startup_warnings = warnings
        return warnings

    async def ensure_catalog_for_identity(self, identity: CallerIdentity) -> None:
        """Lazily introspect any accessible upstreams not yet cached in catalog."""
        for upstream in self.registry.list_upstreams():
            if upstream.name in self.catalog.digests_by_server:
                continue
            if not self.policy.can_access_upstream(identity, upstream)[0]:
                continue
            try:
                headers, _, _ = await self.dispatcher.resolve_upstream_headers(identity, upstream)
                tools = await self.client_manager.discover_tools(upstream, auth_headers=headers)
                self.catalog.set_server_tools(upstream, tools)
            except Exception:
                continue

    async def startup(self) -> None:
        await self.store.init()
        await self.refresh_catalog()

    async def shutdown(self) -> None:
        await self.sandbox.close()
        if hasattr(self.cache, "close"):
            await self.cache.close()  # type: ignore[attr-defined]
        if hasattr(self.store, "close"):
            await self.store.close()  # type: ignore[attr-defined]

    def tools_for_mode(
        self,
        identity: CallerIdentity,
        mode: GatewayMode,
        *,
        allowed_servers: set[str] | None = None,
    ) -> list[dict[str, Any]]:
        """Build the baseline MCP tools/list payload for the requested gateway mode."""
        accessible = self.catalog.list_accessible_tools(
            identity, self.policy, self.registry, allowed_servers=allowed_servers
        )
        if mode == "codemode":
            return [LIST_SERVERS_DEF, SEARCH_TOOLS_DEF, EXECUTE_CODE_DEF]
        if mode == "traditional_discover":
            return [LIST_SERVERS_DEF, SEARCH_TOOLS_DEF]
        if mode == "traditional":
            return [to_mcp_tool_definition(tool) for tool in accessible]
        # hybrid: Code Mode for orchestration + direct MCP definitions for mutating writes
        direct_mutating = [to_mcp_tool_definition(tool) for tool in accessible if tool.mutating]
        return [LIST_SERVERS_DEF, SEARCH_TOOLS_DEF, EXECUTE_CODE_DEF, *direct_mutating]

    async def list_tools_for_request(
        self,
        identity: CallerIdentity,
        mode: GatewayMode,
        *,
        allowed_servers: set[str] | None = None,
        discovery_backend: DiscoveryBackendMode | None = None,
        intent: str | None = None,
        query: str | None = None,
        requested_servers: list[str] | None = None,
        limit: int | None = None,
        cursor: str | None = None,
    ) -> dict[str, Any]:
        """Build tools/list response supporting X-Intent zero-turn hydration and pagination."""
        effective_ceiling = allowed_servers
        if requested_servers:
            req_set = set(requested_servers)
            effective_ceiling = (
                (effective_ceiling & req_set) if effective_ceiling is not None else req_set
            )

        filter_text = (intent or query or "").strip()
        if filter_text:
            top_k = min(limit or 10, 20)
            discovered = await self.catalog.discover_tools_for_caller(
                identity,
                self.policy,
                self.registry,
                self.discovery_engine,
                query=filter_text,
                allowed_servers=effective_ceiling,
                backend=discovery_backend,
                limit=top_k,
                include_schemas=(mode in {"traditional", "traditional_discover"}),
            )
            if mode == "traditional":
                tools = list(discovered["hydrated_tools"])
            elif mode == "traditional_discover":
                tools = [LIST_SERVERS_DEF, SEARCH_TOOLS_DEF, *discovered["hydrated_tools"]]
            else:
                exec_copy = dict(EXECUTE_CODE_DEF)
                stubs = "\n\n".join(t["stub"] for t in discovered["tools"])
                if stubs:
                    exec_copy["description"] = (
                        f"{EXECUTE_CODE_DEF['description']}\n\n"
                        f"Pre-discovered matching stubs ({discovered['backend_used']}):\n{stubs}"
                    )
                if mode == "codemode":
                    tools = [LIST_SERVERS_DEF, SEARCH_TOOLS_DEF, exec_copy]
                else:
                    accessible = self.catalog.list_accessible_tools(
                        identity, self.policy, self.registry, allowed_servers=effective_ceiling
                    )
                    direct_mutating = [
                        to_mcp_tool_definition(t) for t in accessible if t.mutating
                    ]
                    tools = [LIST_SERVERS_DEF, SEARCH_TOOLS_DEF, exec_copy, *direct_mutating]
        else:
            tools = self.tools_for_mode(identity, mode, allowed_servers=effective_ceiling)

        if cursor is not None or limit is not None:
            offset = max(0, int(cursor)) if cursor else 0
            page_size = max(1, min(limit or len(tools) or 1, 200))
            page = tools[offset : offset + page_size]
            out: dict[str, Any] = {"tools": page}
            if offset + page_size < len(tools):
                out["nextCursor"] = str(offset + page_size)
            return out
        return {"tools": tools}

    async def execute_code_for_identity(
        self,
        identity: CallerIdentity,
        code: str,
        *,
        mode: GatewayMode = "codemode",
        servers: list[str] | None = None,
        allowed_servers: set[str] | None = None,
        timeout_ms: int = 10000,
    ) -> dict[str, Any]:
        """Run Code Mode Python script in SandboxBackend and record audit telemetry."""
        started = time.perf_counter()
        await self.ensure_catalog_for_identity(identity)
        if allowed_servers is not None and servers is not None:
            effective_servers: set[str] | None = allowed_servers & set(servers)
        elif allowed_servers is not None:
            effective_servers = allowed_servers
        elif servers is not None:
            effective_servers = set(servers)
        else:
            effective_servers = None

        accessible = self.catalog.list_accessible_tools(
            identity, self.policy, self.registry, allowed_servers=effective_servers
        )
        schema_tokens = estimate_tokens(
            self.tools_for_mode(identity, mode, allowed_servers=allowed_servers)
        )

        budget = ExecutionBudget(self.settings.max_calls_per_execution)
        subcalls: list[SubcallRecord] = []
        external_functions: dict[str, Callable[..., Any]] = {}

        for tool in accessible:
            if mode == "hybrid" and tool.mutating:
                continue
            upstream = self.registry.get(tool.server)
            if upstream is None:
                continue

            def _make_wrapper(t: DiscoveredTool, u: UpstreamConfig) -> Callable[..., Any]:
                async def _wrapper(*args: Any, **kwargs: Any) -> Any:
                    bound = bind_positional_arguments(t, args, kwargs)
                    res, rec = await self.dispatcher.invoke_tool(
                        identity,
                        t,
                        u,
                        bound,
                        budget=budget,
                        code_mode_hints=True,
                    )
                    subcalls.append(rec)
                    if isinstance(res, dict) and "_dispatch_error" in res:
                        raise RuntimeError(res["_dispatch_error"])
                    return res

                return _wrapper

            external_functions[tool.qualified_name] = _make_wrapper(tool, upstream)

        timeout_seconds = min(timeout_ms / 1000.0, self.settings.monty_timeout_seconds)
        sandbox_res = await self.sandbox.execute(
            code,
            external_functions=external_functions,
            timeout_seconds=timeout_seconds,
        )
        duration_ms = round((time.perf_counter() - started) * 1000, 3)

        payload: dict[str, Any] = {
            "ok": sandbox_res.ok,
            "result": sandbox_res.result,
            "stdout": sandbox_res.stdout,
            "truncated": sandbox_res.truncated,
            "subcall_count": len(subcalls),
            "duration_ms": duration_ms,
        }
        if not sandbox_res.ok:
            payload["error"] = sandbox_res.error
            payload["error_type"] = sandbox_res.error_type

        run = await self.telemetry.record_execution(
            mode=mode,
            identity=identity,
            catalog_version=self.catalog.catalog_version,
            schema_tokens=schema_tokens,
            subcalls=subcalls,
            returned_payload=payload,
            duration_ms=duration_ms,
            ok=sandbox_res.ok,
            code=code,
            stdout=sandbox_res.stdout,
            error=sandbox_res.error,
            tool_name="execute_code",
        )
        payload["run_id"] = run["run_id"]
        payload["raw_upstream_bytes"] = run["raw_upstream_bytes"]
        payload["returned_bytes"] = run["returned_bytes"]
        payload["estimated_tokens_saved"] = run["estimated_tokens_saved"]
        return payload

    async def call_direct_tool_for_identity(
        self,
        identity: CallerIdentity,
        tool_name: str,
        arguments: dict[str, Any],
        *,
        mode: GatewayMode = "traditional",
        allowed_servers: set[str] | None = None,
    ) -> dict[str, Any]:
        """Invoke a single upstream tool directly (traditional / traditional_discover / hybrid)."""
        started = time.perf_counter()
        await self.ensure_catalog_for_identity(identity)
        tool = self.catalog.get_tool(tool_name)
        if tool is None:
            raise KeyError(f"Unknown tool '{tool_name}'")
        if allowed_servers is not None and tool.server not in allowed_servers:
            raise PermissionError(
                f"Server '{tool.server}' is outside the request-level server ceiling"
            )
        if mode == "hybrid" and not tool.mutating:
            raise PermissionError(
                f"Non-mutating tool '{tool_name}' should run via execute_code in hybrid mode"
            )
        upstream = self.registry.get(tool.server)
        if upstream is None:
            raise KeyError(f"Upstream '{tool.server}' not found")

        schema_tokens = estimate_tokens(
            self.tools_for_mode(identity, mode, allowed_servers=allowed_servers)
        )
        result, rec = await self.dispatcher.invoke_tool(
            identity, tool, upstream, arguments, code_mode_hints=False
        )
        duration_ms = round((time.perf_counter() - started) * 1000, 3)
        ok = rec.status == "ok"
        run = await self.telemetry.record_execution(
            mode=mode,
            identity=identity,
            catalog_version=self.catalog.catalog_version,
            schema_tokens=schema_tokens,
            subcalls=[rec],
            returned_payload=result,
            duration_ms=duration_ms,
            ok=ok,
            error=rec.error,
            tool_name=tool.qualified_name,
        )
        return {
            "ok": ok,
            "result": result,
            "error": rec.error,
            "run_id": run["run_id"],
            "raw_upstream_bytes": run["raw_upstream_bytes"],
            "returned_bytes": run["returned_bytes"],
            "duration_ms": duration_ms,
        }


def _resolve_mode(
    default_mode: GatewayMode,
    query_mode: str | None,
    header_mode: str | None,
) -> GatewayMode:
    chosen = query_mode or header_mode or default_mode
    if chosen not in _VALID_MODES:
        raise HTTPException(
            status_code=400,
            detail=f"Invalid gateway mode '{chosen}'. Expected one of: {sorted(_VALID_MODES)}",
        )
    return chosen  # type: ignore[return-value]


def _resolve_discovery(
    default_backend: DiscoveryBackendMode,
    query_backend: str | None,
    header_backend: str | None,
) -> DiscoveryBackendMode:
    chosen = query_backend or header_backend or default_backend
    if chosen not in _VALID_DISCOVERY:
        raise HTTPException(
            status_code=400,
            detail=(
                f"Invalid discovery backend '{chosen}'. "
                f"Expected one of: {sorted(_VALID_DISCOVERY)}"
            ),
        )
    return chosen  # type: ignore[return-value]


def _parse_servers_ceiling(query_val: str | None, header_val: str | None) -> set[str] | None:
    raw = query_val if query_val is not None else header_val
    if raw is None or not raw.strip():
        return None
    parts = {item.strip() for item in raw.split(",") if item.strip()}
    return parts or None


def _jsonrpc_ok(req_id: Any, result: Any) -> JSONResponse:
    return JSONResponse({"jsonrpc": "2.0", "id": req_id, "result": result})


def _jsonrpc_err(req_id: Any, code: int, message: str) -> JSONResponse:
    return JSONResponse(
        {"jsonrpc": "2.0", "id": req_id, "error": {"code": code, "message": message}}
    )


class CompareRequest(BaseModel):
    scenario: str = "reconcile_overdue"
    persona: str = "finance_analyst"
    custom_classic_calls: list[dict[str, Any]] | None = None
    custom_code: str | None = None


def create_gateway_app(
    *,
    settings: GatewaySettings | None = None,
    cache: CacheBackend | None = None,
    store: AuditStore | None = None,
    dev_keys: LocalDevKeyManager | None = None,
    http_client: httpx.AsyncClient | None = None,
    discovery_http_client: httpx.AsyncClient | None = None,
    sandbox_backend: SandboxBackend | None = None,
    preloaded_tools: dict[str, list[DiscoveredTool]] | None = None,
) -> FastAPI:
    """Create the FastAPI stateless MCP gateway application."""
    runtime = GatewayRuntime(
        settings=settings,
        cache=cache,
        store=store,
        dev_keys=dev_keys,
        http_client=http_client,
        discovery_http_client=discovery_http_client,
        sandbox_backend=sandbox_backend,
        preloaded_tools=preloaded_tools,
    )

    @asynccontextmanager
    async def lifespan(_: FastAPI) -> AsyncIterator[None]:
        await runtime.startup()
        try:
            yield
        finally:
            await runtime.shutdown()

    app = FastAPI(
        title="MCPcodeaway Gateway",
        version=__version__,
        lifespan=lifespan,
    )
    app.state.runtime = runtime
    app.include_router(
        create_oauth_discovery_router(runtime.settings, runtime.dev_keys, runtime.validator)
    )

    @app.get("/health")
    async def health() -> dict[str, Any]:
        return {
            "status": "ok",
            "version": __version__,
            "app_env": runtime.settings.app_env,
            "auth_mode": runtime.settings.auth_mode,
            "default_mode": runtime.settings.gateway_mode,
            "catalog_version": runtime.catalog.catalog_version,
            "upstreams": len(runtime.registry.list_upstreams()),
            "drift_warnings": runtime.catalog.drift_warnings,
            "startup_warnings": runtime.startup_warnings,
        }

    @app.post("/mcp")
    async def mcp_endpoint(
        request: Request,
        mode: str | None = Query(default=None),
        discovery: str | None = Query(default=None),
        servers: str | None = Query(default=None),
        x_gateway_mode: str | None = Header(default=None),
        x_discovery_backend: str | None = Header(default=None),
        x_mcp_servers: str | None = Header(default=None),
        x_intent: str | None = Header(default=None),
        authorization: str | None = Header(default=None),
    ) -> JSONResponse:
        try:
            identity = await runtime.validator.validate_authorization_header(authorization)
        except AuthError as exc:
            headers = {"WWW-Authenticate": exc.www_authenticate} if exc.www_authenticate else None
            raise HTTPException(
                status_code=exc.status_code, detail=exc.detail, headers=headers
            ) from exc

        effective_mode = _resolve_mode(runtime.settings.gateway_mode, mode, x_gateway_mode)
        effective_discovery = _resolve_discovery(
            runtime.settings.discovery_backend, discovery, x_discovery_backend
        )
        allowed_servers = _parse_servers_ceiling(servers, x_mcp_servers)
        payload = await request.json()
        req_id = payload.get("id")
        method = payload.get("method")
        params = payload.get("params") or {}

        if method == "initialize":
            return _jsonrpc_ok(
                req_id,
                {
                    "protocolVersion": "2025-03-26",
                    "capabilities": {"tools": {"listChanged": True}},
                    "serverInfo": {"name": "mcpcodeaway", "version": __version__},
                },
            )
        if method == "server/discover":
            return _jsonrpc_ok(
                req_id,
                {
                    "supportedVersions": ["2025-03-26"],
                    "capabilities": {"tools": {"listChanged": True}},
                    "ttlMs": 30000,
                    "cacheScope": "private",
                    "instructions": (
                        "Stateless multi-MCP gateway. Call list_servers then search_tools "
                        "(or pass X-Intent / ?servers=...) before calling execute_code or "
                        "hydrated tools."
                    ),
                },
            )
        if method == "notifications/initialized":
            return JSONResponse({}, status_code=202)

        await runtime.ensure_catalog_for_identity(identity)

        if method == "tools/list":
            req_srvs = params.get("servers")
            if not req_srvs and params.get("server"):
                req_srvs = [str(params["server"])]
            listed = await runtime.list_tools_for_request(
                identity,
                effective_mode,
                allowed_servers=allowed_servers,
                discovery_backend=effective_discovery,
                intent=x_intent,
                query=params.get("query"),
                requested_servers=req_srvs,
                limit=params.get("limit"),
                cursor=params.get("cursor"),
            )
            return _jsonrpc_ok(req_id, listed)

        if method == "tools/call":
            tool_name = str(params.get("name") or "")
            arguments = dict(params.get("arguments") or {})

            if tool_name == "list_servers":
                srv_rows = runtime.catalog.list_accessible_servers(
                    identity,
                    runtime.policy,
                    runtime.registry,
                    allowed_servers=allowed_servers,
                    query=str(arguments.get("query", "")),
                )
                srv_payload = {
                    "catalog_version": runtime.catalog.catalog_version,
                    "server_count": len(srv_rows),
                    "servers": srv_rows,
                }
                return _jsonrpc_ok(
                    req_id,
                    {
                        "content": [{"type": "text", "text": json.dumps(srv_payload)}],
                        "structuredContent": srv_payload,
                    },
                )

            if tool_name == "search_tools":
                if effective_mode == "traditional":
                    return _jsonrpc_err(
                        req_id, -32601, "search_tools is disabled in traditional mode"
                    )
                req_srvs = arguments.get("servers")
                if not req_srvs and arguments.get("server"):
                    req_srvs = [str(arguments["server"])]
                res = await runtime.catalog.discover_tools_for_caller(
                    identity,
                    runtime.policy,
                    runtime.registry,
                    runtime.discovery_engine,
                    query=str(arguments.get("query", "")),
                    servers=req_srvs,
                    allowed_servers=allowed_servers,
                    backend=effective_discovery,
                    limit=int(arguments.get("limit", 15)),
                    include_schemas=(effective_mode == "traditional_discover"),
                )
                return _jsonrpc_ok(
                    req_id,
                    {
                        "content": [{"type": "text", "text": json.dumps(res)}],
                        "structuredContent": res,
                    },
                )

            if tool_name == "execute_code":
                if effective_mode in {"traditional", "traditional_discover"}:
                    return _jsonrpc_err(
                        req_id, -32601, f"execute_code is disabled in {effective_mode} mode"
                    )
                code = str(arguments.get("code") or "")
                if not code.strip():
                    return _jsonrpc_err(req_id, -32602, "execute_code requires non-empty 'code'")
                res = await runtime.execute_code_for_identity(
                    identity,
                    code,
                    mode=effective_mode,
                    servers=arguments.get("servers"),
                    allowed_servers=allowed_servers,
                    timeout_ms=int(arguments.get("timeout_ms", 10000)),
                )
                return _jsonrpc_ok(
                    req_id,
                    {
                        "content": [{"type": "text", "text": json.dumps(res, default=str)}],
                        "structuredContent": res,
                        "isError": not res["ok"],
                    },
                )

            if effective_mode == "codemode":
                return _jsonrpc_err(
                    req_id,
                    -32601,
                    f"Direct tool '{tool_name}' is not exposed in codemode; "
                    "discover via search_tools and invoke inside execute_code.",
                )

            try:
                res = await runtime.call_direct_tool_for_identity(
                    identity,
                    tool_name,
                    arguments,
                    mode=effective_mode,
                    allowed_servers=allowed_servers,
                )
            except KeyError as exc:
                return _jsonrpc_err(req_id, -32601, str(exc))
            except PermissionError as exc:
                return _jsonrpc_err(req_id, -32003, str(exc))
            except (ValueError, RuntimeError) as exc:
                return _jsonrpc_err(req_id, -32602, str(exc))

            return _jsonrpc_ok(
                req_id,
                {
                    "content": [{"type": "text", "text": json.dumps(res, default=str)}],
                    "structuredContent": res,
                    "isError": not res["ok"],
                },
            )

        return _jsonrpc_err(req_id, -32601, f"Unsupported MCP method '{method}'")

    @app.get("/audit/runs")
    async def audit_runs(
        limit: int = Query(default=50, ge=1, le=200),
        tenant_id: str | None = Query(default=None),
    ) -> dict[str, Any]:
        runs = await runtime.store.list_runs(limit=limit, tenant_id=tenant_id)
        return {"count": len(runs), "runs": runs}

    @app.get("/audit/runs/{run_id}")
    async def audit_run_detail(run_id: str) -> dict[str, Any]:
        run = await runtime.store.get_run(run_id)
        if run is None:
            raise HTTPException(status_code=404, detail=f"Run '{run_id}' not found")
        return run

    @app.get("/audit/summary")
    async def audit_summary() -> dict[str, Any]:
        return await runtime.telemetry.summary()

    @app.get("/admin/upstreams")
    async def list_admin_upstreams() -> dict[str, Any]:
        upstreams = runtime.registry.list_upstreams(include_disabled=True)
        return {
            "app_env": runtime.settings.app_env,
            "dynamic_upstreams_permitted": runtime.settings.dynamic_upstreams_permitted,
            "catalog_version": runtime.catalog.catalog_version,
            "digests_by_server": runtime.catalog.digests_by_server,
            "drift_warnings": runtime.catalog.drift_warnings,
            "upstreams": [u.model_dump(exclude={"openapi_spec"}) for u in upstreams],
            "comparison_audit": audit_catalog_comparison(runtime.catalog, runtime.registry),
        }

    async def _inspect_candidate(
        cfg: UpstreamConfig, authorization: str | None
    ) -> tuple[dict[str, Any], dict[str, Any]]:
        headers: dict[str, str] = {}
        if authorization:
            try:
                caller = await runtime.validator.validate_authorization_header(authorization)
                headers, _, _ = await runtime.dispatcher.resolve_upstream_headers(caller, cfg)
            except AuthError as exc:
                raise HTTPException(status_code=exc.status_code, detail=exc.detail) from exc
        elif cfg.auth_type != "none":
            dev_id = runtime._dev_discovery_identity()
            if cfg.auth_type == "delegated_token" and dev_id is not None:
                headers, _, _ = await runtime.dispatcher.resolve_upstream_headers(dev_id, cfg)
            elif cfg.auth_type in {"api_key", "bearer_secret"}:
                headers, _, _ = await runtime.dispatcher.resolve_upstream_headers(
                    CallerIdentity(sub="inspector", tenant_id="system"), cfg
                )
        try:
            inspection = await inspect_upstream(cfg, runtime.client_manager, auth_headers=headers)
        except Exception as exc:
            raise HTTPException(
                status_code=400, detail=f"Failed to inspect upstream '{cfg.name}': {exc}"
            ) from exc
        discovered = [DiscoveredTool.model_validate(t) for t in inspection["tools"]]
        audit = audit_catalog_comparison(
            runtime.catalog,
            runtime.registry,
            candidate_name=cfg.name,
            candidate_tools=discovered,
        )
        return inspection, audit

    @app.post("/admin/upstreams/inspect")
    async def inspect_admin_upstream(
        cfg: UpstreamConfig,
        authorization: str | None = Header(default=None),
    ) -> dict[str, Any]:
        inspection, audit = await _inspect_candidate(cfg, authorization)
        return {"inspection": inspection, "onboarding_audit": audit}

    @app.post("/admin/upstreams")
    async def add_admin_upstream(
        cfg: UpstreamConfig,
        authorization: str | None = Header(default=None),
    ) -> dict[str, Any]:
        if not runtime.settings.dynamic_upstreams_permitted:
            raise HTTPException(
                status_code=403,
                detail="Dynamic upstream registration is locked in read-only/prod; "
                "promote changes via config/upstreams.yaml PR.",
            )
        if runtime.settings.app_env == "prod" and cfg.type == "mcp_stdio":
            raise HTTPException(status_code=403, detail="mcp_stdio upstreams are blocked in prod")

        inspection, audit = await _inspect_candidate(cfg, authorization)
        await runtime.store.save_dynamic_upstream(cfg.name, cfg.model_dump(exclude_none=True))
        await runtime.refresh_catalog()
        return {
            "registered": True,
            "upstream": cfg.model_dump(exclude_none=True, exclude={"openapi_spec"}),
            "inspection": inspection,
            "onboarding_audit": audit,
            "catalog_version": runtime.catalog.catalog_version,
        }

    @app.delete("/admin/upstreams/{name}")
    async def delete_admin_upstream(name: str) -> dict[str, Any]:
        if not runtime.settings.dynamic_upstreams_permitted:
            raise HTTPException(
                status_code=403,
                detail="Dynamic upstream deletion is locked in read-only/prod.",
            )
        deleted = await runtime.store.delete_dynamic_upstream(name)
        if not deleted:
            raise HTTPException(
                status_code=404, detail=f"Dynamic upstream '{name}' not found"
            )
        await runtime.refresh_catalog()
        return {
            "deleted": True,
            "name": name,
            "catalog_version": runtime.catalog.catalog_version,
        }

    @app.post("/compare/run")
    async def compare_run(
        body: CompareRequest,
        authorization: str | None = Header(default=None),
    ) -> dict[str, Any]:
        if authorization:
            try:
                identity = await runtime.validator.validate_authorization_header(authorization)
            except AuthError as exc:
                raise HTTPException(status_code=exc.status_code, detail=exc.detail) from exc
        elif runtime.settings.auth_mode == "dev" and runtime.dev_keys is not None:
            if body.persona not in DEV_PERSONAS:
                raise HTTPException(status_code=400, detail=f"Unknown persona '{body.persona}'")
            token = runtime.dev_keys.issue_user_token(persona=body.persona)
            identity = await runtime.validator.validate_token(token)
        else:
            raise HTTPException(status_code=401, detail="Authorization Bearer token required")

        try:
            return await run_side_by_side_comparison(
                runtime,
                identity,
                scenario=body.scenario,
                custom_classic_calls=body.custom_classic_calls,
                custom_code=body.custom_code,
            )
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    @app.get("/benchmarks/latest")
    async def latest_benchmarks() -> dict[str, Any]:
        results_path = Path("benchmarks/results.json")
        load_path = Path("benchmarks/load_results.json")
        live_path = Path("benchmarks/live_llm_results.json")
        discovery_path = Path("benchmarks/discovery_results.json")
        payload: dict[str, Any] = {
            "available": (
                results_path.exists()
                or load_path.exists()
                or live_path.exists()
                or discovery_path.exists()
            ),
            "offline": None,
            "load": None,
            "live_llm": None,
            "discovery": None,
        }
        if results_path.exists():
            payload["offline"] = json.loads(results_path.read_text(encoding="utf-8"))
        if load_path.exists():
            payload["load"] = json.loads(load_path.read_text(encoding="utf-8"))
        if live_path.exists():
            payload["live_llm"] = json.loads(live_path.read_text(encoding="utf-8"))
        if discovery_path.exists():
            payload["discovery"] = json.loads(discovery_path.read_text(encoding="utf-8"))
        return payload

    @app.get("/dashboard", response_class=HTMLResponse)
    async def dashboard() -> HTMLResponse:
        html_path = Path(__file__).parent / "static" / "dashboard.html"
        return HTMLResponse(html_path.read_text(encoding="utf-8"))

    return app


app = create_gateway_app()
