"""Pluggable cache and audit store abstractions for local and scaled gateway deployments."""

from __future__ import annotations

import asyncio
import inspect
import json
import sqlite3
import time
import uuid
from collections import deque
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Protocol


class CacheBackend(Protocol):
    """Protocol for token caching and sliding-window rate limiting."""

    async def get(self, key: str) -> str | None: ...

    async def set(self, key: str, value: str, ttl_seconds: int) -> None: ...

    async def delete(self, key: str) -> None: ...

    async def increment_window(self, key: str, window_seconds: int) -> int: ...


class InMemoryCacheBackend:
    """Zero-dependency async-safe in-memory TTL cache and sliding-window rate limiter."""

    def __init__(self) -> None:
        self._lock = asyncio.Lock()
        self._entries: dict[str, tuple[str, float]] = {}
        self._windows: dict[str, deque[float]] = {}
        self._subscribers: dict[str, set[asyncio.Queue[str]]] = {}

    async def get(self, key: str) -> str | None:
        async with self._lock:
            entry = self._entries.get(key)
            if entry is None:
                return None
            value, expires_at = entry
            if time.monotonic() >= expires_at:
                del self._entries[key]
                return None
            return value

    async def set(self, key: str, value: str, ttl_seconds: int) -> None:
        async with self._lock:
            expires_at = time.monotonic() + max(ttl_seconds, 0)
            self._entries[key] = (value, expires_at)

    async def delete(self, key: str) -> None:
        async with self._lock:
            self._entries.pop(key, None)

    async def increment_window(self, key: str, window_seconds: int) -> int:
        async with self._lock:
            now = time.monotonic()
            cutoff = now - window_seconds
            bucket = self._windows.setdefault(key, deque())
            while bucket and bucket[0] <= cutoff:
                bucket.popleft()
            bucket.append(now)
            return len(bucket)

    async def publish_event(self, channel: str, message: str) -> None:
        for queue in list(self._subscribers.get(channel, ())):
            if queue.full():
                try:
                    queue.get_nowait()
                except asyncio.QueueEmpty:
                    pass
            try:
                queue.put_nowait(message)
            except asyncio.QueueFull:
                pass

    async def subscribe_events(self, channel: str) -> AsyncIterator[str]:
        queue: asyncio.Queue[str] = asyncio.Queue(maxsize=256)
        self._subscribers.setdefault(channel, set()).add(queue)
        try:
            while True:
                yield await queue.get()
        finally:
            subs = self._subscribers.get(channel)
            if subs is not None:
                subs.discard(queue)


class RedisCacheBackend:
    """Shared Redis-backed TTL cache and sliding-window rate limiter for multi-replica scale."""

    def __init__(self, redis_url: str = "", *, client: Any = None) -> None:
        self.redis_url = redis_url
        self._client = client

    def _get_client(self) -> Any:
        if self._client is None:
            import redis.asyncio as aioredis

            self._client = aioredis.from_url(self.redis_url, decode_responses=True)
        return self._client

    async def health_check(self) -> bool:
        try:
            return bool(await self._get_client().ping())
        except Exception:
            return False

    async def get(self, key: str) -> str | None:
        val = await self._get_client().get(key)
        return val.decode("utf-8") if isinstance(val, bytes) else val

    async def set(self, key: str, value: str, ttl_seconds: int) -> None:
        await self._get_client().set(key, value, ex=max(ttl_seconds, 1))

    async def delete(self, key: str) -> None:
        await self._get_client().delete(key)

    async def increment_window(self, key: str, window_seconds: int) -> int:
        client = self._get_client()
        now = time.time()
        cutoff = now - window_seconds
        member = f"{now}:{uuid.uuid4().hex}"
        if hasattr(client, "pipeline"):
            pipe = client.pipeline(transaction=True)
            pipe.zremrangebyscore(key, 0, cutoff)
            pipe.zadd(key, {member: now})
            pipe.zcard(key)
            pipe.expire(key, max(window_seconds, 1))
            res = await pipe.execute()
            return int(res[2])
        await client.zremrangebyscore(key, 0, cutoff)
        await client.zadd(key, {member: now})
        count = int(await client.zcard(key))
        await client.expire(key, max(window_seconds, 1))
        return count

    async def close(self) -> None:
        if self._client is not None and hasattr(self._client, "aclose"):
            await self._client.aclose()

    async def publish_event(self, channel: str, message: str) -> None:
        client = self._get_client()
        if hasattr(client, "publish"):
            await client.publish(channel, message)

    async def subscribe_events(self, channel: str) -> AsyncIterator[str]:
        client = self._get_client()
        if not hasattr(client, "pubsub"):
            return
        pubsub = client.pubsub()
        await pubsub.subscribe(channel)
        try:
            async for raw in pubsub.listen():
                if raw and raw.get("type") == "message":
                    data = raw.get("data")
                    yield data.decode("utf-8") if isinstance(data, bytes) else str(data)
        finally:
            await pubsub.unsubscribe(channel)
            if hasattr(pubsub, "aclose"):
                await pubsub.aclose()


