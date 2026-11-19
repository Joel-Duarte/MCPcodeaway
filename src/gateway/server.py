"""Stateless Streamable HTTP Code-Mode MCP Gateway FastAPI application."""

from __future__ import annotations

import asyncio
import json
import logging
import time
import uuid
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

import httpx
from fastapi import Cookie, FastAPI, Header, HTTPException, Query, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse, PlainTextResponse
from opentelemetry import trace
from pydantic import BaseModel

from gateway import __version__
from gateway.ab_testing import (
    OnboardingAbTestRequest,
    evaluate_mode_algorithm,
    resolve_ab_experiment_variant,
    run_onboarding_ab_test,
)
from gateway.admin_auth import AdminSessionManager, create_admin_auth_router
from gateway.admin_mcp import handle_admin_mcp_request
from gateway.auth import (
    DEV_PERSONAS,
    AuthError,
    CallerIdentity,
    DelegatedTokenExchanger,
    LocalDevKeyManager,
    OidcTokenValidator,
    PolicyEvaluator,
)
from gateway.catalog import (
    ToolCatalog,
    estimate_tokens,
    render_python_stub,
    to_mcp_tool_definition,
)
from gateway.comparator import audit_catalog_comparison, run_side_by_side_comparison
from gateway.config import (
    DiscoveryBackendMode,
    DiscoveryMode,
    ExecutionMode,
    GatewayMode,
    GatewaySettings,
    UpstreamConfig,
    UpstreamRegistry,
)
from gateway.demo_traffic import DemoTrafficRunner
from gateway.discovery import DiscoveryEngine
from gateway.dispatcher import (
    ExecutionBudget,
    HostDispatcher,
    SubcallRecord,
    bind_positional_arguments,
    build_provenance_envelope,
    summarize_value_shape,
)
from gateway.oauth_discovery import create_oauth_discovery_router
from gateway.observability import (
    MetricsRegistry,
    clear_request_context,
    configure_logging,
    log_event,
    set_request_context,
    update_request_context,
)
from gateway.runtime_stats import ProcessStatsCollector, collect_runtime_telemetry
from gateway.sandbox import LocalMontyBackend, RemoteMontyWorkerBackend, SandboxBackend
from gateway.state import (
    AuditStore,
    CacheBackend,
    InMemoryCacheBackend,
    PostgresAuditStore,
    RedisCacheBackend,
    SQLiteAuditStore,
)
from gateway.telemetry import TelemetryRecorder, configure_telemetry
from gateway.upstream_clients import (
    DiscoveredTool,
    UpstreamClientManager,
    build_agent_onboarding_prompt,
    inspect_upstream,
    validate_upstream_url,
)

_TRACER = trace.get_tracer("mcpcodeaway.server")

_VALID_MODES: set[str] = {
    "codemode",
    "codemode_upfront",
    "traditional",
    "traditional_discover",
    "hybrid",
    "auto",
}
_VALID_EXECUTION: set[str] = {"classic", "codemode", "hybrid", "auto"}
_VALID_DISCOVERY_MODES: set[str] = {"upfront", "deferred", "x_intent"}
_VALID_DISCOVERY: set[str] = {"keyword", "bm25", "embedding", "system1", "hybrid_system1"}

_MODE_TO_AXES: dict[GatewayMode, tuple[ExecutionMode, DiscoveryMode]] = {
    "traditional": ("classic", "upfront"),
    "traditional_discover": ("classic", "deferred"),
    "codemode_upfront": ("codemode", "upfront"),
    "codemode": ("codemode", "deferred"),
    "hybrid": ("hybrid", "deferred"),
    "auto": ("auto", "deferred"),
}


