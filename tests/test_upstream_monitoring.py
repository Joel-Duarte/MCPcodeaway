from __future__ import annotations

from typing import cast

import httpx
import pytest

from gateway.alerts import AlertEvaluator
from gateway.auth import CallerIdentity
from gateway.config import GatewaySettings, UpstreamConfig, UpstreamHealthCheckConfig
from gateway.errors import classify_upstream_error
from gateway.monitoring import UpstreamHealthMonitor
from gateway.server import GatewayRuntime
from gateway.upstream_clients import DiscoveredTool, UpstreamClientManager


def test_upstream_errors_are_classified_without_leaking_transport_details() -> None:
    failure = classify_upstream_error(
        httpx.ConnectTimeout("secret-host: internal-api"), operation_is_read=True
    )
    assert failure.error_type == "upstream_timeout"
    assert failure.retryable is True
    assert "secret-host" not in failure.message

    write_failure = classify_upstream_error(httpx.ReadTimeout("timed out"), operation_is_read=False)
    assert write_failure.outcome_unknown is True
    assert write_failure.retryable is False


def test_health_config_requires_explicit_safe_shape() -> None:
    with pytest.raises(ValueError, match="require an explicit"):
        UpstreamHealthCheckConfig(enabled=True)
    with pytest.raises(ValueError, match="query parameters"):
        UpstreamHealthCheckConfig(enabled=True, url="https://api.example/health?token=secret")


@pytest.mark.asyncio
async def test_health_monitor_requires_thresholds_to_recover_and_fail() -> None:
    class FakeClient:
        failing = False

        async def probe_health(self, _url: str, *, timeout_seconds: float) -> object:
            assert timeout_seconds == 0.5
            if self.failing:
                raise httpx.ConnectError("offline")
            return httpx.Response(200, request=httpx.Request("GET", "https://example.test/health"))

    client = FakeClient()
    monitor = UpstreamHealthMonitor(cast(UpstreamClientManager, client))
    upstream = UpstreamConfig(
        name="probe_target",
        type="mcp_http",
        url="https://example.test/mcp",
        auth_type="none",
        health_check=UpstreamHealthCheckConfig(
            enabled=True,
            url="https://example.test/health",
            timeout_seconds=0.5,
            failure_threshold=2,
            success_threshold=2,
        ),
    )

    assert (await monitor.check(upstream))["status"] == "healthy"
    assert not monitor.is_circuit_open(upstream.name)
    client.failing = True
    assert (await monitor.check(upstream))["status"] == "degraded"
    assert (await monitor.check(upstream))["status"] == "unhealthy"
    assert monitor.is_circuit_open(upstream.name)
    client.failing = False
    assert (await monitor.check(upstream))["status"] == "unhealthy"
    assert (await monitor.check(upstream))["status"] == "healthy"
    assert not monitor.is_circuit_open(upstream.name)


@pytest.mark.asyncio
async def test_dispatcher_retries_read_once_and_never_retries_write(tmp_path) -> None:
    runtime = GatewayRuntime(
        settings=GatewaySettings(profile="demo", sqlite_path=str(tmp_path / "retry.db")),
        preloaded_tools={},
    )
    identity = CallerIdentity(
        sub="admin", tenant_id="tenant-acme-dev", roles=["admin"], scopes=["mcp.write"]
    )

    async def call_case(name: str, *, effect: str, mutating: bool, failures: int):
        cfg = UpstreamConfig(
            name=name,
            type="mcp_http",
            url=f"https://{name}.example.test/mcp",
            auth_type="none",
            allow_all_callers=True,
            allowed_operations=["run"],
            tool_effects={"run": effect},
            mutating_tools=["run"] if mutating else [],
        )
        runtime.registry.upsert(cfg)
        tool = DiscoveredTool(
            server=name,
            name="run",
            qualified_name=f"{name}__run",
            input_schema={"type": "object", "properties": {}},
            output_schema={"type": "object"},
            mutating=mutating,
        )
        calls = 0

        async def fake_call(*_args, **_kwargs):
            nonlocal calls
            calls += 1
            if calls <= failures:
                raise httpx.ConnectError("connection refused")
            return {"done": True}

        original = runtime.client_manager.call_tool
        runtime.client_manager.call_tool = fake_call
        try:
            result, record = await runtime.dispatcher.invoke_tool(identity, tool, cfg, {})
        finally:
            runtime.client_manager.call_tool = original
        return calls, result, record

    read_calls, read_result, read_record = await call_case(
        "read_target", effect="read", mutating=False, failures=1
    )
    assert read_calls == 2
    assert read_result == {"done": True}
    assert read_record.retry_attempts == 1
    assert read_record.status == "ok"

    write_calls, write_result, write_record = await call_case(
        "write_target", effect="write", mutating=True, failures=1
    )
    assert write_calls == 1
    assert write_record.status == "error"
    assert write_record.error_type == "upstream_unreachable"
    assert write_record.retry_attempts == 0
    assert write_record.outcome_unknown is False
    assert "_dispatch_error" in write_result

    circuit_cfg = UpstreamConfig(
        name="circuit_target",
        type="mcp_http",
        url="https://circuit.example.test/mcp",
        auth_type="none",
        allow_all_callers=True,
        allowed_operations=["run"],
        tool_effects={"run": "read"},
        health_check=UpstreamHealthCheckConfig(
            enabled=True, url="https://circuit.example.test/health"
        ),
    )
    circuit_tool = DiscoveredTool(
        server="circuit_target",
        name="run",
        qualified_name="circuit_target__run",
        input_schema={"type": "object", "properties": {}},
        mutating=False,
    )
    runtime.upstream_health.statuses[circuit_cfg.name] = {"status": "unhealthy"}
    circuit_calls = 0

    async def circuit_call(*_args, **_kwargs):
        nonlocal circuit_calls
        circuit_calls += 1
        return {"unexpected": True}

    original = runtime.client_manager.call_tool
    runtime.client_manager.call_tool = circuit_call
    try:
        circuit_result, circuit_record = await runtime.dispatcher.invoke_tool(
            identity, circuit_tool, circuit_cfg, {}
        )
    finally:
        runtime.client_manager.call_tool = original
    assert circuit_calls == 0
    assert circuit_record.error_type == "upstream_unhealthy"
    assert "_dispatch_error" in circuit_result
    await runtime.shutdown()


def test_high_error_rate_alert_includes_error_category_breakdown() -> None:
    alerts = AlertEvaluator().evaluate(
        runs=[
            {
                "ok": False,
                "subcalls": [{"server": "billing", "error_type": "upstream_timeout"}],
            }
            for _ in range(5)
        ],
        drift_warnings=[],
        startup_warnings=[],
        worker_status={},
    )
    alert = next(alert for alert in alerts if alert["id"] == "high_error_rate")
    assert alert["error_types"] == {"upstream_timeout": 5}
    assert alert["upstream_error_types"] == {"billing": {"upstream_timeout": 5}}
