"""Gateway settings, RBAC policy config, and upstream registry."""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any, Literal

import yaml
from pydantic import BaseModel, Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

from gateway.state import AuditStore

AppEnv = Literal["sandbox", "uat", "prod"]
AuthMode = Literal["dev", "oidc"]
TokenExchangeDialect = Literal["dev_local", "rfc8693", "entra_v2_obo"]
ExecutionMode = Literal["classic", "codemode", "hybrid"]
DiscoveryMode = Literal["upfront", "deferred", "x_intent"]
GatewayMode = Literal[
    "codemode", "codemode_upfront", "traditional", "traditional_discover", "hybrid"
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
    upstreams_config_path: str = "config/upstreams.yaml"

    require_admin_auth: bool | None = None
    admin_roles: list[str] = Field(default_factory=lambda: ["gateway_admin", "platform_admin"])
    ssrf_protection_enabled: bool = True
    block_private_ips_in_prod: bool = True
    allowed_upstream_hosts: list[str] = Field(default_factory=list)
    otlp_endpoint: str | None = None

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
