"""Unified upstream transport adapters for MCP HTTP, MCP stdio, and Swagger/OpenAPI."""

from __future__ import annotations

import hashlib
import ipaddress
import json
import re
from typing import Any
from urllib.parse import urlparse

import httpx
import yaml
from mcp import ClientSession, StdioServerParameters, stdio_client
from pydantic import BaseModel, Field

from gateway.config import UpstreamConfig

_MUTATING_METHODS = {"POST", "PUT", "PATCH", "DELETE"}
_CLOUD_METADATA_HOSTS = {
    "metadata.google.internal",
    "metadata",
    "169.254.169.254",
    "100.100.100.200",
    "fd00:ec2::254",
}


def validate_upstream_url(
    url: str | None,
    *,
    app_env: str = "sandbox",
    block_private_ips_in_prod: bool = True,
    allowed_hosts: list[str] | None = None,
) -> None:
    """Reject unsafe URL schemes, cloud metadata hosts, and unallowlisted internal IPs."""
    if not url:
        return
    parsed = urlparse(url)
    scheme = parsed.scheme.lower()
    if scheme not in {"http", "https"}:
        raise ValueError(
            f"Unsupported upstream URL scheme '{parsed.scheme}': only http/https allowed"
        )
    host = (parsed.hostname or "").strip().lower().rstrip(".")
    if not host:
        raise ValueError(f"Upstream URL '{url}' is missing a valid hostname")

    allowset = {h.strip().lower().rstrip(".") for h in (allowed_hosts or []) if h.strip()}
    if host in allowset:
        return

    if host in _CLOUD_METADATA_HOSTS or host.endswith(".metadata.google.internal"):
        raise ValueError(f"Blocked cloud metadata endpoint host '{host}' in upstream URL")

    try:
        ip_obj = ipaddress.ip_address(host)
    except ValueError:
        ip_obj = None

    if ip_obj is not None:
        if isinstance(ip_obj, ipaddress.IPv6Address) and ip_obj.ipv4_mapped is not None:
            ip_obj = ip_obj.ipv4_mapped
        if ip_obj.is_link_local or ip_obj.is_multicast or ip_obj.is_unspecified:
            raise ValueError(f"Blocked link-local or unspecified IP '{host}' in upstream URL")
        if app_env == "prod" and block_private_ips_in_prod and (
            ip_obj.is_private or ip_obj.is_loopback or ip_obj.is_reserved
        ):
            raise ValueError(
                f"Blocked private/loopback IP '{host}' in prod; add host to allowed_upstream_hosts"
            )
    elif app_env == "prod" and block_private_ips_in_prod:
        if host == "localhost" or host.endswith((".localhost", ".local", ".internal")):
            raise ValueError(
                f"Blocked local/internal hostname '{host}' in prod; "
                "add host to allowed_upstream_hosts"
            )
_MUTATING_PREFIXES = (
    "create_",
    "delete_",
    "remove_",
    "update_",
    "patch_",
    "send_",
    "post_",
    "issue_",
    "drop_",
    "write_",
)


class DiscoveredTool(BaseModel):
    """Normalized tool descriptor across MCP HTTP, MCP stdio, and OpenAPI backends."""

    server: str
    name: str
    qualified_name: str
    description: str = ""
    input_schema: dict[str, Any] = Field(
        default_factory=lambda: {"type": "object", "properties": {}}
    )
    output_schema: dict[str, Any] | None = None
    mutating: bool = False
    http_method: str | None = None
    http_path: str | None = None
    path_params: list[str] = Field(default_factory=list)
    query_params: list[str] = Field(default_factory=list)
    has_body: bool = False
    annotations: dict[str, Any] = Field(default_factory=dict)


