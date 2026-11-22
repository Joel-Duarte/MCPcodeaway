from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace
from typing import Any

import pytest
from pydantic import SecretStr

from gateway.auth import CallerIdentity
from gateway.recovery import OperationRecovery


def _standalone_definition() -> dict[str, Any]:
    baseline = {
        "manual_baseline_minutes": 5,
        "baseline_source": "administrator_estimate",
        "confidence": "unvalidated",
        "effective_from": "2026-11-22",
    }
    return {
        "integration": {
            "id": "support",
            "display_name": "Support",
            "upstream_name": "support",
        },
        "tool_refs": [{"name": "support__create_ticket", "schema_sha256": "a" * 64}],
        "standalone_tool_values": [
            {
                "id": "create_ticket",
                "name": "Create ticket",
                "description": "Create one support ticket.",
                "tool": "support__create_ticket",
                "completion": {
                    "tool": "support__create_ticket",
                    "success_when": {"path": "/ticket_id", "type": "non_empty_string"},
                },
                "business_value": baseline,
            }
        ],
    }


class _Cipher:
    is_ephemeral = False

    @staticmethod
    def encrypt(value: str, *, aad: bytes) -> str:
        del aad
        return value

    @staticmethod
    def decrypt(value: str, *, aad: bytes) -> str:
        del aad
        return value


class _Store:
    def __init__(self) -> None:
        self.operation: dict[str, Any] | None = None
        self.call = {
            "workflow_call_id": "workflow-call-1",
            "tenant_id": "tenant-a",
            "business_execution_id": "execution-1",
            "tool_qualified_name": "support__create_ticket",
            "status": "pending",
            "evidence": {},
        }
        self.execution = {
            "business_execution_id": "execution-1",
            "tenant_id": "tenant-a",
            "caller_sub": "user-a",
            "client_id": "client-a",
            "kind": "standalone_tool",
            "upstream_name": "support",
            "definition_revision": "1",
            "metadata": {
                "pinned_definition": _standalone_definition(),
                "selected_definition": _standalone_definition()["standalone_tool_values"][0],
                "baseline_snapshot": _standalone_definition()["standalone_tool_values"][0][
                    "business_value"
                ],
            },
        }
        self.released = asyncio.Event()

    async def get_workflow_call_by_id(self, tenant_id: str, workflow_call_id: str) -> dict | None:
        if (
            tenant_id == self.call["tenant_id"]
            and workflow_call_id == self.call["workflow_call_id"]
        ):
            return dict(self.call)
        return None

    async def get_workflow_call(
        self, tenant_id: str, business_execution_id: str, workflow_call_id: str
    ) -> dict | None:
        if (
            tenant_id == self.call["tenant_id"]
            and business_execution_id == self.call["business_execution_id"]
            and workflow_call_id == self.call["workflow_call_id"]
        ):
            return dict(self.call)
        return None

    async def get_business_execution(
        self, execution_id: str, tenant_id: str, caller_sub: str, client_id: str
    ) -> dict | None:
        if (execution_id, tenant_id, caller_sub, client_id) == (
            self.execution["business_execution_id"],
            self.execution["tenant_id"],
            self.execution["caller_sub"],
            self.execution["client_id"],
        ):
            return dict(self.execution)
        return None

    async def update_workflow_call(self, workflow_call_id: str, fields: dict[str, Any]) -> bool:
        if workflow_call_id != self.call["workflow_call_id"]:
            return False
        self.call.update(fields)
        return True

    async def release_workflow_write(self, execution_id: str, workflow_call_id: str) -> bool:
        if (execution_id, workflow_call_id) != (
            self.call["business_execution_id"],
            self.call["workflow_call_id"],
        ):
            return False
        self.released.set()
        return True


def _runtime(store: _Store) -> SimpleNamespace:
    identity = CallerIdentity(
        sub="user-a", tenant_id="tenant-a", client_id="client-a", groups=[], roles=[]
    )
    settings = SimpleNamespace(
        task_worker_poll_interval_seconds=0.01,
        task_worker_lease_seconds=5,
    )
    runtime = SimpleNamespace(
        store=store,
        cipher=_Cipher(),
        settings=settings,
        identity=identity,
        finalized=[],
    )

    async def finalize(*args: Any, **kwargs: Any) -> None:
        runtime.finalized.append((args, kwargs))

    runtime.finalize_attributed_call = finalize
    return runtime


def _operation(status: str, result: dict[str, Any]) -> dict[str, Any]:
    identity = CallerIdentity(
        sub="user-a", tenant_id="tenant-a", client_id="client-a", groups=[], roles=[]
    )
    return {
        "operation_id": "operation-1",
        "tenant_id": "tenant-a",
        "requester_sub": "user-a",
        "client_id": "client-a",
        "status": status,
        "payload_ciphertext": json.dumps(
            {"identity": identity.public_profile(), "workflow_call_id": "workflow-call-1"}
        ),
        "result_ciphertext": json.dumps({"structuredContent": result}),
    }


