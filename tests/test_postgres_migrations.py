"""Focused tests for the explicit PostgreSQL schema lifecycle."""

from __future__ import annotations

import asyncio
import os
import sys
from typing import Any

import pytest

import gateway.migrations as migrations
from gateway.migrations import EXPECTED_COLUMNS, MIGRATIONS, upgrade, verify_schema


class MigrationDatabase:
    def __init__(self, version: int = 0) -> None:
        self.lock = asyncio.Lock()
        self.history_exists = version > 0
        self.versions = list(range(1, version + 1))
        self.statements: list[str] = []
        self.existing_rows = {"audit_runs": ["preexisting-run"]} if version else {}


class MigrationConnection:
    def __init__(self, database: MigrationDatabase) -> None:
        self.database = database

    class Transaction:
        def __init__(self, database: MigrationDatabase) -> None:
            self.database = database

        async def __aenter__(self) -> None:
            await self.database.lock.acquire()

        async def __aexit__(self, *_args: Any) -> None:
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

    with pytest.raises(RuntimeError, match="requires version 2"):
        await verify_schema(MissingSchemaConnection())

    class NewerSchemaConnection:
        async def fetchval(self, sql: str, *_args: Any) -> Any:
            if "to_regclass" in sql:
                return "mcpcodeaway_schema_migrations"
            assert "MAX(version)" in sql
            return 3

    with pytest.raises(RuntimeError, match="requires version 2"):
        await verify_schema(NewerSchemaConnection())


def test_migration_list_is_ordered_and_token_cleanup_is_explicit() -> None:
    assert [migration.version for migration in MIGRATIONS] == [1, 2]
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

    assert await upgrade("postgresql://migration", connect=connect) == (0, 2)
    first_run_statement_count = len(database.statements)
    assert database.versions == [1, 2]
    assert await upgrade("postgresql://migration", connect=connect) == (2, 2)
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
    assert sorted(outcomes) == [(1, 2), (2, 2)]
    assert database.versions == [1, 2]
    assert database.existing_rows == {"audit_runs": ["preexisting-run"]}


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
        assert [row["version"] for row in versions] == [1, 2]
        oauth_rows = await conn.fetchval(
            "SELECT COUNT(*) FROM user_upstream_credentials WHERE auth_kind='user_oauth'"
        )
        assert oauth_rows == 0
        view_exists = await conn.fetchval("SELECT to_regclass('sensitive_write_operations')")
        assert view_exists is not None
    finally:
        await conn.close()
