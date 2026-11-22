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


_BUSINESS_RAW_KEYS = {"args", "arguments", "output", "full_output", "raw_output", "result"}


def _business_json(value: Any) -> str:
    """Serialize compact audit metadata while rejecting raw invocation content."""

    def check(node: Any) -> None:
        if isinstance(node, dict):
            forbidden = _BUSINESS_RAW_KEYS.intersection(str(key).lower() for key in node)
            if forbidden:
                raise ValueError(
                    f"business audit payload contains prohibited field: {sorted(forbidden)[0]}"
                )
            for child in node.values():
                check(child)
        elif isinstance(node, (list, tuple)):
            for child in node:
                check(child)

    check(value)
    return json.dumps(value, default=str, sort_keys=True)


def _decode_workflow_call(row: Any) -> dict[str, Any]:
    """Decode a durable call while exposing only persisted digest/evidence fields."""
    result = dict(row)
    evidence = json.loads(result.pop("payload_json", "{}"))
    result["evidence"] = evidence
    if not result.get("tool_qualified_name"):
        result["tool_qualified_name"] = (
            f"{result.get('upstream_name', '')}__{result.get('tool_name', '')}"
        )
    for key in (
        "evidence_predicate_matched",
        "completion_predicate_matched",
        "sensitive_write_journal_status",
    ):
        if key in evidence:
            result[key] = evidence[key]
    return result


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

    async def save_integration_definition_version(
        self, definition: dict[str, Any]
    ) -> dict[str, Any]: ...

    async def get_integration_definition_version(
        self, tenant_id: str, upstream_name: str, revision: str
    ) -> dict[str, Any] | None: ...

    async def list_integration_definition_versions(
        self, tenant_id: str, upstream_name: str
    ) -> list[dict[str, Any]]: ...

    async def append_integration_definition_publication(
        self, event: dict[str, Any]
    ) -> dict[str, Any]: ...

    async def get_current_published_integration_definition(
        self, tenant_id: str, upstream_name: str
    ) -> dict[str, Any] | None: ...

    async def list_current_published_integration_definitions(
        self, tenant_id: str
    ) -> list[dict[str, Any]]: ...

    async def create_business_execution(self, execution: dict[str, Any]) -> dict[str, Any]: ...

    async def list_business_executions(
        self, tenant_id: str, *, since: str | None = None, limit: int = 1000
    ) -> list[dict[str, Any]]: ...

    async def update_business_execution_status(
        self, business_execution_id: str, tenant_id: str, status: str, updated_at: str
    ) -> bool: ...

    async def get_business_execution(
        self, business_execution_id: str, tenant_id: str, caller_sub: str, client_id: str
    ) -> dict[str, Any] | None: ...

    async def bind_workflow_request(
        self,
        tenant_id: str,
        business_execution_id: str,
        outer_request_id: str,
        request_digest: str,
    ) -> dict[str, Any]: ...

    async def create_workflow_call(self, call: dict[str, Any]) -> tuple[dict[str, Any], bool]: ...

    async def list_workflow_calls(
        self, tenant_id: str, business_execution_id: str
    ) -> list[dict[str, Any]]: ...

    async def get_workflow_call_by_id(
        self, tenant_id: str, workflow_call_id: str
    ) -> dict[str, Any] | None: ...

    async def get_workflow_call(
        self, tenant_id: str, business_execution_id: str, workflow_call_id: str
    ) -> dict[str, Any] | None: ...

    async def update_workflow_call(self, workflow_call_id: str, fields: dict[str, Any]) -> bool: ...

    async def append_workflow_execution_event(self, event: dict[str, Any]) -> bool: ...

    async def claim_workflow_write(
        self, execution_id: str, call_id: str, lease_until: str
    ) -> bool: ...

    async def release_workflow_write(self, execution_id: str, call_id: str) -> bool: ...

    async def record_business_value_event(
        self, event: dict[str, Any]
    ) -> tuple[dict[str, Any] | None, bool]: ...

    async def list_business_value_events(
        self, tenant_id: str, *, since: str | None = None, limit: int = 1000
    ) -> list[dict[str, Any]]: ...

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


