"""Explicit PostgreSQL schema migrations for MCPcodeaway.

Run with ``uv run python -m gateway.migrations upgrade`` using
``MCP_POSTGRES_MIGRATION_DSN``. Gateway processes never apply these migrations.
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import os
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from gateway.state import _postgres_dsn_with_secure_tls_default

log = logging.getLogger("gateway.migrations")
MIGRATION_DSN_ENV = "MCP_POSTGRES_MIGRATION_DSN"
MIGRATION_TABLE = "mcpcodeaway_schema_migrations"
ADVISORY_LOCK_KEY = 7_314_202_611


@dataclass(frozen=True)
class Migration:
    version: int
    description: str
    statements: tuple[str, ...]


MIGRATIONS = (
    Migration(
        1,
        "baseline gateway state schema",
        (
            """CREATE TABLE IF NOT EXISTS audit_runs (
                run_id TEXT PRIMARY KEY, tenant_id TEXT, user_sub TEXT, mode TEXT,
                created_at TEXT NOT NULL, payload_json TEXT NOT NULL)""",
            "CREATE INDEX IF NOT EXISTS idx_audit_runs_created ON audit_runs (created_at DESC)",
            """CREATE INDEX IF NOT EXISTS idx_audit_runs_tenant_created
                ON audit_runs (tenant_id, created_at DESC)""",
            """CREATE TABLE IF NOT EXISTS dynamic_upstreams (
                name TEXT PRIMARY KEY, config_json TEXT NOT NULL, updated_at TEXT NOT NULL)""",
            """CREATE TABLE IF NOT EXISTS model_pricing (
                model_name TEXT PRIMARY KEY, input_per_1m_usd DOUBLE PRECISION NOT NULL,
                output_per_1m_usd DOUBLE PRECISION NOT NULL, updated_at TEXT NOT NULL)""",
            """CREATE TABLE IF NOT EXISTS gateway_settings (
                key TEXT PRIMARY KEY, value_json TEXT NOT NULL, updated_at TEXT NOT NULL)""",
            """CREATE TABLE IF NOT EXISTS adaptive_evidence (
                tenant_id TEXT NOT NULL, evidence_id TEXT NOT NULL, created_at TEXT NOT NULL,
                payload_json TEXT NOT NULL, PRIMARY KEY (tenant_id, evidence_id))""",
            """CREATE INDEX IF NOT EXISTS idx_adaptive_evidence_tenant_created
                ON adaptive_evidence (tenant_id, created_at DESC)""",
            """CREATE TABLE IF NOT EXISTS consumed_request_state_nonces (
                nonce TEXT PRIMARY KEY, expires_at TEXT NOT NULL)""",
            """CREATE TABLE IF NOT EXISTS tool_approvals (
                approval_id TEXT PRIMARY KEY, tenant_id TEXT NOT NULL, requester_sub TEXT NOT NULL,
                upstream_name TEXT NOT NULL, tool_name TEXT NOT NULL, argument_digest TEXT NOT NULL,
                policy_version INTEGER NOT NULL, approver_groups_json TEXT NOT NULL,
                action_summary TEXT NOT NULL, status TEXT NOT NULL, created_at TEXT NOT NULL,
                expires_at TEXT NOT NULL, decided_by TEXT, decided_at TEXT,
                execution_started_at TEXT, completed_at TEXT)""",
            """CREATE UNIQUE INDEX IF NOT EXISTS idx_tool_approvals_active_action
                ON tool_approvals (tenant_id, requester_sub, upstream_name, tool_name,
                                   argument_digest, policy_version)
                WHERE status IN ('pending', 'approved', 'denied')""",
            """CREATE INDEX IF NOT EXISTS idx_tool_approvals_tenant_status_created
                ON tool_approvals (tenant_id, status, created_at DESC)""",
            """CREATE TABLE IF NOT EXISTS tool_guidance (
                tenant_id TEXT NOT NULL, tool_name TEXT NOT NULL, payload_json TEXT NOT NULL,
                PRIMARY KEY (tenant_id, tool_name))""",
            """CREATE TABLE IF NOT EXISTS gateway_secrets (
                name TEXT PRIMARY KEY, ciphertext TEXT NOT NULL,
                updated_by TEXT NOT NULL DEFAULT '', updated_at TEXT NOT NULL)""",
            """CREATE TABLE IF NOT EXISTS recovery_operations (
                operation_id TEXT PRIMARY KEY, tenant_id TEXT NOT NULL, requester_sub TEXT NOT NULL,
                client_id TEXT NOT NULL DEFAULT '', upstream_name TEXT NOT NULL,
                tool_name TEXT NOT NULL,
                dedupe_key TEXT NOT NULL UNIQUE, request_digest TEXT NOT NULL,
                payload_ciphertext TEXT NOT NULL, result_ciphertext TEXT, status TEXT NOT NULL,
                attempt_count INTEGER NOT NULL, max_attempts INTEGER NOT NULL, next_attempt_at TEXT,
                recovery_deadline TEXT NOT NULL, lease_token TEXT, lease_expires_at TEXT,
                cancel_requested BOOLEAN NOT NULL DEFAULT FALSE, last_error_type TEXT,
                last_error TEXT,
                created_at TEXT NOT NULL, updated_at TEXT NOT NULL, expires_at TEXT NOT NULL)""",
            """CREATE TABLE IF NOT EXISTS sensitive_write_audits (
                operation_id TEXT PRIMARY KEY, tenant_id TEXT NOT NULL, caller_sub TEXT NOT NULL,
                upstream_name TEXT NOT NULL, tool_name TEXT NOT NULL, request_digest TEXT NOT NULL,
                correlation_id TEXT NOT NULL, dedupe_key TEXT NOT NULL UNIQUE, status TEXT NOT NULL,
                created_at TEXT NOT NULL, updated_at TEXT NOT NULL)""",
            """CREATE INDEX IF NOT EXISTS idx_recovery_operations_claim
                ON recovery_operations (status, next_attempt_at, lease_expires_at,
                                        recovery_deadline)""",
            """CREATE INDEX IF NOT EXISTS idx_recovery_operations_owner
                ON recovery_operations (tenant_id, requester_sub, client_id, created_at DESC)""",
            """CREATE TABLE IF NOT EXISTS user_upstream_credentials (
                tenant_id TEXT NOT NULL, user_sub TEXT NOT NULL, upstream_name TEXT NOT NULL,
                auth_kind TEXT NOT NULL, ciphertext TEXT NOT NULL, expires_at BIGINT,
                updated_at TEXT NOT NULL, PRIMARY KEY (tenant_id, user_sub, upstream_name))""",
        ),
    ),
    Migration(
        2,
        "remove persisted user OAuth tokens and add safe operations view",
        (
            "DELETE FROM user_upstream_credentials WHERE auth_kind = 'user_oauth'",
            """CREATE OR REPLACE VIEW sensitive_write_operations AS
                SELECT operation_id, tenant_id, upstream_name, tool_name,
                       status, created_at, updated_at
                FROM sensitive_write_audits""",
        ),
    ),
)

EXPECTED_COLUMNS: dict[str, set[str]] = {
    "audit_runs": {"run_id", "tenant_id", "user_sub", "mode", "created_at", "payload_json"},
    "dynamic_upstreams": {"name", "config_json", "updated_at"},
    "model_pricing": {"model_name", "input_per_1m_usd", "output_per_1m_usd", "updated_at"},
    "gateway_settings": {"key", "value_json", "updated_at"},
    "adaptive_evidence": {"tenant_id", "evidence_id", "created_at", "payload_json"},
    "consumed_request_state_nonces": {"nonce", "expires_at"},
    "tool_approvals": {
        "approval_id",
        "tenant_id",
        "requester_sub",
        "upstream_name",
        "tool_name",
        "argument_digest",
        "policy_version",
        "approver_groups_json",
        "action_summary",
        "status",
        "created_at",
        "expires_at",
        "decided_by",
        "decided_at",
        "execution_started_at",
        "completed_at",
    },
    "tool_guidance": {"tenant_id", "tool_name", "payload_json"},
    "gateway_secrets": {"name", "ciphertext", "updated_by", "updated_at"},
    "recovery_operations": {
        "operation_id",
        "tenant_id",
        "requester_sub",
        "client_id",
        "upstream_name",
        "tool_name",
        "dedupe_key",
        "request_digest",
        "payload_ciphertext",
        "result_ciphertext",
        "status",
        "attempt_count",
        "max_attempts",
        "next_attempt_at",
        "recovery_deadline",
        "lease_token",
        "lease_expires_at",
        "cancel_requested",
        "last_error_type",
        "last_error",
        "created_at",
        "updated_at",
        "expires_at",
    },
    "sensitive_write_audits": {
        "operation_id",
        "tenant_id",
        "caller_sub",
        "upstream_name",
        "tool_name",
        "request_digest",
        "correlation_id",
        "dedupe_key",
        "status",
        "created_at",
        "updated_at",
    },
    "user_upstream_credentials": {
        "tenant_id",
        "user_sub",
        "upstream_name",
        "auth_kind",
        "ciphertext",
        "expires_at",
        "updated_at",
    },
}

EXPECTED_INDEXES: dict[str, tuple[str, bool]] = {
    "audit_runs_pkey": ("audit_runs", True),
    "idx_audit_runs_created": ("audit_runs", False),
    "idx_audit_runs_tenant_created": ("audit_runs", False),
    "dynamic_upstreams_pkey": ("dynamic_upstreams", True),
    "model_pricing_pkey": ("model_pricing", True),
    "gateway_settings_pkey": ("gateway_settings", True),
    "adaptive_evidence_pkey": ("adaptive_evidence", True),
    "idx_adaptive_evidence_tenant_created": ("adaptive_evidence", False),
    "consumed_request_state_nonces_pkey": ("consumed_request_state_nonces", True),
    "tool_approvals_pkey": ("tool_approvals", True),
    "idx_tool_approvals_active_action": ("tool_approvals", True),
    "idx_tool_approvals_tenant_status_created": ("tool_approvals", False),
    "tool_guidance_pkey": ("tool_guidance", True),
    "gateway_secrets_pkey": ("gateway_secrets", True),
    "recovery_operations_pkey": ("recovery_operations", True),
    "recovery_operations_dedupe_key_key": ("recovery_operations", True),
    "idx_recovery_operations_claim": ("recovery_operations", False),
    "idx_recovery_operations_owner": ("recovery_operations", False),
    "sensitive_write_audits_pkey": ("sensitive_write_audits", True),
    "sensitive_write_audits_dedupe_key_key": ("sensitive_write_audits", True),
    "user_upstream_credentials_pkey": ("user_upstream_credentials", True),
}


async def current_version(conn: Any) -> int:
    exists = await conn.fetchval("SELECT to_regclass($1)", MIGRATION_TABLE)
    if exists is None:
        return 0
    value = await conn.fetchval(f"SELECT COALESCE(MAX(version), 0) FROM {MIGRATION_TABLE}")
    return int(value or 0)


async def upgrade(dsn: str, *, connect: Any = None) -> tuple[int, int]:
    """Apply pending migrations under a transaction-scoped advisory lock."""
    if connect is None:
        try:
            import asyncpg
        except ImportError as exc:
            raise RuntimeError("PostgreSQL migrations require 'mcpcodeaway[postgres]'.") from exc
        connect = asyncpg.connect
    conn = await connect(_postgres_dsn_with_secure_tls_default(dsn), timeout=10.0)
    try:
        async with conn.transaction():
            await conn.execute("SELECT pg_advisory_xact_lock($1)", ADVISORY_LOCK_KEY)
            before = await current_version(conn)
            latest = MIGRATIONS[-1].version
            if before > latest:
                raise RuntimeError(
                    f"Database schema version {before} is newer than supported version {latest}."
                )
            await conn.execute(
                f"""CREATE TABLE IF NOT EXISTS {MIGRATION_TABLE} (
                    version INTEGER PRIMARY KEY, description TEXT NOT NULL,
                    applied_at TIMESTAMPTZ NOT NULL)"""
            )
            for migration in MIGRATIONS:
                if migration.version <= before:
                    continue
                log.info(
                    "applying PostgreSQL migration version=%d description=%s",
                    migration.version,
                    migration.description,
                )
                for statement in migration.statements:
                    await conn.execute(statement)
                await conn.execute(
                    f"""INSERT INTO {MIGRATION_TABLE}
                        (version, description, applied_at) VALUES ($1, $2, $3)""",
                    migration.version,
                    migration.description,
                    datetime.now(UTC),
                )
            after = await current_version(conn)
            return before, after
    finally:
        await conn.close()


async def verify_schema(conn: Any, *, expected_version: int | None = None) -> int:
    """Check version and required columns without issuing schema DDL."""
    version = await current_version(conn)
    expected = MIGRATIONS[-1].version if expected_version is None else expected_version
    if version != expected:
        raise RuntimeError(
            f"PostgreSQL schema version is {version}; gateway requires version {expected}. "
            f"Run `uv run python -m gateway.migrations upgrade` before starting the gateway."
        )
    rows = await conn.fetch(
        "SELECT table_name, column_name FROM information_schema.columns "
        "WHERE table_schema = current_schema() AND table_name = ANY($1::text[])",
        list(EXPECTED_COLUMNS),
    )
    actual: dict[str, set[str]] = {}
    for row in rows:
        actual.setdefault(str(row["table_name"]), set()).add(str(row["column_name"]))
    missing = {
        table: sorted(columns - actual.get(table, set()))
        for table, columns in EXPECTED_COLUMNS.items()
        if columns - actual.get(table, set())
    }
    if missing:
        summary = "; ".join(f"{table}: {', '.join(columns)}" for table, columns in missing.items())
        raise RuntimeError(
            f"PostgreSQL schema version {version} is incomplete ({summary}). Apply migrations."
        )
    index_rows = await conn.fetch(
        """SELECT index_class.relname AS index_name,
                  table_class.relname AS table_name,
                  index_info.indisunique AS is_unique
           FROM pg_class AS index_class
           JOIN pg_index AS index_info ON index_info.indexrelid = index_class.oid
           JOIN pg_class AS table_class ON table_class.oid = index_info.indrelid
           JOIN pg_namespace AS schema_info ON schema_info.oid = index_class.relnamespace
           WHERE schema_info.nspname = current_schema()
             AND index_class.relname = ANY($1::text[])""",
        list(EXPECTED_INDEXES),
    )
    actual_indexes = {
        str(row["index_name"]): (str(row["table_name"]), bool(row["is_unique"]))
        for row in index_rows
    }
    invalid_indexes = {
        name: expected
        for name, expected in EXPECTED_INDEXES.items()
        if actual_indexes.get(name) != expected
    }
    if invalid_indexes:
        summary = "; ".join(
            f"{name} on {table} (unique={unique})"
            for name, (table, unique) in invalid_indexes.items()
        )
        raise RuntimeError(
            f"PostgreSQL schema version {version} is incomplete or incompatible "
            f"(indexes: {summary}). Apply migrations."
        )
    return version


async def _main(action: str) -> int:
    dsn = os.environ.get(MIGRATION_DSN_ENV, "").strip()
    if not dsn:
        raise RuntimeError(f"Set {MIGRATION_DSN_ENV} to a migration-capable PostgreSQL DSN.")
    if action != "upgrade":
        raise RuntimeError(f"Unsupported migration action: {action}")
    before, after = await upgrade(dsn)
    print(f"PostgreSQL schema version: {before} -> {after}")
    return 0


def main() -> None:
    parser = argparse.ArgumentParser(description="Apply MCPcodeaway PostgreSQL migrations.")
    parser.add_argument("action", choices=("upgrade",))
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    try:
        raise SystemExit(asyncio.run(_main(args.action)))
    except SystemExit:
        raise
    except Exception as exc:
        log.error(
            "PostgreSQL migration failed (%s). Check database and migration logs.",
            type(exc).__name__,
        )
        raise SystemExit(1) from None


if __name__ == "__main__":
    main()
