"""Durable task-backed recovery for explicitly idempotent direct write calls."""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import random
import time
import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

from gateway.auth import CallerIdentity
from gateway.dispatcher import OutputRedactor
from gateway.observability import log_event

_TERMINAL = {"completed", "failed", "needs_attention", "cancelled"}
_SAFE_TO_RECONCILE = {
    "upstream_capacity_exceeded",
    "upstream_unhealthy",
    "upstream_unreachable",
    "upstream_timeout",
    "upstream_unavailable",
}


def _now() -> datetime:
    return datetime.now(UTC)


def _iso(value: datetime) -> str:
    return value.astimezone(UTC).isoformat()


class OperationRecovery:
    """Owns operation encryption, MCP task shaping, and leased recovery work."""

    def __init__(self, runtime: Any) -> None:
        self.runtime = runtime

    def _aad(self, operation_id: str, tenant_id: str) -> bytes:
        return f"recovery-operation:{tenant_id}:{operation_id}".encode()

    def _encrypt(self, operation_id: str, tenant_id: str, value: Any) -> str:
        return self.runtime.cipher.encrypt(
            json.dumps(value, sort_keys=True, separators=(",", ":")),
            aad=self._aad(operation_id, tenant_id),
        )

    def _decrypt(self, row: dict[str, Any], field: str) -> Any:
        ciphertext = row.get(field)
        if not ciphertext:
            return None
        raw = self.runtime.cipher.decrypt(
            str(ciphertext), aad=self._aad(str(row["operation_id"]), str(row["tenant_id"]))
        )
        return json.loads(raw)

    def _fingerprint(self, purpose: str, value: Any) -> str:
        secret = self.runtime.settings.task_dedupe_secret.get_secret_value()
        if len(secret) < 32:
            raise ValueError(
                "GATEWAY_TASK_DEDUPE_SECRET must contain at least 32 characters "
                "for durable recovery"
            )
        key = hashlib.sha256(secret.encode()).digest()
        encoded = json.dumps(
            {"purpose": purpose, "value": value}, sort_keys=True, separators=(",", ":")
        ).encode("utf-8")
        return hmac.new(key, encoded, hashlib.sha256).hexdigest()

    @staticmethod
    def _redact_error(value: Any, sensitive_values: list[str] | None = None) -> str:
        clean = OutputRedactor.redact_text(str(value or ""))
        for sensitive in sensitive_values or []:
            if sensitive:
                clean = clean.replace(sensitive, "[REDACTED]")
        return clean[:500]

    @staticmethod
    def _tool_result(result: dict[str, Any]) -> dict[str, Any]:
        return {
            "content": [{"type": "text", "text": json.dumps(result, default=str)}],
            "structuredContent": result,
            "isError": not bool(result.get("ok")),
        }

    @staticmethod
    def _fallback_result(row: dict[str, Any]) -> dict[str, Any]:
        return {
            "ok": row["status"] == "completed",
            "operation_id": row["operation_id"],
            "operation_status": row["status"],
            "attempt_count": int(row["attempt_count"]),
            "outcome_unknown": row["status"] in {"pending", "working", "needs_attention"},
            "error_type": row.get("last_error_type"),
            "error": row.get("last_error"),
        }

    def task_result(self, row: dict[str, Any], *, create: bool) -> dict[str, Any]:
        status = str(row["status"])
        task_status = (
            "working"
            if status in {"pending", "working"}
            else "cancelled"
            if status == "cancelled"
            else "completed"
        )
        now = _now()
        try:
            expires = datetime.fromisoformat(str(row["expires_at"]))
            ttl_ms = max(0, int((expires - now).total_seconds() * 1000))
        except ValueError:
            ttl_ms = 0
        message = {
            "working": "The gateway is reconciling this write with the upstream.",
            "completed": "The upstream operation has completed.",
            "cancelled": (
                "Recovery was cancelled; the original write may already have reached the upstream."
            ),
        }[task_status]
        if task_status == "completed" and status in {"failed", "needs_attention"}:
            message = "Recovery stopped without a confirmed successful upstream result."
        task: dict[str, Any] = {
            "taskId": str(row["operation_id"]),
            "status": task_status,
            "statusMessage": message,
            "createdAt": str(row["created_at"]),
            "lastUpdatedAt": str(row["updated_at"]),
            "ttlMs": ttl_ms,
            "pollIntervalMs": 1000,
        }
        if task_status == "completed":
            final_result = self._decrypt(row, "result_ciphertext")
            if not isinstance(final_result, dict):
                final_result = self._tool_result(
                    {
                        "ok": False,
                        "operation_id": str(row["operation_id"]),
                        "operation_status": "needs_attention",
                        "outcome_unknown": True,
                        "error_type": "operation_result_unavailable",
                        "error": "The gateway could not recover a stored final result.",
                    }
                )
            task["result"] = final_result
        return {"resultType": "task" if create else "complete", **task}

    async def get_owned(self, identity: CallerIdentity, operation_id: str) -> dict[str, Any] | None:
        row = await self.runtime.store.get_operation(operation_id)
        if row is None:
            return None
        if (
            row["tenant_id"] != identity.tenant_id
            or row["requester_sub"] != identity.sub
            or (row.get("client_id") or "") != (identity.client_id or "")
        ):
            return None
        return row

    def status_result(self, row: dict[str, Any]) -> dict[str, Any]:
        result = self._fallback_result(row)
        stored = self._decrypt(row, "result_ciphertext")
        if isinstance(stored, dict):
            result["result"] = stored.get("structuredContent")
        result["created_at"] = str(row["created_at"])
        result["updated_at"] = str(row["updated_at"])
        return result

    async def begin_direct_call(
        self,
        identity: CallerIdentity,
        *,
        tool_name: str,
        arguments: dict[str, Any],
        mode: str,
        allowed_servers: set[str] | None,
        requester_confirmation_policy: Any,
        supports_tasks: bool,
        mode_decision_reason: str | None = None,
        mode_decision_score: float | None = None,
        experiment_id: str | None = None,
        ab_variant: str | None = None,
        model_name: str | None = None,
    ) -> tuple[dict[str, Any], str]:
        await self.runtime.ensure_catalog_for_identity(identity)
        tool = self.runtime.catalog.get_tool(tool_name)
        if tool is None:
            raise KeyError(f"Unknown tool '{tool_name}'")
        upstream = self.runtime.registry.get(tool.server)
        if upstream is None:
            raise KeyError(f"Upstream '{tool.server}' not found")
        policy = upstream.tool_recovery_policies.get(tool.name)
        if policy is None or not policy.enabled:
            raise ValueError("Recovery is not enabled for this tool")
        if not tool.mutating or upstream.tool_effects.get(tool.name, "write") == "read":
            raise ValueError("Recovery can only be enabled for mutating tools")
        if allowed_servers is not None and upstream.name not in allowed_servers:
            raise PermissionError("Tool is outside the request-level server ceiling.")
        allowed, reason = self.runtime.policy.can_execute_tool(
            identity, upstream, tool.name, tool.mutating
        )
        if not allowed:
            raise PermissionError(reason)
        if upstream.auth_type == "delegated_token":
            raise ValueError("Background recovery is not supported for delegated-token upstreams")
        if self.runtime.cipher.is_ephemeral:
            raise ValueError(
                "A persistent credential encryption key is required for durable write recovery"
            )
        if (
            policy.synchronous_budget_seconds is None
            or policy.synchronous_budget_seconds
            > self.runtime.settings.task_max_synchronous_budget_seconds
            or policy.max_attempts is None
            or policy.max_attempts > self.runtime.settings.task_max_attempts
            or policy.max_recovery_age_seconds is None
            or policy.max_recovery_age_seconds > self.runtime.settings.task_max_recovery_age_seconds
        ):
            raise ValueError("Recovery policy exceeds a platform-configured ceiling")
        key_name = str(policy.idempotency_key_argument or "")
        schema_properties = tool.input_schema.get("properties", {})
        key_schema = (
            schema_properties.get(key_name) if isinstance(schema_properties, dict) else None
        )
        if not isinstance(key_schema, dict) or key_schema.get("type") != "string":
            raise ValueError(
                f"Configured idempotency key argument '{key_name}' must be a string tool property"
            )
        key = arguments.get(key_name)
        if not isinstance(key, str) or not key.strip() or len(key) > 256:
            raise ValueError(
                f"'{key_name}' must be a non-empty idempotency key of at most 256 characters"
            )
        if policy.upstream_key_retention_seconds is not None and (
            policy.upstream_key_retention_seconds < policy.max_recovery_age_seconds
        ):
            raise ValueError("Upstream idempotency-key retention is shorter than recovery age")
        if policy.status_tool:
            status_tool = self.runtime.catalog.get_tool(f"{upstream.name}__{policy.status_tool}")
            if (
                status_tool is None
                or status_tool.server != upstream.name
                or upstream.tool_effects.get(status_tool.name) != "read"
            ):
                raise ValueError(
                    "Configured recovery status tool must be an enabled read operation"
                )
            status_properties = status_tool.input_schema.get("properties", {})
            status_key_schema = (
                status_properties.get(str(policy.status_key_argument))
                if isinstance(status_properties, dict)
                else None
            )
            if not isinstance(status_key_schema, dict) or status_key_schema.get("type") != "string":
                raise ValueError("Configured recovery status key must be a string tool property")

        clean_arguments = {name: value for name, value in arguments.items() if name != key_name}
        request_digest = hashlib.sha256(
            json.dumps(clean_arguments, sort_keys=True, separators=(",", ":")).encode("utf-8")
        ).hexdigest()
        dedupe_material = {
            "tenant": identity.tenant_id,
            "subject": identity.sub,
            "client": identity.client_id or "",
            "upstream": upstream.name,
            "tool": tool.name,
            "key": key,
        }
        dedupe_key = self._fingerprint("idempotency", dedupe_material)
        now = _now()
        operation_id = uuid.uuid4().hex
        lease_token = uuid.uuid4().hex
        recovery_deadline = now + timedelta(seconds=int(policy.max_recovery_age_seconds))
        lease_expires = now + timedelta(seconds=self.runtime.settings.task_worker_lease_seconds)
        payload = {
            "arguments": arguments,
            "identity": identity.public_profile(),
            "allowed_servers": sorted(allowed_servers) if allowed_servers is not None else None,
            "mode": mode,
            "recovery_policy": policy.model_dump(mode="json"),
            "approval_policy": (
                requester_confirmation_policy.model_dump(mode="json")
                if requester_confirmation_policy is not None
                else None
            ),
            "mode_decision_reason": mode_decision_reason,
            "mode_decision_score": mode_decision_score,
            "experiment_id": experiment_id,
            "ab_variant": ab_variant,
            "model_name": model_name,
        }
        row, created = await self.runtime.store.create_or_get_operation(
            {
                "operation_id": operation_id,
                "tenant_id": identity.tenant_id,
                "requester_sub": identity.sub,
                "client_id": identity.client_id or "",
                "upstream_name": upstream.name,
                "tool_name": tool.name,
                "dedupe_key": dedupe_key,
                "request_digest": request_digest,
                "payload_ciphertext": self._encrypt(operation_id, identity.tenant_id, payload),
                "status": "working",
                "attempt_count": 0,
                "max_attempts": int(policy.max_attempts),
                "next_attempt_at": now.isoformat(),
                "recovery_deadline": recovery_deadline.isoformat(),
                "lease_token": lease_token,
                "lease_expires_at": lease_expires.isoformat(),
                "created_at": now.isoformat(),
                "updated_at": now.isoformat(),
                "expires_at": (
                    recovery_deadline
                    + timedelta(seconds=self.runtime.settings.task_result_retention_seconds)
                ).isoformat(),
            }
        )
        if not created:
            if row["request_digest"] != request_digest:
                raise ValueError("Idempotency key was reused with different operation arguments")
            if row["status"] in _TERMINAL:
                stored = self._decrypt(row, "result_ciphertext")
                if isinstance(stored, dict):
                    public_result = stored.get("structuredContent")
                    if isinstance(public_result, dict):
                        return public_result, "ok" if public_result.get("ok") else "error"
            if supports_tasks:
                return self.task_result(row, create=True), "task"
            return self._tool_result(self.status_result(row)), "pending"

        log_event(
            "recovery.operation_started",
            operation_id=operation_id,
            tenant_id=identity.tenant_id,
            requester_sub=identity.sub,
            upstream=upstream.name,
            tool=tool.name,
        )

        count_updated = await self.runtime.store.update_operation(
            operation_id,
            lease_token=lease_token,
            status="working",
            attempt_count=1,
            updated_at=_iso(now),
            lease_expires_at=_iso(lease_expires),
        )
        if not count_updated:
            current = await self.runtime.store.get_operation(operation_id)
            if current is None:
                raise RuntimeError("Recovery operation disappeared before dispatch")
            return (
                self.task_result(current, create=True)
                if supports_tasks
                else self._tool_result(self.status_result(current)),
                "task" if supports_tasks else "pending",
            )

        try:
            try:
                result = await asyncio.wait_for(
                    self.runtime.call_direct_tool_for_identity(
                        identity,
                        tool_name,
                        arguments,
                        mode=mode,
                        allowed_servers=allowed_servers,
                        mode_decision_reason=mode_decision_reason,
                        mode_decision_score=mode_decision_score,
                        experiment_id=experiment_id,
                        ab_variant=ab_variant,
                        model_name=model_name,
                        requester_confirmation_policy=requester_confirmation_policy,
                        request_timeout_seconds=float(policy.synchronous_budget_seconds),
                    ),
                    timeout=float(policy.synchronous_budget_seconds),
                )
            except TimeoutError:
                result = {
                    "ok": False,
                    "outcome_unknown": True,
                    "error_type": "gateway_sync_budget_exceeded",
                    "error": (
                        "The gateway reached the synchronous recovery budget before "
                        "confirming the write."
                    ),
                }
        except BaseException as exc:
            if isinstance(exc, asyncio.CancelledError):
                raise
            safe_error = self._redact_error(exc, [key])
            error_result = self._tool_result(
                {
                    "ok": False,
                    "operation_id": operation_id,
                    "operation_status": "failed",
                    "outcome_unknown": False,
                    "error_type": "gateway_pre_dispatch_error",
                    "error": safe_error,
                }
            )
            await self.runtime.store.update_operation(
                operation_id,
                lease_token=lease_token,
                status="failed",
                attempt_count=1,
                updated_at=_iso(_now()),
                result_ciphertext=self._encrypt(operation_id, identity.tenant_id, error_result),
                last_error_type="gateway_pre_dispatch_error",
                last_error=safe_error,
                expires_at=_iso(
                    _now() + timedelta(seconds=self.runtime.settings.task_result_retention_seconds)
                ),
            )
            raise

        result["operation_id"] = operation_id
        result["operation_attempts"] = 1
        call_result = self._tool_result(result)
        if result.get("ok"):
            await self.runtime.store.update_operation(
                operation_id,
                lease_token=lease_token,
                status="completed",
                attempt_count=1,
                updated_at=_iso(_now()),
                result_ciphertext=self._encrypt(operation_id, identity.tenant_id, call_result),
                expires_at=_iso(
                    _now() + timedelta(seconds=self.runtime.settings.task_result_retention_seconds)
                ),
            )
            log_event(
                "recovery.operation_completed",
                operation_id=operation_id,
                tenant_id=identity.tenant_id,
                attempts=1,
                reconciled=False,
            )
            return result, "ok"

        error_type = str(result.get("error_type") or "upstream_error")
        safe_error = self._redact_error(result.get("error"), [key])
        safe_to_reconcile = bool(result.get("outcome_unknown")) or error_type in _SAFE_TO_RECONCILE
        if safe_to_reconcile:
            next_at = _now() + timedelta(seconds=random.uniform(0.2, 0.8))
            await self.runtime.store.update_operation(
                operation_id,
                lease_token=lease_token,
                status="pending",
                attempt_count=1,
                updated_at=_iso(_now()),
                next_attempt_at=_iso(next_at),
                last_error_type=error_type,
                last_error=safe_error,
            )
            current = await self.runtime.store.get_operation(operation_id)
            if current is None:
                raise RuntimeError("Recovery operation disappeared after dispatch")
            log_event(
                "recovery.operation_pending",
                operation_id=operation_id,
                tenant_id=identity.tenant_id,
                attempts=1,
                error_type=error_type,
            )
            if supports_tasks:
                return self.task_result(current, create=True), "task"
            return self._tool_result(self.status_result(current)), "pending"

        await self.runtime.store.update_operation(
            operation_id,
            lease_token=lease_token,
            status="failed",
            attempt_count=1,
            updated_at=_iso(_now()),
            result_ciphertext=self._encrypt(operation_id, identity.tenant_id, call_result),
            last_error_type=error_type,
            last_error=safe_error,
            expires_at=_iso(
                _now() + timedelta(seconds=self.runtime.settings.task_result_retention_seconds)
            ),
        )
        return result, "error"

    async def _task_result_for_operation(self, row: dict[str, Any], status: str) -> dict[str, Any]:
        result = self._tool_result(
            {
                "ok": False,
                "operation_id": str(row["operation_id"]),
                "operation_status": status,
                "outcome_unknown": status == "needs_attention",
                "error_type": str(row.get("last_error_type") or "upstream_outcome_unknown"),
                "error": str(
                    row.get("last_error")
                    or "The gateway reached its recovery limit without a confirmed upstream result."
                ),
            }
        )
        return result

    async def _schedule(
        self,
        row: dict[str, Any],
        *,
        lease_token: str,
        attempt_count: int,
        error_type: str | None,
        error: str | None,
    ) -> None:
        now = _now()
        try:
            deadline = datetime.fromisoformat(str(row["recovery_deadline"]))
        except ValueError:
            deadline = now
        if now >= deadline:
            result = await self._task_result_for_operation(row, "needs_attention")
            status = "needs_attention"
            next_at = None
            expires_at = _iso(
                now + timedelta(seconds=self.runtime.settings.task_result_retention_seconds)
            )
        elif attempt_count >= int(row["max_attempts"]) and not error_type == "status_pending":
            result = await self._task_result_for_operation(row, "needs_attention")
            status = "needs_attention"
            next_at = None
            expires_at = _iso(
                now + timedelta(seconds=self.runtime.settings.task_result_retention_seconds)
            )
        else:
            result = None
            status = "pending"
            delay = min(60.0, 2.0 ** min(max(attempt_count, 0), 5))
            next_at = _iso(now + timedelta(seconds=delay + random.uniform(0.1, 0.8)))
            expires_at = None
        await self.runtime.store.update_operation(
            str(row["operation_id"]),
            lease_token=lease_token,
            status=status,
            attempt_count=attempt_count,
            updated_at=_iso(now),
            next_attempt_at=next_at,
            result_ciphertext=(
                self._encrypt(str(row["operation_id"]), str(row["tenant_id"]), result)
                if result is not None
                else None
            ),
            last_error_type=error_type,
            last_error=error,
            expires_at=expires_at,
        )
        log_event(
            "recovery.operation_scheduled",
            operation_id=row.get("operation_id"),
            tenant_id=row.get("tenant_id"),
            status=status,
            attempts=attempt_count,
            error_type=error_type,
        )

    async def _process_claimed(self, row: dict[str, Any]) -> None:
        operation_id = str(row["operation_id"])
        tenant_id = str(row["tenant_id"])
        lease_token = str(row.get("lease_token") or "")
        payload = self._decrypt(row, "payload_ciphertext")
        if not isinstance(payload, dict):
            raise ValueError("Encrypted operation payload is invalid")
        if row.get("cancel_requested"):
            result = await self._task_result_for_operation(row, "cancelled")
            await self.runtime.store.update_operation(
                operation_id,
                lease_token=lease_token,
                status="cancelled",
                attempt_count=int(row["attempt_count"]),
                updated_at=_iso(_now()),
                result_ciphertext=self._encrypt(operation_id, tenant_id, result),
                expires_at=_iso(
                    _now() + timedelta(seconds=self.runtime.settings.task_result_retention_seconds)
                ),
            )
            return

        now = _now()
        deadline = datetime.fromisoformat(str(row["recovery_deadline"]))
        if now >= deadline:
            await self._schedule(
                row,
                lease_token=lease_token,
                attempt_count=int(row["attempt_count"]),
                error_type=row.get("last_error_type") or "upstream_outcome_unknown",
                error=row.get("last_error"),
            )
            return

        identity = CallerIdentity.model_validate(payload["identity"])
        upstream = self.runtime.registry.get(str(row["upstream_name"]))
        tool = self.runtime.catalog.get_tool(f"{row['upstream_name']}__{row['tool_name']}")
        if upstream is None or tool is None:
            await self._schedule(
                row,
                lease_token=lease_token,
                attempt_count=int(row["attempt_count"]),
                error_type="recovery_configuration_changed",
                error="The upstream or tool is no longer available.",
            )
            return
        policy = upstream.tool_recovery_policies.get(tool.name)
        if (
            policy is None
            or not policy.enabled
            or policy.model_dump(mode="json") != payload.get("recovery_policy")
            or upstream.auth_type == "delegated_token"
        ):
            await self._schedule(
                row,
                lease_token=lease_token,
                attempt_count=int(row["attempt_count"]),
                error_type="recovery_policy_changed",
                error="The recovery policy changed while this operation was pending.",
            )
            return
        approval = upstream.tool_approval_policies.get(tool.name)
        approval_snapshot = payload.get("approval_policy")
        if (
            approval.model_dump(mode="json") if approval is not None else None
        ) != approval_snapshot:
            await self._schedule(
                row,
                lease_token=lease_token,
                attempt_count=int(row["attempt_count"]),
                error_type="approval_policy_changed",
                error="The tool approval policy changed while this operation was pending.",
            )
            return

        allowed_servers = (
            set(payload["allowed_servers"])
            if isinstance(payload.get("allowed_servers"), list)
            else None
        )
        if policy.status_tool:
            status_name = f"{upstream.name}__{policy.status_tool}"
            status_tool = self.runtime.catalog.get_tool(status_name)
            if status_tool is None or upstream.tool_effects.get(policy.status_tool) != "read":
                await self._schedule(
                    row,
                    lease_token=lease_token,
                    attempt_count=int(row["attempt_count"]),
                    error_type="recovery_status_tool_unavailable",
                    error="The configured read-only status tool is unavailable.",
                )
                return
            key = payload["arguments"][str(policy.idempotency_key_argument)]
            status_result = await self.runtime.call_direct_tool_for_identity(
                identity,
                status_name,
                {str(policy.status_key_argument): key},
                mode="traditional",
                allowed_servers={upstream.name},
                request_timeout_seconds=min(10.0, upstream.request_timeout_seconds),
            )
            state = None
            if status_result.get("ok") and isinstance(status_result.get("result"), dict):
                state = status_result["result"].get(str(policy.status_field))
            normalized_state = str(state or "").casefold()
            if normalized_state in {v.casefold() for v in policy.status_success_values}:
                final = {
                    "ok": True,
                    "result": status_result["result"],
                    "operation_id": operation_id,
                    "operation_attempts": int(row["attempt_count"]),
                    "outcome_unknown": False,
                    "reconciled": True,
                }
                stored_result = self._tool_result(final)
                await self.runtime.store.update_operation(
                    operation_id,
                    lease_token=lease_token,
                    status="completed",
                    attempt_count=int(row["attempt_count"]),
                    updated_at=_iso(_now()),
                    result_ciphertext=self._encrypt(operation_id, tenant_id, stored_result),
                    expires_at=_iso(
                        _now()
                        + timedelta(seconds=self.runtime.settings.task_result_retention_seconds)
                    ),
                )
                return
            if normalized_state in {v.casefold() for v in policy.status_failure_values}:
                final = {
                    "ok": False,
                    "result": status_result["result"],
                    "operation_id": operation_id,
                    "operation_status": "failed",
                    "outcome_unknown": False,
                    "error_type": "upstream_operation_failed",
                    "error": "The upstream reports that this operation failed.",
                }
                stored_result = self._tool_result(final)
                await self.runtime.store.update_operation(
                    operation_id,
                    lease_token=lease_token,
                    status="failed",
                    attempt_count=int(row["attempt_count"]),
                    updated_at=_iso(_now()),
                    result_ciphertext=self._encrypt(operation_id, tenant_id, stored_result),
                    last_error_type="upstream_operation_failed",
                    last_error=final["error"],
                    expires_at=_iso(
                        _now()
                        + timedelta(seconds=self.runtime.settings.task_result_retention_seconds)
                    ),
                )
                return
            if normalized_state in {v.casefold() for v in policy.status_pending_values}:
                await self._schedule(
                    row,
                    lease_token=lease_token,
                    attempt_count=int(row["attempt_count"]),
                    error_type="status_pending",
                    error="The upstream still reports this operation as pending.",
                )
                return
            if normalized_state not in {v.casefold() for v in policy.status_not_found_values}:
                error = (
                    str(
                        status_result.get("error")
                        or "Status lookup returned an unrecognized state."
                    )
                    if not status_result.get("ok")
                    else "Status lookup returned an unrecognized state."
                )
                await self._schedule(
                    row,
                    lease_token=lease_token,
                    attempt_count=int(row["attempt_count"]),
                    error_type="upstream_status_unresolved",
                    error=self._redact_error(error, [str(key)]),
                )
                return

        attempts = int(row["attempt_count"])
        if attempts >= int(row["max_attempts"]):
            await self._schedule(
                row,
                lease_token=lease_token,
                attempt_count=attempts,
                error_type=row.get("last_error_type") or "upstream_outcome_unknown",
                error=row.get("last_error"),
            )
            return

        next_attempt = attempts + 1
        lease_expiry = _now() + timedelta(seconds=self.runtime.settings.task_worker_lease_seconds)
        reserved = await self.runtime.store.update_operation(
            operation_id,
            lease_token=lease_token,
            status="working",
            attempt_count=next_attempt,
            updated_at=_iso(_now()),
            lease_expires_at=_iso(lease_expiry),
        )
        if not reserved:
            return
        remaining_seconds = max(0.2, (deadline - _now()).total_seconds())
        try:
            result = await asyncio.wait_for(
                self.runtime.call_direct_tool_for_identity(
                    identity,
                    f"{upstream.name}__{tool.name}",
                    payload["arguments"],
                    mode=str(payload.get("mode") or "traditional"),
                    allowed_servers=allowed_servers,
                    mode_decision_reason=payload.get("mode_decision_reason"),
                    mode_decision_score=payload.get("mode_decision_score"),
                    experiment_id=payload.get("experiment_id"),
                    ab_variant=payload.get("ab_variant"),
                    model_name=payload.get("model_name"),
                    requester_confirmation_policy=approval,
                    request_timeout_seconds=min(
                        upstream.request_timeout_seconds,
                        remaining_seconds,
                    ),
                ),
                timeout=min(upstream.request_timeout_seconds, remaining_seconds),
            )
        except TimeoutError:
            result = {
                "ok": False,
                "outcome_unknown": True,
                "error_type": "upstream_timeout",
                "error": "The gateway timed out before confirming the upstream write.",
            }
        result["operation_id"] = operation_id
        result["operation_attempts"] = next_attempt
        call_result = self._tool_result(result)
        if result.get("ok"):
            await self.runtime.store.update_operation(
                operation_id,
                lease_token=lease_token,
                status="completed",
                attempt_count=next_attempt,
                updated_at=_iso(_now()),
                result_ciphertext=self._encrypt(operation_id, tenant_id, call_result),
                expires_at=_iso(
                    _now() + timedelta(seconds=self.runtime.settings.task_result_retention_seconds)
                ),
            )
            return

        error_type = str(result.get("error_type") or "upstream_error")
        error = self._redact_error(
            result.get("error"),
            [str(payload["arguments"].get(str(policy.idempotency_key_argument), ""))],
        )
        if bool(result.get("outcome_unknown")) or error_type in _SAFE_TO_RECONCILE:
            await self._schedule(
                row,
                lease_token=lease_token,
                attempt_count=next_attempt,
                error_type=error_type,
                error=error,
            )
            return
        await self.runtime.store.update_operation(
            operation_id,
            lease_token=lease_token,
            status="failed",
            attempt_count=next_attempt,
            updated_at=_iso(_now()),
            result_ciphertext=self._encrypt(operation_id, tenant_id, call_result),
            last_error_type=error_type,
            last_error=error,
            expires_at=_iso(
                _now() + timedelta(seconds=self.runtime.settings.task_result_retention_seconds)
            ),
        )

    async def run_worker(self) -> None:
        last_prune = 0.0
        while True:
            now = _now()
            try:
                if not hasattr(self.runtime.store, "claim_operations"):
                    await asyncio.sleep(self.runtime.settings.task_worker_poll_interval_seconds)
                    continue
                claimed = await self.runtime.store.claim_operations(
                    limit=1,
                    now=_iso(now),
                    lease_expires_at=_iso(
                        now + timedelta(seconds=self.runtime.settings.task_worker_lease_seconds)
                    ),
                )
                for row in claimed:
                    try:
                        await self._process_claimed(row)
                    except asyncio.CancelledError:
                        raise
                    except Exception as exc:
                        log_event(
                            "recovery.worker_operation_failed",
                            level="ERROR",
                            operation_id=row.get("operation_id"),
                            error_type=type(exc).__name__,
                        )
                        try:
                            latest = await self.runtime.store.get_operation(
                                str(row.get("operation_id") or "")
                            )
                            if latest is None:
                                continue
                            await self._schedule(
                                latest,
                                lease_token=str(latest.get("lease_token") or ""),
                                attempt_count=int(latest.get("attempt_count", 0)),
                                error_type="recovery_worker_error",
                                error="The recovery worker encountered an internal error.",
                            )
                        except Exception:
                            pass
                if time.monotonic() - last_prune >= 60.0:
                    await self.runtime.store.prune_operations(now=_iso(_now()))
                    last_prune = time.monotonic()
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                log_event(
                    "recovery.worker_loop_failed",
                    level="ERROR",
                    error_type=type(exc).__name__,
                )
            await asyncio.sleep(self.runtime.settings.task_worker_poll_interval_seconds)