@pytest.mark.asyncio
async def test_sync_recovery_dispatch_receives_attributed_call_id() -> None:
    store = _Store()
    runtime = _runtime(store)
    captured: list[str | None] = []
    identity = runtime.identity
    tool = SimpleNamespace(
        server="support",
        name="create_ticket",
        qualified_name="support__create_ticket",
        mutating=True,
        input_schema={"properties": {"idempotency_key": {"type": "string"}}},
    )
    policy = SimpleNamespace(
        enabled=True,
        idempotency_key_argument="idempotency_key",
        upstream_key_retention_seconds=600,
        synchronous_budget_seconds=2,
        max_attempts=2,
        max_recovery_age_seconds=60,
        status_tool=None,
        status_key_argument=None,
    )
    policy.model_dump = lambda mode="json": {"enabled": True}
    upstream = SimpleNamespace(
        name="support",
        auth_type="api_key",
        tool_recovery_policies={"create_ticket": policy},
        tool_effects={"create_ticket": "write"},
        request_timeout_seconds=5,
    )
    runtime.ensure_catalog_for_identity = _async_noop
    runtime.catalog = SimpleNamespace(
        get_tool=lambda name: tool if name == tool.qualified_name else None
    )
    runtime.registry = SimpleNamespace(get=lambda name: upstream if name == "support" else None)
    runtime.policy = SimpleNamespace(can_execute_tool=lambda *args: (True, ""))
    runtime.cipher = _Cipher()
    runtime.settings = SimpleNamespace(
        task_dedupe_secret=SecretStr("d" * 40),
        task_max_synchronous_budget_seconds=5,
        task_max_attempts=3,
        task_max_recovery_age_seconds=120,
        task_worker_lease_seconds=5,
        task_result_retention_seconds=30,
    )

    async def create_or_get(operation: dict[str, Any]) -> tuple[dict[str, Any], bool]:
        store.operation = dict(operation)
        return store.operation, True

    async def update_operation(operation_id: str, **fields: Any) -> bool:
        assert store.operation is not None and store.operation["operation_id"] == operation_id
        store.operation.update({key: value for key, value in fields.items() if value is not None})
        return True

    async def call_direct(*args: Any, **kwargs: Any) -> dict[str, Any]:
        captured.append(kwargs.get("workflow_call_id"))
        return {
            "ok": True,
            "result": {"ticket_id": "T-1"},
            "sensitive_write_operation_id": "journal-1",
            "sensitive_write_journal_status": "succeeded",
        }

    store.create_or_get_operation = create_or_get
    store.update_operation = update_operation
    runtime.call_direct_tool_for_identity = call_direct
    recovery = OperationRecovery(runtime)
    result, status = await recovery.begin_direct_call(
        identity,
        tool_name=tool.qualified_name,
        arguments={"idempotency_key": "stable-key"},
        mode="traditional",
        allowed_servers={"support"},
        requester_confirmation_policy=None,
        supports_tasks=False,
        workflow_call_id="workflow-call-1",
    )
    assert status == "ok"
    assert result["ok"] is True
    assert captured == ["workflow-call-1"]
    assert (
        json.loads(store.operation["payload_ciphertext"])["workflow_call_id"] == "workflow-call-1"
    )


@pytest.mark.asyncio
async def test_worker_finalizes_pending_call_when_recovery_completes() -> None:
    store = _Store()
    runtime = _runtime(store)
    operation = _operation(
        "working",
        {
            "ok": True,
            "result": {"ticket_id": "T-2"},
            "sensitive_write_operation_id": "journal-2",
            "sensitive_write_journal_status": "succeeded",
        },
    )
    operation["attempt_count"] = 1
    operation["lease_token"] = "lease-1"
    runtime.store.operation = operation
    claimed = False

    async def claim_operations(**kwargs: Any) -> list[dict[str, Any]]:
        nonlocal claimed
        if claimed:
            return []
        claimed = True
        return [dict(operation)]

    async def get_operation(operation_id: str) -> dict[str, Any] | None:
        return dict(operation) if operation_id == operation["operation_id"] else None

    async def update_operation(operation_id: str, **fields: Any) -> bool:
        assert operation_id == operation["operation_id"]
        operation.update({key: value for key, value in fields.items() if value is not None})
        return True

    async def prune_operations(**kwargs: Any) -> int:
        return 0

    runtime.store.claim_operations = claim_operations
    runtime.store.get_operation = get_operation
    runtime.store.update_operation = update_operation
    runtime.store.prune_operations = prune_operations

    class WorkerRecovery(OperationRecovery):
        async def _process_claimed(self, _row: dict[str, Any]) -> None:
            operation["status"] = "completed"

    recovery = WorkerRecovery(runtime)
    worker = asyncio.create_task(recovery.run_worker())
    try:
        await asyncio.wait_for(store.released.wait(), timeout=1)
    finally:
        worker.cancel()
        await asyncio.gather(worker, return_exceptions=True)
    assert len(runtime.finalized) == 1
    assert runtime.finalized[0][1]["status"] == "succeeded"
    assert runtime.finalized[0][1]["result"] == {"ticket_id": "T-2"}


@pytest.mark.parametrize(
    ("operation_status", "result", "expected_call_status", "released"),
    [
        ("failed", {"ok": False, "outcome_unknown": False}, "failed", True),
        ("needs_attention", {"ok": False, "outcome_unknown": True}, "outcome_unknown", False),
    ],
)
@pytest.mark.asyncio
async def test_recovery_terminal_failure_or_unknown_updates_attribution_safely(
    operation_status: str,
    result: dict[str, Any],
    expected_call_status: str,
    released: bool,
) -> None:
    store = _Store()
    runtime = _runtime(store)
    recovery = OperationRecovery(runtime)
    await recovery._sync_attributed_workflow_call(_operation(operation_status, result))
    assert runtime.finalized[0][1]["status"] == expected_call_status
    assert store.released.is_set() is released


async def _async_noop(*args: Any, **kwargs: Any) -> None:
    del args, kwargs
