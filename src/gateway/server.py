"""Stateless Streamable HTTP Code-Mode MCP Gateway FastAPI application."""

from __future__ import annotations

import asyncio
import base64
import binascii
import copy
import hashlib
import hmac
import json
import logging
import math
import os
import re
import secrets
import time
import uuid
from collections import deque
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from enum import StrEnum
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
from pydantic import BaseModel, ConfigDict, Field

from gateway import __version__
from gateway.ab_testing import (
    OnboardingAbTestRequest,
    _run_matches_tool,
    evaluate_mode_algorithm,
    resolve_ab_experiment_variant,
    run_onboarding_ab_test,
)
from gateway.adaptive_execution import (
    EVIDENCE_VERSION,
    MAX_IMPORT_CLOCK_SKEW,
    WORKLOADS,
    infer_workload,
    resolve_adaptive_recommendation,
    summarize_pairs,
)
from gateway.admin_auth import AdminSessionManager, create_admin_auth_router
from gateway.admin_mcp import handle_admin_mcp_request
from gateway.alerts import AlertEvaluator, AlertRulesConfig
from gateway.auth import (
    DEV_PERSONAS,
    AuthError,
    CallerIdentity,
    ClientCredentialsProvider,
    DelegatedTokenExchanger,
    EntraGroupMembershipResolver,
    LocalDevKeyManager,
    OidcTokenValidator,
    PolicyEvaluator,
    UserCredentialRequiredError,
)
from gateway.business_value_runtime import (
    apply_regional_baseline,
    build_workflow_call_key,
    canonical_json_digest,
    encode_jsonrpc_request_id,
    extract_caller_regions,
    required_evidence_satisfied,
    resolve_json_pointer,
    result_matches_predicate,
    terminal_completion_satisfied,
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
    ToolApprovalPolicy,
    UpstreamConfig,
    UpstreamRegistry,
)
from gateway.crypto import EnvelopeCipher
from gateway.demo_traffic import DemoModelProfile, DemoTier, DemoTrafficRunner
from gateway.discovery import DiscoveryEngine
from gateway.dispatcher import (
    ExecutionBudget,
    HostDispatcher,
    OutputRedactor,
    SubcallRecord,
    bind_positional_arguments,
    build_provenance_envelope,
    summarize_value_shape,
)
from gateway.integration_definition import IntegrationDefinition, compute_workflow_fingerprint
from gateway.integration_validation import (
    ATTRIBUTED_TOOL_PREFIX,
    START_BUSINESS_EXECUTION_TOOL,
    validate_attributed_arguments,
    validate_definition_against_catalog,
)
from gateway.mcp_app_renderer import (
    MCP_APP_EXTENSION,
    MCP_APP_MIME_TYPE,
    describe_mcp_app_resource,
    render_mcp_app_resource,
    validate_experience,
)
from gateway.mcp_protocol import (
    MCP_CLIENT_CAPABILITIES_META,
    MCP_PROTOCOL_VERSION,
    MCP_PROTOCOL_VERSION_META,
    MCP_SERVER_INFO_META,
    MCP_TASKS_EXTENSION,
)
from gateway.monitoring import UpstreamHealthMonitor
from gateway.oauth_discovery import create_oauth_discovery_router
from gateway.observability import (
    MetricsRegistry,
    clear_request_context,
    configure_logging,
    log_event,
    set_request_context,
    update_request_context,
)
from gateway.onboarding_prompts import (
    OnboardingBundle,
    build_onboarding_prompt_pack,
    validate_onboarding_bundle_payload,
)
from gateway.recovery import OperationRecovery
from gateway.runtime_stats import ProcessStatsCollector, collect_runtime_telemetry
from gateway.sandbox import (
    LocalMontyBackend,
    RemoteMontyWorkerBackend,
    SandboxBackend,
    SandboxResult,
)
from gateway.secrets import CredentialVault, SecretResolver
from gateway.state import (
    AuditStore,
    BusinessExecutionConflict,
    CacheBackend,
    InMemoryCacheBackend,
    PostgresAuditStore,
    RedisCacheBackend,
    SQLiteAuditStore,
)
from gateway.telemetry import TelemetryRecorder, configure_telemetry
from gateway.upstream_clients import (
    DiscoveredTool,
    ToolGuidance,
    UpstreamClientManager,
    build_agent_onboarding_prompt,
    compute_schema_digest,
    inspect_upstream,
    validate_upstream_url,
)
from gateway.user_connect import create_user_connect_router
from gateway.workflow_detector import (
    WorkflowEpisodeTracker,
    mine_workflows_from_runs,
    pick_schema_predicate,
    synthesize_workflows_from_catalog,
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


class GatewayLifecycleState(StrEnum):
    """Observable lifecycle states for one assembled gateway runtime."""

    NEW = "new"
    STARTING = "starting"
    READY = "ready"
    STOPPING = "stopping"
    STOPPED = "stopped"
    FAILED = "failed"


class IntegrationDefinitionDraftRequest(BaseModel):
    """Strict request envelope for saving an authored manifest draft."""

    model_config = ConfigDict(extra="forbid")

    definition: IntegrationDefinition


class IntegrationDefinitionRevisionRequest(BaseModel):
    """Strict request envelope for publishing or rolling back to one revision."""

    model_config = ConfigDict(extra="forbid")

    revision: str = Field(min_length=1, max_length=40)


_GATEWAY_LIFECYCLE_TRANSITIONS: dict[GatewayLifecycleState, frozenset[GatewayLifecycleState]] = {
    GatewayLifecycleState.NEW: frozenset(
        {GatewayLifecycleState.STARTING, GatewayLifecycleState.STOPPING}
    ),
    GatewayLifecycleState.STARTING: frozenset(
        {GatewayLifecycleState.READY, GatewayLifecycleState.FAILED}
    ),
    GatewayLifecycleState.READY: frozenset(
        {GatewayLifecycleState.STOPPING, GatewayLifecycleState.FAILED}
    ),
    GatewayLifecycleState.STOPPING: frozenset(
        {GatewayLifecycleState.STOPPED, GatewayLifecycleState.FAILED}
    ),
    GatewayLifecycleState.FAILED: frozenset({GatewayLifecycleState.STOPPING}),
    GatewayLifecycleState.STOPPED: frozenset(),
}


def _ensure_local_demo_recovery_secrets(
    settings: GatewaySettings, registry: UpstreamRegistry
) -> None:
    """Persist local keys for demo-only recovery without weakening customer upstreams."""
    if settings.app_env != "sandbox" or settings.effective_profile_name != "demo":
        return
    recovery_upstreams = [
        upstream
        for upstream in registry.list_upstreams(include_disabled=True)
        if any(policy.enabled for policy in upstream.tool_recovery_policies.values())
    ]
    if not recovery_upstreams or any(not upstream.demo_only for upstream in recovery_upstreams):
        return
    if (
        settings.credential_encryption_key
        and len(settings.task_dedupe_secret.get_secret_value()) >= 32
    ):
        return

    database_path = Path(settings.sqlite_path)
    if str(database_path) == ":memory:":
        raise ValueError("Demo write recovery needs a persistent SQLite path for its local keys")
    key_path = database_path.parent / ".mcpcodeaway-demo-recovery-keys.json"
    key_path.parent.mkdir(parents=True, exist_ok=True)
    try:
        key_data = json.loads(key_path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        key_data = {
            "credential_encryption_key": secrets.token_urlsafe(32),
            "task_dedupe_secret": secrets.token_urlsafe(48),
        }
        try:
            descriptor = os.open(key_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        except FileExistsError:
            key_data = json.loads(key_path.read_text(encoding="utf-8"))
        else:
            with os.fdopen(descriptor, "w", encoding="utf-8") as secret_file:
                json.dump(key_data, secret_file)
                secret_file.flush()
                os.fsync(secret_file.fileno())
    if not isinstance(key_data, dict):
        raise ValueError("Local demo recovery key file is malformed")
    if key_path.stat().st_mode & 0o077:
        raise ValueError("Local demo recovery key file must be accessible only to its owner")
    if not settings.credential_encryption_key:
        settings.credential_encryption_key = str(key_data.get("credential_encryption_key") or "")
    if len(settings.task_dedupe_secret.get_secret_value()) < 32:
        from pydantic import SecretStr

        settings.task_dedupe_secret = SecretStr(str(key_data.get("task_dedupe_secret") or ""))


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


def _budget_upfront_listing(
    tools: list[DiscoveredTool], execution: ExecutionMode, budget: int
) -> tuple[list[dict[str, Any]] | None, int | None, str]:
    if not tools:
        return None, 0, "empty_catalog"
    if execution == "codemode" and len(tools) > 100:
        return None, None, "stub_cap"

    definitions: list[dict[str, Any]] = []
    stubs: list[str] = []
    size = 2
    if execution == "codemode":
        definition = dict(EXECUTE_CODE_DEF)
        definition["description"] += "\n\nAuthorized tool stubs:"
        size = len(json.dumps([definition], separators=(",", ":")))
    for tool in tools:
        if execution == "classic":
            definition = to_mcp_tool_definition(tool)
            size += len(json.dumps(definition, separators=(",", ":"))) + bool(definitions)
            definitions.append(definition)
        else:
            stub = ("\n\n" if stubs else "\n") + render_python_stub(tool)
            size += len(json.dumps(stub)) - 2
            stubs.append(stub)
        if size > budget * 4:
            return None, math.ceil(size / 4), "over_budget"
    if stubs:
        definition["description"] += "".join(stubs)
        definitions.append(definition)
    return definitions, math.ceil(size / 4), "fits_budget"


LIST_SERVERS_DEF: dict[str, Any] = {
    "name": "list_servers",
    "description": (
        "Browse authorized upstream MCP servers and OpenAPI services, including tool counts "
        "and sample tool names. Use search_tools to find tools for a task."
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
        "Start here to find tools for a task. Searches all authorized upstream MCP servers and "
        "OpenAPI services by default. Returns upstream names and typed Python stubs, with full "
        "JSON Schemas in Classic with deferred discovery."
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

GET_OPERATION_STATUS_DEF: dict[str, Any] = {
    "name": "get_operation_status",
    "description": (
        "Read the recovery status for an operation started by this authenticated caller. "
        "Use the operation_id returned after an ambiguous write; this does not execute or retry it."
    ),
    "inputSchema": {
        "type": "object",
        "properties": {"operation_id": {"type": "string", "minLength": 1, "maxLength": 64}},
        "required": ["operation_id"],
        "additionalProperties": False,
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
            "business_execution_id": {
                "type": "string",
                "minLength": 1,
                "maxLength": 128,
                "description": (
                    "Optional gateway-issued ID returned by "
                    "mcpcodeaway_start_business_execution. When supplied, all callbacks "
                    "must belong to that selected execution."
                ),
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
        shared_local_signing_key_enabled = (
            self.settings.dev_oidc_private_key_path is not None
            and self.settings.app_env != "prod"
            and self.settings.token_exchange_dialect.endswith("_local")
        )
        if self.dev_keys is None and (
            self.settings.auth_mode == "dev" or shared_local_signing_key_enabled
        ):
            private_key_pem = (
                Path(self.settings.dev_oidc_private_key_path).read_text(encoding="utf-8")
                if self.settings.dev_oidc_private_key_path
                else None
            )
            self.dev_keys = LocalDevKeyManager(
                issuer=self.settings.oidc_issuer,
                private_key_pem=private_key_pem,
            )
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
        _ensure_local_demo_recovery_secrets(self.settings, self.registry)
        self.client_manager = UpstreamClientManager(
            http_client=http_client,
            app_env=self.settings.app_env,
            block_private_ips_in_prod=self.settings.block_private_ips_in_prod,
            allowed_hosts=self.settings.allowed_upstream_hosts,
            ssrf_protection_enabled=self.settings.ssrf_protection_enabled,
        )
        if self.settings.entra_group_overage_resolution_enabled:
            self.validator.group_resolver = EntraGroupMembershipResolver(
                settings=self.settings,
                exchanger=self.exchanger,
                cache=self.cache,
                upstream_client=self.client_manager,
            )
        self.upstream_health = UpstreamHealthMonitor(self.client_manager)
        self.process_stats = ProcessStatsCollector()
        self.admin_sessions = AdminSessionManager(self.settings)
        self.cipher = EnvelopeCipher(
            self.settings.credential_encryption_key,
            allow_ephemeral=self.settings.app_env == "sandbox",
        )
        self.secret_resolver = SecretResolver(
            settings=self.settings,
            store=self.store,
            cipher=self.cipher,
            http_client=http_client,
        )
        self.credential_vault = CredentialVault(store=self.store, cipher=self.cipher)
        self.client_credentials = ClientCredentialsProvider(
            settings=self.settings,
            cache=self.cache,
            http_client=http_client,
        )
        self._connect_signing_key = (
            self.cipher.derive_subkey("connect_ticket")
            if self.cipher.is_configured
            else hashlib.sha256(
                (self.settings.admin_session_secret or "mcpcodeaway-dev-ticket").encode("utf-8")
            ).digest()
        )
        self.discovery_samples: deque[dict[str, Any]] = deque(maxlen=1000)
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
            registry=self.registry,
            metrics=self.metrics,
            secret_resolver=self.secret_resolver,
            credential_vault=self.credential_vault,
            client_credentials=self.client_credentials,
            connect_signing_key=self._connect_signing_key,
            http_client=http_client,
            audit_store=self.store,
        )
        self.dispatcher.health_monitor = self.upstream_health
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
        self._upstream_health_task: asyncio.Task[None] | None = None
        self._recovery_worker_task: asyncio.Task[None] | None = None
        self._demo_auto_golden_task: asyncio.Task[None] | None = None
        self.recovery = OperationRecovery(self)
        self.lifecycle_state = GatewayLifecycleState.NEW
        self._postgres_readiness: bool | None = None
        self._lifecycle_lock = asyncio.Lock()
        self.demo_traffic = DemoTrafficRunner(self)
        self.alert_evaluator = AlertEvaluator()
        self.workflow_detector = WorkflowEpisodeTracker(
            self.settings,
            self.store,
            self.catalog,
            http_client=http_client,
            registry=self.registry,
        )

    def _transition_lifecycle(self, next_state: GatewayLifecycleState) -> None:
        """Apply one legal lifecycle transition, rejecting impossible runtime states."""
        if next_state not in _GATEWAY_LIFECYCLE_TRANSITIONS[self.lifecycle_state]:
            raise RuntimeError(
                f"Invalid gateway lifecycle transition: {self.lifecycle_state} -> {next_state}"
            )
        self.lifecycle_state = next_state

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
        discovery_override: bool = False,
        model_name: str | None = None,
        target_tool: str | None = None,
        target_tools: list[str] | None = None,
        workload_profile: str | None = None,
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
        workload = (
            workload_profile if workload_profile in WORKLOADS else infer_workload(intent, tools)
        )
        inferred_tool = target_tool
        if not inferred_tool and intent:
            intent_low = intent.lower()
            for t in tools:
                if t.qualified_name.lower() in intent_low or t.name.lower() in intent_low:
                    inferred_tool = t.qualified_name
                    break

        if requested_exec == "auto" and inferred_tool and not explicit_override:
            server_name = inferred_tool.split("__", 1)[0]
            preference_upstreams = [
                upstream for upstream in accessible_srvs if upstream.name == server_name
            ]
        elif requested_exec == "auto" and not explicit_override:
            preference_upstreams = accessible_srvs
        else:
            preference_upstreams = []

        preferred_modes = {upstream.preferred_mode for upstream in preference_upstreams}
        preferred_mode = next(iter(preferred_modes)) if len(preferred_modes) == 1 else None
        if preferred_mode in _MODE_TO_AXES and preferred_mode != "auto":
            if all(upstream.preferred_mode == preferred_mode for upstream in preference_upstreams):
                server_name = (
                    inferred_tool.split("__", 1)[0]
                    if inferred_tool
                    else ",".join(upstream.name for upstream in preference_upstreams)
                )
                preferred_exec, preferred_disc = _MODE_TO_AXES[preferred_mode]
                effective_disc = requested_disc if discovery_override else preferred_disc
                effective_mode = _axis_to_gateway_mode(preferred_exec, effective_disc)
                return (
                    preferred_exec,
                    effective_disc,
                    effective_mode,
                    f"upstream_preference:{server_name}:{preferred_mode}",
                    None,
                    None,
                    None,
                )

        recent = await self.store.list_runs(limit=50, tenant_id=identity.tenant_id)
        if requested_exec == "auto" and not explicit_override:
            targeted_workflow = [
                tool for tool in tools if target_tools and tool.qualified_name in target_tools
            ]
            target_effects_read_only = True
            for tool in targeted_workflow:
                tool_upstream = next(
                    (srv for srv in accessible_srvs if srv.name == tool.server), None
                )
                if (
                    tool_upstream is None
                    or tool.mutating
                    or tool_upstream.tool_effects.get(tool.name, "read") != "read"
                ):
                    target_effects_read_only = False
                    break
            targeted_workflow_is_read_only = bool(target_tools) and (
                len(targeted_workflow) == len(set(target_tools)) and target_effects_read_only
            )
            for upstream in accessible_srvs:
                if inferred_tool and not inferred_tool.startswith(f"{upstream.name}__"):
                    continue
                experiment = upstream.ab_experiment
                experiment_tools = [tool for tool in tools if tool.server == upstream.name]
                if inferred_tool:
                    targeted_tool = next(
                        (tool for tool in experiment_tools if tool.qualified_name == inferred_tool),
                        None,
                    )
                    target_name = inferred_tool.split("__", 1)[-1]
                    target_effect = upstream.tool_effects.get(
                        target_name,
                        "write" if target_name in upstream.mutating_tools else "read",
                    )
                    target_is_mutating = (
                        targeted_tool.mutating
                        if targeted_tool is not None
                        else target_name in upstream.mutating_tools
                    )
                    experiment_tools = [targeted_tool] if targeted_tool is not None else []
                    target_is_read_only = target_effect == "read" and not target_is_mutating
                else:
                    target_is_read_only = False
                if (
                    experiment is not None
                    and experiment.read_only_only
                    and (
                        not targeted_workflow_is_read_only
                        if target_tools is not None
                        else (
                            not target_is_read_only
                            if inferred_tool
                            else any(
                                tool.mutating
                                or upstream.tool_effects.get(tool.name) in {"write", "high_impact"}
                                for tool in experiment_tools
                            )
                        )
                    )
                ):
                    continue
                if target_tools is not None and not any(
                    tool.qualified_name in target_tools for tool in experiment_tools
                ):
                    continue
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
                    var_exec = _MODE_TO_AXES[var_mode][0]
                    effective_mode = _axis_to_gateway_mode(var_exec, requested_disc)
                    return (
                        var_exec,
                        requested_disc,
                        effective_mode,
                        reason,
                        None,
                        exp_id,
                        variant_lbl,
                    )

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
        adaptive_observation_note: str | None = None
        if requested_exec == "auto":
            if not model_name:
                adaptive_observation_note = (
                    "adaptive_unavailable:unknown_model; provide a model name for evidence routing"
                )
            elif not workload:
                adaptive_observation_note = (
                    "adaptive_unavailable:unknown_workload; "
                    "request intent did not identify a supported read-only workload"
                )
            elif not tools:
                adaptive_observation_note = (
                    "adaptive_unavailable:no_accessible_tools; normal Auto routing applies"
                )
            elif any(t.mutating for t in tools):
                adaptive_observation_note = (
                    "adaptive_unavailable:mutating_tools; "
                    "adaptive evidence routing is limited to read-only tools"
                )
        if (
            requested_exec == "auto"
            and workload
            and model_name
            and tools
            and not any(t.mutating for t in tools)
        ):
            tenant_key = f"adaptive_execution:{identity.tenant_id or ''}"
            adaptive_setting = (
                await self.store.get_setting(tenant_key)
                if hasattr(self.store, "get_setting")
                else None
            )
            adaptive_mode = (
                adaptive_setting.get("mode", "observe")
                if isinstance(adaptive_setting, dict)
                else "observe"
            )
            evidence = await self.store.list_adaptive_evidence(identity.tenant_id or "")
            pricing = await self.merged_model_pricing()
            route_decision = resolve_adaptive_recommendation(
                evidence, model_name, workload, pricing=pricing, days=30
            )
            winner = route_decision["recommended_mode"]
            if winner:
                tier = route_decision["evidence_tier"]
                adaptive_observation_note = (
                    f"adaptive_recommendation:{tier}:{workload}:{winner}; "
                    f"{route_decision['reason']} ({route_decision['pairs']} matched pairs)"
                )
                if adaptive_mode == "active":
                    routed: GatewayMode = "codemode" if winner == "codemode" else "traditional"
                    routed_exec = "codemode" if winner == "codemode" else "classic"
                    routed_disc = requested_disc
                    if routed_disc not in {"upfront", "deferred", "x_intent"}:
                        routed_disc = _MODE_TO_AXES[routed][1]
                    return (
                        routed_exec,
                        routed_disc,
                        routed,
                        f"adaptive_{tier}:{workload}:{winner}; "
                        f"{route_decision['reason']} ({route_decision['pairs']} matched pairs)",
                        None,
                        None,
                        None,
                    )
            else:
                adaptive_observation_note = (
                    f"adaptive_unavailable:{route_decision['status']}; "
                    f"{route_decision['reason']} ({route_decision['pairs']} exact matched pairs)"
                )

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
        factor_details = "; ".join(
            f"{factor.name} ({factor.delta:+.1f}): {factor.detail}" for factor in decision.factors
        )
        decision_reason = decision.reason
        if factor_details:
            decision_reason = f"{decision_reason}; Factors: {factor_details}"
        return (
            decision.execution,
            decision.discovery_mode,
            decision.gateway_mode,
            f"{decision_reason}; {adaptive_observation_note}"
            if adaptive_observation_note
            else decision_reason,
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
        async with self._lifecycle_lock:
            await self._startup()

    async def _startup(self) -> None:
        if self.lifecycle_state is GatewayLifecycleState.READY:
            return
        self._transition_lifecycle(GatewayLifecycleState.STARTING)
        try:
            await self.store.init()
            if hasattr(self.store, "get_setting"):
                saved_rules = await self.store.get_setting("alert_rules")
                if saved_rules:
                    self.alert_evaluator.rules = AlertRulesConfig.model_validate(saved_rules)
            await self.refresh_catalog()
            if (
                any(
                    policy.enabled
                    for upstream in self.registry.list_upstreams()
                    for policy in upstream.tool_recovery_policies.values()
                )
                and len(self.settings.task_dedupe_secret.get_secret_value()) < 32
            ):
                raise ValueError(
                    "GATEWAY_TASK_DEDUPE_SECRET must be configured before enabling write recovery"
                )
            if self.startup_warnings:
                self._startup_retry_task = asyncio.create_task(
                    self._retry_failed_startup_upstreams()
                )
            self._retention_task = asyncio.create_task(self._retention_loop())
            self._upstream_health_task = asyncio.create_task(
                self.upstream_health.run(self.registry.list_upstreams)
            )
            self._recovery_worker_task = asyncio.create_task(self.recovery.run_worker())
            await self.workflow_detector.start()
            if self.settings.effective_profile_name == "demo":
                await self.demo_traffic.ensure_demo_solutions()
                if self.settings.demo_auto_golden:
                    existing_runs = await self.store.list_runs(limit=1)
                    if not existing_runs:
                        self._demo_auto_golden_task = asyncio.create_task(
                            self._run_demo_auto_golden()
                        )
        except BaseException as exc:
            if isinstance(self.store, PostgresAuditStore) and isinstance(exc, Exception):
                log_event(
                    "database.postgres_startup_failed",
                    level=logging.ERROR,
                    dependency="postgres",
                    error_type=type(exc).__name__,
                )
            self._transition_lifecycle(GatewayLifecycleState.FAILED)
            raise
        self._transition_lifecycle(GatewayLifecycleState.READY)

    async def get_reporting_currency(self) -> str:
        """Return gateway-wide ISO currency code, preferring persisted control-plane setting."""
        fallback = str(self.settings.default_currency or "USD").strip().upper()
        if hasattr(self.store, "get_setting"):
            saved = await self.store.get_setting("business_value_settings")
            if isinstance(saved, dict):
                raw = str(saved.get("reporting_currency") or "").strip().upper()
                if re.match(r"^[A-Z]{3}$", raw):
                    return raw
        return fallback

    async def _run_demo_auto_golden(self) -> None:
        try:
            await self.demo_traffic.golden_run()
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            log_event(
                "demo.auto_golden_failed",
                level=logging.WARNING,
                error_type=type(exc).__name__,
                detail=str(exc),
            )

    async def is_ready(self) -> bool:
        """Check lifecycle state and required shared dependencies for readiness probes."""
        if self.lifecycle_state is not GatewayLifecycleState.READY:
            return False
        if isinstance(self.cache, RedisCacheBackend):
            try:
                if not await asyncio.wait_for(self.cache.health_check(), timeout=1.0):
                    return False
            except Exception:
                return False
        if isinstance(self.store, PostgresAuditStore):
            error_type: str | None = None
            try:
                postgres_ready = await asyncio.wait_for(self.store.health_check(), timeout=1.0)
            except Exception as exc:
                postgres_ready = False
                error_type = type(exc).__name__
            if self._postgres_readiness is None or self._postgres_readiness != postgres_ready:
                self._postgres_readiness = postgres_ready
                log_event(
                    "database.postgres_readiness_changed",
                    level=logging.INFO if postgres_ready else logging.ERROR,
                    dependency="postgres",
                    status="available" if postgres_ready else "unavailable",
                    error_type=error_type,
                )
            if not postgres_ready:
                return False
        return True

    async def shutdown(self) -> None:
        async with self._lifecycle_lock:
            await self._shutdown()

    async def _shutdown(self) -> None:
        if self.lifecycle_state is GatewayLifecycleState.STOPPED:
            return
        self._transition_lifecycle(GatewayLifecycleState.STOPPING)
        try:
            await self.workflow_detector.stop()
            if self._demo_auto_golden_task is not None and not self._demo_auto_golden_task.done():
                self._demo_auto_golden_task.cancel()
                await asyncio.gather(self._demo_auto_golden_task, return_exceptions=True)
            self._demo_auto_golden_task = None
            await self.demo_traffic.stop()
            if self._retention_task is not None and not self._retention_task.done():
                self._retention_task.cancel()
                await asyncio.gather(self._retention_task, return_exceptions=True)
            self._retention_task = None
            if self._upstream_health_task is not None and not self._upstream_health_task.done():
                self._upstream_health_task.cancel()
                await asyncio.gather(self._upstream_health_task, return_exceptions=True)
            self._upstream_health_task = None
            if self._startup_retry_task is not None and not self._startup_retry_task.done():
                self._startup_retry_task.cancel()
                await asyncio.gather(self._startup_retry_task, return_exceptions=True)
            self._startup_retry_task = None
            if self._recovery_worker_task is not None and not self._recovery_worker_task.done():
                self._recovery_worker_task.cancel()
                await asyncio.gather(self._recovery_worker_task, return_exceptions=True)
            self._recovery_worker_task = None
            await self.sandbox.close()
            await self.client_manager.close()
            if hasattr(self.cache, "close"):
                await self.cache.close()  # type: ignore[attr-defined]
            if hasattr(self.store, "close"):
                await self.store.close()  # type: ignore[attr-defined]
        except BaseException:
            self._transition_lifecycle(GatewayLifecycleState.FAILED)
            raise
        self._transition_lifecycle(GatewayLifecycleState.STOPPED)

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
            "mode_decision_score": run.get("mode_decision_score"),
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

    async def business_value_ui_map_for_request(
        self,
        identity: CallerIdentity,
        *,
        allowed_servers: set[str] | None = None,
    ) -> dict[str, str]:
        """Return unambiguous MCP App resourceUris keyed by caller-accessible tool name."""

        if not hasattr(self.store, "list_current_published_integration_definitions"):
            return {}
        await self.ensure_catalog_for_identity(identity)
        rows = await self.store.list_current_published_integration_definitions(
            identity.tenant_id or ""
        )
        accessible = self.catalog.list_accessible_tools(
            identity,
            self.policy,
            self.registry,
            allowed_servers=allowed_servers,
        )
        accessible_by_name = {tool.qualified_name: tool for tool in accessible}
        catalog_names = {
            tool.qualified_name
            for server_name in self.registry.list_upstreams()
            for tool in self.catalog.get_server_tools(server_name.name)
        }
        resource_by_tool: dict[str, str] = {}
        ambiguous_experience_tools: set[str] = set()
        for row in rows:
            if allowed_servers is not None and row["upstream_name"] not in allowed_servers:
                continue
            try:
                manifest = IntegrationDefinition.model_validate(row["definition"])
                validate_definition_against_catalog(
                    manifest,
                    self.catalog,
                    reserved_tool_names=catalog_names,
                )
            except (ValueError, KeyError):
                continue
            for experience in manifest.experiences:
                experience_tool = accessible_by_name.get(experience.tool)
                if experience_tool is None:
                    continue
                try:
                    validate_experience(
                        experience.model_dump(mode="json"),
                        tool_input_schema=experience_tool.input_schema,
                    )
                except ValueError:
                    continue
                if experience.tool in resource_by_tool:
                    ambiguous_experience_tools.add(experience.tool)
                else:
                    resource_by_tool[experience.tool] = experience.resource_uri
        for ambiguous_tool in ambiguous_experience_tools:
            resource_by_tool.pop(ambiguous_tool, None)
        return resource_by_tool

    async def business_value_tools_for_request(
        self,
        identity: CallerIdentity,
        *,
        allowed_servers: set[str] | None = None,
    ) -> tuple[dict[str, Any] | None, list[dict[str, Any]]]:
        """Wrapper tools are retired; passive attribution tracks standard tools directly."""
        _ = (identity, allowed_servers)
        return None, []

    async def business_value_resources_for_request(
        self,
        identity: CallerIdentity,
        *,
        allowed_servers: set[str] | None = None,
    ) -> dict[str, dict[str, Any]]:
        """Return only unambiguous, caller-accessible published App resources."""
        ui_map = await self.business_value_ui_map_for_request(
            identity,
            allowed_servers=allowed_servers,
        )
        linked_uris = set(ui_map.values())
        if not linked_uris:
            return {}
        rows = await self.store.list_current_published_integration_definitions(
            identity.tenant_id or ""
        )
        resources: dict[str, dict[str, Any]] = {}
        ambiguous: set[str] = set()
        for row in rows:
            if allowed_servers is not None and row["upstream_name"] not in allowed_servers:
                continue
            try:
                manifest = IntegrationDefinition.model_validate(row["definition"])
                validate_definition_against_catalog(manifest, self.catalog)
            except (ValueError, KeyError):
                continue
            for experience in manifest.experiences:
                if experience.resource_uri not in linked_uris:
                    continue
                tool = self.catalog.get_tool(experience.tool)
                if tool is None:
                    continue
                try:
                    experience_data = experience.model_dump(mode="json")
                    content = render_mcp_app_resource(
                        experience_data,
                        tool_input_schema=tool.input_schema,
                    )
                    descriptor = describe_mcp_app_resource(
                        experience_data,
                        tool_input_schema=tool.input_schema,
                    )
                except ValueError:
                    continue
                if experience.resource_uri in resources:
                    ambiguous.add(experience.resource_uri)
                    continue
                resources[experience.resource_uri] = {
                    "descriptor": descriptor,
                    "content": content,
                }
        for resource_uri in ambiguous:
            resources.pop(resource_uri, None)
        return resources

    async def start_business_execution_for_identity(
        self,
        identity: CallerIdentity,
        selection: dict[str, Any],
        *,
        request_id: str | int,
        request_payload: dict[str, Any],
        allowed_servers: set[str] | None,
    ) -> dict[str, Any]:
        """Create or replay an owner-bound execution pinned to a published definition."""

        if not hasattr(self.store, "list_current_published_integration_definitions"):
            raise ValueError("no eligible published business-value definition is available")
        kind = selection.get("kind")
        if kind not in {"workflow", "standalone_tool"}:
            raise ValueError("kind must be workflow or standalone_tool")
        selected_id = (
            selection.get("workflow_id") if kind == "workflow" else selection.get("tool_value_id")
        )
        if not isinstance(selected_id, str):
            raise ValueError("select exactly one published workflow or standalone tool value")
        found: tuple[dict[str, Any], dict[str, Any]] | None = None
        expected_prefix = "workflow_id" if kind == "workflow" else "tool_value_id"
        if expected_prefix not in selection or len(selection) != 2:
            raise ValueError("start request must contain only kind and its matching definition ID")
        await self.ensure_catalog_for_identity(identity)
        accessible_names = {
            tool.qualified_name
            for tool in self.catalog.list_accessible_tools(
                identity, self.policy, self.registry, allowed_servers=allowed_servers
            )
        }
        for row in await self.store.list_current_published_integration_definitions(
            identity.tenant_id or ""
        ):
            if allowed_servers is not None and row["upstream_name"] not in allowed_servers:
                continue
            manifest_data = row["definition"]
            item_key = "workflows" if kind == "workflow" else "standalone_tool_values"
            for item in manifest_data.get(item_key, []):
                if kind == "workflow" and item.get("enabled") is False:
                    continue
                qualified_id = f"{row['upstream_name']}/{item['id']}"
                if qualified_id != selected_id:
                    continue
                required_tools = (
                    set(item.get("participating_tools", []))
                    if kind == "workflow"
                    else {item.get("tool")}
                )
                if required_tools <= accessible_names:
                    found = row, item
                break
            if found is not None:
                break
        if found is None:
            raise ValueError("selected business-value definition is unavailable")
        row, selected_definition = found
        manifest = IntegrationDefinition.model_validate(row["definition"])
        validate_definition_against_catalog(manifest, self.catalog)
        if kind == "workflow":
            ttl_minutes = selected_definition.get("execution", {}).get("absolute_ttl_minutes", 1440)
        else:
            ttl_minutes = 1440
        now = datetime.now(UTC)
        expires_at = now + timedelta(minutes=int(ttl_minutes))
        execution_id = uuid.uuid4().hex
        caller_regions = extract_caller_regions(
            claims=identity.claims, client_groups=identity.client_groups
        )
        baseline_snapshot = apply_regional_baseline(
            selected_definition["business_value"], caller_regions
        )
        metadata = {
            "pinned_definition": row["definition"],
            "selected_id": selected_id,
            "selected_kind": kind,
            "selected_definition": selected_definition,
            "baseline_snapshot": baseline_snapshot,
            "definition_digest": row["definition_digest"],
        }
        saved = await self.store.create_business_execution(
            {
                "business_execution_id": execution_id,
                "tenant_id": identity.tenant_id or "",
                "caller_sub": identity.sub,
                "client_id": identity.client_id or "",
                "kind": "standalone_tool" if kind == "standalone_tool" else "workflow",
                "upstream_name": row["upstream_name"],
                "definition_revision": row["revision"],
                "status": "active",
                "created_at": now.isoformat(),
                "expires_at": expires_at.isoformat(),
                "start_request_id": encode_jsonrpc_request_id(request_id),
                "start_request_digest": canonical_json_digest(request_payload),
                "metadata": metadata,
            }
        )
        await self.store.append_workflow_execution_event(
            {
                "event_id": uuid.uuid4().hex,
                "tenant_id": identity.tenant_id or "",
                "business_execution_id": saved["business_execution_id"],
                "idempotency_key": "execution-started-v1",
                "event_type": "started",
                "created_at": saved["created_at"],
                "evidence": {
                    "selected_id": selected_id,
                    "selected_kind": kind,
                    "definition_revision": saved["definition_revision"],
                },
            }
        )
        return {
            "ok": True,
            "business_execution_id": saved["business_execution_id"],
            "kind": saved["kind"],
            "selected_id": selected_id,
            "definition_revision": saved["definition_revision"],
            "expires_at": saved["expires_at"],
        }

    async def resolve_business_execution_for_tool(
        self,
        identity: CallerIdentity,
        business_execution_id: str,
        qualified_tool_name: str | None,
    ) -> tuple[dict[str, Any], dict[str, Any]]:
        """Validate owner, expiry, pinned schema and participation before attribution."""

        row = await self.store.get_business_execution(
            business_execution_id,
            identity.tenant_id or "",
            identity.sub,
            identity.client_id or "",
        )
        if row is None:
            raise PermissionError("business execution is invalid or not owned by this caller")
        if row["status"] != "active" or datetime.fromisoformat(row["expires_at"]) <= datetime.now(
            UTC
        ):
            raise PermissionError("business execution is expired or closed")
        metadata = row["metadata"]
        selected = metadata["selected_definition"]
        if row["kind"] == "workflow":
            participating = set(selected["participating_tools"])
        else:
            participating = {selected["tool"]}
        if qualified_tool_name is not None and qualified_tool_name not in participating:
            raise PermissionError(
                f"tool {qualified_tool_name} is not part of the selected business execution"
            )
        manifest = IntegrationDefinition.model_validate(metadata["pinned_definition"])
        validate_definition_against_catalog(manifest, self.catalog)
        return row, selected

    async def persist_attributed_call_intent(
        self,
        identity: CallerIdentity,
        execution: dict[str, Any],
        selected: dict[str, Any],
        tool: DiscoveredTool,
        *,
        request_id: str | int,
        request_digest: str,
        arguments: dict[str, Any],
        callback_ordinal: int = 0,
    ) -> tuple[dict[str, Any], bool]:
        """Bind the request body and persist a call intent before any upstream dispatch."""

        request_key = encode_jsonrpc_request_id(request_id)
        tenant_id = identity.tenant_id or ""
        call_key = build_workflow_call_key(
            outer_request_id=request_id,
            request_digest=request_digest,
            upstream_name=tool.server,
            tool_name=tool.name,
            arguments=arguments,
            callback_ordinal=callback_ordinal,
            business_execution_id=execution["business_execution_id"],
            tenant_id=tenant_id,
            caller_sub=identity.sub,
            client_id=identity.client_id or "",
        )
        await self.store.bind_workflow_request(
            tenant_id,
            execution["business_execution_id"],
            request_key,
            request_digest,
        )
        call_id = uuid.uuid5(
            uuid.NAMESPACE_URL,
            f"mcpcodeaway:{tenant_id}:{execution['business_execution_id']}:{call_key}",
        ).hex
        evidence: dict[str, Any] = {}
        metadata = execution["metadata"]
        if execution["kind"] == "workflow":
            workflow = next(
                workflow
                for workflow in IntegrationDefinition.model_validate(
                    metadata["pinned_definition"]
                ).workflows
                if workflow.id == selected["id"]
            )
            requirement = next(
                (item for item in workflow.required_evidence if item.tool == tool.qualified_name),
                None,
            )
            if requirement is not None and requirement.result_when is not None:
                evidence["evidence_predicate_path"] = requirement.result_when.path
            if workflow.completion.tool == tool.qualified_name:
                evidence["completion_predicate_path"] = workflow.completion.success_when.path
        else:
            standalone = selected
            if standalone["completion"]["tool"] == tool.qualified_name:
                evidence["completion_predicate_path"] = standalone["completion"]["success_when"][
                    "path"
                ]
        call, created = await self.store.create_workflow_call(
            {
                "workflow_call_id": call_id,
                "tenant_id": tenant_id,
                "business_execution_id": execution["business_execution_id"],
                "call_key": call_key,
                "outer_request_id": request_key,
                "callback_ordinal": callback_ordinal,
                "upstream_name": tool.server,
                "tool_name": tool.name,
                "request_digest": request_digest,
                "arguments_digest": canonical_json_digest(arguments),
                "status": "started",
                "evidence": evidence,
            }
        )
        return call, created

    async def finalize_attributed_call(
        self,
        identity: CallerIdentity,
        execution: dict[str, Any],
        selected: dict[str, Any],
        tool: DiscoveredTool,
        call: dict[str, Any],
        *,
        result: Any,
        status: str,
        run_id: str | None = None,
        sensitive_write_operation_id: str | None = None,
        outcome_unknown: bool = False,
    ) -> dict[str, Any] | None:
        """Persist boolean predicate evidence and insert completion at most once."""

        metadata = execution["metadata"]
        manifest = IntegrationDefinition.model_validate(metadata["pinned_definition"])
        if execution["kind"] == "workflow":
            workflow = next(w for w in manifest.workflows if w.id == selected["id"])
            evidence_rule = next(
                (item for item in workflow.required_evidence if item.tool == tool.qualified_name),
                None,
            )
            evidence_predicate = evidence_rule.result_when if evidence_rule is not None else None
            completion_tool = workflow.completion.tool
            completion_predicate = workflow.completion.success_when
            journal_required = workflow.completion.require_sensitive_write_journal_status
            dedupe = workflow.dedupe
        else:
            from gateway.integration_definition import StandaloneToolValueDefinition

            standalone = StandaloneToolValueDefinition.model_validate(selected)
            evidence_predicate = None
            completion_tool = standalone.completion.tool
            completion_predicate = standalone.completion.success_when
            journal_required = standalone.completion.require_sensitive_write_journal_status
            dedupe = standalone.dedupe

        call_evidence: dict[str, Any] = {"outcome_unknown": outcome_unknown}
        if evidence_predicate is not None:
            call_evidence["evidence_predicate_matched"] = result_matches_predicate(
                result, evidence_predicate
            )
        if tool.qualified_name == completion_tool:
            matched = result_matches_predicate(result, completion_predicate)
            call_evidence["completion_predicate_matched"] = matched
            if journal_required == "succeeded":
                call_evidence["sensitive_write_journal_status"] = (
                    "succeeded"
                    if sensitive_write_operation_id and status == "succeeded"
                    else "unknown"
                )
            if matched and dedupe is not None:
                dedupe_data = (
                    dedupe.model_dump(mode="json") if hasattr(dedupe, "model_dump") else dedupe
                )
                values: dict[str, Any] = {}
                for part in dedupe_data["identity_parts"]:
                    found, value = resolve_json_pointer(result, part["path"])
                    if not found or value is None:
                        values = {}
                        break
                    values[part["path"]] = value
                if values:
                    call_evidence["outcome_namespace"] = dedupe_data["namespace"]
                    call_evidence["outcome_digest"] = canonical_json_digest(values)
        await self.store.update_workflow_call(
            call["workflow_call_id"],
            {
                "status": status,
                "run_id": run_id,
                "sensitive_write_operation_id": sensitive_write_operation_id,
                "evidence": call_evidence,
            },
        )
        if outcome_unknown:
            await self.store.update_business_execution_status(
                execution["business_execution_id"],
                identity.tenant_id or "",
                "outcome_unknown",
                datetime.now(UTC).isoformat(),
            )
        if status != "succeeded" or outcome_unknown:
            return None
        calls = await self.store.list_workflow_calls(
            identity.tenant_id or "", execution["business_execution_id"]
        )
        if execution["kind"] == "workflow":
            completion_satisfied = required_evidence_satisfied(
                workflow, calls
            ) and terminal_completion_satisfied(workflow, calls)
        else:
            completion_satisfied = any(
                item.get("tool_qualified_name") == completion_tool
                and item.get("status") == "succeeded"
                and item.get("evidence", {}).get("completion_predicate_matched") is True
                and (
                    journal_required is None
                    or item.get("evidence", {}).get("sensitive_write_journal_status") == "succeeded"
                )
                for item in calls
            )
        terminal_call = next(
            (
                item
                for item in calls
                if item.get("tool_qualified_name") == completion_tool
                and item.get("status") == "succeeded"
                and item.get("evidence", {}).get("completion_predicate_matched") is True
                and (
                    journal_required is None
                    or item.get("evidence", {}).get("sensitive_write_journal_status")
                    == journal_required
                )
            ),
            None,
        )
        if not completion_satisfied or terminal_call is None:
            return None
        terminal_evidence = terminal_call.get("evidence", {})
        if dedupe is not None and not terminal_evidence.get("outcome_digest"):
            return None
        baseline = metadata["baseline_snapshot"]
        event = {
            "event_id": uuid.uuid4().hex,
            "tenant_id": identity.tenant_id or "",
            "business_execution_id": execution["business_execution_id"],
            "completion_key": "business-value-completion-v1",
            "upstream_name": execution["upstream_name"],
            "outcome_namespace": terminal_evidence.get("outcome_namespace"),
            "outcome_digest": terminal_evidence.get("outcome_digest"),
            "credited": 1,
            "created_at": datetime.now(UTC).isoformat(),
            "evidence": {
                "definition_revision": execution["definition_revision"],
                "selected_id": metadata.get("selected_id"),
                "selected_kind": metadata.get("selected_kind"),
                "integration_id": metadata.get("pinned_definition", {})
                .get("integration", {})
                .get("id"),
                "integration_display_name": metadata.get("pinned_definition", {})
                .get("integration", {})
                .get("display_name"),
                "baseline_snapshot": baseline,
                "workflow_call_id": terminal_call["workflow_call_id"],
                "required_evidence_satisfied": True,
                "completion_predicate_matched": True,
                "sensitive_write_operation_id": terminal_call.get("sensitive_write_operation_id"),
            },
        }
        value_event, inserted = await self.store.record_business_value_event(event)
        await self.store.update_business_execution_status(
            execution["business_execution_id"],
            identity.tenant_id or "",
            "completed",
            datetime.now(UTC).isoformat(),
        )
        if inserted:
            await self.store.append_workflow_execution_event(
                {
                    "event_id": uuid.uuid4().hex,
                    "tenant_id": identity.tenant_id or "",
                    "business_execution_id": execution["business_execution_id"],
                    "idempotency_key": "business-value-completion-v1",
                    "event_type": "completed",
                    "created_at": datetime.now(UTC).isoformat(),
                    "evidence": event["evidence"],
                }
            )
        return value_event

    def tools_for_mode(
        self,
        identity: CallerIdentity,
        mode: GatewayMode,
        *,
        allowed_servers: set[str] | None = None,
        execution: ExecutionMode | None = None,
        discovery_mode: DiscoveryMode | None = None,
        guidance: dict[str, dict[str, Any]] | None = None,
    ) -> list[dict[str, Any]]:
        """Build the baseline MCP tools/list payload for Execution x Discovery."""
        accessible = self.catalog.list_accessible_tools(
            identity, self.policy, self.registry, allowed_servers=allowed_servers, guidance=guidance
        )
        base_exec, base_disc = _MODE_TO_AXES[mode]
        eff_exec = execution or base_exec
        eff_disc = discovery_mode or base_disc
        recovery_status_tool = (
            [GET_OPERATION_STATUS_DEF]
            if any(
                (upstream := self.registry.get(tool.server)) is not None
                and (policy := upstream.tool_recovery_policies.get(tool.name)) is not None
                and policy.enabled
                for tool in accessible
            )
            else []
        )

        if eff_exec == "classic":
            if eff_disc == "upfront":
                return [
                    *[to_mcp_tool_definition(tool) for tool in accessible],
                    *recovery_status_tool,
                ]
            return [LIST_SERVERS_DEF, SEARCH_TOOLS_DEF, *recovery_status_tool]

        if eff_exec == "codemode":
            if eff_disc == "upfront":
                return [_execute_code_with_stubs(accessible), *recovery_status_tool]
            return [LIST_SERVERS_DEF, SEARCH_TOOLS_DEF, EXECUTE_CODE_DEF, *recovery_status_tool]

        direct_mutating = [to_mcp_tool_definition(tool) for tool in accessible if tool.mutating]
        if eff_disc == "upfront":
            read_only = [tool for tool in accessible if not tool.mutating]
            return [
                _execute_code_with_stubs(read_only),
                *direct_mutating,
                *recovery_status_tool,
            ]
        return [
            LIST_SERVERS_DEF,
            SEARCH_TOOLS_DEF,
            EXECUTE_CODE_DEF,
            *direct_mutating,
            *recovery_status_tool,
        ]

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
        auto_discovery: bool = False,
        discovery_sample: dict[str, Any] | None = None,
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

        budget_eligible = (
            auto_discovery and eff_exec in {"classic", "codemode"} and not (intent or query)
        )
        guidance = (
            {}
            if eff_exec == "codemode"
            and eff_disc == "deferred"
            and not (intent or query)
            and not budget_eligible
            else await self.store.list_tool_guidance(identity.tenant_id or "")
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
                include_schemas=(eff_exec == "classic"),
                guidance=guidance,
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
                        identity,
                        self.policy,
                        self.registry,
                        allowed_servers=effective_ceiling,
                        guidance=guidance,
                    )
                    direct_mutating = [to_mcp_tool_definition(t) for t in accessible if t.mutating]
                    tools = (
                        [exec_copy, *direct_mutating]
                        if eff_disc == "upfront"
                        else [LIST_SERVERS_DEF, SEARCH_TOOLS_DEF, exec_copy, *direct_mutating]
                    )
        elif budget_eligible:
            discovery_started = time.perf_counter()
            accessible = self.catalog.list_accessible_tools(
                identity,
                self.policy,
                self.registry,
                allowed_servers=effective_ceiling,
                guidance=guidance,
            )
            upfront, tokens, reason = _budget_upfront_listing(
                accessible, eff_exec, self.settings.auto_discovery_token_budget
            )
            recommended = "upfront" if upfront is not None else "deferred"
            if self.settings.auto_discovery_policy == "active":
                eff_disc = recommended
            decision = {
                "execution": eff_exec,
                "applied_discovery": eff_disc,
                "recommended_discovery": recommended,
                "estimated_upfront_tokens": tokens,
                "estimate_is_lower_bound": reason == "over_budget",
                "decision_ms": round((time.perf_counter() - discovery_started) * 1000, 3),
                "reason": reason,
                "authorized_tools": len(accessible),
                "token_budget": self.settings.auto_discovery_token_budget,
            }
            log_event("mcp.discovery.decision", **decision)
            if discovery_sample is not None:
                discovery_sample.update(decision)
            if eff_disc == "upfront":
                tools = (
                    upfront
                    if upfront is not None
                    else (
                        [to_mcp_tool_definition(tool) for tool in accessible]
                        if eff_exec == "classic"
                        else [_execute_code_with_stubs(accessible)]
                    )
                )
            else:
                tools = [LIST_SERVERS_DEF, SEARCH_TOOLS_DEF]
                if eff_exec == "codemode":
                    tools.append(EXECUTE_CODE_DEF)
        else:
            tools = self.tools_for_mode(
                identity,
                mode,
                allowed_servers=effective_ceiling,
                execution=eff_exec,
                discovery_mode=eff_disc,
                guidance=guidance,
            )

        ui_map = await self.business_value_ui_map_for_request(
            identity,
            allowed_servers=effective_ceiling,
        )
        if ui_map:
            for tool_def in tools:
                tool_name = tool_def.get("name")
                if isinstance(tool_name, str) and tool_name in ui_map:
                    meta = dict(tool_def.get("_meta") or {})
                    meta["ui"] = {
                        "visibility": ["model", "app"],
                        "resourceUri": ui_map[tool_name],
                    }
                    tool_def["_meta"] = meta

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
        attribution_context: dict[str, Any] | None = None,
        intent: str | None = None,
        session_key: str | None = None,
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
                attribution_context=attribution_context,
                intent=intent,
                session_key=session_key,
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
        attribution_context: dict[str, Any] | None = None,
        intent: str | None = None,
        session_key: str | None = None,
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
        workflow_read_lock = asyncio.Lock()
        subcalls: list[SubcallRecord] = []
        detector_calls: list[dict[str, Any]] = []
        attributed_pending_calls: list[dict[str, Any]] = []
        callback_ordinal = 0
        observed_tool_outputs: list[Any] = []
        observed_tool_shapes: dict[str, Any] = {}
        external_functions: dict[str, Callable[..., Any]] = {}
        execution_cancelled = False

        for tool in accessible:
            if eff_exec == "hybrid" and tool.mutating:
                continue
            upstream = self.registry.get(tool.server)
            if upstream is None:
                continue

            def _make_wrapper(t: DiscoveredTool, u: UpstreamConfig) -> Callable[..., Any]:
                async def _wrapper(*args: Any, **kwargs: Any) -> Any:
                    nonlocal callback_ordinal
                    bound = bind_positional_arguments(t, args, kwargs)
                    current_ordinal = callback_ordinal
                    callback_ordinal += 1
                    attributed_call: dict[str, Any] | None = None
                    attributed_execution: dict[str, Any] | None = None
                    attributed_selected: dict[str, Any] | None = None
                    attributed_is_write = False
                    write_claimed = False
                    if attribution_context is not None:
                        (
                            attributed_execution,
                            attributed_selected,
                        ) = await self.resolve_business_execution_for_tool(
                            identity,
                            str(attribution_context["business_execution_id"]),
                            t.qualified_name,
                        )
                        attributed_call, created_call = await self.persist_attributed_call_intent(
                            identity,
                            attributed_execution,
                            attributed_selected,
                            t,
                            request_id=attribution_context["request_id"],
                            request_digest=attribution_context["request_digest"],
                            arguments=bound,
                            callback_ordinal=current_ordinal,
                        )
                        tool_upstream = self.registry.get(t.server) or u
                        attributed_is_write = (
                            t.mutating
                            or tool_upstream.tool_effects.get(
                                t.name, "write" if t.mutating else "read"
                            )
                            != "read"
                        )
                        prior_status = str(attributed_call.get("status") or "started")
                        prior_evidence = attributed_call.get("evidence", {})
                        if (
                            not created_call
                            and attributed_is_write
                            and (
                                prior_status
                                in {"started", "succeeded", "pending", "outcome_unknown"}
                                or prior_evidence.get("outcome_unknown") is True
                            )
                        ):
                            raise RuntimeError(
                                "This attributed write is already active, completed or unresolved; "
                                "the gateway will not dispatch it again."
                            )
                        if not created_call and prior_evidence.get("outcome_unknown") is True:
                            raise RuntimeError(
                                "This attributed call is unresolved and must be reconciled "
                                "before retry."
                            )
                        if not created_call:
                            await self.store.update_workflow_call(
                                attributed_call["workflow_call_id"], {"status": "started"}
                            )
                        if attributed_is_write:
                            lease_until = datetime.now(UTC) + timedelta(
                                seconds=tool_upstream.request_timeout_seconds + 15
                            )
                            write_claimed = await self.store.claim_workflow_write(
                                attributed_execution["business_execution_id"],
                                attributed_call["workflow_call_id"],
                                lease_until.isoformat(),
                            )
                            if not write_claimed:
                                await self.store.update_workflow_call(
                                    attributed_call["workflow_call_id"],
                                    {"status": "failed", "evidence": {"write_blocked": True}},
                                )
                                raise RuntimeError(
                                    "Another workflow write is active or unresolved."
                                )

                    workflow_settings = (
                        attributed_selected.get("execution", {})
                        if attribution_context is not None
                        and attributed_execution is not None
                        and attributed_execution["kind"] == "workflow"
                        and attributed_selected is not None
                        else {}
                    )
                    serialize_read = (
                        attribution_context is not None
                        and not attributed_is_write
                        and workflow_settings.get("allow_parallel_reads") is False
                    )
                    try:
                        if serialize_read:
                            async with workflow_read_lock:
                                async with tool_call_slots:
                                    res, rec = await self.dispatcher.invoke_tool(
                                        identity,
                                        t,
                                        u,
                                        bound,
                                        budget=budget,
                                        code_mode_hints=True,
                                        workflow_call_id=(
                                            attributed_call["workflow_call_id"]
                                            if attributed_call is not None
                                            else None
                                        ),
                                    )
                        else:
                            async with tool_call_slots:
                                res, rec = await self.dispatcher.invoke_tool(
                                    identity,
                                    t,
                                    u,
                                    bound,
                                    budget=budget,
                                    code_mode_hints=True,
                                    workflow_call_id=(
                                        attributed_call["workflow_call_id"]
                                        if attributed_call is not None
                                        else None
                                    ),
                                )
                    except Exception as exc:
                        if attributed_call is not None:
                            failure_unknown = attributed_is_write and not isinstance(
                                exc, (KeyError, ValueError, RuntimeError)
                            )
                            await self.store.update_workflow_call(
                                attributed_call["workflow_call_id"],
                                {
                                    "status": "outcome_unknown" if failure_unknown else "failed",
                                    "evidence": {"outcome_unknown": failure_unknown},
                                },
                            )
                            if write_claimed and not failure_unknown:
                                await self.store.release_workflow_write(
                                    attributed_execution["business_execution_id"],
                                    attributed_call["workflow_call_id"],
                                )
                        raise
                    subcalls.append(rec)
                    tool_upstream = self.registry.get(t.server) or u
                    detector_calls.append(
                        {
                            "upstream_name": t.server,
                            "tool_qualified_name": t.qualified_name,
                            "effect": (
                                "write"
                                if t.mutating
                                or tool_upstream.tool_effects.get(t.name, "read") != "read"
                                else "read"
                            ),
                            "status": (
                                "outcome_unknown"
                                if rec.outcome_unknown
                                else ("succeeded" if rec.status == "ok" else "failed")
                            ),
                            "sensitive_write_journal_status": rec.sensitive_write_journal_status,
                            "arguments": bound,
                            "result": res,
                        }
                    )
                    if attributed_call is not None:
                        outcome_unknown = bool(rec.outcome_unknown)
                        call_status = (
                            "outcome_unknown"
                            if outcome_unknown
                            else "succeeded"
                            if rec.status == "ok"
                            else "failed"
                        )
                        attributed_pending_calls.append(
                            {
                                "execution": attributed_execution,
                                "selected": attributed_selected,
                                "tool": t,
                                "call": attributed_call,
                                "result": res,
                                "status": call_status,
                                "sensitive_write_operation_id": rec.sensitive_write_operation_id,
                                "outcome_unknown": outcome_unknown,
                                "write_claimed": write_claimed,
                            }
                        )
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
                except asyncio.CancelledError:
                    execution_cancelled = True
                    sandbox_res = SandboxResult(
                        ok=False,
                        error="Execution was cancelled before completion.",
                        error_type="ExecutionCancelledError",
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
        failed_subcalls = [subcall for subcall in subcalls if subcall.status == "error"]
        if not effective_ok and failed_subcalls:
            primary_failure = failed_subcalls[0]
            effective_error_type = primary_failure.error_type or effective_error_type
            effective_error = primary_failure.error or effective_error
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
            if failed_subcalls:
                payload["upstream_errors"] = [
                    {
                        "server": subcall.server,
                        "tool": subcall.tool,
                        "error_type": subcall.error_type,
                        "error": subcall.error,
                        "retryable": subcall.retryable,
                        "retry_attempts": subcall.retry_attempts,
                        "outcome_unknown": subcall.outcome_unknown,
                        "upstream_status_code": subcall.upstream_status_code,
                    }
                    for subcall in failed_subcalls
                ]
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
            intent=intent,
        )
        detected_episode: dict[str, Any] | None = None
        if attribution_context is None and detector_calls:
            detected_episode = await self.workflow_detector.observe_execution(
                tenant_id=identity.tenant_id or "",
                user_sub=identity.sub,
                client_id=identity.client_id or "",
                session_key=session_key,
                intent=intent,
                run_id=run["run_id"],
                calls=detector_calls,
                is_single_turn_batch=True,
                caller_regions=extract_caller_regions(
                    claims=identity.claims, client_groups=identity.client_groups
                ),
            )
        business_value_event: dict[str, Any] | None = None
        for attributed_call in attributed_pending_calls:
            value_event = await self.finalize_attributed_call(
                identity,
                attributed_call["execution"],
                attributed_call["selected"],
                attributed_call["tool"],
                attributed_call["call"],
                result=attributed_call["result"],
                status=attributed_call["status"],
                run_id=run["run_id"],
                sensitive_write_operation_id=attributed_call["sensitive_write_operation_id"],
                outcome_unknown=attributed_call["outcome_unknown"],
            )
            if attributed_call["write_claimed"] and not attributed_call["outcome_unknown"]:
                await self.store.release_workflow_write(
                    attributed_call["execution"]["business_execution_id"],
                    attributed_call["call"]["workflow_call_id"],
                )
            if value_event is not None:
                business_value_event = value_event
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
        if business_value_event is not None:
            payload["business_value_completion"] = business_value_event
        if detected_episode is not None:
            payload["workflow_detection"] = {
                "episode_id": detected_episode["episode_id"],
                "status": detected_episode["status"],
                "completion_tier": detected_episode["completion_tier"],
                "confidence": detected_episode["confidence"],
                "completion_pct": detected_episode["completion_pct"],
                "matched_workflow_id": detected_episode.get("matched_workflow_id"),
            }
        if execution_cancelled:
            raise asyncio.CancelledError
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
        requester_confirmation_policy: ToolApprovalPolicy | None = None,
        request_timeout_seconds: float | None = None,
        workflow_call_id: str | None = None,
        intent: str | None = None,
        session_key: str | None = None,
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
            if mode == "hybrid" and not tool.mutating and workflow_call_id is None:
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
            dispatch_upstream = upstream
            if request_timeout_seconds is not None:
                dispatch_upstream = upstream.model_copy(
                    update={
                        "request_timeout_seconds": min(
                            upstream.request_timeout_seconds,
                            max(0.2, request_timeout_seconds),
                        )
                    }
                )
            result, rec = await self.dispatcher.invoke_tool(
                identity,
                tool,
                dispatch_upstream,
                arguments,
                code_mode_hints=False,
                requester_confirmation_policy=requester_confirmation_policy,
                workflow_call_id=workflow_call_id,
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
                intent=intent,
            )
            detected_episode: dict[str, Any] | None = None
            is_elicitation_pause = (
                isinstance(result, dict) and result.get("resultType") == "input_required"
            )
            if workflow_call_id is None and not is_elicitation_pause:
                detected_episode = await self.workflow_detector.observe_execution(
                    tenant_id=identity.tenant_id or "",
                    user_sub=identity.sub,
                    client_id=identity.client_id or "",
                    session_key=session_key,
                    intent=intent,
                    run_id=run["run_id"],
                    calls=[
                        {
                            "upstream_name": tool.server,
                            "tool_qualified_name": tool.qualified_name,
                            "effect": (
                                "write"
                                if tool.mutating
                                or upstream.tool_effects.get(tool.name, "read") != "read"
                                else "read"
                            ),
                            "status": (
                                "outcome_unknown"
                                if rec.outcome_unknown
                                else ("succeeded" if ok else "failed")
                            ),
                            "sensitive_write_journal_status": rec.sensitive_write_journal_status,
                            "arguments": arguments,
                            "result": result,
                        }
                    ],
                    is_single_turn_batch=False,
                    caller_regions=extract_caller_regions(
                        claims=identity.claims, client_groups=identity.client_groups
                    ),
                )
            self._record_execution_metrics(
                identity=identity,
                mode=mode,
                ok=ok,
                duration_ms=duration_ms,
                run=run,
            )
            await self._publish_live_summary(run, identity)
            out_payload: dict[str, Any] = {
                "ok": ok,
                "result": result,
                "error": rec.error,
                "error_type": rec.error_type,
                "retryable": rec.retryable,
                "retry_attempts": rec.retry_attempts,
                "outcome_unknown": rec.outcome_unknown,
                "upstream_status_code": rec.upstream_status_code,
                "run_id": run["run_id"],
                "workflow_call_id": rec.workflow_call_id,
                "sensitive_write_operation_id": rec.sensitive_write_operation_id,
                "sensitive_write_journal_status": rec.sensitive_write_journal_status,
                "raw_upstream_bytes": run["raw_upstream_bytes"],
                "returned_bytes": run["returned_bytes"],
                "duration_ms": duration_ms,
            }
            if detected_episode is not None:
                out_payload["workflow_detection"] = {
                    "episode_id": detected_episode["episode_id"],
                    "status": detected_episode["status"],
                    "completion_tier": detected_episode["completion_tier"],
                    "confidence": detected_episode["confidence"],
                    "completion_pct": detected_episode["completion_pct"],
                    "matched_workflow_id": detected_episode.get("matched_workflow_id"),
                }
            return out_payload
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


def _approval_request_state(settings: GatewaySettings, payload: dict[str, Any]) -> str:
    serialized = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    encoded = base64.urlsafe_b64encode(serialized).rstrip(b"=").decode("ascii")
    key = hashlib.sha256(settings.provenance_signing_secrets[0].encode("utf-8")).digest()
    signature = hmac.new(key, encoded.encode("ascii"), hashlib.sha256).hexdigest()
    return f"{encoded}.{signature}"


def _verify_approval_request_state(settings: GatewaySettings, state: Any) -> dict[str, Any] | None:
    if not isinstance(state, str) or len(state) > 4096 or state.count(".") != 1:
        return None
    encoded, signature = state.split(".", 1)
    if not encoded or not re.fullmatch(r"[0-9a-f]{64}", signature):
        return None
    valid = False
    for secret in settings.provenance_signing_secrets:
        key = hashlib.sha256(secret.encode("utf-8")).digest()
        expected = hmac.new(key, encoded.encode("ascii"), hashlib.sha256).hexdigest()
        valid |= hmac.compare_digest(expected, signature)
    if not valid:
        return None
    try:
        serialized = base64.urlsafe_b64decode(encoded + "=" * (-len(encoded) % 4))
        payload = json.loads(serialized)
    except (ValueError, json.JSONDecodeError):
        return None
    return payload if isinstance(payload, dict) else None


def _input_required_confirmation(req_id: Any, state: str, message: str) -> JSONResponse:
    return _jsonrpc_ok(
        req_id,
        {
            "resultType": "input_required",
            "requestState": state,
            "inputRequests": {
                "confirm": {
                    "method": "elicitation/create",
                    "params": {
                        "mode": "form",
                        "message": message,
                        "requestedSchema": {
                            "type": "object",
                            "properties": {
                                "confirm": {
                                    "type": "boolean",
                                    "title": "Confirm this tool call",
                                }
                            },
                            "required": ["confirm"],
                        },
                    },
                }
            },
        },
    )


_SENSITIVE_APPROVAL_FIELD_RE = re.compile(
    r"password|secret|token|api[_-]?key|authorization|credential|ssn|card|email|phone",
    re.IGNORECASE,
)
_APPROVAL_FIELD_RE = re.compile(r"\{([A-Za-z_][A-Za-z0-9_]*)\}")


def _render_approval_summary(policy: ToolApprovalPolicy, arguments: dict[str, Any]) -> str:
    """Render only administrator-selected scalar argument fields into an approval summary."""
    summary = policy.confirmation_message.strip()

    def substitute(match: re.Match[str]) -> str:
        name = match.group(1)
        value = arguments.get(name)
        if _SENSITIVE_APPROVAL_FIELD_RE.search(name):
            return "[redacted]"
        if not isinstance(value, (str, int, float, bool)):
            return "[omitted]"
        safe_value = OutputRedactor.redact_text(str(value)).replace("\n", " ").strip()
        return safe_value[:120]

    return _APPROVAL_FIELD_RE.sub(substitute, summary)[:600]


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

    if method in {"tasks/get", "tasks/update", "tasks/cancel"}:
        client_capabilities = meta.get(MCP_CLIENT_CAPABILITIES_META)
        extensions = (
            client_capabilities.get("extensions") if isinstance(client_capabilities, dict) else None
        )
        if not isinstance(extensions, dict) or MCP_TASKS_EXTENSION not in extensions:
            return JSONResponse(
                {
                    "jsonrpc": "2.0",
                    "id": req_id,
                    "error": {
                        "code": -32021,
                        "message": "The MCP Tasks extension is required for this request.",
                        "data": {"requiredCapabilities": {"extensions": {MCP_TASKS_EXTENSION: {}}}},
                    },
                }
            )
        task_id = params.get("taskId")
        name_header = request.headers.get("mcp-name")
        if not isinstance(task_id, str) or not task_id or name_header != task_id:
            return mismatch("Mcp-Name header must match the taskId for task methods")
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


class ToolApprovalDecisionRequest(BaseModel):
    decision: str = Field(pattern="^(approve|deny)$")


def _valid_run_timestamp(value: Any) -> bool:
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        return parsed.tzinfo is not None
    except (TypeError, ValueError):
        return False


def _is_dependency_connection_error(exc: BaseException) -> bool:
    pending: list[BaseException] = [exc]
    seen: set[int] = set()
    while pending:
        current = pending.pop()
        if id(current) in seen:
            continue
        seen.add(id(current))
        if isinstance(current, (ConnectionError, TimeoutError, OSError)):
            return True
        module = type(current).__module__
        name = type(current).__name__
        if module.startswith("asyncpg.") and (
            name.endswith("ConnectionError") or name == "CannotConnectNowError"
        ):
            return True
        if module.startswith("redis.") and name in {"ConnectionError", "TimeoutError"}:
            return True
        if current.__cause__ is not None:
            pending.append(current.__cause__)
        if current.__context__ is not None:
            pending.append(current.__context__)
    return False


def _parse_minute_assumption(value: Any, field_name: str) -> dict[str, Any] | None:
    if value is None or value == "":
        return None
    if isinstance(value, dict):
        if value.get("kind") == "capture_actual_when_available":
            return {"kind": "capture_actual_when_available"}
        raw = value.get("minutes")
    else:
        raw = value
    try:
        mins = float(raw)
    except (TypeError, ValueError) as exc:
        raise HTTPException(status_code=422, detail=f"{field_name} must be numeric.") from exc
    if mins < 0 or mins > 1440:
        raise HTTPException(
            status_code=422, detail=f"{field_name} must be between 0 and 1440 minutes."
        )
    return {"kind": "expected_average", "minutes": mins}


def _apply_baseline_updates(
    bv: dict[str, Any], body: dict[str, Any], reporting_currency: str
) -> dict[str, Any]:
    updated = dict(bv)
    if "manual_baseline_minutes" in body:
        try:
            mins = float(body["manual_baseline_minutes"])
        except (TypeError, ValueError) as exc:
            raise HTTPException(
                status_code=422, detail="manual_baseline_minutes must be numeric."
            ) from exc
        if mins <= 0:
            raise HTTPException(status_code=422, detail="manual_baseline_minutes must exceed zero.")
        updated["manual_baseline_minutes"] = mins
    if "baseline_source" in body:
        updated["baseline_source"] = str(body["baseline_source"])
    if "confidence" in body:
        updated["confidence"] = str(body["confidence"])
    if "effective_from" in body and body["effective_from"]:
        updated["effective_from"] = str(body["effective_from"])
    if "evidence_note" in body:
        updated["evidence_note"] = (
            str(body["evidence_note"]) if body["evidence_note"] is not None else None
        )
    if "review_minutes" in body:
        parsed_review = _parse_minute_assumption(body["review_minutes"], "review_minutes")
        if parsed_review is None:
            updated.pop("review_minutes", None)
        else:
            updated["review_minutes"] = parsed_review
    if "correction_minutes" in body:
        parsed_corr = _parse_minute_assumption(body["correction_minutes"], "correction_minutes")
        if parsed_corr is None:
            updated.pop("correction_minutes", None)
        else:
            updated["correction_minutes"] = parsed_corr
    if "regional_labor_rates" in body:
        raw_regional = body["regional_labor_rates"]
        if not isinstance(raw_regional, dict):
            raise HTTPException(
                status_code=422,
                detail="regional_labor_rates must be an object mapping region to rate.",
            )
        normalized_reg: dict[str, float] = {}
        for region_key, region_rate in raw_regional.items():
            clean_key = str(region_key).strip().upper()
            if not re.match(r"^[A-Z0-9_-]{2,24}$", clean_key):
                raise HTTPException(
                    status_code=422,
                    detail=(
                        f"Regional rate key '{region_key}' must be 2-24 characters "
                        "(A-Z, 0-9, _, -)."
                    ),
                )
            try:
                num_rate = float(region_rate)
            except (TypeError, ValueError) as exc:
                raise HTTPException(
                    status_code=422,
                    detail=f"Regional labor rate for '{clean_key}' must be numeric.",
                ) from exc
            if num_rate < 0 or num_rate > 1_000_000:
                raise HTTPException(
                    status_code=422,
                    detail=(
                        f"Regional labor rate for '{clean_key}' must be between 0 and 1,000,000."
                    ),
                )
            normalized_reg[clean_key] = num_rate
        if normalized_reg:
            updated["regional_labor_rates"] = normalized_reg
        else:
            updated.pop("regional_labor_rates", None)
    if "loaded_labor_cost_per_hour" in body:
        raw_hourly = body["loaded_labor_cost_per_hour"]
        if raw_hourly is None or raw_hourly == "":
            updated.pop("loaded_labor_cost_per_hour", None)
        else:
            try:
                hourly = float(raw_hourly)
            except (TypeError, ValueError) as exc:
                raise HTTPException(
                    status_code=422, detail="loaded_labor_cost_per_hour must be numeric."
                ) from exc
            if hourly < 0 or hourly > 1_000_000:
                raise HTTPException(
                    status_code=422,
                    detail="loaded_labor_cost_per_hour must be between 0 and 1,000,000.",
                )
            updated["loaded_labor_cost_per_hour"] = hourly
    if "currency" in body:
        raw_cur = body["currency"]
        if raw_cur is None or str(raw_cur).strip() == "":
            updated.pop("currency", None)
        else:
            clean_cur = str(raw_cur).strip().upper()
            if not re.match(r"^[A-Z]{3}$", clean_cur):
                raise HTTPException(
                    status_code=422, detail="currency must be a 3-letter ISO currency code."
                )
            updated["currency"] = clean_cur
    has_rate = updated.get("loaded_labor_cost_per_hour") is not None or bool(
        updated.get("regional_labor_rates")
    )
    if has_rate and not updated.get("currency"):
        updated["currency"] = reporting_currency
    elif not has_rate:
        updated.pop("currency", None)
    return updated


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

    is_prod = runtime.settings.app_env == "prod"
    app = FastAPI(
        title="MCPcodeaway Gateway",
        version=__version__,
        lifespan=lifespan,
        docs_url=None if is_prod else "/docs",
        redoc_url=None if is_prod else "/redoc",
        openapi_url=None if is_prod else "/openapi.json",
    )
    app.state.runtime = runtime

    @app.exception_handler(Exception)
    async def handle_unexpected_error(request: Request, exc: Exception) -> JSONResponse:
        if not _is_dependency_connection_error(exc):
            raise exc
        log_event(
            "dependency.unavailable",
            level=logging.ERROR,
            path=request.url.path,
            error_type=type(exc).__name__,
        )
        return JSONResponse(
            status_code=503,
            content={"detail": "A required shared service is temporarily unavailable."},
            headers={"Retry-After": "1"},
        )

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

    async def _resolve_session_identity(request: Request) -> CallerIdentity | None:
        return runtime.admin_sessions.verify_session_token(
            request.cookies.get("mcpcodeaway_admin_session")
        )

    app.include_router(
        create_user_connect_router(
            settings=runtime.settings,
            registry=runtime.registry,
            validator=runtime.validator,
            policy=runtime.policy,
            cache=runtime.cache,
            vault=runtime.credential_vault,
            secret_resolver=runtime.secret_resolver,
            connect_signing_key=runtime._connect_signing_key,
            http_client=runtime.client_manager._http_client,
            session_resolver=_resolve_session_identity,
        )
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
            return JSONResponse(
                status_code=503,
                content={"ready": False, "state": runtime.lifecycle_state.value},
            )
        return {"ready": True, "state": runtime.lifecycle_state.value}

    mcp_request_slots = asyncio.Semaphore(max(1, runtime.settings.max_concurrent_mcp_requests))

    @app.middleware("http")
    async def expose_opt_in_instance_id(request: Request, call_next: Callable[..., Any]) -> Any:
        response = await call_next(request)
        if os.getenv("GATEWAY_EXPOSE_INSTANCE_ID", "false").lower() == "true":
            instance_id = os.getenv("POD_NAME", "")
            if instance_id:
                response.headers["X-Gateway-Instance"] = instance_id
        return response

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
        x_admin_force_mode: str | None = Header(default=None),
        x_discovery_mode: str | None = Header(default=None),
        x_discovery_backend: str | None = Header(default=None),
        x_mcp_servers: str | None = Header(default=None),
        x_intent: str | None = Header(default=None),
        x_workload_profile: str | None = Header(default=None),
        x_model_name: str | None = Header(default=None),
        x_agent_model: str | None = Header(default=None),
        x_require_grounding: str | None = Header(default=None),
        x_request_id: str | None = Header(default=None),
        x_workflow_run_id: str | None = Header(default=None),
        authorization: str | None = Header(default=None),
    ) -> JSONResponse:
        effective_session_key = (x_workflow_run_id or "").strip() or None
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

                forced_mode = (
                    x_admin_force_mode.strip()
                    if x_admin_force_mode
                    and (
                        runtime.settings.app_env != "prod"
                        or any(r in runtime.settings.admin_roles for r in identity.roles)
                    )
                    else None
                )
                eff_exec, eff_disc_mode, effective_mode = _resolve_execution_and_discovery(
                    runtime.settings,
                    query_mode=forced_mode or mode,
                    header_mode=None if forced_mode or mode else x_gateway_mode,
                    query_execution=(
                        execution
                        if execution is not None
                        else ("auto" if not (forced_mode or mode or x_gateway_mode) else None)
                    ),
                    header_execution=None if forced_mode else x_execution_mode,
                    query_discovery_mode=discovery_mode,
                    header_discovery_mode=x_discovery_mode,
                    x_intent=x_intent,
                )
                requested_discovery = eff_disc_mode
                fixed_mode_values = (
                    mode,
                    execution,
                    x_gateway_mode,
                    x_execution_mode,
                )
                has_explicit_mode = bool(forced_mode) or any(
                    value and value != "auto" for value in fixed_mode_values
                )
                mode_decision_reason: str | None = (
                    f"admin_force:{effective_mode}" if forced_mode else f"auto:{effective_mode}"
                )
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
                if (
                    not has_explicit_mode
                    and method == "tools/call"
                    and isinstance(params, dict)
                    and params.get("name") == "execute_code"
                ):
                    eff_exec = "codemode"
                    effective_mode = "codemode"
                    mode_decision_reason = "auto:execute_code->codemode"
                elif (
                    not has_explicit_mode
                    and method == "tools/call"
                    and isinstance(params, dict)
                    and "__" in str(params.get("name") or "")
                ):
                    _tname = str(params.get("name") or "")
                    _scfg = runtime.registry.get(_tname.split("__", 1)[0])
                    if _scfg is not None and _tname in _scfg.tool_mode_overrides:
                        _ovr = _scfg.tool_mode_overrides[_tname]
                        effective_mode = _ovr
                        eff_exec = (
                            "classic" if _ovr in ("traditional", "traditional_discover") else _ovr
                        )
                        mode_decision_reason = f"tool_override:{_ovr}"
                request_meta = params.get("_meta", {}) if isinstance(params, dict) else {}
                client_capabilities = (
                    request_meta.get(MCP_CLIENT_CAPABILITIES_META)
                    if isinstance(request_meta, dict)
                    else None
                )
                client_extensions = (
                    client_capabilities.get("extensions")
                    if isinstance(client_capabilities, dict)
                    else None
                )
                mcp_app_capability = (
                    client_extensions.get(MCP_APP_EXTENSION)
                    if isinstance(client_extensions, dict)
                    else None
                )
                supports_mcp_apps = bool(
                    isinstance(mcp_app_capability, dict)
                    and isinstance(mcp_app_capability.get("mimeTypes"), list)
                    and MCP_APP_MIME_TYPE in mcp_app_capability["mimeTypes"]
                )
                supports_tasks = (
                    isinstance(client_extensions, dict) and MCP_TASKS_EXTENSION in client_extensions
                )
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

                discovery_sample: dict[str, Any] = {}

                def _finish(
                    resp: JSONResponse,
                    status_label: str = "ok",
                    **discovery_metrics: Any,
                ) -> JSONResponse:
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
                        response_bytes=len(resp.body),
                        **discovery_metrics,
                    )
                    operation = discovery_metrics.get("discovery_operation")
                    if operation or (
                        method == "tools/call" and params.get("name") == "search_tools"
                    ):
                        runtime.discovery_samples.append(
                            {
                                "tenant": identity.tenant_id,
                                "request_id": req_corr_id,
                                "created_at": datetime.now(UTC).isoformat(),
                                "duration_ms": dur_ms,
                                "response_bytes": len(resp.body),
                                "status": status_label,
                                "operation": operation or "search",
                                "applied_discovery": eff_disc_mode,
                                **discovery_sample,
                                **discovery_metrics,
                            }
                        )
                    resp.headers["X-Request-Id"] = req_corr_id
                    if mode_decision_reason:
                        resp.headers["X-Gateway-Mode-Decision"] = mode_decision_reason
                    if effective_model_name:
                        resp.headers["X-Gateway-Model"] = effective_model_name
                    return resp

                if method in {"resources/list", "resources/read"} and not supports_mcp_apps:
                    return _finish(
                        _jsonrpc_err(
                            req_id,
                            -32021,
                            "The MCP Apps extension with text/html;profile=mcp-app is required.",
                        ),
                        status_label="capability_required",
                    )

                if method in {"resources/list", "resources/read"}:
                    app_resources = await runtime.business_value_resources_for_request(
                        identity,
                        allowed_servers=allowed_servers,
                    )
                    if method == "resources/list":
                        return _finish(
                            _jsonrpc_ok(
                                req_id,
                                {
                                    "resources": [
                                        app_resources[uri]["descriptor"]
                                        for uri in sorted(app_resources)
                                    ]
                                },
                            ),
                            status_label="ok",
                        )
                    resource_uri = params.get("uri") if isinstance(params, dict) else None
                    if not isinstance(resource_uri, str) or not resource_uri:
                        return _finish(
                            _jsonrpc_err(req_id, -32602, "resources/read requires a URI."),
                            status_label="error",
                        )
                    resource = app_resources.get(resource_uri)
                    if resource is None:
                        return _finish(
                            _jsonrpc_err(req_id, -32002, "UI resource not found."),
                            status_label="not_found",
                        )
                    return _finish(
                        _jsonrpc_ok(req_id, {"contents": [resource["content"]]}),
                        status_label="ok",
                    )

                if method in {"tasks/get", "tasks/update", "tasks/cancel"}:
                    task_id = str(params.get("taskId") or "")
                    row = await runtime.recovery.get_owned(identity, task_id)
                    if row is None:
                        return _finish(
                            _jsonrpc_err(req_id, -32602, "Invalid or inaccessible taskId."),
                            status_label="not_found",
                        )
                    if method == "tasks/get":
                        return _finish(
                            _jsonrpc_ok(req_id, runtime.recovery.task_result(row, create=False))
                        )
                    if method == "tasks/cancel":
                        if row["status"] in {"pending", "working"}:
                            await runtime.store.request_operation_cancel(
                                task_id,
                                identity.tenant_id,
                                identity.sub,
                                identity.client_id,
                            )
                        return _finish(_jsonrpc_ok(req_id, {}), status_label="cancel_requested")
                    return _finish(_jsonrpc_ok(req_id, {}))

                if method == "server/discover":
                    advertised_extensions = {MCP_TASKS_EXTENSION: {}}
                    if supports_mcp_apps:
                        advertised_extensions[MCP_APP_EXTENSION] = {
                            "mimeTypes": [MCP_APP_MIME_TYPE]
                        }
                    return _finish(
                        _jsonrpc_ok(
                            req_id,
                            {
                                "supportedVersions": [MCP_PROTOCOL_VERSION],
                                "capabilities": {
                                    "tools": {},
                                    "extensions": advertised_extensions,
                                },
                                "_meta": {
                                    MCP_SERVER_INFO_META: {
                                        "name": "mcpcodeaway",
                                        "version": __version__,
                                    }
                                },
                                "ttlMs": 30000,
                                "cacheScope": "private",
                                "instructions": (
                                    "For deferred discovery, start with search_tools to find "
                                    "tools across authorized upstreams. In Code Mode, use the "
                                    "returned Python stubs with execute_code. Use list_servers "
                                    "for optional browsing. In Classic, call available tools "
                                    "directly. Pass X-Intent on tools/list to preload matches, "
                                    "or use ?servers=... to scope the request."
                                ),
                            },
                        )
                    )
                await runtime.ensure_catalog_for_identity(identity)
                target_tool_hint: str | None = None
                target_tools_hint: list[str] | None = None
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
                        target_tools_hint = [
                            t.qualified_name
                            for t in runtime.catalog.list_accessible_tools(
                                identity,
                                runtime.policy,
                                runtime.registry,
                                allowed_servers=allowed_servers,
                            )
                            if t.qualified_name in code_str
                        ]
                        if target_tools_hint:
                            target_tool_hint = target_tools_hint[0]
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
                    requested_exec=(
                        "auto"
                        if method == "tools/call"
                        and isinstance(params, dict)
                        and params.get("name") == "execute_code"
                        and not has_explicit_mode
                        else eff_exec
                    ),
                    requested_disc=eff_disc_mode,
                    requested_mode=effective_mode,
                    allowed_servers=allowed_servers,
                    intent=effective_intent,
                    request_id=req_corr_id,
                    explicit_override=has_explicit_mode,
                    discovery_override=bool(
                        discovery_mode
                        or x_discovery_mode
                        or effective_intent
                        or runtime.settings.discovery_mode
                    ),
                    model_name=effective_model_name,
                    target_tool=target_tool_hint,
                    target_tools=target_tools_hint,
                    workload_profile=x_workload_profile,
                )
                if (
                    method == "tools/call"
                    and isinstance(params, dict)
                    and params.get("name") == "execute_code"
                    and not has_explicit_mode
                    and not str(mode_decision_reason or "").startswith("adaptive_")
                ):
                    eff_exec = "codemode"
                    effective_mode = "codemode"
                    if "adaptive_recommendation:" not in str(mode_decision_reason or ""):
                        mode_decision_reason = "auto:execute_code->codemode"
                mcp_span.set_attribute("mcp.mode", effective_mode)
                if mode_decision_reason:
                    mcp_span.set_attribute("mcp.mode_decision_reason", mode_decision_reason)
                if experiment_id:
                    mcp_span.set_attribute("mcp.ab_experiment", experiment_id)

                if not experiment_id and (
                    discovery_mode or x_discovery_mode or runtime.settings.discovery_mode
                ):
                    eff_disc_mode = requested_discovery
                    effective_mode = _axis_to_gateway_mode(eff_exec, eff_disc_mode)

                automatic_discovery = (
                    not has_explicit_mode
                    and not experiment_id
                    and not (discovery_mode or x_discovery_mode or runtime.settings.discovery_mode)
                    and not effective_intent
                    and eff_disc_mode != "x_intent"
                    and not (mode_decision_reason or "").startswith(
                        ("model_profile_override:", "tool_override:")
                    )
                )

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
                        auto_discovery=automatic_discovery,
                        discovery_sample=discovery_sample,
                    )
                    listed.setdefault("ttlMs", 0)
                    listed.setdefault("cacheScope", "private")
                    if not supports_mcp_apps:
                        for tool_definition in listed.get("tools", []):
                            metadata = tool_definition.get("_meta")
                            if not isinstance(metadata, dict):
                                continue
                            metadata.pop("ui", None)
                            if not metadata:
                                tool_definition.pop("_meta", None)
                    return _finish(
                        _jsonrpc_ok(req_id, listed),
                        discovery_operation="listing",
                        definitions_returned=len(listed["tools"]),
                        estimated_discovery_tokens=estimate_tokens(listed["tools"]),
                    )

                if method == "tools/call":
                    tool_name = str(params.get("name") or "")
                    arguments = dict(params.get("arguments") or {})
                    attributed_context: dict[str, Any] | None = None

                    if tool_name == START_BUSINESS_EXECUTION_TOOL:
                        try:
                            started_execution = await runtime.start_business_execution_for_identity(
                                identity,
                                arguments,
                                request_id=req_id,
                                request_payload=payload,
                                allowed_servers=allowed_servers,
                            )
                        except BusinessExecutionConflict as exc:
                            return _finish(
                                _jsonrpc_err(req_id, -32602, str(exc)), status_label="error"
                            )
                        except (PermissionError, ValueError) as exc:
                            return _finish(
                                _jsonrpc_err(req_id, -32602, str(exc)), status_label="error"
                            )
                        return _finish(
                            _jsonrpc_ok(
                                req_id,
                                {
                                    "content": [
                                        {
                                            "type": "text",
                                            "text": json.dumps(started_execution),
                                        }
                                    ],
                                    "structuredContent": started_execution,
                                },
                            ),
                            status_label="ok",
                        )

                    if tool_name.startswith(ATTRIBUTED_TOOL_PREFIX):
                        original_tool_name = tool_name[len(ATTRIBUTED_TOOL_PREFIX) :]
                        business_execution_id = arguments.get("business_execution_id")
                        original_arguments = arguments.get("arguments")
                        if (
                            not original_tool_name
                            or set(arguments) != {"business_execution_id", "arguments"}
                            or not isinstance(business_execution_id, str)
                            or not isinstance(original_arguments, dict)
                        ):
                            return _finish(
                                _jsonrpc_err(
                                    req_id,
                                    -32602,
                                    "Attributed calls require a business execution ID and an "
                                    "arguments object.",
                                ),
                                status_label="error",
                            )
                        try:
                            (
                                execution_row,
                                selected_definition,
                            ) = await runtime.resolve_business_execution_for_tool(
                                identity,
                                business_execution_id,
                                original_tool_name,
                            )
                            await runtime.ensure_catalog_for_identity(identity)
                            wrapped_tool = runtime.catalog.get_tool(original_tool_name)
                            if wrapped_tool is None:
                                raise ValueError("attributed tool is no longer available")
                            validate_attributed_arguments(
                                wrapped_tool.input_schema,
                                original_arguments,
                            )
                        except BusinessExecutionConflict as exc:
                            return _finish(
                                _jsonrpc_err(req_id, -32602, str(exc)), status_label="error"
                            )
                        except PermissionError as exc:
                            return _finish(
                                _jsonrpc_err(req_id, -31003, str(exc)), status_label="error"
                            )
                        except (KeyError, ValueError) as exc:
                            return _finish(
                                _jsonrpc_err(req_id, -32602, str(exc)), status_label="error"
                            )
                        attributed_context = {
                            "execution": execution_row,
                            "selected": selected_definition,
                            "request_id": req_id,
                            "request_digest": canonical_json_digest(payload),
                        }
                        tool_name = original_tool_name
                        arguments = original_arguments

                    if tool_name == "get_operation_status":
                        operation_id = str(arguments.get("operation_id") or "")
                        row = await runtime.recovery.get_owned(identity, operation_id)
                        if row is None:
                            return _finish(
                                _jsonrpc_ok(
                                    req_id,
                                    {
                                        "content": [
                                            {"type": "text", "text": "Operation not found."}
                                        ],
                                        "structuredContent": {
                                            "ok": False,
                                            "operation_id": operation_id,
                                            "operation_status": "not_found",
                                        },
                                        "isError": True,
                                    },
                                ),
                                status_label="not_found",
                            )
                        status_result = runtime.recovery.status_result(row)
                        return _finish(
                            _jsonrpc_ok(
                                req_id,
                                {
                                    "content": [
                                        {"type": "text", "text": json.dumps(status_result)}
                                    ],
                                    "structuredContent": status_result,
                                    "isError": row["status"]
                                    in {"failed", "needs_attention", "cancelled"},
                                },
                            ),
                            status_label=str(row["status"]),
                        )

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
                        if (
                            eff_exec == "classic"
                            and eff_disc_mode == "upfront"
                            and not (
                                automatic_discovery
                                and runtime.settings.auto_discovery_policy == "active"
                            )
                        ):
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
                                guidance=await runtime.store.list_tool_guidance(
                                    identity.tenant_id or ""
                                ),
                            )
                        return _finish(
                            _jsonrpc_ok(
                                req_id,
                                {
                                    "content": [{"type": "text", "text": json.dumps(res)}],
                                    "structuredContent": res,
                                },
                            ),
                            discovery_operation="search",
                            definitions_returned=res["returned"],
                            discovery_search_ms=res["latency_ms"],
                            estimated_discovery_tokens=estimate_tokens(res),
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
                        code_attribution: dict[str, Any] | None = None
                        business_execution_id = arguments.get("business_execution_id")
                        if business_execution_id is not None:
                            if not isinstance(business_execution_id, str):
                                return _finish(
                                    _jsonrpc_err(
                                        req_id,
                                        -32602,
                                        "business_execution_id must be a string.",
                                    ),
                                    status_label="error",
                                )
                            try:
                                (
                                    execution_row,
                                    selected_definition,
                                ) = await runtime.resolve_business_execution_for_tool(
                                    identity,
                                    business_execution_id,
                                    None,
                                )
                            except PermissionError as exc:
                                return _finish(
                                    _jsonrpc_err(req_id, -31003, str(exc)),
                                    status_label="error",
                                )
                            code_attribution = {
                                "business_execution_id": business_execution_id,
                                "execution": execution_row,
                                "selected": selected_definition,
                                "request_id": req_id,
                                "request_digest": canonical_json_digest(payload),
                            }
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
                            attribution_context=code_attribution,
                            intent=effective_intent,
                            session_key=effective_session_key,
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

                    if eff_exec == "codemode" and attributed_context is None:
                        return _finish(
                            _jsonrpc_err(
                                req_id,
                                -32601,
                                f"Direct tool '{tool_name}' is not exposed in codemode; "
                                "invoke authorized tools inside execute_code.",
                            ),
                            status_label="error",
                        )

                    requester_confirmation_policy: ToolApprovalPolicy | None = None
                    manager_approval_record: dict[str, Any] | None = None
                    await runtime.ensure_catalog_for_identity(identity)
                    target_tool = runtime.catalog.get_tool(tool_name)
                    if target_tool is not None:
                        target_upstream = runtime.registry.get(target_tool.server)
                        approval_policy = (
                            target_upstream.tool_approval_policies.get(target_tool.name)
                            if target_upstream is not None
                            else None
                        )
                        if (
                            approval_policy is not None
                            and approval_policy.mode == "manager_approval"
                        ):
                            argument_digest = hashlib.sha256(
                                json.dumps(arguments, sort_keys=True, separators=(",", ":")).encode(
                                    "utf-8"
                                )
                            ).hexdigest()
                            manager_approval_record = await runtime.store.find_tool_approval(
                                tenant_id=identity.tenant_id,
                                requester_sub=identity.sub,
                                upstream_name=target_tool.server,
                                tool_name=target_tool.name,
                                argument_digest=argument_digest,
                                policy_version=approval_policy.version,
                            )
                            if manager_approval_record is not None:
                                status = manager_approval_record["status"]
                                if status == "pending":
                                    result = {
                                        "approval_status": "pending",
                                        "approval_id": manager_approval_record["approval_id"],
                                        "message": (
                                            "This action is waiting for an eligible approver."
                                        ),
                                    }
                                    return _finish(
                                        _jsonrpc_ok(
                                            req_id,
                                            {
                                                "content": [
                                                    {"type": "text", "text": result["message"]}
                                                ],
                                                "structuredContent": result,
                                                "isError": True,
                                            },
                                        ),
                                        status_label="approval_pending",
                                    )
                                if status == "denied":
                                    return _finish(
                                        _jsonrpc_err(
                                            req_id,
                                            -31004,
                                            "An approver denied this action. A new request "
                                            "requires a policy change or expiry.",
                                        ),
                                        status_label="approval_denied",
                                    )
                        if (
                            approval_policy is not None
                            and approval_policy.mode
                            in {"requester_confirmation", "manager_approval"}
                            and not (
                                manager_approval_record is not None
                                and manager_approval_record["status"] == "approved"
                            )
                        ):
                            if (
                                allowed_servers is not None
                                and target_tool.server not in allowed_servers
                            ):
                                return _finish(
                                    _jsonrpc_err(
                                        req_id,
                                        -31003,
                                        "Tool is outside the request-level server ceiling.",
                                    ),
                                    status_label="error",
                                )
                            allowed, reason = runtime.policy.can_execute_tool(
                                identity,
                                target_upstream,
                                target_tool.name,
                                target_tool.mutating,
                            )
                            if not allowed:
                                return _finish(
                                    _jsonrpc_err(req_id, -31003, reason), status_label="error"
                                )

                            request_meta = params.get("_meta")
                            request_meta = request_meta if isinstance(request_meta, dict) else {}
                            client_capabilities = request_meta.get(MCP_CLIENT_CAPABILITIES_META)
                            client_capabilities = (
                                client_capabilities if isinstance(client_capabilities, dict) else {}
                            )
                            elicitation_capability = client_capabilities.get("elicitation")
                            supports_form_elicitation = isinstance(
                                elicitation_capability, dict
                            ) and isinstance(elicitation_capability.get("form"), dict)
                            if not supports_form_elicitation:
                                return _finish(
                                    JSONResponse(
                                        {
                                            "jsonrpc": "2.0",
                                            "id": req_id,
                                            "error": {
                                                "code": -32021,
                                                "message": (
                                                    "Requester confirmation is required, but "
                                                    "this client lacks form elicitation."
                                                ),
                                                "data": {
                                                    "requiredCapabilities": {
                                                        "elicitation": {"form": {}}
                                                    }
                                                },
                                            },
                                        }
                                    ),
                                    status_label="error",
                                )

                            policy_payload = approval_policy.model_dump(mode="json")
                            policy_digest = hashlib.sha256(
                                json.dumps(
                                    policy_payload, sort_keys=True, separators=(",", ":")
                                ).encode("utf-8")
                            ).hexdigest()
                            argument_digest = hashlib.sha256(
                                json.dumps(arguments, sort_keys=True, separators=(",", ":")).encode(
                                    "utf-8"
                                )
                            ).hexdigest()
                            request_state = params.get("requestState")
                            responses = params.get("inputResponses")
                            if request_state is None:
                                if responses is not None:
                                    return _finish(
                                        _jsonrpc_err(
                                            req_id,
                                            -32602,
                                            "inputResponses requires the matching requestState.",
                                        ),
                                        status_label="error",
                                    )
                                expires_at = int(time.time()) + 300
                                state_payload = {
                                    "method": "tools/call",
                                    "tool": target_tool.qualified_name,
                                    "arguments": argument_digest,
                                    "tenant": identity.tenant_id,
                                    "subject": identity.sub,
                                    "client": identity.client_id,
                                    "policy": policy_digest,
                                    "expires_at": expires_at,
                                    "nonce": uuid.uuid4().hex,
                                }
                                message = _render_approval_summary(approval_policy, arguments) or (
                                    f"Confirm this call to {target_tool.qualified_name}. "
                                    "Review the tool call and its arguments before responding."
                                )
                                return _finish(
                                    _input_required_confirmation(
                                        req_id,
                                        _approval_request_state(runtime.settings, state_payload),
                                        message,
                                    ),
                                    status_label="input_required",
                                )

                            state_payload = _verify_approval_request_state(
                                runtime.settings, request_state
                            )
                            now = int(time.time())
                            if (
                                state_payload is None
                                or state_payload.get("method") != "tools/call"
                                or state_payload.get("tool") != target_tool.qualified_name
                                or state_payload.get("arguments") != argument_digest
                                or state_payload.get("tenant") != identity.tenant_id
                                or state_payload.get("subject") != identity.sub
                                or state_payload.get("client") != identity.client_id
                                or state_payload.get("policy") != policy_digest
                                or not isinstance(state_payload.get("expires_at"), int)
                                or state_payload["expires_at"] <= now
                                or not isinstance(state_payload.get("nonce"), str)
                            ):
                                return _finish(
                                    _jsonrpc_err(
                                        req_id,
                                        -32602,
                                        "Invalid, expired, or mismatched requestState.",
                                    ),
                                    status_label="error",
                                )
                            if not isinstance(responses, dict):
                                message = _render_approval_summary(approval_policy, arguments) or (
                                    f"Confirm this call to {target_tool.qualified_name}. "
                                    "Review the tool call and its arguments before responding."
                                )
                                return _finish(
                                    _input_required_confirmation(req_id, request_state, message),
                                    status_label="input_required",
                                )
                            confirmation = responses.get("confirm")
                            if not isinstance(confirmation, dict):
                                message = _render_approval_summary(approval_policy, arguments) or (
                                    f"Confirm this call to {target_tool.qualified_name}. "
                                    "Review the tool call and its arguments before responding."
                                )
                                return _finish(
                                    _input_required_confirmation(req_id, request_state, message),
                                    status_label="input_required",
                                )
                            action = confirmation.get("action")
                            if (isinstance(action, str) and action in {"decline", "cancel"}) or (
                                action == "accept"
                                and isinstance(confirmation.get("content"), dict)
                                and confirmation["content"].get("confirm") is False
                            ):
                                consumed = await runtime.store.consume_request_state_nonce(
                                    state_payload["nonce"],
                                    datetime.fromtimestamp(
                                        state_payload["expires_at"], UTC
                                    ).isoformat(),
                                )
                                if not consumed:
                                    return _finish(
                                        _jsonrpc_err(
                                            req_id,
                                            -32602,
                                            "This confirmation has already been used.",
                                        ),
                                        status_label="error",
                                    )
                                log_event(
                                    "approval.requester_confirmation",
                                    outcome="declined" if action == "decline" else "cancelled",
                                    tenant_id=identity.tenant_id,
                                    user_sub=identity.sub,
                                    tool=target_tool.qualified_name,
                                    policy_version=approval_policy.version,
                                )
                                return _finish(
                                    _jsonrpc_ok(
                                        req_id,
                                        {
                                            "content": [
                                                {
                                                    "type": "text",
                                                    "text": (
                                                        "Requester declined or cancelled. "
                                                        "No upstream action was taken."
                                                    ),
                                                }
                                            ],
                                            "isError": True,
                                        },
                                    ),
                                    status_label="cancelled",
                                )
                            if not (
                                action == "accept"
                                and isinstance(confirmation.get("content"), dict)
                                and confirmation["content"].get("confirm") is True
                            ):
                                message = _render_approval_summary(approval_policy, arguments) or (
                                    f"Confirm this call to {target_tool.qualified_name}. "
                                    "Review the tool call and its arguments before responding."
                                )
                                return _finish(
                                    _input_required_confirmation(req_id, request_state, message),
                                    status_label="input_required",
                                )
                            consumed = await runtime.store.consume_request_state_nonce(
                                state_payload["nonce"],
                                datetime.fromtimestamp(
                                    state_payload["expires_at"], UTC
                                ).isoformat(),
                            )
                            if not consumed:
                                return _finish(
                                    _jsonrpc_err(
                                        req_id, -32602, "This confirmation has already been used."
                                    ),
                                    status_label="error",
                                )
                            log_event(
                                "approval.requester_confirmation",
                                outcome="accepted",
                                tenant_id=identity.tenant_id,
                                user_sub=identity.sub,
                                tool=target_tool.qualified_name,
                                policy_version=approval_policy.version,
                            )
                            requester_confirmation_policy = approval_policy
                            if approval_policy.mode == "manager_approval":
                                created_at = datetime.now(UTC)
                                approval = await runtime.store.create_or_get_tool_approval(
                                    {
                                        "approval_id": uuid.uuid4().hex,
                                        "tenant_id": identity.tenant_id,
                                        "requester_sub": identity.sub,
                                        "upstream_name": target_tool.server,
                                        "tool_name": target_tool.name,
                                        "argument_digest": argument_digest,
                                        "policy_version": approval_policy.version,
                                        "approver_groups": approval_policy.approver_groups,
                                        "action_summary": _render_approval_summary(
                                            approval_policy, arguments
                                        ),
                                        "created_at": created_at.isoformat(),
                                        "expires_at": (
                                            created_at
                                            + timedelta(
                                                seconds=runtime.settings.tool_approval_ttl_seconds
                                            )
                                        ).isoformat(),
                                    }
                                )
                                result = {
                                    "approval_status": approval["status"],
                                    "approval_id": approval["approval_id"],
                                    "message": (
                                        "Requester confirmed. This action is waiting for an "
                                        "eligible approver."
                                    ),
                                }
                                log_event(
                                    "approval.manager_requested",
                                    approval_id=approval["approval_id"],
                                    tenant_id=identity.tenant_id,
                                    requester_sub=identity.sub,
                                    tool=target_tool.qualified_name,
                                    policy_version=approval_policy.version,
                                )
                                return _finish(
                                    _jsonrpc_ok(
                                        req_id,
                                        {
                                            "content": [
                                                {"type": "text", "text": result["message"]}
                                            ],
                                            "structuredContent": result,
                                            "isError": True,
                                        },
                                    ),
                                    status_label="approval_pending",
                                )

                        if (
                            approval_policy is not None
                            and approval_policy.mode == "manager_approval"
                            and manager_approval_record is not None
                            and manager_approval_record["status"] == "approved"
                        ):
                            requester_confirmation_policy = approval_policy

                    if (
                        manager_approval_record is not None
                        and manager_approval_record["status"] == "approved"
                    ):
                        claimed = await runtime.store.claim_tool_approval(
                            manager_approval_record["approval_id"],
                            identity.tenant_id,
                            identity.sub,
                            manager_approval_record["argument_digest"],
                            int(manager_approval_record["policy_version"]),
                            datetime.now(UTC).isoformat(),
                        )
                        if not claimed:
                            return _finish(
                                _jsonrpc_err(
                                    req_id,
                                    -32602,
                                    "This approved action has expired or has already been used.",
                                ),
                                status_label="approval_replay",
                            )
                    attributed_call: dict[str, Any] | None = None
                    attributed_write_claimed = False
                    attributed_is_write = False
                    if attributed_context is not None:
                        if target_tool is None or target_upstream is None:
                            return _finish(
                                _jsonrpc_err(req_id, -32602, "Attributed tool is unavailable."),
                                status_label="error",
                            )
                        try:
                            (
                                attributed_call,
                                created_call,
                            ) = await runtime.persist_attributed_call_intent(
                                identity,
                                attributed_context["execution"],
                                attributed_context["selected"],
                                target_tool,
                                request_id=attributed_context["request_id"],
                                request_digest=attributed_context["request_digest"],
                                arguments=arguments,
                            )
                        except BusinessExecutionConflict as exc:
                            return _finish(
                                _jsonrpc_err(req_id, -32602, str(exc)), status_label="error"
                            )
                        attributed_is_write = (
                            target_tool.mutating
                            or target_upstream.tool_effects.get(
                                target_tool.name,
                                "write" if target_tool.mutating else "read",
                            )
                            != "read"
                        )
                        prior_status = str(attributed_call.get("status") or "started")
                        prior_evidence = attributed_call.get("evidence", {})
                        prior_unknown = bool(
                            prior_evidence.get("outcome_unknown")
                            or prior_status == "outcome_unknown"
                        )
                        if (
                            not created_call
                            and attributed_is_write
                            and (
                                prior_unknown or prior_status in {"started", "succeeded", "pending"}
                            )
                        ):
                            message = (
                                "This write already has an unresolved or completed logical call. "
                                "Use its existing recovery/status path; the gateway will not "
                                "dispatch it again."
                            )
                            return _finish(
                                _jsonrpc_err(req_id, -32602, message), status_label="replay_blocked"
                            )
                        if not created_call and prior_unknown:
                            return _finish(
                                _jsonrpc_err(
                                    req_id,
                                    -32602,
                                    "The read outcome is unresolved; the existing call must be "
                                    "reconciled before retry.",
                                ),
                                status_label="replay_blocked",
                            )
                        if not created_call:
                            await runtime.store.update_workflow_call(
                                attributed_call["workflow_call_id"],
                                {"status": "started"},
                            )
                        if attributed_is_write:
                            lease_until = datetime.now(UTC) + timedelta(
                                seconds=target_upstream.request_timeout_seconds + 15
                            )
                            attributed_write_claimed = await runtime.store.claim_workflow_write(
                                attributed_context["execution"]["business_execution_id"],
                                attributed_call["workflow_call_id"],
                                lease_until.isoformat(),
                            )
                            if not attributed_write_claimed:
                                await runtime.store.update_workflow_call(
                                    attributed_call["workflow_call_id"],
                                    {"status": "failed", "evidence": {"write_blocked": True}},
                                )
                                return _finish(
                                    _jsonrpc_err(
                                        req_id,
                                        -32010,
                                        "Another workflow write is active or unresolved.",
                                    ),
                                    status_label="write_blocked",
                                )

                    async def finish_attributed_error(*, unknown: bool) -> None:
                        if attributed_context is None or attributed_call is None:
                            return
                        await runtime.finalize_attributed_call(
                            identity,
                            attributed_context["execution"],
                            attributed_context["selected"],
                            target_tool,
                            attributed_call,
                            result=None,
                            status="outcome_unknown" if unknown else "failed",
                            outcome_unknown=unknown,
                        )
                        if attributed_write_claimed and not unknown:
                            await runtime.store.release_workflow_write(
                                attributed_context["execution"]["business_execution_id"],
                                attributed_call["workflow_call_id"],
                            )

                    try:
                        recovery_enabled = bool(
                            target_tool is not None
                            and (recovery_upstream := runtime.registry.get(target_tool.server))
                            is not None
                            and (
                                recovery_policy := recovery_upstream.tool_recovery_policies.get(
                                    target_tool.name
                                )
                            )
                            is not None
                            and recovery_policy.enabled
                        )
                        if recovery_enabled:
                            res, recovery_status = await runtime.recovery.begin_direct_call(
                                identity,
                                tool_name=tool_name,
                                arguments=arguments,
                                mode=effective_mode,
                                allowed_servers=allowed_servers,
                                requester_confirmation_policy=requester_confirmation_policy,
                                supports_tasks=supports_tasks,
                                mode_decision_reason=mode_decision_reason,
                                mode_decision_score=mode_decision_score,
                                experiment_id=experiment_id,
                                ab_variant=ab_variant,
                                model_name=effective_model_name,
                                workflow_call_id=(
                                    attributed_call["workflow_call_id"]
                                    if attributed_call is not None
                                    else None
                                ),
                            )
                        else:
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
                                requester_confirmation_policy=requester_confirmation_policy,
                                workflow_call_id=(
                                    attributed_call["workflow_call_id"]
                                    if attributed_call is not None
                                    else None
                                ),
                                intent=effective_intent,
                                session_key=effective_session_key,
                            )
                            recovery_status = "ok" if res.get("ok") else "error"
                    except KeyError as exc:
                        await finish_attributed_error(unknown=False)
                        if (
                            manager_approval_record is not None
                            and manager_approval_record["status"] == "approved"
                        ):
                            await runtime.store.finish_tool_approval(
                                manager_approval_record["approval_id"],
                                "execution_failed",
                                datetime.now(UTC).isoformat(),
                            )
                        return _finish(_jsonrpc_err(req_id, -32601, str(exc)), status_label="error")
                    except PermissionError as exc:
                        await finish_attributed_error(unknown=False)
                        if (
                            manager_approval_record is not None
                            and manager_approval_record["status"] == "approved"
                        ):
                            await runtime.store.finish_tool_approval(
                                manager_approval_record["approval_id"],
                                "execution_failed",
                                datetime.now(UTC).isoformat(),
                            )
                        return _finish(_jsonrpc_err(req_id, -31003, str(exc)), status_label="error")
                    except (ValueError, RuntimeError) as exc:
                        await finish_attributed_error(unknown=False)
                        if (
                            manager_approval_record is not None
                            and manager_approval_record["status"] == "approved"
                        ):
                            await runtime.store.finish_tool_approval(
                                manager_approval_record["approval_id"],
                                "execution_failed",
                                datetime.now(UTC).isoformat(),
                            )
                        return _finish(_jsonrpc_err(req_id, -32602, str(exc)), status_label="error")
                    except Exception as exc:
                        await finish_attributed_error(unknown=attributed_is_write)
                        if (
                            manager_approval_record is None
                            or manager_approval_record["status"] != "approved"
                        ):
                            raise
                        await runtime.store.finish_tool_approval(
                            manager_approval_record["approval_id"],
                            "execution_failed",
                            datetime.now(UTC).isoformat(),
                        )
                        log_event(
                            "approval.execution_failed",
                            approval_id=manager_approval_record["approval_id"],
                            tenant_id=identity.tenant_id,
                            requester_sub=identity.sub,
                            tool=target_tool.qualified_name if target_tool else tool_name,
                            error_type=type(exc).__name__,
                        )
                        return _finish(
                            _jsonrpc_err(req_id, -32603, "Approved tool execution failed."),
                            status_label="error",
                        )

                    if attributed_context is not None and attributed_call is not None:
                        pending_recovery = recovery_status in {"task", "pending"}
                        result_value = res.get("result") if isinstance(res, dict) else None
                        attributed_unknown = bool(
                            pending_recovery
                            or (isinstance(res, dict) and res.get("outcome_unknown"))
                        )
                        call_status = (
                            "pending"
                            if pending_recovery
                            else "outcome_unknown"
                            if attributed_unknown
                            else "succeeded"
                            if isinstance(res, dict) and res.get("ok") is True
                            else "failed"
                        )
                        value_event = await runtime.finalize_attributed_call(
                            identity,
                            attributed_context["execution"],
                            attributed_context["selected"],
                            target_tool,
                            attributed_call,
                            result=result_value,
                            status=call_status,
                            run_id=(res.get("run_id") if isinstance(res, dict) else None),
                            sensitive_write_operation_id=(
                                res.get("sensitive_write_operation_id")
                                if isinstance(res, dict)
                                else None
                            ),
                            outcome_unknown=attributed_unknown,
                        )
                        if attributed_write_claimed and not attributed_unknown:
                            await runtime.store.release_workflow_write(
                                attributed_context["execution"]["business_execution_id"],
                                attributed_call["workflow_call_id"],
                            )
                        res = {
                            "ok": bool(isinstance(res, dict) and res.get("ok")),
                            "result": result_value,
                            "business_execution_id": attributed_context["execution"][
                                "business_execution_id"
                            ],
                            "workflow_call_id": attributed_call["workflow_call_id"],
                            "completion_recorded": bool(
                                value_event is not None
                                and value_event.get("business_execution_id")
                                == attributed_context["execution"]["business_execution_id"]
                            ),
                        }

                    if (
                        manager_approval_record is not None
                        and manager_approval_record["status"] == "approved"
                    ):
                        await runtime.store.finish_tool_approval(
                            manager_approval_record["approval_id"],
                            "completed"
                            if recovery_status == "ok"
                            else "recovery_pending"
                            if recovery_status in {"task", "pending"}
                            else "execution_failed",
                            datetime.now(UTC).isoformat(),
                        )

                    return _finish(
                        _jsonrpc_ok(
                            req_id,
                            res
                            if recovery_status in {"task", "pending"}
                            else {
                                "content": [{"type": "text", "text": json.dumps(res, default=str)}],
                                "structuredContent": res,
                                "isError": not res.get("ok"),
                            },
                        ),
                        status_label=recovery_status,
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

    def _integration_definition_tenant(identity: CallerIdentity | None) -> str:
        scoped = _resolve_audit_tenant_scope(identity, None)
        tenant = scoped or (identity.tenant_id if identity is not None else None)
        if not tenant:
            tenant = next(iter(runtime.settings.allowed_tenants), None)
        if not tenant:
            raise HTTPException(status_code=401, detail="Tenant identity is required.")
        return tenant

    def _reject_integration_definition_tenant_override(request: Request) -> None:
        if "tenant_id" in request.query_params:
            raise HTTPException(
                status_code=400,
                detail="tenant_id cannot be selected on integration-definition routes.",
            )

    def _integration_definition_content_digest(definition: IntegrationDefinition) -> str:
        authored = definition.model_dump(mode="json", exclude_none=True)
        authored.pop("publication", None)
        integration = authored.get("integration")
        if isinstance(integration, dict):
            integration.pop("revision", None)
            integration.pop("status", None)
        return canonical_json_digest(authored)

    async def _validate_admin_integration_definition(
        definition: IntegrationDefinition,
        upstream_name: str,
        identity: CallerIdentity | None,
    ) -> str:
        if definition.integration.upstream_name != upstream_name:
            raise HTTPException(
                status_code=422,
                detail="definition integration upstream does not match the route upstream.",
            )
        if runtime.registry.get(upstream_name) is None:
            raise HTTPException(status_code=404, detail=f"Upstream '{upstream_name}' not found.")
        catalog_identity = identity or runtime._dev_discovery_identity()
        if catalog_identity is None:
            raise HTTPException(
                status_code=401,
                detail="An authenticated administrator is required to validate the catalog.",
            )
        await runtime.ensure_catalog_for_identity(catalog_identity)
        catalog_names = {
            tool.qualified_name
            for upstream in runtime.registry.list_upstreams()
            for tool in runtime.catalog.get_server_tools(upstream.name)
        }
        try:
            validate_definition_against_catalog(
                definition,
                runtime.catalog,
                reserved_tool_names=catalog_names,
            )
        except (KeyError, ValueError) as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        return _integration_definition_content_digest(definition)

    async def _definition_revision(
        tenant_id: str,
        upstream_name: str,
        revision: str,
    ) -> dict[str, Any]:
        row = await runtime.store.get_integration_definition_version(
            tenant_id, upstream_name, revision
        )
        if row is None:
            raise HTTPException(
                status_code=404, detail="Integration definition revision not found."
            )
        try:
            definition = IntegrationDefinition.model_validate(row["definition"])
        except ValueError as exc:
            raise HTTPException(
                status_code=409,
                detail="Stored integration definition is invalid and cannot be published.",
            ) from exc
        return {**row, "definition_model": definition}

    def _integration_definition_api_row(
        row: dict[str, Any], *, is_current: bool = False
    ) -> dict[str, Any]:
        view = copy.deepcopy(row)
        definition = view.get("definition")
        if isinstance(definition, dict):
            integration = definition.get("integration")
            if isinstance(integration, dict):
                try:
                    integration["revision"] = int(view["revision"])
                except (KeyError, TypeError, ValueError):
                    pass
                if is_current:
                    integration["status"] = "published"
        view["is_current"] = is_current
        return view

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
            window: str | None = Query(default=None),
            authorization: str | None = Header(default=None),
            mcpcodeaway_admin_session: str | None = Cookie(default=None),
        ) -> dict[str, Any]:
            identity = await _authorize_control_plane(
                authorization, mcpcodeaway_admin_session, require_admin_role=False
            )
            effective_tenant = _resolve_audit_tenant_scope(identity, tenant_id)
            window_hours = {"1h": 1, "24h": 24, "7d": 168, "30d": 720}.get(window or "")
            return await runtime.telemetry.analytics_report(
                limit=limit,
                tenant_id=effective_tenant,
                group_filter=group,
                user_filter=user,
                upstream_filter=upstream,
                model_name=model,
                model_pricing=await runtime.merged_model_pricing(),
                window_hours=window_hours,
            )

    async def _catalog_tool_for_admin(name: str, tool_name: str) -> DiscoveredTool:
        await runtime.sync_catalog_if_stale()
        tool = runtime.catalog.get_tool(tool_name)
        if tool is None or tool.server != name or runtime.registry.get(name) is None:
            raise HTTPException(
                status_code=404, detail="Tool is no longer in this upstream's catalog."
            )
        return tool

    if features.admin_read:

        @app.get("/admin/business-value/report")
        async def admin_business_value_report(
            since: str | None = Query(default=None, max_length=64),
            limit: int = Query(default=1000, ge=1, le=10_000),
            tenant_id: str | None = Query(default=None, min_length=1, max_length=120),
            authorization: str | None = Header(default=None),
            mcpcodeaway_admin_session: str | None = Cookie(default=None),
        ) -> dict[str, Any]:
            identity = await _authorize_control_plane(
                authorization,
                mcpcodeaway_admin_session,
                require_admin_role=True,
                allow_viewer=True,
            )
            if identity is None:
                if tenant_id is not None:
                    raise HTTPException(
                        status_code=401,
                        detail="Tenant switching requires an authenticated administrator.",
                    )
                effective_tenant = next(iter(runtime.settings.allowed_tenants), None)
            else:
                effective_tenant = _resolve_audit_tenant_scope(identity, tenant_id)
                effective_tenant = effective_tenant or identity.tenant_id
            if not effective_tenant:
                raise HTTPException(status_code=401, detail="Tenant identity is required.")
            return await runtime.telemetry.business_value_report(
                tenant_id=effective_tenant,
                since=since,
                limit=limit,
                reporting_currency=await runtime.get_reporting_currency(),
            )

        @app.get("/admin/business-value/settings")
        async def admin_business_value_settings(
            authorization: str | None = Header(default=None),
            mcpcodeaway_admin_session: str | None = Cookie(default=None),
        ) -> dict[str, Any]:
            await _authorize_control_plane(
                authorization,
                mcpcodeaway_admin_session,
                require_admin_role=True,
                allow_viewer=True,
            )
            return {
                "reporting_currency": await runtime.get_reporting_currency(),
                "env_default_currency": runtime.settings.default_currency,
                "supported_presets": ["USD", "EUR", "GBP", "BRL", "CAD", "AUD", "JPY", "CHF"],
            }

        @app.get("/admin/onboarding/prompt-pack")
        async def admin_onboarding_prompt_pack(
            request: Request,
            upstream_name: str | None = Query(default=None, min_length=1, max_length=120),
            authorization: str | None = Header(default=None),
            mcpcodeaway_admin_session: str | None = Cookie(default=None),
        ) -> dict[str, Any]:
            identity = await _authorize_control_plane(
                authorization,
                mcpcodeaway_admin_session,
                require_admin_role=True,
                allow_viewer=True,
            )
            _reject_integration_definition_tenant_override(request)
            tenant_id = _integration_definition_tenant(identity)
            tools = []
            candidates: list[dict[str, Any]] = []
            if upstream_name:
                catalog_identity = identity or runtime._dev_discovery_identity()
                if catalog_identity is None:
                    raise HTTPException(
                        status_code=401,
                        detail="An authenticated administrator is required to inspect the catalog.",
                    )
                await runtime.ensure_catalog_for_identity(catalog_identity)
                tools = runtime.catalog.get_server_tools(upstream_name)
                if hasattr(runtime.store, "list_discovered_workflow_candidates"):
                    candidates = await runtime.store.list_discovered_workflow_candidates(
                        tenant_id, upstream_name=upstream_name, status="pending_review", limit=50
                    )
            public_url = runtime.settings.gateway_public_url or str(request.base_url).rstrip("/")
            return build_onboarding_prompt_pack(
                gateway_public_url=public_url,
                upstream_name=upstream_name,
                tools=tools,
                discovered_candidates=candidates,
            )

        @app.get("/admin/workflows/discovered")
        async def admin_list_discovered_workflows(
            request: Request,
            upstream_name: str | None = Query(default=None, min_length=1, max_length=120),
            status: str | None = Query(default=None, max_length=40),
            limit: int = Query(default=100, ge=1, le=500),
            authorization: str | None = Header(default=None),
            mcpcodeaway_admin_session: str | None = Cookie(default=None),
        ) -> dict[str, Any]:
            identity = await _authorize_control_plane(
                authorization,
                mcpcodeaway_admin_session,
                require_admin_role=True,
                allow_viewer=True,
            )
            _reject_integration_definition_tenant_override(request)
            tenant_id = _integration_definition_tenant(identity)
            items = (
                await runtime.store.list_discovered_workflow_candidates(
                    tenant_id, upstream_name=upstream_name, status=status, limit=limit
                )
                if hasattr(runtime.store, "list_discovered_workflow_candidates")
                else []
            )
            return {"count": len(items), "candidates": items}

        @app.get("/admin/workflows/episodes")
        async def admin_list_workflow_episodes(
            request: Request,
            status: str | None = Query(default=None, max_length=40),
            limit: int = Query(default=100, ge=1, le=500),
            authorization: str | None = Header(default=None),
            mcpcodeaway_admin_session: str | None = Cookie(default=None),
        ) -> dict[str, Any]:
            identity = await _authorize_control_plane(
                authorization,
                mcpcodeaway_admin_session,
                require_admin_role=True,
                allow_viewer=True,
            )
            _reject_integration_definition_tenant_override(request)
            tenant_id = _integration_definition_tenant(identity)
            episodes = (
                await runtime.store.list_workflow_episodes(tenant_id, status=status, limit=limit)
                if hasattr(runtime.store, "list_workflow_episodes")
                else []
            )
            return {"count": len(episodes), "episodes": episodes}

        @app.get("/admin/integration-definitions/{upstream_name}")
        async def get_admin_integration_definitions(
            upstream_name: str,
            request: Request,
            authorization: str | None = Header(default=None),
            mcpcodeaway_admin_session: str | None = Cookie(default=None),
        ) -> dict[str, Any]:
            identity = await _authorize_control_plane(
                authorization,
                mcpcodeaway_admin_session,
                require_admin_role=True,
                allow_viewer=True,
            )
            _reject_integration_definition_tenant_override(request)
            tenant_id = _integration_definition_tenant(identity)
            versions = await runtime.store.list_integration_definition_versions(
                tenant_id, upstream_name
            )
            current = await runtime.store.get_current_published_integration_definition(
                tenant_id, upstream_name
            )
            current_revision = current["revision"] if current else None
            return {
                "upstream_name": upstream_name,
                "current_revision": current_revision,
                "current": (
                    _integration_definition_api_row(current, is_current=True) if current else None
                ),
                "versions": [
                    _integration_definition_api_row(
                        row, is_current=row["revision"] == current_revision
                    )
                    for row in versions
                ],
            }

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
                upstream_health=runtime.upstream_health.list_statuses(),
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
                        upstream_health=runtime.upstream_health.list_statuses(),
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
                upstream_health=runtime.upstream_health.list_statuses(),
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
                "upstreams": [
                    {
                        **u.model_dump(exclude={"openapi_spec"}),
                        "tools": [
                            t.qualified_name for t in runtime.catalog.get_server_tools(u.name)
                        ],
                    }
                    for u in page_items
                ],
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

        @app.get("/admin/upstreams/{name}/tools/{tool_name}/guidance")
        async def get_admin_tool_guidance(
            name: str,
            tool_name: str,
            authorization: str | None = Header(default=None),
            mcpcodeaway_admin_session: str | None = Cookie(default=None),
        ) -> dict[str, Any]:
            caller = await _authorize_control_plane(
                authorization,
                mcpcodeaway_admin_session,
                require_admin_role=True,
                allow_viewer=True,
            )
            tool = await _catalog_tool_for_admin(name, tool_name)
            entries = await runtime.store.list_tool_guidance(
                (caller.tenant_id or "") if caller else ""
            )
            upstream = runtime.registry.get(name)
            configured_guidance = (
                upstream.tool_guidance.get(tool.name).model_dump()
                if upstream and tool.name in upstream.tool_guidance
                else None
            )
            saved_guidance = entries.get(tool_name)
            guidance = configured_guidance
            if saved_guidance:
                guidance = {
                    **(configured_guidance or {}),
                    **saved_guidance,
                    "parameter_hints": {
                        **((configured_guidance or {}).get("parameter_hints") or {}),
                        **(saved_guidance.get("parameter_hints") or {}),
                    },
                }
            approval_policy = upstream.tool_approval_policies.get(tool.name, ToolApprovalPolicy())
            effect = (upstream.tool_effects.get(tool.name) if upstream else None) or (
                "write" if tool.mutating else "read"
            )
            return {
                "description": tool.description,
                "input_schema": tool.input_schema,
                "output_schema": tool.output_schema,
                "schema_sha256": compute_schema_digest([tool]),
                "guidance": guidance,
                "mutating": tool.mutating,
                "effect": effect,
                "approval_policy": approval_policy.model_dump(),
                "can_edit": features.admin_write
                and runtime.settings.dynamic_upstreams_permitted
                and (caller is None or bool(set(caller.roles) & set(runtime.settings.admin_roles))),
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
            return {"admin_overrides": saved}

    async def _inspect_candidate(
        cfg: UpstreamConfig, authorization: str | None
    ) -> tuple[dict[str, Any], dict[str, Any]]:
        if runtime.settings.ssrf_protection_enabled:
            try:
                for candidate_url in (cfg.url, cfg.openapi_spec_url, cfg.health_check.url):
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
            except UserCredentialRequiredError:
                headers = {}
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
        except httpx.HTTPStatusError as exc:
            status = exc.response.status_code
            detail = f"Schema inspection received HTTP {status} from upstream '{cfg.name}'."
            challenge = exc.response.headers.get("WWW-Authenticate", "")
            scheme = challenge.split(maxsplit=1)[0] if challenge else ""
            if scheme and re.fullmatch(r"[A-Za-z][A-Za-z0-9_-]{0,31}", scheme):
                detail += f" The upstream advertised a {scheme} authentication challenge."
            if status == 401:
                detail += " Configure the upstream credential type and retry inspection."
            elif status == 403:
                detail += " The inspection identity lacks access; use an authorized identity."
            raise HTTPException(status_code=400, detail=detail) from exc
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

    @app.get("/admin/operations")
    async def list_admin_operations(
        status: str = Query(
            default="all",
            pattern="^(all|pending|working|completed|failed|needs_attention|cancelled)$",
        ),
        limit: int = Query(default=100, ge=1, le=200),
        authorization: str | None = Header(default=None),
        mcpcodeaway_admin_session: str | None = Cookie(default=None),
    ) -> dict[str, Any]:
        identity = await _authorize_control_plane(
            authorization,
            mcpcodeaway_admin_session,
            require_admin_role=True,
            allow_viewer=True,
            allowed_roles=set(runtime.settings.admin_roles) | set(runtime.settings.viewer_roles),
        )
        if identity is None:
            return {"operations": [], "count": 0}
        statuses = None if status == "all" else [status]
        records = await runtime.store.list_operations(
            identity.tenant_id,
            statuses=statuses,
            limit=limit,
        )
        visible = []
        for record in records:
            visible.append(
                {
                    "operation_id": record["operation_id"],
                    "requester_sub": record["requester_sub"],
                    "client_id": record.get("client_id") or None,
                    "upstream_name": record["upstream_name"],
                    "tool_name": record["tool_name"],
                    "status": record["status"],
                    "attempt_count": int(record["attempt_count"]),
                    "max_attempts": int(record["max_attempts"]),
                    "created_at": record["created_at"],
                    "updated_at": record["updated_at"],
                    "recovery_deadline": record["recovery_deadline"],
                    "last_error_type": record.get("last_error_type"),
                    "last_error": OutputRedactor.redact_text(str(record.get("last_error") or ""))[
                        :500
                    ]
                    or None,
                }
            )
        return {"operations": visible, "count": len(visible)}

    @app.get("/admin/operations/metrics")
    async def get_admin_operation_metrics(
        authorization: str | None = Header(default=None),
        mcpcodeaway_admin_session: str | None = Cookie(default=None),
    ) -> dict[str, Any]:
        identity = await _authorize_control_plane(
            authorization,
            mcpcodeaway_admin_session,
            require_admin_role=True,
            allow_viewer=True,
            allowed_roles=set(runtime.settings.admin_roles) | set(runtime.settings.viewer_roles),
        )
        if identity is None or not identity.tenant_id:
            return {
                "by_status": {
                    "pending": 0,
                    "working": 0,
                    "completed": 0,
                    "failed": 0,
                    "needs_attention": 0,
                    "cancelled": 0,
                },
                "recent_by_status": {"completed": 0, "failed": 0, "cancelled": 0},
                "attempts": 0,
                "open": 0,
                "needs_attention": 0,
                "completed_24h": 0,
                "failed_24h": 0,
                "cancelled_24h": 0,
                "failure_rate_24h_pct": None,
                "oldest_open_at": None,
                "by_upstream_24h": [],
                "upstream_health": [],
            }
        since = (datetime.now(UTC) - timedelta(hours=24)).isoformat()
        metrics = await runtime.store.get_operation_metrics(identity.tenant_id, since=since)
        status_counts = metrics["by_status"]
        recent_counts = metrics["recent_by_status"]
        open_count = status_counts.get("pending", 0) + status_counts.get("working", 0)
        terminal_recent = sum(
            recent_counts.get(status, 0) for status in ("completed", "failed", "cancelled")
        )
        metrics.update(
            {
                "open": open_count,
                "needs_attention": status_counts.get("needs_attention", 0),
                "completed_24h": recent_counts.get("completed", 0),
                "failed_24h": recent_counts.get("failed", 0),
                "cancelled_24h": recent_counts.get("cancelled", 0),
                "failure_rate_24h_pct": round(
                    recent_counts.get("failed", 0) * 100 / terminal_recent, 1
                )
                if terminal_recent
                else None,
                "upstream_health": [
                    {
                        "server": row["server"],
                        "status": row["status"],
                        "checked_at": row["checked_at"],
                        "error_type": row.get("error_type"),
                        "consecutive_failures": int(row.get("consecutive_failures", 0)),
                    }
                    for row in runtime.upstream_health.list_statuses()
                ],
            }
        )
        return metrics

    if features.approval_api:

        def _approval_public_record(record: dict[str, Any]) -> dict[str, Any]:
            return {
                "approval_id": record["approval_id"],
                "upstream_name": record["upstream_name"],
                "tool_name": record["tool_name"],
                "action_summary": record["action_summary"],
                "requester_sub": record["requester_sub"],
                "approver_groups": record["approver_groups"],
                "status": record["status"],
                "created_at": record["created_at"],
                "expires_at": record["expires_at"],
                "decided_by": record.get("decided_by"),
                "decided_at": record.get("decided_at"),
                "completed_at": record.get("completed_at"),
            }

        def _approval_roles() -> set[str]:
            return set(runtime.settings.admin_roles) | set(runtime.settings.approval_roles)

        def _is_approval_group_member(identity: CallerIdentity, record: dict[str, Any]) -> bool:
            return bool(set(identity.groups) & set(record["approver_groups"]))

        @app.get("/admin/approvals")
        async def list_admin_tool_approvals(
            status: str = Query(default="pending", pattern="^(pending|approved|denied|all)$"),
            limit: int = Query(default=100, ge=1, le=200),
            authorization: str | None = Header(default=None),
            mcpcodeaway_admin_session: str | None = Cookie(default=None),
        ) -> dict[str, Any]:
            identity = await _authorize_control_plane(
                authorization,
                mcpcodeaway_admin_session,
                require_admin_role=True,
                allow_viewer=True,
                allowed_roles=(
                    set(runtime.settings.admin_roles)
                    | set(runtime.settings.viewer_roles)
                    | set(runtime.settings.approval_roles)
                ),
            )
            if identity is None:
                return {"approvals": [], "count": 0}
            statuses = ["pending", "approved", "denied"] if status == "all" else [status]
            records = await runtime.store.list_tool_approvals(
                identity.tenant_id, statuses=statuses, limit=limit
            )
            is_admin = bool(set(identity.roles) & set(runtime.settings.admin_roles))
            is_approver = bool(set(identity.roles) & set(runtime.settings.approval_roles))
            visible = [
                _approval_public_record(record)
                for record in records
                if _is_approval_group_member(identity, record)
                and (is_admin or is_approver or record["requester_sub"] == identity.sub)
            ]
            return {"approvals": visible, "count": len(visible)}

        @app.post("/admin/approvals/{approval_id}/decision")
        async def decide_admin_tool_approval(
            approval_id: str,
            body: ToolApprovalDecisionRequest,
            authorization: str | None = Header(default=None),
            mcpcodeaway_admin_session: str | None = Cookie(default=None),
        ) -> dict[str, Any]:
            identity = await _authorize_control_plane(
                authorization,
                mcpcodeaway_admin_session,
                require_admin_role=True,
                allowed_roles=_approval_roles(),
            )
            if identity is None:
                raise HTTPException(status_code=401, detail="Approval identity required")
            record = await runtime.store.get_tool_approval(approval_id, identity.tenant_id)
            if record is None:
                raise HTTPException(status_code=404, detail="Approval request not found")
            if record["requester_sub"] == identity.sub:
                raise HTTPException(
                    status_code=403, detail="Requesters cannot approve their own action"
                )
            if not _is_approval_group_member(identity, record):
                raise HTTPException(status_code=403, detail="Approver group membership required")
            if not record["approver_groups"]:
                raise HTTPException(
                    status_code=409, detail="Approval policy has no eligible approver group"
                )
            upstream = runtime.registry.get(record["upstream_name"])
            active_policy = (
                upstream.tool_approval_policies.get(record["tool_name"])
                if upstream is not None
                else None
            )
            if (
                active_policy is None
                or active_policy.mode != "manager_approval"
                or active_policy.version != record["policy_version"]
                or active_policy.approver_groups != record["approver_groups"]
            ):
                await runtime.store.invalidate_tool_approvals(
                    record["upstream_name"],
                    record["tool_name"],
                    active_policy.version + 1 if active_policy is not None else -1,
                    datetime.now(UTC).isoformat(),
                )
                raise HTTPException(
                    status_code=409,
                    detail="Approval policy changed. The requester must submit a new action.",
                )
            timestamp = datetime.now(UTC).isoformat()
            decided = await runtime.store.decide_tool_approval(
                approval_id,
                identity.tenant_id,
                identity.sub,
                "approved" if body.decision == "approve" else "denied",
                timestamp,
            )
            if decided is None or decided["status"] == "pending":
                raise HTTPException(
                    status_code=409,
                    detail="Approval is expired or has already received a decision.",
                )
            if decided.get("decided_by") != identity.sub:
                raise HTTPException(
                    status_code=409,
                    detail="Approval is expired or has already received a decision.",
                )
            log_event(
                "approval.manager_decision",
                approval_id=approval_id,
                outcome=decided["status"],
                tenant_id=identity.tenant_id,
                requester_sub=decided["requester_sub"],
                approver_sub=identity.sub,
                tool=f"{decided['upstream_name']}__{decided['tool_name']}",
            )
            return {"approval": _approval_public_record(decided)}

    if features.admin_write:

        @app.post("/admin/integration-definitions/validate")
        async def validate_admin_integration_definition(
            definition: IntegrationDefinition,
            request: Request,
            authorization: str | None = Header(default=None),
            mcpcodeaway_admin_session: str | None = Cookie(default=None),
        ) -> dict[str, Any]:
            identity = await _authorize_control_plane(
                authorization, mcpcodeaway_admin_session, require_admin_role=True
            )
            _reject_integration_definition_tenant_override(request)
            upstream_name = definition.integration.upstream_name
            digest = await _validate_admin_integration_definition(
                definition, upstream_name, identity
            )
            return {
                "valid": True,
                "upstream_name": upstream_name,
                "definition_digest": digest,
                "tool_count": len(definition.tool_refs),
                "workflow_count": len(definition.workflows),
                "standalone_tool_value_count": len(definition.standalone_tool_values),
            }

        @app.post("/admin/integration-definitions/drafts")
        async def create_admin_integration_definition_draft(
            body: IntegrationDefinitionDraftRequest,
            request: Request,
            authorization: str | None = Header(default=None),
            mcpcodeaway_admin_session: str | None = Cookie(default=None),
        ) -> dict[str, Any]:
            identity = await _authorize_control_plane(
                authorization, mcpcodeaway_admin_session, require_admin_role=True
            )
            _reject_integration_definition_tenant_override(request)
            definition = body.definition.model_copy(
                update={
                    "integration": body.definition.integration.model_copy(
                        update={"status": "draft", "revision": 1}
                    ),
                    "publication": None,
                }
            )
            upstream_name = definition.integration.upstream_name
            if runtime.registry.get(upstream_name) is None:
                raise HTTPException(
                    status_code=404, detail=f"Upstream '{upstream_name}' not found."
                )
            tenant_id = _integration_definition_tenant(identity)
            digest = _integration_definition_content_digest(definition)
            saved = await runtime.store.save_integration_definition_version(
                {
                    "tenant_id": tenant_id,
                    "upstream_name": upstream_name,
                    "definition_digest": digest,
                    "definition": definition.model_dump(mode="json", exclude_none=True),
                }
            )
            return {
                "saved": True,
                "tenant_id": tenant_id,
                "upstream_name": upstream_name,
                "revision": saved["revision"],
                "definition_digest": saved["definition_digest"],
                "validator_version": "1.0",
                "definition": _integration_definition_api_row(saved)["definition"],
            }

        @app.post("/admin/integration-definitions/{upstream_name}/publish")
        async def publish_admin_integration_definition(
            upstream_name: str,
            body: IntegrationDefinitionRevisionRequest,
            request: Request,
            authorization: str | None = Header(default=None),
            mcpcodeaway_admin_session: str | None = Cookie(default=None),
        ) -> dict[str, Any]:
            identity = await _authorize_control_plane(
                authorization, mcpcodeaway_admin_session, require_admin_role=True
            )
            _reject_integration_definition_tenant_override(request)
            tenant_id = _integration_definition_tenant(identity)
            row = await _definition_revision(tenant_id, upstream_name, body.revision)
            definition: IntegrationDefinition = row["definition_model"]
            digest = await _validate_admin_integration_definition(
                definition, upstream_name, identity
            )
            if digest != row["definition_digest"]:
                raise HTTPException(
                    status_code=409,
                    detail="Stored definition digest does not match its authored content.",
                )
            current = await runtime.store.get_current_published_integration_definition(
                tenant_id, upstream_name
            )
            previous_revision = current["revision"] if current else None
            event = {
                "event_id": uuid.uuid4().hex,
                "tenant_id": tenant_id,
                "upstream_name": upstream_name,
                "action": "publish",
                "revision": body.revision,
                "actor_sub": identity.sub if identity else "local-admin",
                "created_at": datetime.now(UTC).isoformat(),
                "evidence": {
                    "previous_revision": previous_revision,
                    "target_revision": body.revision,
                },
            }
            try:
                await runtime.store.append_integration_definition_publication(event)
            except (BusinessExecutionConflict, ValueError) as exc:
                raise HTTPException(status_code=409, detail=str(exc)) from exc
            return {
                "published": True,
                "tenant_id": tenant_id,
                "upstream_name": upstream_name,
                "revision": body.revision,
                "previous_revision": previous_revision,
                "definition_digest": digest,
            }

        @app.post("/admin/integration-definitions/{upstream_name}/rollback")
        async def rollback_admin_integration_definition(
            upstream_name: str,
            body: IntegrationDefinitionRevisionRequest,
            request: Request,
            authorization: str | None = Header(default=None),
            mcpcodeaway_admin_session: str | None = Cookie(default=None),
        ) -> dict[str, Any]:
            identity = await _authorize_control_plane(
                authorization, mcpcodeaway_admin_session, require_admin_role=True
            )
            _reject_integration_definition_tenant_override(request)
            tenant_id = _integration_definition_tenant(identity)
            current = await runtime.store.get_current_published_integration_definition(
                tenant_id, upstream_name
            )
            if current is None:
                raise HTTPException(
                    status_code=409,
                    detail="Cannot roll back an integration with no current publication.",
                )
            if current["revision"] == body.revision:
                raise HTTPException(
                    status_code=409, detail="Rollback target is already the current revision."
                )
            row = await _definition_revision(tenant_id, upstream_name, body.revision)
            definition: IntegrationDefinition = row["definition_model"]
            digest = await _validate_admin_integration_definition(
                definition, upstream_name, identity
            )
            if digest != row["definition_digest"]:
                raise HTTPException(
                    status_code=409,
                    detail="Stored definition digest does not match its authored content.",
                )
            event = {
                "event_id": uuid.uuid4().hex,
                "tenant_id": tenant_id,
                "upstream_name": upstream_name,
                "action": "rollback",
                "revision": body.revision,
                "actor_sub": identity.sub if identity else "local-admin",
                "created_at": datetime.now(UTC).isoformat(),
                "evidence": {
                    "previous_revision": current["revision"],
                    "target_revision": body.revision,
                },
            }
            try:
                await runtime.store.append_integration_definition_publication(event)
            except (BusinessExecutionConflict, ValueError) as exc:
                raise HTTPException(status_code=409, detail=str(exc)) from exc
            return {
                "rolled_back": True,
                "tenant_id": tenant_id,
                "upstream_name": upstream_name,
                "revision": body.revision,
                "previous_revision": current["revision"],
                "definition_digest": digest,
            }

        @app.post("/admin/integration-definitions/{upstream_name}/discover")
        async def discover_admin_workflows(
            upstream_name: str,
            request: Request,
            body: dict[str, Any] | None = None,
            authorization: str | None = Header(default=None),
            mcpcodeaway_admin_session: str | None = Cookie(default=None),
        ) -> dict[str, Any]:
            identity = await _authorize_control_plane(
                authorization, mcpcodeaway_admin_session, require_admin_role=True
            )
            _reject_integration_definition_tenant_override(request)
            tenant_id = _integration_definition_tenant(identity)
            if runtime.registry.get(upstream_name) is None:
                raise HTTPException(
                    status_code=404, detail=f"Upstream '{upstream_name}' not found."
                )
            catalog_identity = identity or runtime._dev_discovery_identity()
            if catalog_identity is None:
                raise HTTPException(
                    status_code=401,
                    detail="An authenticated administrator is required to inspect the catalog.",
                )
            await runtime.ensure_catalog_for_identity(catalog_identity)
            tools = runtime.catalog.get_server_tools(upstream_name)
            schema_candidates = synthesize_workflows_from_catalog(upstream_name, tools)
            runs = await runtime.store.list_runs(limit=300, tenant_id=tenant_id)
            min_support = int((body or {}).get("min_support", 2))
            mined_candidates = mine_workflows_from_runs(
                upstream_name, runs, min_support=max(1, min_support)
            )
            published_fps: set[str] = set()
            current_pub = await runtime.store.get_current_published_integration_definition(
                tenant_id, upstream_name
            )
            if current_pub and isinstance(current_pub.get("definition"), dict):
                for existing_wf in current_pub["definition"].get("workflows", []):
                    comp = existing_wf.get("completion") or {}
                    if comp.get("tool") and existing_wf.get("participating_tools"):
                        published_fps.add(
                            compute_workflow_fingerprint(
                                upstream_name=upstream_name,
                                participating_tools=existing_wf.get("participating_tools") or [],
                                completion_tool=str(comp.get("tool") or ""),
                                completion_kind=str(comp.get("kind") or "terminal_tool_result"),
                            )
                        )
            blocked_fps: set[str] = set()
            if hasattr(runtime.store, "list_discovered_workflow_candidates"):
                prior_cands = await runtime.store.list_discovered_workflow_candidates(
                    tenant_id, upstream_name=upstream_name, limit=500
                )
                for item in prior_cands:
                    if item.get("status") in {"dismissed", "promoted"}:
                        blocked_fps.add(str(item.get("fingerprint") or item.get("workflow_slug")))

            saved_candidates: list[dict[str, Any]] = []
            seen_fps: set[str] = set()
            for draft in [*schema_candidates, *mined_candidates]:
                comp = draft.get("completion") or {}
                fp = str(
                    draft.get("fingerprint")
                    or compute_workflow_fingerprint(
                        upstream_name=upstream_name,
                        participating_tools=draft.get("participating_tools") or [],
                        completion_tool=str(comp.get("tool") or ""),
                        completion_kind=str(comp.get("kind") or "terminal_tool_result"),
                    )
                )
                if fp in published_fps or fp in blocked_fps or fp in seen_fps:
                    continue
                seen_fps.add(fp)
                if hasattr(runtime.store, "upsert_discovered_workflow_candidate"):
                    saved = await runtime.store.upsert_discovered_workflow_candidate(
                        {
                            "tenant_id": tenant_id,
                            "upstream_name": upstream_name,
                            "workflow_slug": draft["id"],
                            "fingerprint": fp,
                            "origin": draft.get("origin", "auto_schema"),
                            "status": "pending_review",
                            "observed_count": int(draft.get("observed_count", 1)),
                            "verified_count": 0,
                            "sample_intents": draft.get("intent_examples", []),
                            "draft_definition": {**draft, "fingerprint": fp},
                        }
                    )
                    saved_candidates.append(saved)
            saved_candidates.sort(
                key=lambda item: (
                    -int(item.get("verified_count", 0)),
                    -int(item.get("observed_count", 0)),
                )
            )
            return {
                "upstream_name": upstream_name,
                "discovered_count": len(saved_candidates),
                "candidates": saved_candidates,
            }

        @app.post("/admin/workflows/discovered/{candidate_id}/promote")
        async def promote_discovered_workflow(
            candidate_id: str,
            body: dict[str, Any],
            request: Request,
            authorization: str | None = Header(default=None),
            mcpcodeaway_admin_session: str | None = Cookie(default=None),
        ) -> dict[str, Any]:
            identity = await _authorize_control_plane(
                authorization, mcpcodeaway_admin_session, require_admin_role=True
            )
            _reject_integration_definition_tenant_override(request)
            tenant_id = _integration_definition_tenant(identity)
            if not hasattr(runtime.store, "get_discovered_workflow_candidate"):
                raise HTTPException(status_code=501, detail="Candidate store unavailable.")
            candidate = await runtime.store.get_discovered_workflow_candidate(
                tenant_id, candidate_id
            )
            if candidate is None:
                raise HTTPException(status_code=404, detail="Discovered candidate not found.")
            try:
                minutes = float(body["manual_baseline_minutes"])
            except (KeyError, TypeError, ValueError) as exc:
                raise HTTPException(
                    status_code=422,
                    detail="manual_baseline_minutes is required to promote a workflow.",
                ) from exc
            if minutes <= 0:
                raise HTTPException(
                    status_code=422, detail="manual_baseline_minutes must be greater than zero."
                )
            upstream_name = candidate["upstream_name"]
            if bool(body.get("mark_promoted_only")):
                await runtime.store.update_discovered_workflow_candidate_status(
                    tenant_id, candidate_id, "promoted", datetime.now(UTC).isoformat()
                )
                return {
                    "promoted": True,
                    "candidate_id": candidate_id,
                    "upstream_name": upstream_name,
                    "revision": None,
                    "published": False,
                }
            catalog_identity = identity or runtime._dev_discovery_identity()
            if catalog_identity is None:
                raise HTTPException(
                    status_code=401,
                    detail="An authenticated administrator is required to inspect the catalog.",
                )
            await runtime.ensure_catalog_for_identity(catalog_identity)
            tools = runtime.catalog.get_server_tools(upstream_name)
            tool_by_name = {t.qualified_name: t for t in tools}

            current = await runtime.store.get_current_published_integration_definition(
                tenant_id, upstream_name
            )
            base_def: dict[str, Any] = (
                json.loads(json.dumps(current["definition"]))
                if current
                else {
                    "manifest_version": "1.0",
                    "integration": {
                        "id": re.sub(r"[^a-z0-9]+", "_", upstream_name.lower()).strip("_")
                        or "integration",
                        "display_name": upstream_name,
                        "upstream_name": upstream_name,
                        "status": "draft",
                        "revision": 1,
                    },
                    "tool_refs": [],
                    "experiences": [],
                    "workflows": [],
                    "standalone_tool_values": [],
                    "publication": None,
                }
            )
            base_def["integration"]["status"] = "draft"
            base_def["integration"]["revision"] = 1
            base_def["publication"] = None

            wf_draft = dict(candidate["draft_definition"])
            for transient_key in (
                "tool_refs",
                "needs_baseline_confirmation",
                "observed_count",
                "verified_count",
                "fingerprint",
                "origin",
            ):
                wf_draft.pop(transient_key, None)
            completion_cfg = dict(wf_draft.get("completion") or {})
            terminal_name = str(completion_cfg.get("tool") or "")
            success_when = dict(completion_cfg.get("success_when") or {})
            if not success_when.get("path"):
                completion_cfg["success_when"] = pick_schema_predicate(
                    tool_by_name.get(terminal_name)
                )
            if (
                terminal_name in tool_by_name
                and tool_by_name[terminal_name].mutating
                and "require_sensitive_write_journal_status" not in completion_cfg
            ):
                completion_cfg["require_sensitive_write_journal_status"] = "succeeded"
            wf_draft["completion"] = completion_cfg
            wf_draft["business_value"] = _apply_baseline_updates(
                {
                    "manual_baseline_minutes": minutes,
                    "baseline_source": str(body.get("baseline_source") or "administrator_estimate"),
                    "confidence": str(body.get("confidence") or "unvalidated"),
                    "effective_from": str(
                        body.get("effective_from") or datetime.now(UTC).date().isoformat()
                    ),
                },
                body,
                await runtime.get_reporting_currency(),
            )
            existing_wfs = [
                w for w in base_def.get("workflows", []) if w.get("id") != wf_draft["id"]
            ]
            existing_wfs.append(wf_draft)
            base_def["workflows"] = existing_wfs
            needed_tools = {t for wf in existing_wfs for t in wf.get("participating_tools", [])} | {
                e.get("tool") for e in base_def.get("experiences", []) if e.get("tool")
            }
            base_def["tool_refs"] = [
                {
                    "name": tname,
                    "schema_sha256": compute_schema_digest([tool_by_name[tname]]),
                }
                for tname in sorted(needed_tools)
                if tname in tool_by_name
            ]
            try:
                definition = IntegrationDefinition.model_validate(base_def)
                digest = await _validate_admin_integration_definition(
                    definition, upstream_name, identity
                )
            except ValueError as exc:
                raise HTTPException(status_code=422, detail=str(exc)) from exc
            saved = await runtime.store.save_integration_definition_version(
                {
                    "tenant_id": tenant_id,
                    "upstream_name": upstream_name,
                    "definition_digest": digest,
                    "definition": definition.model_dump(mode="json", exclude_none=True),
                }
            )
            if bool(body.get("publish", True)):
                prev = current["revision"] if current else None
                await runtime.store.append_integration_definition_publication(
                    {
                        "event_id": uuid.uuid4().hex,
                        "tenant_id": tenant_id,
                        "upstream_name": upstream_name,
                        "action": "publish",
                        "revision": saved["revision"],
                        "actor_sub": identity.sub if identity else "local-admin",
                        "created_at": datetime.now(UTC).isoformat(),
                        "evidence": {
                            "previous_revision": prev,
                            "target_revision": saved["revision"],
                            "promoted_candidate_id": candidate_id,
                        },
                    }
                )
            await runtime.store.update_discovered_workflow_candidate_status(
                tenant_id, candidate_id, "promoted", datetime.now(UTC).isoformat()
            )
            return {
                "promoted": True,
                "candidate_id": candidate_id,
                "upstream_name": upstream_name,
                "revision": saved["revision"],
                "published": bool(body.get("publish", True)),
            }

        @app.post("/admin/workflows/discovered/{candidate_id}/dismiss")
        async def dismiss_discovered_workflow(
            candidate_id: str,
            request: Request,
            authorization: str | None = Header(default=None),
            mcpcodeaway_admin_session: str | None = Cookie(default=None),
        ) -> dict[str, Any]:
            identity = await _authorize_control_plane(
                authorization, mcpcodeaway_admin_session, require_admin_role=True
            )
            _reject_integration_definition_tenant_override(request)
            tenant_id = _integration_definition_tenant(identity)
            if not hasattr(runtime.store, "update_discovered_workflow_candidate_status"):
                raise HTTPException(status_code=501, detail="Candidate store unavailable.")
            updated = await runtime.store.update_discovered_workflow_candidate_status(
                tenant_id, candidate_id, "dismissed", datetime.now(UTC).isoformat()
            )
            if not updated:
                raise HTTPException(status_code=404, detail="Discovered candidate not found.")
            return {"dismissed": True, "candidate_id": candidate_id}

        @app.post("/admin/workflows/discovered/dismiss-schema")
        async def dismiss_schema_candidates(
            request: Request,
            body: dict[str, Any] | None = None,
            authorization: str | None = Header(default=None),
            mcpcodeaway_admin_session: str | None = Cookie(default=None),
        ) -> dict[str, Any]:
            identity = await _authorize_control_plane(
                authorization, mcpcodeaway_admin_session, require_admin_role=True
            )
            _reject_integration_definition_tenant_override(request)
            tenant_id = _integration_definition_tenant(identity)
            if not hasattr(runtime.store, "list_discovered_workflow_candidates"):
                raise HTTPException(status_code=501, detail="Candidate store unavailable.")
            target_upstream = str((body or {}).get("upstream_name") or "").strip() or None
            candidates = await runtime.store.list_discovered_workflow_candidates(
                tenant_id, upstream_name=target_upstream, limit=500
            )
            dismissed_ids: list[str] = []
            now_iso = datetime.now(UTC).isoformat()
            for item in candidates:
                if (
                    item.get("status") == "pending_review"
                    and item.get("origin") == "auto_schema"
                    and int(item.get("verified_count", 0)) == 0
                ):
                    ok = await runtime.store.update_discovered_workflow_candidate_status(
                        tenant_id, item["candidate_id"], "dismissed", now_iso
                    )
                    if ok:
                        dismissed_ids.append(item["candidate_id"])
            return {"dismissed_count": len(dismissed_ids), "dismissed_ids": dismissed_ids}

        @app.post("/admin/onboarding/bundle/validate")
        async def validate_admin_onboarding_bundle(
            body: dict[str, Any],
            request: Request,
            authorization: str | None = Header(default=None),
            mcpcodeaway_admin_session: str | None = Cookie(default=None),
        ) -> dict[str, Any]:
            identity = await _authorize_control_plane(
                authorization, mcpcodeaway_admin_session, require_admin_role=True
            )
            _reject_integration_definition_tenant_override(request)
            catalog_identity = identity or runtime._dev_discovery_identity()
            if catalog_identity is None:
                raise HTTPException(
                    status_code=401,
                    detail="An authenticated administrator is required to inspect the catalog.",
                )
            try:
                await runtime.ensure_catalog_for_identity(catalog_identity)
                return validate_onboarding_bundle_payload(
                    body, settings=runtime.settings, catalog=runtime.catalog
                )
            except ValueError as exc:
                raise HTTPException(status_code=422, detail=str(exc)) from exc

        @app.post("/admin/onboarding/bundle/apply")
        async def apply_admin_onboarding_bundle(
            body: dict[str, Any],
            request: Request,
            authorization: str | None = Header(default=None),
            mcpcodeaway_admin_session: str | None = Cookie(default=None),
        ) -> dict[str, Any]:
            identity = await _authorize_control_plane(
                authorization, mcpcodeaway_admin_session, require_admin_role=True
            )
            _reject_integration_definition_tenant_override(request)
            catalog_identity = identity or runtime._dev_discovery_identity()
            if catalog_identity is None:
                raise HTTPException(
                    status_code=401,
                    detail="An authenticated administrator is required to inspect the catalog.",
                )
            try:
                bundle = OnboardingBundle.model_validate(body)
                if bundle.upstream is None:
                    await runtime.ensure_catalog_for_identity(catalog_identity)
                preview = validate_onboarding_bundle_payload(
                    body, settings=runtime.settings, catalog=runtime.catalog
                )
            except ValueError as exc:
                raise HTTPException(status_code=422, detail=str(exc)) from exc

            upstream_result: dict[str, Any] | None = None
            if bundle.upstream is not None:
                try:
                    upstream_cfg = UpstreamConfig.model_validate(bundle.upstream)
                except ValueError as exc:
                    raise HTTPException(status_code=422, detail=str(exc)) from exc
                upstream_result = await add_admin_upstream(
                    upstream_cfg,
                    authorization=authorization,
                    mcpcodeaway_admin_session=mcpcodeaway_admin_session,
                )

            definition_result: dict[str, Any] | None = None
            if preview.get("integration_definition") is not None:
                try:
                    await runtime.ensure_catalog_for_identity(catalog_identity)
                    refreshed = validate_onboarding_bundle_payload(
                        body, settings=runtime.settings, catalog=runtime.catalog
                    )
                    def_model = IntegrationDefinition.model_validate(
                        refreshed["integration_definition"]
                    )
                    upstream_name = def_model.integration.upstream_name
                    tenant_id = _integration_definition_tenant(identity)
                    draft_def = def_model.model_copy(
                        update={
                            "integration": def_model.integration.model_copy(
                                update={"status": "draft", "revision": 1}
                            ),
                            "publication": None,
                        }
                    )
                    digest = await _validate_admin_integration_definition(
                        draft_def, upstream_name, identity
                    )
                except ValueError as exc:
                    raise HTTPException(status_code=422, detail=str(exc)) from exc
                saved = await runtime.store.save_integration_definition_version(
                    {
                        "tenant_id": tenant_id,
                        "upstream_name": upstream_name,
                        "definition_digest": digest,
                        "definition": draft_def.model_dump(mode="json", exclude_none=True),
                    }
                )
                published = False
                if bundle.publish:
                    current = await runtime.store.get_current_published_integration_definition(
                        tenant_id, upstream_name
                    )
                    await runtime.store.append_integration_definition_publication(
                        {
                            "event_id": uuid.uuid4().hex,
                            "tenant_id": tenant_id,
                            "upstream_name": upstream_name,
                            "action": "publish",
                            "revision": saved["revision"],
                            "actor_sub": identity.sub if identity else "local-admin",
                            "created_at": datetime.now(UTC).isoformat(),
                            "evidence": {
                                "previous_revision": current["revision"] if current else None,
                                "target_revision": saved["revision"],
                                "source": "onboarding_bundle_apply",
                            },
                        }
                    )
                    published = True
                definition_result = {
                    "upstream_name": upstream_name,
                    "revision": saved["revision"],
                    "definition_digest": digest,
                    "published": published,
                }
            return {
                "applied": True,
                "upstream": upstream_result,
                "integration_definition": definition_result,
            }

        async def _save_and_publish_definition_mutation(
            *,
            tenant_id: str,
            upstream_name: str,
            identity: CallerIdentity | None,
            base_def: dict[str, Any],
            previous_revision: int | None,
            source: str,
        ) -> dict[str, Any]:
            catalog_identity = identity or runtime._dev_discovery_identity()
            if catalog_identity is None:
                raise HTTPException(
                    status_code=401,
                    detail="An authenticated administrator is required to update integrations.",
                )
            await runtime.ensure_catalog_for_identity(catalog_identity)
            tools = runtime.catalog.get_server_tools(upstream_name)
            tool_by_name = {t.qualified_name: t for t in tools}
            base_def["integration"]["status"] = "draft"
            base_def["integration"]["revision"] = 1
            base_def["publication"] = None
            needed_tools = {
                t for wf in base_def.get("workflows", []) for t in wf.get("participating_tools", [])
            } | {e.get("tool") for e in base_def.get("experiences", []) if e.get("tool")}
            base_def["tool_refs"] = [
                {
                    "name": tname,
                    "schema_sha256": compute_schema_digest([tool_by_name[tname]]),
                }
                for tname in sorted(needed_tools)
                if tname in tool_by_name
            ]
            try:
                definition = IntegrationDefinition.model_validate(base_def)
                digest = await _validate_admin_integration_definition(
                    definition, upstream_name, identity
                )
            except ValueError as exc:
                raise HTTPException(status_code=422, detail=str(exc)) from exc
            saved = await runtime.store.save_integration_definition_version(
                {
                    "tenant_id": tenant_id,
                    "upstream_name": upstream_name,
                    "definition_digest": digest,
                    "definition": definition.model_dump(mode="json", exclude_none=True),
                }
            )
            await runtime.store.append_integration_definition_publication(
                {
                    "event_id": uuid.uuid4().hex,
                    "tenant_id": tenant_id,
                    "upstream_name": upstream_name,
                    "action": "publish",
                    "revision": saved["revision"],
                    "actor_sub": identity.sub if identity else "local-admin",
                    "created_at": datetime.now(UTC).isoformat(),
                    "evidence": {
                        "previous_revision": previous_revision,
                        "target_revision": saved["revision"],
                        "source": source,
                    },
                }
            )
            return {
                "upstream_name": upstream_name,
                "revision": saved["revision"],
                "definition_digest": digest,
                "definition": saved["definition"],
            }

        @app.get("/admin/workflows/catalog")
        async def admin_workflows_catalog(
            request: Request,
            authorization: str | None = Header(default=None),
            mcpcodeaway_admin_session: str | None = Cookie(default=None),
        ) -> dict[str, Any]:
            identity = await _authorize_control_plane(
                authorization,
                mcpcodeaway_admin_session,
                require_admin_role=True,
                allow_viewer=True,
            )
            _reject_integration_definition_tenant_override(request)
            tenant_id = _integration_definition_tenant(identity)
            published_defs = await runtime.store.list_current_published_integration_definitions(
                tenant_id
            )
            workflows: list[dict[str, Any]] = []
            for row in published_defs:
                definition = row.get("definition") or {}
                for wf in definition.get("workflows", []):
                    workflows.append(
                        {
                            "upstream_name": row["upstream_name"],
                            "definition_revision": row["revision"],
                            "workflow": wf,
                        }
                    )
            candidates = (
                await runtime.store.list_discovered_workflow_candidates(
                    tenant_id, status="pending_review", limit=200
                )
                if hasattr(runtime.store, "list_discovered_workflow_candidates")
                else []
            )
            episodes = (
                await runtime.store.list_workflow_episodes(tenant_id, limit=100)
                if hasattr(runtime.store, "list_workflow_episodes")
                else []
            )
            return {
                "upstreams": [u.name for u in runtime.registry.list_upstreams()],
                "workflows": workflows,
                "candidates": candidates,
                "episodes": episodes,
                "reporting_currency": await runtime.get_reporting_currency(),
            }

        @app.patch("/admin/integration-definitions/{upstream_name}/workflows/{workflow_id}")
        async def update_admin_workflow(
            upstream_name: str,
            workflow_id: str,
            body: dict[str, Any],
            request: Request,
            authorization: str | None = Header(default=None),
            mcpcodeaway_admin_session: str | None = Cookie(default=None),
        ) -> dict[str, Any]:
            identity = await _authorize_control_plane(
                authorization, mcpcodeaway_admin_session, require_admin_role=True
            )
            _reject_integration_definition_tenant_override(request)
            tenant_id = _integration_definition_tenant(identity)
            current = await runtime.store.get_current_published_integration_definition(
                tenant_id, upstream_name
            )
            if current is None:
                raise HTTPException(
                    status_code=404,
                    detail=f"No published integration definition found for '{upstream_name}'.",
                )
            base_def = json.loads(json.dumps(current["definition"]))
            target = next(
                (w for w in base_def.get("workflows", []) if w.get("id") == workflow_id), None
            )
            if target is None:
                raise HTTPException(status_code=404, detail=f"Workflow '{workflow_id}' not found.")
            if "enabled" in body:
                target["enabled"] = bool(body["enabled"])
            if "name" in body and isinstance(body["name"], str):
                target["name"] = body["name"]
            if "description" in body and isinstance(body["description"], str):
                target["description"] = body["description"]
            if "intent_examples" in body and isinstance(body["intent_examples"], list):
                target["intent_examples"] = [
                    str(x).strip() for x in body["intent_examples"] if str(x).strip()
                ][:25]
            if any(
                key in body
                for key in (
                    "manual_baseline_minutes",
                    "baseline_source",
                    "confidence",
                    "evidence_note",
                    "effective_from",
                    "review_minutes",
                    "correction_minutes",
                    "loaded_labor_cost_per_hour",
                    "currency",
                    "regional_labor_rates",
                )
            ):
                bv = dict(target.get("business_value") or {})
                target["business_value"] = _apply_baseline_updates(
                    bv, body, await runtime.get_reporting_currency()
                )
            result = await _save_and_publish_definition_mutation(
                tenant_id=tenant_id,
                upstream_name=upstream_name,
                identity=identity,
                base_def=base_def,
                previous_revision=current["revision"],
                source="admin_workflow_patch",
            )
            updated_wf = next(
                w for w in result["definition"].get("workflows", []) if w.get("id") == workflow_id
            )
            return {
                "updated": True,
                "upstream_name": upstream_name,
                "revision": result["revision"],
                "workflow": updated_wf,
            }

        @app.delete("/admin/integration-definitions/{upstream_name}/workflows/{workflow_id}")
        async def delete_admin_workflow(
            upstream_name: str,
            workflow_id: str,
            request: Request,
            authorization: str | None = Header(default=None),
            mcpcodeaway_admin_session: str | None = Cookie(default=None),
        ) -> dict[str, Any]:
            identity = await _authorize_control_plane(
                authorization, mcpcodeaway_admin_session, require_admin_role=True
            )
            _reject_integration_definition_tenant_override(request)
            tenant_id = _integration_definition_tenant(identity)
            current = await runtime.store.get_current_published_integration_definition(
                tenant_id, upstream_name
            )
            if current is None:
                raise HTTPException(
                    status_code=404,
                    detail=f"No published integration definition found for '{upstream_name}'.",
                )
            base_def = json.loads(json.dumps(current["definition"]))
            existing = list(base_def.get("workflows", []))
            remaining = [w for w in existing if w.get("id") != workflow_id]
            if len(remaining) == len(existing):
                raise HTTPException(status_code=404, detail=f"Workflow '{workflow_id}' not found.")
            base_def["workflows"] = remaining
            result = await _save_and_publish_definition_mutation(
                tenant_id=tenant_id,
                upstream_name=upstream_name,
                identity=identity,
                base_def=base_def,
                previous_revision=current["revision"],
                source="admin_workflow_delete",
            )
            return {
                "deleted": True,
                "upstream_name": upstream_name,
                "workflow_id": workflow_id,
                "revision": result["revision"],
            }

        @app.get("/admin/mcp-ui/experiences")
        async def admin_list_mcp_ui_experiences(
            request: Request,
            authorization: str | None = Header(default=None),
            mcpcodeaway_admin_session: str | None = Cookie(default=None),
        ) -> dict[str, Any]:
            identity = await _authorize_control_plane(
                authorization,
                mcpcodeaway_admin_session,
                require_admin_role=True,
                allow_viewer=True,
            )
            _reject_integration_definition_tenant_override(request)
            tenant_id = _integration_definition_tenant(identity)
            catalog_identity = identity or runtime._dev_discovery_identity()
            if catalog_identity is not None:
                await runtime.ensure_catalog_for_identity(catalog_identity)
            published_defs = await runtime.store.list_current_published_integration_definitions(
                tenant_id
            )
            experiences: list[dict[str, Any]] = []
            for row in published_defs:
                definition = row.get("definition") or {}
                for exp in definition.get("experiences", []):
                    tool_name = str(exp.get("tool") or "")
                    tool_obj = runtime.catalog.get_tool(tool_name)
                    experiences.append(
                        {
                            "upstream_name": row["upstream_name"],
                            "definition_revision": row["revision"],
                            "experience": exp,
                            "tool_input_schema": (tool_obj.input_schema if tool_obj else None),
                            "tool_description": (tool_obj.description if tool_obj else None),
                        }
                    )
            registered_upstreams = runtime.registry.list_upstreams()
            tools_by_upstream = {
                u.name: [
                    {
                        "qualified_name": t.qualified_name,
                        "name": t.name,
                        "description": t.description,
                        "mutating": t.mutating,
                        "input_schema": t.input_schema,
                    }
                    for t in runtime.catalog.get_server_tools(u.name)
                ]
                for u in registered_upstreams
            }
            return {
                "upstreams": [u.name for u in registered_upstreams],
                "experiences": experiences,
                "tools_by_upstream": tools_by_upstream,
            }

        @app.post("/admin/mcp-ui/preview")
        async def admin_preview_mcp_ui_experience(
            body: dict[str, Any],
            request: Request,
            authorization: str | None = Header(default=None),
            mcpcodeaway_admin_session: str | None = Cookie(default=None),
        ) -> dict[str, Any]:
            identity = await _authorize_control_plane(
                authorization,
                mcpcodeaway_admin_session,
                require_admin_role=True,
                allow_viewer=True,
            )
            _reject_integration_definition_tenant_override(request)
            exp = body.get("experience") if isinstance(body.get("experience"), dict) else body
            if not isinstance(exp, dict):
                raise HTTPException(status_code=422, detail="experience payload must be an object.")
            catalog_identity = identity or runtime._dev_discovery_identity()
            if catalog_identity is not None:
                await runtime.ensure_catalog_for_identity(catalog_identity)
            tool_name = str(exp.get("tool") or "")
            tool_obj = runtime.catalog.get_tool(tool_name) if tool_name else None
            tool_input_schema = (
                body.get("tool_input_schema")
                if isinstance(body.get("tool_input_schema"), dict)
                else (tool_obj.input_schema if tool_obj else None)
            )
            if tool_input_schema is None and isinstance(exp.get("input_mapping"), dict):
                tool_input_schema = {
                    "type": "object",
                    "properties": {str(k): {"type": "string"} for k in exp["input_mapping"]},
                    "required": [],
                    "additionalProperties": False,
                }
            try:
                rendered = render_mcp_app_resource(exp, tool_input_schema=tool_input_schema)
            except ValueError as exc:
                raise HTTPException(status_code=422, detail=str(exc)) from exc
            return {
                "valid": True,
                "uri": rendered["uri"],
                "mimeType": rendered["mimeType"],
                "html": rendered["text"],
                "meta": rendered.get("_meta"),
                "tool_input_schema": tool_input_schema,
            }

        @app.post("/admin/mcp-ui/simulate")
        async def admin_simulate_mcp_ui_tool_call(
            body: dict[str, Any],
            request: Request,
            authorization: str | None = Header(default=None),
            mcpcodeaway_admin_session: str | None = Cookie(default=None),
        ) -> dict[str, Any]:
            identity = await _authorize_control_plane(
                authorization,
                mcpcodeaway_admin_session,
                require_admin_role=True,
                allow_viewer=True,
            )
            _reject_integration_definition_tenant_override(request)
            tool_name = str(body.get("tool") or "").strip()
            arguments = body.get("arguments")
            if not tool_name:
                raise HTTPException(status_code=422, detail="tool name is required.")
            if arguments is None:
                arguments = {}
            if not isinstance(arguments, dict):
                raise HTTPException(status_code=422, detail="arguments must be an object.")
            catalog_identity = identity or runtime._dev_discovery_identity()
            if catalog_identity is not None:
                await runtime.ensure_catalog_for_identity(catalog_identity)
            tool_obj = runtime.catalog.get_tool(tool_name)

            def _sample_from_schema(
                schema: Any, args_map: dict[str, Any], field_hint: str = "", row_idx: int = 0
            ) -> Any:
                if not isinstance(schema, dict):
                    return {"status": "ok", "simulated": True, **args_map}
                stype = schema.get("type")
                if stype == "array":
                    item_schema = schema.get("items") or {"type": "string"}
                    if isinstance(item_schema, dict) and item_schema.get("type") == "object":
                        return [
                            _sample_from_schema(item_schema, args_map, row_idx=0),
                            _sample_from_schema(item_schema, args_map, row_idx=1),
                        ]
                    return [_sample_from_schema(item_schema, args_map)]
                if stype == "integer":
                    return 42 + row_idx * 7
                if stype == "number":
                    return round(98.4 - row_idx * 3.2, 1)
                if stype == "boolean":
                    return field_hint != "sla_breached"
                if stype == "string":
                    hints = {
                        "ticket_id": f"TCK-{1042 + row_idx}",
                        "id": f"REC-{101 + row_idx}",
                        "subject": "Checkout latency spike in eu-west",
                        "title": "Checkout latency spike",
                        "assignee": "sre-oncall" if row_idx == 0 else "platform-triage",
                        "team": "core-checkout",
                        "queue_id": "eu-priority",
                        "region": "EU" if row_idx == 0 else "US",
                        "status": "open" if row_idx == 0 else "resolved",
                        "priority": "high" if row_idx == 0 else "medium",
                        "requester_email": "ops-lead@example.com",
                    }
                    if field_hint in hints:
                        return hints[field_hint]
                    return f"sample_{field_hint or 'value'}"
                props = schema.get("properties")
                if isinstance(props, dict) and props:
                    out: dict[str, Any] = {}
                    for k, sub in props.items():
                        if k in args_map and args_map[k] != "":
                            out[k] = args_map[k]
                        else:
                            out[k] = _sample_from_schema(
                                sub, {}, field_hint=str(k), row_idx=row_idx
                            )
                    return out
                return {"status": "ok", "simulated": True, **args_map}

            sample_payload = _sample_from_schema(
                tool_obj.output_schema if tool_obj else None, arguments
            )
            if isinstance(sample_payload, dict):
                for arg_k, arg_v in arguments.items():
                    if arg_v not in (None, "") and arg_k not in sample_payload:
                        sample_payload[arg_k] = arg_v
            return {
                "simulated": True,
                "tool": tool_name,
                "arguments": arguments,
                "structuredContent": sample_payload,
                "isError": False,
            }

        @app.put("/admin/integration-definitions/{upstream_name}/experiences/{experience_id}")
        async def upsert_admin_mcp_ui_experience(
            upstream_name: str,
            experience_id: str,
            body: dict[str, Any],
            request: Request,
            authorization: str | None = Header(default=None),
            mcpcodeaway_admin_session: str | None = Cookie(default=None),
        ) -> dict[str, Any]:
            identity = await _authorize_control_plane(
                authorization, mcpcodeaway_admin_session, require_admin_role=True
            )
            _reject_integration_definition_tenant_override(request)
            tenant_id = _integration_definition_tenant(identity)
            if runtime.registry.get(upstream_name) is None:
                raise HTTPException(
                    status_code=404, detail=f"Upstream '{upstream_name}' not found."
                )
            exp_payload = dict(
                body.get("experience") if isinstance(body.get("experience"), dict) else body
            )
            exp_payload["id"] = experience_id
            current = await runtime.store.get_current_published_integration_definition(
                tenant_id, upstream_name
            )
            base_def: dict[str, Any] = (
                json.loads(json.dumps(current["definition"]))
                if current
                else {
                    "manifest_version": "1.0",
                    "integration": {
                        "id": re.sub(r"[^a-z0-9]+", "_", upstream_name.lower()).strip("_")
                        or "integration",
                        "display_name": upstream_name,
                        "upstream_name": upstream_name,
                        "status": "draft",
                        "revision": 1,
                    },
                    "tool_refs": [],
                    "experiences": [],
                    "workflows": [],
                    "standalone_tool_values": [],
                    "publication": None,
                }
            )
            exps = [e for e in base_def.get("experiences", []) if e.get("id") != experience_id]
            exps.append(exp_payload)
            base_def["experiences"] = exps
            result = await _save_and_publish_definition_mutation(
                tenant_id=tenant_id,
                upstream_name=upstream_name,
                identity=identity,
                base_def=base_def,
                previous_revision=current["revision"] if current else None,
                source="admin_experience_upsert",
            )
            saved_exp = next(
                e
                for e in result["definition"].get("experiences", [])
                if e.get("id") == experience_id
            )
            return {
                "saved": True,
                "upstream_name": upstream_name,
                "revision": result["revision"],
                "experience": saved_exp,
            }

        @app.delete("/admin/integration-definitions/{upstream_name}/experiences/{experience_id}")
        async def delete_admin_mcp_ui_experience(
            upstream_name: str,
            experience_id: str,
            request: Request,
            authorization: str | None = Header(default=None),
            mcpcodeaway_admin_session: str | None = Cookie(default=None),
        ) -> dict[str, Any]:
            identity = await _authorize_control_plane(
                authorization, mcpcodeaway_admin_session, require_admin_role=True
            )
            _reject_integration_definition_tenant_override(request)
            tenant_id = _integration_definition_tenant(identity)
            current = await runtime.store.get_current_published_integration_definition(
                tenant_id, upstream_name
            )
            if current is None:
                raise HTTPException(
                    status_code=404,
                    detail=f"No published integration definition found for '{upstream_name}'.",
                )
            base_def = json.loads(json.dumps(current["definition"]))
            existing = list(base_def.get("experiences", []))
            remaining = [e for e in existing if e.get("id") != experience_id]
            if len(remaining) == len(existing):
                raise HTTPException(
                    status_code=404, detail=f"MCP App experience '{experience_id}' not found."
                )
            base_def["experiences"] = remaining
            result = await _save_and_publish_definition_mutation(
                tenant_id=tenant_id,
                upstream_name=upstream_name,
                identity=identity,
                base_def=base_def,
                previous_revision=current["revision"],
                source="admin_experience_delete",
            )
            return {
                "deleted": True,
                "upstream_name": upstream_name,
                "experience_id": experience_id,
                "revision": result["revision"],
            }

        @app.put("/admin/upstreams/{name}/tools/{tool_name}/guidance")
        async def save_admin_tool_guidance(
            name: str,
            tool_name: str,
            body: ToolGuidance,
            authorization: str | None = Header(default=None),
            mcpcodeaway_admin_session: str | None = Cookie(default=None),
        ) -> dict[str, Any]:
            caller = await _authorize_control_plane(
                authorization, mcpcodeaway_admin_session, require_admin_role=True
            )
            tool = await _catalog_tool_for_admin(name, tool_name)
            hints = {key: value for key, value in body.parameter_hints.items() if value}
            if unknown := hints.keys() - (tool.input_schema.get("properties") or {}).keys():
                raise HTTPException(
                    status_code=422,
                    detail=f"Unknown tool parameters: {', '.join(sorted(unknown))}",
                )
            guidance = {"description": body.description, "parameter_hints": hints}
            saved = guidance if body.description or hints else None
            await runtime.store.save_tool_guidance(
                (caller.tenant_id or "") if caller else "", tool_name, saved
            )
            return {"guidance": saved}

        @app.put("/admin/upstreams/{name}/tools/{tool_name}/approval-policy")
        async def save_admin_tool_approval_policy(
            name: str,
            tool_name: str,
            payload: dict[str, Any],
            authorization: str | None = Header(default=None),
            mcpcodeaway_admin_session: str | None = Cookie(default=None),
        ) -> dict[str, Any]:
            actor = await _authorize_control_plane(
                authorization, mcpcodeaway_admin_session, require_admin_role=True
            )
            if not runtime.settings.dynamic_upstreams_permitted:
                raise HTTPException(
                    status_code=403,
                    detail=(
                        "Tool approval policies are managed through "
                        "config/upstreams.yaml in this profile."
                    ),
                )
            tool = await _catalog_tool_for_admin(name, tool_name)
            cfg = runtime.registry.get(name)
            if cfg is None:
                raise HTTPException(status_code=404, detail=f"Upstream '{name}' not found")
            mode = payload.get("mode", "none")
            if not isinstance(mode, str) or mode not in {
                "none",
                "requester_confirmation",
                "manager_approval",
            }:
                raise HTTPException(
                    status_code=422,
                    detail="mode must be none, requester_confirmation, or manager_approval.",
                )
            message = payload.get("confirmation_message", "")
            if not isinstance(message, str) or len(message) > 300:
                raise HTTPException(
                    status_code=422,
                    detail="confirmation_message must be text of at most 300 characters.",
                )
            approver_groups = payload.get("approver_groups", [])
            if not isinstance(approver_groups, list) or any(
                not isinstance(group, str) for group in approver_groups
            ):
                raise HTTPException(
                    status_code=422, detail="approver_groups must be a list of strings."
                )
            previous = cfg.tool_approval_policies.get(tool.name, ToolApprovalPolicy())
            effect = cfg.tool_effects.get(tool.name)
            if effect is not None:
                expected_mode = {
                    "read": "none",
                    "write": "requester_confirmation",
                    "high_impact": "manager_approval",
                }[effect]
                if mode != expected_mode:
                    raise HTTPException(
                        status_code=422,
                        detail=(
                            f"Operation effect '{effect}' requires policy '{expected_mode}'. "
                            "Update the operation classification to change its approval gate."
                        ),
                    )
            try:
                policy = ToolApprovalPolicy(
                    mode=mode,
                    version=previous.version + 1,
                    confirmation_message=message.strip(),
                    approver_groups=approver_groups,
                )
            except ValueError as exc:
                raise HTTPException(status_code=422, detail=str(exc)) from exc
            policies = dict(cfg.tool_approval_policies)
            policies[tool.name] = policy
            updated = cfg.model_copy(update={"tool_approval_policies": policies})
            await runtime.store.save_dynamic_upstream(
                updated.name, updated.model_dump(exclude_none=True)
            )
            runtime.registry.upsert(updated)
            await runtime.refresh_catalog()
            await runtime.bump_catalog_epoch()
            await runtime.store.invalidate_tool_approvals(
                name, tool.name, policy.version, datetime.now(UTC).isoformat()
            )
            log_event(
                "approval.policy.updated",
                upstream=name,
                tool=tool.qualified_name,
                mode=policy.mode,
                version=policy.version,
                tenant_id=actor.tenant_id if actor else "",
                actor_sub=actor.sub if actor else "admin",
            )
            return {"approval_policy": policy.model_dump()}

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

        @app.put("/admin/business-value/settings")
        async def admin_save_business_value_settings(
            body: dict[str, Any],
            authorization: str | None = Header(default=None),
            mcpcodeaway_admin_session: str | None = Cookie(default=None),
        ) -> dict[str, Any]:
            identity = await _authorize_control_plane(
                authorization, mcpcodeaway_admin_session, require_admin_role=True
            )
            raw_currency = str(body.get("reporting_currency") or "").strip().upper()
            if not re.match(r"^[A-Z]{3}$", raw_currency):
                raise HTTPException(
                    status_code=422,
                    detail=(
                        "reporting_currency must be a 3-letter ISO currency code "
                        "(e.g. USD, EUR, GBP)."
                    ),
                )
            payload = {
                "reporting_currency": raw_currency,
                "updated_by": identity.sub if identity is not None else "dev-admin",
                "updated_at": datetime.now(UTC).isoformat(),
            }
            if hasattr(runtime.store, "save_setting"):
                await runtime.store.save_setting("business_value_settings", payload)
            return {
                "saved": True,
                "reporting_currency": raw_currency,
                "env_default_currency": runtime.settings.default_currency,
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

        if runtime.settings.effective_profile_name == "demo":

            @app.post("/admin/demo/recovery/run")
            async def admin_demo_recovery_run(
                authorization: str | None = Header(default=None),
                mcpcodeaway_admin_session: str | None = Cookie(default=None),
            ) -> dict[str, Any]:
                identity = await _authorize_control_plane(
                    authorization,
                    mcpcodeaway_admin_session,
                    require_admin_role=True,
                )
                if identity is None:
                    raise HTTPException(
                        status_code=401,
                        detail="An authenticated demo administrator is required.",
                    )
                _ensure_demo_traffic_allowed()
                from examples.recovery_lab import run_recovery_lab

                return await run_recovery_lab()

            @app.post("/admin/demo/workflow-evidence")
            @app.post("/admin/demo/golden-run")
            async def admin_demo_workflow_evidence(
                authorization: str | None = Header(default=None),
                mcpcodeaway_admin_session: str | None = Cookie(default=None),
            ) -> dict[str, Any]:
                identity = await _authorize_control_plane(
                    authorization,
                    mcpcodeaway_admin_session,
                    require_admin_role=True,
                )
                if identity is None:
                    raise HTTPException(
                        status_code=401,
                        detail="An authenticated demo administrator is required.",
                    )
                _ensure_demo_traffic_allowed()
                tenant = _resolve_audit_tenant_scope(identity, None) or identity.tenant_id
                if not tenant:
                    raise HTTPException(status_code=401, detail="Tenant identity is required.")
                task_polls_before = runtime.demo_traffic.task_polls_count
                operations_before = await runtime.store.list_operations(
                    tenant, statuses=None, limit=200
                )
                golden_run = await runtime.demo_traffic.golden_run()
                evidence = runtime.demo_traffic.simulated_workflow_evidence()
                await runtime.store.save_setting(
                    f"demo_adaptive_evidence:{tenant}",
                    {"evidence": evidence, "created_at": datetime.now(UTC).isoformat()},
                )
                operations_after = await runtime.store.list_operations(
                    tenant, statuses=None, limit=200
                )
                before_ids = {str(row["operation_id"]) for row in operations_before}
                created = [
                    row for row in operations_after if str(row["operation_id"]) not in before_ids
                ]
                return {
                    "evidence_rows": len(evidence),
                    "models": len(runtime.demo_traffic.model_profiles),
                    "workloads_per_model": len({row["workload"] for row in evidence}),
                    "pairs_per_workload": 5,
                    "source": "demo simulator; excluded from routing recommendations",
                    "traffic_requests": golden_run["traffic"]["sent"],
                    "traffic_workload_cases": golden_run["workload_cases"],
                    "traffic_ok": golden_run["traffic"]["ok"],
                    "traffic_errors": golden_run["traffic"]["errors"],
                    "business_value_workflow": golden_run["business_value_workflow"],
                    "discovery_requests": golden_run["discovery"]["sent"],
                    "task_polls": runtime.demo_traffic.task_polls_count - task_polls_before,
                    "recovery_operations_created": len(created),
                    "recovery_operations_pending": sum(
                        row["status"] in {"pending", "working"} for row in created
                    ),
                    "recovery_operations_needs_attention": sum(
                        row["status"] == "needs_attention" for row in created
                    ),
                }

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
                    "detail": f"Webhook delivery failed ({type(exc).__name__})",
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
            candidate_policy = body.upstream.tool_approval_policies.get(target_tool.name)
            registered_upstream = runtime.registry.get(body.upstream.name)
            registered_policy = (
                registered_upstream.tool_approval_policies.get(target_tool.name)
                if registered_upstream is not None
                else None
            )
            if any(
                policy is not None and policy.mode == "requester_confirmation"
                for policy in (candidate_policy, registered_policy)
            ):
                raise HTTPException(
                    status_code=400,
                    detail=(
                        "Live test calls are disabled for tools requiring requester confirmation."
                    ),
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
            if model_name in runtime.settings.model_pricing:
                runtime.settings.model_pricing.pop(model_name, None)
                deleted = True
            if hasattr(runtime.store, "get_setting") and hasattr(runtime.store, "save_setting"):
                scores = dict(await runtime.store.get_setting("model_routing_scores") or {})
                if model_name in scores:
                    scores.pop(model_name, None)
                    await runtime.store.save_setting("model_routing_scores", scores)
            if hasattr(runtime, "demo_traffic") and runtime.demo_traffic:
                runtime.demo_traffic.model_profiles = [
                    p for p in runtime.demo_traffic.model_profiles if p.get("model") != model_name
                ]
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
            count: int = Query(default=6, ge=1, le=100),
            authorization: str | None = Header(default=None),
            mcpcodeaway_admin_session: str | None = Cookie(default=None),
        ) -> dict[str, Any]:
            await _authorize_control_plane(
                authorization, mcpcodeaway_admin_session, require_admin_role=True
            )
            _ensure_demo_traffic_allowed()
            return await runtime.demo_traffic.burst(count=count)

        @app.get("/admin/demo/models")
        async def admin_demo_models_list(
            authorization: str | None = Header(default=None),
            mcpcodeaway_admin_session: str | None = Cookie(default=None),
        ) -> dict[str, Any]:
            await _authorize_control_plane(
                authorization,
                mcpcodeaway_admin_session,
                require_admin_role=True,
                allow_viewer=True,
            )
            _ensure_demo_traffic_allowed()
            return {"models": runtime.demo_traffic.model_profiles}

        @app.post("/admin/demo/models")
        async def admin_demo_models_update(
            payload: dict[str, Any],
            authorization: str | None = Header(default=None),
            mcpcodeaway_admin_session: str | None = Cookie(default=None),
        ) -> dict[str, Any]:
            await _authorize_control_plane(
                authorization, mcpcodeaway_admin_session, require_admin_role=True
            )
            _ensure_demo_traffic_allowed()
            models = payload.get("models")
            if isinstance(models, list):
                if len(models) > 32:
                    raise HTTPException(
                        status_code=422, detail="At most 32 demo model profiles are allowed."
                    )
                try:
                    validated = [DemoModelProfile.model_validate(model) for model in models]
                except (TypeError, ValueError) as exc:
                    raise HTTPException(status_code=422, detail=str(exc)) from exc
                names = [profile.model.strip() for profile in validated]
                if len(names) != len(set(names)):
                    raise HTTPException(
                        status_code=422, detail="Demo model profile names must be unique."
                    )
                runtime.demo_traffic.model_profiles = [
                    profile.model_copy(update={"model": profile.model.strip()}).model_dump()
                    for profile in validated
                ]
            return {"models": runtime.demo_traffic.model_profiles}

        @app.get("/admin/discovery/report")
        async def admin_discovery_report(
            limit: int = Query(default=100, ge=0, le=1000),
            authorization: str | None = Header(default=None),
            mcpcodeaway_admin_session: str | None = Cookie(default=None),
        ) -> dict[str, Any]:
            identity = await _authorize_control_plane(
                authorization, mcpcodeaway_admin_session, require_admin_role=True, allow_viewer=True
            )
            tenant = (
                identity.tenant_id
                if identity
                else (runtime.settings.allowed_tenants or ["tenant-acme-dev"])[0]
            )
            samples = [
                {key: value for key, value in sample.items() if key != "tenant"}
                for sample in reversed(runtime.discovery_samples)
                if sample["tenant"] == tenant
            ][:limit]
            return {
                "policy": runtime.settings.auto_discovery_policy,
                "token_budget": runtime.settings.auto_discovery_token_budget,
                "samples": samples,
            }

        @app.get("/admin/adaptive-execution/report")
        async def admin_adaptive_execution_report(
            window: str = Query(default="30d"),
            authorization: str | None = Header(default=None),
            mcpcodeaway_admin_session: str | None = Cookie(default=None),
        ) -> dict[str, Any]:
            identity = await _authorize_control_plane(
                authorization, mcpcodeaway_admin_session, require_admin_role=True, allow_viewer=True
            )
            tenant = _resolve_audit_tenant_scope(identity, None) or (
                identity.tenant_id
                if identity
                else (
                    runtime.settings.allowed_tenants[0]
                    if runtime.settings.allowed_tenants
                    else "tenant-acme-dev"
                )
            )
            evidence = await runtime.store.list_adaptive_evidence(tenant or "")
            days = {"1h": 1 / 24, "24h": 1, "7d": 7, "30d": 30, "all": None}.get(window, 30)
            pricing = await runtime.merged_model_pricing()
            rows = summarize_pairs(evidence, days=days, pricing=pricing)
            live_rows = summarize_pairs(evidence, days=30, pricing=pricing)
            live_decisions = {
                (row["model"].lower(), row["workload"]): resolve_adaptive_recommendation(
                    evidence,
                    row["model"],
                    row["workload"],
                    pricing=pricing,
                    days=30,
                    summaries=live_rows,
                )
                for row in live_rows
            }
            for row in rows:
                row["live_routing"] = live_decisions.get(
                    (row["model"].lower(), row["workload"]),
                    resolve_adaptive_recommendation(
                        evidence,
                        row["model"],
                        row["workload"],
                        pricing=pricing,
                        days=30,
                        summaries=live_rows,
                    ),
                )
            if runtime.settings.effective_profile_name == "demo":
                demo_record = await runtime.store.get_setting(
                    f"demo_adaptive_evidence:{tenant or ''}"
                )
                demo_evidence = demo_record.get("evidence", []) if demo_record else []
                simulated = summarize_pairs(demo_evidence, days=days, pricing=pricing)
                for row in simulated:
                    row["simulated_recommendation"] = row.get("recommended_mode")
                    row["simulated_recommendation_reason"] = row.get("reason")
                    row.update(
                        evidence_tier="demo_simulated",
                        qualification="simulated",
                        recommended_mode=None,
                        reason="Simulated demo fixture; excluded from adaptive routing.",
                    )
                rows.extend(simulated)
            return {
                "window": window if window in {"1h", "24h", "7d", "30d", "all"} else "30d",
                "rows": rows,
                "routing_window_days": 30,
                "adaptive_mode": (
                    await runtime.store.get_setting(f"adaptive_execution:{tenant or ''}") or {}
                ).get("mode", "observe"),
            }

        @app.get("/admin/adaptive-execution/tool-health")
        async def admin_adaptive_tool_health(
            window: str = Query(default="30d"),
            authorization: str | None = Header(default=None),
            mcpcodeaway_admin_session: str | None = Cookie(default=None),
        ) -> dict[str, Any]:
            identity = await _authorize_control_plane(
                authorization, mcpcodeaway_admin_session, require_admin_role=True, allow_viewer=True
            )
            tenant = _resolve_audit_tenant_scope(identity, None) or (
                identity.tenant_id
                if identity
                else (
                    runtime.settings.allowed_tenants[0]
                    if runtime.settings.allowed_tenants
                    else "tenant-acme-dev"
                )
            )
            hours = {"1h": 1, "24h": 24, "7d": 168, "30d": 720, "all": 24 * 3650}.get(window, 720)
            runs = await runtime.store.list_runs(limit=5000, tenant_id=tenant)
            cut = datetime.now(UTC).timestamp() - hours * 3600
            try:
                runs = [
                    r
                    for r in runs
                    if datetime.fromisoformat(
                        str(r.get("created_at", "")).replace("Z", "+00:00")
                    ).timestamp()
                    >= cut
                ]
            except ValueError:
                runs = [
                    r
                    for r in runs
                    if _valid_run_timestamp(r.get("created_at"))
                    and datetime.fromisoformat(
                        str(r["created_at"]).replace("Z", "+00:00")
                    ).timestamp()
                    >= cut
                ]
            buckets: dict[tuple[str, str], dict[str, Any]] = {}
            for run in runs:
                for call in (
                    run.get("subcalls", []) if isinstance(run.get("subcalls"), list) else []
                ):
                    if not isinstance(call, dict):
                        continue
                    server, name = (
                        str(call.get("server") or "unknown"),
                        str(call.get("tool") or "unknown"),
                    )
                    key = (server, name)
                    b = buckets.setdefault(
                        key,
                        {
                            "count": 0,
                            "bytes": 0,
                            "latencies": [],
                            "errors": 0,
                            "timeouts": 0,
                            "error_types": {},
                            "invalid_arguments": 0,
                            "schema_known": 0,
                            "schema_covered": 0,
                            "read": 0,
                            "write": 0,
                        },
                    )
                    b["count"] += 1
                    if isinstance(call.get("raw_bytes"), (int, float)):
                        b["bytes"] += int(call["raw_bytes"])
                    if isinstance(call.get("duration_ms"), (int, float)):
                        b["latencies"].append(float(call["duration_ms"]))
                    status, error = str(call.get("status") or ""), str(call.get("error") or "")
                    b["errors"] += int(status != "ok" or bool(error))
                    b["timeouts"] += int("timeout" in (status + " " + error).lower())
                    error_type = call.get("error_type")
                    if error_type:
                        types = b["error_types"]
                        types[str(error_type)] = types.get(str(error_type), 0) + 1
                    if isinstance(call.get("invalid_arguments"), (int, float)):
                        b["invalid_arguments"] += int(call["invalid_arguments"])
                    if isinstance(call.get("schema_covered"), bool):
                        b["schema_known"] += 1
                        b["schema_covered"] += int(call["schema_covered"])
                    if call.get("is_mutating") is True:
                        b["write"] += 1
                    elif call.get("is_mutating") is False:
                        b["read"] += 1
            rows = []
            for (server, tool), b in sorted(buckets.items()):
                latencies = sorted(b["latencies"])
                rows.append(
                    {
                        "server": server,
                        "tool": tool,
                        "count": b["count"],
                        "bytes": b["bytes"],
                        "mean_latency_ms": round(sum(latencies) / len(latencies), 2)
                        if latencies
                        else None,
                        "p95_latency_ms": round(
                            latencies[min(len(latencies) - 1, int((len(latencies) - 1) * 0.95))], 2
                        )
                        if latencies
                        else None,
                        "errors": b["errors"],
                        "error_types": b["error_types"],
                        "timeouts": b["timeouts"],
                        "invalid_arguments": b["invalid_arguments"]
                        if any(
                            call.get("invalid_arguments") is not None
                            for run in runs
                            for call in (
                                run.get("subcalls", [])
                                if isinstance(run.get("subcalls"), list)
                                else []
                            )
                            if isinstance(call, dict)
                            and str(call.get("server") or "unknown") == server
                            and str(call.get("tool") or "unknown") == tool
                        )
                        else None,
                        "schema_coverage_pct": round(
                            100 * b["schema_covered"] / b["schema_known"], 1
                        )
                        if b["schema_known"]
                        else None,
                        "read_calls": b["read"] if b["read"] or b["write"] else None,
                        "write_calls": b["write"] if b["read"] or b["write"] else None,
                    }
                )
            return {
                "window": window,
                "rows": rows,
                "upstream_health": runtime.upstream_health.list_statuses(),
            }

        @app.post("/admin/adaptive-execution/evidence")
        async def admin_import_adaptive_evidence(
            request: Request,
            authorization: str | None = Header(default=None),
            mcpcodeaway_admin_session: str | None = Cookie(default=None),
        ) -> dict[str, Any]:
            identity = await _authorize_control_plane(
                authorization,
                mcpcodeaway_admin_session,
                require_admin_role=True,
                allow_viewer=False,
            )
            tenant = _resolve_audit_tenant_scope(identity, None) or (
                identity.tenant_id
                if identity
                else (
                    runtime.settings.allowed_tenants[0]
                    if runtime.settings.allowed_tenants
                    else "tenant-acme-dev"
                )
            )
            body = await request.json()
            rows = body.get("evidence") if isinstance(body, dict) else None
            if not isinstance(rows, list) or len(rows) > 10000:
                raise HTTPException(
                    status_code=400, detail="evidence must be a list of at most 10000 rows"
                )
            safe_rows = []
            pricing = (
                await runtime.store.list_model_pricing()
                if hasattr(runtime.store, "list_model_pricing")
                else {}
            )
            required = {
                "evidence_id",
                "created_at",
                "model",
                "workload",
                "scenario_id",
                "repeat_id",
                "mode",
                "correct",
                "grounded",
            }
            for row in rows:
                if (
                    not isinstance(row, dict)
                    or not required.issubset(row)
                    or row.get("version") != EVIDENCE_VERSION
                ):
                    raise HTTPException(
                        status_code=400, detail="unsupported or incomplete evidence row"
                    )
                try:
                    created_at = datetime.fromisoformat(
                        str(row["created_at"]).replace("Z", "+00:00")
                    )
                    if created_at.tzinfo is None:
                        raise ValueError("timezone required")
                    if created_at.astimezone(UTC) > datetime.now(UTC) + MAX_IMPORT_CLOCK_SKEW:
                        raise HTTPException(
                            status_code=400,
                            detail="created_at cannot be more than 5 minutes in the future",
                        )
                except (TypeError, ValueError):
                    raise HTTPException(
                        status_code=400, detail="created_at must be an ISO timestamp with timezone"
                    ) from None
                if not all(
                    isinstance(row.get(k), str) and row[k].strip()
                    for k in ("evidence_id", "model", "scenario_id", "repeat_id")
                ):
                    raise HTTPException(
                        status_code=400, detail="evidence identity fields must be non-empty strings"
                    )
                if (
                    not isinstance(row["workload"], str)
                    or row["workload"] not in WORKLOADS
                    or not isinstance(row["mode"], str)
                    or row["mode"]
                    not in {
                        "traditional",
                        "codemode",
                    }
                ):
                    raise HTTPException(status_code=400, detail="invalid workload or mode")
                allowed_keys = required | {
                    "version",
                    "fixture_id",
                    "discovery_config",
                    "input_tokens",
                    "output_tokens",
                    "wall_ms",
                    "upstream_calls",
                    "turns",
                    "recovery_count",
                    "runtime_failure",
                    "run_ids",
                }
                if set(row) - allowed_keys:
                    raise HTTPException(status_code=400, detail="unsupported evidence fields")
                config = row.get("discovery_config", {})
                config_keys = {
                    "task",
                    "synthetic_tools",
                    "discovery_mode",
                    "discovery_backend",
                    "x_intent",
                    "reasoning_effort",
                    "runner_version",
                    "model_source",
                    "model_config",
                }
                if (
                    not isinstance(config, dict)
                    or set(config) - config_keys
                    or any(isinstance(v, (dict, list)) for v in config.values())
                ):
                    raise HTTPException(status_code=400, detail="invalid discovery_config")
                if row.get("run_ids") is not None and (
                    not isinstance(row["run_ids"], list)
                    or any(not isinstance(v, str) for v in row["run_ids"])
                ):
                    raise HTTPException(status_code=400, detail="run_ids must be a list of strings")
                if not isinstance(row.get("runtime_failure", False), bool):
                    raise HTTPException(status_code=400, detail="runtime_failure must be boolean")
                if any(
                    any(
                        term in str(k).lower()
                        for term in ("prompt", "answer", "response", "message")
                    )
                    for k in row
                ):
                    raise HTTPException(
                        status_code=400, detail="prompt and answer content cannot be imported"
                    )
                clean = {
                    k: row.get(k)
                    for k in (
                        "version",
                        "evidence_id",
                        "created_at",
                        "model",
                        "workload",
                        "scenario_id",
                        "repeat_id",
                        "fixture_id",
                        "discovery_config",
                        "mode",
                        "correct",
                        "grounded",
                        "input_tokens",
                        "output_tokens",
                        "wall_ms",
                        "upstream_calls",
                        "turns",
                        "recovery_count",
                        "runtime_failure",
                        "run_ids",
                    )
                }
                if not isinstance(clean["correct"], bool) or not isinstance(
                    clean["grounded"], bool
                ):
                    raise HTTPException(
                        status_code=400, detail="correct and grounded must be booleans"
                    )
                for metric in (
                    "input_tokens",
                    "output_tokens",
                    "wall_ms",
                    "upstream_calls",
                    "turns",
                    "recovery_count",
                ):
                    if clean.get(metric) is not None and (
                        not isinstance(clean[metric], (int, float))
                        or isinstance(clean[metric], bool)
                        or not math.isfinite(clean[metric])
                        or clean[metric] < 0
                    ):
                        raise HTTPException(
                            status_code=400, detail=f"{metric} must be numeric or null"
                        )
                clean["created_at"] = created_at.astimezone(UTC).isoformat()
                clean["model"] = str(clean["model"]).strip().lower()
                costs = pricing.get(str(clean["model"]), {})
                input_price, output_price = (
                    float(costs.get("input", 0)),
                    float(costs.get("output", 0)),
                )
                if (
                    clean["input_tokens"] is not None
                    and clean["output_tokens"] is not None
                    and (input_price > 0 or output_price > 0)
                ):
                    clean["cost_usd"] = (
                        clean["input_tokens"] * input_price + clean["output_tokens"] * output_price
                    ) / 1_000_000
                else:
                    clean["cost_usd"] = None
                safe_rows.append(clean)
            if not tenant:
                raise HTTPException(status_code=401, detail="tenant identity is required")
            imported = await runtime.store.import_adaptive_evidence(tenant, safe_rows)
            return {"received": len(rows), "imported": imported, "duplicates": len(rows) - imported}

        @app.post("/admin/adaptive-execution/settings")
        async def admin_set_adaptive_execution(
            request: Request,
            authorization: str | None = Header(default=None),
            mcpcodeaway_admin_session: str | None = Cookie(default=None),
        ) -> dict[str, Any]:
            identity = await _authorize_control_plane(
                authorization,
                mcpcodeaway_admin_session,
                require_admin_role=True,
                allow_viewer=False,
            )
            tenant = _resolve_audit_tenant_scope(identity, None) or (
                identity.tenant_id
                if identity
                else (
                    runtime.settings.allowed_tenants[0]
                    if runtime.settings.allowed_tenants
                    else "tenant-acme-dev"
                )
            )
            body = await request.json()
            mode = body.get("mode") if isinstance(body, dict) else None
            if mode not in {"observe", "active"}:
                raise HTTPException(status_code=400, detail="mode must be observe or active")
            key = f"adaptive_execution:{tenant or ''}"
            await runtime.store.save_setting(
                key, {"mode": mode, "updated_at": datetime.now(UTC).isoformat()}
            )
            return {"mode": mode}

        @app.patch("/admin/upstreams/{name}/mode")
        async def admin_set_upstream_preferred_mode(
            name: str,
            payload: dict[str, Any],
            authorization: str | None = Header(default=None),
            mcpcodeaway_admin_session: str | None = Cookie(default=None),
        ) -> dict[str, Any]:
            await _authorize_control_plane(
                authorization, mcpcodeaway_admin_session, require_admin_role=True
            )
            if not runtime.settings.dynamic_upstreams_permitted:
                raise HTTPException(
                    status_code=403,
                    detail=(
                        "Upstream modes are managed through config/upstreams.yaml in this profile."
                    ),
                )
            cfg = runtime.registry.get(name)
            if cfg is None:
                raise HTTPException(status_code=404, detail=f"Upstream '{name}' not found")
            mode_val = payload.get("preferred_mode") or "auto"
            if mode_val not in {
                "codemode",
                "codemode_upfront",
                "traditional",
                "traditional_discover",
                "hybrid",
                "auto",
            }:
                raise HTTPException(
                    status_code=422,
                    detail="preferred_mode must be a supported gateway mode.",
                )
            updated = cfg.model_copy(update={"preferred_mode": mode_val})
            await runtime.registry.save_upstream(updated)
            await runtime.bump_catalog_epoch()
            return {"upstream": name, "preferred_mode": updated.preferred_mode}

        @app.patch("/admin/upstreams/{name}/auto-discovery")
        async def admin_set_upstream_auto_discovery(
            name: str,
            payload: dict[str, Any],
            authorization: str | None = Header(default=None),
            mcpcodeaway_admin_session: str | None = Cookie(default=None),
        ) -> dict[str, Any]:
            await _authorize_control_plane(
                authorization, mcpcodeaway_admin_session, require_admin_role=True
            )
            cfg = runtime.registry.get(name)
            if cfg is None:
                raise HTTPException(status_code=404, detail=f"Upstream '{name}' not found")
            if not isinstance(payload.get("enabled"), bool):
                raise HTTPException(status_code=422, detail="enabled must be a boolean.")
            updated = cfg.model_copy(update={"auto_discovery_enabled": payload["enabled"]})
            runtime.registry.upsert(updated)
            if hasattr(runtime.store, "upsert_upstream"):
                await runtime.store.upsert_upstream(updated.name, updated.model_dump())
            return {
                "upstream": name,
                "auto_discovery_enabled": updated.auto_discovery_enabled,
            }

        @app.patch("/admin/upstreams/{name}/tools/{tool_name}/mode")
        async def admin_set_tool_mode_override(
            name: str,
            tool_name: str,
            payload: dict[str, Any],
            authorization: str | None = Header(default=None),
            mcpcodeaway_admin_session: str | None = Cookie(default=None),
        ) -> dict[str, Any]:
            await _authorize_control_plane(
                authorization, mcpcodeaway_admin_session, require_admin_role=True
            )
            cfg = runtime.registry.get(name)
            if cfg is None:
                raise HTTPException(status_code=404, detail=f"Upstream '{name}' not found")
            mode_val = payload.get("mode")
            overrides = dict(cfg.tool_mode_overrides)
            if not mode_val or mode_val == "inherit":
                overrides.pop(tool_name, None)
            else:
                overrides[tool_name] = mode_val
            updated = cfg.model_copy(update={"tool_mode_overrides": overrides})
            runtime.registry.upsert(updated)
            if hasattr(runtime.store, "upsert_upstream"):
                await runtime.store.upsert_upstream(updated.name, updated.model_dump())
            return {"upstream": name, "tool": tool_name, "tool_mode_overrides": overrides}

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

            if cfg.allowed_operations is None:
                raise HTTPException(
                    status_code=422,
                    detail=(
                        "Select operations to enable and classify each as read, write, "
                        "or high impact."
                    ),
                )
            if (
                cfg.authorization_mode == "gateway"
                and not cfg.allowed_roles
                and not cfg.allowed_groups
                and not cfg.allow_all_callers
            ):
                raise HTTPException(
                    status_code=422,
                    detail=(
                        "Select gateway roles or groups, explicitly allow all callers, "
                        "or choose upstream authorization/shared access."
                    ),
                )

            inspection, audit = await _inspect_candidate(cfg, authorization)
            discovered_tools = {
                str(tool["name"]): DiscoveredTool.model_validate(tool)
                for tool in inspection.get("all_tools", [])
            }
            unknown_operations = set(cfg.allowed_operations) - discovered_tools.keys()
            if unknown_operations:
                raise HTTPException(
                    status_code=422,
                    detail=(
                        "Selected operations were not discovered: "
                        f"{', '.join(sorted(unknown_operations))}"
                    ),
                )
            missing_effects = set(cfg.allowed_operations) - cfg.tool_effects.keys()
            if missing_effects:
                raise HTTPException(
                    status_code=422,
                    detail=(
                        f"Classify every enabled operation: {', '.join(sorted(missing_effects))}"
                    ),
                )
            for operation, recovery_policy in cfg.tool_recovery_policies.items():
                if not recovery_policy.enabled:
                    continue
                if len(runtime.settings.task_dedupe_secret.get_secret_value()) < 32:
                    raise HTTPException(
                        status_code=422,
                        detail=(
                            "Configure GATEWAY_TASK_DEDUPE_SECRET (at least 32 characters) "
                            "before enabling recovery."
                        ),
                    )
                operation_tool = discovered_tools.get(operation)
                key_name = str(recovery_policy.idempotency_key_argument or "")
                key_schema = (
                    operation_tool.input_schema.get("properties", {}).get(key_name)
                    if operation_tool is not None
                    else None
                )
                if cfg.auth_type == "delegated_token":
                    raise HTTPException(
                        status_code=422,
                        detail=(
                            f"Recovery for '{operation}' requires a background-capable "
                            "upstream credential."
                        ),
                    )
                if operation not in cfg.allowed_operations or cfg.tool_effects.get(
                    operation
                ) not in {"write", "high_impact"}:
                    raise HTTPException(
                        status_code=422,
                        detail=(
                            "Recovery can only be enabled for an enabled write operation: "
                            f"'{operation}'."
                        ),
                    )
                if not isinstance(key_schema, dict) or key_schema.get("type") != "string":
                    raise HTTPException(
                        status_code=422,
                        detail=(
                            f"Recovery key '{key_name}' must be a string property on '{operation}'."
                        ),
                    )
                if recovery_policy.status_tool:
                    status_tool = discovered_tools.get(recovery_policy.status_tool)
                    status_key_schema = (
                        status_tool.input_schema.get("properties", {}).get(
                            recovery_policy.status_key_argument or ""
                        )
                        if status_tool is not None
                        else None
                    )
                    if (
                        status_tool is None
                        or recovery_policy.status_tool not in cfg.allowed_operations
                        or cfg.tool_effects.get(recovery_policy.status_tool) != "read"
                        or not isinstance(status_key_schema, dict)
                        or status_key_schema.get("type") != "string"
                    ):
                        raise HTTPException(
                            status_code=422,
                            detail=(
                                "Recovery status lookup must be an enabled read operation "
                                "with a string key argument."
                            ),
                        )
            for operation in cfg.allowed_operations:
                effect = cfg.tool_effects[operation]
                tool = discovered_tools[operation]
                if effect == "read" and tool.mutating:
                    raise HTTPException(
                        status_code=422,
                        detail=(
                            f"Operation '{operation}' is reported as mutating and cannot "
                            "be classified as read."
                        ),
                    )
                policy = cfg.tool_approval_policies.get(operation)
                expected_mode = (
                    "none"
                    if effect == "read"
                    else (
                        "manager_approval" if effect == "high_impact" else "requester_confirmation"
                    )
                )
                if (policy.mode if policy else "none") != expected_mode:
                    raise HTTPException(
                        status_code=422,
                        detail=(
                            f"Operation '{operation}' requires the '{expected_mode}' policy "
                            "for its effect classification."
                        ),
                    )
            effective_mutating_tools = sorted(
                set(cfg.mutating_tools)
                | {
                    operation
                    for operation, effect in cfg.tool_effects.items()
                    if effect in {"write", "high_impact"}
                }
            )
            if effective_mutating_tools != cfg.mutating_tools:
                cfg = cfg.model_copy(update={"mutating_tools": effective_mutating_tools})
            existing = runtime.registry.get(cfg.name)
            if existing is not None:
                versioned_policies = dict(cfg.tool_approval_policies)
                for tool_key, previous in existing.tool_approval_policies.items():
                    incoming = versioned_policies.get(tool_key)
                    if incoming is None:
                        versioned_policies[tool_key] = ToolApprovalPolicy(
                            mode="none",
                            version=previous.version + 1,
                            confirmation_message=previous.confirmation_message,
                        )
                    else:
                        if not incoming.confirmation_message:
                            incoming = incoming.model_copy(
                                update={"confirmation_message": previous.confirmation_message}
                            )
                        changed = incoming.model_dump(exclude={"version"}) != previous.model_dump(
                            exclude={"version"}
                        )
                        versioned_policies[tool_key] = incoming.model_copy(
                            update={
                                "version": previous.version + 1 if changed else previous.version
                            }
                        )
                cfg = cfg.model_copy(update={"tool_approval_policies": versioned_policies})
            await runtime.store.save_dynamic_upstream(cfg.name, cfg.model_dump(exclude_none=True))
            await runtime.refresh_catalog()
            await runtime.bump_catalog_epoch()
            for tool_key, policy in cfg.tool_approval_policies.items():
                await runtime.store.invalidate_tool_approvals(
                    cfg.name, tool_key, policy.version, datetime.now(UTC).isoformat()
                )
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

        @app.get("/admin/secrets")
        async def list_admin_secrets(
            authorization: str | None = Header(default=None),
            mcpcodeaway_admin_session: str | None = Cookie(default=None),
        ) -> dict[str, Any]:
            await _authorize_control_plane(
                authorization, mcpcodeaway_admin_session, allow_viewer=True
            )
            secrets = await runtime.store.list_gateway_secrets()
            return {"secrets": secrets}

        @app.put("/admin/secrets/{name}")
        async def put_admin_secret(
            name: str,
            body: dict[str, Any],
            authorization: str | None = Header(default=None),
            mcpcodeaway_admin_session: str | None = Cookie(default=None),
        ) -> dict[str, Any]:
            if not re.fullmatch(r"[a-zA-Z0-9_.-]{1,128}", name):
                raise HTTPException(status_code=400, detail="Invalid secret name")
            actor = await _authorize_control_plane(
                authorization, mcpcodeaway_admin_session, require_admin_role=True
            )
            value = str(body.get("value") or "")
            if not value:
                raise HTTPException(status_code=400, detail="Secret value cannot be empty")
            updated_by = actor.email or actor.sub if actor is not None else "admin"
            await runtime.credential_vault.save_gateway_secret(name, value, updated_by=updated_by)
            runtime.secret_resolver.invalidate(f"vault://{name}")
            return {"saved": True, "name": name}

        @app.delete("/admin/secrets/{name}")
        async def delete_admin_secret(
            name: str,
            authorization: str | None = Header(default=None),
            mcpcodeaway_admin_session: str | None = Cookie(default=None),
        ) -> dict[str, Any]:
            await _authorize_control_plane(
                authorization, mcpcodeaway_admin_session, require_admin_role=True
            )
            deleted = await runtime.store.delete_gateway_secret(name)
            runtime.secret_resolver.invalidate(f"vault://{name}")
            if not deleted:
                raise HTTPException(status_code=404, detail=f"Secret '{name}' not found")
            return {"deleted": True, "name": name}

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
