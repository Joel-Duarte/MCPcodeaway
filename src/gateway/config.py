"""Gateway settings, RBAC policy config, and upstream registry."""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any, Literal

import yaml
from pydantic import BaseModel, Field, PrivateAttr, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

from gateway.profiles import PROFILE_SETTING_FIELDS, ProfileFeatures, resolve_profile
from gateway.state import AuditStore

AppEnv = Literal["sandbox", "uat", "prod"]
AuthMode = Literal["dev", "oidc"]
TokenExchangeDialect = Literal["dev_local", "rfc8693", "entra_v2_obo"]
ExecutionMode = Literal["classic", "codemode", "hybrid", "auto"]
DiscoveryMode = Literal["upfront", "deferred", "x_intent"]
GatewayMode = Literal[
    "codemode", "codemode_upfront", "traditional", "traditional_discover", "hybrid", "auto"
]
DiscoveryBackendMode = Literal["keyword", "bm25", "embedding", "system1", "hybrid_system1"]
UpstreamType = Literal["mcp_http", "mcp_stdio", "openapi"]
UpstreamAuthType = Literal["delegated_token", "bearer_secret", "api_key", "none"]

_SLUG_RE = re.compile(r"^[a-z][a-z0-9_]*$")


class ClaimMappingConfig(BaseModel):
    """Configurable OIDC claim mapping with fallback claim names across IdPs."""

    subject: str = "sub"
    tenant: str = "tid"
    email: str = "email"
    roles: str = "roles"
    groups: str = "groups"
    scopes: str = "scp"
    client_id: str = "azp"
    client_groups: str = "client_groups"


class AbacRuleConfig(BaseModel):
    """Gateway-enforced argument & response-row ABAC rule keyed on a caller claim."""

    claim: str = "client_groups"
    arg_name: str | None = None
    response_list_key: str | None = None
    row_field: str | None = None
    enforce_on_args: bool = True
    filter_response_rows: bool = True
    require_claim: bool = False


class AbExperimentConfig(BaseModel):
    """Live per-upstream Classic vs. Code Mode A/B traffic experiment settings."""

    enabled: bool = False
    experiment_id: str = ""
    codemode_traffic_pct: int = Field(default=50, ge=0, le=100)
    read_only_only: bool = True
    auto_promote_min_runs: int = Field(default=10, ge=2, le=10000)
    auto_switch_enabled: bool = True


class UpstreamConfig(BaseModel):
    """Declarative configuration for a downstream MCP server or OpenAPI service."""

    name: str
    type: UpstreamType
    url: str | None = None
    openapi_spec_url: str | None = None
    openapi_spec: dict[str, Any] | None = None
    command: str | None = None
    args: list[str] = Field(default_factory=list)
    env: dict[str, str] = Field(default_factory=dict)
    auth_type: UpstreamAuthType = "delegated_token"
    downstream_audience: str | None = None
    downstream_scope: str | None = None
    secret_env_var: str | None = None
    api_key_header: str = "X-API-Key"
    allowed_roles: list[str] = Field(default_factory=list)
    allowed_groups: list[str] = Field(default_factory=list)
    allowed_clients: list[str] = Field(default_factory=list)
    allowed_scopes: list[str] = Field(default_factory=list)
    mutating_tools: list[str] = Field(default_factory=list)
    allowed_operations: list[str] = Field(default_factory=list)
    excluded_operations: list[str] = Field(default_factory=list)
    preferred_mode: GatewayMode | None = None
    abac_rules: list[AbacRuleConfig] = Field(default_factory=list)
    pinned_schema_digest: str | None = None
    stateful_session: bool = False
    ab_experiment: AbExperimentConfig | None = None
    enabled: bool = True

    @field_validator("name")
    @classmethod
    def _validate_name(cls, value: str) -> str:
        if not _SLUG_RE.match(value):
            raise ValueError(
                f"Upstream name '{value}' must match ^[a-z][a-z0-9_]*$ (lowercase slug)"
            )
        return value


