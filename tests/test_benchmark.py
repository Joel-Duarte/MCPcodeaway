"""Phase 4 tests: Deterministic, scaling, load, and dry-run live LLM benchmarks."""

from __future__ import annotations

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
