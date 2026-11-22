"""Tool catalog, Python stub compiler, schema-digest versioning, and RBAC-filtered search."""

from __future__ import annotations

import hashlib
import json
import math
from typing import Any

from gateway.auth import CallerIdentity, PolicyEvaluator
from gateway.config import DiscoveryBackendMode, UpstreamConfig, UpstreamRegistry
from gateway.discovery import DiscoveryEngine
from gateway.upstream_clients import (
    DiscoveredTool,
    ToolGuidance,
    UpstreamClientManager,
    compute_schema_digest,
)

_PY_TYPE_MAP = {
    "string": "str",
    "integer": "int",
    "number": "float",
    "boolean": "bool",
    "array": "list[Any]",
    "object": "dict[str, Any]",
}


def estimate_tokens(value: Any) -> int:
    """Estimate LLM token count using ~4 chars/token heuristic."""
    text = value if isinstance(value, str) else json.dumps(value, separators=(",", ":"))
    return max(1, math.ceil(len(text) / 4))


def _unwrap_union(schema: dict[str, Any]) -> tuple[dict[str, Any], bool]:
    variants = schema.get("anyOf") or schema.get("oneOf")
    if not isinstance(variants, list) or not variants:
        return schema, False
    nullable = any(isinstance(v, dict) and v.get("type") == "null" for v in variants)
    non_null = [v for v in variants if isinstance(v, dict) and v.get("type") != "null"]
    if non_null:
        return non_null[0], nullable
    return schema, nullable


def _schema_return_type(schema: dict[str, Any] | None) -> str:
    if not schema:
        return "Any"
    inner, nullable = _unwrap_union(schema)
    if "properties" in inner or inner.get("type") == "object":
        base = "dict[str, Any]"
    elif inner.get("type") == "array":
        base = "list[Any]"
    else:
        base = _PY_TYPE_MAP.get(str(inner.get("type", "any")), "Any")
    return f"{base} | None" if nullable and base != "Any" else base


def _format_schema_shape(schema: dict[str, Any], *, depth: int = 0) -> str:
    inner, nullable = _unwrap_union(schema)
    suffix = " | None" if nullable else ""
    if depth >= 3:
        if "properties" in inner or inner.get("type") == "object":
            return f"dict[str, Any]{suffix}"
        if inner.get("type") == "array":
            return f"list[Any]{suffix}"
    if "properties" in inner or inner.get("type") == "object":
        props = inner.get("properties") or {}
        if not props:
            return f"dict[str, Any]{suffix}"
        items = list(props.items())
        parts = [
            f"'{k}': {_format_schema_shape(v, depth=depth + 1)}"
            for k, v in items[:12]
            if isinstance(v, dict)
        ]
        if len(items) > 12:
            parts.append("...")
        return f"{{{', '.join(parts)}}}{suffix}"
    if inner.get("type") == "array":
        item_schema = inner.get("items")
        inner_str = (
            _format_schema_shape(item_schema, depth=depth + 1)
            if isinstance(item_schema, dict)
            else "Any"
        )
        return f"list[{inner_str}]{suffix}"
    base = _PY_TYPE_MAP.get(str(inner.get("type", "any")), "Any")
    return f"{base}{suffix}" if base != "Any" else "Any"


