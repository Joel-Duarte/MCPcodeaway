from __future__ import annotations

import asyncio
from datetime import UTC, datetime

import httpx
import pytest

from gateway.auth import CallerIdentity
from gateway.config import GatewaySettings, UpstreamConfig
from gateway.observability import clear_request_context, set_request_context
from gateway.server import GatewayRuntime
from gateway.state import SQLiteAuditStore
from gateway.upstream_clients import DiscoveredTool


def _audit_operation(operation_id: str, dedupe_key: str) -> dict[str, str]:
    now = datetime.now(UTC).isoformat()
    return {
        "operation_id": operation_id,
        "tenant_id": "tenant-a",
        "caller_sub": "caller-a",
        "upstream_name": "api",
        "tool_name": "write_item",
        "request_digest": "digest",
        "correlation_id": "request-1",
        "dedupe_key": dedupe_key,
        "created_at": now,
    }


@pytest.mark.asyncio
async def test_sensitive_write_claim_is_atomic_across_store_instances(tmp_path) -> None:
    path = str(tmp_path / "shared-audit.db")
    first = SQLiteAuditStore(path)
    second = SQLiteAuditStore(path)
    await first.init()
    await second.init()
    row, _ = await first.prepare_sensitive_write_audit(_audit_operation("op-1", "same"))

    claims = await asyncio.gather(
        first.claim_sensitive_write_audit(row["operation_id"], datetime.now(UTC).isoformat()),
        second.claim_sensitive_write_audit(row["operation_id"], datetime.now(UTC).isoformat()),
    )
    assert sorted(claims) == [False, True]


@pytest.mark.asyncio
async def test_read_tool_does_not_require_sensitive_write_audit(tmp_path) -> None:
    runtime = GatewayRuntime(
        settings=GatewaySettings(
            auth_mode="dev",
            sqlite_path=str(tmp_path / "read-audit.db"),
            upstreams_config_path=str(tmp_path / "missing.yaml"),
        ),
        preloaded_tools={},
    )
    upstream = UpstreamConfig(
        name="read_api",
        type="mcp_http",
        url="https://api.example.test/mcp",
        auth_type="none",
        tool_effects={"get_item": "read"},
    )
    tool = DiscoveredTool(
        server="read_api",
        name="get_item",
        qualified_name="read_api__get_item",
        input_schema={"type": "object", "properties": {}},
        mutating=False,
    )

    class UnavailableAudit:
        async def prepare_sensitive_write_audit(self, _operation):
            raise AssertionError("read tool consulted sensitive-write audit storage")

    runtime.dispatcher.audit_store = UnavailableAudit()
    operation_id = await runtime.dispatcher._prepare_sensitive_write_audit(
        CallerIdentity(sub="caller-a", tenant_id="tenant-a"), tool, upstream, {}
    )
    assert operation_id is None


@pytest.mark.asyncio
async def test_sensitive_write_audit_failure_blocks_upstream_call(tmp_path) -> None:
    runtime = GatewayRuntime(
        settings=GatewaySettings(
            auth_mode="dev",
            sqlite_path=str(tmp_path / "gateway.db"),
            upstreams_config_path=str(tmp_path / "missing.yaml"),
        ),
        preloaded_tools={},
    )
    await runtime.store.init()
    upstream = UpstreamConfig(
        name="write_api",
        type="mcp_http",
        url="https://api.example.test/mcp",
        auth_type="none",
        allow_all_callers=True,
        allowed_operations=["write_item"],
        tool_effects={"write_item": "write"},
        mutating_tools=["write_item"],
    )
    runtime.registry.upsert(upstream)
    identity = CallerIdentity(
        sub="caller-a", tenant_id="tenant-a", roles=["admin"], scopes=["mcp.write"]
    )
    tool = DiscoveredTool(
        server=upstream.name,
        name="write_item",
        qualified_name="write_api__write_item",
        input_schema={"type": "object", "properties": {"secret": {"type": "string"}}},
        mutating=True,
    )
    calls = 0

    async def upstream_call(*_args, **_kwargs):
        nonlocal calls
        calls += 1
        return {"ok": True}

    class UnavailableAudit:
        async def prepare_sensitive_write_audit(self, _operation):
            raise OSError("database unavailable")

    runtime.dispatcher.audit_store = UnavailableAudit()
    runtime.client_manager.call_tool = upstream_call
    set_request_context(request_id="audit-preflight")
    try:
        result, record = await runtime.dispatcher.invoke_tool(
            identity, tool, upstream, {"secret": "do-not-store"}
        )
    finally:
        clear_request_context()
    assert calls == 0
    assert record.status == "error"
    assert "_dispatch_error" in result


