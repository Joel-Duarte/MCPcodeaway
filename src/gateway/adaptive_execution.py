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