class AuditStore(Protocol):
    """Protocol for recording execution audit trails and dynamic upstreams."""

    async def init(self) -> None: ...

    async def record_run(self, run: dict[str, Any]) -> None: ...

    async def list_runs(
        self, limit: int = 50, tenant_id: str | None = None
    ) -> list[dict[str, Any]]: ...

    async def get_run(self, run_id: str) -> dict[str, Any] | None: ...

    async def save_dynamic_upstream(self, name: str, config: dict[str, Any]) -> None: ...

    async def list_dynamic_upstreams(self) -> list[dict[str, Any]]: ...

    async def delete_dynamic_upstream(self, name: str) -> bool: ...

    async def export_runs_older_than(self, retention_days: int) -> list[dict[str, Any]]: ...

    async def prune_runs_older_than(self, retention_days: int) -> int: ...


class SQLiteAuditStore:
    """Zero-dependency SQLite-backed store for execution audit runs and dynamic upstreams."""

    def __init__(self, db_path: str = "data/gateway.db") -> None:
        self.db_path = db_path
        self._lock = asyncio.Lock()
        self._conn: sqlite3.Connection | None = None

    def _ensure_conn(self) -> sqlite3.Connection:
        if self._conn is not None:
            return self._conn
        if self.db_path != ":memory:":
            Path(self.db_path).parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(self.db_path, check_same_thread=False)
        conn.row_factory = sqlite3.Row
        if self.db_path != ":memory:":
            conn.execute("PRAGMA journal_mode=WAL;")
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS audit_runs (
                run_id TEXT PRIMARY KEY,
                tenant_id TEXT,
                user_sub TEXT,
                mode TEXT,
                created_at TEXT NOT NULL,
                payload_json TEXT NOT NULL
            )
            """
        )
        conn.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_audit_runs_created
            ON audit_runs (created_at DESC)
            """
        )
        conn.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_audit_runs_tenant_created
            ON audit_runs (tenant_id, created_at DESC)
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS dynamic_upstreams (
                name TEXT PRIMARY KEY,
                config_json TEXT NOT NULL,
                updated_at TEXT NOT NULL
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS model_pricing (
                model_name TEXT PRIMARY KEY,
                input_per_1m_usd REAL NOT NULL,
                output_per_1m_usd REAL NOT NULL,
                updated_at TEXT NOT NULL
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS gateway_settings (
                key TEXT PRIMARY KEY,
                value_json TEXT NOT NULL,
                updated_at TEXT NOT NULL
            )
            """
        )
        conn.commit()
        self._conn = conn
        return conn

    async def init(self) -> None:
        async with self._lock:
            self._ensure_conn()

    async def close(self) -> None:
        async with self._lock:
            if self._conn is not None:
                self._conn.close()
                self._conn = None

    async def record_run(self, run: dict[str, Any]) -> None:
        created_at = str(run.get("created_at") or datetime.now(UTC).isoformat())
        payload = json.dumps(run, default=str)
        async with self._lock:
            conn = self._ensure_conn()
            conn.execute(
                """
                INSERT OR REPLACE INTO audit_runs
                    (run_id, tenant_id, user_sub, mode, created_at, payload_json)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    str(run["run_id"]),
                    run.get("tenant_id"),
                    run.get("user_sub"),
                    run.get("mode"),
                    created_at,
                    payload,
                ),
            )
            conn.commit()

    async def list_runs(
        self, limit: int = 50, tenant_id: str | None = None
    ) -> list[dict[str, Any]]:
        async with self._lock:
            conn = self._ensure_conn()
            if tenant_id is not None:
                rows = conn.execute(
                    """
                    SELECT payload_json FROM audit_runs
                    WHERE tenant_id = ?
                    ORDER BY created_at DESC
                    LIMIT ?
                    """,
                    (tenant_id, limit),
                ).fetchall()
            else:
                rows = conn.execute(
                    """
                    SELECT payload_json FROM audit_runs
                    ORDER BY created_at DESC
                    LIMIT ?
                    """,
                    (limit,),
                ).fetchall()
        return [json.loads(row["payload_json"]) for row in rows]

    async def query_runs(
        self,
        *,
        limit: int = 25,
        offset: int = 0,
        tenant_id: str | None = None,
        status: str = "all",
        mode: str | None = None,
        q: str | None = None,
    ) -> tuple[list[dict[str, Any]], int]:
        clauses: list[str] = []
        params: list[Any] = []
        if tenant_id is not None:
            clauses.append("tenant_id = ?")
            params.append(tenant_id)
        if mode and mode != "all":
            clauses.append("mode = ?")
            params.append(mode)
        if status == "errors":
            clauses.append("json_extract(payload_json, '$.ok') = 0")
        elif status == "grounding_blocked":
            clauses.append("json_extract(payload_json, '$.grounding_blocked') = 1")
        elif status == "ok":
            clauses.append("json_extract(payload_json, '$.ok') = 1")
        if q and q.strip():
            needle = f"%{q.strip()}%"
            clauses.append("(run_id LIKE ? OR user_sub LIKE ? OR payload_json LIKE ?)")
            params.extend([needle, needle, needle])

        where_sql = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        async with self._lock:
            conn = self._ensure_conn()
            total_row = conn.execute(
                f"SELECT COUNT(*) AS cnt FROM audit_runs {where_sql}",
                tuple(params),
            ).fetchone()
            total = int(total_row["cnt"]) if total_row is not None else 0
            rows = conn.execute(
                f"""
                SELECT payload_json FROM audit_runs
                {where_sql}
                ORDER BY created_at DESC
                LIMIT ? OFFSET ?
                """,
                (*params, max(1, min(limit, 200)), max(0, offset)),
            ).fetchall()
        return [json.loads(row["payload_json"]) for row in rows], total

    async def get_run(self, run_id: str) -> dict[str, Any] | None:
        async with self._lock:
            conn = self._ensure_conn()
            row = conn.execute(
                "SELECT payload_json FROM audit_runs WHERE run_id = ?",
                (run_id,),
            ).fetchone()
        return json.loads(row["payload_json"]) if row is not None else None

    async def save_dynamic_upstream(self, name: str, config: dict[str, Any]) -> None:
        now = datetime.now(UTC).isoformat()
        payload = json.dumps(config, default=str)
        async with self._lock:
            conn = self._ensure_conn()
            conn.execute(
                """
                INSERT OR REPLACE INTO dynamic_upstreams (name, config_json, updated_at)
                VALUES (?, ?, ?)
                """,
                (name, payload, now),
            )
            conn.commit()

    async def list_dynamic_upstreams(self) -> list[dict[str, Any]]:
        async with self._lock:
            conn = self._ensure_conn()
            rows = conn.execute(
                "SELECT config_json FROM dynamic_upstreams ORDER BY name ASC"
            ).fetchall()
        return [json.loads(row["config_json"]) for row in rows]

    async def delete_dynamic_upstream(self, name: str) -> bool:
        async with self._lock:
            conn = self._ensure_conn()
            cursor = conn.execute("DELETE FROM dynamic_upstreams WHERE name = ?", (name,))
            conn.commit()
            return cursor.rowcount > 0

    async def export_runs_older_than(self, retention_days: int) -> list[dict[str, Any]]:
        cutoff = (datetime.now(UTC) - timedelta(days=max(0, retention_days))).isoformat()
        async with self._lock:
            conn = self._ensure_conn()
            rows = conn.execute(
                "SELECT payload_json FROM audit_runs WHERE created_at < ? ORDER BY created_at ASC",
                (cutoff,),
            ).fetchall()
        return [json.loads(r["payload_json"]) for r in rows]

    async def prune_runs_older_than(self, retention_days: int) -> int:
        cutoff = (datetime.now(UTC) - timedelta(days=max(0, retention_days))).isoformat()
        async with self._lock:
            conn = self._ensure_conn()
            cursor = conn.execute("DELETE FROM audit_runs WHERE created_at < ?", (cutoff,))
            conn.commit()
            return cursor.rowcount

    async def list_model_pricing(self) -> dict[str, dict[str, float]]:
        async with self._lock:
            conn = self._ensure_conn()
            rows = conn.execute(
                "SELECT model_name, input_per_1m_usd, output_per_1m_usd "
                "FROM model_pricing ORDER BY model_name ASC"
            ).fetchall()
        return {
            str(r["model_name"]): {
                "input": float(r["input_per_1m_usd"]),
                "output": float(r["output_per_1m_usd"]),
            }
            for r in rows
        }

    async def save_model_pricing(
        self, model_name: str, input_per_1m_usd: float, output_per_1m_usd: float
    ) -> None:
        now = datetime.now(UTC).isoformat()
        async with self._lock:
            conn = self._ensure_conn()
            conn.execute(
                """
                INSERT INTO model_pricing (
                    model_name, input_per_1m_usd, output_per_1m_usd, updated_at
                )
                VALUES (?, ?, ?, ?)
                ON CONFLICT(model_name) DO UPDATE SET
                    input_per_1m_usd=excluded.input_per_1m_usd,
                    output_per_1m_usd=excluded.output_per_1m_usd,
                    updated_at=excluded.updated_at
                """,
                (model_name, float(input_per_1m_usd), float(output_per_1m_usd), now),
            )
            conn.commit()

    async def delete_model_pricing(self, model_name: str) -> bool:
        async with self._lock:
            conn = self._ensure_conn()
            cursor = conn.execute("DELETE FROM model_pricing WHERE model_name = ?", (model_name,))
            conn.commit()
            return cursor.rowcount > 0

    async def get_setting(self, key: str) -> dict[str, Any] | None:
        async with self._lock:
            conn = self._ensure_conn()
            row = conn.execute(
                "SELECT value_json FROM gateway_settings WHERE key = ?",
                (key,),
            ).fetchone()
        return json.loads(row["value_json"]) if row is not None else None

    async def save_setting(self, key: str, value: dict[str, Any]) -> None:
        now = datetime.now(UTC).isoformat()
        payload = json.dumps(value, default=str)
        async with self._lock:
            conn = self._ensure_conn()
            conn.execute(
                """
                INSERT INTO gateway_settings (key, value_json, updated_at)
                VALUES (?, ?, ?)
                ON CONFLICT(key) DO UPDATE SET
                    value_json=excluded.value_json,
                    updated_at=excluded.updated_at
                """,
                (key, payload, now),
            )
            conn.commit()


