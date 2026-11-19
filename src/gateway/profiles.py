"""Composable barebones-first gateway profile loader and inheritance resolver."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel, Field

PROFILE_SETTING_FIELDS: tuple[str, ...] = (
    "app_env",
    "auth_mode",
    "token_exchange_dialect",
    "gateway_mode",
    "execution_mode",
    "discovery_mode",
    "require_admin_auth",
    "require_tool_grounding",
    "allow_dynamic_upstreams",
    "upstreams_config_path",
)


class ProfileFeatures(BaseModel):
    """Feature flags enabled by a profile (defaults to minimal barebones production)."""

    audit_api: bool = True
    metrics: bool = True
    admin_read: bool = True
    admin_write: bool = False
    dashboard_ui: bool = False
    comparator: bool = False
    benchmarks_api: bool = False
    dev_auth: bool = False


class GatewayProfile(BaseModel):
    """Declarative gateway profile overriding barebones production or another parent profile."""

    name: str
    description: str = ""
    extends: str | None = None
    app_env: str | None = None
    auth_mode: str | None = None
    token_exchange_dialect: str | None = None
    gateway_mode: str | None = None
    execution_mode: str | None = None
    discovery_mode: str | None = None
    require_admin_auth: bool | None = None
    require_tool_grounding: bool | None = None
    allow_dynamic_upstreams: bool | None = None
    upstreams_config_path: str | None = None
    features: dict[str, bool] = Field(default_factory=dict)


class ResolvedProfile(BaseModel):
    """Fully resolved profile settings and feature flags after inheritance flattening."""

    name: str
    description: str = ""
    inheritance_chain: list[str] = Field(default_factory=list)
    app_env: str | None = None
    auth_mode: str | None = None
    token_exchange_dialect: str | None = None
    gateway_mode: str | None = None
    execution_mode: str | None = None
    discovery_mode: str | None = None
    require_admin_auth: bool | None = None
    require_tool_grounding: bool | None = None
    allow_dynamic_upstreams: bool | None = None
    upstreams_config_path: str | None = None
    features: ProfileFeatures = Field(default_factory=ProfileFeatures)


BUILTIN_PROFILES: dict[str, dict[str, Any]] = {
    "production": {
        "description": "Barebones production gateway: MCP, OIDC/OBO, metrics, read-only GitOps.",
        "extends": None,
        "app_env": "prod",
        "auth_mode": "oidc",
        "require_admin_auth": True,
        "allow_dynamic_upstreams": False,
        "features": {
            "audit_api": True,
            "metrics": True,
            "admin_read": True,
            "admin_write": False,
            "dashboard_ui": False,
            "comparator": False,
            "benchmarks_api": False,
            "dev_auth": False,
        },
    },
    "staging": {
        "description": (
            "Onboarding/UAT profile extending production with HITL wizard and admin API."
        ),
        "extends": "production",
        "app_env": "uat",
        "auth_mode": "oidc",
        "require_admin_auth": True,
        "allow_dynamic_upstreams": True,
        "features": {
            "admin_write": True,
            "dashboard_ui": True,
        },
    },
    "demo": {
        "description": "Local evaluation profile extending staging with dev JWTs and benchmarks.",
        "extends": "staging",
        "app_env": "sandbox",
        "auth_mode": "dev",
        "token_exchange_dialect": "dev_local",
        "require_admin_auth": False,
        "allow_dynamic_upstreams": True,
        "features": {
            "dev_auth": True,
            "comparator": True,
            "benchmarks_api": True,
        },
    },
}


def load_profiles(config_path: str | Path | None = None) -> dict[str, dict[str, Any]]:
    """Return built-in profiles merged with any custom definitions from YAML."""
    merged: dict[str, dict[str, Any]] = {
        name: {**raw, "features": dict(raw.get("features") or {})}
        for name, raw in BUILTIN_PROFILES.items()
    }
    if config_path is None:
        return merged
    path = Path(config_path)
    if not path.exists():
        return merged

    raw_yaml = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    entries = raw_yaml.get("profiles", {}) if isinstance(raw_yaml, dict) else {}
    if not isinstance(entries, dict):
        raise ValueError(f"Invalid profiles YAML structure in '{path}'")

    for name, item in entries.items():
        if not isinstance(item, dict):
            raise ValueError(f"Profile '{name}' in '{path}' must be a mapping")
        base = merged.get(name, {})
        combined_features = {
            **dict(base.get("features") or {}),
            **dict(item.get("features") or {}),
        }
        merged[name] = {**base, **item, "features": combined_features}
    return merged


def resolve_profile(name: str, config_path: str | Path | None = None) -> ResolvedProfile:
    """Resolve a profile and its inheritance chain (defaulting custom roots to 'production')."""
    catalog = load_profiles(config_path)
    if name not in catalog:
        available = ", ".join(sorted(catalog))
        raise ValueError(f"Unknown gateway profile '{name}'. Available: {available}")

    chain_raw: list[tuple[str, dict[str, Any]]] = []
    seen: set[str] = set()
    cursor: str | None = name
    while cursor is not None:
        if cursor in seen:
            cycle = " -> ".join([c for c, _ in chain_raw] + [cursor])
            raise ValueError(f"Circular profile inheritance detected: {cycle}")
        if cursor not in catalog:
            raise ValueError(f"Profile '{name}' extends unknown parent profile '{cursor}'")
        seen.add(cursor)
        node = catalog[cursor]
        chain_raw.append((cursor, node))
        parent = node.get("extends")
        if parent is None and cursor != "production":
            parent = "production"
        cursor = parent

    chain_raw.reverse()
    merged_scalars: dict[str, Any] = {}
    merged_features: dict[str, bool] = ProfileFeatures().model_dump()
    description = ""

    for prof_name, node in chain_raw:
        parsed = GatewayProfile.model_validate({"name": prof_name, **node})
        for field_name in PROFILE_SETTING_FIELDS:
            val = getattr(parsed, field_name)
            if val is not None:
                merged_scalars[field_name] = val
        for feat_key, feat_val in parsed.features.items():
            if feat_key not in merged_features:
                raise ValueError(f"Unknown feature flag '{feat_key}' in profile '{prof_name}'")
            merged_features[feat_key] = bool(feat_val)
        if parsed.description:
            description = parsed.description

    return ResolvedProfile(
        name=name,
        description=description,
        inheritance_chain=[prof_name for prof_name, _ in chain_raw],
        features=ProfileFeatures.model_validate(merged_features),
        **merged_scalars,
    )