def render_python_stub(tool: DiscoveredTool) -> str:
    """Compile a compact typed async Python stub (with output shape) for Code Mode prompts."""
    props = (tool.input_schema or {}).get("properties") or {}
    required = set((tool.input_schema or {}).get("required") or [])
    params: list[str] = []

    req_names = [name for name in props if name in required]
    opt_names = [name for name in props if name not in required]

    for pname in req_names + opt_names:
        pschema = props[pname] if isinstance(props[pname], dict) else {}
        ptype = _PY_TYPE_MAP.get(str(pschema.get("type", "any")), "Any")
        if pname in required:
            params.append(f"{pname}: {ptype}")
        else:
            default = repr(pschema["default"]) if "default" in pschema else "None"
            suffix = "" if "default" in pschema else " | None"
            params.append(f"{pname}: {ptype}{suffix} = {default}")

    sig = ", ".join(params)
    doc = " ".join(tool.description.split())
    if len(doc) > 120 and not (tool.guidance and tool.guidance.description):
        doc = f"{doc[:117]}..."
    tag = " [MUTATING]" if tool.mutating else ""
    lines = [f"{doc}{tag}"]
    if tool.guidance:
        lines.extend(
            f"{name}: {hint}"
            for name, hint in tool.guidance.parameter_hints.items()
            if name in props
        )
    ret_type = _schema_return_type(tool.output_schema)
    if tool.output_schema:
        lines.append(f"Returns: {_format_schema_shape(tool.output_schema)}")
    stub_doc = "\n".join(lines).replace("\\", "\\\\").replace('"""', '\\"""')
    return f'async def {tool.qualified_name}({sig}) -> {ret_type}:\n    """{stub_doc}"""'


def to_mcp_tool_definition(tool: DiscoveredTool) -> dict[str, Any]:
    """Convert a DiscoveredTool into a Classic MCP tools/list definition."""
    defn: dict[str, Any] = {
        "name": tool.qualified_name,
        "description": tool.description,
        "inputSchema": tool.input_schema,
        "annotations": tool.annotations or {"readOnlyHint": not tool.mutating},
    }
    if tool.output_schema:
        defn["outputSchema"] = tool.output_schema
    return defn


def _apply_tool_guidance(tool: DiscoveredTool, guidance: ToolGuidance) -> DiscoveredTool:
    properties = dict(tool.input_schema.get("properties") or {})
    for name, hint in guidance.parameter_hints.items():
        if isinstance(schema := properties.get(name), dict):
            description = " ".join(part for part in (schema.get("description"), hint) if part)
            properties[name] = {**schema, "description": description}
    return tool.model_copy(
        update={
            "description": guidance.description or tool.description,
            "input_schema": {**tool.input_schema, "properties": properties}
            if guidance.parameter_hints
            else tool.input_schema,
            "guidance": guidance,
        }
    )


