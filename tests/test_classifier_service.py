"""Tests for standalone CPU System-1 workflow classifier microservice."""

from __future__ import annotations

import httpx
import pytest

from gateway.classifier_service import create_classifier_app


@pytest.mark.asyncio
async def test_classifier_service_auth_and_dual_output_verdict() -> None:
    """Classifier enforces secret auth and returns boolean + calibrated percentage verdicts."""
    app = create_classifier_app(
        classifier_secret="active-secret-12345678901234567890,previous-secret-12345678901234567890",
        allow_unauthenticated=False,
        model_dir="/nonexistent-models-dir",
    )
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://classifier"
    ) as client:
        health = await client.get("/health")
        assert health.status_code == 200
        assert health.json()["status"] == "ok"

        unauth = await client.post("/v1/judge-episode", json={"steps": []})
        assert unauth.status_code == 401

        matched = await client.post(
            "/v1/match-intent",
            headers={"Authorization": "Bearer previous-secret-12345678901234567890"},
            json={
                "redacted_intent": "Update employee role and verify",
                "observed_tools": [
                    "hr_directory__get_hr_employee",
                    "hr_directory__update_hr_employee",
                ],
                "candidates": [
                    {
                        "workflow_id": "hr_directory/update_profile",
                        "name": "Update employee profile",
                        "description": "Read and update HR employee role",
                        "participating_tools": [
                            "hr_directory__get_hr_employee",
                            "hr_directory__update_hr_employee",
                        ],
                    }
                ],
            },
        )
        assert matched.status_code == 200
        assert matched.json()["matches"][0]["workflow_id"] == "hr_directory/update_profile"

        judged = await client.post(
            "/v1/judge-episode",
            headers={"X-Classifier-Secret": "active-secret-12345678901234567890"},
            json={
                "redacted_intent": "Update employee role and confirm change",
                "steps": [
                    {
                        "tool_qualified_name": "hr_directory__get_hr_employee",
                        "effect": "read",
                        "status": "succeeded",
                        "shape_diff": {"readback_verified": False},
                    },
                    {
                        "tool_qualified_name": "hr_directory__update_hr_employee",
                        "effect": "write",
                        "status": "succeeded",
                        "sensitive_write_journal_status": "succeeded",
                        "shape_diff": {"readback_verified": False},
                    },
                    {
                        "tool_qualified_name": "hr_directory__get_hr_employee",
                        "effect": "read",
                        "status": "succeeded",
                        "shape_diff": {"readback_verified": True},
                    },
                ],
            },
        )
        assert judged.status_code == 200
        payload = judged.json()
        assert payload["completed"] is True
        assert 0.8 <= payload["confidence"] <= 1.0
        assert 80 <= payload["completion_pct"] <= 100
        assert payload["verdict"] == "completed_verified"
