"""MCP Tasks and legacy status fallback over durable write reconciliation."""

from __future__ import annotations

import asyncio
import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import httpx
import pytest

from examples.mock_backends import create_mock_backends_app
from gateway.auth import LocalDevKeyManager
from gateway.config import GatewaySettings, ToolRecoveryPolicy, UpstreamConfig
from gateway.mcp_protocol import request_headers, request_meta
from gateway.server import create_gateway_app
from gateway.state import SQLiteAuditStore

_GOLDEN = json.loads((Path(__file__).parent / "golden" / "flows.json").read_text(encoding="utf-8"))[
    "task_recovery_flows"
]


@pytest.mark.parametrize("flow", _GOLDEN, ids=lambda flow: flow["id"])
async def test_recovery_flows_are_explicitly_declared(flow: dict[str, Any]) -> None:
    assert flow["kind"] in {
        "mcp_task_recovery",
        "legacy_recovery_status",
        "idempotency_conflict",
    }


async def test_task_recovery_status_fallback_conflict_and_tenant_isolation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("RECONCILIATION_MOCK_API_KEY", "local-task-test-secret")
    monkeypatch.setenv("MOCK_IDEMPOTENCY_API_KEY", "local-task-test-secret")
    mock_app = create_mock_backends_app()
    upstream = UpstreamConfig.model_validate(
        {
            "name": "idempotency_api",
            "type": "openapi",
            "url": "http://mock-backends",
            "auth_type": "api_key",
            "secret_env_var": "RECONCILIATION_MOCK_API_KEY",
            "api_key_header": "X-API-Key",
            "openapi_spec": mock_app.openapi(),
            "allowed_roles": ["finance_analyst", "admin"],
            "allowed_operations": [
                "create_idempotent_operation",
                "get_idempotent_operation_status",
                "get_idempotent_operation_metrics",
            ],
            "tool_effects": {
                "create_idempotent_operation": "write",
                "get_idempotent_operation_status": "read",
                "get_idempotent_operation_metrics": "read",
            },
            "mutating_tools": ["create_idempotent_operation"],
            "tool_recovery_policies": {
                "create_idempotent_operation": {
                    "enabled": True,
                    "idempotency_key_argument": "idempotency_key",
                    "synchronous_budget_seconds": 2,
                    "max_attempts": 3,
                    "max_recovery_age_seconds": 300,
                    "upstream_key_retention_seconds": 3600,
                    "status_tool": "get_idempotent_operation_status",
                    "status_key_argument": "idempotency_key",
                    "status_field": "status",
                }
            },
            "max_requests_per_minute": 120,
            "max_concurrent_calls": 10,
        }
    )
    config_path = tmp_path / "upstreams.yaml"
    config_path.write_text(
        json.dumps({"upstreams": [upstream.model_dump(mode="json", exclude_none=True)]}),
        encoding="utf-8",
    )
    settings = GatewaySettings(
        app_env="sandbox",
        auth_mode="dev",
        profile="demo",
        upstreams_config_path=str(config_path),
        sqlite_path=str(tmp_path / "gateway.db"),
        provenance_signing_secret="task-recovery-test-signing-secret",
        credential_encryption_key="11" * 32,
        task_dedupe_secret="task-recovery-stable-dedupe-secret-32chars",
        task_worker_poll_interval_seconds=0.1,
    )
    keys = LocalDevKeyManager(issuer=settings.oidc_issuer)
    backend_client = httpx.AsyncClient(transport=httpx.ASGITransport(app=mock_app))
    app = create_gateway_app(
        settings=settings,
        store=SQLiteAuditStore(settings.sqlite_path),
        dev_keys=keys,
        http_client=backend_client,
    )
    token = keys.issue_user_token(
        persona="gateway_admin",
        overrides={"roles": ["admin", "gateway_admin"], "scopes": ["mcp.tools", "mcp.write"]},
    )
    foreign_token = keys.issue_user_token(
        persona="gateway_admin",
        overrides={
            "sub": "other-user",
            "roles": ["admin", "gateway_admin"],
            "scopes": ["mcp.tools", "mcp.write"],
        },
    )

    async def call(
        client: httpx.AsyncClient,
        bearer: str,
        name: str,
        arguments: dict[str, Any],
        *,
        supports_tasks: bool = False,
    ) -> httpx.Response:
        return await client.post(
            "/mcp?mode=traditional&servers=idempotency_api",
            headers={
                "Authorization": f"Bearer {bearer}",
                **request_headers("tools/call", tool_name=name),
            },
            json={
                "jsonrpc": "2.0",
                "id": 1,
                "method": "tools/call",
                "params": {
                    "_meta": request_meta(supports_tasks=supports_tasks),
                    "name": name,
                    "arguments": arguments,
                },
            },
        )

    try:
        async with app.router.lifespan_context(app):
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app), base_url="http://gateway"
            ) as gateway:
                write_args = {
                    "idempotency_key": "task-commit-lost-response",
                    "item_id": "invoice-task-12",
                    "simulate_lost_response": True,
                }
                started = await call(
                    gateway,
                    token,
                    "idempotency_api__create_idempotent_operation",
                    write_args,
                    supports_tasks=True,
                )
                assert started.status_code == httpx.codes.OK, started.text
                task_handle = started.json()["result"]
                assert task_handle["resultType"] == "task"
                task_id = task_handle["taskId"]

                deadline = asyncio.get_running_loop().time() + 5
                polled: dict[str, Any] = {}
                while asyncio.get_running_loop().time() < deadline:
                    response = await gateway.post(
                        "/mcp",
                        headers={
                            "Authorization": f"Bearer {token}",
                            **request_headers("tasks/get", task_id=task_id),
                        },
                        json={
                            "jsonrpc": "2.0",
                            "id": 2,
                            "method": "tasks/get",
                            "params": {
                                "taskId": task_id,
                                "_meta": request_meta(supports_tasks=True),
                            },
                        },
                    )
                    assert response.status_code == httpx.codes.OK, response.text
                    polled = response.json()["result"]
                    if polled["status"] != "working":
                        break
                    await asyncio.sleep(0.05)
                assert polled["status"] == "completed"
                assert polled["result"]["structuredContent"]["result"]["status"] == "committed"

                replay = await call(
                    gateway,
                    token,
                    "idempotency_api__create_idempotent_operation",
                    write_args,
                )
                replay_result = replay.json()["result"]["structuredContent"]
                assert replay_result["operation_id"] == task_id
                assert replay_result["ok"] is True

                runtime = app.state.runtime
                configured_upstream = runtime.registry.get("idempotency_api")
                assert configured_upstream is not None
                configured_policy = configured_upstream.tool_recovery_policies[
                    "create_idempotent_operation"
                ]
                retry_policy = configured_policy.model_copy(
                    update={"status_tool": None, "status_key_argument": None}
                )
                runtime.registry._static["idempotency_api"] = configured_upstream.model_copy(
                    update={"tool_recovery_policies": {"create_idempotent_operation": retry_policy}}
                )
                retry_started = await call(
                    gateway,
                    token,
                    "idempotency_api__create_idempotent_operation",
                    {
                        "idempotency_key": "task-retry-same-key",
                        "item_id": "invoice-retry-14",
                        "simulate_lost_response": True,
                    },
                    supports_tasks=True,
                )
                retry_task_id = retry_started.json()["result"]["taskId"]
                deadline = asyncio.get_running_loop().time() + 8
                retry_task: dict[str, Any] = {}
                while asyncio.get_running_loop().time() < deadline:
                    retry_poll = await gateway.post(
                        "/mcp",
                        headers={
                            "Authorization": f"Bearer {token}",
                            **request_headers("tasks/get", task_id=retry_task_id),
                        },
                        json={
                            "jsonrpc": "2.0",
                            "id": 5,
                            "method": "tasks/get",
                            "params": {
                                "taskId": retry_task_id,
                                "_meta": request_meta(supports_tasks=True),
                            },
                        },
                    )
                    retry_task = retry_poll.json()["result"]
                    if retry_task["status"] != "working":
                        break
                    await asyncio.sleep(0.1)
                assert retry_task["status"] == "completed"
                assert retry_task["result"]["structuredContent"]["result"]["replayed"] is True

                conflict = await call(
                    gateway,
                    token,
                    "idempotency_api__create_idempotent_operation",
                    {**write_args, "item_id": "different-invoice"},
                )
                assert conflict.json()["error"]["code"] == -32602

                foreign_read = await gateway.post(
                    "/mcp",
                    headers={
                        "Authorization": f"Bearer {foreign_token}",
                        **request_headers("tasks/get", task_id=task_id),
                    },
                    json={
                        "jsonrpc": "2.0",
                        "id": 3,
                        "method": "tasks/get",
                        "params": {"taskId": task_id, "_meta": request_meta(supports_tasks=True)},
                    },
                )
                assert foreign_read.json()["error"]["code"] == -32602

                legacy_args = {
                    "idempotency_key": "legacy-commit-lost-response",
                    "item_id": "invoice-legacy-13",
                    "simulate_lost_response": True,
                }
                legacy = await call(
                    gateway,
                    token,
                    "idempotency_api__create_idempotent_operation",
                    legacy_args,
                )
                legacy_result = legacy.json()["result"]["structuredContent"]
                assert legacy_result["operation_id"]
                assert legacy_result["operation_status"] in {"pending", "working", "completed"}

                legacy_status = await call(
                    gateway,
                    token,
                    "get_operation_status",
                    {"operation_id": legacy_result["operation_id"]},
                )
                assert (
                    legacy_status.json()["result"]["structuredContent"]["operation_id"]
                    == legacy_result["operation_id"]
                )

                admin_view = await gateway.get(
                    "/admin/operations?status=all",
                    headers={"Authorization": f"Bearer {token}"},
                )
                assert admin_view.status_code == httpx.codes.OK, admin_view.text
                assert len(admin_view.json()["operations"]) == 3
                assert "payload_ciphertext" not in admin_view.text

                no_capability = await gateway.post(
                    "/mcp",
                    headers={
                        "Authorization": f"Bearer {token}",
                        **request_headers("tasks/get", task_id=task_id),
                    },
                    json={
                        "jsonrpc": "2.0",
                        "id": 4,
                        "method": "tasks/get",
                        "params": {"taskId": task_id, "_meta": request_meta()},
                    },
                )
                assert no_capability.json()["error"]["code"] == -32021

                stored = await app.state.runtime.store.get_operation(task_id)
                assert stored is not None
                assert stored["status"] == "completed"
                assert "task-commit-lost-response" not in stored["payload_ciphertext"]
    finally:
        await backend_client.aclose()


