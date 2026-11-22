"""Live PostgreSQL checks for durable sensitive-write journal behavior.

Run with MCP_TEST_POSTGRES_DSN set and the optional asyncpg dependency installed.
The tests only use rows with a unique local-test prefix and remove them afterward.
"""

from __future__ import annotations

import asyncio
import os
import secrets
from datetime import UTC, datetime

import pytest

from gateway.state import PostgresAuditStore

pytestmark = [pytest.mark.integration, pytest.mark.postgres]


def _operation(prefix: str, correlation_id: str) -> dict[str, str]:
    now = datetime.now(UTC).isoformat()
    return {
        "operation_id": f"{prefix}-{secrets.token_hex(8)}",
        "tenant_id": "local-acceptance-tenant",
        "caller_sub": "local-acceptance-user",
        "upstream_name": "local-acceptance-upstream",
        "tool_name": "write_item",
        "request_digest": "sha256:acceptance-fixture",
        "correlation_id": correlation_id,
        "dedupe_key": f"{prefix}-{correlation_id}",
        "created_at": now,
    }


@pytest.fixture
async def postgres_stores():
    dsn = os.getenv("MCP_TEST_POSTGRES_DSN")
    if not dsn:
        pytest.skip(
            "MCP_TEST_POSTGRES_DSN is not set; start the isolated acceptance Compose service"
        )
    pytest.importorskip("asyncpg", reason="install the optional mcpcodeaway[postgres] dependency")

    first = PostgresAuditStore(dsn)
    second = PostgresAuditStore(dsn)
    await first.init()
    await second.init()
    assert await first.health_check()
    prefix = f"local-acceptance-{secrets.token_hex(10)}"
    try:
        yield first, second, prefix
    finally:
        pool = await first._get_pool()
        await pool.execute(
            "DELETE FROM sensitive_write_audits WHERE dedupe_key LIKE $1", f"{prefix}-%"
        )
        await first.close()
        await second.close()


async def test_postgres_claim_is_atomic_across_independent_pools(postgres_stores) -> None:
    first, second, prefix = postgres_stores
    row, created = await first.prepare_sensitive_write_audit(_operation(prefix, "same-request"))
    assert created
    assert row["status"] == "prepared"

    outcomes = await asyncio.gather(
        first.claim_sensitive_write_audit(row["operation_id"], datetime.now(UTC).isoformat()),
        second.claim_sensitive_write_audit(row["operation_id"], datetime.now(UTC).isoformat()),
    )
    assert sorted(outcomes) == [False, True]

    assert await first.finish_sensitive_write_audit(
        row["operation_id"], "succeeded", datetime.now(UTC).isoformat()
    )
    persisted = await second.get_sensitive_write_audit_by_dedupe_key(row["dedupe_key"])
    assert persisted is not None
    assert persisted["status"] == "succeeded"
    assert persisted["request_digest"] == "sha256:acceptance-fixture"
    assert "arguments" not in persisted


async def test_postgres_keeps_distinct_correlation_ids_independent(postgres_stores) -> None:
    first, second, prefix = postgres_stores
    rows = await asyncio.gather(
        first.prepare_sensitive_write_audit(_operation(prefix, "request-a")),
        second.prepare_sensitive_write_audit(_operation(prefix, "request-b")),
    )
    assert all(created for _, created in rows)
    assert {row["correlation_id"] for row, _ in rows} == {"request-a", "request-b"}
    assert {row["status"] for row, _ in rows} == {"prepared"}