@pytest.mark.asyncio
async def test_sensitive_write_outcome_and_audit_failure_block_replay(tmp_path) -> None:
    runtime = GatewayRuntime(
        settings=GatewaySettings(
            auth_mode="dev",
            sqlite_path=str(tmp_path / "gateway.db"),
            upstreams_config_path=str(tmp_path / "missing.yaml"),
        ),
        preloaded_tools={},
    )
    await runtime.store.init()
    upstream = UpstreamConfig(
        name="write_api",
        type="mcp_http",
        url="https://api.example.test/mcp",
        auth_type="none",
        allow_all_callers=True,
        allowed_operations=["write_item"],
        tool_effects={"write_item": "write"},
        mutating_tools=["write_item"],
    )
    runtime.registry.upsert(upstream)
    identity = CallerIdentity(
        sub="caller-a", tenant_id="tenant-a", roles=["admin"], scopes=["mcp.write"]
    )
    tool = DiscoveredTool(
        server=upstream.name,
        name="write_item",
        qualified_name="write_api__write_item",
        input_schema={"type": "object", "properties": {"secret": {"type": "string"}}},
        mutating=True,
    )
    calls = 0

    async def upstream_call(*_args, **_kwargs):
        nonlocal calls
        calls += 1
        return {"ok": True}

    class FinalizationFailure:
        def __init__(self, backing):
            self.backing = backing

        async def prepare_sensitive_write_audit(self, operation):
            return await self.backing.prepare_sensitive_write_audit(operation)

        async def claim_sensitive_write_audit(self, operation_id, started_at):
            return await self.backing.claim_sensitive_write_audit(operation_id, started_at)

        async def finish_sensitive_write_audit(self, *_args):
            operation_id, status, updated_at = _args
            if status == "succeeded":
                raise OSError("database unavailable after upstream execution")
            return await self.backing.finish_sensitive_write_audit(operation_id, status, updated_at)

    runtime.dispatcher.audit_store = FinalizationFailure(runtime.store)
    runtime.client_manager.call_tool = upstream_call
    set_request_context(request_id="audit-finalize-failure")
    try:
        result, record = await runtime.dispatcher.invoke_tool(
            identity, tool, upstream, {"secret": "do-not-store"}
        )
        retry_result, retry_record = await runtime.dispatcher.invoke_tool(
            identity, tool, upstream, {"secret": "do-not-store"}
        )
    finally:
        clear_request_context()

    assert calls == 1
    assert record.status == "error"
    assert "_dispatch_error" in result
    assert retry_record.status == "error"
    assert "_dispatch_error" in retry_result
    rows = (
        runtime.store._ensure_conn()
        .execute(
            "SELECT * FROM sensitive_write_audits WHERE correlation_id=?",
            ("audit-finalize-failure",),
        )
        .fetchall()
    )
    assert len(rows) == 1
    row = dict(rows[0])
    assert row["status"] == "outcome_unknown"
    assert "do-not-store" not in str(row)


@pytest.mark.asyncio
async def test_ambiguous_write_failure_is_recorded_as_outcome_unknown(tmp_path) -> None:
    runtime = GatewayRuntime(
        settings=GatewaySettings(
            auth_mode="dev",
            sqlite_path=str(tmp_path / "gateway.db"),
            upstreams_config_path=str(tmp_path / "missing.yaml"),
        ),
        preloaded_tools={},
    )
    await runtime.store.init()
    upstream = UpstreamConfig(
        name="write_api",
        type="mcp_http",
        url="https://api.example.test/mcp",
        auth_type="none",
        allow_all_callers=True,
        allowed_operations=["write_item"],
        tool_effects={"write_item": "write"},
        mutating_tools=["write_item"],
    )
    runtime.registry.upsert(upstream)
    identity = CallerIdentity(
        sub="caller-a", tenant_id="tenant-a", roles=["admin"], scopes=["mcp.write"]
    )
    tool = DiscoveredTool(
        server=upstream.name,
        name="write_item",
        qualified_name="write_api__write_item",
        input_schema={"type": "object", "properties": {}},
        mutating=True,
    )

    async def ambiguous(*_args, **_kwargs):
        raise httpx.ReadTimeout("upstream timed out after accepting request")

    runtime.client_manager.call_tool = ambiguous
    set_request_context(request_id="audit-outcome-unknown")
    try:
        result, record = await runtime.dispatcher.invoke_tool(identity, tool, upstream, {})
    finally:
        clear_request_context()
    row = (
        runtime.store._ensure_conn()
        .execute(
            "SELECT * FROM sensitive_write_audits WHERE correlation_id=?",
            ("audit-outcome-unknown",),
        )
        .fetchone()
    )
    assert record.outcome_unknown
    assert "_dispatch_error" in result
    assert row["status"] == "outcome_unknown"