def _execute_code_with_stubs(
    tools: list[DiscoveredTool], *, header_note: str = "Authorized tool stubs:"
) -> dict[str, Any]:
    exec_copy = dict(EXECUTE_CODE_DEF)
    capped = tools[:100]
    stubs = "\n\n".join(render_python_stub(t) for t in capped)
    if not stubs:
        return exec_copy
    extra = (
        f"\n\n... ({len(tools) - 100} additional tools omitted; use search_tools)."
        if len(tools) > 100
        else ""
    )
    exec_copy["description"] = f"{EXECUTE_CODE_DEF['description']}\n\n{header_note}\n{stubs}{extra}"
    return exec_copy


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
        configure_logging(self.settings.log_format, self.settings.log_level)
        configure_telemetry(otlp_endpoint=self.settings.otlp_endpoint)
        self.metrics = MetricsRegistry()
        if cache is not None:
            self.cache: CacheBackend = cache
        elif self.settings.redis_url:
            self.cache = RedisCacheBackend(self.settings.redis_url)
        else:
            self.cache = InMemoryCacheBackend()
        if store is not None:
            self.store: AuditStore = store
        elif self.settings.postgres_dsn:
            self.store = PostgresAuditStore(self.settings.postgres_dsn)
        else:
            self.store = SQLiteAuditStore(self.settings.sqlite_path)
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
        self.client_manager = UpstreamClientManager(
            http_client=http_client,
            cache=self.cache,
            session_ttl_seconds=self.settings.mcp_session_ttl_seconds,
        )
        self.process_stats = ProcessStatsCollector()
        self.admin_sessions = AdminSessionManager(self.settings)
        self._local_catalog_epoch: str = "0"
        self.catalog = ToolCatalog()
        self.discovery_engine = DiscoveryEngine(self.settings, http_client=discovery_http_client)
        self.dispatcher = HostDispatcher(
            settings=self.settings,
            policy=self.policy,
            exchanger=self.exchanger,
            cache=self.cache,
            client_manager=self.client_manager,
            catalog=self.catalog,
            metrics=self.metrics,
        )
        if sandbox_backend is not None:
            self.sandbox: SandboxBackend = sandbox_backend
        elif self.settings.sandbox_backend == "remote":
            if not self.settings.sandbox_worker_url:
                raise ValueError(
                    "sandbox_worker_url must be configured when sandbox_backend='remote'"
                )
            if self.settings.app_env == "prod" and not self.settings.sandbox_worker_secret:
                raise ValueError(
                    "sandbox_worker_secret must be configured when sandbox_backend='remote' in prod"
                )
            self.sandbox = RemoteMontyWorkerBackend(
                self.settings.sandbox_worker_url,
                worker_secret=self.settings.sandbox_worker_secret,
            )
        else:
            self.sandbox = LocalMontyBackend()
        self.telemetry = TelemetryRecorder(self.store)
        self.preloaded_tools = preloaded_tools or {}
        self.startup_warnings: list[str] = []
        self._startup_retry_task: asyncio.Task[None] | None = None
        self.demo_traffic = DemoTrafficRunner(self)

    def _dev_discovery_identity(self) -> CallerIdentity | None:
        if self.dev_keys is None:
            return None
        tenant = (
            self.settings.allowed_tenants[0] if self.settings.allowed_tenants else "tenant-acme-dev"
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

    async def bump_catalog_epoch(self) -> str:
        """Broadcast catalog changes via shared cache so sibling replicas refresh."""
        epoch = uuid.uuid4().hex[:12]
        await self.cache.set("gateway:catalog_epoch", epoch, ttl_seconds=86400)
        self._local_catalog_epoch = epoch
        return epoch

    async def sync_catalog_if_stale(self) -> None:
        """Reload upstreams and tool catalog if a sibling replica bumped catalog_epoch."""
        remote_epoch = await self.cache.get("gateway:catalog_epoch")
        if remote_epoch is not None and remote_epoch != self._local_catalog_epoch:
            await self.refresh_catalog()
            self._local_catalog_epoch = remote_epoch

    async def resolve_dynamic_mode(
        self,
        identity: CallerIdentity,
        *,
        requested_exec: ExecutionMode,
        requested_disc: DiscoveryMode,
        requested_mode: GatewayMode,
        allowed_servers: set[str] | None,
        intent: str | None,
        request_id: str,
        explicit_override: bool,
    ) -> tuple[
        ExecutionMode,
        DiscoveryMode,
        GatewayMode,
        str | None,
        float | None,
        str | None,
        str | None,
    ]:
        """Resolve execution mode dynamically via A/B experiment split or algorithmic scorer."""
        accessible_srvs: list[UpstreamConfig] = []
        for upstream in self.registry.list_upstreams():
            if allowed_servers is not None and upstream.name not in allowed_servers:
                continue
            if self.policy.can_access_upstream(identity, upstream)[0]:
                accessible_srvs.append(upstream)

        if not explicit_override or requested_exec == "auto":
            for upstream in accessible_srvs:
                split = resolve_ab_experiment_variant(upstream, identity, request_id)
                if split is not None:
                    var_mode, exp_id, variant_lbl, reason = split
                    var_exec, var_disc = _MODE_TO_AXES[var_mode]
                    return var_exec, var_disc, var_mode, reason, None, exp_id, variant_lbl

        if requested_exec != "auto":
            return (
                requested_exec,
                requested_disc,
                requested_mode,
                f"explicit:{requested_mode}",
                None,
                None,
                None,
            )

        tools = self.catalog.list_accessible_tools(
            identity, self.policy, self.registry, allowed_servers=allowed_servers
        )
        tool_count = len(tools)
        classic_tokens = estimate_tokens([to_mcp_tool_definition(t) for t in tools]) if tools else 0
        mutating_ratio = (sum(1 for t in tools if t.mutating) / tool_count) if tool_count else 0.0
        typed_ratio = (
            sum(1 for t in tools if t.output_schema is not None) / tool_count if tool_count else 0.0
        )
        recent = await self.store.list_runs(limit=30, tenant_id=identity.tenant_id)
        avg_bytes = (
            sum(int(r.get("raw_upstream_bytes", 0)) for r in recent) / len(recent)
            if recent
            else 0.0
        )
        decision = evaluate_mode_algorithm(
            tool_count=tool_count,
            classic_schema_tokens=classic_tokens,
            avg_payload_bytes=avg_bytes,
            mutating_ratio=mutating_ratio,
            typed_output_ratio=typed_ratio,
            intent=intent,
            server_count=max(1, len({t.server for t in tools})),
        )
        return (
            decision.execution,
            decision.discovery_mode,
            decision.gateway_mode,
            decision.reason,
            decision.score,
            None,
            None,
        )

    async def ensure_catalog_for_identity(self, identity: CallerIdentity) -> None:
        """Lazily introspect any accessible upstreams not yet cached in catalog."""
        await self.sync_catalog_if_stale()
        for upstream in self.registry.list_upstreams():
            if upstream.name in self.catalog.digests_by_server:
                continue
            if not self.policy.can_access_upstream(identity, upstream)[0]:
                continue
            try:
                headers, _, _ = await self.dispatcher.resolve_upstream_headers(identity, upstream)
                tools = await self.client_manager.discover_tools(upstream, auth_headers=headers)
                self.catalog.set_server_tools(upstream, tools)
                self.startup_warnings = [
                    w for w in self.startup_warnings if f"'{upstream.name}'" not in w
                ]
            except Exception:
                continue

    async def _retry_failed_startup_upstreams(self) -> None:
        """Retry failed startup upstreams once HTTP listeners/peers finish binding."""
        for delay in (0.5, 1.5, 3.0):
            await asyncio.sleep(delay)
            if not self.startup_warnings:
                return
            await self.refresh_catalog()
            if not self.startup_warnings:
                return

    async def startup(self) -> None:
        await self.store.init()
        await self.refresh_catalog()
        if self.startup_warnings:
            self._startup_retry_task = asyncio.create_task(self._retry_failed_startup_upstreams())

    async def shutdown(self) -> None:
        await self.demo_traffic.stop()
        if self._startup_retry_task is not None and not self._startup_retry_task.done():
            self._startup_retry_task.cancel()
            await asyncio.gather(self._startup_retry_task, return_exceptions=True)
        self._startup_retry_task = None
        await self.sandbox.close()
        await self.client_manager.close()
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
        execution: ExecutionMode | None = None,
        discovery_mode: DiscoveryMode | None = None,
    ) -> list[dict[str, Any]]:
        """Build the baseline MCP tools/list payload for Execution x Discovery."""
        accessible = self.catalog.list_accessible_tools(
            identity, self.policy, self.registry, allowed_servers=allowed_servers
        )
        base_exec, base_disc = _MODE_TO_AXES[mode]
        eff_exec = execution or base_exec
        eff_disc = discovery_mode or base_disc

        if eff_exec == "classic":
            if eff_disc == "upfront":
                return [to_mcp_tool_definition(tool) for tool in accessible]
            return [LIST_SERVERS_DEF, SEARCH_TOOLS_DEF]

        if eff_exec == "codemode":
            if eff_disc == "upfront":
                return [_execute_code_with_stubs(accessible)]
            return [LIST_SERVERS_DEF, SEARCH_TOOLS_DEF, EXECUTE_CODE_DEF]

        # hybrid: Code Mode for orchestration + direct MCP definitions for mutating writes
        direct_mutating = [to_mcp_tool_definition(tool) for tool in accessible if tool.mutating]
        if eff_disc == "upfront":
            read_only = [tool for tool in accessible if not tool.mutating]
            return [_execute_code_with_stubs(read_only), *direct_mutating]
        return [LIST_SERVERS_DEF, SEARCH_TOOLS_DEF, EXECUTE_CODE_DEF, *direct_mutating]

    async def list_tools_for_request(
        self,
        identity: CallerIdentity,
        mode: GatewayMode,
        *,
        allowed_servers: set[str] | None = None,
        discovery_backend: DiscoveryBackendMode | None = None,
        execution: ExecutionMode | None = None,
        discovery_mode: DiscoveryMode | None = None,
        intent: str | None = None,
        query: str | None = None,
        requested_servers: list[str] | None = None,
        limit: int | None = None,
        cursor: str | None = None,
    ) -> dict[str, Any]:
        """Build tools/list response supporting X-Intent, upfront/deferred axes & pagination."""
        effective_ceiling = allowed_servers
        if requested_servers:
            req_set = set(requested_servers)
            effective_ceiling = (
                (effective_ceiling & req_set) if effective_ceiling is not None else req_set
            )

        base_exec, base_disc = _MODE_TO_AXES[mode]
        eff_exec = execution or base_exec
        eff_disc = discovery_mode or base_disc

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
                include_schemas=(eff_exec == "classic"),
            )
            if eff_exec == "classic":
                if mode == "traditional" and eff_disc == "upfront":
                    tools = list(discovered["hydrated_tools"])
                else:
                    tools = [LIST_SERVERS_DEF, SEARCH_TOOLS_DEF, *discovered["hydrated_tools"]]
            else:
                exec_copy = dict(EXECUTE_CODE_DEF)
                stubs = "\n\n".join(t["stub"] for t in discovered["tools"])
                if stubs:
                    exec_copy["description"] = (
                        f"{EXECUTE_CODE_DEF['description']}\n\n"
                        f"Pre-discovered matching stubs ({discovered['backend_used']}):\n{stubs}"
                    )
                if eff_exec == "codemode":
                    tools = (
                        [exec_copy]
                        if eff_disc == "upfront"
                        else [LIST_SERVERS_DEF, SEARCH_TOOLS_DEF, exec_copy]
                    )
                else:
                    accessible = self.catalog.list_accessible_tools(
                        identity, self.policy, self.registry, allowed_servers=effective_ceiling
                    )
                    direct_mutating = [to_mcp_tool_definition(t) for t in accessible if t.mutating]
                    tools = (
                        [exec_copy, *direct_mutating]
                        if eff_disc == "upfront"
                        else [LIST_SERVERS_DEF, SEARCH_TOOLS_DEF, exec_copy, *direct_mutating]
                    )
        else:
            tools = self.tools_for_mode(
                identity,
                mode,
                allowed_servers=effective_ceiling,
                execution=eff_exec,
                discovery_mode=eff_disc,
            )

        if cursor is not None or limit is not None:
            offset = max(0, int(cursor)) if cursor else 0
            page_size = max(1, min(limit or len(tools) or 1, 200))
            page = tools[offset : offset + page_size]
            out: dict[str, Any] = {"tools": page}
            if offset + page_size < len(tools):
                out["nextCursor"] = str(offset + page_size)
            return out
        return {"tools": tools}

    def _record_execution_metrics(
        self,
        *,
        identity: CallerIdentity,
        mode: str,
        ok: bool,
        duration_ms: float,
        run: dict[str, Any],
    ) -> int:
        total_saved = int(run["estimated_tokens_saved"]) + int(run["schema_tokens_saved"])
        metric_labels = {"tenant": identity.tenant_id, "mode": mode}
        self.metrics.inc_counter(
            "gateway_tokens_saved_total", amount=total_saved, labels=metric_labels
        )
        self.metrics.inc_counter(
            "gateway_payload_bytes_in_total",
            amount=run["raw_upstream_bytes"],
            labels=metric_labels,
        )
        self.metrics.inc_counter(
            "gateway_payload_bytes_out_total",
            amount=run["returned_bytes"],
            labels=metric_labels,
        )
        if run["turns_avoided"] > 0:
            self.metrics.inc_counter(
                "gateway_turns_avoided_total",
                amount=run["turns_avoided"],
                labels=metric_labels,
            )
        self.metrics.observe_histogram(
            "gateway_execution_duration_seconds",
            duration_ms / 1000.0,
            labels={"mode": mode, "ok": str(ok).lower()},
        )
        return total_saved

    async def execute_code_for_identity(
        self,
        identity: CallerIdentity,
        code: str,
        *,
        mode: GatewayMode = "codemode",
        execution: ExecutionMode | None = None,
        discovery_mode: DiscoveryMode | None = None,
        servers: list[str] | None = None,
        allowed_servers: set[str] | None = None,
        timeout_ms: int = 10000,
        require_grounding: bool = False,
        mcp_session_id: str | None = None,
        mode_decision_reason: str | None = None,
        mode_decision_score: float | None = None,
        experiment_id: str | None = None,
        ab_variant: str | None = None,
    ) -> dict[str, Any]:
        """Run Code Mode Python script in SandboxBackend and record audit telemetry."""
        started = time.perf_counter()
        self.process_stats.active_executions += 1
        self.process_stats.total_executions += 1
        try:
            return await self._execute_code_inner(
                identity,
                code,
                started=started,
                mode=mode,
                execution=execution,
                discovery_mode=discovery_mode,
                servers=servers,
                allowed_servers=allowed_servers,
                timeout_ms=timeout_ms,
                require_grounding=require_grounding,
                mcp_session_id=mcp_session_id,
                mode_decision_reason=mode_decision_reason,
                mode_decision_score=mode_decision_score,
                experiment_id=experiment_id,
                ab_variant=ab_variant,
            )
        finally:
            self.process_stats.active_executions = max(0, self.process_stats.active_executions - 1)

    async def _execute_code_inner(
        self,
        identity: CallerIdentity,
        code: str,
        *,
        started: float,
        mode: GatewayMode = "codemode",
        execution: ExecutionMode | None = None,
        discovery_mode: DiscoveryMode | None = None,
        servers: list[str] | None = None,
        allowed_servers: set[str] | None = None,
        timeout_ms: int = 10000,
        require_grounding: bool = False,
        mcp_session_id: str | None = None,
        mode_decision_reason: str | None = None,
        mode_decision_score: float | None = None,
        experiment_id: str | None = None,
        ab_variant: str | None = None,
    ) -> dict[str, Any]:
        await self.ensure_catalog_for_identity(identity)
        if allowed_servers is not None and servers is not None:
            effective_servers: set[str] | None = allowed_servers & set(servers)
        elif allowed_servers is not None:
            effective_servers = allowed_servers
        elif servers is not None:
            effective_servers = set(servers)
        else:
            effective_servers = None

        eff_exec = execution or _MODE_TO_AXES[mode][0]
        accessible = self.catalog.list_accessible_tools(
            identity, self.policy, self.registry, allowed_servers=effective_servers
        )
        schema_tokens = estimate_tokens(
            self.tools_for_mode(
                identity,
                mode,
                allowed_servers=allowed_servers,
                execution=eff_exec,
                discovery_mode=discovery_mode,
            )
        )
        classic_schema_tokens = estimate_tokens(
            self.tools_for_mode(identity, "traditional", allowed_servers=allowed_servers)
        )

        budget = ExecutionBudget(self.settings.max_calls_per_execution)
        subcalls: list[SubcallRecord] = []
        observed_tool_outputs: list[Any] = []
        observed_tool_shapes: dict[str, Any] = {}
        external_functions: dict[str, Callable[..., Any]] = {}

        for tool in accessible:
            if eff_exec == "hybrid" and tool.mutating:
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
                        mcp_session_id=mcp_session_id,
                    )
                    subcalls.append(rec)
                    if isinstance(res, dict) and "_dispatch_error" in res:
                        raise RuntimeError(res["_dispatch_error"])
                    observed_tool_outputs.append(res)
                    observed_tool_shapes[t.qualified_name] = summarize_value_shape(res)
                    return res

                return _wrapper

            external_functions[tool.qualified_name] = _make_wrapper(tool, upstream)

        timeout_seconds = min(timeout_ms / 1000.0, self.settings.monty_timeout_seconds)
        with _TRACER.start_as_current_span("gateway.sandbox.execute") as span:
            span.set_attribute("mcp.mode", mode)
            span.set_attribute("mcp.accessible_tools", len(external_functions))
            sandbox_res = await self.sandbox.execute(
                code,
                external_functions=external_functions,
                timeout_seconds=timeout_seconds,
            )
            span.set_attribute("sandbox.ok", sandbox_res.ok)
            span.set_attribute("sandbox.subcalls", len(subcalls))
        duration_ms = round((time.perf_counter() - started) * 1000, 3)

        provenance = build_provenance_envelope(
            allowed_servers=effective_servers,
            subcalls=subcalls,
            observed_tool_outputs=observed_tool_outputs,
            returned_value=sandbox_res.result if sandbox_res.ok else None,
            signing_secret=self.settings.provenance_signing_secret,
        )
        effective_ok = sandbox_res.ok
        effective_result = sandbox_res.result
        effective_error = sandbox_res.error
        effective_error_type = sandbox_res.error_type
        grounding_blocked = False
        if (self.settings.require_tool_grounding or require_grounding) and effective_ok:
            if provenance["subcall_count"] == 0 and effective_result is not None:
                effective_ok = False
                effective_result = None
                effective_error_type = "UngroundedResultError"
                effective_error = (
                    "Policy require_tool_grounding blocked result returned "
                    "with 0 allowed tool calls."
                )
                grounding_blocked = True
            elif provenance["unverified_identifiers"]:
                effective_ok = False
                effective_result = None
                effective_error_type = "UnverifiedEntityError"
                effective_error = (
                    "Policy require_tool_grounding blocked unverified identifiers not "
                    f"returned by allowed tools: {provenance['unverified_identifiers']}"
                )
                grounding_blocked = True

        if grounding_blocked:
            self.metrics.inc_counter(
                "gateway_grounding_rejections_total",
                labels={"reason": str(effective_error_type)},
            )
            log_event(
                "grounding.blocked",
                level=logging.WARNING,
                reason=effective_error_type,
                mode=mode,
            )

        payload: dict[str, Any] = {
            "ok": effective_ok,
            "result": effective_result,
            "stdout": sandbox_res.stdout,
            "truncated": sandbox_res.truncated,
            "subcall_count": len(subcalls),
            "duration_ms": duration_ms,
            "_provenance": provenance,
        }
        if not effective_ok:
            if observed_tool_shapes and any(
                k in f"{effective_error_type or ''} {effective_error or ''}"
                for k in ("KeyError", "TypeError", "AttributeError")
            ):
                payload["observed_output_shapes"] = observed_tool_shapes
                hint = json.dumps(observed_tool_shapes)
                effective_error = (
                    f"{effective_error or 'Execution error'}\n"
                    f"Observed return shapes from tools called in this run: {hint}"
                )
            payload["error"] = effective_error
            payload["error_type"] = effective_error_type

        run = await self.telemetry.record_execution(
            mode=mode,
            identity=identity,
            catalog_version=self.catalog.catalog_version,
            schema_tokens=schema_tokens,
            subcalls=subcalls,
            returned_payload=payload,
            duration_ms=duration_ms,
            ok=effective_ok,
            code=code,
            stdout=sandbox_res.stdout,
            error=effective_error,
            tool_name="execute_code",
            provenance=provenance,
            classic_schema_tokens=classic_schema_tokens,
            grounding_blocked=grounding_blocked,
            mode_decision_reason=mode_decision_reason,
            mode_decision_score=mode_decision_score,
            experiment_id=experiment_id,
            ab_variant=ab_variant,
        )
        total_saved = self._record_execution_metrics(
            identity=identity,
            mode=mode,
            ok=effective_ok,
            duration_ms=duration_ms,
            run=run,
        )
        log_event(
            "sandbox.executed",
            mode=mode,
            ok=effective_ok,
            subcalls=len(subcalls),
            raw_bytes=run["raw_upstream_bytes"],
            returned_bytes=run["returned_bytes"],
            tokens_saved=total_saved,
            turns_avoided=run["turns_avoided"],
            duration_ms=duration_ms,
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
        mcp_session_id: str | None = None,
        mode_decision_reason: str | None = None,
        mode_decision_score: float | None = None,
        experiment_id: str | None = None,
        ab_variant: str | None = None,
    ) -> dict[str, Any]:
        """Invoke a single upstream tool directly (traditional / traditional_discover / hybrid)."""
        started = time.perf_counter()
        self.process_stats.active_executions += 1
        self.process_stats.total_executions += 1
        try:
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
            classic_schema_tokens = estimate_tokens(
                self.tools_for_mode(identity, "traditional", allowed_servers=allowed_servers)
            )
            result, rec = await self.dispatcher.invoke_tool(
                identity,
                tool,
                upstream,
                arguments,
                code_mode_hints=False,
                mcp_session_id=mcp_session_id,
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
                classic_schema_tokens=classic_schema_tokens,
                mode_decision_reason=mode_decision_reason,
                mode_decision_score=mode_decision_score,
                experiment_id=experiment_id,
                ab_variant=ab_variant,
            )
            self._record_execution_metrics(
                identity=identity,
                mode=mode,
                ok=ok,
                duration_ms=duration_ms,
                run=run,
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
        finally:
            self.process_stats.active_executions = max(0, self.process_stats.active_executions - 1)


def _axis_to_gateway_mode(execution: ExecutionMode, discovery_mode: DiscoveryMode) -> GatewayMode:
    if execution == "auto":
        return "auto"
    if execution == "classic":
        return "traditional" if discovery_mode == "upfront" else "traditional_discover"
    if execution == "codemode":
        return "codemode_upfront" if discovery_mode == "upfront" else "codemode"
    return "hybrid"


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


def _resolve_execution_and_discovery(
    settings: GatewaySettings,
    *,
    query_mode: str | None,
    header_mode: str | None,
    query_execution: str | None,
    header_execution: str | None,
    query_discovery_mode: str | None,
    header_discovery_mode: str | None,
    x_intent: str | None,
) -> tuple[ExecutionMode, DiscoveryMode, GatewayMode]:
    has_mode_override = bool(query_mode or header_mode)
    base_mode = _resolve_mode(settings.gateway_mode, query_mode, header_mode)
    base_exec, base_disc = _MODE_TO_AXES[base_mode]

    raw_exec = (
        query_execution
        or header_execution
        or (settings.execution_mode if not has_mode_override else None)
    )
    if raw_exec is not None and raw_exec not in _VALID_EXECUTION:
        raise HTTPException(
            status_code=400,
            detail=(
                f"Invalid execution mode '{raw_exec}'. Expected one of: {sorted(_VALID_EXECUTION)}"
            ),
        )
    raw_disc = (
        query_discovery_mode
        or header_discovery_mode
        or (settings.discovery_mode if not has_mode_override else None)
    )
    if raw_disc is not None and raw_disc not in _VALID_DISCOVERY_MODES:
        raise HTTPException(
            status_code=400,
            detail=(
                f"Invalid discovery mode '{raw_disc}'. "
                f"Expected one of: {sorted(_VALID_DISCOVERY_MODES)}"
            ),
        )

    eff_exec: ExecutionMode = raw_exec or base_exec  # type: ignore[assignment]
    eff_disc: DiscoveryMode = raw_disc or base_disc  # type: ignore[assignment]
    if (
        x_intent
        and x_intent.strip()
        and raw_disc != "upfront"
        and not (base_mode == "traditional" and raw_disc is None)
    ):
        eff_disc = "x_intent"
    eff_mode = (
        base_mode
        if (raw_exec is None and raw_disc is None)
        else _axis_to_gateway_mode(eff_exec, eff_disc)
    )
    return eff_exec, eff_disc, eff_mode


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
                f"Invalid discovery backend '{chosen}'. Expected one of: {sorted(_VALID_DISCOVERY)}"
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
    runtime.demo_traffic.bind_app(app)
    if runtime.settings.cors_allowed_origins:
        app.add_middleware(
            CORSMiddleware,
            allow_origins=runtime.settings.cors_allowed_origins,
            allow_credentials=True,
            allow_methods=["*"],
            allow_headers=["*"],
        )
    app.include_router(
        create_oauth_discovery_router(runtime.settings, runtime.dev_keys, runtime.validator)
    )
    features = runtime.settings.active_features
    if features.dashboard_ui or features.admin_read or features.audit_api:
        app.include_router(create_admin_auth_router(runtime))

    @app.get("/health")
    async def health() -> dict[str, Any]:
        return {
            "status": "ok",
            "version": __version__,
            "profile": runtime.settings.effective_profile_name,
            "features": runtime.settings.active_features.model_dump(),
            "app_env": runtime.settings.app_env,
            "auth_mode": runtime.settings.auth_mode,
            "default_mode": runtime.settings.gateway_mode,
            "catalog_version": runtime.catalog.catalog_version,
            "upstreams": len(runtime.registry.list_upstreams()),
            "drift_warnings": runtime.catalog.drift_warnings,
            "startup_warnings": runtime.startup_warnings,
        }

    @app.get("/ready")
    async def ready() -> dict[str, Any]:
        return {
            "ready": True,
            "profile": runtime.settings.effective_profile_name,
            "upstreams": len(runtime.registry.list_upstreams()),
        }

    @app.post("/mcp")
    async def mcp_endpoint(
        request: Request,
        mode: str | None = Query(default=None),
        execution: str | None = Query(default=None),
        discovery_mode: str | None = Query(default=None),
        discovery: str | None = Query(default=None),
        servers: str | None = Query(default=None),
        require_grounding: bool | None = Query(default=None),
        x_gateway_mode: str | None = Header(default=None),
        x_execution_mode: str | None = Header(default=None),
        x_discovery_mode: str | None = Header(default=None),
        x_discovery_backend: str | None = Header(default=None),
        x_mcp_servers: str | None = Header(default=None),
        x_intent: str | None = Header(default=None),
        x_require_grounding: str | None = Header(default=None),
        x_request_id: str | None = Header(default=None),
        mcp_session_id: str | None = Header(default=None),
        authorization: str | None = Header(default=None),
    ) -> JSONResponse:
        req_corr_id = x_request_id or f"req_{uuid.uuid4().hex[:12]}"
        req_started = time.perf_counter()
        set_request_context(
            request_id=req_corr_id,
            profile=runtime.settings.effective_profile_name,
        )
        with _TRACER.start_as_current_span("gateway.mcp.request") as mcp_span:
            mcp_span.set_attribute("gateway.profile", runtime.settings.effective_profile_name)
            try:
                try:
                    identity = await runtime.validator.validate_authorization_header(authorization)
                except AuthError as exc:
                    runtime.metrics.inc_counter(
                        "gateway_auth_denials_total",
                        labels={"surface": "mcp", "status_code": exc.status_code},
                    )
                    log_event(
                        "auth.denied",
                        level=logging.WARNING,
                        surface="mcp",
                        status_code=exc.status_code,
                        reason=exc.detail,
                    )
                    headers = (
                        {"WWW-Authenticate": exc.www_authenticate} if exc.www_authenticate else None
                    )
                    raise HTTPException(
                        status_code=exc.status_code, detail=exc.detail, headers=headers
                    ) from exc

                update_request_context(tenant_id=identity.tenant_id, user_sub=identity.sub)
                mcp_span.set_attribute("tenant.id", identity.tenant_id)

                eff_exec, eff_disc_mode, effective_mode = _resolve_execution_and_discovery(
                    runtime.settings,
                    query_mode=mode,
                    header_mode=x_gateway_mode,
                    query_execution=execution,
                    header_execution=x_execution_mode,
                    query_discovery_mode=discovery_mode,
                    header_discovery_mode=x_discovery_mode,
                    x_intent=x_intent,
                )
                has_explicit_mode = bool(mode or x_gateway_mode or execution or x_execution_mode)
                mode_decision_reason: str | None = f"explicit:{effective_mode}"
                mode_decision_score: float | None = None
                experiment_id: str | None = None
                ab_variant: str | None = None

                effective_discovery = _resolve_discovery(
                    runtime.settings.discovery_backend, discovery, x_discovery_backend
                )
                allowed_servers = _parse_servers_ceiling(servers, x_mcp_servers)
                enforce_grounding = bool(
                    require_grounding
                    or (
                        x_require_grounding
                        and x_require_grounding.strip().lower() in {"1", "true", "yes"}
                    )
                )
                payload = await request.json()
                req_id = payload.get("id")
                method = payload.get("method")
                params = payload.get("params") or {}
                mcp_span.set_attribute("mcp.method", str(method))
                mcp_span.set_attribute("mcp.mode", effective_mode)

                def _finish(resp: JSONResponse, status_label: str = "ok") -> JSONResponse:
                    dur_ms = round((time.perf_counter() - req_started) * 1000, 3)
                    runtime.metrics.inc_counter(
                        "gateway_mcp_requests_total",
                        labels={
                            "method": str(method),
                            "mode": effective_mode,
                            "status": status_label,
                        },
                    )
                    log_event(
                        "mcp.request.completed",
                        method=str(method),
                        mode=effective_mode,
                        discovery=effective_discovery,
                        mode_decision_reason=mode_decision_reason,
                        experiment_id=experiment_id,
                        ab_variant=ab_variant,
                        status=status_label,
                        duration_ms=dur_ms,
                    )
                    resp.headers["X-Request-Id"] = req_corr_id
                    if mode_decision_reason:
                        resp.headers["X-Gateway-Mode-Decision"] = mode_decision_reason
                    return resp

                if method == "initialize":
                    return _finish(
                        _jsonrpc_ok(
                            req_id,
                            {
                                "protocolVersion": "2025-03-26",
                                "capabilities": {"tools": {"listChanged": True}},
                                "serverInfo": {"name": "mcpcodeaway", "version": __version__},
                            },
                        )
                    )
                if method == "server/discover":
                    return _finish(
                        _jsonrpc_ok(
                            req_id,
                            {
                                "supportedVersions": ["2025-03-26"],
                                "capabilities": {"tools": {"listChanged": True}},
                                "ttlMs": 30000,
                                "cacheScope": "private",
                                "instructions": (
                                    "Stateless multi-MCP gateway. Call list_servers then "
                                    "search_tools (or pass X-Intent / ?servers=...) before "
                                    "calling execute_code or hydrated tools."
                                ),
                            },
                        )
                    )
                if method == "notifications/initialized":
                    return _finish(JSONResponse({}, status_code=202))

                await runtime.ensure_catalog_for_identity(identity)
                (
                    eff_exec,
                    eff_disc_mode,
                    effective_mode,
                    mode_decision_reason,
                    mode_decision_score,
                    experiment_id,
                    ab_variant,
                ) = await runtime.resolve_dynamic_mode(
                    identity,
                    requested_exec=eff_exec,
                    requested_disc=eff_disc_mode,
                    requested_mode=effective_mode,
                    allowed_servers=allowed_servers,
                    intent=x_intent,
                    request_id=req_corr_id,
                    explicit_override=has_explicit_mode,
                )
                mcp_span.set_attribute("mcp.mode", effective_mode)
                if mode_decision_reason:
                    mcp_span.set_attribute("mcp.mode_decision_reason", mode_decision_reason)
                if experiment_id:
                    mcp_span.set_attribute("mcp.ab_experiment", experiment_id)

                if method == "tools/list":
                    req_srvs = params.get("servers")
                    if not req_srvs and params.get("server"):
                        req_srvs = [str(params["server"])]
                    listed = await runtime.list_tools_for_request(
                        identity,
                        effective_mode,
                        allowed_servers=allowed_servers,
                        discovery_backend=effective_discovery,
                        execution=eff_exec,
                        discovery_mode=eff_disc_mode,
                        intent=x_intent,
                        query=params.get("query"),
                        requested_servers=req_srvs,
                        limit=params.get("limit"),
                        cursor=params.get("cursor"),
                    )
                    return _finish(_jsonrpc_ok(req_id, listed))

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
                        return _finish(
                            _jsonrpc_ok(
                                req_id,
                                {
                                    "content": [{"type": "text", "text": json.dumps(srv_payload)}],
                                    "structuredContent": srv_payload,
                                },
                            )
                        )

                    if tool_name == "search_tools":
                        if eff_exec == "classic" and eff_disc_mode == "upfront":
                            return _finish(
                                _jsonrpc_err(
                                    req_id,
                                    -32601,
                                    "search_tools is disabled in traditional upfront mode",
                                ),
                                status_label="error",
                            )
                        req_srvs = arguments.get("servers")
                        if not req_srvs and arguments.get("server"):
                            req_srvs = [str(arguments["server"])]
                        with _TRACER.start_as_current_span("gateway.discovery.search") as dspan:
                            dspan.set_attribute("discovery.backend", effective_discovery)
                            dspan.set_attribute("discovery.query", str(arguments.get("query", "")))
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
                                include_schemas=(eff_exec == "classic"),
                            )
                        return _finish(
                            _jsonrpc_ok(
                                req_id,
                                {
                                    "content": [{"type": "text", "text": json.dumps(res)}],
                                    "structuredContent": res,
                                },
                            )
                        )

                    if tool_name == "execute_code":
                        if eff_exec == "classic":
                            return _finish(
                                _jsonrpc_err(
                                    req_id,
                                    -32601,
                                    f"execute_code is disabled in {effective_mode} mode",
                                ),
                                status_label="error",
                            )
                        code = str(arguments.get("code") or "")
                        if not code.strip():
                            return _finish(
                                _jsonrpc_err(
                                    req_id, -32602, "execute_code requires non-empty 'code'"
                                ),
                                status_label="error",
                            )
                        res = await runtime.execute_code_for_identity(
                            identity,
                            code,
                            mode=effective_mode,
                            execution=eff_exec,
                            discovery_mode=eff_disc_mode,
                            servers=arguments.get("servers"),
                            allowed_servers=allowed_servers,
                            timeout_ms=int(arguments.get("timeout_ms", 10000)),
                            require_grounding=enforce_grounding,
                            mcp_session_id=mcp_session_id,
                            mode_decision_reason=mode_decision_reason,
                            mode_decision_score=mode_decision_score,
                            experiment_id=experiment_id,
                            ab_variant=ab_variant,
                        )
                        return _finish(
                            _jsonrpc_ok(
                                req_id,
                                {
                                    "content": [
                                        {"type": "text", "text": json.dumps(res, default=str)}
                                    ],
                                    "structuredContent": res,
                                    "isError": not res["ok"],
                                },
                            ),
                            status_label="ok" if res["ok"] else "error",
                        )

                    if eff_exec == "codemode":
                        return _finish(
                            _jsonrpc_err(
                                req_id,
                                -32601,
                                f"Direct tool '{tool_name}' is not exposed in codemode; "
                                "invoke authorized tools inside execute_code.",
                            ),
                            status_label="error",
                        )

                    try:
                        res = await runtime.call_direct_tool_for_identity(
                            identity,
                            tool_name,
                            arguments,
                            mode=effective_mode,
                            allowed_servers=allowed_servers,
                            mcp_session_id=mcp_session_id,
                            mode_decision_reason=mode_decision_reason,
                            mode_decision_score=mode_decision_score,
                            experiment_id=experiment_id,
                            ab_variant=ab_variant,
                        )
                    except KeyError as exc:
                        return _finish(_jsonrpc_err(req_id, -32601, str(exc)), status_label="error")
                    except PermissionError as exc:
                        return _finish(_jsonrpc_err(req_id, -32003, str(exc)), status_label="error")
                    except (ValueError, RuntimeError) as exc:
                        return _finish(_jsonrpc_err(req_id, -32602, str(exc)), status_label="error")

                    return _finish(
                        _jsonrpc_ok(
                            req_id,
                            {
                                "content": [{"type": "text", "text": json.dumps(res, default=str)}],
                                "structuredContent": res,
                                "isError": not res["ok"],
                            },
                        ),
                        status_label="ok" if res["ok"] else "error",
                    )

                return _finish(
                    _jsonrpc_err(req_id, -32601, f"Unsupported MCP method '{method}'"),
                    status_label="error",
                )
            finally:
                clear_request_context()

    async def _authorize_control_plane(
        authorization: str | None,
        admin_cookie: str | None = None,
        *,
        require_admin_role: bool = True,
    ) -> CallerIdentity | None:
        if authorization:
            try:
                identity = await runtime.validator.validate_authorization_header(authorization)
            except AuthError as exc:
                runtime.metrics.inc_counter(
                    "gateway_auth_denials_total",
                    labels={"surface": "control_plane", "status_code": exc.status_code},
                )
                log_event(
                    "auth.denied",
                    level=logging.WARNING,
                    surface="control_plane",
                    status_code=exc.status_code,
                    reason=exc.detail,
                )
                headers = (
                    {"WWW-Authenticate": exc.www_authenticate} if exc.www_authenticate else None
                )
                raise HTTPException(
                    status_code=exc.status_code, detail=exc.detail, headers=headers
                ) from exc
            if require_admin_role and not (set(identity.roles) & set(runtime.settings.admin_roles)):
                runtime.metrics.inc_counter(
                    "gateway_auth_denials_total",
                    labels={"surface": "control_plane", "status_code": 403},
                )
                log_event(
                    "auth.denied",
                    level=logging.WARNING,
                    surface="control_plane",
                    status_code=403,
                    reason="missing_admin_role",
                )
                raise HTTPException(
                    status_code=403,
                    detail="Admin role required for control-plane access.",
                )
            return identity
        if admin_cookie:
            cookie_id = runtime.admin_sessions.verify_session_token(admin_cookie)
            if cookie_id is not None:
                if require_admin_role and not (
                    set(cookie_id.roles) & set(runtime.settings.admin_roles)
                ):
                    raise HTTPException(
                        status_code=403,
                        detail="Admin role required for control-plane access.",
                    )
                return cookie_id
        if runtime.settings.admin_auth_required:
            runtime.metrics.inc_counter(
                "gateway_auth_denials_total",
                labels={"surface": "control_plane", "status_code": 401},
            )
            log_event(
                "auth.denied",
                level=logging.WARNING,
                surface="control_plane",
                status_code=401,
                reason="missing_bearer_token",
            )
            raise HTTPException(
                status_code=401,
                detail="Authorization Bearer token required for control-plane access.",
                headers={"WWW-Authenticate": "Bearer"},
            )
        return None

    def _resolve_audit_tenant_scope(
        identity: CallerIdentity | None,
        requested_tenant: str | None,
    ) -> str | None:
        if identity is None:
            return requested_tenant
        if set(identity.roles) & set(runtime.settings.admin_roles):
            return requested_tenant
        if requested_tenant is not None and requested_tenant != identity.tenant_id:
            runtime.metrics.inc_counter(
                "gateway_auth_denials_total",
                labels={"surface": "audit", "status_code": 403},
            )
            log_event(
                "auth.denied",
                level=logging.WARNING,
                surface="audit",
                status_code=403,
                reason="cross_tenant_denied",
            )
            raise HTTPException(status_code=403, detail="Cross-tenant audit access denied.")
        return identity.tenant_id

    if features.metrics:

        @app.get("/metrics", response_class=PlainTextResponse)
        async def prometheus_metrics(
            authorization: str | None = Header(default=None),
            mcpcodeaway_admin_session: str | None = Cookie(default=None),
        ) -> PlainTextResponse:
            if runtime.settings.metrics_require_auth:
                await _authorize_control_plane(
                    authorization, mcpcodeaway_admin_session, require_admin_role=False
                )
            return PlainTextResponse(
                runtime.metrics.render_prometheus(),
                media_type="text/plain; version=0.0.4; charset=utf-8",
            )

    if features.audit_api:

        @app.get("/audit/runs")
        async def audit_runs(
            limit: int = Query(default=50, ge=1, le=200),
            tenant_id: str | None = Query(default=None),
            authorization: str | None = Header(default=None),
            mcpcodeaway_admin_session: str | None = Cookie(default=None),
        ) -> dict[str, Any]:
            identity = await _authorize_control_plane(
                authorization, mcpcodeaway_admin_session, require_admin_role=False
            )
            effective_tenant = _resolve_audit_tenant_scope(identity, tenant_id)
            runs = await runtime.store.list_runs(limit=limit, tenant_id=effective_tenant)
            return {"count": len(runs), "runs": runs}

        @app.get("/audit/runs/{run_id}")
        async def audit_run_detail(
            run_id: str,
            authorization: str | None = Header(default=None),
            mcpcodeaway_admin_session: str | None = Cookie(default=None),
        ) -> dict[str, Any]:
            identity = await _authorize_control_plane(
                authorization, mcpcodeaway_admin_session, require_admin_role=False
            )
            run = await runtime.store.get_run(run_id)
            if run is None:
                raise HTTPException(status_code=404, detail=f"Run '{run_id}' not found")
            if (
                identity is not None
                and not (set(identity.roles) & set(runtime.settings.admin_roles))
                and run.get("tenant_id") != identity.tenant_id
            ):
                runtime.metrics.inc_counter(
                    "gateway_auth_denials_total",
                    labels={"surface": "audit", "status_code": 403},
                )
                raise HTTPException(status_code=403, detail="Cross-tenant audit run access denied.")
            return run

        @app.get("/audit/summary")
        async def audit_summary(
            tenant_id: str | None = Query(default=None),
            authorization: str | None = Header(default=None),
            mcpcodeaway_admin_session: str | None = Cookie(default=None),
        ) -> dict[str, Any]:
            identity = await _authorize_control_plane(
                authorization, mcpcodeaway_admin_session, require_admin_role=False
            )
            effective_tenant = _resolve_audit_tenant_scope(identity, tenant_id)
            return await runtime.telemetry.summary(tenant_id=effective_tenant)

        @app.get("/audit/value")
        async def audit_value(
            limit: int = Query(default=500, ge=1, le=2000),
            tenant_id: str | None = Query(default=None),
            authorization: str | None = Header(default=None),
            mcpcodeaway_admin_session: str | None = Cookie(default=None),
        ) -> dict[str, Any]:
            identity = await _authorize_control_plane(
                authorization, mcpcodeaway_admin_session, require_admin_role=False
            )
            effective_tenant = _resolve_audit_tenant_scope(identity, tenant_id)
            return await runtime.telemetry.value_report(
                limit=limit,
                tenant_id=effective_tenant,
                cost_per_1m_input_tokens_usd=runtime.settings.cost_per_1m_input_tokens_usd,
                context_overflow_threshold_tokens=(
                    runtime.settings.context_overflow_threshold_tokens
                ),
            )

        @app.get("/audit/analytics")
        async def audit_analytics(
            limit: int = Query(default=1000, ge=1, le=5000),
            tenant_id: str | None = Query(default=None),
            group: str | None = Query(default=None),
            user: str | None = Query(default=None),
            upstream: str | None = Query(default=None),
            model: str = Query(default="default"),
            authorization: str | None = Header(default=None),
            mcpcodeaway_admin_session: str | None = Cookie(default=None),
        ) -> dict[str, Any]:
            identity = await _authorize_control_plane(
                authorization, mcpcodeaway_admin_session, require_admin_role=False
            )
            effective_tenant = _resolve_audit_tenant_scope(identity, tenant_id)
            return await runtime.telemetry.analytics_report(
                limit=limit,
                tenant_id=effective_tenant,
                group_filter=group,
                user_filter=user,
                upstream_filter=upstream,
                model_name=model,
                model_pricing=runtime.settings.model_pricing,
            )

    if features.admin_read:

        @app.get("/admin/upstreams")
        async def list_admin_upstreams(
            authorization: str | None = Header(default=None),
            mcpcodeaway_admin_session: str | None = Cookie(default=None),
        ) -> dict[str, Any]:
            await _authorize_control_plane(
                authorization, mcpcodeaway_admin_session, require_admin_role=True
            )
            await runtime.sync_catalog_if_stale()
            upstreams = runtime.registry.list_upstreams(include_disabled=True)
            return {
                "app_env": runtime.settings.app_env,
                "profile": runtime.settings.effective_profile_name,
                "dynamic_upstreams_permitted": runtime.settings.dynamic_upstreams_permitted,
                "catalog_version": runtime.catalog.catalog_version,
                "digests_by_server": runtime.catalog.digests_by_server,
                "drift_warnings": runtime.catalog.drift_warnings,
                "upstreams": [u.model_dump(exclude={"openapi_spec"}) for u in upstreams],
                "comparison_audit": audit_catalog_comparison(runtime.catalog, runtime.registry),
            }

        @app.get("/admin/telemetry/runtime")
        async def admin_runtime_telemetry(
            authorization: str | None = Header(default=None),
            mcpcodeaway_admin_session: str | None = Cookie(default=None),
        ) -> dict[str, Any]:
            await _authorize_control_plane(
                authorization, mcpcodeaway_admin_session, require_admin_role=True
            )
            return await collect_runtime_telemetry(runtime)

        @app.get("/admin/ab/experiments")
        async def admin_ab_experiments(
            authorization: str | None = Header(default=None),
            mcpcodeaway_admin_session: str | None = Cookie(default=None),
        ) -> dict[str, Any]:
            await _authorize_control_plane(
                authorization, mcpcodeaway_admin_session, require_admin_role=True
            )
            analytics = await runtime.telemetry.analytics_report(
                limit=1000,
                model_pricing=runtime.settings.model_pricing,
            )
            configured = [
                {
                    "upstream": u.name,
                    "preferred_mode": u.preferred_mode,
                    "ab_experiment": u.ab_experiment.model_dump() if u.ab_experiment else None,
                }
                for u in runtime.registry.list_upstreams(include_disabled=True)
            ]
            return {
                "configured": configured,
                "results": analytics["ab_experiments"],
                "mode_decisions": analytics["mode_decisions"],
            }

    async def _inspect_candidate(
        cfg: UpstreamConfig, authorization: str | None
    ) -> tuple[dict[str, Any], dict[str, Any]]:
        if runtime.settings.ssrf_protection_enabled:
            try:
                for candidate_url in (cfg.url, cfg.openapi_spec_url):
                    validate_upstream_url(
                        candidate_url,
                        app_env=runtime.settings.app_env,
                        block_private_ips_in_prod=runtime.settings.block_private_ips_in_prod,
                        allowed_hosts=runtime.settings.allowed_upstream_hosts,
                    )
            except ValueError as exc:
                raise HTTPException(status_code=400, detail=str(exc)) from exc
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

    if features.admin_write:

        @app.get("/admin/upstreams/agent-prompt")
        async def admin_agent_onboarding_prompt(
            authorization: str | None = Header(default=None),
            mcpcodeaway_admin_session: str | None = Cookie(default=None),
        ) -> dict[str, Any]:
            await _authorize_control_plane(
                authorization, mcpcodeaway_admin_session, require_admin_role=True
            )
            return {
                "prompt": build_agent_onboarding_prompt(),
                "schema": UpstreamConfig.model_json_schema(),
            }

        @app.post("/admin/catalog/refresh")
        async def admin_refresh_catalog(
            authorization: str | None = Header(default=None),
            mcpcodeaway_admin_session: str | None = Cookie(default=None),
        ) -> dict[str, Any]:
            await _authorize_control_plane(
                authorization, mcpcodeaway_admin_session, require_admin_role=True
            )
            warnings = await runtime.refresh_catalog()
            epoch = await runtime.bump_catalog_epoch()
            return {
                "refreshed": True,
                "catalog_version": runtime.catalog.catalog_version,
                "catalog_epoch": epoch,
                "warnings": warnings,
            }

        @app.post("/admin/audit/prune")
        async def admin_prune_audit_runs(
            retention_days: int | None = Query(default=None, ge=0, le=3650),
            authorization: str | None = Header(default=None),
            mcpcodeaway_admin_session: str | None = Cookie(default=None),
        ) -> dict[str, Any]:
            await _authorize_control_plane(
                authorization, mcpcodeaway_admin_session, require_admin_role=True
            )
            days = (
                retention_days
                if retention_days is not None
                else runtime.settings.audit_retention_days
            )
            deleted = await runtime.store.prune_runs_older_than(days)
            return {"deleted_runs": deleted, "retention_days": days}

        def _ensure_demo_traffic_allowed() -> None:
            if (
                runtime.settings.app_env == "prod"
                or runtime.settings.effective_profile_name == "production"
            ):
                raise HTTPException(
                    status_code=403, detail="Demo traffic simulator is disabled in production"
                )

        @app.get("/admin/demo/traffic/status")
        async def admin_demo_traffic_status(
            authorization: str | None = Header(default=None),
            mcpcodeaway_admin_session: str | None = Cookie(default=None),
        ) -> dict[str, Any]:
            await _authorize_control_plane(
                authorization, mcpcodeaway_admin_session, require_admin_role=True
            )
            return runtime.demo_traffic.status()

        @app.post("/admin/demo/traffic/start")
        async def admin_demo_traffic_start(
            interval_seconds: float = Query(default=2.0, ge=0.5, le=10.0),
            authorization: str | None = Header(default=None),
            mcpcodeaway_admin_session: str | None = Cookie(default=None),
        ) -> dict[str, Any]:
            await _authorize_control_plane(
                authorization, mcpcodeaway_admin_session, require_admin_role=True
            )
            _ensure_demo_traffic_allowed()
            return await runtime.demo_traffic.start(interval_seconds=interval_seconds)

        @app.post("/admin/demo/traffic/stop")
        async def admin_demo_traffic_stop(
            authorization: str | None = Header(default=None),
            mcpcodeaway_admin_session: str | None = Cookie(default=None),
        ) -> dict[str, Any]:
            await _authorize_control_plane(
                authorization, mcpcodeaway_admin_session, require_admin_role=True
            )
            return await runtime.demo_traffic.stop()

        @app.post("/admin/demo/traffic/burst")
        async def admin_demo_traffic_burst(
            count: int = Query(default=6, ge=1, le=24),
            authorization: str | None = Header(default=None),
            mcpcodeaway_admin_session: str | None = Cookie(default=None),
        ) -> dict[str, Any]:
            await _authorize_control_plane(
                authorization, mcpcodeaway_admin_session, require_admin_role=True
            )
            _ensure_demo_traffic_allowed()
            return await runtime.demo_traffic.burst(count=count)

        @app.post("/admin/mcp", response_model=None)
        async def admin_mcp_endpoint(
            request: Request,
            authorization: str | None = Header(default=None),
            mcpcodeaway_admin_session: str | None = Cookie(default=None),
        ) -> JSONResponse:
            caller = await _authorize_control_plane(
                authorization, mcpcodeaway_admin_session, require_admin_role=True
            )
            payload = await request.json()
            return await handle_admin_mcp_request(
                runtime,
                caller,
                payload,
                inspect_fn=lambda cfg: _inspect_candidate(cfg, authorization),
                analytics_fn=lambda model, tenant: audit_analytics(
                    model=model,
                    tenant_id=tenant,
                    authorization=authorization,
                    mcpcodeaway_admin_session=mcpcodeaway_admin_session,
                ),
                telemetry_fn=lambda: admin_runtime_telemetry(
                    authorization=authorization,
                    mcpcodeaway_admin_session=mcpcodeaway_admin_session,
                ),
            )

        @app.post("/admin/upstreams/ab-test")
        async def admin_upstream_ab_test(
            body: OnboardingAbTestRequest,
            authorization: str | None = Header(default=None),
            mcpcodeaway_admin_session: str | None = Cookie(default=None),
        ) -> dict[str, Any]:
            caller = await _authorize_control_plane(
                authorization, mcpcodeaway_admin_session, require_admin_role=True
            )
            if body.upstream is not None and runtime.settings.ssrf_protection_enabled:
                try:
                    for candidate_url in (body.upstream.url, body.upstream.openapi_spec_url):
                        validate_upstream_url(
                            candidate_url,
                            app_env=runtime.settings.app_env,
                            block_private_ips_in_prod=runtime.settings.block_private_ips_in_prod,
                            allowed_hosts=runtime.settings.allowed_upstream_hosts,
                        )
                except ValueError as exc:
                    raise HTTPException(status_code=400, detail=str(exc)) from exc
            eval_id = (
                caller
                or runtime._dev_discovery_identity()
                or CallerIdentity(
                    sub="admin-ab-evaluator",
                    tenant_id=(
                        runtime.settings.allowed_tenants[0]
                        if runtime.settings.allowed_tenants
                        else "tenant-acme-dev"
                    ),
                    roles=["gateway_admin", "finance_analyst", "ops_engineer"],
                    groups=["platform"],
                )
            )
            try:
                return await run_onboarding_ab_test(runtime, eval_id, body)
            except ValueError as exc:
                raise HTTPException(status_code=400, detail=str(exc)) from exc

        @app.post("/admin/upstreams/inspect")
        async def inspect_admin_upstream(
            cfg: UpstreamConfig,
            authorization: str | None = Header(default=None),
            mcpcodeaway_admin_session: str | None = Cookie(default=None),
        ) -> dict[str, Any]:
            await _authorize_control_plane(
                authorization, mcpcodeaway_admin_session, require_admin_role=True
            )
            inspection, audit = await _inspect_candidate(cfg, authorization)
            return {"inspection": inspection, "onboarding_audit": audit}

        @app.post("/admin/upstreams")
        async def add_admin_upstream(
            cfg: UpstreamConfig,
            authorization: str | None = Header(default=None),
            mcpcodeaway_admin_session: str | None = Cookie(default=None),
        ) -> dict[str, Any]:
            if not runtime.settings.dynamic_upstreams_permitted:
                raise HTTPException(
                    status_code=403,
                    detail="Dynamic upstream registration is locked in read-only/prod; "
                    "promote changes via config/upstreams.yaml PR.",
                )
            if runtime.settings.app_env == "prod" and cfg.type == "mcp_stdio":
                raise HTTPException(
                    status_code=403, detail="mcp_stdio upstreams are blocked in prod"
                )
            await _authorize_control_plane(
                authorization, mcpcodeaway_admin_session, require_admin_role=True
            )

            inspection, audit = await _inspect_candidate(cfg, authorization)
            await runtime.store.save_dynamic_upstream(cfg.name, cfg.model_dump(exclude_none=True))
            await runtime.refresh_catalog()
            await runtime.bump_catalog_epoch()
            return {
                "registered": True,
                "upstream": cfg.model_dump(exclude_none=True, exclude={"openapi_spec"}),
                "inspection": inspection,
                "onboarding_audit": audit,
                "catalog_version": runtime.catalog.catalog_version,
            }

        @app.delete("/admin/upstreams/{name}")
        async def delete_admin_upstream(
            name: str,
            authorization: str | None = Header(default=None),
            mcpcodeaway_admin_session: str | None = Cookie(default=None),
        ) -> dict[str, Any]:
            if not runtime.settings.dynamic_upstreams_permitted:
                raise HTTPException(
                    status_code=403,
                    detail="Dynamic upstream deletion is locked in read-only/prod.",
                )
            await _authorize_control_plane(
                authorization, mcpcodeaway_admin_session, require_admin_role=True
            )
            deleted = await runtime.store.delete_dynamic_upstream(name)
            if not deleted:
                raise HTTPException(status_code=404, detail=f"Dynamic upstream '{name}' not found")
            await runtime.refresh_catalog()
            await runtime.bump_catalog_epoch()
            return {
                "deleted": True,
                "name": name,
                "catalog_version": runtime.catalog.catalog_version,
            }

    if features.comparator:

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

    if features.benchmarks_api:

        @app.get("/benchmarks/latest")
        async def latest_benchmarks() -> dict[str, Any]:
            results_path = Path("benchmarks/results.json")
            load_path = Path("benchmarks/load_results.json")
            live_path = Path("benchmarks/live_llm_results.json")
            discovery_path = Path("benchmarks/discovery_results.json")
            matrix_path = Path("benchmarks/matrix_2x2_results.json")
            payload: dict[str, Any] = {
                "available": (
                    results_path.exists()
                    or load_path.exists()
                    or live_path.exists()
                    or discovery_path.exists()
                    or matrix_path.exists()
                ),
                "offline": None,
                "load": None,
                "live_llm": None,
                "discovery": None,
                "matrix_2x2": None,
            }
            if results_path.exists():
                payload["offline"] = json.loads(results_path.read_text(encoding="utf-8"))
            if load_path.exists():
                payload["load"] = json.loads(load_path.read_text(encoding="utf-8"))
            if live_path.exists():
                payload["live_llm"] = json.loads(live_path.read_text(encoding="utf-8"))
            if discovery_path.exists():
                payload["discovery"] = json.loads(discovery_path.read_text(encoding="utf-8"))
            if matrix_path.exists():
                payload["matrix_2x2"] = json.loads(matrix_path.read_text(encoding="utf-8"))
            return payload

    if features.dashboard_ui:

        def _find_spa_dir() -> Path | None:
            for candidate in (
                Path(__file__).parent / "static" / "spa",
                Path("web/.output/public"),
            ):
                if (candidate / "index.html").is_file():
                    return candidate
            return None

        @app.get("/dashboard", response_model=None)
        async def dashboard() -> FileResponse | JSONResponse:
            spa_dir = _find_spa_dir()
            if spa_dir is not None:
                return FileResponse(spa_dir / "index.html")
            return JSONResponse(
                {"detail": "Admin SPA not built; build web/ first."}, status_code=404
            )

        @app.get("/dashboard/{subpath:path}", response_model=None)
        async def dashboard_spa(subpath: str) -> FileResponse | JSONResponse:
            spa_dir = _find_spa_dir()
            if spa_dir is None:
                return JSONResponse(
                    {"detail": "Admin SPA not built; build web/ first."}, status_code=404
                )
            root = spa_dir.resolve()
            target = (root / subpath).resolve()
            if target.is_relative_to(root) and target.is_file():
                return FileResponse(target)
            return FileResponse(spa_dir / "index.html")

    return app


app = create_gateway_app()
