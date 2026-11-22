"""Phase 4 tests: Deterministic, scaling, load, and dry-run live LLM benchmarks."""

from __future__ import annotations

import asyncio

from benchmarks import live_llm_bench
from benchmarks.live_llm_bench import main as live_bench_main
from benchmarks.live_llm_bench import run_live_bench
from benchmarks.load_benchmark import run_load_benchmark
from benchmarks.run_benchmark import run_offline_benchmark


async def test_offline_scaling_and_enterprise_benchmark(tmp_path) -> None:
    out_file = tmp_path / "results.json"
    report = await run_offline_benchmark(output_path=out_file)
    assert out_file.exists()

    by_count = {row["tool_count"]: row for row in report["scaling"]}
    assert by_count[300]["reduction_pct"] >= 80.0
    assert by_count[500]["codemode_surface_tokens"] == by_count[20]["codemode_surface_tokens"]

    by_scenario = {s["scenario"]: s for s in report["scenarios"]}
    rec = by_scenario["reconcile_overdue"]
    assert rec["code_mode"]["ok"]
    assert rec["code_mode"]["round_trips"] <= 2
    assert rec["classic"]["round_trips"] >= 4
    assert rec["code_mode"]["total_context_tokens"] < rec["classic"]["total_context_tokens"]
    assert rec["deltas"]["payload_bytes_saved"] > 0

    tb = by_scenario["trial_balance_audit"]
    assert tb["code_mode"]["ok"]
    assert tb["code_mode"]["round_trips"] < tb["classic"]["round_trips"]


async def test_concurrent_load_benchmark_and_live_dry_run(tmp_path) -> None:
    load_file = tmp_path / "load_results.json"
    report = await run_load_benchmark(num_users=12, concurrency=6, output_path=load_file)
    assert load_file.exists()

    for key in ("traditional", "codemode"):
        stats = report[key]
        assert stats["success_rate_pct"] == 100.0
        assert stats["token_cache_hit_rate_pct"] > 0.0
        assert stats["throughput_rps"] > 0.0

    dry = await run_live_bench(None, None, True)
    assert dry["dry_run"]


def test_live_bench_cli_dry_run() -> None:
    assert live_bench_main(["--dry-run"]) == 0


async def test_classic_parallel_tool_calls_are_dispatched_concurrently(monkeypatch) -> None:
    all_calls_started = asyncio.Event()
    started: list[str] = []

    async def fake_call_mcp_tool(
        _gw, _token, _mode, _request_id, name, _args, **_kwargs
    ) -> dict[str, str]:
        started.append(name)
        if len(started) == 3:
            all_calls_started.set()
        await asyncio.wait_for(all_calls_started.wait(), timeout=1)
        return {"tool": name}

    monkeypatch.setattr(live_llm_bench, "_call_mcp_tool", fake_call_mcp_tool)
    tool_calls = [
        {
            "id": f"call-{index}",
            "function": {"name": f"tool-{index}", "arguments": "{}"},
        }
        for index in range(3)
    ]

    dispatched = await live_llm_bench._dispatch_openai_tool_calls(
        None,
        "test-token",
        "traditional",
        tool_calls,
        request_id_start=0,
        parallel=True,
    )

    assert set(started) == {"tool-0", "tool-1", "tool-2"}
    assert [row[0]["id"] for row in dispatched] == ["call-0", "call-1", "call-2"]
    assert [row[2] for row in dispatched] == [
        {"tool": "tool-0"},
        {"tool": "tool-1"},
        {"tool": "tool-2"},
    ]


def test_parallel_classic_calls_excludes_codemode_and_mutating_workflows() -> None:
    assert live_llm_bench._parallel_classic_calls_allowed("classic", "standard")
    assert not live_llm_bench._parallel_classic_calls_allowed("codemode", "standard")
    assert not live_llm_bench._parallel_classic_calls_allowed("classic", "mutating_ticket_workflow")


async def test_live_suite_uses_fresh_gateway_per_model_and_tool_cohort(monkeypatch) -> None:
    async def fake_auth_check(_gw, _dev_keys):
        return {"all_passed": True}

    async def fake_llm_loop(_gw, _token, _api_key, mode, model, **kwargs):
        if "task_name" not in kwargs or kwargs["task_name"] == "mutating_ticket_workflow":
            return {"answer": "blocked", "subcalls_total": 0}
        return {
            "model": model,
            "task": kwargs["task_name"],
            "mode": mode,
            "execution": "classic",
            "discovery_mode": "upfront",
            "discovery_backend": "bm25",
            "correct": True,
            "audit_evidence_verified": True,
            "prompt_tokens": 100,
            "completion_tokens": 10,
            "duration_ms": 10,
            "subcalls_total": 1,
            "turns": 2,
            "self_corrections": 0,
            "run_ids": [],
        }

    monkeypatch.setattr(live_llm_bench, "run_auth_rbac_check", fake_auth_check)
    monkeypatch.setattr(live_llm_bench, "_call_openai_loop", fake_llm_loop)

    report = await live_llm_bench.run_openai_compatible_suite(
        base_url="http://model.invalid/v1",
        api_key="local-test-key",
        models=["model-a", "model-b"],
        synthetic_counts=[0, 1],
        modes=["traditional"],
        tasks=["standard"],
        concurrency=1,
        output_path=None,
    )

    assert report["auth_preflight"]["all_passed"] is True
    assert len(report["runs"]) == 4
    assert {run["model"] for run in report["runs"]} == {"model-a", "model-b"}
    assert {run["extra_synthetic_tools"] for run in report["runs"]} == {0, 1}
