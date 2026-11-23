"""Benchmark synthetic, local ONNX/classifier service, and LM Studio System-1 models."""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
import time
from pathlib import Path
from typing import Any

import httpx

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT / "src") not in sys.path:
    sys.path.insert(0, str(ROOT / "src"))

from gateway.classifier_service import JudgeEpisodeRequest, OnnxOrHeuristicEngine
from gateway.workflow_detector import (
    SyntheticWorkflowClassifier,
    System1JudgeVerdict,
    call_openai_compatible_judge,
)


def _compute_metrics(
    episodes: list[dict[str, Any]], results: list[System1JudgeVerdict]
) -> dict[str, Any]:
    total = len(episodes)
    correct_bool = 0
    false_credits = 0
    negatives = 0
    latencies: list[float] = []
    ece_sum = 0.0

    for ep, pred in zip(episodes, results, strict=True):
        exp_bool = bool(ep["expected_completed"])
        if pred.completed == exp_bool:
            correct_bool += 1
        if not exp_bool:
            negatives += 1
            if pred.completed:
                false_credits += 1
        target_prob = 1.0 if exp_bool else 0.0
        ece_sum += abs(pred.confidence - target_prob)
        latencies.append(pred.latency_ms)

    latencies.sort()
    p50 = latencies[len(latencies) // 2] if latencies else 0.0
    p95 = latencies[min(len(latencies) - 1, int(len(latencies) * 0.95))] if latencies else 0.0
    return {
        "episodes": total,
        "accuracy": round(correct_bool / max(total, 1), 4),
        "false_credit_rate": round(false_credits / max(negatives, 1), 4),
        "mean_calibration_gap": round(ece_sum / max(total, 1), 4),
        "p50_ms": round(p50, 2),
        "p95_ms": round(p95, 2),
    }


async def run_benchmarks(
    *,
    fixtures_path: Path,
    backends: list[str],
    lm_studio_url: str,
    lm_studio_models: list[str],
    classifier_model_dir: str | None,
) -> dict[str, Any]:
    payload = json.loads(fixtures_path.read_text(encoding="utf-8"))
    episodes = payload["episodes"]
    report: dict[str, Any] = {"fixtures": str(fixtures_path), "results": {}}

    if "synthetic_heuristic" in backends:
        clf = SyntheticWorkflowClassifier()
        preds: list[System1JudgeVerdict] = []
        for ep in episodes:
            t0 = time.perf_counter()
            verdict = clf.judge_episode(redacted_intent=ep["intent"], steps=ep["steps"])
            verdict.latency_ms = round((time.perf_counter() - t0) * 1000.0, 3)
            preds.append(verdict)
        report["results"]["synthetic_heuristic"] = {
            **_compute_metrics(episodes, preds),
            "sample_verdicts": [p.model_dump(mode="json") for p in preds[:3]],
        }

    if "classifier_service" in backends:
        engine = OnnxOrHeuristicEngine(model_dir=classifier_model_dir)
        preds = [
            engine.judge_episode(
                JudgeEpisodeRequest(redacted_intent=ep["intent"], steps=ep["steps"])
            )
            for ep in episodes
        ]
        report["results"]["classifier_service"] = {
            "onnx_loaded": engine.onnx_loaded,
            **_compute_metrics(episodes, preds),
        }

    if "lm_studio" in backends:
        async with httpx.AsyncClient() as client:
            for model in lm_studio_models:
                preds_lm: list[System1JudgeVerdict] = []
                errors = 0
                for ep in episodes:
                    try:
                        verdict = await call_openai_compatible_judge(
                            base_url=lm_studio_url,
                            model=model,
                            redacted_intent=ep["intent"],
                            steps=ep["steps"],
                            timeout_seconds=20.0,
                            app_env="dev",
                            block_private_ips_in_prod=False,
                            http_client=client,
                        )
                    except Exception:
                        errors += 1
                        verdict = System1JudgeVerdict(
                            completed=False,
                            confidence=0.0,
                            completion_pct=0,
                            verdict="abandoned",
                            backend=f"openai:{model}:error",
                        )
                    preds_lm.append(verdict)
                report["results"][f"lm_studio:{model}"] = {
                    **_compute_metrics(episodes, preds_lm),
                    "errors": errors,
                    "sample_verdicts": [p.model_dump(mode="json") for p in preds_lm[:3]],
                }
    return report


def main() -> None:
    parser = argparse.ArgumentParser(description="Benchmark System-1 workflow classifiers")
    parser.add_argument(
        "--fixtures",
        type=Path,
        default=ROOT / "benchmarks" / "workflow_classifier_fixtures.json",
    )
    parser.add_argument(
        "--backends",
        default="synthetic_heuristic,classifier_service",
        help="Comma-separated: synthetic_heuristic,classifier_service,lm_studio",
    )
    parser.add_argument("--lm-studio-url", default="http://127.0.0.1:1234/v1")
    parser.add_argument("--lm-studio-models", default="laya")
    parser.add_argument("--model-dir", default=None)
    parser.add_argument("--output", type=Path, default=None)
    parser.add_argument(
        "--thresholds",
        type=Path,
        default=ROOT / "benchmarks" / "thresholds.json",
    )
    parser.add_argument("--verify-thresholds", action="store_true")
    args = parser.parse_args()

    backends = [b.strip() for b in args.backends.split(",") if b.strip()]
    models = [m.strip() for m in args.lm_studio_models.split(",") if m.strip()]
    result = asyncio.run(
        run_benchmarks(
            fixtures_path=args.fixtures,
            backends=backends,
            lm_studio_url=args.lm_studio_url,
            lm_studio_models=models,
            classifier_model_dir=args.model_dir,
        )
    )
    encoded = json.dumps(result, indent=2)
    if args.output:
        args.output.write_text(encoded, encoding="utf-8")
    print(encoded)

    if args.verify_thresholds:
        all_thresholds = json.loads(args.thresholds.read_text(encoding="utf-8"))
        rules = all_thresholds.get("workflow_classifier") or {}
        failures: list[str] = []
        for backend_name, rule in rules.items():
            if backend_name not in result["results"]:
                failures.append(f"Missing required backend result '{backend_name}'")
                continue
            metrics = result["results"][backend_name]
            if metrics["accuracy"] < float(rule.get("min_accuracy", 0.0)):
                failures.append(
                    f"{backend_name} accuracy {metrics['accuracy']} < {rule['min_accuracy']}"
                )
            if metrics["false_credit_rate"] > float(rule.get("max_false_credit_rate", 1.0)):
                failures.append(
                    f"{backend_name} false_credit_rate {metrics['false_credit_rate']} > "
                    f"{rule['max_false_credit_rate']}"
                )
            if "max_p95_ms" in rule and metrics["p95_ms"] > float(rule["max_p95_ms"]):
                failures.append(f"{backend_name} p95_ms {metrics['p95_ms']} > {rule['max_p95_ms']}")
        if failures:
            raise SystemExit("Workflow classifier thresholds failed: " + "; ".join(failures))


if __name__ == "__main__":
    main()
