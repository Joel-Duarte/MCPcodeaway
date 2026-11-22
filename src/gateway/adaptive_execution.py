"""Paired workflow evidence and deterministic adaptive routing decisions."""

from __future__ import annotations

from collections import defaultdict
from datetime import UTC, datetime, timedelta
from typing import Any

WORKLOADS = {
    "single_read",
    "small_fanout",
    "large_fanout",
    "data_reconciliation",
    "large_data_reconciliation",
    "batch_compute",
    "sequential_workflow",
    "mutating_workflow",
    "large_payload_processing",
}
EVIDENCE_VERSION = 1
MAX_IMPORT_CLOCK_SKEW = timedelta(minutes=5)


def infer_workload(intent: str | None, tools: list[Any]) -> str | None:
    text = (intent or "").lower()
    if not text:
        return None
    if any(x in text for x in ("update", "write", "create", "delete", "mutate")):
        return None
    scoped = [t for t in tools if not getattr(t, "mutating", False)]
    count = len(scoped)
    if any(x in text for x in ("reconcile", "join", "cross-check", "compare")):
        large = any(x in text for x in ("large", "bulk", "all records", "all rows", "many"))
        return "large_data_reconciliation" if large else "data_reconciliation"
    if any(x in text for x in ("batch", "aggregate", "calculate", "compute")):
        return "batch_compute"
    if any(x in text for x in ("large payload", "process file", "document")):
        return "large_payload_processing"
    if any(x in text for x in ("then", "after that", "workflow", "sequence")):
        return "sequential_workflow"
    if any(x in text for x in ("fan out", "across all", "query all", "from every")):
        return (
            "large_fanout"
            if any(x in text for x in ("large", "all", "every", "bulk"))
            else "small_fanout"
        )
    if any(x in text for x in ("single read", "look up", "fetch one", "read one")) and count > 0:
        return "single_read"
    return None


def _quality(rows: list[dict[str, Any]]) -> tuple[float, float]:
    n = len(rows)
    if not n:
        return 0.0, 0.0
    correct = sum(bool(r.get("correct")) for r in rows) / n
    grounded = sum(bool(r.get("grounded")) for r in rows) / n
    return correct, grounded


