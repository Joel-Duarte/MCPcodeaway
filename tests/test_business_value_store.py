from __future__ import annotations

import asyncio

import pytest

from gateway.migrations import EXPECTED_COLUMNS, MIGRATIONS
from gateway.state import BusinessExecutionConflict, SQLiteAuditStore


@pytest.mark.asyncio
async def test_definition_versions_are_immutable_and_revision_allocation_is_monotonic(
    tmp_path,
) -> None:
    store = SQLiteAuditStore(str(tmp_path / "business.db"))
    await store.init()
    base = {
        "tenant_id": "tenant-a",
        "upstream_name": "crm",
        "definition_digest": "sha256:a",
        "definition": {"workflow_id": "support-case", "description": "Create support case"},
    }
    first = await store.save_integration_definition_version(base)
    second = await store.save_integration_definition_version(
        {**base, "definition_digest": "sha256:b", "definition": {"workflow_id": "other"}}
    )
    assert (first["revision"], second["revision"]) == ("1", "2")
    assert await store.list_integration_definition_versions("tenant-a", "crm") == [
        first,
        second,
    ]
    with pytest.raises(BusinessExecutionConflict):
        await store.save_integration_definition_version(
            {**base, "revision": "1", "definition_digest": "sha256:changed"}
        )
    await store.close()


@pytest.mark.asyncio
async def test_revision_allocation_serializes_across_sqlite_store_instances(tmp_path) -> None:
    path = str(tmp_path / "shared-business.db")
    first_store = SQLiteAuditStore(path)
    second_store = SQLiteAuditStore(path)
    await first_store.init()
    await second_store.init()

    async def save(store: SQLiteAuditStore, suffix: str) -> dict:
        return await store.save_integration_definition_version(
            {
                "tenant_id": "tenant-a",
                "upstream_name": "crm",
                "definition_digest": f"sha256:{suffix}",
                "definition": {"workflow_id": suffix},
            }
        )

    versions = await asyncio.gather(save(first_store, "a"), save(second_store, "b"))
    assert sorted(item["revision"] for item in versions) == ["1", "2"]
    await first_store.close()
    await second_store.close()


@pytest.mark.asyncio
async def test_publication_history_tracks_latest_publish_rollback_and_unpublish(tmp_path) -> None:
    store = SQLiteAuditStore(str(tmp_path / "business.db"))
    await store.init()
    for revision in ("1", "2"):
        await store.save_integration_definition_version(
            {
                "tenant_id": "tenant-a",
                "upstream_name": "crm",
                "revision": revision,
                "definition_digest": f"sha256:{revision}",
                "definition": {"workflow_id": f"workflow-{revision}"},
            }
        )
    await store.append_integration_definition_publication(
        {"tenant_id": "tenant-a", "upstream_name": "crm", "revision": "1", "action": "publish"}
    )
    await store.append_integration_definition_publication(
        {"tenant_id": "tenant-a", "upstream_name": "crm", "revision": "2", "action": "publish"}
    )
    rollback = await store.append_integration_definition_publication(
        {"tenant_id": "tenant-a", "upstream_name": "crm", "revision": "1", "action": "rollback"}
    )
    assert (await store.get_current_published_integration_definition("tenant-a", "crm"))[
        "revision"
    ] == "1"
    assert [
        row["revision"]
        for row in await store.list_current_published_integration_definitions("tenant-a")
    ] == ["1"]
    await store.append_integration_definition_publication(
        {"tenant_id": "tenant-a", "upstream_name": "crm", "revision": None, "action": "unpublish"}
    )
    assert await store.get_current_published_integration_definition("tenant-a", "crm") is None
    assert rollback["action"] == "rollback"
    await store.close()


