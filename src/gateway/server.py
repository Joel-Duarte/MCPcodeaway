"""Stateless Streamable HTTP Code-Mode MCP Gateway FastAPI application."""

from __future__ import annotations

import asyncio
import base64
import binascii
import hashlib
import json
import logging
import time
import uuid
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import httpx
from fastapi import (
    Cookie,
    FastAPI,
    Header,
    HTTPException,
    Query,
    Request,
    WebSocket,
    WebSocketDisconnect,
)
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse, PlainTextResponse
from opentelemetry import trace
from pydantic import BaseModel, Field

from gateway import __version__
from gateway.ab_testing import (
    OnboardingAbTestRequest,
    _run_matches_tool,
    compute_empirical_model_delta,
    evaluate_mode_algorithm,
    resolve_ab_experiment_variant,
    run_onboarding_ab_test,
)
from gateway.admin_auth import AdminSessionManager, create_admin_auth_router
from gateway.admin_mcp import handle_admin_mcp_request
from gateway.alerts import AlertEvaluator, AlertRulesConfig
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
from gateway.demo_traffic import DemoTier, DemoTrafficRunner
from gateway.discovery import DiscoveryEngine
from gateway.dispatcher import (
    ExecutionBudget,
    HostDispatcher,
    SubcallRecord,
    bind_positional_arguments,
    build_provenance_envelope,
    summarize_value_shape,
)
from gateway.mcp_protocol import (
    MCP_CLIENT_CAPABILITIES_META,
    MCP_PROTOCOL_VERSION,
    MCP_PROTOCOL_VERSION_META,
    MCP_SERVER_INFO_META,
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
from gateway.sandbox import (
    LocalMontyBackend,
    RemoteMontyWorkerBackend,
    SandboxBackend,
    SandboxResult,
)
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
_VALID_DISCOVERY: set[str] = {"keyword", "bm25"}

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
        self.settings.validate_production_secrets()
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
            app_env=self.settings.app_env,
            block_private_ips_in_prod=self.settings.block_private_ips_in_prod,
            allowed_hosts=self.settings.allowed_upstream_hosts,
            ssrf_protection_enabled=self.settings.ssrf_protection_enabled,
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
            self.sandbox = LocalMontyBackend(
                checkout_timeout_seconds=self.settings.monty_pool_checkout_timeout_seconds,
                request_timeout_seconds=self.settings.monty_worker_request_timeout_seconds,
            )
        self.telemetry = TelemetryRecorder(self.store)
        self.preloaded_tools = preloaded_tools or {}
        self.startup_warnings: list[str] = []
        self._startup_retry_task: asyncio.Task[None] | None = None
        self._retention_task: asyncio.Task[None] | None = None
        self._ready = False
        self.demo_traffic = DemoTrafficRunner(self)
        self.alert_evaluator = AlertEvaluator()

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
        sem = asyncio.Semaphore(16)

        async def _discover_one(upstream: UpstreamConfig) -> str | None:
            if upstream.name in self.preloaded_tools:
                return None
            headers: dict[str, str] = {}
            async with sem:
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
                        return None
                    tools = await self.client_manager.discover_tools(upstream, auth_headers=headers)
                    self.catalog.set_server_tools(upstream, tools)
                    return None
                except Exception as exc:
                    return f"Upstream '{upstream.name}' discovery failed: {exc}"

        results = await asyncio.gather(*(_discover_one(u) for u in upstreams))
        warnings = [w for w in results if w is not None]
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
        model_name: str | None = None,
        target_tool: str | None = None,
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

        tools = self.catalog.list_accessible_tools(
            identity, self.policy, self.registry, allowed_servers=allowed_servers
        )
        inferred_tool = target_tool
        if not inferred_tool and intent:
            intent_low = intent.lower()
            for t in tools:
                if t.qualified_name.lower() in intent_low or t.name.lower() in intent_low:
                    inferred_tool = t.qualified_name
                    break

        recent = await self.store.list_runs(limit=50, tenant_id=identity.tenant_id)
        if not explicit_override or requested_exec == "auto":
            for upstream in accessible_srvs:
                split = resolve_ab_experiment_variant(
                    upstream,
                    identity,
                    request_id,
                    historical_runs=recent,
                    model_name=model_name,
                    target_tool=inferred_tool,
                )
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

        tool_count = len(tools)
        classic_tokens = estimate_tokens([to_mcp_tool_definition(t) for t in tools]) if tools else 0
        mutating_ratio = (sum(1 for t in tools if t.mutating) / tool_count) if tool_count else 0.0
        typed_ratio = (
            sum(1 for t in tools if t.output_schema is not None) / tool_count if tool_count else 0.0
        )
        scoped_recent = (
            [r for r in recent if _run_matches_tool(r, inferred_tool)]
            if inferred_tool
            else recent[:30]
        ) or recent[:30]
        avg_bytes = (
            sum(int(r.get("raw_upstream_bytes", 0)) for r in scoped_recent) / len(scoped_recent)
            if scoped_recent
            else 0.0
        )
        model_delta: float | None = None
        model_reason: str | None = None
        if model_name:
            saved_scores: dict[str, Any] = {}
            if hasattr(self.store, "get_setting"):
                saved_scores = await self.store.get_setting("model_routing_scores") or {}
            entry = saved_scores.get(model_name) or saved_scores.get(model_name.lower())
            if isinstance(entry, dict) and entry.get("preferred_mode") in {
                "classic",
                "codemode",
            }:
                pref = str(entry["preferred_mode"])
                gw_p: GatewayMode = "codemode" if pref == "codemode" else "traditional"
                p_exec, p_disc = _MODE_TO_AXES[gw_p]
                return (
                    p_exec,
                    p_disc,
                    gw_p,
                    f"model_profile_override:{model_name}->{gw_p}",
                    None,
                    None,
                    None,
                )
            if isinstance(entry, (int, float)):
                model_delta = float(entry)
                model_reason = f"Admin score override for '{model_name}' ({model_delta:+.1f})"
            elif isinstance(entry, dict) and isinstance(entry.get("score_delta"), (int, float)):
                model_delta = float(entry["score_delta"])
                model_reason = (
                    str(entry.get("notes") or "")
                    or f"Admin score override for '{model_name}' ({model_delta:+.1f})"
                )
            else:
                learned = compute_empirical_model_delta(
                    recent, model_name, target_tool=inferred_tool
                )
                if learned is not None:
                    model_delta, model_reason = learned

        decision = evaluate_mode_algorithm(
            tool_count=tool_count,
            classic_schema_tokens=classic_tokens,
            avg_payload_bytes=avg_bytes,
            mutating_ratio=mutating_ratio,
            typed_output_ratio=typed_ratio,
            intent=intent,
            server_count=max(1, len({t.server for t in tools})),
            model_name=model_name,
            model_score_delta=model_delta,
            model_score_reason=model_reason,
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

    async def _retention_loop(self) -> None:
        """Background compliance retention scheduler executing saved hourly/daily prune policy."""
        while True:
            await asyncio.sleep(60.0)
            try:
                if not hasattr(self.store, "get_setting"):
                    continue
                saved = await self.store.get_setting("retention_policy") or {}
                interval = str(saved.get("auto_interval", "off"))
                if interval not in {"hourly", "daily"}:
                    continue
                days = int(saved.get("retention_days", self.settings.audit_retention_days))
                if bool(saved.get("prune_after_archive", True)):
                    await self.store.prune_runs_older_than(days)
            except asyncio.CancelledError:
                return
            except Exception:
                continue

    async def startup(self) -> None:
        self._ready = False
        await self.store.init()
        if hasattr(self.store, "get_setting"):
            saved_rules = await self.store.get_setting("alert_rules")
            if saved_rules:
                self.alert_evaluator.rules = AlertRulesConfig.model_validate(saved_rules)
        await self.refresh_catalog()
        if self.startup_warnings:
            self._startup_retry_task = asyncio.create_task(self._retry_failed_startup_upstreams())
        self._retention_task = asyncio.create_task(self._retention_loop())
        self._ready = True

    async def is_ready(self) -> bool:
        """Check lifecycle state and required shared dependencies for readiness probes."""
        if not self._ready:
            return False
        if isinstance(self.cache, RedisCacheBackend):
            try:
                if not await asyncio.wait_for(self.cache.health_check(), timeout=1.0):
                    return False
            except Exception:
                return False
        if isinstance(self.store, PostgresAuditStore):
            try:
                if not await asyncio.wait_for(self.store.health_check(), timeout=1.0):
                    return False
            except Exception:
                return False
        return True

    async def shutdown(self) -> None:
        self._ready = False
        await self.demo_traffic.stop()
        if self._retention_task is not None and not self._retention_task.done():
            self._retention_task.cancel()
            await asyncio.gather(self._retention_task, return_exceptions=True)
        self._retention_task = None
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

    def _publish_live_summary_payload(
        self, run: dict[str, Any], identity: CallerIdentity
    ) -> dict[str, Any]:
        return {
            "type": "run_recorded",
            "run_id": run.get("run_id"),
            "created_at": run.get("created_at"),
            "mode": run.get("mode"),
            "tool_name": run.get("tool_name"),
            "tenant_id": identity.tenant_id,
            "company_group": (identity.groups[0] if identity.groups else "ungrouped"),
            "user_sub": identity.sub,
            "user_email": identity.email,
            "user_groups": list(identity.groups),
            "ok": bool(run.get("ok")),
            "grounding_blocked": bool(run.get("grounding_blocked", False)),
            "duration_ms": run.get("duration_ms"),
            "subcall_count": int(run.get("subcall_count", 0)),
            "raw_upstream_bytes": int(run.get("raw_upstream_bytes", 0)),
            "returned_bytes": int(run.get("returned_bytes", 0)),
            "tokens_saved": run.get("tokens_saved", 0)
            or (int(run.get("estimated_tokens_saved", 0)) + int(run.get("schema_tokens_saved", 0))),
            "estimated_tokens_saved": int(run.get("estimated_tokens_saved", 0)),
            "schema_tokens_saved": int(run.get("schema_tokens_saved", 0)),
            "abac_rows_filtered": int(run.get("abac_rows_filtered", 0)),
            "redactions_count": int(run.get("redactions_count", 0)),
            "mode_decision_reason": run.get("mode_decision_reason"),
        }

    async def _publish_live_summary(self, run: dict[str, Any], identity: CallerIdentity) -> None:
        if not hasattr(self.cache, "publish_event"):
            return
        event = self._publish_live_summary_payload(run, identity)
        try:
            await self.cache.publish_event("mcpcodeaway:telemetry", json.dumps(event, default=str))
        except Exception:
            pass

    async def merged_model_pricing(self) -> dict[str, dict[str, float]]:
        merged = {k: dict(v) for k, v in self.settings.model_pricing.items()}
        if hasattr(self.store, "list_model_pricing"):
            custom = await self.store.list_model_pricing()
            for k, v in custom.items():
                merged[k] = dict(v)
        return merged

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
        # Prometheus is deployment-wide and commonly readable by operations
        # identities. Keep labels bounded and avoid exporting tenant identifiers.
        metric_labels = {"mode": mode}
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
        mode_decision_reason: str | None = None,
        mode_decision_score: float | None = None,
        experiment_id: str | None = None,
        ab_variant: str | None = None,
        model_name: str | None = None,
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
                mode_decision_reason=mode_decision_reason,
                mode_decision_score=mode_decision_score,
                experiment_id=experiment_id,
                ab_variant=ab_variant,
                model_name=model_name,
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
        mode_decision_reason: str | None = None,
        mode_decision_score: float | None = None,
        experiment_id: str | None = None,
        ab_variant: str | None = None,
        model_name: str | None = None,
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
        tool_call_slots = asyncio.Semaphore(self.settings.max_concurrent_tool_calls_per_execution)
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
                    async with tool_call_slots:
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
                    observed_tool_outputs.append(res)
                    observed_tool_shapes[t.qualified_name] = summarize_value_shape(res)
                    return res

                return _wrapper

            external_functions[tool.qualified_name] = _make_wrapper(tool, upstream)

        timeout_seconds = min(timeout_ms / 1000.0, self.settings.monty_timeout_seconds)
        with _TRACER.start_as_current_span("gateway.sandbox.execute") as span:
            span.set_attribute("mcp.mode", mode)
            span.set_attribute("mcp.accessible_tools", len(external_functions))
            code_bytes = len(code.encode("utf-8", errors="replace"))
            if code_bytes > self.settings.max_sandbox_code_bytes:
                sandbox_res = SandboxResult(
                    ok=False,
                    error=(
                        "Sandbox code payload exceeds the configured limit of "
                        f"{self.settings.max_sandbox_code_bytes} bytes."
                    ),
                    error_type="SandboxInputTooLarge",
                )
            else:
                try:
                    async with asyncio.timeout(self.settings.execution_wall_timeout_seconds):
                        sandbox_res = await self.sandbox.execute(
                            code,
                            external_functions=external_functions,
                            timeout_seconds=timeout_seconds,
                        )
                except TimeoutError:
                    sandbox_res = SandboxResult(
                        ok=False,
                        error=(
                            "Execution exceeded the gateway wall-clock limit of "
                            f"{self.settings.execution_wall_timeout_seconds:g} seconds."
                        ),
                        error_type="ExecutionTimeoutError",
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
            model_name=model_name,
            monty_spawn_ms=sandbox_res.monty_spawn_ms,
            monty_exec_ms=sandbox_res.monty_exec_ms,
        )
        total_saved = self._record_execution_metrics(
            identity=identity,
            mode=mode,
            ok=effective_ok,
            duration_ms=duration_ms,
            run=run,
        )
        await self._publish_live_summary(run, identity)
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
        mode_decision_reason: str | None = None,
        mode_decision_score: float | None = None,
        experiment_id: str | None = None,
        ab_variant: str | None = None,
        model_name: str | None = None,
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
                model_name=model_name,
            )
            self._record_execution_metrics(
                identity=identity,
                mode=mode,
                ok=ok,
                duration_ms=duration_ms,
                run=run,
            )
            await self._publish_live_summary(run, identity)
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
    if isinstance(result, dict):
        result.setdefault("resultType", "complete")
        result.setdefault(
            "_meta",
            {
                MCP_SERVER_INFO_META: {"name": "mcpcodeaway", "version": __version__},
            },
        )
    return JSONResponse({"jsonrpc": "2.0", "id": req_id, "result": result})


def _jsonrpc_err(req_id: Any, code: int, message: str) -> JSONResponse:
    return JSONResponse(
        {"jsonrpc": "2.0", "id": req_id, "error": {"code": code, "message": message}}
    )


def _validate_modern_mcp_request(request: Request, payload: Any) -> JSONResponse | None:
    """Require the current sessionless per-request envelope and HTTP headers."""
    if not isinstance(payload, dict):
        return JSONResponse(
            {
                "jsonrpc": "2.0",
                "id": None,
                "error": {"code": -32020, "message": "MCP request must be a JSON object"},
            },
            status_code=400,
        )
    method = payload.get("method")
    params = payload.get("params")
    params = params if isinstance(params, dict) else {}
    meta = params.get("_meta")
    meta = meta if isinstance(meta, dict) else {}
    body_version = meta.get(MCP_PROTOCOL_VERSION_META)
    header_version = request.headers.get("mcp-protocol-version")
    method_header = request.headers.get("mcp-method")
    name_header = request.headers.get("mcp-name")
    req_id = payload.get("id")

    def mismatch(message: str) -> JSONResponse:
        return JSONResponse(
            {"jsonrpc": "2.0", "id": req_id, "error": {"code": -32020, "message": message}},
            status_code=400,
        )

    if request.headers.get("mcp-session-id"):
        return mismatch("MCP protocol sessions are not supported")

    request_id = payload.get("id")
    if payload.get("jsonrpc") != "2.0" or not (
        isinstance(request_id, str)
        or (isinstance(request_id, int) and not isinstance(request_id, bool))
    ):
        return mismatch("MCP requests require JSON-RPC 2.0 and a non-null string or integer id")
    if not isinstance(method, str):
        return mismatch("MCP request method must be a string")

    if not isinstance(body_version, str) or not isinstance(
        meta.get(MCP_CLIENT_CAPABILITIES_META), dict
    ):
        return mismatch(
            "Modern MCP requests require protocolVersion and clientCapabilities metadata"
        )
    if not header_version or header_version != body_version:
        return mismatch("MCP-Protocol-Version header does not match request metadata")
    if not isinstance(method, str) or not method_header or method_header != method:
        return mismatch("Mcp-Method header does not match request method")

    if body_version != MCP_PROTOCOL_VERSION:
        return JSONResponse(
            {
                "jsonrpc": "2.0",
                "id": req_id,
                "error": {
                    "code": -32022,
                    "message": "Unsupported protocol version",
                    "data": {"supportedVersions": [MCP_PROTOCOL_VERSION]},
                },
            },
            status_code=400,
        )

    if method == "tools/call":
        name = params.get("name")
        name_header = request.headers.get("mcp-name")
        if (
            isinstance(name_header, str)
            and name_header.startswith("=?base64?")
            and name_header.endswith("?=")
        ):
            try:
                name_header = base64.b64decode(
                    name_header[len("=?base64?") : -2], validate=True
                ).decode("utf-8")
            except (binascii.Error, UnicodeDecodeError):
                return mismatch("Mcp-Name header contains invalid Base64 encoding")
        if not isinstance(name, str) or not name_header or name_header != name:
            return mismatch("Mcp-Name header does not match tools/call name")
    return None


class CompareRequest(BaseModel):
    scenario: str = "reconcile_overdue"
    persona: str = "finance_analyst"
    custom_classic_calls: list[dict[str, Any]] | None = None
    custom_code: str | None = None


class ModelPriceBody(BaseModel):
    model_name: str = Field(..., min_length=1, max_length=64)
    input_per_1m_usd: float = Field(..., ge=0.0, le=1000.0)
    output_per_1m_usd: float = Field(..., ge=0.0, le=1000.0)


class ModelRoutingScoreBody(BaseModel):
    model_name: str = Field(..., min_length=1, max_length=128)
    score_delta: float = Field(default=0.0, ge=-50.0, le=50.0)
    preferred_mode: str | None = None
    notes: str = ""


class UpstreamTestCallRequest(BaseModel):
    upstream: UpstreamConfig
    tool_name: str
    arguments: dict[str, Any] = Field(default_factory=dict)


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
        if runtime.settings.app_env == "prod":
            return {"status": "ok"}
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

    @app.get("/ready", response_model=None)
    async def ready() -> dict[str, Any] | JSONResponse:
        is_ready = await runtime.is_ready()
        if not is_ready:
            return JSONResponse(status_code=503, content={"ready": False})
        return {"ready": True}

    mcp_request_slots = asyncio.Semaphore(max(1, runtime.settings.max_concurrent_mcp_requests))

    @app.middleware("http")
    async def bound_mcp_concurrency(request: Request, call_next: Callable[..., Any]) -> Any:
        if request.url.path != "/mcp":
            return await call_next(request)
        try:
            await asyncio.wait_for(mcp_request_slots.acquire(), timeout=0.05)
        except TimeoutError:
            return JSONResponse(
                status_code=503,
                content={"detail": "Gateway is at its concurrent request limit."},
                headers={"Retry-After": "1"},
            )
        try:
            return await call_next(request)
        finally:
            mcp_request_slots.release()

    @app.post("/mcp")
    async def mcp_endpoint(
        request: Request,
        mode: str | None = Query(default=None),
        execution: str | None = Query(default=None),
        discovery_mode: str | None = Query(default=None),
        discovery: str | None = Query(default=None),
        servers: str | None = Query(default=None),
        model: str | None = Query(default=None),
        require_grounding: bool | None = Query(default=None),
        x_gateway_mode: str | None = Header(default=None),
        x_execution_mode: str | None = Header(default=None),
        x_discovery_mode: str | None = Header(default=None),
        x_discovery_backend: str | None = Header(default=None),
        x_mcp_servers: str | None = Header(default=None),
        x_intent: str | None = Header(default=None),
        x_model_name: str | None = Header(default=None),
        x_agent_model: str | None = Header(default=None),
        x_require_grounding: str | None = Header(default=None),
        x_request_id: str | None = Header(default=None),
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
                origin = request.headers.get("origin")
                if origin is not None and origin not in runtime.settings.cors_allowed_origins:
                    return JSONResponse(
                        {"detail": "Origin is not allowed for the MCP endpoint."}, status_code=403
                    )
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
                    base_url = runtime.settings.gateway_public_url.rstrip("/")
                    if exc.status_code == 401:
                        guidance = (
                            f"{exc.detail}. Remediation for agent/user: Authenticate via OIDC SSO "
                            f"(discover at '{base_url}/.well-known/oauth-protected-resource' "
                            f"for issuer '{runtime.settings.oidc_issuer}' and audience "
                            f"'{runtime.settings.oidc_audience}')"
                        )
                        if runtime.settings.auth_mode == "dev":
                            guidance += (
                                f", or mint a local dev token via POST '{base_url}/dev/auth/token'"
                            )
                        guidance += (
                            " and pass 'Authorization: Bearer <token>'. Inform the user that "
                            "their MCP session needs to sign in or refresh its credentials."
                        )
                    else:
                        guidance = (
                            f"{exc.detail}. Remediation for agent/user: Inform the user that "
                            "their identity token is from an untrusted tenant or lacks access; "
                            f"permitted tenants: {runtime.settings.allowed_tenants}."
                        )
                    raise HTTPException(
                        status_code=exc.status_code, detail=guidance, headers=headers
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
                protocol_error = _validate_modern_mcp_request(request, payload)
                if protocol_error is not None:
                    return protocol_error
                req_id = payload.get("id")
                method = payload.get("method")
                params = payload.get("params") or {}
                request_meta = params.get("_meta", {}) if isinstance(params, dict) else {}
                client_info = (
                    request_meta.get("io.modelcontextprotocol/clientInfo")
                    or params.get("clientInfo")
                    if isinstance(params, dict) and isinstance(request_meta, dict)
                    else None
                )
                client_model = (
                    (client_info.get("model") or client_info.get("name"))
                    if isinstance(client_info, dict)
                    else None
                )
                effective_model_name = (
                    model
                    or x_model_name
                    or x_agent_model
                    or (params.get("model") if isinstance(params, dict) else None)
                    or client_model
                )
                if isinstance(effective_model_name, str):
                    effective_model_name = effective_model_name.strip() or None
                else:
                    effective_model_name = None
                effective_intent = x_intent or (
                    str(params.get("intent") or params.get("query") or "").strip() or None
                    if isinstance(params, dict) and (params.get("intent") or params.get("query"))
                    else None
                )
                mcp_span.set_attribute("mcp.method", str(method))
                mcp_span.set_attribute("mcp.mode", effective_mode)
                if effective_model_name:
                    mcp_span.set_attribute("mcp.model_name", effective_model_name)

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
                        model_name=effective_model_name,
                        status=status_label,
                        duration_ms=dur_ms,
                    )
                    resp.headers["X-Request-Id"] = req_corr_id
                    if mode_decision_reason:
                        resp.headers["X-Gateway-Mode-Decision"] = mode_decision_reason
                    if effective_model_name:
                        resp.headers["X-Gateway-Model"] = effective_model_name
                    return resp

                if method == "server/discover":
                    return _finish(
                        _jsonrpc_ok(
                            req_id,
                            {
                                "supportedVersions": [MCP_PROTOCOL_VERSION],
                                "capabilities": {"tools": {}},
                                "_meta": {
                                    MCP_SERVER_INFO_META: {
                                        "name": "mcpcodeaway",
                                        "version": __version__,
                                    }
                                },
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
                await runtime.ensure_catalog_for_identity(identity)
                target_tool_hint: str | None = None
                if method == "tools/call" and isinstance(params, dict):
                    raw_tname = str(params.get("name") or "")
                    if raw_tname and raw_tname not in {
                        "list_servers",
                        "search_tools",
                        "execute_code",
                    }:
                        target_tool_hint = raw_tname
                    elif raw_tname == "execute_code":
                        code_str = str((params.get("arguments") or {}).get("code") or "")
                        for t in runtime.catalog.list_accessible_tools(
                            identity,
                            runtime.policy,
                            runtime.registry,
                            allowed_servers=allowed_servers,
                        ):
                            if t.qualified_name in code_str:
                                target_tool_hint = t.qualified_name
                                break
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
                    intent=effective_intent,
                    request_id=req_corr_id,
                    explicit_override=has_explicit_mode,
                    model_name=effective_model_name,
                    target_tool=target_tool_hint,
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
                    listed.setdefault("ttlMs", 0)
                    listed.setdefault("cacheScope", "private")
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
                            mode_decision_reason=mode_decision_reason,
                            mode_decision_score=mode_decision_score,
                            experiment_id=experiment_id,
                            ab_variant=ab_variant,
                            model_name=effective_model_name,
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
                            mode_decision_reason=mode_decision_reason,
                            mode_decision_score=mode_decision_score,
                            experiment_id=experiment_id,
                            ab_variant=ab_variant,
                            model_name=effective_model_name,
                        )
                    except KeyError as exc:
                        return _finish(_jsonrpc_err(req_id, -32601, str(exc)), status_label="error")
                    except PermissionError as exc:
                        return _finish(_jsonrpc_err(req_id, -31003, str(exc)), status_label="error")
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

                unsupported_method = _jsonrpc_err(
                    req_id, -32601, f"Unsupported MCP method '{method}'"
                )
                unsupported_method.status_code = 404
                return _finish(unsupported_method, status_label="error")
            finally:
                clear_request_context()

    async def _authorize_control_plane(
        authorization: str | None,
        admin_cookie: str | None = None,
        *,
        require_admin_role: bool = True,
        allow_viewer: bool = False,
        allowed_roles: set[str] | None = None,
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
            role_allowlist = (
                set(allowed_roles)
                if allowed_roles is not None
                else set(runtime.settings.admin_roles)
            )
            if allow_viewer:
                role_allowlist |= set(runtime.settings.viewer_roles)
            if require_admin_role and not (set(identity.roles) & role_allowlist):
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
                role_allowlist = (
                    set(allowed_roles)
                    if allowed_roles is not None
                    else set(runtime.settings.admin_roles)
                )
                if allow_viewer:
                    role_allowlist |= set(runtime.settings.viewer_roles)
                if require_admin_role and not (set(cookie_id.roles) & role_allowlist):
                    raise HTTPException(
                        status_code=403,
                        detail="Admin role required for control-plane access.",
                    )
                return cookie_id
            if runtime.settings.admin_auth_required:
                raise HTTPException(
                    status_code=401,
                    detail="Invalid or expired admin session cookie.",
                    headers={"WWW-Authenticate": "Bearer"},
                )
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
            if runtime.settings.app_env == "prod" or runtime.settings.metrics_require_auth:
                await _authorize_control_plane(
                    authorization,
                    mcpcodeaway_admin_session,
                    require_admin_role=True,
                    allowed_roles=set(runtime.settings.metrics_roles),
                )
            return PlainTextResponse(
                runtime.metrics.render_prometheus(),
                media_type="text/plain; version=0.0.4; charset=utf-8",
            )

    if features.audit_api:

        @app.get("/audit/runs")
        async def audit_runs(
            limit: int = Query(default=25, ge=1, le=200),
            offset: int = Query(default=0, ge=0),
            status: str = Query(default="all"),
            mode: str | None = Query(default=None),
            q: str | None = Query(default=None),
            tenant_id: str | None = Query(default=None),
            authorization: str | None = Header(default=None),
            mcpcodeaway_admin_session: str | None = Cookie(default=None),
        ) -> dict[str, Any]:
            identity = await _authorize_control_plane(
                authorization, mcpcodeaway_admin_session, require_admin_role=False
            )
            effective_tenant = _resolve_audit_tenant_scope(identity, tenant_id)
            if hasattr(runtime.store, "query_runs"):
                runs, total_count = await runtime.store.query_runs(
                    limit=limit,
                    offset=offset,
                    tenant_id=effective_tenant,
                    status=status,
                    mode=mode,
                    q=q,
                )
            else:
                runs = await runtime.store.list_runs(limit=limit, tenant_id=effective_tenant)
                total_count = len(runs)
            return {
                "count": len(runs),
                "total_count": total_count,
                "limit": limit,
                "offset": offset,
                "runs": runs,
            }

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
                model_pricing=await runtime.merged_model_pricing(),
            )

    if features.admin_read:

        @app.get("/admin/pricing/models")
        async def admin_list_model_pricing(
            authorization: str | None = Header(default=None),
            mcpcodeaway_admin_session: str | None = Cookie(default=None),
        ) -> dict[str, Any]:
            await _authorize_control_plane(
                authorization,
                mcpcodeaway_admin_session,
                require_admin_role=True,
                allow_viewer=True,
            )
            custom: dict[str, dict[str, float]] = {}
            if hasattr(runtime.store, "list_model_pricing"):
                custom = await runtime.store.list_model_pricing()
            return {
                "models": await runtime.merged_model_pricing(),
                "custom_models": sorted(custom),
                "defaults": sorted(runtime.settings.model_pricing),
                "billing_note": (
                    "Models per-token API pricing ($/1M in & out); "
                    "subscription plans are not exposed by MCP."
                ),
            }

        @app.websocket("/admin/ws/telemetry")
        async def admin_ws_telemetry(
            websocket: WebSocket,
            mcpcodeaway_admin_session: str | None = Cookie(default=None),
        ) -> None:
            auth_hdr = websocket.headers.get("authorization")
            try:
                identity = await _authorize_control_plane(
                    auth_hdr,
                    mcpcodeaway_admin_session,
                    require_admin_role=True,
                    allow_viewer=True,
                )
            except HTTPException:
                await websocket.close(code=4403)
                return

            is_admin = identity is None or bool(
                set(identity.roles) & set(runtime.settings.admin_roles)
            )
            tenant_scope = None if is_admin else identity.tenant_id

            await websocket.accept()
            initial_rt = await collect_runtime_telemetry(runtime)
            recent_runs = await runtime.store.list_runs(limit=50, tenant_id=tenant_scope)
            initial_alerts = runtime.alert_evaluator.evaluate(
                runs=recent_runs,
                drift_warnings=runtime.catalog.drift_warnings,
                startup_warnings=runtime.startup_warnings,
                worker_status=initial_rt.get("sandbox_worker", {}),
            )
            await websocket.send_json(
                {
                    "type": "snapshot",
                    "runtime": initial_rt,
                    "demo_traffic": runtime.demo_traffic.status(),
                    "alerts": initial_alerts,
                }
            )

            async def _forward_pubsub() -> None:
                if not hasattr(runtime.cache, "subscribe_events"):
                    return
                async for raw_msg in runtime.cache.subscribe_events("mcpcodeaway:telemetry"):
                    try:
                        event = json.loads(raw_msg)
                    except (TypeError, json.JSONDecodeError):
                        continue
                    if not is_admin and event.get("tenant_id") != tenant_scope:
                        continue
                    await websocket.send_json(event)

            async def _runtime_ticks() -> None:
                while True:
                    await asyncio.sleep(2.0)
                    rt_snap = await collect_runtime_telemetry(runtime)
                    tick_runs = await runtime.store.list_runs(limit=50, tenant_id=tenant_scope)
                    tick_alerts = runtime.alert_evaluator.evaluate(
                        runs=tick_runs,
                        drift_warnings=runtime.catalog.drift_warnings,
                        startup_warnings=runtime.startup_warnings,
                        worker_status=rt_snap.get("sandbox_worker", {}),
                    )
                    await runtime.alert_evaluator.notify_webhook_if_needed(
                        tick_alerts,
                        app_env=runtime.settings.app_env,
                        block_private_ips_in_prod=runtime.settings.block_private_ips_in_prod,
                        allowed_hosts=runtime.settings.allowed_upstream_hosts,
                    )
                    await websocket.send_json(
                        {
                            "type": "runtime_tick",
                            "runtime": rt_snap,
                            "demo_traffic": runtime.demo_traffic.status(),
                            "alerts": tick_alerts,
                        }
                    )

            pub_task = asyncio.create_task(_forward_pubsub())
            tick_task = asyncio.create_task(_runtime_ticks())
            try:
                while True:
                    await websocket.receive_text()
            except WebSocketDisconnect:
                pass
            finally:
                pub_task.cancel()
                tick_task.cancel()
                await asyncio.gather(pub_task, tick_task, return_exceptions=True)

        @app.get("/admin/roles")
        async def admin_list_roles_and_groups(
            authorization: str | None = Header(default=None),
            mcpcodeaway_admin_session: str | None = Cookie(default=None),
        ) -> dict[str, Any]:
            await _authorize_control_plane(
                authorization,
                mcpcodeaway_admin_session,
                require_admin_role=True,
                allow_viewer=True,
            )
            roles: set[str] = set()
            groups: set[str] = set()
            for persona in DEV_PERSONAS.values():
                roles.update(persona.get("roles", []))
                groups.update(persona.get("groups", []))
            for u in runtime.registry.list_upstreams(include_disabled=True):
                roles.update(u.allowed_roles)
                groups.update(u.allowed_groups)
            return {"roles": sorted(roles), "groups": sorted(groups)}

        @app.get("/admin/alerts")
        async def admin_list_alerts(
            authorization: str | None = Header(default=None),
            mcpcodeaway_admin_session: str | None = Cookie(default=None),
        ) -> dict[str, Any]:
            await _authorize_control_plane(
                authorization,
                mcpcodeaway_admin_session,
                require_admin_role=True,
                allow_viewer=True,
            )
            rt_snap = await collect_runtime_telemetry(runtime)
            recent = await runtime.store.list_runs(limit=50)
            alerts = runtime.alert_evaluator.evaluate(
                runs=recent,
                drift_warnings=runtime.catalog.drift_warnings,
                startup_warnings=runtime.startup_warnings,
                worker_status=rt_snap.get("sandbox_worker", {}),
            )
            await runtime.alert_evaluator.notify_webhook_if_needed(
                alerts,
                app_env=runtime.settings.app_env,
                block_private_ips_in_prod=runtime.settings.block_private_ips_in_prod,
                allowed_hosts=runtime.settings.allowed_upstream_hosts,
            )
            return {
                "alerts": alerts,
                "rules": runtime.alert_evaluator.rules.model_dump(),
            }

        @app.get("/admin/alerts/rules")
        async def admin_get_alert_rules(
            authorization: str | None = Header(default=None),
            mcpcodeaway_admin_session: str | None = Cookie(default=None),
        ) -> dict[str, Any]:
            await _authorize_control_plane(
                authorization,
                mcpcodeaway_admin_session,
                require_admin_role=True,
                allow_viewer=True,
            )
            return runtime.alert_evaluator.rules.model_dump()

        @app.get("/admin/upstreams")
        async def list_admin_upstreams(
            q: str | None = Query(default=None),
            group: str | None = Query(default=None),
            auth_type: str | None = Query(default=None),
            mode: str | None = Query(default=None),
            page: int = Query(default=1, ge=1),
            page_size: int = Query(default=200, ge=1, le=500),
            authorization: str | None = Header(default=None),
            mcpcodeaway_admin_session: str | None = Cookie(default=None),
        ) -> dict[str, Any]:
            await _authorize_control_plane(
                authorization,
                mcpcodeaway_admin_session,
                require_admin_role=True,
                allow_viewer=True,
            )
            await runtime.sync_catalog_if_stale()
            all_upstreams = runtime.registry.list_upstreams(include_disabled=True)
            facets_groups = sorted({g for u in all_upstreams for g in u.allowed_groups})
            facets_auth = sorted({u.auth_type for u in all_upstreams})
            facets_modes = sorted({u.preferred_mode or "codemode" for u in all_upstreams})

            filtered = all_upstreams
            if q:
                needle = q.strip().lower()
                filtered = [
                    u
                    for u in filtered
                    if needle in u.name.lower() or needle in (u.url or "").lower()
                ]
            if group:
                filtered = [u for u in filtered if group in u.allowed_groups]
            if auth_type:
                filtered = [u for u in filtered if u.auth_type == auth_type]
            if mode:
                filtered = [u for u in filtered if u.preferred_mode == mode]

            total = len(filtered)
            total_pages = max(1, (total + page_size - 1) // page_size)
            start = (page - 1) * page_size
            page_items = filtered[start : start + page_size]
            return {
                "app_env": runtime.settings.app_env,
                "profile": runtime.settings.effective_profile_name,
                "dynamic_upstreams_permitted": runtime.settings.dynamic_upstreams_permitted,
                "catalog_version": runtime.catalog.catalog_version,
                "digests_by_server": runtime.catalog.digests_by_server,
                "drift_warnings": runtime.catalog.drift_warnings,
                "upstreams": [u.model_dump(exclude={"openapi_spec"}) for u in page_items],
                "total": total,
                "total_registered": len(all_upstreams),
                "page": page,
                "page_size": page_size,
                "total_pages": total_pages,
                "facets": {
                    "groups": facets_groups,
                    "auth_types": facets_auth,
                    "modes": facets_modes,
                },
                "comparison_audit": audit_catalog_comparison(runtime.catalog, runtime.registry),
            }

        @app.get("/admin/telemetry/runtime")
        async def admin_runtime_telemetry(
            authorization: str | None = Header(default=None),
            mcpcodeaway_admin_session: str | None = Cookie(default=None),
        ) -> dict[str, Any]:
            await _authorize_control_plane(
                authorization,
                mcpcodeaway_admin_session,
                require_admin_role=True,
                allow_viewer=True,
            )
            return await collect_runtime_telemetry(runtime)

        @app.get("/admin/ab/experiments")
        async def admin_ab_experiments(
            authorization: str | None = Header(default=None),
            mcpcodeaway_admin_session: str | None = Cookie(default=None),
        ) -> dict[str, Any]:
            await _authorize_control_plane(
                authorization,
                mcpcodeaway_admin_session,
                require_admin_role=True,
                allow_viewer=True,
            )
            analytics = await runtime.telemetry.analytics_report(
                limit=1000,
                model_pricing=await runtime.merged_model_pricing(),
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

        @app.get("/admin/routing/models")
        async def admin_get_model_routing_scores(
            authorization: str | None = Header(default=None),
            mcpcodeaway_admin_session: str | None = Cookie(default=None),
        ) -> dict[str, Any]:
            await _authorize_control_plane(
                authorization,
                mcpcodeaway_admin_session,
                require_admin_role=True,
                allow_viewer=True,
            )
            saved: dict[str, Any] = {}
            if hasattr(runtime.store, "get_setting"):
                saved = await runtime.store.get_setting("model_routing_scores") or {}
            recent = await runtime.store.list_runs(limit=250)
            seen_models = sorted(
                {str(r.get("model_name")).strip() for r in recent if r.get("model_name")}
            )
            learned: dict[str, Any] = {}
            for m_name in seen_models:
                emp = compute_empirical_model_delta(recent, m_name)
                if emp is not None:
                    learned[m_name] = {"score_delta": emp[0], "reason": emp[1]}
            return {"admin_overrides": saved, "learned_from_runs": learned}

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

        @app.post("/admin/audit/archive")
        async def admin_archive_audit_runs(
            retention_days: int | None = Query(default=None, ge=0, le=3650),
            prune_after_archive: bool = Query(default=False),
            s3_bucket: str | None = Query(default=None),
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
            if not hasattr(runtime.store, "export_runs_older_than"):
                raise HTTPException(
                    status_code=501, detail="Active AuditStore does not support archive export"
                )
            rows = await runtime.store.export_runs_older_than(days)
            jsonl_payload = "\n".join(json.dumps(r, default=str) for r in rows)
            if jsonl_payload:
                jsonl_payload += "\n"
            digest = hashlib.sha256(jsonl_payload.encode("utf-8")).hexdigest()
            stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
            filename = f"mcpcodeaway-audit-archive-{stamp}.jsonl"
            bucket = (s3_bucket or runtime.settings.audit_archive_s3_bucket or "").strip()
            s3_uri: str | None = None
            if bucket and rows:
                try:
                    import boto3

                    client_kwargs: dict[str, Any] = {
                        "region_name": runtime.settings.audit_archive_s3_region
                    }
                    if runtime.settings.audit_archive_s3_endpoint:
                        client_kwargs["endpoint_url"] = runtime.settings.audit_archive_s3_endpoint
                    s3 = boto3.client("s3", **client_kwargs)
                    prefix = runtime.settings.audit_archive_s3_prefix.strip("/")
                    key = f"{prefix}/{filename}" if prefix else filename
                    await asyncio.to_thread(
                        s3.put_object,
                        Bucket=bucket,
                        Key=key,
                        Body=jsonl_payload.encode("utf-8"),
                        ContentType="application/x-ndjson",
                    )
                    s3_uri = f"s3://{bucket}/{key}"
                except ImportError as exc:
                    raise HTTPException(
                        status_code=501,
                        detail=(
                            "boto3 is required for direct S3 upload; "
                            "omit s3_bucket for JSONL download"
                        ),
                    ) from exc
                except Exception as exc:
                    raise HTTPException(
                        status_code=502, detail=f"S3 archive upload failed: {exc}"
                    ) from exc

            deleted = 0
            if prune_after_archive and rows:
                deleted = await runtime.store.prune_runs_older_than(days)
            return {
                "exported_runs": len(rows),
                "deleted_runs": deleted,
                "retention_days": days,
                "filename": filename,
                "sha256": digest,
                "s3_uri": s3_uri,
                "jsonl_content": None if s3_uri else jsonl_payload,
            }

        @app.get("/admin/audit/retention-policy")
        async def admin_get_retention_policy(
            authorization: str | None = Header(default=None),
            mcpcodeaway_admin_session: str | None = Cookie(default=None),
        ) -> dict[str, Any]:
            await _authorize_control_plane(
                authorization, mcpcodeaway_admin_session, require_admin_role=True
            )
            saved: dict[str, Any] = {}
            if hasattr(runtime.store, "get_setting"):
                saved = await runtime.store.get_setting("retention_policy") or {}
            return {
                "retention_days": int(
                    saved.get("retention_days", runtime.settings.audit_retention_days)
                ),
                "auto_interval": str(saved.get("auto_interval", "off")),
                "s3_bucket": str(saved.get("s3_bucket", runtime.settings.audit_archive_s3_bucket)),
                "prune_after_archive": bool(saved.get("prune_after_archive", True)),
            }

        @app.post("/admin/audit/retention-policy")
        async def admin_save_retention_policy(
            retention_days: int = Query(default=90, ge=0, le=3650),
            auto_interval: str = Query(default="off"),
            s3_bucket: str = Query(default=""),
            prune_after_archive: bool = Query(default=True),
            authorization: str | None = Header(default=None),
            mcpcodeaway_admin_session: str | None = Cookie(default=None),
        ) -> dict[str, Any]:
            await _authorize_control_plane(
                authorization, mcpcodeaway_admin_session, require_admin_role=True
            )
            if auto_interval not in {"off", "hourly", "daily"}:
                raise HTTPException(
                    status_code=400, detail="auto_interval must be 'off', 'hourly', or 'daily'"
                )
            policy = {
                "retention_days": retention_days,
                "auto_interval": auto_interval,
                "s3_bucket": s3_bucket.strip(),
                "prune_after_archive": prune_after_archive,
            }
            if hasattr(runtime.store, "save_setting"):
                await runtime.store.save_setting("retention_policy", policy)
            return {"saved": True, "policy": policy}

        @app.post("/admin/upstreams/{name}/optimize-mode")
        async def admin_optimize_upstream_mode(
            name: str,
            target_mode: str = Query(default="codemode"),
            authorization: str | None = Header(default=None),
            mcpcodeaway_admin_session: str | None = Cookie(default=None),
        ) -> dict[str, Any]:
            await _authorize_control_plane(
                authorization, mcpcodeaway_admin_session, require_admin_role=True
            )
            if target_mode not in {"codemode", "auto", "hybrid", "traditional"}:
                raise HTTPException(status_code=400, detail="Invalid target_mode")
            upstream = runtime.registry.get(name)
            if upstream is None:
                raise HTTPException(status_code=404, detail=f"Upstream '{name}' not found")
            updated = upstream.model_copy(update={"preferred_mode": target_mode})
            if runtime.settings.dynamic_upstreams_permitted:
                await runtime.store.save_dynamic_upstream(
                    updated.name, updated.model_dump(exclude_none=True)
                )
                await runtime.refresh_catalog()
                await runtime.bump_catalog_epoch()
            else:
                runtime.registry.upsert(updated)
            return {
                "updated": True,
                "upstream": name,
                "preferred_mode": target_mode,
            }

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

        @app.post("/admin/alerts/rules")
        async def admin_save_alert_rules(
            body: AlertRulesConfig,
            authorization: str | None = Header(default=None),
            mcpcodeaway_admin_session: str | None = Cookie(default=None),
        ) -> dict[str, Any]:
            await _authorize_control_plane(
                authorization, mcpcodeaway_admin_session, require_admin_role=True
            )
            urls_to_check = [body.webhook_url] + [r.webhook_url for r in body.custom_rules]
            for raw_url in urls_to_check:
                if raw_url:
                    try:
                        validate_upstream_url(
                            raw_url,
                            app_env=runtime.settings.app_env,
                            block_private_ips_in_prod=runtime.settings.block_private_ips_in_prod,
                            allowed_hosts=runtime.settings.allowed_upstream_hosts,
                        )
                    except ValueError as exc:
                        raise HTTPException(status_code=400, detail=str(exc)) from exc
            runtime.alert_evaluator.rules = body
            if hasattr(runtime.store, "save_setting"):
                await runtime.store.save_setting("alert_rules", body.model_dump())
            return {"saved": True, "rules": body.model_dump()}

        @app.post("/admin/alerts/test-webhook")
        async def admin_test_alert_webhook(
            webhook_url: str | None = Query(default=None),
            authorization: str | None = Header(default=None),
            mcpcodeaway_admin_session: str | None = Cookie(default=None),
        ) -> dict[str, Any]:
            await _authorize_control_plane(
                authorization, mcpcodeaway_admin_session, require_admin_role=True
            )
            target = (webhook_url or runtime.alert_evaluator.rules.webhook_url or "").strip()
            if not target:
                raise HTTPException(status_code=400, detail="No webhook_url configured")
            try:
                validate_upstream_url(
                    target,
                    app_env=runtime.settings.app_env,
                    block_private_ips_in_prod=runtime.settings.block_private_ips_in_prod,
                    allowed_hosts=runtime.settings.allowed_upstream_hosts,
                )
            except ValueError as exc:
                raise HTTPException(status_code=400, detail=str(exc)) from exc
            is_https = target.lower().startswith("https://")
            try:
                async with httpx.AsyncClient(timeout=5.0) as client:
                    resp = await client.post(
                        target,
                        json={
                            "source": "mcpcodeaway",
                            "test": True,
                            "alerts": [
                                {
                                    "id": "webhook_test",
                                    "severity": "info",
                                    "title": "MCPcodeaway test alert webhook",
                                }
                            ],
                        },
                    )
                return {
                    "delivered": resp.is_success,
                    "status_code": resp.status_code,
                    "https_enforced": is_https,
                    "detail": f"HTTP {resp.status_code}" if resp.is_success else resp.text[:200],
                }
            except Exception as exc:
                return {
                    "delivered": False,
                    "status_code": None,
                    "https_enforced": is_https,
                    "detail": str(exc),
                }

        @app.post("/admin/upstreams/test-call")
        async def admin_test_upstream_call(
            body: UpstreamTestCallRequest,
            authorization: str | None = Header(default=None),
            mcpcodeaway_admin_session: str | None = Cookie(default=None),
        ) -> dict[str, Any]:
            caller = await _authorize_control_plane(
                authorization, mcpcodeaway_admin_session, require_admin_role=True
            )
            if runtime.settings.ssrf_protection_enabled:
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
            ident = (
                caller
                or runtime._dev_discovery_identity()
                or CallerIdentity(
                    sub="admin-probe",
                    tenant_id=(
                        runtime.settings.allowed_tenants[0]
                        if runtime.settings.allowed_tenants
                        else "tenant-acme-dev"
                    ),
                    roles=["gateway_admin", "finance_analyst", "ops_engineer"],
                    groups=["platform"],
                )
            )
            headers: dict[str, str] = {}
            if body.upstream.auth_type != "none":
                headers, _, _ = await runtime.dispatcher.resolve_upstream_headers(
                    ident, body.upstream
                )
            tools = await runtime.client_manager.discover_tools(body.upstream, auth_headers=headers)
            target_tool = next(
                (
                    t
                    for t in tools
                    if t.name == body.tool_name or t.qualified_name == body.tool_name
                ),
                None,
            )
            if target_tool is None:
                raise HTTPException(
                    status_code=404, detail=f"Tool '{body.tool_name}' not found on upstream"
                )
            if target_tool.mutating:
                raise HTTPException(
                    status_code=400, detail="Live test call only supports read-only tools"
                )
            result = await runtime.client_manager.call_tool(
                body.upstream,
                target_tool,
                body.arguments,
                auth_headers=headers,
            )
            return {
                "ok": True,
                "tool": target_tool.qualified_name,
                "result": result,
            }

        @app.post("/admin/pricing/models")
        async def admin_save_model_pricing(
            body: ModelPriceBody,
            authorization: str | None = Header(default=None),
            mcpcodeaway_admin_session: str | None = Cookie(default=None),
        ) -> dict[str, Any]:
            await _authorize_control_plane(
                authorization, mcpcodeaway_admin_session, require_admin_role=True
            )
            if not hasattr(runtime.store, "save_model_pricing"):
                raise HTTPException(
                    status_code=501, detail="Active AuditStore lacks pricing storage"
                )
            await runtime.store.save_model_pricing(
                body.model_name.strip(), body.input_per_1m_usd, body.output_per_1m_usd
            )
            return {"saved": True, "models": await runtime.merged_model_pricing()}

        @app.delete("/admin/pricing/models/{model_name}")
        async def admin_delete_model_pricing(
            model_name: str,
            authorization: str | None = Header(default=None),
            mcpcodeaway_admin_session: str | None = Cookie(default=None),
        ) -> dict[str, Any]:
            await _authorize_control_plane(
                authorization, mcpcodeaway_admin_session, require_admin_role=True
            )
            deleted = False
            if hasattr(runtime.store, "delete_model_pricing"):
                deleted = await runtime.store.delete_model_pricing(model_name)
            return {"deleted": deleted, "models": await runtime.merged_model_pricing()}

        @app.post("/admin/routing/models")
        async def admin_save_model_routing_score(
            body: ModelRoutingScoreBody,
            authorization: str | None = Header(default=None),
            mcpcodeaway_admin_session: str | None = Cookie(default=None),
        ) -> dict[str, Any]:
            await _authorize_control_plane(
                authorization, mcpcodeaway_admin_session, require_admin_role=True
            )
            if body.preferred_mode is not None and body.preferred_mode not in {
                "classic",
                "codemode",
                "auto",
            }:
                raise HTTPException(
                    status_code=400,
                    detail="preferred_mode must be 'classic', 'codemode', or 'auto'",
                )
            saved: dict[str, Any] = {}
            if hasattr(runtime.store, "get_setting"):
                saved = dict(await runtime.store.get_setting("model_routing_scores") or {})
            saved[body.model_name.strip()] = {
                "score_delta": body.score_delta,
                "preferred_mode": (
                    None if body.preferred_mode in {None, "auto"} else body.preferred_mode
                ),
                "notes": body.notes.strip(),
            }
            if hasattr(runtime.store, "save_setting"):
                await runtime.store.save_setting("model_routing_scores", saved)
            return {"saved": True, "admin_overrides": saved}

        @app.delete("/admin/routing/models/{model_name:path}")
        async def admin_delete_model_routing_score(
            model_name: str,
            authorization: str | None = Header(default=None),
            mcpcodeaway_admin_session: str | None = Cookie(default=None),
        ) -> dict[str, Any]:
            await _authorize_control_plane(
                authorization, mcpcodeaway_admin_session, require_admin_role=True
            )
            saved: dict[str, Any] = {}
            if hasattr(runtime.store, "get_setting"):
                saved = dict(await runtime.store.get_setting("model_routing_scores") or {})
            deleted = saved.pop(model_name, None) is not None
            if hasattr(runtime.store, "save_setting"):
                await runtime.store.save_setting("model_routing_scores", saved)
            return {"deleted": deleted, "admin_overrides": saved}

        @app.post("/admin/demo/traffic/start")
        async def admin_demo_traffic_start(
            tier: DemoTier = "enterprise",
            authorization: str | None = Header(default=None),
            mcpcodeaway_admin_session: str | None = Cookie(default=None),
        ) -> dict[str, Any]:
            await _authorize_control_plane(
                authorization, mcpcodeaway_admin_session, require_admin_role=True
            )
            _ensure_demo_traffic_allowed()
            return await runtime.demo_traffic.start(tier=tier)

        @app.post("/admin/demo/traffic/tier")
        async def admin_demo_traffic_tier(
            tier: DemoTier = "enterprise",
            authorization: str | None = Header(default=None),
            mcpcodeaway_admin_session: str | None = Cookie(default=None),
        ) -> dict[str, Any]:
            await _authorize_control_plane(
                authorization, mcpcodeaway_admin_session, require_admin_role=True
            )
            _ensure_demo_traffic_allowed()
            return await runtime.demo_traffic.set_tier(tier=tier)

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
            origin = request.headers.get("origin")
            if origin is not None and origin not in runtime.settings.cors_allowed_origins:
                return JSONResponse(
                    {"detail": "Origin is not allowed for the MCP endpoint."}, status_code=403
                )
            caller = await _authorize_control_plane(
                authorization, mcpcodeaway_admin_session, require_admin_role=True
            )
            payload = await request.json()
            protocol_error = _validate_modern_mcp_request(request, payload)
            if protocol_error is not None:
                return protocol_error
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