class ToolCatalog:
    """Holds introspected upstream tools, SHA-256 schema digests, and RBAC search."""

    def __init__(self) -> None:
        self._tools_by_server: dict[str, list[DiscoveredTool]] = {}
        self._digests_by_server: dict[str, str] = {}
        self._drift_warnings: list[dict[str, str]] = []
        self._catalog_version: str = hashlib.sha256(b"empty").hexdigest()[:16]

    @property
    def catalog_version(self) -> str:
        return self._catalog_version

    @property
    def digests_by_server(self) -> dict[str, str]:
        return dict(self._digests_by_server)

    @property
    def drift_warnings(self) -> list[dict[str, str]]:
        return list(self._drift_warnings)

    def _recompute_catalog_version(self) -> None:
        digest_input = json.dumps(self._digests_by_server, sort_keys=True).encode("utf-8")
        self._catalog_version = hashlib.sha256(digest_input).hexdigest()[:16]

    def set_server_tools(self, upstream: UpstreamConfig, tools: list[DiscoveredTool]) -> None:
        """Store discovered tools for an upstream and check pinned digest drift."""
        digest = compute_schema_digest(tools)
        self._tools_by_server[upstream.name] = list(tools)
        self._digests_by_server[upstream.name] = digest
        self._drift_warnings = [w for w in self._drift_warnings if w["server"] != upstream.name]
        if upstream.pinned_schema_digest and upstream.pinned_schema_digest != digest:
            self._drift_warnings.append(
                {
                    "server": upstream.name,
                    "pinned": upstream.pinned_schema_digest,
                    "actual": digest,
                    "message": (
                        f"Schema drift detected on '{upstream.name}': "
                        f"pinned {upstream.pinned_schema_digest[:12]} != actual {digest[:12]}"
                    ),
                }
            )
        self._recompute_catalog_version()

    def remove_server(self, server_name: str) -> None:
        self._tools_by_server.pop(server_name, None)
        self._digests_by_server.pop(server_name, None)
        self._drift_warnings = [w for w in self._drift_warnings if w["server"] != server_name]
        self._recompute_catalog_version()

    def get_server_tools(self, server_name: str) -> list[DiscoveredTool]:
        return list(self._tools_by_server.get(server_name, []))

    def get_tool(self, qualified_name: str) -> DiscoveredTool | None:
        for tools in self._tools_by_server.values():
            for tool in tools:
                if tool.qualified_name == qualified_name:
                    return tool
        return None

    async def refresh_all(
        self,
        upstreams: list[UpstreamConfig],
        client_manager: UpstreamClientManager,
        auth_headers_by_server: dict[str, dict[str, str]] | None = None,
    ) -> list[str]:
        """Introspect all enabled upstreams, returning non-fatal warning strings if unreachable."""
        warnings: list[str] = []
        active_names = {u.name for u in upstreams}
        for existing in list(self._tools_by_server):
            if existing not in active_names:
                self.remove_server(existing)

        for upstream in upstreams:
            try:
                headers = (auth_headers_by_server or {}).get(upstream.name)
                tools = await client_manager.discover_tools(upstream, auth_headers=headers)
                self.set_server_tools(upstream, tools)
            except Exception as exc:
                warnings.append(f"Upstream '{upstream.name}' discovery failed: {exc}")
        return warnings

    def list_accessible_servers(
        self,
        identity: CallerIdentity,
        policy: PolicyEvaluator,
        registry: UpstreamRegistry,
        *,
        allowed_servers: set[str] | None = None,
        query: str = "",
    ) -> list[dict[str, Any]]:
        """Return RBAC-filtered server summaries for Level-1 multi-MCP discovery."""
        q_terms = [tok for tok in query.lower().split() if tok]
        servers: list[dict[str, Any]] = []
        for upstream in registry.list_upstreams():
            if allowed_servers is not None and upstream.name not in allowed_servers:
                continue
            if not policy.can_access_upstream(identity, upstream)[0]:
                continue
            permitted_tools = [
                t
                for t in self._tools_by_server.get(upstream.name, [])
                if policy.can_execute_tool(identity, upstream, t.name, t.mutating)[0]
            ]
            if q_terms:
                text = (
                    f"{upstream.name} {upstream.type} "
                    + " ".join(f"{t.name} {t.description}" for t in permitted_tools)
                ).lower()
                if not any(term in text for term in q_terms):
                    continue
            servers.append(
                {
                    "server": upstream.name,
                    "type": upstream.type,
                    "auth_type": upstream.auth_type,
                    "tool_count": len(permitted_tools),
                    "mutating_count": sum(1 for t in permitted_tools if t.mutating),
                    "sample_tools": [t.name for t in permitted_tools[:5]],
                    "summary": (f"{upstream.name} ({upstream.type}, {len(permitted_tools)} tools)"),
                }
            )
        return servers

    def list_accessible_tools(
        self,
        identity: CallerIdentity,
        policy: PolicyEvaluator,
        registry: UpstreamRegistry,
        *,
        allowed_servers: set[str] | None = None,
        guidance: dict[str, dict[str, Any]] | None = None,
    ) -> list[DiscoveredTool]:
        """Return only tools authorized for this caller's roles, groups, client, and scopes."""
        accessible: list[DiscoveredTool] = []
        for upstream in registry.list_upstreams():
            if allowed_servers is not None and upstream.name not in allowed_servers:
                continue
            if not policy.can_access_upstream(identity, upstream)[0]:
                continue
            for tool in self._tools_by_server.get(upstream.name, []):
                if policy.can_execute_tool(identity, upstream, tool.name, tool.mutating)[0]:
                    configured = upstream.tool_guidance.get(tool.name)
                    entry = configured.model_dump() if configured is not None else None
                    saved = (guidance or {}).get(tool.qualified_name)
                    if saved:
                        entry = {
                            **(entry or {}),
                            **saved,
                            "parameter_hints": {
                                **((entry or {}).get("parameter_hints") or {}),
                                **(saved.get("parameter_hints") or {}),
                            },
                        }
                    accessible.append(
                        _apply_tool_guidance(tool, ToolGuidance.model_validate(entry))
                        if entry
                        else tool
                    )
        return accessible

    async def discover_tools_for_caller(
        self,
        identity: CallerIdentity,
        policy: PolicyEvaluator,
        registry: UpstreamRegistry,
        discovery_engine: DiscoveryEngine,
        *,
        query: str = "",
        servers: list[str] | None = None,
        allowed_servers: set[str] | None = None,
        backend: DiscoveryBackendMode | None = None,
        limit: int = 15,
        include_schemas: bool = False,
        guidance: dict[str, dict[str, Any]] | None = None,
    ) -> dict[str, Any]:
        """RBAC-first, ceiling-enforced pluggable tool discovery."""
        accessible = self.list_accessible_tools(
            identity, policy, registry, allowed_servers=allowed_servers, guidance=guidance
        )
        if servers:
            requested = set(servers)
            candidates = [t for t in accessible if t.server in requested]
        else:
            candidates = accessible

        outcome = await discovery_engine.discover(
            query,
            candidates,
            catalog_version=self._catalog_version,
            backend=backend,
            limit=limit,
        )
        payload: dict[str, Any] = {
            "catalog_version": self._catalog_version,
            "backend_requested": outcome.backend_requested,
            "backend_used": outcome.backend_used,
            "fallback_triggered": outcome.fallback_triggered,
            "fallback_reason": outcome.fallback_reason,
            "stage1_servers": outcome.stage1_servers,
            "latency_ms": outcome.latency_ms,
            "total_accessible": len(accessible),
            "returned": len(outcome.tools),
            "tools": [
                {
                    "qualified_name": st.tool.qualified_name,
                    "server": st.tool.server,
                    "name": st.tool.name,
                    "mutating": st.tool.mutating,
                    "score": st.score,
                    "stage1_server_score": st.stage1_server_score,
                    "description": st.tool.description,
                    "stub": render_python_stub(st.tool),
                }
                for st in outcome.tools
            ],
        }
        if include_schemas:
            payload["hydrated_tools"] = [to_mcp_tool_definition(st.tool) for st in outcome.tools]
        return payload

    def search_tools(
        self,
        identity: CallerIdentity,
        policy: PolicyEvaluator,
        registry: UpstreamRegistry,
        *,
        query: str = "",
        server: str | None = None,
        limit: int = 15,
    ) -> dict[str, Any]:
        """Sync keyword discovery kept for backwards compatibility."""
        accessible = self.list_accessible_tools(identity, policy, registry)
        if server:
            accessible = [t for t in accessible if t.server == server]

        terms = [tok for tok in query.lower().split() if tok]
        if not terms:
            ranked = accessible[:limit]
        else:
            scored: list[tuple[int, DiscoveredTool]] = []
            for tool in accessible:
                haystack = (
                    f"{tool.qualified_name} {tool.server} {tool.name} {tool.description}".lower()
                )
                score = sum(
                    (3 if term in tool.name.lower() else 1) for term in terms if term in haystack
                )
                if score > 0:
                    scored.append((score, tool))
            scored.sort(key=lambda pair: (-pair[0], pair[1].qualified_name))
            ranked = [tool for _, tool in scored[:limit]]

        return {
            "catalog_version": self._catalog_version,
            "total_accessible": len(accessible),
            "returned": len(ranked),
            "tools": [
                {
                    "qualified_name": tool.qualified_name,
                    "server": tool.server,
                    "name": tool.name,
                    "mutating": tool.mutating,
                    "description": tool.description,
                    "stub": render_python_stub(tool),
                }
                for tool in ranked
            ],
        }
