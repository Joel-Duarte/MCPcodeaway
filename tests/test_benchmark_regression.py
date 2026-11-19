"""Locked benchmark regression tests verifying performance bounds and threshold failures."""

from __future__ import annotations

import copy
import json
from pathlib import Path

from benchmarks.run_benchmark import run_offline_benchmark, verify_benchmark_thresholds

_THRESHOLDS_PATH = Path("benchmarks/thresholds.json")


async def test_locked_benchmark_thresholds_hold(tmp_path: Path) -> None:
    thresholds = json.loads(_THRESHOLDS_PATH.read_text(encoding="utf-8"))
    report = await run_offline_benchmark(output_path=tmp_path / "bench.json")
    failures = verify_benchmark_thresholds(report, thresholds)
    assert failures == [], f"Benchmark threshold regressions: {failures}"


async def test_threshold_verifier_catches_synthetic_regression() -> None:
    thresholds = json.loads(_THRESHOLDS_PATH.read_text(encoding="utf-8"))
    baseline = await run_offline_benchmark(output_path=None)
    broken = copy.deepcopy(baseline)
    broken["scaling"][-1]["reduction_pct"] = 10.0
    broken["scenarios"][0]["deltas"]["context_token_savings_pct"] = 5.0
    broken["scenarios"][0]["winner"] = "classic"

    failures = verify_benchmark_thresholds(broken, thresholds)
    assert len(failures) >= 3
    assert any("scaling[500]" in msg for msg in failures)
    assert any("reconcile_overdue" in msg for msg in failures)
