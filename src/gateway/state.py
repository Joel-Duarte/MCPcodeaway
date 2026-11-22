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
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit


def _postgres_dsn_with_secure_tls_default(dsn: str) -> str:
    """Default PostgreSQL connections to certificate and hostname verification.

    asyncpg's default ``prefer`` mode can fall back to plaintext. Require
    ``verify-full`` unless the operator explicitly selects an sslmode in the
    connection string (for example, ``disable`` for disposable local fixtures).
    """
    if not dsn.strip():
        return dsn
    if "://" in dsn:
        parsed = urlsplit(dsn)
        query = parse_qsl(parsed.query, keep_blank_values=True)
        if any(key.lower() == "sslmode" for key, _ in query):
            return dsn
        query.append(("sslmode", "verify-full"))
        return urlunsplit(parsed._replace(query=urlencode(query)))
    if any(part.lower().startswith("sslmode=") for part in dsn.split()):
        return dsn
    return f"{dsn} sslmode=verify-full"


class CacheBackend(Protocol):
    """Protocol for token caching and sliding-window rate limiting."""

    async def get(self, key: str) -> str | None: ...

    async def set(self, key: str, value: str, ttl_seconds: int) -> None: ...

    async def delete(self, key: str) -> None: ...

    async def increment_window(self, key: str, window_seconds: int) -> int: ...

    async def acquire_permit(
        self, key: str, *, capacity: int, lease_seconds: int, token: str
    ) -> bool: ...

    async def release_permit(self, key: str, token: str) -> None: ...


