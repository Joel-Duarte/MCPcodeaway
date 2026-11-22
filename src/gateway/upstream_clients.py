"""Unified upstream transport adapters for MCP HTTP, MCP stdio, and Swagger/OpenAPI."""

from __future__ import annotations

import asyncio
import hashlib
import ipaddress
import json
import re
import socket
from contextvars import ContextVar
from typing import Annotated, Any
from urllib.parse import quote, urlparse

import httpcore
import httpx
import yaml
from httpcore._backends.auto import AutoBackend
from mcp import Client, StdioServerParameters
from opentelemetry import propagate
from pydantic import BaseModel, ConfigDict, Field, StringConstraints

from gateway.config import UpstreamConfig
from gateway.mcp_protocol import MCP_PROTOCOL_VERSION, request_headers, request_meta

_MUTATING_METHODS = {"POST", "PUT", "PATCH", "DELETE"}
_CLOUD_METADATA_HOSTS = {
    "metadata.google.internal",
    "metadata",
    "169.254.169.254",
    "100.100.100.200",
    "fd00:ec2::254",
}
_PINNED_DNS: ContextVar[dict[str, tuple[str, ...]] | None] = ContextVar(
    "gateway_pinned_dns", default=None
)


class _PinnedNetworkBackend:
    """Connect sockets only to addresses resolved and checked for the active request."""

    def __init__(self) -> None:
        self._backend = AutoBackend()

    async def connect_tcp(
        self,
        host: str,
        port: int,
        timeout: float | None = None,
        local_address: str | None = None,
        socket_options: Any = None,
    ) -> httpcore.AsyncNetworkStream:
        key = host.strip("[]").lower().rstrip(".")
        addresses = (_PINNED_DNS.get() or {}).get(key)
        if not addresses:
            raise OSError(f"No validated destination addresses for '{host}'")
        last_error: Exception | None = None
        for address in addresses:
            try:
                stream = await self._backend.connect_tcp(
                    address,
                    port,
                    timeout=timeout,
                    local_address=local_address,
                    socket_options=socket_options,
                )
            except (OSError, TimeoutError) as exc:
                last_error = exc
                continue
            peer = stream.get_extra_info("server_addr")
            try:
                peer_ip = ipaddress.ip_address(str(peer[0]).split("%")[0]) if peer else None
            except (ValueError, IndexError, TypeError):
                peer_ip = None
            if peer_ip is not None and str(peer_ip) in addresses:
                return stream
            await stream.aclose()
            last_error = OSError("Connected peer did not match a validated destination address")
        raise last_error or OSError(f"Could not connect to validated destination for '{host}'")

    async def connect_unix_socket(
        self, path: str, timeout: float | None = None, socket_options: Any = None
    ) -> httpcore.AsyncNetworkStream:
        return await self._backend.connect_unix_socket(
            path, timeout=timeout, socket_options=socket_options
        )

    async def sleep(self, seconds: float) -> None:
        await self._backend.sleep(seconds)


def _check_resolved_ip(
    ip_obj: ipaddress.IPv4Address | ipaddress.IPv6Address,
    host: str,
    *,
    app_env: str,
    block_private_ips_in_prod: bool,
) -> None:
    if isinstance(ip_obj, ipaddress.IPv6Address) and ip_obj.ipv4_mapped is not None:
        ip_obj = ip_obj.ipv4_mapped
    if str(ip_obj) in _CLOUD_METADATA_HOSTS:
        raise ValueError(f"Blocked cloud metadata endpoint IP '{ip_obj}' for host '{host}'")
    if ip_obj.is_link_local or ip_obj.is_multicast or ip_obj.is_unspecified:
        raise ValueError(f"Blocked link-local or unspecified IP '{ip_obj}' in upstream URL")
    if (
        app_env == "prod"
        and block_private_ips_in_prod
        and (ip_obj.is_private or ip_obj.is_loopback or ip_obj.is_reserved)
    ):
        raise ValueError(
            f"Blocked private/loopback IP '{ip_obj}' ({host}) in prod; "
            "add host to allowed_upstream_hosts"
        )