class GatewaySettings(BaseSettings):
    """Environment-driven settings for the Code-Mode Universal MCP Gateway."""

    model_config = SettingsConfigDict(
        env_prefix="GATEWAY_",
        case_sensitive=False,
        extra="ignore",
    )

    profile: str | None = None
    profiles_config_path: str = "config/profiles.yaml"

    app_env: AppEnv = "sandbox"
    auth_mode: AuthMode = "dev"
    token_exchange_dialect: TokenExchangeDialect = "dev_local"
    gateway_mode: GatewayMode = "codemode"
    execution_mode: ExecutionMode | None = None
    discovery_mode: DiscoveryMode | None = None
    read_only_prod: bool = True
    allow_dynamic_upstreams: bool = True

    gateway_public_url: str = "http://127.0.0.1:8000"
    oidc_issuer: str = "http://127.0.0.1:8000/dev/oidc"
    oidc_jwks_uri: str | None = None
    oidc_audience: str = "api://mcp-code-gateway"
    oidc_token_endpoint: str | None = None
    oidc_client_id: str = "mcp-code-gateway-client"
    oidc_client_secret: str = ""
    allowed_tenants: list[str] = Field(default_factory=lambda: ["tenant-acme-dev"])
    claim_mapping: ClaimMappingConfig = Field(default_factory=ClaimMappingConfig)

    token_cache_ttl_seconds: int = 300
    max_calls_per_execution: int = 50
    rate_limit_per_minute: int = 120
    monty_timeout_seconds: float = 10.0

    sandbox_backend: Literal["local", "remote"] = "local"
    sandbox_worker_url: str | None = None
    sandbox_worker_secret: str = ""
    require_tool_grounding: bool = False
    provenance_signing_secret: str = "dev-provenance-secret"
    redis_url: str | None = None

    discovery_backend: DiscoveryBackendMode = "bm25"
    system1_url: str | None = None
    system1_model: str = "laya"
    system1_api_key: str = ""
    system1_threshold: float = 0.35
    embedding_url: str | None = None
    embedding_model: str = "text-embedding-qwen3-embedding-0.6b"
    embedding_api_key: str = ""

    sqlite_path: str = "data/gateway.db"
    postgres_dsn: str | None = None
    upstreams_config_path: str = "config/upstreams.yaml"

    require_admin_auth: bool | None = None
    admin_roles: list[str] = Field(default_factory=lambda: ["gateway_admin", "platform_admin"])
    viewer_roles: list[str] = Field(default_factory=lambda: ["operator_viewer"])
    ssrf_protection_enabled: bool = True
    block_private_ips_in_prod: bool = True
    allowed_upstream_hosts: list[str] = Field(default_factory=list)
    otlp_endpoint: str | None = None
    log_format: Literal["json", "text"] = "json"
    log_level: str = "INFO"
    cost_per_1m_input_tokens_usd: float = 3.0
    cost_per_1m_output_tokens_usd: float = 15.0
    context_overflow_threshold_tokens: int = 32768
    metrics_require_auth: bool = False

    mcp_session_ttl_seconds: int = 1800
    audit_retention_days: int = 90
    admin_session_secret: str = "dev-admin-session-secret"
    admin_session_ttl_seconds: int = 28800
    admin_cookie_samesite: Literal["lax", "strict", "none"] = "lax"
    admin_oidc_authorization_endpoint: str | None = None
    admin_oidc_redirect_uri: str | None = None
    admin_oidc_scopes: str = "openid profile email"
    cors_allowed_origins: list[str] = Field(default_factory=list)
    model_pricing: dict[str, dict[str, float]] = Field(
        default_factory=lambda: {
            "qwen3.5-9b": {"input": 0.10, "output": 0.15},
            "openrouter-auto": {"input": 0.50, "output": 1.50},
            "gemini-4-hydrogen": {"input": 1.50, "output": 7.50},
            "gemini-4-argon": {"input": 2.00, "output": 10.0},
            "gemini-4-neon": {"input": 3.00, "output": 15.0},
            "gemini-3.8-flash": {"input": 0.75, "output": 3.75},
            "claude-haiku-5.5": {"input": 0.10, "output": 0.50},
            "claude-sonnet-5.5": {"input": 2.00, "output": 10.0},
            "claude-opus-5.5": {"input": 4.00, "output": 20.0},
            "gpt-6-luna": {"input": 0.10, "output": 0.50},
            "gpt-6.1-sol": {"input": 2.00, "output": 10.0},
            "gpt-6.2-astra": {"input": 10.00, "output": 50.0},
        }
    )
    audit_archive_s3_bucket: str = ""
    audit_archive_s3_prefix: str = "mcpcodeaway/audit-archive/"
    audit_archive_s3_endpoint: str = ""
    audit_archive_s3_region: str = "us-east-1"

    _effective_profile_name: str = PrivateAttr(default="demo")
    _resolved_features: ProfileFeatures = PrivateAttr(default_factory=ProfileFeatures)

    @model_validator(mode="after")
    def _apply_profile_defaults(self) -> GatewaySettings:
        if self.profile:
            resolved = resolve_profile(self.profile, self.profiles_config_path)
            for field_name in PROFILE_SETTING_FIELDS:
                val = getattr(resolved, field_name)
                if val is not None and field_name not in self.model_fields_set:
                    setattr(self, field_name, val)
            self._effective_profile_name = resolved.name
            self._resolved_features = resolved.features
            return self

        if self.app_env == "prod" and self.auth_mode == "oidc":
            self._effective_profile_name = "production"
            self._resolved_features = ProfileFeatures()
        else:
            self._effective_profile_name = "demo" if self.auth_mode == "dev" else "custom"
            self._resolved_features = ProfileFeatures(
                audit_api=True,
                metrics=True,
                admin_read=True,
                admin_write=True,
                dashboard_ui=True,
                comparator=True,
                benchmarks_api=True,
                dev_auth=(self.auth_mode == "dev"),
            )
        return self

    @property
    def effective_profile_name(self) -> str:
        """Resolved active profile name."""
        return self._effective_profile_name

    @property
    def active_features(self) -> ProfileFeatures:
        """Resolved feature flags controlling which control-plane/demo surfaces mount."""
        return self._resolved_features

    @property
    def dynamic_upstreams_permitted(self) -> bool:
        """Dynamic runtime upstream registration is blocked in prod."""
        return self.allow_dynamic_upstreams and self.app_env != "prod"

    @property
    def admin_auth_required(self) -> bool:
        """Require authenticated control-plane callers on /admin/* and /audit/* routes."""
        if self.require_admin_auth is not None:
            return self.require_admin_auth
        return self.auth_mode == "oidc"

    @property
    def sandbox_worker_secrets(self) -> list[str]:
        """Parse active + previous sandbox worker secrets for zero-downtime key rotation."""
        return [s.strip() for s in self.sandbox_worker_secret.split(",") if s.strip()]

    @property
    def provenance_signing_secrets(self) -> list[str]:
        """Parse active + previous HMAC provenance keys for zero-downtime rotation."""
        keys = [s.strip() for s in self.provenance_signing_secret.split(",") if s.strip()]
        return keys or ["dev-provenance-secret"]

    @property
    def admin_session_secrets(self) -> list[str]:
        """Parse active + previous admin session cookie HMAC signing keys."""
        keys = [s.strip() for s in self.admin_session_secret.split(",") if s.strip()]
        return keys or ["dev-admin-session-secret"]


