"""Grant the disposable PostgreSQL runtime role DML after migrations."""

from __future__ import annotations

import asyncio
import os

from gateway.migrations import MIGRATION_DSN_ENV
from gateway.state import _postgres_dsn_with_secure_tls_default


async def main() -> None:
    dsn = os.environ.get(MIGRATION_DSN_ENV, "").strip()
    if not dsn:
        raise RuntimeError(f"Set {MIGRATION_DSN_ENV} to the local migration-role DSN.")
    try:
        import asyncpg
    except ImportError as exc:
        raise RuntimeError("Install mcpcodeaway[postgres] for PostgreSQL acceptance.") from exc

    conn = await asyncpg.connect(_postgres_dsn_with_secure_tls_default(dsn))
    try:
        await conn.execute(
            "GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA gateway "
            "TO gateway_limited"
        )
        await conn.execute(
            "REVOKE INSERT, UPDATE, DELETE ON mcpcodeaway_schema_migrations FROM gateway_limited"
        )
        await conn.execute(
            "ALTER DEFAULT PRIVILEGES IN SCHEMA gateway GRANT SELECT, INSERT, UPDATE, DELETE "
            "ON TABLES TO gateway_limited"
        )
        await conn.execute(
            "CREATE TABLE gateway._migration_ddl_probe (probe_id INTEGER PRIMARY KEY)"
        )
        await conn.execute("DROP TABLE gateway._migration_ddl_probe")
    finally:
        await conn.close()


if __name__ == "__main__":
    asyncio.run(main())