def validate_upstream_url(
    url: str | None,
    *,
    app_env: str = "sandbox",
    block_private_ips_in_prod: bool = True,
    allowed_hosts: list[str] | None = None,
    resolve_dns: bool | None = None,
) -> tuple[str, ...]:
    """Reject unsafe destinations and return DNS addresses when resolution is requested."""
    if not url:
        return ()
    parsed = urlparse(url)
    scheme = parsed.scheme.lower()
    if scheme not in {"http", "https"}:
        raise ValueError(
            f"Unsupported upstream URL scheme '{parsed.scheme}': only http/https allowed"
        )
    host = (parsed.hostname or "").strip().lower().rstrip(".")
    if not host:
        raise ValueError(f"Upstream URL '{url}' is missing a valid hostname")

    if (
        host in _CLOUD_METADATA_HOSTS
        or host.endswith(".metadata.google.internal")
        or "169.254.169.254" in host
        or "100.100.100.200" in host
    ):
        raise ValueError(f"Blocked cloud metadata endpoint host '{host}' in upstream URL")

    allowset = {h.strip().lower().rstrip(".") for h in (allowed_hosts or []) if h.strip()}
    explicitly_allowed = host in allowset

    if resolve_dns is None:
        resolve_dns = app_env == "prod" and block_private_ips_in_prod

    try:
        ip_obj = ipaddress.ip_address(host)
    except ValueError:
        ip_obj = None

    if ip_obj is not None:
        addresses = (str(ip_obj),)
    elif resolve_dns:
        if (
            host == "localhost"
            or host.endswith(
                (".localhost", ".local", ".internal", ".nip.io", ".sslip.io", ".localtest.me")
            )
            and not explicitly_allowed
        ):
            raise ValueError(
                f"Blocked local/internal hostname '{host}' in prod; "
                "add host to allowed_upstream_hosts"
            )
        try:
            infos = socket.getaddrinfo(
                host,
                parsed.port or (443 if scheme == "https" else 80),
                type=socket.SOCK_STREAM,
            )
        except socket.gaierror as exc:
            raise ValueError(f"Could not resolve upstream hostname '{host}'") from exc
        resolved: list[str] = []
        for info in infos:
            if not info[4]:
                continue
            try:
                address = str(ipaddress.ip_address(str(info[4][0]).split("%")[0]))
            except ValueError:
                continue
            if address not in resolved:
                resolved.append(address)
        addresses = tuple(resolved)
        if not addresses:
            raise ValueError(f"Could not resolve upstream hostname '{host}'")
    else:
        if (
            app_env == "prod"
            and block_private_ips_in_prod
            and not explicitly_allowed
            and host == "localhost"
        ):
            raise ValueError(f"Blocked local/internal hostname '{host}' in prod")
        return ()

    for address in addresses:
        resolved_ip = ipaddress.ip_address(address)
        if str(resolved_ip) in _CLOUD_METADATA_HOSTS:
            raise ValueError(
                f"Blocked cloud metadata endpoint IP '{resolved_ip}' for host '{host}'"
            )
        _check_resolved_ip(
            resolved_ip,
            host,
            app_env=app_env,
            block_private_ips_in_prod=block_private_ips_in_prod and not explicitly_allowed,
        )
    return addresses


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


