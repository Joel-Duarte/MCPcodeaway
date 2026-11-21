"""Run an isolated end-to-end demonstration of MCP write recovery."""

from __future__ import annotations

import asyncio
import json
import secrets
import tempfile
from pathlib import Path
from typing import Any

import httpx

from examples.mock_backends import create_mock_backends_app
from gateway.auth import LocalDevKeyManager
from gateway.config import GatewaySettings, UpstreamConfig
from gateway.mcp_protocol import request_headers, request_meta
from gateway.server import create_gateway_app
from gateway.state import SQLiteAuditStore

_WRITE_TOOL = "idempotency_api__create_idempotent_operation"
_STATUS_TOOL = "idempotency_api__get_idempotent_operation_status"
_METRICS_TOOL = "idempotency_api__get_idempotent_operation_metrics"


def _scenario(
    scenario_id: str,
    passed: bool,
    detail: str,
    *,
    operation_id: str | None = None,
    task_id: str | None = None,
    side_effect_count: int | None = None,
) -> dict[str, Any]:
    return {
        "id": scenario_id,
        "status": "passed" if passed else "failed",
        "detail": detail,
        "operation_id": operation_id,
        "task_id": task_id,
        "side_effect_count": side_effect_count,
    }


async def run_recovery_lab() -> dict[str, Any]:
    """Exercise Tasks and legacy recovery against a fresh in-process mock API."""
    with tempfile.TemporaryDirectory(prefix="mcp-recovery-lab-") as temporary_directory:
        directory = Path(temporary_directory)
        mock_app = create_mock_backends_app(allow_unauthenticated_idempotency_api=True)
        upstream = UpstreamConfig.model_validate(
            {
                "name": "idempotency_api",
                "type": "openapi",
                "url": "http://recovery-mock",
                "auth_type": "none",
                "openapi_spec": mock_app.openapi(),
                "allowed_roles": ["admin", "gateway_admin"],
                "allowed_groups": ["platform"],
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
                        "max_recovery_age_seconds": 60,
                        "upstream_key_retention_seconds": 300,
                        "status_tool": "get_idempotent_operation_status",
                        "status_key_argument": "idempotency_key",
                        "status_field": "status",
                    }
                },
                "request_timeout_seconds": 3,
            }
        )
        config_path = directory / "upstreams.yaml"
        config_path.write_text(
            json.dumps({"upstreams": [upstream.model_dump(mode="json", exclude_none=True)]}),
            encoding="utf-8",
        )
        settings = GatewaySettings(
            app_env="sandbox",
            auth_mode="dev",
            profile="demo",
            upstreams_config_path=str(config_path),
            sqlite_path=str(directory / "gateway.db"),
            provenance_signing_secret=secrets.token_urlsafe(32),
            credential_encryption_key=secrets.token_hex(32),
            task_dedupe_secret=secrets.token_urlsafe(48),
            task_worker_poll_interval_seconds=0.1,
        )
        keys = LocalDevKeyManager(issuer=settings.oidc_issuer)
        token = keys.issue_user_token(
            persona="gateway_admin",
            overrides={
                "roles": ["admin", "gateway_admin"],
                "groups": ["platform"],
                "scopes": ["mcp.tools", "mcp.write"],
            },
        )
        mock_client = httpx.AsyncClient(transport=httpx.ASGITransport(app=mock_app))
        app = create_gateway_app(
            settings=settings,
            store=SQLiteAuditStore(settings.sqlite_path),
            dev_keys=keys,
            http_client=mock_client,
        )
        scenarios: list[dict[str, Any]] = []

        async def call_tool(
            client: httpx.AsyncClient,
            tool_name: str,
            arguments: dict[str, Any],
            *,
            supports_tasks: bool = False,
        ) -> dict[str, Any]:
            response = await client.post(
                "/mcp?mode=traditional&servers=idempotency_api",
                headers={
                    "Authorization": f"Bearer {token}",
                    **request_headers("tools/call", tool_name=tool_name),
                },
                json={
                    "jsonrpc": "2.0",
                    "id": secrets.randbelow(1_000_000) + 1,
                    "method": "tools/call",
                    "params": {
                        "_meta": request_meta(
                            client_name="recovery-lab", supports_tasks=supports_tasks
                        ),
                        "name": tool_name,
                        "arguments": arguments,
                    },
                },
            )
            response.raise_for_status()
            return response.json()

        async def side_effect_count(client: httpx.AsyncClient) -> int:
            response = await call_tool(client, _METRICS_TOOL, {})
            result = response.get("result", {}).get("structuredContent", {})
            nested_result = result.get("result", {})
            return int(nested_result.get("side_effect_count", -1))

        try:
            async with app.router.lifespan_context(app):
                async with httpx.AsyncClient(
                    transport=httpx.ASGITransport(app=app), base_url="http://gateway"
                ) as gateway:
                    first_arguments = {
                        "idempotency_key": f"recovery-lab-{secrets.token_hex(12)}",
                        "item_id": "demo-invoice-001",
                        "simulate_lost_response": True,
                    }
                    first_response = await call_tool(
                        gateway,
                        _WRITE_TOOL,
                        first_arguments,
                        supports_tasks=True,
                    )
                    task = first_response.get("result", {})
                    task_id = task.get("taskId")
                    task_snapshot: dict[str, Any] = task
                    deadline = asyncio.get_running_loop().time() + 5
                    while task_id and asyncio.get_running_loop().time() < deadline:
                        poll = await gateway.post(
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
                                    "_meta": request_meta(
                                        client_name="recovery-lab", supports_tasks=True
                                    ),
                                },
                            },
                        )
                        poll.raise_for_status()
                        task_snapshot = poll.json().get("result", {})
                        if task_snapshot.get("status") != "working":
                            break
                        poll_interval = max(
                            0.1,
                            float(task_snapshot.get("pollIntervalMs", 1000)) / 1000,
                        )
                        await asyncio.sleep(poll_interval)

                    first_count = await side_effect_count(gateway)
                    stored_result = (
                        task_snapshot.get("result", {})
                        .get("structuredContent", {})
                        .get("result", {})
                    )
                    scenarios.append(
                        _scenario(
                            "task_recovers_lost_response",
                            bool(
                                task_id
                                and task_snapshot.get("status") == "completed"
                                and stored_result.get("status") == "committed"
                                and first_count == 1
                            ),
                            "The mock committed once, dropped the reply, and the gateway "
                            f"reported task status '{task_snapshot.get('status', 'missing')}'.",
                            operation_id=task_id,
                            task_id=task_id,
                            side_effect_count=first_count,
                        )
                    )

                    replay = await call_tool(gateway, _WRITE_TOOL, first_arguments)
                    replay_content = replay.get("result", {}).get("structuredContent", {})
                    replay_count = await side_effect_count(gateway)
                    scenarios.append(
                        _scenario(
                            "same_key_replay_is_deduplicated",
                            bool(
                                replay_content.get("operation_id") == task_id
                                and replay_content.get("ok") is True
                                and replay_count == 1
                            ),
                            "Repeating the same key and payload returned the prior operation "
                            "without another side effect.",
                            operation_id=task_id,
                            side_effect_count=replay_count,
                        )
                    )

                    conflict = await call_tool(
                        gateway,
                        _WRITE_TOOL,
                        {**first_arguments, "item_id": "different-demo-invoice"},
                    )
                    conflict_count = await side_effect_count(gateway)
                    conflict_error = conflict.get("error", {}).get("code") == -32602
                    scenarios.append(
                        _scenario(
                            "changed_payload_is_rejected",
                            bool(conflict_error and conflict_count == 1),
                            "Reusing the same key with changed arguments was rejected before "
                            "dispatch.",
                            operation_id=task_id,
                            side_effect_count=conflict_count,
                        )
                    )

                    legacy_arguments = {
                        "idempotency_key": f"recovery-lab-legacy-{secrets.token_hex(12)}",
                        "item_id": "demo-invoice-002",
                        "simulate_lost_response": True,
                    }
                    legacy = await call_tool(gateway, _WRITE_TOOL, legacy_arguments)
                    legacy_content = legacy.get("result", {}).get("structuredContent", {})
                    legacy_operation_id = legacy_content.get("operation_id")
                    legacy_status = (
                        await call_tool(
                            gateway,
                            "get_operation_status",
                            {"operation_id": str(legacy_operation_id)},
                        )
                        if legacy_operation_id
                        else {}
                    )
                    status_content = legacy_status.get("result", {}).get("structuredContent", {})
                    final_count = await side_effect_count(gateway)
                    scenarios.append(
                        _scenario(
                            "legacy_client_uses_status_fallback",
                            bool(
                                legacy_operation_id
                                and legacy_content.get("operation_status")
                                in {"pending", "working", "completed"}
                                and status_content.get("operation_id") == legacy_operation_id
                                and final_count == 2
                            ),
                            "A client without Tasks received an operation ID and retrieved "
                            "status with the legacy tool.",
                            operation_id=str(legacy_operation_id) if legacy_operation_id else None,
                            side_effect_count=final_count,
                        )
                    )
        finally:
            await mock_client.aclose()

        return {
            "status": "passed"
            if all(item["status"] == "passed" for item in scenarios)
            else "failed",
            "scenarios": scenarios,
            "isolation": (
                "Fresh in-process mock API and temporary SQLite database; "
                "no configured upstream was called."
            ),
        }