class BusinessExecutionConflict(ValueError):
    """Expected attribution conflict, such as request-ID body mismatch or a second
    distinct call identity in a standalone execution. Callers should reject the
    attribution request without treating the durable store as unavailable.
    """


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
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS integration_definition_versions (
                tenant_id TEXT NOT NULL, upstream_name TEXT NOT NULL, revision TEXT NOT NULL,
                definition_digest TEXT NOT NULL, definition_json TEXT NOT NULL,
                created_at TEXT NOT NULL, PRIMARY KEY (tenant_id, upstream_name, revision)
            );
            CREATE TABLE IF NOT EXISTS integration_definition_publications (
                event_id TEXT PRIMARY KEY, tenant_id TEXT NOT NULL, upstream_name TEXT NOT NULL,
                action TEXT NOT NULL, revision TEXT, actor_sub TEXT NOT NULL, created_at
                TEXT NOT NULL,
                evidence_json TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_definition_publications_latest
                ON integration_definition_publications (tenant_id, upstream_name,
                created_at DESC, event_id DESC);
            CREATE TABLE IF NOT EXISTS workflow_executions (
                business_execution_id TEXT PRIMARY KEY, tenant_id TEXT NOT NULL,
                caller_sub TEXT NOT NULL,
                client_id TEXT NOT NULL, kind TEXT NOT NULL, upstream_name TEXT NOT NULL,
                definition_revision TEXT,
                status TEXT NOT NULL CHECK (status IN (
                    'active','completed','outcome_unknown','failed','expired')),
                created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
                expires_at TEXT NOT NULL, standalone_call_key TEXT, active_write_call_id TEXT,
                write_lease_until TEXT, start_request_id TEXT, start_request_digest TEXT,
                payload_json TEXT NOT NULL,
                UNIQUE (tenant_id, caller_sub, client_id, start_request_id)
            );
            CREATE TABLE IF NOT EXISTS workflow_request_bindings (
                tenant_id TEXT NOT NULL, business_execution_id TEXT NOT NULL,
                outer_request_id TEXT NOT NULL, request_digest TEXT NOT NULL, created_at
                TEXT NOT NULL,
                PRIMARY KEY (tenant_id, business_execution_id, outer_request_id)
            );
            CREATE TABLE IF NOT EXISTS workflow_calls (
                workflow_call_id TEXT PRIMARY KEY, tenant_id TEXT NOT NULL,
                business_execution_id TEXT NOT NULL, call_key TEXT NOT NULL,
                outer_request_id TEXT NOT NULL, callback_ordinal INTEGER NOT NULL,
                upstream_name TEXT NOT NULL, tool_name TEXT NOT NULL, request_digest TEXT NOT NULL,
                tool_qualified_name TEXT NOT NULL, arguments_digest TEXT NOT NULL,
                status TEXT NOT NULL, created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL, payload_json TEXT NOT NULL, run_id TEXT,
                sensitive_write_operation_id TEXT, approval_id TEXT,
                UNIQUE (tenant_id, call_key)
            );
            CREATE UNIQUE INDEX IF NOT EXISTS idx_workflow_calls_callback_slot
                ON workflow_calls (tenant_id, business_execution_id, outer_request_id,
                                   callback_ordinal);
            CREATE INDEX IF NOT EXISTS idx_workflow_calls_execution
                ON workflow_calls (tenant_id, business_execution_id, created_at);
            CREATE TABLE IF NOT EXISTS workflow_execution_events (
                event_id TEXT PRIMARY KEY, tenant_id TEXT NOT NULL,
                business_execution_id TEXT NOT NULL, idempotency_key TEXT NOT NULL,
                event_type TEXT NOT NULL, created_at TEXT NOT NULL, evidence_json TEXT NOT NULL,
                UNIQUE (tenant_id, business_execution_id, idempotency_key)
            );
            CREATE TABLE IF NOT EXISTS business_value_events (
                event_id TEXT PRIMARY KEY, tenant_id TEXT NOT NULL,
                business_execution_id TEXT NOT NULL, completion_key TEXT NOT NULL,
                upstream_name TEXT NOT NULL, outcome_namespace TEXT, outcome_digest TEXT,
                credited INTEGER NOT NULL, created_at TEXT NOT NULL, evidence_json TEXT NOT NULL,
                UNIQUE (tenant_id, business_execution_id, completion_key)
            );
            CREATE UNIQUE INDEX IF NOT EXISTS idx_business_value_outcome_identity
                ON business_value_events (tenant_id, upstream_name, outcome_namespace,
                outcome_digest)
                WHERE outcome_namespace IS NOT NULL AND outcome_digest IS NOT NULL;
            CREATE TRIGGER IF NOT EXISTS immutable_definition_versions_update
                BEFORE UPDATE ON integration_definition_versions BEGIN SELECT
                RAISE(ABORT, 'immutable record'); END;
            CREATE TRIGGER IF NOT EXISTS immutable_definition_versions_delete
                BEFORE DELETE ON integration_definition_versions BEGIN SELECT
                RAISE(ABORT, 'immutable record'); END;
            CREATE TRIGGER IF NOT EXISTS append_only_definition_publications_update
                BEFORE UPDATE ON integration_definition_publications BEGIN SELECT
                RAISE(ABORT, 'append-only record'); END;
            CREATE TRIGGER IF NOT EXISTS append_only_definition_publications_delete
                BEFORE DELETE ON integration_definition_publications BEGIN SELECT
                RAISE(ABORT, 'append-only record'); END;
            CREATE TRIGGER IF NOT EXISTS append_only_workflow_events_update
                BEFORE UPDATE ON workflow_execution_events BEGIN SELECT RAISE(ABORT,
                'append-only record'); END;
            CREATE TRIGGER IF NOT EXISTS append_only_workflow_events_delete
                BEFORE DELETE ON workflow_execution_events BEGIN SELECT RAISE(ABORT,
                'append-only record'); END;
            CREATE TRIGGER IF NOT EXISTS append_only_business_value_update
                BEFORE UPDATE ON business_value_events BEGIN SELECT RAISE(ABORT,
                'append-only record'); END;
            CREATE TRIGGER IF NOT EXISTS append_only_business_value_delete
                BEFORE DELETE ON business_value_events BEGIN SELECT RAISE(ABORT,
                'append-only record'); END;
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

    async def save_integration_definition_version(
        self, definition: dict[str, Any]
    ) -> dict[str, Any]:
        """Insert an immutable, digest-addressed integration definition version."""
        row = dict(definition)
        row.setdefault("created_at", datetime.now(UTC).isoformat())
        payload = json.dumps(row.get("definition", {}), default=str, sort_keys=True)
        required = ("tenant_id", "upstream_name", "definition_digest")
        if any(not row.get(key) for key in required):
            raise ValueError("integration definition requires tenant, upstream and digest")
        async with self._lock:
            conn = self._ensure_conn()
            conn.execute("BEGIN IMMEDIATE")
            if not row.get("revision"):
                maximum = conn.execute(
                    (
                        "SELECT MAX(CAST(revision AS INTEGER)) FROM "
                        "integration_definition_versions WHERE tenant_id=? AND "
                        "upstream_name=? AND revision GLOB '[0-9]*'"
                    ),
                    (row["tenant_id"], row["upstream_name"]),
                ).fetchone()[0]
                row["revision"] = str(int(maximum or 0) + 1)
            conn.execute(
                """INSERT OR IGNORE INTO integration_definition_versions
                (tenant_id, upstream_name, revision, definition_digest, definition_json, created_at)
                VALUES (?, ?, ?, ?, ?, ?)""",
                (
                    row["tenant_id"],
                    row["upstream_name"],
                    row["revision"],
                    row["definition_digest"],
                    payload,
                    row["created_at"],
                ),
            )
            saved = conn.execute(
                (
                    "SELECT * FROM integration_definition_versions WHERE "
                    "tenant_id=? AND upstream_name=? AND revision=?"
                ),
                (row["tenant_id"], row["upstream_name"], row["revision"]),
            ).fetchone()
            if (
                saved["definition_digest"] != row["definition_digest"]
                or saved["definition_json"] != payload
            ):
                conn.rollback()
                raise BusinessExecutionConflict(
                    "immutable integration definition revision already exists with "
                    "different content"
                )
            conn.commit()
        return {**dict(saved), "definition": json.loads(saved["definition_json"])}

    async def get_integration_definition_version(
        self, tenant_id: str, upstream_name: str, revision: str
    ) -> dict[str, Any] | None:
        async with self._lock:
            row = (
                self._ensure_conn()
                .execute(
                    (
                        "SELECT * FROM integration_definition_versions WHERE "
                        "tenant_id=? AND upstream_name=? AND revision=?"
                    ),
                    (tenant_id, upstream_name, revision),
                )
                .fetchone()
            )
        return {**dict(row), "definition": json.loads(row["definition_json"])} if row else None

    async def list_integration_definition_versions(
        self, tenant_id: str, upstream_name: str
    ) -> list[dict[str, Any]]:
        async with self._lock:
            rows = (
                self._ensure_conn()
                .execute(
                    (
                        "SELECT * FROM integration_definition_versions WHERE "
                        "tenant_id=? AND upstream_name=? ORDER BY CAST(revision AS "
                        "INTEGER),created_at"
                    ),
                    (tenant_id, upstream_name),
                )
                .fetchall()
            )
        return [{**dict(row), "definition": json.loads(row["definition_json"])} for row in rows]

    async def append_integration_definition_publication(
        self, event: dict[str, Any]
    ) -> dict[str, Any]:
        row = dict(event)
        row.setdefault("event_id", uuid.uuid4().hex)
        row.setdefault("created_at", datetime.now(UTC).isoformat())
        action = str(row.get("action", "publish"))
        if action not in {"publish", "rollback", "unpublish"}:
            raise ValueError("invalid definition publication action")
        async with self._lock:
            conn = self._ensure_conn()
            conn.execute("BEGIN IMMEDIATE")
            revision = row.get("revision")
            if (
                revision is not None
                and not conn.execute(
                    (
                        "SELECT 1 FROM integration_definition_versions WHERE "
                        "tenant_id=? AND upstream_name=? AND revision=?"
                    ),
                    (row["tenant_id"], row["upstream_name"], revision),
                ).fetchone()
            ):
                conn.rollback()
                raise ValueError("publication target definition version does not exist")
            evidence = _business_json(row.get("evidence", {}))
            conn.execute(
                "INSERT INTO integration_definition_publications VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    row["event_id"],
                    row["tenant_id"],
                    row["upstream_name"],
                    action,
                    revision,
                    row.get("actor_sub", ""),
                    row["created_at"],
                    evidence,
                ),
            )
            conn.commit()
        return {**row, "evidence": json.loads(evidence)}

    async def get_current_published_integration_definition(
        self, tenant_id: str, upstream_name: str
    ) -> dict[str, Any] | None:
        async with self._lock:
            conn = self._ensure_conn()
            event = conn.execute(
                (
                    "SELECT * FROM integration_definition_publications WHERE "
                    "tenant_id=? AND upstream_name=? ORDER BY created_at DESC, "
                    "rowid DESC LIMIT 1"
                ),
                (tenant_id, upstream_name),
            ).fetchone()
            if not event or event["action"] == "unpublish" or not event["revision"]:
                return None
            definition = conn.execute(
                (
                    "SELECT * FROM integration_definition_versions WHERE "
                    "tenant_id=? AND upstream_name=? AND revision=?"
                ),
                (tenant_id, upstream_name, event["revision"]),
            ).fetchone()
        return (
            {**dict(definition), "definition": json.loads(definition["definition_json"])}
            if definition
            else None
        )

    async def list_current_published_integration_definitions(
        self, tenant_id: str
    ) -> list[dict[str, Any]]:
        async with self._lock:
            rows = (
                self._ensure_conn()
                .execute(
                    """SELECT d.* FROM integration_definition_publications p
                JOIN integration_definition_versions d ON d.tenant_id=p.tenant_id AND
                d.upstream_name=p.upstream_name AND d.revision=p.revision
                WHERE p.tenant_id=? AND p.action<>'unpublish' AND p.revision IS NOT NULL
                AND p.rowid=(SELECT p2.rowid FROM integration_definition_publications p2
                    WHERE p2.tenant_id=p.tenant_id AND p2.upstream_name=p.upstream_name
                    ORDER BY p2.created_at DESC,p2.rowid DESC LIMIT 1)
                ORDER BY d.upstream_name,d.revision""",
                    (tenant_id,),
                )
                .fetchall()
            )
        return [{**dict(row), "definition": json.loads(row["definition_json"])} for row in rows]

    async def create_business_execution(self, execution: dict[str, Any]) -> dict[str, Any]:
        row = dict(execution)
        row.setdefault("created_at", datetime.now(UTC).isoformat())
        payload = _business_json(row.get("metadata", {}))
        if row.get("status", "active") not in {
            "active",
            "completed",
            "outcome_unknown",
            "failed",
            "expired",
        }:
            raise ValueError("invalid business execution status")
        if row.get("start_request_id") is not None and not row.get("start_request_digest"):
            raise ValueError("a start request ID requires its exact request body digest")
        async with self._lock:
            conn = self._ensure_conn()
            conn.execute("BEGIN IMMEDIATE")
            request_id = row.get("start_request_id")
            if request_id is not None:
                existing = conn.execute(
                    """SELECT * FROM workflow_executions
                    WHERE tenant_id=? AND caller_sub=? AND client_id=? AND start_request_id=?""",
                    (row["tenant_id"], row["caller_sub"], row.get("client_id", ""), request_id),
                ).fetchone()
                if existing:
                    if existing["start_request_digest"] != row.get("start_request_digest"):
                        conn.rollback()
                        raise BusinessExecutionConflict(
                            "start request ID was reused with a different request body"
                        )
                    conn.commit()
                    return {**dict(existing), "metadata": json.loads(existing["payload_json"])}
            conn.execute(
                """INSERT INTO workflow_executions
                (business_execution_id,tenant_id,caller_sub,client_id,kind,upstream_name,
                 definition_revision,status,created_at,updated_at,expires_at,start_request_id,
                 start_request_digest,payload_json)
                VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    row["business_execution_id"],
                    row["tenant_id"],
                    row["caller_sub"],
                    row.get("client_id", ""),
                    row["kind"],
                    row["upstream_name"],
                    row.get("definition_revision"),
                    row.get("status", "active"),
                    row["created_at"],
                    row.get("updated_at", row["created_at"]),
                    row["expires_at"],
                    request_id,
                    row.get("start_request_digest"),
                    payload,
                ),
            )
            conn.commit()
            saved = conn.execute(
                "SELECT * FROM workflow_executions WHERE business_execution_id=?",
                (row["business_execution_id"],),
            ).fetchone()
        return {**dict(saved), "metadata": json.loads(saved["payload_json"])}

    async def list_business_executions(
        self, tenant_id: str, *, since: str | None = None, limit: int = 1000
    ) -> list[dict[str, Any]]:
        async with self._lock:
            rows = (
                self._ensure_conn()
                .execute(
                    """SELECT * FROM workflow_executions WHERE tenant_id=?
                AND (? IS NULL OR created_at>=?) ORDER BY created_at DESC,business_execution_id
                LIMIT ?""",
                    (tenant_id, since, since, max(1, min(limit, 10000))),
                )
                .fetchall()
            )
        return [{**dict(row), "metadata": json.loads(row["payload_json"])} for row in rows]

    async def update_business_execution_status(
        self, business_execution_id: str, tenant_id: str, status: str, updated_at: str
    ) -> bool:
        allowed = {"active", "completed", "outcome_unknown", "failed", "expired"}
        if status not in allowed:
            raise ValueError("invalid business execution status")
        async with self._lock:
            conn = self._ensure_conn()
            conn.execute("BEGIN IMMEDIATE")
            current = conn.execute(
                "SELECT status FROM workflow_executions WHERE tenant_id=? "
                "AND business_execution_id=?",
                (tenant_id, business_execution_id),
            ).fetchone()
            if current is None:
                conn.commit()
                return False
            current_status = str(current["status"])
            if current_status in {"completed", "failed", "expired"} and status != current_status:
                conn.commit()
                return False
            cursor = conn.execute(
                "UPDATE workflow_executions SET status=?,updated_at=? "
                "WHERE tenant_id=? AND business_execution_id=?",
                (status, updated_at, tenant_id, business_execution_id),
            )
            conn.commit()
        return bool(cursor.rowcount)

    async def get_business_execution(
        self, business_execution_id: str, tenant_id: str, caller_sub: str, client_id: str
    ) -> dict[str, Any] | None:
        async with self._lock:
            row = (
                self._ensure_conn()
                .execute(
                    (
                        "SELECT * FROM workflow_executions WHERE "
                        "business_execution_id=? AND tenant_id=? AND caller_sub=? AND "
                        "client_id=?"
                    ),
                    (business_execution_id, tenant_id, caller_sub, client_id),
                )
                .fetchone()
            )
        return {**dict(row), "metadata": json.loads(row["payload_json"])} if row else None

    async def bind_workflow_request(
        self, tenant_id: str, business_execution_id: str, outer_request_id: str, request_digest: str
    ) -> dict[str, Any]:
        async with self._lock:
            conn = self._ensure_conn()
            conn.execute("BEGIN IMMEDIATE")
            conn.execute(
                "INSERT OR IGNORE INTO workflow_request_bindings VALUES (?, ?, ?, ?, ?)",
                (
                    tenant_id,
                    business_execution_id,
                    outer_request_id,
                    request_digest,
                    datetime.now(UTC).isoformat(),
                ),
            )
            row = conn.execute(
                (
                    "SELECT * FROM workflow_request_bindings WHERE tenant_id=? AND "
                    "business_execution_id=? AND outer_request_id=?"
                ),
                (tenant_id, business_execution_id, outer_request_id),
            ).fetchone()
            if row["request_digest"] != request_digest:
                conn.rollback()
                raise BusinessExecutionConflict(
                    "outer request ID was reused with a different request body"
                )
            conn.commit()
        return dict(row)

    async def create_workflow_call(self, call: dict[str, Any]) -> tuple[dict[str, Any], bool]:
        row = dict(call)
        row.setdefault("created_at", datetime.now(UTC).isoformat())
        row.setdefault("updated_at", row["created_at"])
        payload = _business_json(row.get("evidence", {}))
        async with self._lock:
            conn = self._ensure_conn()
            conn.execute("BEGIN IMMEDIATE")
            execution = conn.execute(
                (
                    "SELECT kind, standalone_call_key FROM workflow_executions "
                    "WHERE business_execution_id=? AND tenant_id=?"
                ),
                (row["business_execution_id"], row["tenant_id"]),
            ).fetchone()
            if not execution:
                conn.rollback()
                raise ValueError("business execution does not exist")
            if execution["kind"] == "standalone_tool":
                if (
                    execution["standalone_call_key"]
                    and execution["standalone_call_key"] != row["call_key"]
                ):
                    conn.rollback()
                    raise BusinessExecutionConflict(
                        "standalone execution already claimed by a different logical call"
                    )
                conn.execute(
                    (
                        "UPDATE workflow_executions SET "
                        "standalone_call_key=COALESCE(standalone_call_key, ?) WHERE "
                        "business_execution_id=?"
                    ),
                    (row["call_key"], row["business_execution_id"]),
                )
            inserted = conn.execute(
                (
                    "INSERT OR IGNORE INTO workflow_calls (workflow_call_id, "
                    "tenant_id, business_execution_id, call_key, outer_request_id, "
                    "callback_ordinal, upstream_name, tool_name, request_digest, "
                    "tool_qualified_name, arguments_digest, status, created_at, "
                    "updated_at, payload_json) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, "
                    "?, ?, ?, ?, ?, ?)"
                ),
                (
                    row["workflow_call_id"],
                    row["tenant_id"],
                    row["business_execution_id"],
                    row["call_key"],
                    row["outer_request_id"],
                    int(row.get("callback_ordinal", 0)),
                    row["upstream_name"],
                    row["tool_name"],
                    row["request_digest"],
                    row.get("tool_qualified_name", f"{row['upstream_name']}__{row['tool_name']}"),
                    row["arguments_digest"],
                    row.get("status", "started"),
                    row["created_at"],
                    row["updated_at"],
                    payload,
                ),
            )
            saved = conn.execute(
                "SELECT * FROM workflow_calls WHERE tenant_id=? AND call_key=?",
                (row["tenant_id"], row["call_key"]),
            ).fetchone()
            if saved is None:
                saved = conn.execute(
                    "SELECT * FROM workflow_calls WHERE tenant_id=? AND business_execution_id=? "
                    "AND outer_request_id=? AND callback_ordinal=?",
                    (
                        row["tenant_id"],
                        row["business_execution_id"],
                        row["outer_request_id"],
                        int(row.get("callback_ordinal", 0)),
                    ),
                ).fetchone()
            if saved is None:
                conn.rollback()
                raise BusinessExecutionConflict(
                    "workflow call identity conflicts with an existing call"
                )
            if (
                saved["workflow_call_id"] != row["workflow_call_id"]
                or saved["call_key"] != row["call_key"]
                or saved["business_execution_id"] != row["business_execution_id"]
                or saved["outer_request_id"] != row["outer_request_id"]
                or saved["request_digest"] != row["request_digest"]
                or saved["arguments_digest"] != row["arguments_digest"]
                or saved["upstream_name"] != row["upstream_name"]
                or saved["tool_name"] != row["tool_name"]
                or saved["tool_qualified_name"]
                != row.get("tool_qualified_name", f"{row['upstream_name']}__{row['tool_name']}")
                or saved["callback_ordinal"] != int(row.get("callback_ordinal", 0))
            ):
                conn.rollback()
                raise BusinessExecutionConflict("call key is already bound to another invocation")
            created = bool(inserted.rowcount)
            conn.commit()
        return (_decode_workflow_call(saved), created)

    async def list_workflow_calls(
        self, tenant_id: str, business_execution_id: str
    ) -> list[dict[str, Any]]:
        async with self._lock:
            rows = (
                self._ensure_conn()
                .execute(
                    (
                        "SELECT * FROM workflow_calls WHERE tenant_id=? AND "
                        "business_execution_id=? ORDER BY created_at, workflow_call_id"
                    ),
                    (tenant_id, business_execution_id),
                )
                .fetchall()
            )
        return [_decode_workflow_call(row) for row in rows]

    async def get_workflow_call_by_id(
        self, tenant_id: str, workflow_call_id: str
    ) -> dict[str, Any] | None:
        async with self._lock:
            row = (
                self._ensure_conn()
                .execute(
                    "SELECT * FROM workflow_calls WHERE tenant_id=? AND workflow_call_id=?",
                    (tenant_id, workflow_call_id),
                )
                .fetchone()
            )
        return _decode_workflow_call(row) if row else None

    async def get_workflow_call(
        self, tenant_id: str, business_execution_id: str, workflow_call_id: str
    ) -> dict[str, Any] | None:
        async with self._lock:
            row = (
                self._ensure_conn()
                .execute(
                    """SELECT * FROM workflow_calls WHERE tenant_id=? AND business_execution_id=?
                AND workflow_call_id=?""",
                    (tenant_id, business_execution_id, workflow_call_id),
                )
                .fetchone()
            )
        return _decode_workflow_call(row) if row else None

    async def update_workflow_call(self, workflow_call_id: str, fields: dict[str, Any]) -> bool:
        allowed = {
            "status",
            "updated_at",
            "run_id",
            "sensitive_write_operation_id",
            "approval_id",
            "evidence",
        }
        if set(fields) - allowed:
            raise ValueError("unsupported workflow call update field")
        fields = dict(fields)
        if "evidence" in fields:
            fields["payload_json"] = _business_json(fields.pop("evidence"))
        fields.setdefault("updated_at", datetime.now(UTC).isoformat())
        async with self._lock:
            conn = self._ensure_conn()
            cur = conn.execute(
                "UPDATE workflow_calls SET "
                + ", ".join(f"{key}=?" for key in fields)
                + " WHERE workflow_call_id=?",
                (*fields.values(), workflow_call_id),
            )
            conn.commit()
        return bool(cur.rowcount)

    async def append_workflow_execution_event(self, event: dict[str, Any]) -> bool:
        row = dict(event)
        row.setdefault("event_id", uuid.uuid4().hex)
        row.setdefault("created_at", datetime.now(UTC).isoformat())
        evidence = _business_json(row.get("evidence", {}))
        async with self._lock:
            conn = self._ensure_conn()
            cur = conn.execute(
                "INSERT OR IGNORE INTO workflow_execution_events VALUES (?, ?, ?, ?, ?, ?, ?)",
                (
                    row["event_id"],
                    row["tenant_id"],
                    row["business_execution_id"],
                    row["idempotency_key"],
                    row["event_type"],
                    row["created_at"],
                    evidence,
                ),
            )
            inserted = bool(cur.rowcount)
            if not inserted:
                saved = conn.execute(
                    """SELECT event_type,evidence_json FROM workflow_execution_events
                    WHERE tenant_id=? AND business_execution_id=? AND idempotency_key=?""",
                    (row["tenant_id"], row["business_execution_id"], row["idempotency_key"]),
                ).fetchone()
                if (
                    saved is None
                    or saved["event_type"] != row["event_type"]
                    or saved["evidence_json"] != evidence
                ):
                    raise BusinessExecutionConflict(
                        "workflow event idempotency key was reused with different content"
                    )
            conn.commit()
        return inserted

    async def claim_workflow_write(self, execution_id: str, call_id: str, lease_until: str) -> bool:
        async with self._lock:
            conn = self._ensure_conn()
            cur = conn.execute(
                (
                    "UPDATE workflow_executions SET active_write_call_id=?, "
                    "write_lease_until=? WHERE business_execution_id=? AND "
                    "(active_write_call_id IS NULL OR active_write_call_id=?)"
                ),
                (call_id, lease_until, execution_id, call_id),
            )
            conn.commit()
        return bool(cur.rowcount)

    async def release_workflow_write(self, execution_id: str, call_id: str) -> bool:
        async with self._lock:
            conn = self._ensure_conn()
            cur = conn.execute(
                (
                    "UPDATE workflow_executions SET active_write_call_id=NULL, "
                    "write_lease_until=NULL WHERE business_execution_id=? AND "
                    "active_write_call_id=?"
                ),
                (execution_id, call_id),
            )
            conn.commit()
        return bool(cur.rowcount)

    async def record_business_value_event(
        self, event: dict[str, Any]
    ) -> tuple[dict[str, Any] | None, bool]:
        row = dict(event)
        row.setdefault("event_id", uuid.uuid4().hex)
        row.setdefault("created_at", datetime.now(UTC).isoformat())
        evidence = _business_json(row.get("evidence", {}))
        identity = (row.get("outcome_namespace"), row.get("outcome_digest"))
        async with self._lock:
            conn = self._ensure_conn()
            conn.execute("BEGIN IMMEDIATE")
            prior = conn.execute(
                (
                    "SELECT * FROM business_value_events WHERE tenant_id=? AND "
                    "business_execution_id=? AND completion_key=?"
                ),
                (row["tenant_id"], row["business_execution_id"], row["completion_key"]),
            ).fetchone()
            if not prior and all(identity):
                prior = conn.execute(
                    (
                        "SELECT * FROM business_value_events WHERE tenant_id=? AND "
                        "upstream_name=? AND outcome_namespace=? AND outcome_digest=?"
                    ),
                    (row["tenant_id"], row["upstream_name"], *identity),
                ).fetchone()
            if prior:
                conn.commit()
                return ({**dict(prior), "evidence": json.loads(prior["evidence_json"])}, False)
            conn.execute(
                "INSERT INTO business_value_events VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    row["event_id"],
                    row["tenant_id"],
                    row["business_execution_id"],
                    row["completion_key"],
                    row["upstream_name"],
                    identity[0],
                    identity[1],
                    1,
                    row["created_at"],
                    evidence,
                ),
            )
            conn.commit()
        return ({**row, "credited": 1, "evidence": json.loads(evidence)}, True)

    async def list_business_value_events(
        self, tenant_id: str, *, since: str | None = None, limit: int = 1000
    ) -> list[dict[str, Any]]:
        async with self._lock:
            rows = (
                self._ensure_conn()
                .execute(
                    (
                        "SELECT * FROM business_value_events WHERE tenant_id=? AND (? "
                        "IS NULL OR created_at>=?) ORDER BY created_at DESC LIMIT ?"
                    ),
                    (tenant_id, since, since, max(1, min(limit, 10000))),
                )
                .fetchall()
            )
        return [{**dict(row), "evidence": json.loads(row["evidence_json"])} for row in rows]


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

    async def save_integration_definition_version(
        self, definition: dict[str, Any]
    ) -> dict[str, Any]:
        row = dict(definition)
        row.setdefault("created_at", datetime.now(UTC).isoformat())
        payload = json.dumps(row.get("definition", {}), default=str, sort_keys=True)
        required = ("tenant_id", "upstream_name", "definition_digest")
        if any(not row.get(key) for key in required):
            raise ValueError("integration definition requires tenant, upstream and digest")
        pool = await self._get_pool()
        async with pool.acquire() as conn, conn.transaction():
            if not row.get("revision"):
                lock_key = f"integration-definition:{row['tenant_id']}:{row['upstream_name']}"
                await conn.execute(
                    "SELECT pg_advisory_xact_lock(hashtextextended($1, 0))", lock_key
                )
                maximum = await conn.fetchval(
                    (
                        "SELECT MAX(revision::bigint) FROM "
                        "integration_definition_versions WHERE tenant_id=$1 AND "
                        "upstream_name=$2 AND revision ~ '^[0-9]+$'"
                    ),
                    row["tenant_id"],
                    row["upstream_name"],
                )
                row["revision"] = str(int(maximum or 0) + 1)
            await conn.execute(
                """INSERT INTO integration_definition_versions VALUES ($1,$2,$3,$4,$5,$6)
                ON CONFLICT (tenant_id,upstream_name,revision) DO NOTHING""",
                row["tenant_id"],
                row["upstream_name"],
                row["revision"],
                row["definition_digest"],
                payload,
                row["created_at"],
            )
            saved = await conn.fetchrow(
                (
                    "SELECT * FROM integration_definition_versions WHERE "
                    "tenant_id=$1 AND upstream_name=$2 AND revision=$3"
                ),
                row["tenant_id"],
                row["upstream_name"],
                row["revision"],
            )
        if (
            saved["definition_digest"] != row["definition_digest"]
            or saved["definition_json"] != payload
        ):
            raise BusinessExecutionConflict(
                "immutable integration definition revision already exists with different content"
            )
        return {**dict(saved), "definition": json.loads(saved["definition_json"])}

    async def get_integration_definition_version(
        self, tenant_id: str, upstream_name: str, revision: str
    ) -> dict[str, Any] | None:
        pool = await self._get_pool()
        async with pool.acquire() as conn:
            row = await conn.fetchrow(
                (
                    "SELECT * FROM integration_definition_versions WHERE "
                    "tenant_id=$1 AND upstream_name=$2 AND revision=$3"
                ),
                tenant_id,
                upstream_name,
                revision,
            )
        return {**dict(row), "definition": json.loads(row["definition_json"])} if row else None

    async def list_integration_definition_versions(
        self, tenant_id: str, upstream_name: str
    ) -> list[dict[str, Any]]:
        pool = await self._get_pool()
        async with pool.acquire() as conn:
            rows = await conn.fetch(
                (
                    "SELECT * FROM integration_definition_versions WHERE "
                    "tenant_id=$1 AND upstream_name=$2 ORDER BY CASE WHEN revision "
                    "~ '^[0-9]+$' THEN revision::bigint END NULLS LAST,created_at"
                ),
                tenant_id,
                upstream_name,
            )
        return [{**dict(row), "definition": json.loads(row["definition_json"])} for row in rows]

    async def append_integration_definition_publication(
        self, event: dict[str, Any]
    ) -> dict[str, Any]:
        row = dict(event)
        row.setdefault("event_id", uuid.uuid4().hex)
        row.setdefault("created_at", datetime.now(UTC).isoformat())
        action = str(row.get("action", "publish"))
        if action not in {"publish", "rollback", "unpublish"}:
            raise ValueError("invalid definition publication action")
        evidence = _business_json(row.get("evidence", {}))
        pool = await self._get_pool()
        async with pool.acquire() as conn, conn.transaction():
            if row.get("revision") is not None and not await conn.fetchval(
                (
                    "SELECT EXISTS(SELECT 1 FROM integration_definition_versions "
                    "WHERE tenant_id=$1 AND upstream_name=$2 AND revision=$3)"
                ),
                row["tenant_id"],
                row["upstream_name"],
                row["revision"],
            ):
                raise ValueError("publication target definition version does not exist")
            await conn.execute(
                "INSERT INTO integration_definition_publications VALUES ($1,$2,$3,$4,$5,$6,$7,$8)",
                row["event_id"],
                row["tenant_id"],
                row["upstream_name"],
                action,
                row.get("revision"),
                row.get("actor_sub", ""),
                row["created_at"],
                evidence,
            )
        return {**row, "evidence": json.loads(evidence)}

    async def get_current_published_integration_definition(
        self, tenant_id: str, upstream_name: str
    ) -> dict[str, Any] | None:
        pool = await self._get_pool()
        async with pool.acquire() as conn:
            row = await conn.fetchrow(
                """SELECT d.* FROM integration_definition_publications p
                JOIN integration_definition_versions d USING (tenant_id,upstream_name,revision)
                WHERE p.tenant_id=$1 AND p.upstream_name=$2 AND p.action<>'unpublish' AND
                p.revision IS NOT NULL
                ORDER BY p.created_at DESC,p.event_id DESC LIMIT 1""",
                tenant_id,
                upstream_name,
            )
        return {**dict(row), "definition": json.loads(row["definition_json"])} if row else None

    async def list_current_published_integration_definitions(
        self, tenant_id: str
    ) -> list[dict[str, Any]]:
        pool = await self._get_pool()
        async with pool.acquire() as conn:
            rows = await conn.fetch(
                """SELECT d.* FROM (
                SELECT DISTINCT ON (tenant_id,upstream_name) * FROM
                integration_definition_publications
                WHERE tenant_id=$1 ORDER BY tenant_id,upstream_name,created_at DESC,event_id DESC
                ) p JOIN integration_definition_versions d USING (tenant_id,upstream_name,revision)
                WHERE p.action<>'unpublish' AND p.revision IS NOT NULL ORDER BY
                d.upstream_name,d.revision""",
                tenant_id,
            )
        return [{**dict(row), "definition": json.loads(row["definition_json"])} for row in rows]

    async def create_business_execution(self, execution: dict[str, Any]) -> dict[str, Any]:
        row = dict(execution)
        row.setdefault("created_at", datetime.now(UTC).isoformat())
        payload = _business_json(row.get("metadata", {}))
        if row.get("status", "active") not in {
            "active",
            "completed",
            "outcome_unknown",
            "failed",
            "expired",
        }:
            raise ValueError("invalid business execution status")
        if row.get("start_request_id") is not None and not row.get("start_request_digest"):
            raise ValueError("a start request ID requires its exact request body digest")
        pool = await self._get_pool()
        async with pool.acquire() as conn, conn.transaction():
            saved = await conn.fetchrow(
                """INSERT INTO workflow_executions
                (business_execution_id,tenant_id,caller_sub,client_id,kind,upstream_name,
                 definition_revision,status,created_at,updated_at,expires_at,start_request_id,
                 start_request_digest,payload_json)
                VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12,$13,$14)
                ON CONFLICT (tenant_id,caller_sub,client_id,start_request_id) DO NOTHING
                RETURNING *""",
                row["business_execution_id"],
                row["tenant_id"],
                row["caller_sub"],
                row.get("client_id", ""),
                row["kind"],
                row["upstream_name"],
                row.get("definition_revision"),
                row.get("status", "active"),
                row["created_at"],
                row.get("updated_at", row["created_at"]),
                row["expires_at"],
                row.get("start_request_id"),
                row.get("start_request_digest"),
                payload,
            )
            if saved is None and row.get("start_request_id") is not None:
                saved = await conn.fetchrow(
                    """SELECT * FROM workflow_executions WHERE tenant_id=$1 AND caller_sub=$2
                    AND client_id=$3 AND start_request_id=$4 FOR UPDATE""",
                    row["tenant_id"],
                    row["caller_sub"],
                    row.get("client_id", ""),
                    row["start_request_id"],
                )
                if saved["start_request_digest"] != row["start_request_digest"]:
                    raise BusinessExecutionConflict(
                        "start request ID was reused with a different request body"
                    )
            elif saved is None:
                raise RuntimeError("business execution ID already exists")
        return {**dict(saved), "metadata": json.loads(saved["payload_json"])}

    async def list_business_executions(
        self, tenant_id: str, *, since: str | None = None, limit: int = 1000
    ) -> list[dict[str, Any]]:
        pool = await self._get_pool()
        async with pool.acquire() as conn:
            rows = await conn.fetch(
                """SELECT * FROM workflow_executions WHERE tenant_id=$1
                AND ($2::TEXT IS NULL OR created_at >= $2)
                ORDER BY created_at DESC,business_execution_id LIMIT $3""",
                tenant_id,
                since,
                max(1, min(limit, 10000)),
            )
        return [{**dict(row), "metadata": json.loads(row["payload_json"])} for row in rows]

    async def update_business_execution_status(
        self, business_execution_id: str, tenant_id: str, status: str, updated_at: str
    ) -> bool:
        allowed = {"active", "completed", "outcome_unknown", "failed", "expired"}
        if status not in allowed:
            raise ValueError("invalid business execution status")
        pool = await self._get_pool()
        async with pool.acquire() as conn, conn.transaction():
            result = await conn.execute(
                """UPDATE workflow_executions SET status=$1,updated_at=$2
                WHERE tenant_id=$3 AND business_execution_id=$4
                AND (status NOT IN ('completed','failed','expired') OR status=$1)""",
                status,
                updated_at,
                tenant_id,
                business_execution_id,
            )
        return result.endswith(" 1")

    async def get_business_execution(
        self, business_execution_id: str, tenant_id: str, caller_sub: str, client_id: str
    ) -> dict[str, Any] | None:
        pool = await self._get_pool()
        async with pool.acquire() as conn:
            row = await conn.fetchrow(
                (
                    "SELECT * FROM workflow_executions WHERE "
                    "business_execution_id=$1 AND tenant_id=$2 AND caller_sub=$3 "
                    "AND client_id=$4"
                ),
                business_execution_id,
                tenant_id,
                caller_sub,
                client_id,
            )
        return {**dict(row), "metadata": json.loads(row["payload_json"])} if row else None

    async def bind_workflow_request(
        self, tenant_id: str, business_execution_id: str, outer_request_id: str, request_digest: str
    ) -> dict[str, Any]:
        pool = await self._get_pool()
        async with pool.acquire() as conn, conn.transaction():
            await conn.execute(
                (
                    "INSERT INTO workflow_request_bindings VALUES ($1,$2,$3,$4,$5) "
                    "ON CONFLICT (tenant_id,business_execution_id,outer_request_id) "
                    "DO NOTHING"
                ),
                tenant_id,
                business_execution_id,
                outer_request_id,
                request_digest,
                datetime.now(UTC).isoformat(),
            )
            row = await conn.fetchrow(
                (
                    "SELECT * FROM workflow_request_bindings WHERE tenant_id=$1 AND "
                    "business_execution_id=$2 AND outer_request_id=$3 FOR UPDATE"
                ),
                tenant_id,
                business_execution_id,
                outer_request_id,
            )
            if row["request_digest"] != request_digest:
                raise BusinessExecutionConflict(
                    "outer request ID was reused with a different request body"
                )
        return dict(row)

    async def create_workflow_call(self, call: dict[str, Any]) -> tuple[dict[str, Any], bool]:
        row = dict(call)
        row.setdefault("created_at", datetime.now(UTC).isoformat())
        row.setdefault("updated_at", row["created_at"])
        payload = _business_json(row.get("evidence", {}))
        pool = await self._get_pool()
        async with pool.acquire() as conn, conn.transaction():
            execution = await conn.fetchrow(
                (
                    "SELECT kind,standalone_call_key FROM workflow_executions WHERE "
                    "business_execution_id=$1 AND tenant_id=$2 FOR UPDATE"
                ),
                row["business_execution_id"],
                row["tenant_id"],
            )
            if not execution:
                raise ValueError("business execution does not exist")
            if execution["kind"] == "standalone_tool":
                if (
                    execution["standalone_call_key"]
                    and execution["standalone_call_key"] != row["call_key"]
                ):
                    raise BusinessExecutionConflict(
                        "standalone execution already claimed by a different logical call"
                    )
                await conn.execute(
                    (
                        "UPDATE workflow_executions SET "
                        "standalone_call_key=COALESCE(standalone_call_key,$1) WHERE "
                        "business_execution_id=$2"
                    ),
                    row["call_key"],
                    row["business_execution_id"],
                )
            inserted = await conn.fetchrow(
                """INSERT INTO workflow_calls
                (workflow_call_id,tenant_id,business_execution_id,call_key,outer_request_id,callback_ordinal,upstream_name,tool_name,request_digest,tool_qualified_name,arguments_digest,status,created_at,updated_at,payload_json)
                VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12,$13,$14,$15)
                ON CONFLICT DO NOTHING RETURNING *""",
                row["workflow_call_id"],
                row["tenant_id"],
                row["business_execution_id"],
                row["call_key"],
                row["outer_request_id"],
                int(row.get("callback_ordinal", 0)),
                row["upstream_name"],
                row["tool_name"],
                row["request_digest"],
                row.get("tool_qualified_name", f"{row['upstream_name']}__{row['tool_name']}"),
                row["arguments_digest"],
                row.get("status", "started"),
                row["created_at"],
                row["updated_at"],
                payload,
            )
            saved = inserted
            if saved is None:
                saved = await conn.fetchrow(
                    "SELECT * FROM workflow_calls WHERE tenant_id=$1 AND call_key=$2",
                    row["tenant_id"],
                    row["call_key"],
                )
            if saved is None:
                saved = await conn.fetchrow(
                    """SELECT * FROM workflow_calls WHERE tenant_id=$1
                       AND business_execution_id=$2 AND outer_request_id=$3
                       AND callback_ordinal=$4""",
                    row["tenant_id"],
                    row["business_execution_id"],
                    row["outer_request_id"],
                    int(row.get("callback_ordinal", 0)),
                )
            if saved is None:
                raise BusinessExecutionConflict(
                    "workflow call identity conflicts with an existing call"
                )
            if (
                saved["workflow_call_id"] != row["workflow_call_id"]
                or saved["call_key"] != row["call_key"]
                or saved["business_execution_id"] != row["business_execution_id"]
                or saved["outer_request_id"] != row["outer_request_id"]
                or saved["request_digest"] != row["request_digest"]
                or saved["arguments_digest"] != row["arguments_digest"]
                or saved["upstream_name"] != row["upstream_name"]
                or saved["tool_name"] != row["tool_name"]
                or saved["tool_qualified_name"]
                != row.get("tool_qualified_name", f"{row['upstream_name']}__{row['tool_name']}")
                or saved["callback_ordinal"] != int(row.get("callback_ordinal", 0))
            ):
                raise BusinessExecutionConflict("call key is already bound to another invocation")
        return (_decode_workflow_call(saved), inserted is not None)

    async def list_workflow_calls(
        self, tenant_id: str, business_execution_id: str
    ) -> list[dict[str, Any]]:
        pool = await self._get_pool()
        async with pool.acquire() as conn:
            rows = await conn.fetch(
                (
                    "SELECT * FROM workflow_calls WHERE tenant_id=$1 AND "
                    "business_execution_id=$2 ORDER BY created_at,workflow_call_id"
                ),
                tenant_id,
                business_execution_id,
            )
        return [_decode_workflow_call(row) for row in rows]

    async def get_workflow_call_by_id(
        self, tenant_id: str, workflow_call_id: str
    ) -> dict[str, Any] | None:
        pool = await self._get_pool()
        async with pool.acquire() as conn:
            row = await conn.fetchrow(
                "SELECT * FROM workflow_calls WHERE tenant_id=$1 AND workflow_call_id=$2",
                tenant_id,
                workflow_call_id,
            )
        return _decode_workflow_call(row) if row else None

    async def get_workflow_call(
        self, tenant_id: str, business_execution_id: str, workflow_call_id: str
    ) -> dict[str, Any] | None:
        pool = await self._get_pool()
        async with pool.acquire() as conn:
            row = await conn.fetchrow(
                """SELECT * FROM workflow_calls WHERE tenant_id=$1 AND business_execution_id=$2
                AND workflow_call_id=$3""",
                tenant_id,
                business_execution_id,
                workflow_call_id,
            )
        return _decode_workflow_call(row) if row else None

    async def update_workflow_call(self, workflow_call_id: str, fields: dict[str, Any]) -> bool:
        allowed = {
            "status",
            "updated_at",
            "run_id",
            "sensitive_write_operation_id",
            "approval_id",
            "evidence",
        }
        if set(fields) - allowed:
            raise ValueError("unsupported workflow call update field")
        fields = dict(fields)
        if "evidence" in fields:
            fields["payload_json"] = _business_json(fields.pop("evidence"))
        fields.setdefault("updated_at", datetime.now(UTC).isoformat())
        values = list(fields.values()) + [workflow_call_id]
        assignments = ", ".join(f"{key}=${index}" for index, key in enumerate(fields, 1))
        pool = await self._get_pool()
        async with pool.acquire() as conn, conn.transaction():
            result = await conn.execute(
                f"UPDATE workflow_calls SET {assignments} WHERE workflow_call_id=${len(values)}",
                *values,
            )
        return int(str(result).rsplit(" ", 1)[-1]) > 0

    async def append_workflow_execution_event(self, event: dict[str, Any]) -> bool:
        row = dict(event)
        row.setdefault("event_id", uuid.uuid4().hex)
        row.setdefault("created_at", datetime.now(UTC).isoformat())
        evidence = _business_json(row.get("evidence", {}))
        pool = await self._get_pool()
        async with pool.acquire() as conn:
            result = await conn.execute(
                """INSERT INTO workflow_execution_events VALUES ($1,$2,$3,$4,$5,$6,$7)
                ON CONFLICT (tenant_id,business_execution_id,idempotency_key) DO NOTHING""",
                row["event_id"],
                row["tenant_id"],
                row["business_execution_id"],
                row["idempotency_key"],
                row["event_type"],
                row["created_at"],
                evidence,
            )
            inserted = int(str(result).rsplit(" ", 1)[-1]) > 0
            if not inserted:
                saved = await conn.fetchrow(
                    """SELECT event_type,evidence_json FROM workflow_execution_events
                    WHERE tenant_id=$1 AND business_execution_id=$2 AND idempotency_key=$3""",
                    row["tenant_id"],
                    row["business_execution_id"],
                    row["idempotency_key"],
                )
                if (
                    saved is None
                    or saved["event_type"] != row["event_type"]
                    or saved["evidence_json"] != evidence
                ):
                    raise BusinessExecutionConflict(
                        "workflow event idempotency key was reused with different content"
                    )
        return inserted

    async def claim_workflow_write(self, execution_id: str, call_id: str, lease_until: str) -> bool:
        pool = await self._get_pool()
        async with pool.acquire() as conn:
            result = await conn.execute(
                """UPDATE workflow_executions SET active_write_call_id=$1,write_lease_until=$2
                WHERE business_execution_id=$3 AND (active_write_call_id IS NULL OR
                active_write_call_id=$1)""",
                call_id,
                lease_until,
                execution_id,
            )
        return int(str(result).rsplit(" ", 1)[-1]) > 0

    async def release_workflow_write(self, execution_id: str, call_id: str) -> bool:
        pool = await self._get_pool()
        async with pool.acquire() as conn:
            result = await conn.execute(
                (
                    "UPDATE workflow_executions SET "
                    "active_write_call_id=NULL,write_lease_until=NULL WHERE "
                    "business_execution_id=$1 AND active_write_call_id=$2"
                ),
                execution_id,
                call_id,
            )
        return int(str(result).rsplit(" ", 1)[-1]) > 0

    async def record_business_value_event(
        self, event: dict[str, Any]
    ) -> tuple[dict[str, Any] | None, bool]:
        row = dict(event)
        row.setdefault("event_id", uuid.uuid4().hex)
        row.setdefault("created_at", datetime.now(UTC).isoformat())
        evidence = _business_json(row.get("evidence", {}))
        pool = await self._get_pool()
        async with pool.acquire() as conn, conn.transaction():
            saved = await conn.fetchrow(
                """INSERT INTO business_value_events
                (event_id,tenant_id,business_execution_id,completion_key,upstream_name,outcome_namespace,outcome_digest,credited,created_at,evidence_json)
                VALUES ($1,$2,$3,$4,$5,$6,$7,TRUE,$8,$9)
                ON CONFLICT DO NOTHING RETURNING *""",
                row["event_id"],
                row["tenant_id"],
                row["business_execution_id"],
                row["completion_key"],
                row["upstream_name"],
                row.get("outcome_namespace"),
                row.get("outcome_digest"),
                row["created_at"],
                evidence,
            )
            if not saved:
                saved = await conn.fetchrow(
                    """SELECT * FROM business_value_events WHERE
                    (tenant_id=$1 AND business_execution_id=$2 AND completion_key=$3) OR
                    ($4::text IS NOT NULL AND $5::text IS NOT NULL AND tenant_id=$1 AND
                    upstream_name=$6 AND outcome_namespace=$4 AND outcome_digest=$5)
                    ORDER BY created_at LIMIT 1""",
                    row["tenant_id"],
                    row["business_execution_id"],
                    row["completion_key"],
                    row.get("outcome_namespace"),
                    row.get("outcome_digest"),
                    row["upstream_name"],
                )
        return (
            {**dict(saved), "evidence": json.loads(saved["evidence_json"])},
            saved["event_id"] == row["event_id"],
        )

    async def list_business_value_events(
        self, tenant_id: str, *, since: str | None = None, limit: int = 1000
    ) -> list[dict[str, Any]]:
        pool = await self._get_pool()
        async with pool.acquire() as conn:
            rows = await conn.fetch(
                (
                    "SELECT * FROM business_value_events WHERE tenant_id=$1 AND "
                    "($2::text IS NULL OR created_at>=$2) ORDER BY created_at DESC "
                    "LIMIT $3"
                ),
                tenant_id,
                since,
                max(1, min(limit, 10000)),
            )
        return [{**dict(row), "evidence": json.loads(row["evidence_json"])} for row in rows]