def summarize_pairs(
    evidence: list[dict[str, Any]],
    *,
    now: datetime | None = None,
    days: int | None = 30,
    pricing: dict[str, Any] | None = None,
) -> list[dict[str, Any]]:
    """Summarize only stable, recent, fully matched model/workload cohorts."""
    now = now or datetime.now(UTC)
    pricing = pricing or {}
    groups: dict[tuple[str, str], dict[str, list[dict[str, Any]]]] = defaultdict(
        lambda: defaultdict(list)
    )
    for item in evidence:
        if item.get("version") != EVIDENCE_VERSION or item.get("workload") not in WORKLOADS:
            continue
        try:
            created = datetime.fromisoformat(str(item["created_at"]).replace("Z", "+00:00"))
        except (KeyError, TypeError, ValueError):
            continue
        if created.tzinfo is None or created > now:
            continue
        if days is not None and created < now - timedelta(seconds=days * 86400):
            continue
        key = (str(item.get("model") or ""), str(item["workload"]))
        repeat_key = str(item.get("repeat_id") or item.get("evidence_id") or "")
        import json

        fixture = str(item.get("fixture_id") or "")
        config = json.dumps(
            item.get("discovery_config") or {}, sort_keys=True, separators=(",", ":"), default=str
        )
        groups[key][f"{item.get('scenario_id') or ''}:{repeat_key}:{fixture}:{config}"].append(item)
    out = []
    for (model, workload), scenarios in groups.items():
        arms: dict[str, list[dict[str, Any]]] = defaultdict(list)
        for paired in scenarios.values():
            by_mode = {str(x.get("mode")): x for x in paired}
            if "traditional" in by_mode and "codemode" in by_mode:
                arms["traditional"].append(by_mode["traditional"])
                arms["codemode"].append(by_mode["codemode"])
        row: dict[str, Any] = {
            "model": model,
            "workload": workload,
            "pairs": len(arms["traditional"]),
            "latest_evidence_at": max(
                (str(x.get("created_at") or "") for group in scenarios.values() for x in group),
                default=None,
            ),
            "qualification": "qualified" if len(arms["traditional"]) >= 5 else "sparse",
            "evidence_tier": "exact",
        }
        if min(map(len, arms.values()), default=0) == 0:
            out.append(
                {
                    **row,
                    "traditional": None,
                    "codemode": None,
                    "recommended_mode": None,
                    "reason": "No complete matched pairs.",
                }
            )
            continue
        for arm in ("traditional", "codemode"):
            rs = arms[arm]
            correct, grounded = _quality(rs)
            row[arm] = {
                "correct_pct": round(correct * 100, 2),
                "grounded_pct": round(grounded * 100, 2),
                "verified_pct": round(
                    sum(bool(r.get("correct") and r.get("grounded")) for r in rs) * 100 / len(rs), 2
                ),
                "mean_cost_usd": _mean_cost_usd(rs, model, pricing),
                "mean_provider_tokens": _mean_provider_tokens(rs),
                "verified_completions": sum(
                    bool(r.get("correct") and r.get("grounded")) for r in rs
                ),
                "attempts": len(rs),
                "p95_wall_ms": _percentile([r.get("wall_ms") for r in rs], 0.95),
                "mean_upstream_calls": _mean_known(rs, "upstream_calls"),
                "mean_recovery": _mean_known(rs, "recovery_count"),
            }
        a, b = row["traditional"], row["codemode"]
        if row["pairs"] < 5:
            row.update(
                recommended_mode=None, reason="Fewer than five matched repeats; report only."
            )
            out.append(row)
            continue
        floor_a = a["verified_pct"] >= 90
        floor_b = b["verified_pct"] >= 90
        winner = None
        reason = "No arm meets the 90% jointly correct and grounded floor."
        if floor_a or floor_b:
            if floor_a != floor_b:
                winner = "traditional" if floor_a else "codemode"
                reason = (
                    f"{winner} is the only arm meeting the joint correctness and grounding floor."
                )
            elif a["verified_pct"] != b["verified_pct"]:
                winner = "traditional" if a["verified_pct"] > b["verified_pct"] else "codemode"
                reason = "Higher verified quality takes precedence."
            else:
                costs = [a["mean_cost_usd"], b["mean_cost_usd"]]
                cost_basis = "USD"
                if not all(x is not None and x > 0 for x in costs):
                    costs = [a["mean_provider_tokens"], b["mean_provider_tokens"]]
                    cost_basis = "provider tokens"
                if all(x is not None and x > 0 for x in costs):
                    candidate = "codemode" if costs[1] < costs[0] else "traditional"
                    c, other = (b, a) if candidate == "codemode" else (a, b)
                    metric_key = "mean_cost_usd" if cost_basis == "USD" else "mean_provider_tokens"
                    if (
                        c[metric_key] <= other[metric_key] * 0.95
                        and _within(c["p95_wall_ms"], other["p95_wall_ms"], 1.2)
                        and _within(c["mean_upstream_calls"], other["mean_upstream_calls"], 1.2)
                        and _within_add(c["mean_recovery"], other["mean_recovery"], 0.25)
                    ):
                        winner, reason = (
                            candidate,
                            f"Equal verified quality; {cost_basis} cost is at least 5% lower "
                            "within regression limits.",
                        )
                    else:
                        reason = (
                            "Equal quality without sufficient cost advantage inside "
                            "regression limits."
                        )
        row.update(recommended_mode=winner, reason=reason)
        out.append(row)
    return out


