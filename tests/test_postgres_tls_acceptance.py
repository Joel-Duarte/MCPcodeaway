"""Opt-in external PostgreSQL TLS, least-privilege and recovery acceptance."""

from __future__ import annotations

import asyncio
import os
import secrets
import subprocess
import time
from datetime import UTC, datetime
from pathlib import Path
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

import asyncpg
import pytest

from gateway.state import PostgresAuditStore

pytestmark = [pytest.mark.integration, pytest.mark.postgres]


def _dsn_with(dsn: str, **changes: str) -> str:
    parsed = urlsplit(dsn)
    query = dict(parse_qsl(parsed.query, keep_blank_values=True))
    query.update(changes)
    return urlunsplit(parsed._replace(query=urlencode(query)))


def _operation(prefix: str, correlation_id: str) -> dict[str, str]:
    now = datetime.now(UTC).isoformat()
    return {
        "operation_id": f"{prefix}-{secrets.token_hex(8)}",
        "tenant_id": "local-tls-acceptance-tenant",
        "caller_sub": "local-tls-acceptance-user",
        "upstream_name": "local-tls-upstream",
        "tool_name": "write_item",
        "request_digest": "sha256:local-tls-fixture",
        "correlation_id": correlation_id,
        "dedupe_key": f"{prefix}-{correlation_id}",
        "created_at": now,
    }


@pytest.fixture
def tls_acceptance_config() -> tuple[str, str]:
    dsn = os.getenv("MCP_TEST_POSTGRES_DSN")
    ca_file = os.getenv("MCP_TEST_POSTGRES_CA_FILE")
    if not dsn or not ca_file:
        pytest.skip("use scripts/local_acceptance.py --with-postgres-tls")
    return dsn, ca_file


async def test_verified_tls_journal_and_atomic_claim(tls_acceptance_config) -> None:
    dsn, _ca_file = tls_acceptance_config
    query = dict(parse_qsl(urlsplit(dsn).query))
    assert query.get("sslmode") == "verify-full"
    first, second = PostgresAuditStore(dsn), PostgresAuditStore(dsn)
    prefix = f"tls-journal-{secrets.token_hex(8)}"
    try:
        await first.init()
        await second.init()
        operation = _operation(prefix, "same-correlation")
        row, inserted = await first.prepare_sensitive_write_audit(operation)
        assert inserted and row["status"] == "prepared"
        outcomes = await asyncio.gather(
            first.claim_sensitive_write_audit(row["operation_id"], datetime.now(UTC).isoformat()),
            second.claim_sensitive_write_audit(row["operation_id"], datetime.now(UTC).isoformat()),
        )
        assert sorted(outcomes) == [False, True]
        assert await first.finish_sensitive_write_audit(
            row["operation_id"], "succeeded", datetime.now(UTC).isoformat()
        )
        persisted = await second.get_sensitive_write_audit_by_dedupe_key(operation["dedupe_key"])
        assert persisted is not None and persisted["status"] == "succeeded"
        other, inserted = await second.prepare_sensitive_write_audit(
            _operation(prefix, "different-correlation")
        )
        assert inserted and other["correlation_id"] == "different-correlation"
    finally:
        if first._pool is not None:
            await first._pool.execute(
                "DELETE FROM sensitive_write_audits WHERE dedupe_key LIKE $1", f"{prefix}-%"
            )
        await first.close()
        await second.close()


async def test_untrusted_ca_wrong_hostname_and_bad_password_fail_closed(
    tls_acceptance_config,
) -> None:
    dsn, ca_file = tls_acceptance_config
    untrusted_ca = os.getenv("MCP_TEST_POSTGRES_UNTRUSTED_CA_FILE")
    assert untrusted_ca and Path(untrusted_ca).is_file()
    parsed = urlsplit(dsn)
    bad_ca = _dsn_with(dsn, sslrootcert=untrusted_ca)
    wrong_host = urlunsplit(parsed._replace(netloc=parsed.netloc.replace("127.0.0.1", "localhost")))
    bad_auth = urlunsplit(
        parsed._replace(netloc=parsed.netloc.replace("gateway_test:", "gateway_test:incorrect-"))
    )
    for attempt in (bad_ca, wrong_host, bad_auth):
        with pytest.raises((OSError, asyncpg.PostgresError)):
            pool = await asyncpg.create_pool(attempt, min_size=1, max_size=1)
            await pool.execute("SELECT 1")
            await pool.close()
    assert Path(ca_file).is_file()


async def test_gateway_role_is_not_superuser_and_missing_schema_create_fails(
    tls_acceptance_config,
) -> None:
    dsn, _ca_file = tls_acceptance_config
    connection = await asyncpg.connect(dsn)
    try:
        assert (
            await connection.fetchval("SELECT rolsuper FROM pg_roles WHERE rolname=current_user")
            is False
        )
    finally:
        await connection.close()

    parsed = urlsplit(dsn)
    limited = urlunsplit(
        parsed._replace(netloc=parsed.netloc.replace("gateway_test:", "gateway_limited:"))
    )
    limited_store = PostgresAuditStore(limited)
    with pytest.raises(asyncpg.InsufficientPrivilegeError):
        await limited_store.init()
    await limited_store.close()


async def test_pool_recovers_after_postgres_restart_without_replaying_prepared_write(
    tls_acceptance_config,
) -> None:
    dsn, _ca_file = tls_acceptance_config
    project = os.getenv("MCP_TEST_POSTGRES_COMPOSE_PROJECT", "mcpcodeaway-local-acceptance")
    compose_files = os.getenv("MCP_TEST_POSTGRES_COMPOSE_FILES", "").split(os.pathsep)
    if not compose_files or not compose_files[0]:
        pytest.skip("TLS recovery needs the opt-in Compose file list")

    def compose(*args: str) -> None:
        command = ["docker", "compose"]
        for compose_file in compose_files:
            command.extend(["-f", compose_file])
        command.extend(["-p", project, *args])
        subprocess.run(command, check=True, capture_output=True, text=True, timeout=90)

    store = PostgresAuditStore(dsn)
    prefix = f"tls-recovery-{secrets.token_hex(8)}"
    operation = _operation(prefix, "outage-correlation")
    try:
        await store.init()
        prepared, created = await store.prepare_sensitive_write_audit(operation)
        assert created
        compose("stop", "--timeout", "2", "postgres")
        with pytest.raises((OSError, asyncpg.PostgresError)):
            await store.prepare_sensitive_write_audit(_operation(prefix, "during-outage"))
        compose("start", "postgres")
        deadline = time.monotonic() + 45
        while time.monotonic() < deadline:
            if await store.health_check():
                break
            await asyncio.sleep(0.5)
        assert await store.health_check()
        persisted = await store.get_sensitive_write_audit_by_dedupe_key(operation["dedupe_key"])
        assert persisted is not None and persisted["status"] == "prepared"
        assert not await store.claim_sensitive_write_audit(
            "missing-operation", datetime.now(UTC).isoformat()
        )
        resumed = await store.prepare_sensitive_write_audit(operation)
        assert not resumed[1] and resumed[0]["operation_id"] == prepared["operation_id"]
    finally:
        if store._pool is not None:
            await store._pool.execute(
                "DELETE FROM sensitive_write_audits WHERE dedupe_key LIKE $1", f"{prefix}-%"
            )
        await store.close()
