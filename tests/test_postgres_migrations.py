"""Focused tests for the explicit PostgreSQL schema lifecycle."""

from __future__ import annotations

import asyncio
import copy
import os
import sys
from typing import Any
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

import pytest

import gateway.migrations as migrations
from gateway.migrations import (
    EXPECTED_COLUMNS,
    EXPECTED_INDEXES,
    MIGRATION_TABLE,
    MIGRATIONS,
    upgrade,
    verify_schema,
)
from gateway.state import BusinessExecutionConflict, PostgresAuditStore


class MigrationDatabase:
    def __init__(self, version: int = 0) -> None:
        self.lock = asyncio.Lock()
        self.history_exists = version > 0
        self.versions = list(range(1, version + 1))
        self.statements: list[str] = []
        self.existing_rows = (
            {
                "audit_runs": ["preexisting-run"],
                "sensitive_write_audits": ["preexisting-journal"],
                "tool_approvals": ["preexisting-approval"],
                "recovery_operations": ["preexisting-recovery"],
            }
            if version
            else {}
        )
        self.user_credentials = (
            [{"auth_kind": "user_oauth"}, {"auth_kind": "user_api_key"}] if version else []
        )
        self.gateway_secrets = (
            [{"name": "unrelated-secret", "ciphertext": "opaque"}] if version else []
        )


class MigrationConnection:
    def __init__(self, database: MigrationDatabase) -> None:
        self.database = database

    class Transaction:
        def __init__(self, database: MigrationDatabase) -> None:
            self.database = database
            self.snapshot: dict[str, Any] = {}

        async def __aenter__(self) -> None:
            await self.database.lock.acquire()
            self.snapshot = copy.deepcopy(self.database.__dict__)

        async def __aexit__(self, _exc_type: Any, exc: Any, _traceback: Any) -> None:
            if exc is not None:
                self.database.__dict__.update(self.snapshot)
            self.database.lock.release()

    def transaction(self) -> Transaction:
        return self.Transaction(self.database)

    async def execute(self, sql: str, *args: Any) -> str:
        normalized = " ".join(sql.split())
        if "CREATE TABLE IF NOT EXISTS mcpcodeaway_schema_migrations" in normalized:
            self.database.history_exists = True
        elif normalized.startswith("INSERT INTO mcpcodeaway_schema_migrations"):
            self.database.versions.append(int(args[0]))
        elif "pg_advisory_xact_lock" in normalized:
            return "OK"
        elif normalized == "DELETE FROM user_upstream_credentials WHERE auth_kind = 'user_oauth'":
            self.database.user_credentials = [
                row for row in self.database.user_credentials if row["auth_kind"] != "user_oauth"
            ]
        elif normalized == "FAIL_TEST_MIGRATION":
            raise RuntimeError("simulated migration failure")
        else:
            self.database.statements.append(normalized)
        return "OK"

    async def fetchval(self, sql: str, *_args: Any) -> Any:
        if "to_regclass" in sql:
            return MIGRATIONS[-1].version if self.database.history_exists else None
        return max(self.database.versions, default=0)

    async def close(self) -> None:
        return None


async def _fake_connect(
    database: MigrationDatabase, *_args: Any, **_kwargs: Any
) -> MigrationConnection:
    return MigrationConnection(database)


@pytest.mark.asyncio
async def test_gateway_rejects_missing_or_unsupported_schema_without_ddl() -> None:
    class MissingSchemaConnection:
        async def fetchval(self, sql: str, *_args: Any) -> Any:
            assert "to_regclass" in sql
            return None

    with pytest.raises(RuntimeError, match="requires version 6"):
        await verify_schema(MissingSchemaConnection())

    class NewerSchemaConnection:
        async def fetchval(self, sql: str, *_args: Any) -> Any:
            if "to_regclass" in sql:
                return "mcpcodeaway_schema_migrations"
            assert "MAX(version)" in sql
            return 7

    with pytest.raises(RuntimeError, match="requires version 6"):
        await verify_schema(NewerSchemaConnection())

    class IncompatibleSchemaConnection:
        async def fetchval(self, sql: str, *_args: Any) -> Any:
            if "to_regclass" in sql:
                return MIGRATIONS[-1].version
            return max(migrations.MIGRATIONS[-1].version, 0)

        async def fetch(self, _sql: str, _tables: list[str]) -> list[dict[str, str]]:
            return [{"table_name": "audit_runs", "column_name": "run_id"}]

    with pytest.raises(RuntimeError, match="schema version 6 is incomplete"):
        await verify_schema(IncompatibleSchemaConnection())

    class MissingSafetyIndexConnection:
        async def fetchval(self, sql: str, *_args: Any) -> Any:
            if "to_regclass" in sql:
                return MIGRATIONS[-1].version
            return max(migrations.MIGRATIONS[-1].version, 0)

        async def fetch(self, sql: str, _names: list[str]) -> list[dict[str, Any]]:
            if "information_schema.columns" in sql:
                return [
                    {"table_name": table, "column_name": column}
                    for table, columns in EXPECTED_COLUMNS.items()
                    for column in columns
                ]
            return [
                {
                    "index_name": name,
                    "table_name": table,
                    "is_unique": unique,
                }
                for name, (table, unique) in EXPECTED_INDEXES.items()
                if name != "sensitive_write_audits_dedupe_key_key"
            ]

    with pytest.raises(RuntimeError, match="sensitive_write_audits_dedupe_key_key"):
        await verify_schema(MissingSafetyIndexConnection())


