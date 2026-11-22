"""Tenant-scoped reporting for credited business-value events."""

from __future__ import annotations

import pytest

from gateway.telemetry import TelemetryRecorder


class _EventStore:
    def __init__(self, events: list[dict], executions: list[dict] | None = None) -> None:
        self.events = events
        self.executions = executions or []
        self.request: tuple[str, str | None, int] | None = None

    async def list_business_value_events(
        self, tenant_id: str, *, since: str | None = None, limit: int = 1000
    ) -> list[dict]:
        self.request = (tenant_id, since, limit)
        return [
            event
            for event in self.events
            if event["tenant_id"] == tenant_id and (since is None or event["created_at"] >= since)
        ][:limit]

    async def list_business_executions(
        self, tenant_id: str, *, since: str | None = None, limit: int = 1000
    ) -> list[dict]:
        return [
            execution
            for execution in self.executions
            if execution["tenant_id"] == tenant_id
            and (since is None or execution["created_at"] >= since)
        ][:limit]


def _event(
    event_id: str,
    *,
    tenant_id: str = "tenant-a",
    selected_id: str = "support_case",
    revision: str = "3",
    confidence: str = "validated",
    review_minutes: dict | None = None,
    correction_minutes: dict | None = None,
    rate: float | None = 30.0,
    currency: str | None = "EUR",
    created_at: str = "2026-11-22T10:30:00+01:00",
) -> dict:
    baseline = {
        "manual_baseline_minutes": 10,
        "baseline_source": "sampled_manual_measurement",
        "confidence": confidence,
        "loaded_labor_cost_per_hour": rate,
        "currency": currency,
        "effective_from": "2026-01-01",
    }
    if review_minutes is not None:
        baseline["review_minutes"] = review_minutes
    if correction_minutes is not None:
        baseline["correction_minutes"] = correction_minutes
    return {
        "event_id": event_id,
        "tenant_id": tenant_id,
        "business_execution_id": f"execution-{event_id}",
        "completion_key": "business-value-completion-v1",
        "upstream_name": "support_api",
        "credited": 1,
        "created_at": created_at,
        "evidence": {
            "definition_revision": revision,
            "integration_id": "support_desk",
            "integration_display_name": "Support Desk",
            "selected_kind": "workflow",
            "selected_id": selected_id,
            "baseline_snapshot": baseline,
        },
    }


@pytest.mark.asyncio
async def test_business_value_report_pins_baselines_and_separates_gross_from_savings() -> None:
    store = _EventStore(
        [
            _event(
                "a1",
                review_minutes={"kind": "expected_average", "minutes": 2},
                correction_minutes={"kind": "expected_average", "minutes": 1},
            ),
            _event(
                "a2",
                review_minutes={"kind": "expected_average", "minutes": 2},
                correction_minutes={"kind": "expected_average", "minutes": 1},
            ),
            _event("b1", selected_id="other_flow", revision="4", confidence="unvalidated"),
            _event("foreign", tenant_id="tenant-b"),
        ],
        [
            {
                "tenant_id": "tenant-a",
                "created_at": "2026-11-22",
                "status": status,
                "expires_at": "2999-01-01T00:00:00+00:00",
            }
            for status in ("active", "completed", "outcome_unknown", "failed", "expired")
        ]
        + [
            {
                "tenant_id": "tenant-a",
                "created_at": "2026-11-22",
                "status": "active",
                "expires_at": "2000-01-01T00:00:00+00:00",
            }
        ]
        + [{"tenant_id": "tenant-b", "created_at": "2026-11-22", "status": "completed"}],
    )

    report = await TelemetryRecorder(store).business_value_report(
        tenant_id="tenant-a", since="2026-11-22", limit=100
    )

    assert store.request == ("tenant-a", "2026-11-22", 100)
    assert report["tenant_scope"] == "tenant-a"
    assert report["completed_count"] == 3
    assert report["execution_status_counts"] == {
        "active": 1,
        "completed": 1,
        "outcome_unknown": 1,
        "failed": 1,
        "expired": 2,
    }
    assert report["validated_minutes_saved"] is None
    assert report["cost_coverage"]["roi_available"] is False
    assert "llm_inference" in report["cost_coverage"]["missing_categories"]
    known = next(
        item
        for item in report["by_integration_workflow_day"]
        if item["selected_id"] == "support_case"
    )
    assert known["integration_id"] == "support_desk"
    assert known["integration_display_name"] == "Support Desk"
    assert known["integration_upstream_name"] == "support_api"
    assert known["selected_kind"] == "workflow"
    assert known["definition_revision"] == "3"
    assert known["period_utc"] == "2026-11-22"
    assert known["completed_count"] == 2
    assert known["estimated_manual_baseline_minutes"] == 20
    assert known["validated_manual_baseline_minutes"] == 20
    assert known["estimated_minutes_saved"] == 14
    assert known["estimated_labor_value"] == {"amount": 7.0, "currency": "EUR"}
    unknown = next(
        item
        for item in report["by_integration_workflow_day"]
        if item["selected_id"] == "other_flow"
    )
    assert unknown["estimated_manual_baseline_minutes"] == 10
    assert unknown["validated_manual_baseline_minutes"] == 0
    assert unknown["estimated_minutes_saved"] is None
    assert unknown["estimated_labor_value"] is None


@pytest.mark.asyncio
async def test_business_value_report_requires_a_tenant_scope() -> None:
    recorder = TelemetryRecorder(_EventStore([]))
    with pytest.raises(ValueError, match="explicit tenant scope"):
        await recorder.business_value_report(tenant_id="")