def resolve_adaptive_recommendation(
    evidence: list[dict[str, Any]],
    model: str,
    workload: str,
    *,
    pricing: dict[str, Any] | None = None,
    days: int | None = 30,
    summaries: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Resolve the adaptive recommendation and explain which evidence tier won."""
    model_rows = [
        row
        for row in summaries or summarize_pairs(evidence, days=days, pricing=pricing)
        if row["model"].lower() == model.lower()
    ]
    exact = next((row for row in model_rows if row["workload"] == workload), None)
    selected = exact if exact and exact.get("recommended_mode") else None
    tier = "exact"
    exact_sparse = not exact or exact.get("pairs", 0) < 5
    conflicting_fallback = False

    family_map = {
        "small_fanout": "fanout",
        "large_fanout": "fanout",
        "data_reconciliation": "reconciliation",
        "large_data_reconciliation": "reconciliation",
    }
    family = family_map.get(workload)
    if selected is None and exact_sparse:
        siblings = [
            row for row in model_rows if family and family_map.get(row["workload"]) == family
        ]
        family_qualified = [row for row in siblings if row.get("recommended_mode")]
        family_winners = {row["recommended_mode"] for row in family_qualified}
        if len(family_winners) == 1:
            selected, tier = family_qualified[0], "workload_family"
        elif len(family_winners) > 1:
            conflicting_fallback = True
        elif not family_qualified:
            pooled = [
                {
                    **item,
                    "scenario_id": f"{item.get('workload')}:{item.get('scenario_id')}",
                    "workload": workload,
                }
                for item in evidence
                if str(item.get("model") or "").lower() == model.lower()
                and family
                and family_map.get(item.get("workload")) == family
            ]
            pooled_winner = next(
                (
                    row
                    for row in summarize_pairs(pooled, days=days, pricing=pricing)
                    if row["workload"] == workload and row.get("recommended_mode")
                ),
                None,
            )
            if pooled_winner:
                selected, tier = pooled_winner, "workload_family"
            else:
                qualified = [row for row in model_rows if row.get("recommended_mode")]
                model_winners = {row["recommended_mode"] for row in qualified}
                if len(model_winners) == 1:
                    selected, tier = qualified[0], "model_wide"
                elif len(model_winners) > 1:
                    conflicting_fallback = True
                elif not qualified:
                    pooled_all = [
                        {
                            **item,
                            "scenario_id": f"{item.get('workload')}:{item.get('scenario_id')}",
                            "workload": workload,
                        }
                        for item in evidence
                        if str(item.get("model") or "").lower() == model.lower()
                    ]
                    broad_winner = next(
                        (
                            row
                            for row in summarize_pairs(pooled_all, days=days, pricing=pricing)
                            if row["workload"] == workload and row.get("recommended_mode")
                        ),
                        None,
                    )
                    if broad_winner:
                        selected, tier = broad_winner, "model_wide"

    if selected:
        return {
            "recommended_mode": selected["recommended_mode"],
            "evidence_tier": tier,
            "evidence_model": selected["model"],
            "evidence_workload": selected["workload"],
            "pairs": selected["pairs"],
            "qualification": selected["qualification"],
            "latest_evidence_at": selected["latest_evidence_at"],
            "traditional": selected["traditional"],
            "codemode": selected["codemode"],
            "reason": selected["reason"],
            "status": "recommended",
        }

    reason = (
        exact["reason"]
        if exact and not exact_sparse
        else (
            "Qualified fallback cohorts disagree; normal Auto routing applies."
            if conflicting_fallback
            else "No qualified exact or consistent fallback evidence; normal Auto routing applies."
        )
    )
    status = (
        "quality_gate"
        if exact and not exact_sparse
        else "conflicting_evidence"
        if conflicting_fallback
        else "insufficient_evidence"
    )
    if (
        exact
        and exact.get("pairs", 0) >= 5
        and exact.get("reason", "").startswith("Equal quality without")
    ):
        status = "quality_gate"
    return {
        "recommended_mode": None,
        "evidence_tier": None,
        "evidence_model": model,
        "evidence_workload": workload,
        "pairs": exact.get("pairs", 0) if exact else 0,
        "qualification": exact.get("qualification", "sparse") if exact else "sparse",
        "latest_evidence_at": exact.get("latest_evidence_at") if exact else None,
        "traditional": exact.get("traditional") if exact else None,
        "codemode": exact.get("codemode") if exact else None,
        "reason": reason,
        "status": status,
    }


def _mean_known(rows: list[dict[str, Any]], key: str) -> float | None:
    if not rows or any(not _finite_number(r.get(key)) for r in rows):
        return None
    vals = [float(r[key]) for r in rows]
    return round(sum(vals) / len(vals), 6)


def _mean_provider_tokens(rows: list[dict[str, Any]]) -> float | None:
    if not rows or any(
        not _finite_number(r.get("input_tokens")) or not _finite_number(r.get("output_tokens"))
        for r in rows
    ):
        return None
    verified = sum(bool(r.get("correct") and r.get("grounded")) for r in rows)
    return (
        round(sum(float(r["input_tokens"]) + float(r["output_tokens"]) for r in rows) / verified, 3)
        if verified
        else None
    )


def _percentile(values: list[Any], p: float) -> float | None:
    if not values or any(not _finite_number(v) for v in values):
        return None
    vals = sorted(float(v) for v in values)
    return round(
        vals[max(0, min(len(vals) - 1, int(__import__("math").ceil(p * len(vals)) - 1)))], 3
    )


def _within(value: float | None, baseline: float | None, ratio: float) -> bool:
    return value is not None and baseline is not None and value <= baseline * ratio


def _within_add(value: float | None, baseline: float | None, delta: float) -> bool:
    return value is not None and baseline is not None and value <= baseline + delta


def _finite_number(value: Any) -> bool:
    import math

    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)


def _mean_cost_usd(rows: list[dict[str, Any]], model: str, pricing: dict[str, Any]) -> float | None:
    if not rows or any(
        not _finite_number(r.get("input_tokens")) or not _finite_number(r.get("output_tokens"))
        for r in rows
    ):
        return None
    rates = pricing.get(model) or pricing.get(model.lower())
    if not isinstance(rates, dict):
        return None
    input_rate, output_rate = rates.get("input"), rates.get("output")
    if not _finite_number(input_rate) or not _finite_number(output_rate):
        return None
    total = (
        sum(
            float(r["input_tokens"]) * float(input_rate)
            + float(r["output_tokens"]) * float(output_rate)
            for r in rows
        )
        / 1_000_000
    )
    verified = sum(bool(r.get("correct") and r.get("grounded")) for r in rows)
    return round(total / verified, 8) if verified else None