class ToolGuidance(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    description: str = Field(default="", max_length=400)
    parameter_hints: dict[str, Annotated[str, StringConstraints(max_length=160)]] = Field(
        default_factory=dict, max_length=24
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
    guidance: ToolGuidance | None = None


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


def _resolve_refs(node: Any, root: dict[str, Any], seen: set[str] | None = None) -> Any:
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


def _reject_mcp_session_header(response: httpx.Response, upstream_name: str) -> None:
    if response.headers.get("mcp-session-id"):
        raise RuntimeError(
            f"Upstream '{upstream_name}' returned Mcp-Session-Id; protocol sessions are unsupported"
        )


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

    def __init__(
        self,
        http_client: httpx.AsyncClient | None = None,
        *,
        app_env: str = "sandbox",
        block_private_ips_in_prod: bool = True,
        allowed_hosts: list[str] | None = None,
        ssrf_protection_enabled: bool = True,
    ) -> None:
        self._http_client = http_client
        self._owned_client: httpx.AsyncClient | None = None
        self._app_env = app_env
        self._block_private_ips_in_prod = block_private_ips_in_prod
        self._allowed_hosts = list(allowed_hosts or [])
        self._ssrf_protection_enabled = ssrf_protection_enabled
        self._pinning_supported = False
        self._source_ssl_context = self._extract_ssl_context(http_client)
        if (
            self._app_env == "prod"
            and self._ssrf_protection_enabled
            and http_client is not None
            and self._source_ssl_context is None
        ):
            raise ValueError(
                "Production upstream pinning requires a standard HTTPX transport so its "
                "TLS context can be preserved"
            )

    @staticmethod
    def _extract_ssl_context(client: httpx.AsyncClient | None) -> Any | None:
        if client is None:
            return None
        transport = getattr(client, "_transport", None)
        pool = getattr(transport, "_pool", None)
        if not isinstance(transport, httpx.AsyncHTTPTransport) or not isinstance(
            pool, httpcore.AsyncConnectionPool
        ):
            return None
        return getattr(pool, "_ssl_context", None)

    def _get_client(self) -> httpx.AsyncClient:
        if self._http_client is not None and not (
            self._app_env == "prod" and self._ssrf_protection_enabled
        ):
            return self._http_client
        if self._owned_client is None:
            transport = httpx.AsyncHTTPTransport(
                verify=self._source_ssl_context if self._source_ssl_context is not None else True,
                trust_env=False,
                limits=httpx.Limits(
                    max_connections=100,
                    max_keepalive_connections=20,
                    keepalive_expiry=0,
                ),
            )
            pool = transport._pool
            if isinstance(pool, httpcore.AsyncConnectionPool):
                pool._network_backend = _PinnedNetworkBackend()
                self._pinning_supported = True
            self._owned_client = httpx.AsyncClient(
                timeout=120.0,
                transport=transport,
                trust_env=False,
            )
        return self._owned_client

    async def close(self) -> None:
        """Close the gateway-owned pooled HTTP client if initialized."""
        if self._owned_client is not None:
            await self._owned_client.aclose()
            self._owned_client = None

    async def _request(self, method: str, url: str, **kwargs: Any) -> httpx.Response:
        pinned: dict[str, tuple[str, ...]] | None = None
        if self._ssrf_protection_enabled:
            client = self._get_client()
            if self._pinning_supported:
                addresses = validate_upstream_url(
                    url,
                    app_env=self._app_env,
                    block_private_ips_in_prod=self._block_private_ips_in_prod,
                    allowed_hosts=self._allowed_hosts,
                    resolve_dns=True,
                )
                host = (urlparse(url).hostname or "").strip("[]").lower().rstrip(".")
                pinned = {host: addresses}
            else:
                if self._app_env == "prod":
                    raise RuntimeError(
                        "Production SSRF protection requires connection-time destination pinning"
                    )
                validate_upstream_url(
                    url,
                    app_env=self._app_env,
                    block_private_ips_in_prod=self._block_private_ips_in_prod,
                    allowed_hosts=self._allowed_hosts,
                )
        else:
            client = self._get_client()
        headers = {str(k): str(v) for k, v in (kwargs.pop("headers", None) or {}).items()}
        headers.pop("Mcp-Session-Id", None)
        propagate.inject(headers)
        timeout_seconds = kwargs.pop("timeout_seconds", None)
        if timeout_seconds is None:
            request = httpx.Request(method, url, headers=headers, **kwargs)
        else:
            request = client.build_request(
                method, url, headers=headers, timeout=timeout_seconds, **kwargs
            )
        request.headers.pop("cookie", None)
        context_token = _PINNED_DNS.set(pinned) if pinned is not None else None
        try:
            return await client.send(request, follow_redirects=False)
        finally:
            if context_token is not None:
                _PINNED_DNS.reset(context_token)

    async def probe_health(self, url: str, *, timeout_seconds: float) -> httpx.Response:
        """Issue a bounded unauthenticated health request through SSRF validation."""
        async with asyncio.timeout(timeout_seconds):
            return await self._request("GET", url)

    @staticmethod
    def filter_tools(upstream: UpstreamConfig, tools: list[DiscoveredTool]) -> list[DiscoveredTool]:
        """Apply optional allowed_operations / excluded_operations curation lists."""
        filtered = list(tools)
        if upstream.allowed_operations is not None:
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
        async with asyncio.timeout(upstream.request_timeout_seconds):
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
        async with asyncio.timeout(upstream.request_timeout_seconds):
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
        discover_headers = {
            **(auth_headers or {}),
            **request_headers("server/discover"),
        }
        discover = await self._request(
            "POST",
            upstream.url,
            headers=discover_headers,
            timeout_seconds=upstream.request_timeout_seconds,
            json={
                "jsonrpc": "2.0",
                "id": 1,
                "method": "server/discover",
                "params": {"_meta": request_meta()},
            },
        )
        discover.raise_for_status()
        _reject_mcp_session_header(discover, upstream.name)
        discover_body = _parse_mcp_jsonrpc_response(discover)
        supported = (discover_body.get("result") or {}).get("supportedVersions", [])
        if MCP_PROTOCOL_VERSION not in supported:
            raise RuntimeError(
                f"Upstream '{upstream.name}' does not advertise sessionless MCP "
                f"{MCP_PROTOCOL_VERSION}; legacy session negotiation is not supported"
            )

        headers = {**(auth_headers or {}), **request_headers("tools/list")}
        resp = await self._request(
            "POST",
            upstream.url,
            headers=headers,
            timeout_seconds=upstream.request_timeout_seconds,
            json={
                "jsonrpc": "2.0",
                "id": 2,
                "method": "tools/list",
                "params": {"_meta": request_meta()},
            },
        )
        resp.raise_for_status()
        _reject_mcp_session_header(resp, upstream.name)
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
            **(auth_headers or {}),
            **request_headers("tools/call", tool_name=tool.name),
        }
        payload = {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "tools/call",
            "params": {
                "_meta": request_meta(),
                "name": tool.name,
                "arguments": arguments,
            },
        }
        resp = await self._request(
            "POST",
            upstream.url,
            headers=headers,
            timeout_seconds=upstream.request_timeout_seconds,
            json=payload,
        )
        resp.raise_for_status()
        _reject_mcp_session_header(resp, upstream.name)
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
        async with Client(params, mode=MCP_PROTOCOL_VERSION) as client:
            listed = await client.list_tools()
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
        async with Client(params, mode=MCP_PROTOCOL_VERSION) as client:
            result = await client.call_tool(tool.name, arguments)
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
                "GET",
                upstream.openapi_spec_url,
                headers=auth_headers or {},
                timeout_seconds=upstream.request_timeout_seconds,
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
        _validate_openapi_stateless_auth(spec, upstream.name)
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
            safe_val = quote(str(arguments[pname]), safe="")
            resolved_path = resolved_path.replace(f"{{{pname}}}", safe_val)

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
        req_kwargs: dict[str, Any] = {
            "headers": auth_headers or {},
            "params": query or None,
            "timeout_seconds": upstream.request_timeout_seconds,
        }
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


def _validate_openapi_stateless_auth(spec: dict[str, Any], upstream_name: str) -> None:
    """Reject OpenAPI cookie auth because the gateway never retains cookie state."""
    components = spec.get("components")
    schemes = components.get("securitySchemes", {}) if isinstance(components, dict) else {}
    cookie_schemes = (
        {
            name
            for name, scheme in schemes.items()
            if isinstance(scheme, dict)
            and scheme.get("type") == "apiKey"
            and scheme.get("in") == "cookie"
        }
        if isinstance(schemes, dict)
        else set()
    )

    def referenced_cookie_schemes(security: Any) -> set[str]:
        if not isinstance(security, list):
            return set()
        return {
            name
            for requirement in security
            if isinstance(requirement, dict)
            for name in requirement
            if name in cookie_schemes
        }

    used_cookie_schemes = referenced_cookie_schemes(spec.get("security"))
    paths = spec.get("paths")
    if isinstance(paths, dict):
        for path_item in paths.values():
            if not isinstance(path_item, dict):
                continue
            if any(
                isinstance(param, dict) and param.get("in") == "cookie"
                for param in path_item.get("parameters", [])
            ):
                raise ValueError(
                    f"OpenAPI upstream '{upstream_name}' requires cookie parameters; "
                    "cookie-based API sessions are unsupported. Use bearer/OAuth or header auth."
                )
            for operation in path_item.values():
                if not isinstance(operation, dict):
                    continue
                used_cookie_schemes.update(referenced_cookie_schemes(operation.get("security")))
                if any(
                    isinstance(param, dict) and param.get("in") == "cookie"
                    for param in operation.get("parameters", [])
                ):
                    raise ValueError(
                        f"OpenAPI upstream '{upstream_name}' requires cookie parameters; "
                        "cookie-based API sessions are unsupported. "
                        "Use bearer/OAuth or header auth."
                    )
    if used_cookie_schemes:
        names = ", ".join(sorted(used_cookie_schemes))
        raise ValueError(
            f"OpenAPI upstream '{upstream_name}' uses cookie auth ({names}); "
            "cookie-based API sessions are unsupported. Use bearer/OAuth or header auth."
        )


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
        context_lines.append(f"- Detected OpenAPI securitySchemes: {json.dumps(security_schemes)}")
    if tools:
        ops_preview = ", ".join(f"{t.name}{'[MUTATING]' if t.mutating else ''}" for t in tools[:25])
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
        "You have three ways to apply the human's answers (pick the best one available):\n"
        "A) **UI / WebMCP (`@ui-sdk/vue`):** If operating inside `/dashboard/onboarding`, "
        "inspect controls via `snapshot()`, fill fields step-by-step (`1. Connect & Inspect`, "
        "`2. Policy & Mode`, `3. Test & Activate`), and trigger `Save Upstream` (which pauses "
        "for explicit human confirmation).\n"
        "B) **Gateway Self-Admin MCP (`POST /admin/mcp`):** With a `gateway_admin` Bearer "
        "token, call `admin_inspect_upstream`, `admin_run_ab_test`, and `admin_save_upstream`.\n"
        "C) **JSON Import:** Emit ONE valid JSON object matching `UpstreamConfig` (plus "
        "separate Corporate IdP / backend setup bullets if OBO is needed) for "
        "`/dashboard/onboarding` ('Import Agent JSON') or `POST /admin/upstreams`."
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
    has_declared_security = bool(security_schemes)

    if has_apikey_scheme:
        checklist.append(
            {
                "target": "operator",
                "severity": "required",
                "field": "auth_type",
                "title": "Confirm Who Owns the API Key",
                "detail": (
                    "OpenAPI declares an API key, but cannot tell whether it is a personal key "
                    "or a shared service key. Choose user_api_key for per-user credentials or "
                    "api_key with shared_access only when every admitted caller may use the "
                    "enabled operations."
                ),
            }
        )

    if upstream.auth_type == "delegated_token" or (
        has_oauth_scheme and upstream.auth_type == "none"
    ):
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

    if upstream.auth_type in {"api_key", "bearer_secret"}:
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

    if upstream.auth_type == "user_api_key":
        checklist.append(
            {
                "target": "user_connection",
                "severity": "required",
                "field": "user_api_key",
                "title": "Each User Connects a Personal API Key",
                "detail": (
                    "The gateway stores the key encrypted for this tenant, user, and upstream, "
                    "then injects it only for that user's calls. No shared-key fallback is used."
                ),
            }
        )
    elif upstream.auth_type == "user_oauth":
        checklist.append(
            {
                "target": "user_connection",
                "severity": "required",
                "field": "user_oauth",
                "title": "Each User Connects Their Upstream Account",
                "detail": (
                    "Use OAuth authorization code with PKCE for each user. The upstream "
                    "authorizes the resulting user-scoped token."
                ),
            }
        )
    if (
        upstream.authorization_mode == "gateway"
        and not upstream.allowed_roles
        and not upstream.allowed_groups
    ):
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

    if upstream.authorization_mode == "upstream":
        checklist.append(
            {
                "target": "operator",
                "severity": "recommended",
                "field": "authorization_mode",
                "title": "Upstream Authorization Is Authoritative",
                "detail": (
                    "The gateway will send a user-scoped credential and will not filter tools "
                    "by local groups. The upstream must enforce user permissions on each call."
                ),
            }
        )
    elif upstream.authorization_mode == "shared_access":
        checklist.append(
            {
                "target": "operator",
                "severity": "required",
                "field": "authorization_mode",
                "title": "Confirm Shared Access Scope",
                "detail": (
                    "The upstream sees one shared service identity. Every authenticated caller "
                    "can invoke the enabled operations; the upstream cannot distinguish them "
                    "unless another user identity is supplied."
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
        "auth_discovery": {
            "status": (
                "declared"
                if has_declared_security
                else "not_observed"
                if upstream.type == "mcp_http"
                else "not_declared"
            ),
            "source": (
                "openapi_security_schemes" if upstream.type == "openapi" else "mcp_tools_discovery"
            ),
            "security_schemes": security_schemes,
            "authorization_mode": upstream.authorization_mode,
            "requires_operator_confirmation": True,
        },
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
        "tools": [{**t.model_dump(), "schema_sha256": compute_schema_digest([t])} for t in tools],
        "all_tools": [
            {**t.model_dump(), "schema_sha256": compute_schema_digest([t])} for t in all_tools
        ],
        "onboarding_guidance": guidance,
        "yaml_snippet": yaml_snippet,
    }