class InMemoryCacheBackend:
    """Zero-dependency async-safe in-memory TTL cache and sliding-window rate limiter."""

    def __init__(self) -> None:
        self._lock = asyncio.Lock()
        self._entries: dict[str, tuple[str, float]] = {}
        self._windows: dict[str, deque[float]] = {}
        self._permits: dict[str, dict[str, float]] = {}
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

    async def acquire_permit(
        self, key: str, *, capacity: int, lease_seconds: int, token: str
    ) -> bool:
        async with self._lock:
            now = time.monotonic()
            slots = self._permits.setdefault(key, {})
            for expired in [name for name, deadline in slots.items() if deadline <= now]:
                del slots[expired]
            if len(slots) >= capacity:
                return False
            slots[token] = now + max(lease_seconds, 1)
            return True

    async def release_permit(self, key: str, token: str) -> None:
        async with self._lock:
            self._permits.get(key, {}).pop(token, None)

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

            self._client = aioredis.from_url(
                self.redis_url,
                decode_responses=True,
                health_check_interval=30,
            )
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
        """Increment the sliding window and reconnect once on Redis connection loss."""
        client = self._get_client()
        now = time.time()
        cutoff = now - window_seconds
        member = f"{now}:{uuid.uuid4().hex}"

        async def increment(active_client: Any) -> int:
            if hasattr(active_client, "pipeline"):
                pipe = active_client.pipeline(transaction=True)
                pipe.zremrangebyscore(key, 0, cutoff)
                pipe.zadd(key, {member: now})
                pipe.zcard(key)
                pipe.expire(key, max(window_seconds, 1))
                results = await pipe.execute()
                return int(results[2])
            await active_client.zremrangebyscore(key, 0, cutoff)
            await active_client.zadd(key, {member: now})
            count = int(await active_client.zcard(key))
            await active_client.expire(key, max(window_seconds, 1))
            return count

        try:
            return await increment(client)
        except Exception as exc:
            try:
                from redis.exceptions import ConnectionError as RedisConnectionError
            except ImportError:
                raise exc from None
            if not isinstance(exc, RedisConnectionError):
                raise
            pool = getattr(client, "connection_pool", None)
            disconnect = getattr(pool, "disconnect", None)
            if disconnect is None:
                raise
            await disconnect(inuse_connections=True)
            return await increment(self._get_client())

    async def acquire_permit(
        self, key: str, *, capacity: int, lease_seconds: int, token: str
    ) -> bool:
        script = """
        local key = KEYS[1]
        local now = tonumber(ARGV[1])
        local expires = tonumber(ARGV[2])
        local capacity = tonumber(ARGV[3])
        local token = ARGV[4]
        redis.call('ZREMRANGEBYSCORE', key, '-inf', now)
        if redis.call('ZCARD', key) >= capacity then return 0 end
        redis.call('ZADD', key, expires, token)
        redis.call('EXPIRE', key, math.max(tonumber(ARGV[5]), 1))
        return 1
        """
        now = time.time()
        result = await self._get_client().eval(
            script, 1, key, now, now + max(lease_seconds, 1), capacity, token, lease_seconds
        )
        return int(result) == 1

    async def release_permit(self, key: str, token: str) -> None:
        await self._get_client().zrem(key, token)

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

    async def import_adaptive_evidence(self, tenant_id: str, rows: list[dict[str, Any]]) -> int: ...

    async def list_adaptive_evidence(self, tenant_id: str) -> list[dict[str, Any]]: ...

    async def list_tool_guidance(self, tenant_id: str) -> dict[str, dict[str, Any]]: ...

    async def save_tool_guidance(
        self, tenant_id: str, tool_name: str, guidance: dict[str, Any] | None
    ) -> None: ...

    async def list_runs(
        self, limit: int = 50, tenant_id: str | None = None
    ) -> list[dict[str, Any]]: ...

    async def get_run(self, run_id: str) -> dict[str, Any] | None: ...

    async def save_dynamic_upstream(self, name: str, config: dict[str, Any]) -> None: ...

    async def list_dynamic_upstreams(self) -> list[dict[str, Any]]: ...

    async def delete_dynamic_upstream(self, name: str) -> bool: ...

    async def consume_request_state_nonce(self, nonce: str, expires_at: str) -> bool: ...

    async def create_or_get_tool_approval(self, approval: dict[str, Any]) -> dict[str, Any]: ...

    async def get_tool_approval(
        self, approval_id: str, tenant_id: str
    ) -> dict[str, Any] | None: ...

    async def find_tool_approval(
        self,
        *,
        tenant_id: str,
        requester_sub: str,
        upstream_name: str,
        tool_name: str,
        argument_digest: str,
        policy_version: int,
    ) -> dict[str, Any] | None: ...

    async def list_tool_approvals(
        self, tenant_id: str, *, statuses: list[str], limit: int = 100
    ) -> list[dict[str, Any]]: ...

    async def decide_tool_approval(
        self,
        approval_id: str,
        tenant_id: str,
        approver_sub: str,
        decision: str,
        decided_at: str,
    ) -> dict[str, Any] | None: ...

    async def claim_tool_approval(
        self,
        approval_id: str,
        tenant_id: str,
        requester_sub: str,
        argument_digest: str,
        policy_version: int,
        started_at: str,
    ) -> bool: ...

    async def finish_tool_approval(
        self, approval_id: str, status: str, finished_at: str
    ) -> None: ...

    async def invalidate_tool_approvals(
        self, upstream_name: str, tool_name: str, current_policy_version: int, invalidated_at: str
    ) -> int: ...

    async def export_runs_older_than(self, retention_days: int) -> list[dict[str, Any]]: ...

    async def prune_runs_older_than(self, retention_days: int) -> int: ...

    async def set_gateway_secret(
        self, name: str, ciphertext: str, *, updated_by: str = ""
    ) -> None: ...

    async def get_gateway_secret(self, name: str) -> str | None: ...

    async def list_gateway_secrets(self) -> list[dict[str, Any]]: ...

    async def delete_gateway_secret(self, name: str) -> bool: ...

    async def set_user_credential(
        self,
        tenant_id: str,
        user_sub: str,
        upstream_name: str,
        auth_kind: str,
        ciphertext: str,
        *,
        expires_at: int | None = None,
    ) -> None: ...

    async def get_user_credential(
        self, tenant_id: str, user_sub: str, upstream_name: str
    ) -> dict[str, Any] | None: ...

    async def list_user_credentials(
        self, tenant_id: str, user_sub: str
    ) -> list[dict[str, Any]]: ...

    async def delete_user_credential(
        self, tenant_id: str, user_sub: str, upstream_name: str
    ) -> bool: ...

    async def create_or_get_operation(
        self, operation: dict[str, Any]
    ) -> tuple[dict[str, Any], bool]: ...

    async def prepare_sensitive_write_audit(
        self, operation: dict[str, Any]
    ) -> tuple[dict[str, Any], bool]: ...

    async def claim_sensitive_write_audit(self, operation_id: str, started_at: str) -> bool: ...

    async def finish_sensitive_write_audit(
        self, operation_id: str, status: str, updated_at: str
    ) -> bool: ...

    async def get_sensitive_write_audit_by_dedupe_key(
        self, dedupe_key: str
    ) -> dict[str, Any] | None: ...

    async def get_operation(self, operation_id: str) -> dict[str, Any] | None: ...

    async def get_operation_by_dedupe_key(self, dedupe_key: str) -> dict[str, Any] | None: ...

    async def list_operations(
        self, tenant_id: str, *, statuses: list[str] | None, limit: int = 100
    ) -> list[dict[str, Any]]: ...

    async def get_operation_metrics(self, tenant_id: str, *, since: str) -> dict[str, Any]: ...

    async def claim_operations(
        self, *, limit: int, now: str, lease_expires_at: str
    ) -> list[dict[str, Any]]: ...

    async def update_operation(
        self,
        operation_id: str,
        *,
        lease_token: str,
        status: str,
        attempt_count: int,
        updated_at: str,
        next_attempt_at: str | None = None,
        lease_expires_at: str | None = None,
        result_ciphertext: str | None = None,
        last_error_type: str | None = None,
        last_error: str | None = None,
        cancel_requested: bool | None = None,
        expires_at: str | None = None,
    ) -> bool: ...

    async def request_operation_cancel(
        self, operation_id: str, tenant_id: str, requester_sub: str, client_id: str | None
    ) -> bool: ...

    async def prune_operations(self, *, now: str) -> int: ...


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
        conn.execute(
            """CREATE TABLE IF NOT EXISTS adaptive_evidence (
                tenant_id TEXT NOT NULL, evidence_id TEXT NOT NULL, created_at TEXT NOT NULL,
                payload_json TEXT NOT NULL, PRIMARY KEY (tenant_id, evidence_id))"""
        )
        conn.execute(
            """CREATE TABLE IF NOT EXISTS consumed_request_state_nonces (
                nonce TEXT PRIMARY KEY, expires_at TEXT NOT NULL)"""
        )
        conn.execute(
            """CREATE TABLE IF NOT EXISTS tool_approvals (
                approval_id TEXT PRIMARY KEY,
                tenant_id TEXT NOT NULL,
                requester_sub TEXT NOT NULL,
                upstream_name TEXT NOT NULL,
                tool_name TEXT NOT NULL,
                argument_digest TEXT NOT NULL,
                policy_version INTEGER NOT NULL,
                approver_groups_json TEXT NOT NULL,
                action_summary TEXT NOT NULL,
                status TEXT NOT NULL,
                created_at TEXT NOT NULL,
                expires_at TEXT NOT NULL,
                decided_by TEXT,
                decided_at TEXT,
                execution_started_at TEXT,
                completed_at TEXT
            )"""
        )
        conn.execute(
            """CREATE UNIQUE INDEX IF NOT EXISTS idx_tool_approvals_active_action
            ON tool_approvals (tenant_id, requester_sub, upstream_name, tool_name,
                               argument_digest, policy_version)
            WHERE status IN ('pending', 'approved', 'denied')"""
        )
        conn.execute(
            """CREATE INDEX IF NOT EXISTS idx_tool_approvals_tenant_status_created
            ON tool_approvals (tenant_id, status, created_at DESC)"""
        )
        conn.execute(
            """CREATE TABLE IF NOT EXISTS tool_guidance (
                tenant_id TEXT NOT NULL, tool_name TEXT NOT NULL, payload_json TEXT NOT NULL,
                PRIMARY KEY (tenant_id, tool_name))"""
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS gateway_secrets (
                name TEXT PRIMARY KEY,
                ciphertext TEXT NOT NULL,
                updated_by TEXT NOT NULL DEFAULT '',
                updated_at TEXT NOT NULL
            )
            """
        )
        conn.execute(
            """CREATE TABLE IF NOT EXISTS recovery_operations (
                operation_id TEXT PRIMARY KEY,
                tenant_id TEXT NOT NULL,
                requester_sub TEXT NOT NULL,
                client_id TEXT NOT NULL DEFAULT '',
                upstream_name TEXT NOT NULL,
                tool_name TEXT NOT NULL,
                dedupe_key TEXT NOT NULL UNIQUE,
                request_digest TEXT NOT NULL,
                payload_ciphertext TEXT NOT NULL,
                result_ciphertext TEXT,
                status TEXT NOT NULL,
                attempt_count INTEGER NOT NULL,
                max_attempts INTEGER NOT NULL,
                next_attempt_at TEXT,
                recovery_deadline TEXT NOT NULL,
                lease_token TEXT,
                lease_expires_at TEXT,
                cancel_requested INTEGER NOT NULL DEFAULT 0,
                last_error_type TEXT,
                last_error TEXT,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                expires_at TEXT NOT NULL
            )"""
        )
        conn.execute(
            """CREATE TABLE IF NOT EXISTS sensitive_write_audits (
                operation_id TEXT PRIMARY KEY,
                tenant_id TEXT NOT NULL,
                caller_sub TEXT NOT NULL,
                upstream_name TEXT NOT NULL,
                tool_name TEXT NOT NULL,
                request_digest TEXT NOT NULL,
                correlation_id TEXT NOT NULL,
                dedupe_key TEXT NOT NULL UNIQUE,
                status TEXT NOT NULL,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            )"""
        )
        conn.execute(
            """CREATE INDEX IF NOT EXISTS idx_recovery_operations_claim
            ON recovery_operations (status, next_attempt_at, lease_expires_at, recovery_deadline)"""
        )
        conn.execute(
            """CREATE INDEX IF NOT EXISTS idx_recovery_operations_owner
            ON recovery_operations (tenant_id, requester_sub, client_id, created_at DESC)"""
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS user_upstream_credentials (
                tenant_id TEXT NOT NULL,
                user_sub TEXT NOT NULL,
                upstream_name TEXT NOT NULL,
                auth_kind TEXT NOT NULL,
                ciphertext TEXT NOT NULL,
                expires_at INTEGER,
                updated_at TEXT NOT NULL,
                PRIMARY KEY (tenant_id, user_sub, upstream_name)
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS tool_model_rollups (
                bucket_hour TEXT NOT NULL,
                tenant_id TEXT NOT NULL,
                model TEXT NOT NULL,
                server TEXT NOT NULL,
                tool TEXT NOT NULL,
                mode TEXT NOT NULL,
                runs INTEGER NOT NULL DEFAULT 0,
                ok_count INTEGER NOT NULL DEFAULT 0,
                sum_duration_ms REAL NOT NULL DEFAULT 0,
                sum_tokens INTEGER NOT NULL DEFAULT 0,
                sum_turns REAL NOT NULL DEFAULT 0,
                PRIMARY KEY (bucket_hour, tenant_id, model, server, tool, mode)
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
            self._upsert_rollup_sqlite(conn, run, created_at)
            conn.commit()

    def _upsert_rollup_sqlite(
        self, conn: sqlite3.Connection, run: dict[str, Any], created_at: str
    ) -> None:
        bucket_hour = created_at[:13] + ":00:00+00:00"
        tenant_id = str(run.get("tenant_id") or "")
        model = str(run.get("model_name") or "unknown")
        raw_mode = str(run.get("ab_variant") or run.get("mode") or "traditional")
        mode_family = (
            "codemode" if raw_mode.startswith("codemode") or raw_mode == "hybrid" else "traditional"
        )
        subcalls = run.get("subcalls") if isinstance(run.get("subcalls"), list) else []
        if run.get("tool_name") == "execute_code" and subcalls:
            targets = list(
                dict.fromkeys(
                    (
                        str(sc.get("server") or "gateway"),
                        f"{sc.get('server')}__{sc.get('tool')}"
                        if "__" not in str(sc.get("tool") or "") and sc.get("server")
                        else str(sc.get("tool") or "execute_code"),
                    )
                    for sc in subcalls
                    if isinstance(sc, dict) and sc.get("tool")
                )
            )
            if not targets:
                targets = [(str(run.get("server_name") or "gateway"), "execute_code")]
        else:
            tool = str(run.get("tool_name") or "unknown")
            server = str(
                run.get("server_name") or (tool.split("__", 1)[0] if "__" in tool else "gateway")
            )
            targets = [(server, tool)]
        ok_inc = 1 if run.get("ok") else 0
        dur = float(run.get("duration_ms") or 0.0)
        tok = int(run.get("total_tokens") or run.get("tokens_used_est") or 0)
        turns = float(run.get("turns") or 1.0)
        for server, tool in targets:
            conn.execute(
                """
                INSERT INTO tool_model_rollups
                    (bucket_hour, tenant_id, model, server, tool, mode,
                     runs, ok_count, sum_duration_ms, sum_tokens, sum_turns)
                VALUES (?, ?, ?, ?, ?, ?, 1, ?, ?, ?, ?)
                ON CONFLICT(bucket_hour, tenant_id, model, server, tool, mode) DO UPDATE SET
                    runs = runs + 1,
                    ok_count = ok_count + excluded.ok_count,
                    sum_duration_ms = sum_duration_ms + excluded.sum_duration_ms,
                    sum_tokens = sum_tokens + excluded.sum_tokens,
                    sum_turns = sum_turns + excluded.sum_turns
                """,
                (bucket_hour, tenant_id, model, server, tool, mode_family, ok_inc, dur, tok, turns),
            )

    async def query_tool_model_rollups(
        self, *, tenant_id: str | None = None, hours: int = 24
    ) -> list[dict[str, Any]]:
        cutoff = (datetime.now(UTC) - timedelta(hours=max(1, hours))).isoformat()[
            :13
        ] + ":00:00+00:00"
        async with self._lock:
            conn = self._ensure_conn()
            if tenant_id:
                cur = conn.execute(
                    """
                    SELECT model, server, tool, mode,
                           SUM(runs) AS runs,
                           SUM(ok_count) AS ok_count,
                           SUM(sum_duration_ms) AS sum_duration_ms,
                           SUM(sum_tokens) AS sum_tokens,
                           SUM(sum_turns) AS sum_turns
                    FROM tool_model_rollups
                    WHERE bucket_hour >= ? AND tenant_id = ?
                    GROUP BY model, server, tool, mode
                    ORDER BY runs DESC
                    """,
                    (cutoff, tenant_id),
                )
            else:
                cur = conn.execute(
                    """
                    SELECT model, server, tool, mode,
                           SUM(runs) AS runs,
                           SUM(ok_count) AS ok_count,
                           SUM(sum_duration_ms) AS sum_duration_ms,
                           SUM(sum_tokens) AS sum_tokens,
                           SUM(sum_turns) AS sum_turns
                    FROM tool_model_rollups
                    WHERE bucket_hour >= ?
                    GROUP BY model, server, tool, mode
                    ORDER BY runs DESC
                    """,
                    (cutoff,),
                )
            return [dict(row) for row in cur.fetchall()]

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

    async def list_tool_guidance(self, tenant_id: str) -> dict[str, dict[str, Any]]:
        async with self._lock:
            rows = (
                self._ensure_conn()
                .execute(
                    "SELECT tool_name, payload_json FROM tool_guidance WHERE tenant_id = ?",
                    (tenant_id,),
                )
                .fetchall()
            )
        return {row["tool_name"]: json.loads(row["payload_json"]) for row in rows}

    async def save_tool_guidance(
        self, tenant_id: str, tool_name: str, guidance: dict[str, Any] | None
    ) -> None:
        async with self._lock:
            conn = self._ensure_conn()
            if guidance is None:
                conn.execute(
                    "DELETE FROM tool_guidance WHERE tenant_id = ? AND tool_name = ?",
                    (tenant_id, tool_name),
                )
            else:
                conn.execute(
                    """INSERT INTO tool_guidance (tenant_id, tool_name, payload_json)
                       VALUES (?, ?, ?) ON CONFLICT (tenant_id, tool_name)
                       DO UPDATE SET payload_json = excluded.payload_json""",
                    (tenant_id, tool_name, json.dumps(guidance)),
                )
            conn.commit()

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

    async def consume_request_state_nonce(self, nonce: str, expires_at: str) -> bool:
        async with self._lock:
            conn = self._ensure_conn()
            conn.execute(
                "DELETE FROM consumed_request_state_nonces WHERE expires_at < ?",
                (datetime.now(UTC).isoformat(),),
            )
            cursor = conn.execute(
                "INSERT OR IGNORE INTO consumed_request_state_nonces "
                "(nonce, expires_at) VALUES (?, ?)",
                (nonce, expires_at),
            )
            conn.commit()
            return cursor.rowcount == 1

    @staticmethod
    def _decode_tool_approval(row: sqlite3.Row | None) -> dict[str, Any] | None:
        if row is None:
            return None
        result = dict(row)
        result["approver_groups"] = json.loads(result.pop("approver_groups_json"))
        return result

    async def create_or_get_tool_approval(self, approval: dict[str, Any]) -> dict[str, Any]:
        async with self._lock:
            conn = self._ensure_conn()
            try:
                conn.execute("BEGIN IMMEDIATE")
                conn.execute(
                    "UPDATE tool_approvals SET status='expired' WHERE status IN "
                    "('pending','approved','denied') AND expires_at<=?",
                    (approval["created_at"],),
                )
                conn.execute(
                    """INSERT OR IGNORE INTO tool_approvals (
                        approval_id, tenant_id, requester_sub, upstream_name, tool_name,
                        argument_digest, policy_version, approver_groups_json, action_summary,
                        status, created_at, expires_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'pending', ?, ?)""",
                    (
                        approval["approval_id"],
                        approval["tenant_id"],
                        approval["requester_sub"],
                        approval["upstream_name"],
                        approval["tool_name"],
                        approval["argument_digest"],
                        int(approval["policy_version"]),
                        json.dumps(approval["approver_groups"], separators=(",", ":")),
                        approval["action_summary"],
                        approval["created_at"],
                        approval["expires_at"],
                    ),
                )
                row = conn.execute(
                    """SELECT * FROM tool_approvals WHERE tenant_id=? AND requester_sub=?
                    AND upstream_name=? AND tool_name=? AND argument_digest=? AND policy_version=?
                    AND status IN ('pending', 'approved', 'denied')
                    ORDER BY created_at DESC LIMIT 1""",
                    (
                        approval["tenant_id"],
                        approval["requester_sub"],
                        approval["upstream_name"],
                        approval["tool_name"],
                        approval["argument_digest"],
                        int(approval["policy_version"]),
                    ),
                ).fetchone()
                conn.commit()
                decoded = self._decode_tool_approval(row)
                if decoded is None:
                    raise RuntimeError("Tool approval record could not be loaded after creation")
                return decoded
            except Exception:
                conn.rollback()
                raise

    async def get_tool_approval(self, approval_id: str, tenant_id: str) -> dict[str, Any] | None:
        async with self._lock:
            row = (
                self._ensure_conn()
                .execute(
                    "SELECT * FROM tool_approvals WHERE approval_id=? AND tenant_id=?",
                    (approval_id, tenant_id),
                )
                .fetchone()
            )
        return self._decode_tool_approval(row)

    async def find_tool_approval(
        self,
        *,
        tenant_id: str,
        requester_sub: str,
        upstream_name: str,
        tool_name: str,
        argument_digest: str,
        policy_version: int,
    ) -> dict[str, Any] | None:
        async with self._lock:
            conn = self._ensure_conn()
            conn.execute(
                "UPDATE tool_approvals SET status='expired' WHERE status IN "
                "('pending','approved','denied') AND expires_at<=?",
                (datetime.now(UTC).isoformat(),),
            )
            conn.commit()
            row = conn.execute(
                """SELECT * FROM tool_approvals WHERE tenant_id=? AND requester_sub=?
                AND upstream_name=? AND tool_name=? AND argument_digest=? AND policy_version=?
                AND status IN ('pending', 'approved', 'denied')
                ORDER BY created_at DESC LIMIT 1""",
                (
                    tenant_id,
                    requester_sub,
                    upstream_name,
                    tool_name,
                    argument_digest,
                    policy_version,
                ),
            ).fetchone()
        return self._decode_tool_approval(row)

    async def list_tool_approvals(
        self, tenant_id: str, *, statuses: list[str], limit: int = 100
    ) -> list[dict[str, Any]]:
        if not statuses:
            return []
        marks = ",".join("?" for _ in statuses)
        async with self._lock:
            conn = self._ensure_conn()
            conn.execute(
                "UPDATE tool_approvals SET status='expired' WHERE status IN "
                "('pending','approved','denied') AND expires_at<=?",
                (datetime.now(UTC).isoformat(),),
            )
            conn.commit()
            rows = conn.execute(
                f"SELECT * FROM tool_approvals WHERE tenant_id=? AND status IN ({marks}) "
                "ORDER BY created_at DESC LIMIT ?",
                (tenant_id, *statuses, max(1, min(limit, 200))),
            ).fetchall()
        return [self._decode_tool_approval(row) for row in rows if row is not None]

    async def decide_tool_approval(
        self, approval_id: str, tenant_id: str, approver_sub: str, decision: str, decided_at: str
    ) -> dict[str, Any] | None:
        if decision not in {"approved", "denied"}:
            raise ValueError("decision must be approved or denied")
        async with self._lock:
            conn = self._ensure_conn()
            try:
                conn.execute("BEGIN IMMEDIATE")
                cursor = conn.execute(
                    """UPDATE tool_approvals SET status=?, decided_by=?, decided_at=?
                    WHERE approval_id=? AND tenant_id=? AND requester_sub<>? AND status='pending'
                    AND expires_at>?""",
                    (
                        decision,
                        approver_sub,
                        decided_at,
                        approval_id,
                        tenant_id,
                        approver_sub,
                        decided_at,
                    ),
                )
                if cursor.rowcount != 1:
                    conn.commit()
                    return None
                row = conn.execute(
                    "SELECT * FROM tool_approvals WHERE approval_id=? AND tenant_id=?",
                    (approval_id, tenant_id),
                ).fetchone()
                conn.commit()
                return self._decode_tool_approval(row)
            except Exception:
                conn.rollback()
                raise

    async def claim_tool_approval(
        self,
        approval_id: str,
        tenant_id: str,
        requester_sub: str,
        argument_digest: str,
        policy_version: int,
        started_at: str,
    ) -> bool:
        async with self._lock:
            conn = self._ensure_conn()
            cursor = conn.execute(
                """UPDATE tool_approvals SET status='executing', execution_started_at=?
                WHERE approval_id=? AND tenant_id=? AND requester_sub=? AND argument_digest=?
                AND policy_version=? AND status='approved' AND expires_at>?""",
                (
                    started_at,
                    approval_id,
                    tenant_id,
                    requester_sub,
                    argument_digest,
                    policy_version,
                    started_at,
                ),
            )
            conn.commit()
            return cursor.rowcount == 1

    async def finish_tool_approval(self, approval_id: str, status: str, finished_at: str) -> None:
        if status not in {"completed", "execution_failed"}:
            raise ValueError("invalid tool approval completion status")
        async with self._lock:
            self._ensure_conn().execute(
                "UPDATE tool_approvals SET status=?, completed_at=? "
                "WHERE approval_id=? AND status='executing'",
                (status, finished_at, approval_id),
            )
            self._ensure_conn().commit()

    async def invalidate_tool_approvals(
        self, upstream_name: str, tool_name: str, current_policy_version: int, invalidated_at: str
    ) -> int:
        async with self._lock:
            conn = self._ensure_conn()
            cursor = conn.execute(
                """UPDATE tool_approvals SET status='invalidated', completed_at=?
                WHERE upstream_name=? AND tool_name=? AND policy_version<>?
                AND status IN ('pending','approved','denied')""",
                (invalidated_at, upstream_name, tool_name, current_policy_version),
            )
            conn.commit()
            return cursor.rowcount

    async def import_adaptive_evidence(self, tenant_id: str, rows: list[dict[str, Any]]) -> int:
        async with self._lock:
            conn = self._ensure_conn()
            try:
                conn.execute("BEGIN IMMEDIATE")
                inserted = 0
                for row in rows:
                    evidence_id = str(row["evidence_id"])
                    cur = conn.execute(
                        "INSERT OR IGNORE INTO adaptive_evidence VALUES (?, ?, ?, ?)",
                        (
                            tenant_id,
                            evidence_id,
                            str(row["created_at"]),
                            json.dumps(row, default=str),
                        ),
                    )
                    inserted += cur.rowcount
                conn.commit()
                return inserted
            except Exception:
                conn.rollback()
                raise

    async def list_adaptive_evidence(self, tenant_id: str) -> list[dict[str, Any]]:
        async with self._lock:
            rows = (
                self._ensure_conn()
                .execute(
                    "SELECT payload_json FROM adaptive_evidence "
                    "WHERE tenant_id=? ORDER BY created_at DESC",
                    (tenant_id,),
                )
                .fetchall()
            )
        return [json.loads(r[0]) for r in rows]

    async def set_gateway_secret(self, name: str, ciphertext: str, *, updated_by: str = "") -> None:
        now = datetime.now(UTC).isoformat()
        async with self._lock:
            conn = self._ensure_conn()
            conn.execute(
                """
                INSERT INTO gateway_secrets (name, ciphertext, updated_by, updated_at)
                VALUES (?, ?, ?, ?)
                ON CONFLICT(name) DO UPDATE SET
                    ciphertext=excluded.ciphertext,
                    updated_by=excluded.updated_by,
                    updated_at=excluded.updated_at
                """,
                (name, ciphertext, updated_by, now),
            )
            conn.commit()

    async def get_gateway_secret(self, name: str) -> str | None:
        async with self._lock:
            conn = self._ensure_conn()
            row = conn.execute(
                "SELECT ciphertext FROM gateway_secrets WHERE name = ?",
                (name,),
            ).fetchone()
        return str(row["ciphertext"]) if row is not None else None

    async def list_gateway_secrets(self) -> list[dict[str, Any]]:
        async with self._lock:
            conn = self._ensure_conn()
            rows = conn.execute(
                "SELECT name, updated_by, updated_at FROM gateway_secrets ORDER BY name ASC"
            ).fetchall()
        return [
            {
                "name": str(r["name"]),
                "updated_by": str(r["updated_by"]),
                "updated_at": str(r["updated_at"]),
            }
            for r in rows
        ]

    async def delete_gateway_secret(self, name: str) -> bool:
        async with self._lock:
            conn = self._ensure_conn()
            cursor = conn.execute("DELETE FROM gateway_secrets WHERE name = ?", (name,))
            conn.commit()
            return cursor.rowcount > 0

    async def set_user_credential(
        self,
        tenant_id: str,
        user_sub: str,
        upstream_name: str,
        auth_kind: str,
        ciphertext: str,
        *,
        expires_at: int | None = None,
    ) -> None:
        now = datetime.now(UTC).isoformat()
        async with self._lock:
            conn = self._ensure_conn()
            conn.execute(
                """
                INSERT INTO user_upstream_credentials (
                    tenant_id, user_sub, upstream_name, auth_kind,
                    ciphertext, expires_at, updated_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(tenant_id, user_sub, upstream_name) DO UPDATE SET
                    auth_kind=excluded.auth_kind,
                    ciphertext=excluded.ciphertext,
                    expires_at=excluded.expires_at,
                    updated_at=excluded.updated_at
                """,
                (tenant_id, user_sub, upstream_name, auth_kind, ciphertext, expires_at, now),
            )
            conn.commit()

    async def get_user_credential(
        self, tenant_id: str, user_sub: str, upstream_name: str
    ) -> dict[str, Any] | None:
        async with self._lock:
            conn = self._ensure_conn()
            row = conn.execute(
                """
                SELECT auth_kind, ciphertext, expires_at, updated_at
                FROM user_upstream_credentials
                WHERE tenant_id = ? AND user_sub = ? AND upstream_name = ?
                """,
                (tenant_id, user_sub, upstream_name),
            ).fetchone()
        if row is None:
            return None
        return {
            "auth_kind": str(row["auth_kind"]),
            "ciphertext": str(row["ciphertext"]),
            "expires_at": int(row["expires_at"]) if row["expires_at"] is not None else None,
            "updated_at": str(row["updated_at"]),
        }

    async def list_user_credentials(self, tenant_id: str, user_sub: str) -> list[dict[str, Any]]:
        async with self._lock:
            conn = self._ensure_conn()
            rows = conn.execute(
                """
                SELECT upstream_name, auth_kind, expires_at, updated_at
                FROM user_upstream_credentials
                WHERE tenant_id = ? AND user_sub = ?
                ORDER BY upstream_name ASC
                """,
                (tenant_id, user_sub),
            ).fetchall()
        return [
            {
                "upstream_name": str(r["upstream_name"]),
                "auth_kind": str(r["auth_kind"]),
                "expires_at": int(r["expires_at"]) if r["expires_at"] is not None else None,
                "updated_at": str(r["updated_at"]),
            }
            for r in rows
        ]

    async def delete_user_credential(
        self, tenant_id: str, user_sub: str, upstream_name: str
    ) -> bool:
        async with self._lock:
            conn = self._ensure_conn()
            cursor = conn.execute(
                """
                DELETE FROM user_upstream_credentials
                WHERE tenant_id = ? AND user_sub = ? AND upstream_name = ?
                """,
                (tenant_id, user_sub, upstream_name),
            )
            conn.commit()
            return cursor.rowcount > 0

    @staticmethod
    def _decode_operation(row: sqlite3.Row | None) -> dict[str, Any] | None:
        if row is None:
            return None
        operation = dict(row)
        operation["cancel_requested"] = bool(operation.get("cancel_requested"))
        return operation

    async def create_or_get_operation(
        self, operation: dict[str, Any]
    ) -> tuple[dict[str, Any], bool]:
        async with self._lock:
            conn = self._ensure_conn()
            cursor = conn.execute(
                """INSERT OR IGNORE INTO recovery_operations (
                    operation_id, tenant_id, requester_sub, client_id, upstream_name,
                    tool_name, dedupe_key, request_digest, payload_ciphertext,
                    result_ciphertext, status, attempt_count, max_attempts,
                    next_attempt_at, recovery_deadline, lease_token, lease_expires_at,
                    cancel_requested, last_error_type, last_error, created_at,
                    updated_at, expires_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    operation["operation_id"],
                    operation["tenant_id"],
                    operation["requester_sub"],
                    operation.get("client_id") or "",
                    operation["upstream_name"],
                    operation["tool_name"],
                    operation["dedupe_key"],
                    operation["request_digest"],
                    operation["payload_ciphertext"],
                    operation.get("result_ciphertext"),
                    operation["status"],
                    operation["attempt_count"],
                    operation["max_attempts"],
                    operation.get("next_attempt_at"),
                    operation["recovery_deadline"],
                    operation.get("lease_token"),
                    operation.get("lease_expires_at"),
                    int(bool(operation.get("cancel_requested", False))),
                    operation.get("last_error_type"),
                    operation.get("last_error"),
                    operation["created_at"],
                    operation["updated_at"],
                    operation["expires_at"],
                ),
            )
            conn.commit()
            row = conn.execute(
                "SELECT * FROM recovery_operations WHERE dedupe_key = ?",
                (operation["dedupe_key"],),
            ).fetchone()
        decoded = self._decode_operation(row)
        if decoded is None:
            raise RuntimeError("Operation insert did not produce a durable row")
        return decoded, cursor.rowcount > 0

    async def prepare_sensitive_write_audit(
        self, operation: dict[str, Any]
    ) -> tuple[dict[str, Any], bool]:
        async with self._lock:
            conn = self._ensure_conn()
            cursor = conn.execute(
                """INSERT OR IGNORE INTO sensitive_write_audits (
                    operation_id, tenant_id, caller_sub, upstream_name, tool_name,
                    request_digest, correlation_id, dedupe_key, status, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'prepared', ?, ?)""",
                (
                    operation["operation_id"],
                    operation["tenant_id"],
                    operation["caller_sub"],
                    operation["upstream_name"],
                    operation["tool_name"],
                    operation["request_digest"],
                    operation["correlation_id"],
                    operation["dedupe_key"],
                    operation["created_at"],
                    operation["created_at"],
                ),
            )
            conn.commit()
            row = conn.execute(
                "SELECT * FROM sensitive_write_audits WHERE dedupe_key=?",
                (operation["dedupe_key"],),
            ).fetchone()
        if row is None:
            raise RuntimeError("Sensitive write audit preparation was not durable")
        return dict(row), cursor.rowcount > 0

    async def claim_sensitive_write_audit(self, operation_id: str, started_at: str) -> bool:
        async with self._lock:
            conn = self._ensure_conn()
            cursor = conn.execute(
                "UPDATE sensitive_write_audits SET status='executing', updated_at=? "
                "WHERE operation_id=? AND status='prepared'",
                (started_at, operation_id),
            )
            conn.commit()
        return cursor.rowcount == 1

    async def finish_sensitive_write_audit(
        self, operation_id: str, status: str, updated_at: str
    ) -> bool:
        if status not in {"succeeded", "failed", "outcome_unknown"}:
            raise ValueError("invalid sensitive write audit status")
        async with self._lock:
            conn = self._ensure_conn()
            cursor = conn.execute(
                "UPDATE sensitive_write_audits SET status=?, updated_at=? "
                "WHERE operation_id=? AND status='executing'",
                (status, updated_at, operation_id),
            )
            conn.commit()
        return cursor.rowcount == 1

    async def get_sensitive_write_audit_by_dedupe_key(
        self, dedupe_key: str
    ) -> dict[str, Any] | None:
        async with self._lock:
            row = (
                self._ensure_conn()
                .execute("SELECT * FROM sensitive_write_audits WHERE dedupe_key=?", (dedupe_key,))
                .fetchone()
            )
        return dict(row) if row is not None else None

    async def get_operation(self, operation_id: str) -> dict[str, Any] | None:
        async with self._lock:
            row = (
                self._ensure_conn()
                .execute(
                    "SELECT * FROM recovery_operations WHERE operation_id = ?", (operation_id,)
                )
                .fetchone()
            )
        return self._decode_operation(row)

    async def get_operation_by_dedupe_key(self, dedupe_key: str) -> dict[str, Any] | None:
        async with self._lock:
            row = (
                self._ensure_conn()
                .execute("SELECT * FROM recovery_operations WHERE dedupe_key = ?", (dedupe_key,))
                .fetchone()
            )
        return self._decode_operation(row)

    async def list_operations(
        self, tenant_id: str, *, statuses: list[str] | None, limit: int = 100
    ) -> list[dict[str, Any]]:
        marks = ",".join("?" for _ in statuses) if statuses else None
        async with self._lock:
            query = "SELECT * FROM recovery_operations WHERE tenant_id = ?"
            args: tuple[Any, ...] = (tenant_id,)
            if marks is not None:
                query += f" AND status IN ({marks})"
                args += tuple(statuses or [])
            query += " ORDER BY created_at DESC LIMIT ?"
            args += (max(1, min(limit, 200)),)
            rows = self._ensure_conn().execute(query, args).fetchall()
        return [decoded for row in rows if (decoded := self._decode_operation(row)) is not None]

    async def get_operation_metrics(self, tenant_id: str, *, since: str) -> dict[str, Any]:
        async with self._lock:
            conn = self._ensure_conn()
            counts = conn.execute(
                """SELECT status, COUNT(*) AS count, COALESCE(SUM(attempt_count), 0) AS attempts
                FROM recovery_operations WHERE tenant_id=? GROUP BY status""",
                (tenant_id,),
            ).fetchall()
            recent = conn.execute(
                """SELECT status, COUNT(*) AS count FROM recovery_operations
                WHERE tenant_id=? AND updated_at>=? GROUP BY status""",
                (tenant_id, since),
            ).fetchall()
            upstream = conn.execute(
                """SELECT upstream_name, status, COUNT(*) AS count,
                COALESCE(SUM(attempt_count), 0) AS attempts FROM recovery_operations
                WHERE tenant_id=? AND updated_at>=? GROUP BY upstream_name, status
                ORDER BY upstream_name, status""",
                (tenant_id, since),
            ).fetchall()
            oldest_open = conn.execute(
                """SELECT MIN(created_at) AS oldest_open_at FROM recovery_operations
                WHERE tenant_id=? AND status IN ('pending', 'working')""",
                (tenant_id,),
            ).fetchone()
        return {
            "by_status": {str(row["status"]): int(row["count"]) for row in counts},
            "attempts": sum(int(row["attempts"]) for row in counts),
            "recent_by_status": {str(row["status"]): int(row["count"]) for row in recent},
            "oldest_open_at": oldest_open["oldest_open_at"] if oldest_open else None,
            "by_upstream_24h": [
                {
                    "upstream_name": row["upstream_name"],
                    "status": row["status"],
                    "count": int(row["count"]),
                    "attempts": int(row["attempts"]),
                }
                for row in upstream
            ],
        }

    async def claim_operations(
        self, *, limit: int, now: str, lease_expires_at: str
    ) -> list[dict[str, Any]]:
        async with self._lock:
            conn = self._ensure_conn()
            candidates = conn.execute(
                """SELECT operation_id FROM recovery_operations
                WHERE status IN ('pending', 'working')
                  AND (status = 'pending' OR lease_expires_at IS NULL OR lease_expires_at <= ?)
                  AND (next_attempt_at IS NULL OR next_attempt_at <= ?)
                ORDER BY COALESCE(next_attempt_at, created_at), created_at
                LIMIT ?""",
                (now, now, max(1, min(limit, 100))),
            ).fetchall()
            claimed: list[dict[str, Any]] = []
            for candidate in candidates:
                operation_id = str(candidate["operation_id"])
                lease_token = uuid.uuid4().hex
                updated = conn.execute(
                    """UPDATE recovery_operations SET status='working', lease_token=?,
                    lease_expires_at=?, updated_at=? WHERE operation_id=?
                    AND (status='pending' OR lease_expires_at IS NULL OR lease_expires_at<=?)""",
                    (lease_token, lease_expires_at, now, operation_id, now),
                )
                if updated.rowcount:
                    row = conn.execute(
                        "SELECT * FROM recovery_operations WHERE operation_id = ?",
                        (operation_id,),
                    ).fetchone()
                    decoded = self._decode_operation(row)
                    if decoded is not None:
                        claimed.append(decoded)
            conn.commit()
        return claimed

    async def update_operation(
        self,
        operation_id: str,
        *,
        lease_token: str,
        status: str,
        attempt_count: int,
        updated_at: str,
        next_attempt_at: str | None = None,
        lease_expires_at: str | None = None,
        result_ciphertext: str | None = None,
        last_error_type: str | None = None,
        last_error: str | None = None,
        cancel_requested: bool | None = None,
        expires_at: str | None = None,
    ) -> bool:
        if status not in {
            "working",
            "pending",
            "completed",
            "failed",
            "needs_attention",
            "cancelled",
        }:
            raise ValueError("Invalid recovery operation status")
        async with self._lock:
            conn = self._ensure_conn()
            cursor = conn.execute(
                """UPDATE recovery_operations SET status=?, attempt_count=?,
                next_attempt_at=?, result_ciphertext=COALESCE(?, result_ciphertext),
                lease_token=?, lease_expires_at=?, last_error_type=?, last_error=?,
                cancel_requested=COALESCE(?, cancel_requested), updated_at=?,
                expires_at=COALESCE(?, expires_at)
                WHERE operation_id=? AND lease_token=?""",
                (
                    status,
                    attempt_count,
                    next_attempt_at,
                    result_ciphertext,
                    lease_token if status == "working" else None,
                    lease_expires_at if status == "working" else None,
                    last_error_type,
                    last_error,
                    None if cancel_requested is None else int(cancel_requested),
                    updated_at,
                    expires_at,
                    operation_id,
                    lease_token,
                ),
            )
            conn.commit()
            return cursor.rowcount > 0

    async def request_operation_cancel(
        self, operation_id: str, tenant_id: str, requester_sub: str, client_id: str | None
    ) -> bool:
        async with self._lock:
            conn = self._ensure_conn()
            cursor = conn.execute(
                """UPDATE recovery_operations SET cancel_requested=1, updated_at=?
                WHERE operation_id=? AND tenant_id=? AND requester_sub=? AND client_id=?
                AND status IN ('pending', 'working')""",
                (
                    datetime.now(UTC).isoformat(),
                    operation_id,
                    tenant_id,
                    requester_sub,
                    client_id or "",
                ),
            )
            conn.commit()
            return cursor.rowcount > 0

    async def prune_operations(self, *, now: str) -> int:
        terminal = ("completed", "failed", "needs_attention", "cancelled")
        async with self._lock:
            conn = self._ensure_conn()
            cursor = conn.execute(
                "DELETE FROM recovery_operations WHERE status IN (?, ?, ?, ?) AND expires_at <= ?",
                (*terminal, now),
            )
            conn.commit()
            return cursor.rowcount


class PostgresAuditStore:
    """Postgres-backed AuditStore for multi-replica production deployments."""

    def __init__(self, dsn: str = "", *, pool: Any = None) -> None:
        self.dsn = _postgres_dsn_with_secure_tls_default(dsn)
        self._pool = pool

    async def _get_pool(self) -> Any:
        if self._pool is None:
            try:
                import asyncpg
            except ImportError as exc:
                raise RuntimeError(
                    "postgres_dsn requires optional dependency 'mcpcodeaway[postgres]' (asyncpg)."
                ) from exc
            self._pool = await asyncpg.create_pool(
                self.dsn,
                min_size=1,
                max_size=10,
                timeout=10.0,
                command_timeout=30.0,
            )
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
        async with pool.acquire() as conn:
            from gateway.migrations import verify_schema

            await verify_schema(conn)

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

    async def list_tool_guidance(self, tenant_id: str) -> dict[str, dict[str, Any]]:
        pool = await self._get_pool()
        rows = await pool.fetch(
            "SELECT tool_name, payload_json FROM tool_guidance WHERE tenant_id = $1", tenant_id
        )
        return {row["tool_name"]: self._decode_json(row["payload_json"]) for row in rows}

    async def save_tool_guidance(
        self, tenant_id: str, tool_name: str, guidance: dict[str, Any] | None
    ) -> None:
        pool = await self._get_pool()
        if guidance is None:
            await pool.execute(
                "DELETE FROM tool_guidance WHERE tenant_id = $1 AND tool_name = $2",
                tenant_id,
                tool_name,
            )
        else:
            await pool.execute(
                """INSERT INTO tool_guidance (tenant_id, tool_name, payload_json)
                   VALUES ($1, $2, $3) ON CONFLICT (tenant_id, tool_name)
                   DO UPDATE SET payload_json = excluded.payload_json""",
                tenant_id,
                tool_name,
                json.dumps(guidance),
            )

    async def get_setting(self, key: str) -> dict[str, Any] | None:
        pool = await self._get_pool()
        row = await pool.fetchrow("SELECT value_json FROM gateway_settings WHERE key = $1", key)
        return self._decode_json(row["value_json"]) if row is not None else None

    async def save_setting(self, key: str, value: dict[str, Any]) -> None:
        pool = await self._get_pool()
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

    async def consume_request_state_nonce(self, nonce: str, expires_at: str) -> bool:
        pool = await self._get_pool()
        async with pool.acquire() as conn:
            async with conn.transaction():
                await conn.execute(
                    "DELETE FROM consumed_request_state_nonces WHERE expires_at < $1",
                    datetime.now(UTC).isoformat(),
                )
                status = await conn.execute(
                    "INSERT INTO consumed_request_state_nonces (nonce, expires_at) VALUES ($1, $2) "
                    "ON CONFLICT (nonce) DO NOTHING",
                    nonce,
                    expires_at,
                )
        return status.endswith(" 1")

    @staticmethod
    def _decode_tool_approval(row: Any) -> dict[str, Any] | None:
        if row is None:
            return None
        result = dict(row)
        result["approver_groups"] = json.loads(result.pop("approver_groups_json"))
        return result

    async def create_or_get_tool_approval(self, approval: dict[str, Any]) -> dict[str, Any]:
        pool = await self._get_pool()
        async with pool.acquire() as conn:
            async with conn.transaction():
                await conn.execute(
                    "UPDATE tool_approvals SET status='expired' WHERE status IN "
                    "('pending','approved','denied') AND expires_at<=$1",
                    approval["created_at"],
                )
                await conn.execute(
                    """INSERT INTO tool_approvals (
                        approval_id, tenant_id, requester_sub, upstream_name, tool_name,
                        argument_digest, policy_version, approver_groups_json, action_summary,
                        status, created_at, expires_at
                    ) VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,'pending',$10,$11)
                    ON CONFLICT DO NOTHING""",
                    approval["approval_id"],
                    approval["tenant_id"],
                    approval["requester_sub"],
                    approval["upstream_name"],
                    approval["tool_name"],
                    approval["argument_digest"],
                    int(approval["policy_version"]),
                    json.dumps(approval["approver_groups"], separators=(",", ":")),
                    approval["action_summary"],
                    approval["created_at"],
                    approval["expires_at"],
                )
                row = await conn.fetchrow(
                    """SELECT * FROM tool_approvals WHERE tenant_id=$1 AND requester_sub=$2
                    AND upstream_name=$3 AND tool_name=$4 AND argument_digest=$5
                    AND policy_version=$6 AND status IN ('pending','approved','denied')
                    ORDER BY created_at DESC LIMIT 1""",
                    approval["tenant_id"],
                    approval["requester_sub"],
                    approval["upstream_name"],
                    approval["tool_name"],
                    approval["argument_digest"],
                    int(approval["policy_version"]),
                )
        decoded = self._decode_tool_approval(row)
        if decoded is None:
            raise RuntimeError("Tool approval record could not be loaded after creation")
        return decoded

    async def get_tool_approval(self, approval_id: str, tenant_id: str) -> dict[str, Any] | None:
        pool = await self._get_pool()
        row = await pool.fetchrow(
            "SELECT * FROM tool_approvals WHERE approval_id=$1 AND tenant_id=$2",
            approval_id,
            tenant_id,
        )
        return self._decode_tool_approval(row)

    async def find_tool_approval(
        self,
        *,
        tenant_id: str,
        requester_sub: str,
        upstream_name: str,
        tool_name: str,
        argument_digest: str,
        policy_version: int,
    ) -> dict[str, Any] | None:
        pool = await self._get_pool()
        await pool.execute(
            "UPDATE tool_approvals SET status='expired' WHERE status IN "
            "('pending','approved','denied') AND expires_at<=$1",
            datetime.now(UTC).isoformat(),
        )
        row = await pool.fetchrow(
            """SELECT * FROM tool_approvals WHERE tenant_id=$1 AND requester_sub=$2
            AND upstream_name=$3 AND tool_name=$4 AND argument_digest=$5 AND policy_version=$6
            AND status IN ('pending','approved','denied') ORDER BY created_at DESC LIMIT 1""",
            tenant_id,
            requester_sub,
            upstream_name,
            tool_name,
            argument_digest,
            policy_version,
        )
        return self._decode_tool_approval(row)

    async def list_tool_approvals(
        self, tenant_id: str, *, statuses: list[str], limit: int = 100
    ) -> list[dict[str, Any]]:
        if not statuses:
            return []
        pool = await self._get_pool()
        await pool.execute(
            "UPDATE tool_approvals SET status='expired' WHERE status IN "
            "('pending','approved','denied') AND expires_at<=$1",
            datetime.now(UTC).isoformat(),
        )
        rows = await pool.fetch(
            "SELECT * FROM tool_approvals WHERE tenant_id=$1 AND status=ANY($2::text[]) "
            "ORDER BY created_at DESC LIMIT $3",
            tenant_id,
            statuses,
            max(1, min(limit, 200)),
        )
        return [self._decode_tool_approval(row) for row in rows]

    async def decide_tool_approval(
        self, approval_id: str, tenant_id: str, approver_sub: str, decision: str, decided_at: str
    ) -> dict[str, Any] | None:
        if decision not in {"approved", "denied"}:
            raise ValueError("decision must be approved or denied")
        pool = await self._get_pool()
        async with pool.acquire() as conn:
            async with conn.transaction():
                updated = await conn.execute(
                    """UPDATE tool_approvals SET status=$1, decided_by=$2, decided_at=$3
                    WHERE approval_id=$4 AND tenant_id=$5 AND requester_sub<>$2
                    AND status='pending' AND expires_at>$3""",
                    decision,
                    approver_sub,
                    decided_at,
                    approval_id,
                    tenant_id,
                )
                if not updated.endswith(" 1"):
                    return None
                row = await conn.fetchrow(
                    "SELECT * FROM tool_approvals WHERE approval_id=$1 AND tenant_id=$2",
                    approval_id,
                    tenant_id,
                )
        return self._decode_tool_approval(row)

    async def claim_tool_approval(
        self,
        approval_id: str,
        tenant_id: str,
        requester_sub: str,
        argument_digest: str,
        policy_version: int,
        started_at: str,
    ) -> bool:
        pool = await self._get_pool()
        status = await pool.execute(
            """UPDATE tool_approvals SET status='executing', execution_started_at=$1
            WHERE approval_id=$2 AND tenant_id=$3 AND requester_sub=$4 AND argument_digest=$5
            AND policy_version=$6 AND status='approved' AND expires_at>$1""",
            started_at,
            approval_id,
            tenant_id,
            requester_sub,
            argument_digest,
            policy_version,
        )
        return status.endswith(" 1")

    async def finish_tool_approval(self, approval_id: str, status: str, finished_at: str) -> None:
        if status not in {"completed", "execution_failed"}:
            raise ValueError("invalid tool approval completion status")
        pool = await self._get_pool()
        await pool.execute(
            "UPDATE tool_approvals SET status=$1, completed_at=$2 "
            "WHERE approval_id=$3 AND status='executing'",
            status,
            finished_at,
            approval_id,
        )

    async def invalidate_tool_approvals(
        self, upstream_name: str, tool_name: str, current_policy_version: int, invalidated_at: str
    ) -> int:
        pool = await self._get_pool()
        status = await pool.execute(
            """UPDATE tool_approvals SET status='invalidated', completed_at=$1
            WHERE upstream_name=$2 AND tool_name=$3 AND policy_version<>$4
            AND status IN ('pending','approved','denied')""",
            invalidated_at,
            upstream_name,
            tool_name,
            current_policy_version,
        )
        return int(status.split()[-1]) if status.split()[-1].isdigit() else 0

    async def import_adaptive_evidence(self, tenant_id: str, rows: list[dict[str, Any]]) -> int:
        pool = await self._get_pool()
        async with pool.acquire() as conn:
            async with conn.transaction():
                count = 0
                for row in rows:
                    status = await conn.execute(
                        "INSERT INTO adaptive_evidence VALUES ($1,$2,$3,$4) ON CONFLICT DO NOTHING",
                        tenant_id,
                        str(row["evidence_id"]),
                        str(row["created_at"]),
                        json.dumps(row),
                    )
                    count += int(str(status).split()[-1])
        return count

    async def list_adaptive_evidence(self, tenant_id: str) -> list[dict[str, Any]]:
        pool = await self._get_pool()
        rows = await pool.fetch(
            "SELECT payload_json FROM adaptive_evidence "
            "WHERE tenant_id=$1 ORDER BY created_at DESC",
            tenant_id,
        )
        return [self._decode_json(r["payload_json"]) for r in rows]

    async def set_gateway_secret(self, name: str, ciphertext: str, *, updated_by: str = "") -> None:
        pool = await self._get_pool()
        now = datetime.now(UTC).isoformat()
        await pool.execute(
            """
            INSERT INTO gateway_secrets (name, ciphertext, updated_by, updated_at)
            VALUES ($1, $2, $3, $4)
            ON CONFLICT (name) DO UPDATE SET
                ciphertext = EXCLUDED.ciphertext,
                updated_by = EXCLUDED.updated_by,
                updated_at = EXCLUDED.updated_at
            """,
            name,
            ciphertext,
            updated_by,
            now,
        )

    async def get_gateway_secret(self, name: str) -> str | None:
        pool = await self._get_pool()
        row = await pool.fetchrow("SELECT ciphertext FROM gateway_secrets WHERE name = $1", name)
        return str(row["ciphertext"]) if row is not None else None

    async def list_gateway_secrets(self) -> list[dict[str, Any]]:
        pool = await self._get_pool()
        rows = await pool.fetch(
            "SELECT name, updated_by, updated_at FROM gateway_secrets ORDER BY name ASC"
        )
        return [
            {
                "name": str(r["name"]),
                "updated_by": str(r["updated_by"]),
                "updated_at": str(r["updated_at"]),
            }
            for r in rows
        ]

    async def delete_gateway_secret(self, name: str) -> bool:
        pool = await self._get_pool()
        res = await pool.execute("DELETE FROM gateway_secrets WHERE name = $1", name)
        return str(res).endswith("1")

    async def set_user_credential(
        self,
        tenant_id: str,
        user_sub: str,
        upstream_name: str,
        auth_kind: str,
        ciphertext: str,
        *,
        expires_at: int | None = None,
    ) -> None:
        pool = await self._get_pool()
        now = datetime.now(UTC).isoformat()
        await pool.execute(
            """
            INSERT INTO user_upstream_credentials (
                tenant_id, user_sub, upstream_name, auth_kind, ciphertext, expires_at, updated_at
            )
            VALUES ($1, $2, $3, $4, $5, $6, $7)
            ON CONFLICT (tenant_id, user_sub, upstream_name) DO UPDATE SET
                auth_kind = EXCLUDED.auth_kind,
                ciphertext = EXCLUDED.ciphertext,
                expires_at = EXCLUDED.expires_at,
                updated_at = EXCLUDED.updated_at
            """,
            tenant_id,
            user_sub,
            upstream_name,
            auth_kind,
            ciphertext,
            expires_at,
            now,
        )

    async def get_user_credential(
        self, tenant_id: str, user_sub: str, upstream_name: str
    ) -> dict[str, Any] | None:
        pool = await self._get_pool()
        row = await pool.fetchrow(
            """
            SELECT auth_kind, ciphertext, expires_at, updated_at
            FROM user_upstream_credentials
            WHERE tenant_id = $1 AND user_sub = $2 AND upstream_name = $3
            """,
            tenant_id,
            user_sub,
            upstream_name,
        )
        if row is None:
            return None
        return {
            "auth_kind": str(row["auth_kind"]),
            "ciphertext": str(row["ciphertext"]),
            "expires_at": int(row["expires_at"]) if row["expires_at"] is not None else None,
            "updated_at": str(row["updated_at"]),
        }

    async def list_user_credentials(self, tenant_id: str, user_sub: str) -> list[dict[str, Any]]:
        pool = await self._get_pool()
        rows = await pool.fetch(
            """
            SELECT upstream_name, auth_kind, expires_at, updated_at
            FROM user_upstream_credentials
            WHERE tenant_id = $1 AND user_sub = $2
            ORDER BY upstream_name ASC
            """,
            tenant_id,
            user_sub,
        )
        return [
            {
                "upstream_name": str(r["upstream_name"]),
                "auth_kind": str(r["auth_kind"]),
                "expires_at": int(r["expires_at"]) if r["expires_at"] is not None else None,
                "updated_at": str(r["updated_at"]),
            }
            for r in rows
        ]

    async def delete_user_credential(
        self, tenant_id: str, user_sub: str, upstream_name: str
    ) -> bool:
        pool = await self._get_pool()
        res = await pool.execute(
            """
            DELETE FROM user_upstream_credentials
            WHERE tenant_id = $1 AND user_sub = $2 AND upstream_name = $3
            """,
            tenant_id,
            user_sub,
            upstream_name,
        )
        return str(res).endswith("1")

    @staticmethod
    def _decode_operation(row: Any) -> dict[str, Any] | None:
        if row is None:
            return None
        return dict(row)

    async def create_or_get_operation(
        self, operation: dict[str, Any]
    ) -> tuple[dict[str, Any], bool]:
        pool = await self._get_pool()
        async with pool.acquire() as conn:
            inserted = await conn.fetchrow(
                """INSERT INTO recovery_operations (
                    operation_id, tenant_id, requester_sub, client_id, upstream_name,
                    tool_name, dedupe_key, request_digest, payload_ciphertext,
                    result_ciphertext, status, attempt_count, max_attempts,
                    next_attempt_at, recovery_deadline, lease_token, lease_expires_at,
                    cancel_requested, last_error_type, last_error, created_at,
                    updated_at, expires_at
                ) VALUES (
                    $1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12,$13,$14,$15,$16,$17,
                    $18,$19,$20,$21,$22,$23
                ) ON CONFLICT (dedupe_key) DO NOTHING RETURNING *""",
                operation["operation_id"],
                operation["tenant_id"],
                operation["requester_sub"],
                operation.get("client_id") or "",
                operation["upstream_name"],
                operation["tool_name"],
                operation["dedupe_key"],
                operation["request_digest"],
                operation["payload_ciphertext"],
                operation.get("result_ciphertext"),
                operation["status"],
                operation["attempt_count"],
                operation["max_attempts"],
                operation.get("next_attempt_at"),
                operation["recovery_deadline"],
                operation.get("lease_token"),
                operation.get("lease_expires_at"),
                bool(operation.get("cancel_requested", False)),
                operation.get("last_error_type"),
                operation.get("last_error"),
                operation["created_at"],
                operation["updated_at"],
                operation["expires_at"],
            )
            row = inserted or await conn.fetchrow(
                "SELECT * FROM recovery_operations WHERE dedupe_key=$1",
                operation["dedupe_key"],
            )
        decoded = self._decode_operation(row)
        if decoded is None:
            raise RuntimeError("Operation insert did not produce a durable row")
        return decoded, inserted is not None

    async def prepare_sensitive_write_audit(
        self, operation: dict[str, Any]
    ) -> tuple[dict[str, Any], bool]:
        pool = await self._get_pool()
        inserted = await pool.fetchrow(
            """INSERT INTO sensitive_write_audits (
                operation_id, tenant_id, caller_sub, upstream_name, tool_name,
                request_digest, correlation_id, dedupe_key, status, created_at, updated_at
            ) VALUES ($1,$2,$3,$4,$5,$6,$7,$8,'prepared',$9,$9)
            ON CONFLICT (dedupe_key) DO NOTHING RETURNING *""",
            operation["operation_id"],
            operation["tenant_id"],
            operation["caller_sub"],
            operation["upstream_name"],
            operation["tool_name"],
            operation["request_digest"],
            operation["correlation_id"],
            operation["dedupe_key"],
            operation["created_at"],
        )
        row = inserted or await pool.fetchrow(
            "SELECT * FROM sensitive_write_audits WHERE dedupe_key=$1",
            operation["dedupe_key"],
        )
        if row is None:
            raise RuntimeError("Sensitive write audit preparation was not durable")
        return dict(row), inserted is not None

    async def claim_sensitive_write_audit(self, operation_id: str, started_at: str) -> bool:
        pool = await self._get_pool()
        status = await pool.execute(
            "UPDATE sensitive_write_audits SET status='executing', updated_at=$1 "
            "WHERE operation_id=$2 AND status='prepared'",
            started_at,
            operation_id,
        )
        return status.endswith(" 1")

    async def finish_sensitive_write_audit(
        self, operation_id: str, status: str, updated_at: str
    ) -> bool:
        if status not in {"succeeded", "failed", "outcome_unknown"}:
            raise ValueError("invalid sensitive write audit status")
        pool = await self._get_pool()
        result = await pool.execute(
            "UPDATE sensitive_write_audits SET status=$1, updated_at=$2 "
            "WHERE operation_id=$3 AND status='executing'",
            status,
            updated_at,
            operation_id,
        )
        return result.endswith(" 1")

    async def get_sensitive_write_audit_by_dedupe_key(
        self, dedupe_key: str
    ) -> dict[str, Any] | None:
        pool = await self._get_pool()
        row = await pool.fetchrow(
            "SELECT * FROM sensitive_write_audits WHERE dedupe_key=$1", dedupe_key
        )
        return dict(row) if row is not None else None

    async def get_operation(self, operation_id: str) -> dict[str, Any] | None:
        pool = await self._get_pool()
        row = await pool.fetchrow(
            "SELECT * FROM recovery_operations WHERE operation_id=$1", operation_id
        )
        return self._decode_operation(row)

    async def get_operation_by_dedupe_key(self, dedupe_key: str) -> dict[str, Any] | None:
        pool = await self._get_pool()
        row = await pool.fetchrow(
            "SELECT * FROM recovery_operations WHERE dedupe_key=$1", dedupe_key
        )
        return self._decode_operation(row)

    async def list_operations(
        self, tenant_id: str, *, statuses: list[str] | None, limit: int = 100
    ) -> list[dict[str, Any]]:
        pool = await self._get_pool()
        if statuses:
            rows = await pool.fetch(
                """SELECT * FROM recovery_operations WHERE tenant_id=$1 AND status=ANY($2::text[])
                ORDER BY created_at DESC LIMIT $3""",
                tenant_id,
                statuses,
                max(1, min(limit, 200)),
            )
        else:
            rows = await pool.fetch(
                """SELECT * FROM recovery_operations WHERE tenant_id=$1
                ORDER BY created_at DESC LIMIT $2""",
                tenant_id,
                max(1, min(limit, 200)),
            )
        return [dict(row) for row in rows]

    async def get_operation_metrics(self, tenant_id: str, *, since: str) -> dict[str, Any]:
        pool = await self._get_pool()
        counts = await pool.fetch(
            """SELECT status, COUNT(*) AS count, COALESCE(SUM(attempt_count), 0) AS attempts
            FROM recovery_operations WHERE tenant_id=$1 GROUP BY status""",
            tenant_id,
        )
        recent = await pool.fetch(
            """SELECT status, COUNT(*) AS count FROM recovery_operations
            WHERE tenant_id=$1 AND updated_at >= $2 GROUP BY status""",
            tenant_id,
            since,
        )
        upstream = await pool.fetch(
            """SELECT upstream_name, status, COUNT(*) AS count,
            COALESCE(SUM(attempt_count), 0) AS attempts FROM recovery_operations
            WHERE tenant_id=$1 AND updated_at >= $2 GROUP BY upstream_name, status
            ORDER BY upstream_name, status""",
            tenant_id,
            since,
        )
        oldest_open = await pool.fetchval(
            """SELECT MIN(created_at) FROM recovery_operations
            WHERE tenant_id=$1 AND status IN ('pending', 'working')""",
            tenant_id,
        )
        return {
            "by_status": {str(row["status"]): int(row["count"]) for row in counts},
            "attempts": sum(int(row["attempts"]) for row in counts),
            "recent_by_status": {str(row["status"]): int(row["count"]) for row in recent},
            "oldest_open_at": oldest_open.isoformat() if oldest_open else None,
            "by_upstream_24h": [
                {
                    "upstream_name": row["upstream_name"],
                    "status": row["status"],
                    "count": int(row["count"]),
                    "attempts": int(row["attempts"]),
                }
                for row in upstream
            ],
        }

    async def claim_operations(
        self, *, limit: int, now: str, lease_expires_at: str
    ) -> list[dict[str, Any]]:
        pool = await self._get_pool()
        claimed: list[dict[str, Any]] = []
        async with pool.acquire() as conn:
            async with conn.transaction():
                rows = await conn.fetch(
                    """SELECT operation_id FROM recovery_operations
                    WHERE status IN ('pending', 'working')
                      AND (status='pending' OR lease_expires_at IS NULL OR lease_expires_at<=$1)
                      AND (next_attempt_at IS NULL OR next_attempt_at<=$1)
                    ORDER BY COALESCE(next_attempt_at, created_at), created_at
                    LIMIT $2 FOR UPDATE SKIP LOCKED""",
                    now,
                    max(1, min(limit, 100)),
                )
                for row in rows:
                    lease_token = uuid.uuid4().hex
                    updated = await conn.fetchrow(
                        """UPDATE recovery_operations SET status='working', lease_token=$1,
                        lease_expires_at=$2, updated_at=$3 WHERE operation_id=$4 RETURNING *""",
                        lease_token,
                        lease_expires_at,
                        now,
                        row["operation_id"],
                    )
                    if updated is not None:
                        claimed.append(dict(updated))
        return claimed

    async def update_operation(
        self,
        operation_id: str,
        *,
        lease_token: str,
        status: str,
        attempt_count: int,
        updated_at: str,
        next_attempt_at: str | None = None,
        lease_expires_at: str | None = None,
        result_ciphertext: str | None = None,
        last_error_type: str | None = None,
        last_error: str | None = None,
        cancel_requested: bool | None = None,
        expires_at: str | None = None,
    ) -> bool:
        if status not in {
            "working",
            "pending",
            "completed",
            "failed",
            "needs_attention",
            "cancelled",
        }:
            raise ValueError("Invalid recovery operation status")
        pool = await self._get_pool()
        row = await pool.fetchrow(
            """UPDATE recovery_operations SET status=$1, attempt_count=$2,
            next_attempt_at=$3, result_ciphertext=COALESCE($4, result_ciphertext),
            lease_token=$5, lease_expires_at=$6, last_error_type=$7, last_error=$8,
            cancel_requested=COALESCE($9, cancel_requested), updated_at=$10,
            expires_at=COALESCE($11, expires_at)
            WHERE operation_id=$12 AND lease_token=$13 RETURNING operation_id""",
            status,
            attempt_count,
            next_attempt_at,
            result_ciphertext,
            lease_token if status == "working" else None,
            lease_expires_at if status == "working" else None,
            last_error_type,
            last_error,
            cancel_requested,
            updated_at,
            expires_at,
            operation_id,
            lease_token,
        )
        return row is not None

    async def request_operation_cancel(
        self, operation_id: str, tenant_id: str, requester_sub: str, client_id: str | None
    ) -> bool:
        pool = await self._get_pool()
        row = await pool.fetchrow(
            """UPDATE recovery_operations SET cancel_requested=TRUE, updated_at=$1
            WHERE operation_id=$2 AND tenant_id=$3 AND requester_sub=$4 AND client_id=$5
            AND status IN ('pending','working') RETURNING operation_id""",
            datetime.now(UTC).isoformat(),
            operation_id,
            tenant_id,
            requester_sub,
            client_id or "",
        )
        return row is not None

    async def prune_operations(self, *, now: str) -> int:
        pool = await self._get_pool()
        result = await pool.execute(
            """DELETE FROM recovery_operations WHERE status IN
            ('completed','failed','needs_attention','cancelled') AND expires_at<=$1""",
            now,
        )
        return int(str(result).rsplit(" ", 1)[-1])

    async def close(self) -> None:
        if self._pool is not None and hasattr(self._pool, "close"):
            res = self._pool.close()
            if inspect.isawaitable(res):
                await res