def infer_mutating(
    name: str,
    annotations: dict[str, Any] | None = None,
    http_method: str | None = None,
    explicit_mutating: list[str] | None = None,
) -> bool:
    """Infer whether a tool performs state-changing writes."""
    if explicit_mutating and name in explicit_mutating:
        return True
    if http_method and http_method.upper() in _MUTATING_METHODS:
        return True
    if annotations:
        destructive = (
            annotations.get("destructiveHint") is True
            or annotations.get("destructive_hint") is True
        )
        if destructive:
            return True
        if annotations.get("readOnlyHint") is False or annotations.get("read_only_hint") is False:
            return True
        if annotations.get("readOnlyHint") is True or annotations.get("read_only_hint") is True:
            return False
    lowered = name.lower()
    return any(lowered.startswith(prefix) for prefix in _MUTATING_PREFIXES)


def compute_schema_digest(tools: list[DiscoveredTool]) -> str:
    """Compute deterministic SHA-256 schema digest for drift detection."""
    canonical = [
        {
            "name": t.name,
            "description": t.description,
            "input_schema": t.input_schema,
            "output_schema": t.output_schema,
            "mutating": t.mutating,
        }
        for t in sorted(tools, key=lambda item: item.name)
    ]
    encoded = json.dumps(canonical, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _sanitize_identifier(raw: str) -> str:
    cleaned = re.sub(r"[^a-zA-Z0-9_]+", "_", raw).strip("_")
    if not cleaned:
        return "operation"
    if cleaned[0].isdigit():
        return f"op_{cleaned}"
    return cleaned


def _resolve_refs(
    node: Any, root: dict[str, Any], seen: set[str] | None = None
) -> Any:
    seen_refs = seen or set()
    if isinstance(node, list):
        return [_resolve_refs(item, root, seen_refs) for item in node]
    if not isinstance(node, dict):
        return node
    ref = node.get("$ref")
    if isinstance(ref, str) and ref.startswith("#/") and ref not in seen_refs:
        target: Any = root
        for part in ref[2:].split("/"):
            if isinstance(target, dict) and part in target:
                target = target[part]
            else:
                target = None
                break
        if isinstance(target, dict):
            merged = {k: v for k, v in node.items() if k != "$ref"}
            resolved = _resolve_refs(target, root, seen_refs | {ref})
            if isinstance(resolved, dict):
                return {**resolved, **merged}
            return resolved
    return {k: _resolve_refs(v, root, seen_refs) for k, v in node.items()}


def _parse_mcp_jsonrpc_response(resp: httpx.Response) -> dict[str, Any]:
    content_type = resp.headers.get("content-type", "").lower()
    if "text/event-stream" in content_type:
        last_data: dict[str, Any] | None = None
        for line in resp.text.splitlines():
            if line.startswith("data:"):
                payload = line[5:].strip()
                if payload:
                    last_data = json.loads(payload)
        if last_data is not None:
            return last_data
    return resp.json()


def _extract_mcp_call_result(result: dict[str, Any]) -> Any:
    if result.get("isError") or result.get("is_error"):
        content = result.get("content") or []
        msg = content[0].get("text") if content and isinstance(content[0], dict) else str(result)
        raise RuntimeError(f"Downstream MCP tool error: {msg}")
    structured = result.get("structuredContent")
    if structured is None:
        structured = result.get("structured_content")
    if structured is not None:
        return structured
    content = result.get("content") or []
    texts = [
        item.get("text", "")
        for item in content
        if isinstance(item, dict) and item.get("type") == "text"
    ]
    if len(texts) == 1:
        try:
            return json.loads(texts[0])
        except json.JSONDecodeError:
            return texts[0]
    if texts:
        return "\n".join(texts)
    return result


class UpstreamClientManager:
    """Handles discovery and invocation across MCP HTTP, MCP stdio, and OpenAPI backends."""

    def __init__(self, http_client: httpx.AsyncClient | None = None) -> None:
        self._http_client = http_client

    async def _request(self, method: str, url: str, **kwargs: Any) -> httpx.Response:
        if self._http_client is not None:
            return await self._http_client.request(method, url, **kwargs)
        async with httpx.AsyncClient(timeout=15.0) as client:
            return await client.request(method, url, **kwargs)

    @staticmethod
    def filter_tools(
        upstream: UpstreamConfig, tools: list[DiscoveredTool]
    ) -> list[DiscoveredTool]:
        """Apply optional allowed_operations / excluded_operations curation lists."""
        filtered = list(tools)
        if upstream.allowed_operations:
            allowed = set(upstream.allowed_operations)
            filtered = [t for t in filtered if t.name in allowed]
        if upstream.excluded_operations:
            excluded = set(upstream.excluded_operations)
            filtered = [t for t in filtered if t.name not in excluded]
        return filtered

    async def discover_tools(
        self,
        upstream: UpstreamConfig,
        auth_headers: dict[str, str] | None = None,
        *,
        apply_filters: bool = True,
    ) -> list[DiscoveredTool]:
        if upstream.type == "mcp_http":
            tools = await self._discover_mcp_http(upstream, auth_headers)
        elif upstream.type == "mcp_stdio":
            tools = await self._discover_mcp_stdio(upstream)
        elif upstream.type == "openapi":
            tools = await self._discover_openapi(upstream, auth_headers)
        else:
            raise ValueError(f"Unsupported upstream type '{upstream.type}'")
        return self.filter_tools(upstream, tools) if apply_filters else tools

    async def call_tool(
        self,
        upstream: UpstreamConfig,
        tool: DiscoveredTool,
        arguments: dict[str, Any],
        auth_headers: dict[str, str] | None = None,
    ) -> Any:
        if upstream.type == "mcp_http":
            return await self._call_mcp_http(upstream, tool, arguments, auth_headers)
        if upstream.type == "mcp_stdio":
            return await self._call_mcp_stdio(upstream, tool, arguments)
        if upstream.type == "openapi":
            return await self._call_openapi(upstream, tool, arguments, auth_headers)
        raise ValueError(f"Unsupported upstream type '{upstream.type}'")

    async def _discover_mcp_http(
        self, upstream: UpstreamConfig, auth_headers: dict[str, str] | None
    ) -> list[DiscoveredTool]:
        if not upstream.url:
            raise ValueError(f"mcp_http upstream '{upstream.name}' missing url")
        headers = {
            "Content-Type": "application/json",
            "Accept": "application/json, text/event-stream",
            **(auth_headers or {}),
        }
        resp = await self._request(
            "POST",
            upstream.url,
            headers=headers,
            json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"},
        )
        resp.raise_for_status()
        body = _parse_mcp_jsonrpc_response(resp)
        if "error" in body:
            raise RuntimeError(f"Upstream '{upstream.name}' tools/list failed: {body['error']}")
        raw_tools = (body.get("result") or {}).get("tools", [])
        return [self._normalize_mcp_tool(upstream, item) for item in raw_tools]

    async def _call_mcp_http(
        self,
        upstream: UpstreamConfig,
        tool: DiscoveredTool,
        arguments: dict[str, Any],
        auth_headers: dict[str, str] | None,
    ) -> Any:
        if not upstream.url:
            raise ValueError(f"mcp_http upstream '{upstream.name}' missing url")
        headers = {
            "Content-Type": "application/json",
            "Accept": "application/json, text/event-stream",
            **(auth_headers or {}),
        }
        resp = await self._request(
            "POST",
            upstream.url,
            headers=headers,
            json={
                "jsonrpc": "2.0",
                "id": 1,
                "method": "tools/call",
                "params": {"name": tool.name, "arguments": arguments},
            },
        )
        resp.raise_for_status()
        body = _parse_mcp_jsonrpc_response(resp)
        if "error" in body:
            raise RuntimeError(f"Upstream tool error ({tool.qualified_name}): {body['error']}")
        return _extract_mcp_call_result(body.get("result") or {})

    async def _discover_mcp_stdio(self, upstream: UpstreamConfig) -> list[DiscoveredTool]:
        if not upstream.command:
            raise ValueError(f"mcp_stdio upstream '{upstream.name}' missing command")
        params = StdioServerParameters(
            command=upstream.command,
            args=upstream.args,
            env=upstream.env or None,
        )
        async with stdio_client(params) as (read_stream, write_stream):
            async with ClientSession(read_stream, write_stream) as session:
                await session.initialize()
                listed = await session.list_tools()
        tools: list[DiscoveredTool] = []
        for item in listed.tools:
            annotations = item.annotations.model_dump() if item.annotations else {}
            schema = getattr(item, "input_schema", None) or getattr(item, "inputSchema", None)
            out_schema = getattr(item, "output_schema", None) or getattr(item, "outputSchema", None)
            tools.append(
                self._normalize_mcp_tool(
                    upstream,
                    {
                        "name": item.name,
                        "description": item.description or "",
                        "inputSchema": schema or {"type": "object", "properties": {}},
                        "outputSchema": out_schema,
                        "annotations": annotations,
                    },
                )
            )
        return tools

    async def _call_mcp_stdio(
        self,
        upstream: UpstreamConfig,
        tool: DiscoveredTool,
        arguments: dict[str, Any],
    ) -> Any:
        if not upstream.command:
            raise ValueError(f"mcp_stdio upstream '{upstream.name}' missing command")
        params = StdioServerParameters(
            command=upstream.command,
            args=upstream.args,
            env=upstream.env or None,
        )
        async with stdio_client(params) as (read_stream, write_stream):
            async with ClientSession(read_stream, write_stream) as session:
                await session.initialize()
                result = await session.call_tool(tool.name, arguments)
        return _extract_mcp_call_result(result.model_dump())

    def _normalize_mcp_tool(self, upstream: UpstreamConfig, raw: dict[str, Any]) -> DiscoveredTool:
        name = str(raw["name"])
        annotations = dict(raw.get("annotations") or {})
        raw_out = raw.get("outputSchema") or raw.get("output_schema")
        out_schema = dict(raw_out) if isinstance(raw_out, dict) else None
        return DiscoveredTool(
            server=upstream.name,
            name=name,
            qualified_name=f"{upstream.name}__{name}",
            description=str(raw.get("description") or ""),
            input_schema=dict(raw.get("inputSchema") or {"type": "object", "properties": {}}),
            output_schema=out_schema,
            mutating=infer_mutating(
                name,
                annotations=annotations,
                explicit_mutating=upstream.mutating_tools,
            ),
            annotations=annotations,
        )

    async def _load_openapi_spec(
        self, upstream: UpstreamConfig, auth_headers: dict[str, str] | None
    ) -> dict[str, Any]:
        if upstream.openapi_spec is not None:
            spec = upstream.openapi_spec
        elif upstream.openapi_spec_url:
            resp = await self._request(
                "GET", upstream.openapi_spec_url, headers=auth_headers or {}
            )
            resp.raise_for_status()
            spec = yaml.safe_load(resp.text)
        else:
            raise ValueError(
                f"openapi upstream '{upstream.name}' requires openapi_spec or openapi_spec_url"
            )
        if not isinstance(spec, dict):
            raise ValueError(f"Invalid OpenAPI spec for '{upstream.name}'")
        return spec

    def _discover_openapi_from_spec(
        self, upstream: UpstreamConfig, spec: dict[str, Any]
    ) -> list[DiscoveredTool]:

        tools: list[DiscoveredTool] = []
        paths = spec.get("paths") or {}
        http_methods = ("get", "post", "put", "patch", "delete")

        for path_str, path_item in paths.items():
            if not isinstance(path_item, dict):
                continue
            shared_params = list(path_item.get("parameters") or [])
            for method_lower in http_methods:
                op = path_item.get(method_lower)
                if not isinstance(op, dict):
                    continue
                http_method = method_lower.upper()
                op_id = op.get("operationId") or f"{method_lower}_{path_str}"
                tool_name = _sanitize_identifier(str(op_id))
                raw_desc = op.get("summary") or op.get("description") or f"{http_method} {path_str}"
                desc = str(raw_desc)

                properties: dict[str, Any] = {}
                required: list[str] = []
                path_params: list[str] = []
                query_params: list[str] = []

                for raw_param in shared_params + list(op.get("parameters") or []):
                    param = _resolve_refs(raw_param, spec)
                    pname = param.get("name")
                    pin = param.get("in")
                    if not pname or pin not in {"path", "query"}:
                        continue
                    schema = _resolve_refs(param.get("schema") or {"type": "string"}, spec)
                    if param.get("description") and "description" not in schema:
                        schema["description"] = param["description"]
                    properties[pname] = schema
                    if param.get("required") or pin == "path":
                        required.append(pname)
                    if pin == "path":
                        path_params.append(pname)
                    else:
                        query_params.append(pname)

                has_body = False
                req_body = op.get("requestBody")
                if isinstance(req_body, dict):
                    resolved_body = _resolve_refs(req_body, spec)
                    json_content = (resolved_body.get("content") or {}).get("application/json")
                    if isinstance(json_content, dict):
                        has_body = True
                        body_schema = _resolve_refs(json_content.get("schema") or {}, spec)
                        if body_schema.get("type") == "object" or "properties" in body_schema:
                            for b_name, b_prop in (body_schema.get("properties") or {}).items():
                                properties[b_name] = b_prop
                            for b_req in body_schema.get("required") or []:
                                if b_req not in required:
                                    required.append(b_req)
                        else:
                            properties["body"] = body_schema
                            if resolved_body.get("required"):
                                required.append("body")

                input_schema: dict[str, Any] = {"type": "object", "properties": properties}
                if required:
                    input_schema["required"] = required

                output_schema: dict[str, Any] | None = None
                responses = op.get("responses")
                if isinstance(responses, dict):
                    for code_key in ("200", "201", "202", "default"):
                        raw_resp = responses.get(code_key) or (
                            responses.get(int(code_key)) if code_key.isdigit() else None
                        )
                        if not isinstance(raw_resp, dict):
                            continue
                        resolved_resp = _resolve_refs(raw_resp, spec)
                        json_resp = (resolved_resp.get("content") or {}).get("application/json")
                        schema_obj = (
                            json_resp.get("schema") if isinstance(json_resp, dict) else None
                        )
                        if isinstance(schema_obj, dict):
                            cand = _resolve_refs(schema_obj, spec)
                            if cand and not (
                                cand.get("type") == "object" and not cand.get("properties")
                            ):
                                output_schema = cand
                                break

                tools.append(
                    DiscoveredTool(
                        server=upstream.name,
                        name=tool_name,
                        qualified_name=f"{upstream.name}__{tool_name}",
                        description=desc,
                        input_schema=input_schema,
                        output_schema=output_schema,
                        mutating=infer_mutating(
                            tool_name,
                            http_method=http_method,
                            explicit_mutating=upstream.mutating_tools,
                        ),
                        http_method=http_method,
                        http_path=path_str,
                        path_params=path_params,
                        query_params=query_params,
                        has_body=has_body,
                    )
                )
        return tools

    async def _discover_openapi(
        self, upstream: UpstreamConfig, auth_headers: dict[str, str] | None
    ) -> list[DiscoveredTool]:
        spec = await self._load_openapi_spec(upstream, auth_headers)
        return self._discover_openapi_from_spec(upstream, spec)

    async def _call_openapi(
        self,
        upstream: UpstreamConfig,
        tool: DiscoveredTool,
        arguments: dict[str, Any],
        auth_headers: dict[str, str] | None,
    ) -> Any:
        if not upstream.url or not tool.http_path or not tool.http_method:
            raise ValueError(f"OpenAPI tool '{tool.qualified_name}' missing url/path/method")
        resolved_path = tool.http_path
        for pname in tool.path_params:
            if pname not in arguments:
                raise ValueError(f"Missing required path parameter '{pname}'")
            resolved_path = resolved_path.replace(f"{{{pname}}}", str(arguments[pname]))

        query: dict[str, Any] = {}
        body: dict[str, Any] = {}
        for key, val in arguments.items():
            if key in tool.path_params:
                continue
            if key in tool.query_params or (not tool.has_body and tool.http_method == "GET"):
                if val is not None:
                    query[key] = val
            else:
                body[key] = val

        url = f"{upstream.url.rstrip('/')}/{resolved_path.lstrip('/')}"
        req_kwargs: dict[str, Any] = {"headers": auth_headers or {}, "params": query or None}
        if tool.has_body:
            req_kwargs["json"] = body.get("body") if list(body.keys()) == ["body"] else body
        resp = await self._request(tool.http_method, url, **req_kwargs)
        resp.raise_for_status()
        if not resp.text.strip():
            return {"status_code": resp.status_code}
        try:
            return resp.json()
        except ValueError:
            return resp.text


def _extract_openapi_security_schemes(spec: dict[str, Any]) -> list[dict[str, Any]]:
    """Summarize OpenAPI components.securitySchemes into gateway onboarding hints."""
    components = spec.get("components")
    if not isinstance(components, dict):
        return []
    schemes = components.get("securitySchemes")
    if not isinstance(schemes, dict):
        return []
    found: list[dict[str, Any]] = []
    for name, raw in schemes.items():
        if not isinstance(raw, dict):
            continue
        entry: dict[str, Any] = {
            "name": str(name),
            "type": str(raw.get("type", "")),
        }
        if raw.get("scheme"):
            entry["scheme"] = str(raw["scheme"])
        if raw.get("in"):
            entry["in"] = str(raw["in"])
        if raw.get("name"):
            entry["header_name"] = str(raw["name"])
        if raw.get("openIdConnectUrl"):
            entry["openIdConnectUrl"] = str(raw["openIdConnectUrl"])
        flows = raw.get("flows")
        if isinstance(flows, dict):
            flow_summary: dict[str, Any] = {}
            for fname, fval in flows.items():
                if isinstance(fval, dict):
                    flow_summary[fname] = {
                        "tokenUrl": fval.get("tokenUrl"),
                        "authorizationUrl": fval.get("authorizationUrl"),
                        "scopes": sorted((fval.get("scopes") or {}).keys()),
                    }
            if flow_summary:
                entry["flows"] = flow_summary
        found.append(entry)
    return found


def build_agent_onboarding_prompt(
    *,
    upstream: UpstreamConfig | None = None,
    tools: list[DiscoveredTool] | None = None,
    security_schemes: list[dict[str, Any]] | None = None,
    checklist: list[dict[str, Any]] | None = None,
    recommended_mode: str = "codemode",
) -> str:
    """Build a copy-paste prompt for an AI agent to interview a human and emit UpstreamConfig."""
    context_lines: list[str] = []
    if upstream is not None:
        context_lines.append(
            f"- Candidate upstream: name={upstream.name}, type={upstream.type}, "
            f"url={upstream.url or upstream.command or 'inline'}"
        )
    if security_schemes:
        context_lines.append(
            f"- Detected OpenAPI securitySchemes: {json.dumps(security_schemes)}"
        )
    if tools:
        ops_preview = ", ".join(
            f"{t.name}{'[MUTATING]' if t.mutating else ''}" for t in tools[:25]
        )
        suffix = f" (+{len(tools) - 25} more)" if len(tools) > 25 else ""
        context_lines.append(f"- Discovered operations ({len(tools)}): {ops_preview}{suffix}")
    if checklist:
        summary_items = "; ".join(f"[{c['target']}] {c['title']}" for c in checklist)
        context_lines.append(f"- Setup checklist items: {summary_items}")

    context_block = "\n".join(context_lines) if context_lines else "- No spec inspected yet."
    return (
        "You are configuring an upstream API/MCP server for MCPcodeaway.\n"
        "Ask the human operator concise setup questions one topic at a time first:\n"
        "1. Transport & Spec: Confirm upstream slug (`^[a-z][a-z0-9_]*$`), `type` "
        "(`openapi` | `mcp_http` | `mcp_stdio`), `url`, and `openapi_spec_url`.\n"
        "2. Auth & Corporate IdP Propagation:\n"
        '   - Prefer `auth_type="delegated_token"` (RFC 8693 / Entra OBO) when target '
        "APIs accept corporate IdP JWTs; ask for `downstream_audience` and "
        "`downstream_scope`, and remind admin to grant OBO consent on the IdP.\n"
        "   - Otherwise use `api_key` / `bearer_secret` and ask ONLY for `secret_env_var` "
        "(e.g. `PARTNER_API_KEY`) + `api_key_header` (never raw secrets).\n"
        "3. Endpoint Curation: List discovered routes and let the human pick safe ones "
        "for `allowed_operations` or `excluded_operations` (omit unsafe writes).\n"
        "4. Team RBAC & Record/Row ABAC: Ask for `allowed_roles`, `allowed_groups`, "
        "`allowed_scopes`, and whether callers are restricted to subsets like "
        "`client_groups` / `region` via `abac_rules` "
        "(`claim`, `arg_name`, `response_list_key`, `row_field`).\n"
        "5. Execution Mode: Confirm `preferred_mode` (`codemode`, `codemode_upfront`, "
        "`traditional_discover`, `traditional`, or `hybrid`; "
        f"recommended: `{recommended_mode}`).\n\n"
        f"Discovered context:\n{context_block}\n\n"
        "Once answered, emit ONE valid JSON object matching Gateway `UpstreamConfig` "
        "(plus separate Corporate IdP / backend setup bullets if OBO is needed) ready "
        "for `/dashboard` ('Import Agent JSON') or `POST /admin/upstreams`."
    )


def build_onboarding_guidance(
    upstream: UpstreamConfig,
    all_tools: list[DiscoveredTool],
    security_schemes: list[dict[str, Any]],
) -> dict[str, Any]:
    """Produce structured HITL auth/RBAC/ABAC setup prompts for gateway, IdP & backend."""
    checklist: list[dict[str, Any]] = []
    has_oauth_scheme = any(
        s.get("type") in {"oauth2", "openIdConnect"}
        or (s.get("type") == "http" and str(s.get("scheme", "")).lower() == "bearer")
        for s in security_schemes
    )
    has_apikey_scheme = any(s.get("type") == "apiKey" for s in security_schemes)

    if has_oauth_scheme or upstream.auth_type == "delegated_token":
        checklist.append(
            {
                "target": "corporate_idp",
                "severity": "required",
                "field": "downstream_audience",
                "title": "Grant Gateway OBO / RFC 8693 Token Exchange Consent",
                "detail": (
                    "Ask your corporate IdP admin (Entra ID, Okta, Keycloak) to allow the "
                    "gateway client app to exchange user tokens for this API's audience/scopes."
                ),
            }
        )
        checklist.append(
            {
                "target": "gateway",
                "severity": "required",
                "field": "auth_type",
                "title": "Configure Delegated User Token Propagation",
                "detail": (
                    "Set auth_type='delegated_token' with downstream_audience and downstream_scope "
                    "so per-user claims (roles, client_groups) propagate cleanly in Code Mode."
                ),
            }
        )
        checklist.append(
            {
                "target": "target_backend",
                "severity": "recommended",
                "field": "abac_rules",
                "title": "Confirm Backend JWT & Row-Level Security (or Add Gateway ABAC)",
                "detail": (
                    "No backend code changes needed if this service already enforces user/group "
                    "RLS on OIDC JWTs; otherwise attach gateway abac_rules for record isolation."
                ),
            }
        )

    if has_apikey_scheme or upstream.auth_type in {"api_key", "bearer_secret"}:
        header_hint = next(
            (s.get("header_name") for s in security_schemes if s.get("header_name")),
            upstream.api_key_header,
        )
        checklist.append(
            {
                "target": "gateway",
                "severity": "required",
                "field": "secret_env_var",
                "title": "Bind Secret Env Reference & Header Name",
                "detail": (
                    f"Set secret_env_var (never hardcode secrets in YAML) and confirm "
                    f"api_key_header='{header_hint}'."
                ),
            }
        )
        checklist.append(
            {
                "target": "gateway",
                "severity": "recommended",
                "field": "abac_rules",
                "title": "Enforce Host-Side RBAC & Row-Level ABAC on Shared-Key Upstream",
                "detail": (
                    "Shared API keys carry no caller identity; configure allowed_roles plus "
                    "abac_rules (e.g. client_groups -> region) so HostDispatcher filters rows."
                ),
            }
        )

    if not upstream.allowed_roles and not upstream.allowed_groups:
        checklist.append(
            {
                "target": "gateway",
                "severity": "recommended",
                "field": "allowed_roles",
                "title": "Restrict Least-Privilege Team Roles / Groups",
                "detail": (
                    "Set allowed_roles / allowed_groups before production promotion so "
                    "unrelated agent personas cannot discover or invoke this upstream."
                ),
            }
        )

    missing_out = [t.name for t in all_tools if not t.output_schema]
    if missing_out:
        checklist.append(
            {
                "target": "target_backend",
                "severity": "warning",
                "field": "outputSchema",
                "title": f"{len(missing_out)} Operation(s) Missing Typed outputSchema",
                "detail": (
                    "Adding response models/outputSchema avoids runtime key guesses and lets "
                    "Code Mode complete in 2-3 turns instead of retrying KeyErrors."
                ),
            }
        )

    mutating = [t.name for t in all_tools if t.mutating]
    if mutating:
        checklist.append(
            {
                "target": "gateway",
                "severity": "recommended",
                "field": "allowed_operations",
                "title": f"Review {len(mutating)} Mutating Operation(s)",
                "detail": (
                    "Exclude unsafe admin routes via allowed_operations / excluded_operations, "
                    "or use preferred_mode='hybrid' so writes require direct MCP confirmation."
                ),
            }
        )

    read_only_count = len(all_tools) - len(mutating)
    if mutating and read_only_count > 0 and len(all_tools) > 3:
        rec_mode = "hybrid"
    elif len(all_tools) <= 10 and not mutating:
        rec_mode = "traditional"
    elif len(all_tools) <= 25:
        rec_mode = "codemode_upfront"
    else:
        rec_mode = "codemode"

    prompt = build_agent_onboarding_prompt(
        upstream=upstream,
        tools=all_tools,
        security_schemes=security_schemes,
        checklist=checklist,
        recommended_mode=rec_mode,
    )
    return {
        "recommended_mode": rec_mode,
        "detected_security_schemes": security_schemes,
        "missing_output_schema_tools": missing_out,
        "checklist": checklist,
        "agent_onboarding_prompt": prompt,
    }


async def inspect_upstream(
    upstream: UpstreamConfig,
    client_manager: UpstreamClientManager,
    auth_headers: dict[str, str] | None = None,
) -> dict[str, Any]:
    """Discover tools, build HITL onboarding guidance, and emit a safe YAML snippet."""
    security_schemes: list[dict[str, Any]] = []
    if upstream.type == "openapi":
        spec = await client_manager._load_openapi_spec(upstream, auth_headers)
        all_tools = client_manager._discover_openapi_from_spec(upstream, spec)
        security_schemes = _extract_openapi_security_schemes(spec)
    else:
        all_tools = await client_manager.discover_tools(
            upstream, auth_headers=auth_headers, apply_filters=False
        )

    tools = client_manager.filter_tools(upstream, all_tools)
    digest = compute_schema_digest(tools)
    export_dict = upstream.model_dump(exclude_none=True, exclude={"openapi_spec"})
    export_dict["pinned_schema_digest"] = digest
    if export_dict.get("env"):
        export_dict["env"] = {k: "<SET_VIA_ENV>" for k in export_dict["env"]}
    yaml_snippet = yaml.safe_dump({"upstreams": [export_dict]}, sort_keys=False)
    guidance = build_onboarding_guidance(upstream, all_tools, security_schemes)
    return {
        "name": upstream.name,
        "type": upstream.type,
        "tool_count": len(tools),
        "total_discovered_count": len(all_tools),
        "selected_operations": [t.name for t in tools],
        "schema_digest": digest,
        "mutating_tools": [t.name for t in tools if t.mutating],
        "tools": [t.model_dump() for t in tools],
        "all_tools": [t.model_dump() for t in all_tools],
        "onboarding_guidance": guidance,
        "yaml_snippet": yaml_snippet,
    }