class PostgresAuditStore:
    """Postgres-backed AuditStore for multi-replica production deployments."""

    def __init__(self, dsn: str = "", *, pool: Any = None) -> None:
        self.dsn = dsn
        self._pool = pool

    async def _get_pool(self) -> Any:
        if self._pool is None:
            try:
                import asyncpg
            except ImportError as exc:
                raise RuntimeError(
                    "postgres_dsn requires optional dependency 'mcpcodeaway[postgres]' (asyncpg)."
                ) from exc
            self._pool = await asyncpg.create_pool(self.dsn)
        return self._pool

    async def health_check(self) -> bool:
        try:
            pool = await self._get_pool()
            await pool.execute("SELECT 1")
            return True
        except Exception:
            return False

    @staticmethod
    def _decode_json(value: Any) -> dict[str, Any]:
        if isinstance(value, dict):
            return value
        return dict(json.loads(str(value)))

    async def init(self) -> None:
        pool = await self._get_pool()
        await pool.execute(
            """
            CREATE TABLE IF NOT EXISTS audit_runs (
                run_id TEXT PRIMARY KEY,
                tenant_id TEXT,
                user_sub TEXT,
                mode TEXT,
                created_at TEXT NOT NULL,
                payload_json TEXT NOT NULL
            )
            """
        )
        await pool.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_audit_runs_tenant_created
            ON audit_runs(tenant_id, created_at DESC)
            """
        )
        await pool.execute(
            """
            CREATE TABLE IF NOT EXISTS dynamic_upstreams (
                name TEXT PRIMARY KEY,
                config_json TEXT NOT NULL,
                updated_at TEXT NOT NULL
            )
            """
        )
        await pool.execute(
            """
            CREATE TABLE IF NOT EXISTS model_pricing (
                model_name TEXT PRIMARY KEY,
                input_per_1m_usd DOUBLE PRECISION NOT NULL,
                output_per_1m_usd DOUBLE PRECISION NOT NULL,
                updated_at TEXT NOT NULL
            )
            """
        )

    async def record_run(self, run: dict[str, Any]) -> None:
        pool = await self._get_pool()
        created_at = str(run.get("created_at") or datetime.now(UTC).isoformat())
        payload = json.dumps(run, default=str)
        await pool.execute(
            """
            INSERT INTO audit_runs (run_id, tenant_id, user_sub, mode, created_at, payload_json)
            VALUES ($1, $2, $3, $4, $5, $6)
            ON CONFLICT (run_id) DO UPDATE SET
                tenant_id = EXCLUDED.tenant_id,
                user_sub = EXCLUDED.user_sub,
                mode = EXCLUDED.mode,
                created_at = EXCLUDED.created_at,
                payload_json = EXCLUDED.payload_json
            """,
            str(run["run_id"]),
            run.get("tenant_id"),
            run.get("user_sub"),
            run.get("mode"),
            created_at,
            payload,
        )

    async def list_runs(
        self, limit: int = 50, tenant_id: str | None = None
    ) -> list[dict[str, Any]]:
        pool = await self._get_pool()
        if tenant_id is not None:
            rows = await pool.fetch(
                """
                SELECT payload_json FROM audit_runs
                WHERE tenant_id = $1
                ORDER BY created_at DESC
                LIMIT $2
                """,
                tenant_id,
                limit,
            )
        else:
            rows = await pool.fetch(
                """
                SELECT payload_json FROM audit_runs
                ORDER BY created_at DESC
                LIMIT $1
                """,
                limit,
            )
        return [self._decode_json(row["payload_json"]) for row in rows]

    async def query_runs(
        self,
        *,
        limit: int = 25,
        offset: int = 0,
        tenant_id: str | None = None,
        status: str = "all",
        mode: str | None = None,
        q: str | None = None,
    ) -> tuple[list[dict[str, Any]], int]:
        pool = await self._get_pool()
        clauses: list[str] = []
        args: list[Any] = []
        idx = 1
        if tenant_id is not None:
            clauses.append(f"tenant_id = ${idx}")
            args.append(tenant_id)
            idx += 1
        if mode and mode != "all":
            clauses.append(f"mode = ${idx}")
            args.append(mode)
            idx += 1
        if status == "errors":
            clauses.append("(payload_json::jsonb->>'ok') = 'false'")
        elif status == "grounding_blocked":
            clauses.append("(payload_json::jsonb->>'grounding_blocked') = 'true'")
        elif status == "ok":
            clauses.append("(payload_json::jsonb->>'ok') = 'true'")
        if q and q.strip():
            clauses.append(
                f"(run_id ILIKE ${idx} OR user_sub ILIKE ${idx} OR payload_json ILIKE ${idx})"
            )
            args.append(f"%{q.strip()}%")
            idx += 1

        where_sql = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        cnt_row = await pool.fetchrow(
            f"SELECT COUNT(*) AS cnt FROM audit_runs {where_sql}",
            *args,
        )
        total = int(cnt_row["cnt"]) if cnt_row is not None else 0
        rows = await pool.fetch(
            f"""
            SELECT payload_json FROM audit_runs
            {where_sql}
            ORDER BY created_at DESC
            LIMIT ${idx} OFFSET ${idx + 1}
            """,
            *args,
            max(1, min(limit, 200)),
            max(0, offset),
        )
        return [self._decode_json(row["payload_json"]) for row in rows], total

    async def get_run(self, run_id: str) -> dict[str, Any] | None:
        pool = await self._get_pool()
        row = await pool.fetchrow(
            "SELECT payload_json FROM audit_runs WHERE run_id = $1",
            run_id,
        )
        return self._decode_json(row["payload_json"]) if row is not None else None

    async def save_dynamic_upstream(self, name: str, config: dict[str, Any]) -> None:
        pool = await self._get_pool()
        now = datetime.now(UTC).isoformat()
        payload = json.dumps(config, default=str)
        await pool.execute(
            """
            INSERT INTO dynamic_upstreams (name, config_json, updated_at)
            VALUES ($1, $2, $3)
            ON CONFLICT (name) DO UPDATE SET
                config_json = EXCLUDED.config_json,
                updated_at = EXCLUDED.updated_at
            """,
            name,
            payload,
            now,
        )

    async def list_dynamic_upstreams(self) -> list[dict[str, Any]]:
        pool = await self._get_pool()
        rows = await pool.fetch("SELECT config_json FROM dynamic_upstreams ORDER BY name ASC")
        return [self._decode_json(row["config_json"]) for row in rows]

    async def delete_dynamic_upstream(self, name: str) -> bool:
        pool = await self._get_pool()
        status = await pool.execute(
            "DELETE FROM dynamic_upstreams WHERE name = $1",
            name,
        )
        try:
            return int(str(status).split()[-1]) > 0
        except (IndexError, ValueError):
            return bool(status)

    async def export_runs_older_than(self, retention_days: int) -> list[dict[str, Any]]:
        cutoff = (datetime.now(UTC) - timedelta(days=max(0, retention_days))).isoformat()
        pool = await self._get_pool()
        rows = await pool.fetch(
            "SELECT payload_json FROM audit_runs WHERE created_at < $1 ORDER BY created_at ASC",
            cutoff,
        )
        return [json.loads(r["payload_json"]) for r in rows]

    async def prune_runs_older_than(self, retention_days: int) -> int:
        cutoff = (datetime.now(UTC) - timedelta(days=max(0, retention_days))).isoformat()
        pool = await self._get_pool()
        status = await pool.execute(
            "DELETE FROM audit_runs WHERE created_at < $1",
            cutoff,
        )
        try:
            return int(str(status).split()[-1])
        except (IndexError, ValueError):
            return 0

    async def list_model_pricing(self) -> dict[str, dict[str, float]]:
        pool = await self._get_pool()
        rows = await pool.fetch(
            "SELECT model_name, input_per_1m_usd, output_per_1m_usd "
            "FROM model_pricing ORDER BY model_name ASC"
        )
        return {
            str(r["model_name"]): {
                "input": float(r["input_per_1m_usd"]),
                "output": float(r["output_per_1m_usd"]),
            }
            for r in rows
        }

    async def save_model_pricing(
        self, model_name: str, input_per_1m_usd: float, output_per_1m_usd: float
    ) -> None:
        pool = await self._get_pool()
        now = datetime.now(UTC).isoformat()
        await pool.execute(
            """
            INSERT INTO model_pricing (
                model_name, input_per_1m_usd, output_per_1m_usd, updated_at
            )
            VALUES ($1, $2, $3, $4)
            ON CONFLICT (model_name) DO UPDATE SET
                input_per_1m_usd = EXCLUDED.input_per_1m_usd,
                output_per_1m_usd = EXCLUDED.output_per_1m_usd,
                updated_at = EXCLUDED.updated_at
            """,
            model_name,
            float(input_per_1m_usd),
            float(output_per_1m_usd),
            now,
        )

    async def delete_model_pricing(self, model_name: str) -> bool:
        pool = await self._get_pool()
        status = await pool.execute(
            "DELETE FROM model_pricing WHERE model_name = $1",
            model_name,
        )
        try:
            return int(str(status).split()[-1]) > 0
        except (IndexError, ValueError):
            return bool(status)

    async def _ensure_settings_table(self, pool: Any) -> None:
        await pool.execute(
            """
            CREATE TABLE IF NOT EXISTS gateway_settings (
                key TEXT PRIMARY KEY,
                value_json TEXT NOT NULL,
                updated_at TEXT NOT NULL
            )
            """
        )

    async def get_setting(self, key: str) -> dict[str, Any] | None:
        pool = await self._get_pool()
        await self._ensure_settings_table(pool)
        row = await pool.fetchrow("SELECT value_json FROM gateway_settings WHERE key = $1", key)
        return self._decode_json(row["value_json"]) if row is not None else None

    async def save_setting(self, key: str, value: dict[str, Any]) -> None:
        pool = await self._get_pool()
        await self._ensure_settings_table(pool)
        now = datetime.now(UTC).isoformat()
        payload = json.dumps(value, default=str)
        await pool.execute(
            """
            INSERT INTO gateway_settings (key, value_json, updated_at)
            VALUES ($1, $2, $3)
            ON CONFLICT (key) DO UPDATE SET
                value_json = EXCLUDED.value_json,
                updated_at = EXCLUDED.updated_at
            """,
            key,
            payload,
            now,
        )

    async def close(self) -> None:
        if self._pool is not None and hasattr(self._pool, "close"):
            res = self._pool.close()
            if inspect.isawaitable(res):
                await res