def test_migration_list_is_ordered_and_token_cleanup_is_explicit() -> None:
    assert [migration.version for migration in MIGRATIONS] == [1, 2, 3, 4, 5, 6]
    assert any(
        "DELETE FROM user_upstream_credentials" in statement
        for statement in MIGRATIONS[1].statements
    )
    assert "sensitive_write_audits" in EXPECTED_COLUMNS


@pytest.mark.asyncio
async def test_empty_database_migrations_are_versioned_and_repeatable() -> None:
    database = MigrationDatabase()

    def connect(*args: Any, **kwargs: Any) -> Any:
        return _fake_connect(database, *args, **kwargs)

    assert await upgrade("postgresql://migration", connect=connect) == (0, 6)
    first_run_statement_count = len(database.statements)
    assert database.versions == [1, 2, 3, 4, 5, 6]
    assert await upgrade("postgresql://migration", connect=connect) == (6, 6)
    assert len(database.statements) == first_run_statement_count


@pytest.mark.asyncio
async def test_existing_database_upgrade_keeps_rows_and_concurrent_jobs_serialize() -> None:
    database = MigrationDatabase(version=1)

    def connect(*args: Any, **kwargs: Any) -> Any:
        return _fake_connect(database, *args, **kwargs)

    outcomes = await asyncio.gather(
        upgrade("postgresql://migration", connect=connect),
        upgrade("postgresql://migration", connect=connect),
    )
    assert sorted(outcomes) == [(1, 6), (6, 6)]
    assert database.versions == [1, 2, 3, 4, 5, 6]
    assert database.existing_rows == {
        "audit_runs": ["preexisting-run"],
        "sensitive_write_audits": ["preexisting-journal"],
        "tool_approvals": ["preexisting-approval"],
        "recovery_operations": ["preexisting-recovery"],
    }
    assert database.user_credentials == [{"auth_kind": "user_api_key"}]
    assert database.gateway_secrets == [{"name": "unrelated-secret", "ciphertext": "opaque"}]