class UpstreamRegistry:
    """Loads static YAML upstreams and merges permitted dynamic SQLite upstreams."""

    def __init__(self, settings: GatewaySettings, store: AuditStore | None = None) -> None:
        self.settings = settings
        self.store = store
        self._static: dict[str, UpstreamConfig] = {}
        self._dynamic: dict[str, UpstreamConfig] = {}
        self._load_yaml()

    def _load_yaml(self) -> None:
        self._static.clear()
        path = Path(self.settings.upstreams_config_path)
        if not path.exists():
            return
        raw = yaml.safe_load(path.read_text(encoding="utf-8"))
        if not raw:
            return
        items = raw.get("upstreams", []) if isinstance(raw, dict) else raw
        for item in items:
            cfg = UpstreamConfig.model_validate(item)
            self._static[cfg.name] = cfg

    async def refresh(self) -> None:
        """Reload static YAML and permitted dynamic upstreams from AuditStore."""
        self._load_yaml()
        self._dynamic.clear()
        if self.store is not None and self.settings.dynamic_upstreams_permitted:
            for item in await self.store.list_dynamic_upstreams():
                cfg = UpstreamConfig.model_validate(item)
                self._dynamic[cfg.name] = cfg

    def list_upstreams(self, include_disabled: bool = False) -> list[UpstreamConfig]:
        """Return effective upstreams respecting prod stdio restrictions."""
        merged: dict[str, UpstreamConfig] = dict(self._static)
        if self.settings.dynamic_upstreams_permitted:
            merged.update(self._dynamic)
        result: list[UpstreamConfig] = []
        for cfg in merged.values():
            if not include_disabled and not cfg.enabled:
                continue
            if self.settings.app_env == "prod" and cfg.type == "mcp_stdio":
                continue
            result.append(cfg)
        return sorted(result, key=lambda c: c.name)

    def get(self, name: str) -> UpstreamConfig | None:
        for cfg in self.list_upstreams(include_disabled=True):
            if cfg.name == name:
                return cfg
        return None

    def upsert(self, cfg: UpstreamConfig) -> None:
        """Update an upstream configuration in-memory in the static registry."""
        self._static[cfg.name] = cfg

    async def save_upstream(self, cfg: UpstreamConfig) -> None:
        """Persist a dynamic upstream to AuditStore (when permitted) and update registry."""
        if self.store is not None and self.settings.dynamic_upstreams_permitted:
            await self.store.save_dynamic_upstream(cfg.name, cfg.model_dump(exclude_none=True))
            self._dynamic[cfg.name] = cfg
        else:
            self.upsert(cfg)
