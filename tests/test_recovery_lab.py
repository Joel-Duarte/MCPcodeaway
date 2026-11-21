"""Golden proof and control-plane boundary tests for the in-dashboard Recovery Lab."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import httpx
import pytest

from examples.recovery_lab import run_recovery_lab
from gateway.auth import LocalDevKeyManager
from gateway.config import GatewaySettings
from gateway.server import create_gateway_app
from gateway.state import SQLiteAuditStore

_GOLDEN = json.loads((Path(__file__).parent / "golden" / "flows.json").read_text(encoding="utf-8"))[
    "recovery_lab_flows"
]


@pytest.mark.parametrize("flow", _GOLDEN, ids=lambda flow: flow["id"])
async def test_recovery_lab_scenarios_are_declared(flow: dict[str, Any]) -> None:
    assert flow["kind"] in {
        "task_status_reconciliation",
        "idempotent_replay",
        "idempotency_conflict",
        "legacy_status_fallback",
    }


async def test_recovery_lab_runs_real_mcp_flows_against_isolated_mock() -> None:
    result = await run_recovery_lab()

    assert result["status"] == "passed", result
    assert result["isolation"].startswith("Fresh in-process mock API")
    scenarios = {scenario["id"]: scenario for scenario in result["scenarios"]}
    assert set(scenarios) == {flow["id"] for flow in _GOLDEN}
    assert all(scenario["status"] == "passed" for scenario in scenarios.values())
    assert scenarios["task_recovers_lost_response"]["side_effect_count"] == 1
    assert scenarios["same_key_replay_is_deduplicated"]["side_effect_count"] == 1
    assert scenarios["changed_payload_is_rejected"]["side_effect_count"] == 1
    assert scenarios["legacy_client_uses_status_fallback"]["side_effect_count"] == 2


async def test_recovery_lab_route_requires_admin_and_is_demo_only(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    settings = GatewaySettings(
        app_env="sandbox",
        auth_mode="dev",
        profile="demo",
        require_admin_auth=True,
        upstreams_config_path=str(tmp_path / "empty-upstreams.yaml"),
        sqlite_path=str(tmp_path / "gateway.db"),
        credential_encryption_key="12" * 32,
    )
    Path(settings.upstreams_config_path).write_text("upstreams: []\n", encoding="utf-8")
    keys = LocalDevKeyManager(issuer=settings.oidc_issuer)
    app = create_gateway_app(
        settings=settings,
        store=SQLiteAuditStore(settings.sqlite_path),
        dev_keys=keys,
    )

    async def fake_recovery_lab() -> dict[str, Any]:
        return {"status": "passed", "scenarios": [], "isolation": "test"}

    monkeypatch.setattr("examples.recovery_lab.run_recovery_lab", fake_recovery_lab)
    admin_token = keys.issue_user_token(
        persona="gateway_admin",
        overrides={"roles": ["admin", "gateway_admin"]},
    )
    viewer_token = keys.issue_user_token(
        persona="gateway_admin",
        overrides={"sub": "recovery-viewer", "roles": ["operator_viewer"]},
    )

    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://gateway"
        ) as client:
            unauthenticated = await client.post("/admin/demo/recovery/run")
            assert unauthenticated.status_code == httpx.codes.UNAUTHORIZED
            forbidden = await client.post(
                "/admin/demo/recovery/run",
                headers={"Authorization": f"Bearer {viewer_token}"},
            )
            assert forbidden.status_code == httpx.codes.FORBIDDEN
            authorized = await client.post(
                "/admin/demo/recovery/run",
                headers={"Authorization": f"Bearer {admin_token}"},
            )
            assert authorized.status_code == httpx.codes.OK, authorized.text
            assert authorized.json()["status"] == "passed"