@pytest.mark.asyncio
async def test_standalone_call_claim_request_binding_and_completion_are_idempotent(
    tmp_path,
) -> None:
    store = SQLiteAuditStore(str(tmp_path / "business.db"))
    await store.init()
    execution = await store.create_business_execution(
        {
            "business_execution_id": "exec-1",
            "tenant_id": "tenant-a",
            "caller_sub": "user-a",
            "client_id": "client-a",
            "kind": "standalone_tool",
            "upstream_name": "crm",
            "expires_at": "2099-01-01T00:00:00+00:00",
            "start_request_id": "string:jsonrpc-start-1",
            "start_request_digest": "start-body-digest",
            "metadata": {"baseline_id": "create-case", "pinned_definition": {"revision": "7"}},
        }
    )
    assert execution["metadata"]["baseline_id"] == "create-case"
    assert await store.get_business_execution("exec-1", "tenant-a", "user-a", "client-a")
    repeated_start = await store.create_business_execution(
        {
            "business_execution_id": "exec-2",
            "tenant_id": "tenant-a",
            "caller_sub": "user-a",
            "client_id": "client-a",
            "kind": "standalone_tool",
            "upstream_name": "crm",
            "expires_at": "2099-01-01T00:00:00+00:00",
            "start_request_id": "string:jsonrpc-start-1",
            "start_request_digest": "start-body-digest",
        }
    )
    assert repeated_start["business_execution_id"] == "exec-1"
    with pytest.raises(BusinessExecutionConflict, match="start request ID"):
        await store.create_business_execution(
            {
                "business_execution_id": "exec-3",
                "tenant_id": "tenant-a",
                "caller_sub": "user-a",
                "client_id": "client-a",
                "kind": "standalone_tool",
                "upstream_name": "crm",
                "expires_at": "2099-01-01T00:00:00+00:00",
                "start_request_id": "string:jsonrpc-start-1",
                "start_request_digest": "different-body",
            }
        )
    await store.bind_workflow_request("tenant-a", "exec-1", "jsonrpc-1", "body-digest-a")
    with pytest.raises(BusinessExecutionConflict, match="different request body"):
        await store.bind_workflow_request("tenant-a", "exec-1", "jsonrpc-1", "body-digest-b")

    call = {
        "workflow_call_id": "call-1",
        "tenant_id": "tenant-a",
        "business_execution_id": "exec-1",
        "call_key": "stable-call-key",
        "outer_request_id": "jsonrpc-1",
        "callback_ordinal": 0,
        "upstream_name": "crm",
        "tool_name": "create_case",
        "request_digest": "body-digest-a",
        "arguments_digest": "args-digest-a",
        "evidence": {"completion_predicate_matched": True},
    }
    _, created = await store.create_workflow_call(call)
    assert created is True
    _, created = await store.create_workflow_call(call)
    assert created is False
    with pytest.raises(BusinessExecutionConflict, match="different logical call"):
        await store.create_workflow_call(
            {**call, "workflow_call_id": "call-2", "call_key": "another-key"}
        )
    assert (await store.list_workflow_calls("tenant-a", "exec-1"))[0]["evidence"] == {
        "completion_predicate_matched": True
    }

    event = {
        "event_id": "value-1",
        "tenant_id": "tenant-a",
        "business_execution_id": "exec-1",
        "completion_key": "terminal-completion",
        "upstream_name": "crm",
        "outcome_namespace": "case",
        "outcome_digest": "outcome-sha256",
        "evidence": {"completion_predicate_matched": True},
    }
    credited, inserted = await store.record_business_value_event(event)
    assert inserted and credited["credited"] == 1
    duplicate, inserted = await store.record_business_value_event({**event, "event_id": "value-2"})
    assert not inserted and duplicate["event_id"] == "value-1"
    await store.close()


@pytest.mark.asyncio
async def test_workflow_calls_support_multiple_operations_write_lease_and_event_dedup(
    tmp_path,
) -> None:
    store = SQLiteAuditStore(str(tmp_path / "business.db"))
    await store.init()
    await store.create_business_execution(
        {
            "business_execution_id": "flow-1",
            "tenant_id": "tenant-a",
            "caller_sub": "user-a",
            "client_id": "client-a",
            "kind": "workflow",
            "upstream_name": "crm",
            "definition_revision": "3",
            "expires_at": "2099-01-01T00:00:00+00:00",
            "metadata": {"baseline": {"required_evidence": ["lookup", "create"]}},
        }
    )

    async def add_call(call_id: str, ordinal: int) -> tuple[dict, bool]:
        return await store.create_workflow_call(
            {
                "workflow_call_id": call_id,
                "tenant_id": "tenant-a",
                "business_execution_id": "flow-1",
                "call_key": call_id,
                "outer_request_id": "outer-1",
                "callback_ordinal": ordinal,
                "upstream_name": "crm",
                "tool_name": call_id,
                "request_digest": "body-digest",
                "arguments_digest": f"args-{call_id}",
            }
        )

    await asyncio.gather(add_call("lookup", 0), add_call("create", 1))
    assert len(await store.list_workflow_calls("tenant-a", "flow-1")) == 2
    assert await store.claim_workflow_write("flow-1", "create", "2000-01-01T00:00:00+00:00")
    assert not await store.claim_workflow_write("flow-1", "other", "2099-01-01T00:00:00+00:00")
    assert await store.release_workflow_write("flow-1", "create")
    event = {
        "tenant_id": "tenant-a",
        "business_execution_id": "flow-1",
        "idempotency_key": "call-create-completed",
        "event_type": "call_completed",
        "evidence": {"completion_predicate_matched": True},
    }
    assert await store.append_workflow_execution_event(event)
    assert not await store.append_workflow_execution_event(event)
    with pytest.raises(ValueError, match="prohibited field"):
        await store.append_workflow_execution_event({**event, "evidence": {"output": "sensitive"}})
    await store.close()


def test_postgres_migration_contract_includes_business_value_constraints() -> None:
    assert MIGRATIONS[-1].version == 3
    assert {
        "integration_definition_versions",
        "workflow_executions",
        "workflow_request_bindings",
        "workflow_calls",
        "workflow_execution_events",
        "business_value_events",
    }.issubset(EXPECTED_COLUMNS)
    statements = "\n".join(MIGRATIONS[-1].statements)
    assert "idx_workflow_calls_call_key" in statements
    assert "idx_business_value_completion" in statements
    assert "idx_business_value_outcome_identity" in statements
