"""Small operational signal checks for dependency and write-journal failures."""

from __future__ import annotations

import logging

import asyncpg
import pytest

from gateway.auth import CallerIdentity
from gateway.config import GatewaySettings, UpstreamConfig
from gateway.dispatcher import HostDispatcher
from gateway.server import GatewayLifecycleState, GatewayRuntime
from gateway.state import PostgresAuditStore
from gateway.upstream_clients import DiscoveredTool


@pytest.mark.asyncio
async def test_postgres_pool_uses_bounded_connection_and_command_timeouts(monkeypatch) -> None:
    created = {}

    async def create_pool(dsn, **kwargs):
        created.update(dsn=dsn, **kwargs)
        return object()

    monkeypatch.setattr(asyncpg, "create_pool", create_pool)
    store = PostgresAuditStore("postgresql://example.test/gateway")

    await store._get_pool()

    assert created["min_size"] == 1
    assert created["max_size"] == 10
    assert created["timeout"] == 10.0
    assert created["command_timeout"] == 30.0
    assert "sslmode=verify-full" in created["dsn"]


@pytest.mark.asyncio
async def test_postgres_readiness_logs_only_state_transitions(caplog) -> None:
    runtime = GatewayRuntime(
        settings=GatewaySettings(app_env="sandbox", postgres_dsn="postgresql://unused"),
        store=PostgresAuditStore("postgresql://unused", pool=object()),
    )
    runtime.lifecycle_state = GatewayLifecycleState.READY
    health = iter([False, False, True, True, False])

    async def health_check() -> bool:
        value = next(health)
        if value is False:
            raise OSError("private connection detail")
        return value

    runtime.store.health_check = health_check
    caplog.set_level(logging.INFO, logger="mcpcodeaway")

    results = [await runtime.is_ready() for _ in range(5)]

    assert results == [False, False, True, True, False]
    events = [
        record
        for record in caplog.records
        if getattr(record, "event", None) == "database.postgres_readiness_changed"
    ]
    assert len(events) == 3
    assert events[0].fields["status"] == "unavailable"
    assert events[1].fields["status"] == "available"
    assert "private connection detail" not in caplog.text
    assert "postgresql://unused" not in caplog.text


@pytest.mark.asyncio
async def test_blocked_sensitive_write_emits_safe_operation_signal(caplog) -> None:
    class Store:
        async def prepare_sensitive_write_audit(self, operation):
            return {**operation, "status": "executing"}, False

        async def claim_sensitive_write_audit(self, _operation_id, _started_at):
            return False

    dispatcher = HostDispatcher.__new__(HostDispatcher)
    dispatcher.settings = GatewaySettings()
    dispatcher.audit_store = Store()
    identity = CallerIdentity(
        sub="caller-private",
        tenant_id="tenant-private",
        roles=[],
        scopes=[],
    )
    tool = DiscoveredTool(
        server="example",
        name="write_item",
        qualified_name="example__write_item",
        input_schema={"type": "object"},
        mutating=True,
    )
    upstream = UpstreamConfig(
        name="example",
        type="mcp_http",
        url="https://api.example.test/mcp",
    )
    caplog.set_level(logging.INFO, logger="mcpcodeaway")

    with pytest.raises(PermissionError, match="Duplicate sensitive write blocked"):
        await dispatcher._prepare_sensitive_write_audit(
            identity, tool, upstream, {"secret": "private payload"}
        )

    assert "audit.sensitive_write_retry_blocked" in caplog.text
    assert "private payload" not in caplog.text
    assert "caller-private" not in caplog.text
    assert "tenant-private" not in caplog.text