@pytest.mark.asyncio
async def test_failed_migration_rolls_back_token_cleanup_and_restart_can_retry(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database = MigrationDatabase(version=1)

    def connect(*args: Any, **kwargs: Any) -> Any:
        return _fake_connect(database, *args, **kwargs)

    original_migrations = migrations.MIGRATIONS
    monkeypatch.setattr(
        migrations,
        "MIGRATIONS",
        (
            original_migrations[0],
            migrations.Migration(
                2,
                "failing",
                (
                    "DELETE FROM user_upstream_credentials WHERE auth_kind = 'user_oauth'",
                    "FAIL_TEST_MIGRATION",
                ),
            ),
        ),
    )
    with pytest.raises(RuntimeError, match="simulated migration failure"):
        await upgrade("postgresql://migration", connect=connect)
    assert database.versions == [1]
    assert database.user_credentials == [
        {"auth_kind": "user_oauth"},
        {"auth_kind": "user_api_key"},
    ]

    monkeypatch.setattr(migrations, "MIGRATIONS", original_migrations)
    assert await upgrade("postgresql://migration", connect=connect) == (1, 6)
    assert database.user_credentials == [{"auth_kind": "user_api_key"}]
    assert database.gateway_secrets == [{"name": "unrelated-secret", "ciphertext": "opaque"}]


def test_migration_failure_is_reported_without_connection_details(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    async def fail_upgrade(_dsn: str) -> tuple[int, int]:
        raise PermissionError("database rejected migration credentials")

    monkeypatch.setenv(migrations.MIGRATION_DSN_ENV, "postgresql://secret:password@db/app")
    monkeypatch.setattr(migrations, "upgrade", fail_upgrade)
    monkeypatch.setattr(sys, "argv", ["gateway.migrations", "upgrade"])
    with pytest.raises(SystemExit) as exc_info:
        migrations.main()
    assert exc_info.value.code == 1
    assert "PermissionError" in caplog.text
    assert "password" not in caplog.text


@pytest.mark.integration
@pytest.mark.postgres
@pytest.mark.asyncio
async def test_explicit_postgres_migrations_are_repeatable_and_concurrent() -> None:
    dsn = os.getenv("MCP_TEST_POSTGRES_MIGRATION_DSN")
    if not dsn:
        pytest.skip(
            "Set MCP_TEST_POSTGRES_MIGRATION_DSN to a disposable database with migration privileges"
        )
    pytest.importorskip("asyncpg", reason="install mcpcodeaway[postgres]")

    before, after = await upgrade(dsn)
    assert after == MIGRATIONS[-1].version
    assert before <= after
    repeated = await upgrade(dsn)
    assert repeated == (after, after)

    concurrent = await asyncio.gather(upgrade(dsn), upgrade(dsn))
    assert concurrent == [(after, after), (after, after)]

    import asyncpg

    conn = await asyncpg.connect(dsn)
    try:
        assert await verify_schema(conn) == MIGRATIONS[-1].version
        versions = await conn.fetch(
            "SELECT version FROM mcpcodeaway_schema_migrations ORDER BY version"
        )
        assert [row["version"] for row in versions] == [1, 2, 3, 4]
        oauth_rows = await conn.fetchval(
            "SELECT COUNT(*) FROM user_upstream_credentials WHERE auth_kind='user_oauth'"
        )
        assert oauth_rows == 0
        view_exists = await conn.fetchval("SELECT to_regclass('sensitive_write_operations')")
        assert view_exists is not None
        callback_slot_index = await conn.fetchval(
            "SELECT to_regclass('idx_workflow_calls_callback_slot')"
        )
        assert callback_slot_index is not None
    finally:
        await conn.close()


@pytest.mark.integration
@pytest.mark.postgres
@pytest.mark.asyncio
async def test_postgres_workflow_callback_slot_is_immutable_and_idempotent() -> None:
    dsn = os.getenv("MCP_TEST_POSTGRES_MIGRATION_DSN")
    if not dsn:
        pytest.skip("set MCP_TEST_POSTGRES_MIGRATION_DSN to a disposable migrated database")
    pytest.importorskip("asyncpg", reason="install mcpcodeaway[postgres]")

    store = PostgresAuditStore(dsn)
    prefix = f"callback-slot-{os.urandom(8).hex()}"
    execution_id = f"{prefix}-execution"
    try:
        await store.init()
        await store.create_business_execution(
            {
                "business_execution_id": execution_id,
                "tenant_id": prefix,
                "caller_sub": "callback-slot-test-user",
                "client_id": "callback-slot-test-client",
                "kind": "workflow",
                "upstream_name": "callback-slot-test-upstream",
                "expires_at": "2099-01-01T00:00:00+00:00",
                "metadata": {},
            }
        )
        call = {
            "workflow_call_id": f"{prefix}-call-0",
            "tenant_id": prefix,
            "business_execution_id": execution_id,
            "call_key": f"{prefix}-key-0",
            "outer_request_id": f"{prefix}-request",
            "callback_ordinal": 0,
            "upstream_name": "callback-slot-test-upstream",
            "tool_name": "read_customer",
            "request_digest": "body-digest",
            "arguments_digest": "arguments-digest-a",
        }
        _, inserted = await store.create_workflow_call(call)
        assert inserted
        _, inserted = await store.create_workflow_call(call)
        assert not inserted
        with pytest.raises(BusinessExecutionConflict, match="call key is already bound"):
            await store.create_workflow_call(
                {
                    **call,
                    "workflow_call_id": f"{prefix}-call-diverged",
                    "call_key": f"{prefix}-key-diverged",
                    "tool_name": "read_order_history",
                    "arguments_digest": "arguments-digest-b",
                }
            )
        assert len(await store.list_workflow_calls(prefix, execution_id)) == 1
    finally:
        if store._pool is not None:
            await store._pool.execute(
                "DELETE FROM workflow_calls WHERE tenant_id=$1 AND business_execution_id=$2",
                prefix,
                execution_id,
            )
            await store._pool.execute(
                "DELETE FROM workflow_executions WHERE tenant_id=$1 AND business_execution_id=$2",
                prefix,
                execution_id,
            )
        await store.close()


@pytest.mark.integration
@pytest.mark.postgres
@pytest.mark.asyncio
async def test_migration_cli_upgrades_v1_atomically_and_preserves_unrelated_state() -> None:
    migration_dsn = os.getenv("MCP_TEST_POSTGRES_MIGRATION_DSN")
    bootstrap_dsn = os.getenv("MCP_TEST_POSTGRES_BOOTSTRAP_DSN")
    if not migration_dsn or not bootstrap_dsn:
        pytest.skip("use scripts/local_acceptance.py --with-postgres for isolated migration roles")
    asyncpg = pytest.importorskip("asyncpg", reason="install mcpcodeaway[postgres]")
    schema = f"migration_v1_{os.urandom(6).hex()}"

    def scoped_dsn(dsn: str) -> str:
        parsed = urlsplit(dsn)
        query = dict(parse_qsl(parsed.query, keep_blank_values=True))
        query["options"] = f"-csearch_path={schema}"
        return urlunsplit(parsed._replace(query=urlencode(query)))

    bootstrap = await asyncpg.connect(bootstrap_dsn)
    try:
        await bootstrap.execute(f'CREATE SCHEMA "{schema}" AUTHORIZATION gateway_test')
    finally:
        await bootstrap.close()

    schema_dsn = scoped_dsn(migration_dsn)

    async def run_cli() -> tuple[int, str, str]:
        environment = os.environ.copy()
        environment["MCP_POSTGRES_MIGRATION_DSN"] = schema_dsn
        process = await asyncio.create_subprocess_exec(
            sys.executable,
            "-m",
            "gateway.migrations",
            "upgrade",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=environment,
        )
        stdout, stderr = await process.communicate()
        return process.returncode or 0, stdout.decode(), stderr.decode()

    try:
        conn = await asyncpg.connect(schema_dsn)
        try:
            for statement in MIGRATIONS[0].statements:
                await conn.execute(statement)
            await conn.execute(
                f"""CREATE TABLE {MIGRATION_TABLE} (
                    version INTEGER PRIMARY KEY, description TEXT NOT NULL,
                    applied_at TIMESTAMPTZ NOT NULL)"""
            )
            await conn.execute(
                f"INSERT INTO {MIGRATION_TABLE} VALUES (1, 'baseline gateway state schema', now())"
            )
            await conn.execute(
                "INSERT INTO user_upstream_credentials VALUES "
                "('tenant-a','user-a','oauth-upstream','user_oauth','old-oauth-envelope',NULL,'now'),"
                " "
                "('tenant-a','user-a','api-upstream','user_api_key','api-key-envelope',NULL,'now')"
            )
            await conn.execute(
                "INSERT INTO gateway_secrets VALUES ('legacy-secret','opaque','operator','now')"
            )
            await conn.execute(
                "INSERT INTO sensitive_write_audits VALUES "
                "('journal-1','tenant-a','user-a','upstream','write','digest','corr','dedupe',"
                "'succeeded','now','now')"
            )
            await conn.execute(
                """INSERT INTO tool_approvals (
                    approval_id, tenant_id, requester_sub, upstream_name, tool_name,
                    argument_digest, policy_version, approver_groups_json, action_summary,
                    status, created_at, expires_at
                ) VALUES ('approval-1','tenant-a','user-a','upstream','write','digest',1,
                         '[]','summary','pending','now','2099-01-01')"""
            )
            await conn.execute("CREATE TABLE sensitive_write_operations (unrelated INTEGER)")
        finally:
            await conn.close()

        failed_code, _failed_stdout, _failed_stderr = await run_cli()
        assert failed_code != 0
        conn = await asyncpg.connect(schema_dsn)
        try:
            assert await conn.fetchval(f"SELECT MAX(version) FROM {MIGRATION_TABLE}") == 1
            assert (
                await conn.fetchval(
                    "SELECT COUNT(*) FROM user_upstream_credentials WHERE auth_kind='user_oauth'"
                )
                == 1
            )
        finally:
            await conn.close()

        bootstrap = await asyncpg.connect(bootstrap_dsn)
        try:
            await bootstrap.execute(f'DROP TABLE "{schema}".sensitive_write_operations')
        finally:
            await bootstrap.close()

        success_code, success_stdout, _success_stderr = await run_cli()
        assert success_code == 0
        assert "PostgreSQL schema version: 1 -> 4" in success_stdout
        conn = await asyncpg.connect(schema_dsn)
        try:
            assert await verify_schema(conn) == 4
            assert (
                await conn.fetchval(
                    "SELECT COUNT(*) FROM user_upstream_credentials WHERE auth_kind='user_oauth'"
                )
                == 0
            )
            assert (
                await conn.fetchval(
                    "SELECT COUNT(*) FROM user_upstream_credentials WHERE auth_kind='user_api_key'"
                )
                == 1
            )
            assert await conn.fetchval("SELECT COUNT(*) FROM gateway_secrets") == 1
            assert await conn.fetchval("SELECT COUNT(*) FROM sensitive_write_audits") == 1
            assert await conn.fetchval("SELECT COUNT(*) FROM tool_approvals") == 1
            assert (
                await conn.fetchval("SELECT to_regclass('sensitive_write_operations')") is not None
            )
        finally:
            await conn.close()
    finally:
        bootstrap = await asyncpg.connect(bootstrap_dsn)
        try:
            await bootstrap.execute(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE')
        finally:
            await bootstrap.close()