def test_recovery_configuration_requires_explicit_safe_contract() -> None:
    with pytest.raises(ValueError, match="upstream_key_retention_seconds"):
        ToolRecoveryPolicy.model_validate(
            {
                "enabled": True,
                "idempotency_key_argument": "idempotency_key",
                "synchronous_budget_seconds": 2,
                "max_attempts": 2,
                "max_recovery_age_seconds": 60,
            }
        )


async def test_sqlite_recovery_claims_are_shared_across_replicas_and_expired_leases(
    tmp_path: Path,
) -> None:
    database = tmp_path / "shared-recovery.db"
    replica_a = SQLiteAuditStore(str(database))
    replica_b = SQLiteAuditStore(str(database))
    await replica_a.init()
    now = datetime.now(UTC)
    await replica_a.create_or_get_operation(
        {
            "operation_id": "operation-shared-1",
            "tenant_id": "tenant-one",
            "requester_sub": "subject-one",
            "client_id": "client-one",
            "upstream_name": "mock_api",
            "tool_name": "write_record",
            "dedupe_key": "fingerprint-one",
            "request_digest": "digest-one",
            "payload_ciphertext": "ciphertext",
            "status": "pending",
            "attempt_count": 1,
            "max_attempts": 3,
            "next_attempt_at": now.isoformat(),
            "recovery_deadline": (now + timedelta(minutes=5)).isoformat(),
            "created_at": now.isoformat(),
            "updated_at": now.isoformat(),
            "expires_at": (now + timedelta(days=1)).isoformat(),
        }
    )
    lease_until = (now + timedelta(minutes=1)).isoformat()
    first_claim, second_claim = await asyncio.gather(
        replica_a.claim_operations(limit=1, now=now.isoformat(), lease_expires_at=lease_until),
        replica_b.claim_operations(limit=1, now=now.isoformat(), lease_expires_at=lease_until),
    )
    assert len(first_claim) + len(second_claim) == 1

    await replica_a.close()
    await replica_b.close()
    restarted = SQLiteAuditStore(str(database))
    await restarted.init()
    recovered = await restarted.claim_operations(
        limit=1,
        now=(now + timedelta(minutes=2)).isoformat(),
        lease_expires_at=(now + timedelta(minutes=3)).isoformat(),
    )
    assert len(recovered) == 1
    assert recovered[0]["operation_id"] == "operation-shared-1"
    await restarted.close()
