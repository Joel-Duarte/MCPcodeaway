"""Benchmark comparing Regular Discovery (Keyword, BM25) vs. Embeddings & System 1."""

from __future__ import annotations

import argparse
import asyncio
import json
import math
import sys
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from examples.mock_backends import build_synthetic_tools
from gateway.config import DiscoveryBackendMode, GatewaySettings
from gateway.discovery import DiscoveryEngine
from gateway.upstream_clients import DiscoveredTool

_EVAL_QUERIES: list[dict[str, Any]] = [
    # 10 Direct / Exact queries
    {
        "id": "direct_01",
        "kind": "direct",
        "query": "query overdue debt ledger in snowflake",
        "expected": {"snowflake_ledger__query_debt_ledger"},
    },
    {
        "id": "direct_02",
        "kind": "direct",
        "query": "list eu invoices",
        "expected": {"billing_legacy_eu__list_eu_invoices"},
    },
    {
        "id": "direct_03",
        "kind": "direct",
        "query": "list us invoices",
        "expected": {"billing_legacy_us__list_us_invoices"},
    },
    {
        "id": "direct_04",
        "kind": "direct",
        "query": "get customer balance from snowflake",
        "expected": {"snowflake_ledger__get_customer_balance"},
    },
    {
        "id": "direct_05",
        "kind": "direct",
        "query": "get trial balance lines variances",
        "expected": {"snowflake_ledger__get_trial_balance_lines"},
    },
    {
        "id": "direct_06",
        "kind": "direct",
        "query": "get eu customer details",
        "expected": {"billing_legacy_eu__get_eu_customer"},
    },
    {
        "id": "direct_07",
        "kind": "direct",
        "query": "get us customer details",
        "expected": {"billing_legacy_us__get_us_customer"},
    },
    {
        "id": "direct_08",
        "kind": "direct",
        "query": "list terminal incidents",
        "expected": {"ops_ticketing__list_terminal_incidents"},
    },
    {
        "id": "direct_09",
        "kind": "direct",
        "query": "get terminal health telemetry",
        "expected": {"ops_ticketing__get_terminal_health"},
    },
    {
        "id": "direct_10",
        "kind": "direct",
        "query": "create collection case",
        "expected": {"ops_ticketing__create_collection_case"},
    },
    # 10 Paraphrased / Semantic queries (avoiding exact tool identifiers)
    {
        "id": "semantic_01",
        "kind": "semantic",
        "query": "Which European merchants owe money past their payment deadline?",
        "expected": {
            "billing_legacy_eu__list_eu_invoices",
            "snowflake_ledger__query_debt_ledger",
        },
    },
    {
        "id": "semantic_02",
        "kind": "semantic",
        "query": "Check how much money customer CUST-1003 currently owes in accounting",
        "expected": {"snowflake_ledger__get_customer_balance"},
    },
    {
        "id": "semantic_03",
        "kind": "semantic",
        "query": "Find accounting book mismatches between debit and credit entries",
        "expected": {"snowflake_ledger__get_trial_balance_lines"},
    },
    {
        "id": "semantic_04",
        "kind": "semantic",
        "query": "Pull the email address for an American merchant account",
        "expected": {"billing_legacy_us__get_us_customer"},
    },
    {
        "id": "semantic_05",
        "kind": "semantic",
        "query": "Why cannot shoppers tap their credit cards at point-of-sale devices?",
        "expected": {
            "ops_ticketing__list_terminal_incidents",
            "ops_ticketing__get_terminal_health",
        },
    },
    {
        "id": "semantic_06",
        "kind": "semantic",
        "query": "Escalate a delinquent account to outbound recovery agents",
        "expected": {"ops_ticketing__create_collection_case"},
    },
    {
        "id": "semantic_07",
        "kind": "semantic",
        "query": "Verify if a specific card reader unit is online right now",
        "expected": {"ops_ticketing__get_terminal_health"},
    },
    {
        "id": "semantic_08",
        "kind": "semantic",
        "query": "Fetch contact info for a store located in Europe",
        "expected": {"billing_legacy_eu__get_eu_customer"},
    },
    {
        "id": "semantic_09",
        "kind": "semantic",
        "query": "Show unpaid bills issued to US businesses",
        "expected": {"billing_legacy_us__list_us_invoices"},
    },
    {
        "id": "semantic_10",
        "kind": "semantic",
        "query": "Compare central finance arrears against regional billing records",
        "expected": {
            "snowflake_ledger__query_debt_ledger",
            "billing_legacy_eu__list_eu_invoices",
            "billing_legacy_us__list_us_invoices",
        },
    },
]


def _base_enterprise_tools() -> list[DiscoveredTool]:
    return [
        DiscoveredTool(
            server="snowflake_ledger",
            name="query_debt_ledger",
            qualified_name="snowflake_ledger__query_debt_ledger",
            description="Query Snowflake debt ledger rows with optional status or region filter.",
            input_schema={
                "type": "object",
                "properties": {"status": {"type": "string"}, "region": {"type": "string"}},
            },
        ),
        DiscoveredTool(
            server="snowflake_ledger",
            name="get_customer_balance",
            qualified_name="snowflake_ledger__get_customer_balance",
            description="Fetch a customer's current debt ledger balance from Snowflake.",
            input_schema={
                "type": "object",
                "properties": {"customer_id": {"type": "string"}},
                "required": ["customer_id"],
            },
        ),
        DiscoveredTool(
            server="snowflake_ledger",
            name="get_trial_balance_lines",
            qualified_name="snowflake_ledger__get_trial_balance_lines",
            description="Fetch trial balance reconciliation lines with variance details.",
            input_schema={
                "type": "object",
                "properties": {"only_variances": {"type": "boolean"}},
            },
        ),
        DiscoveredTool(
            server="billing_legacy_eu",
            name="list_eu_invoices",
            qualified_name="billing_legacy_eu__list_eu_invoices",
            description="List EU invoices with optional status filter.",
            input_schema={"type": "object", "properties": {"status": {"type": "string"}}},
        ),
        DiscoveredTool(
            server="billing_legacy_eu",
            name="get_eu_customer",
            qualified_name="billing_legacy_eu__get_eu_customer",
            description="Get EU merchant profile and contact email by customer_id.",
            input_schema={
                "type": "object",
                "properties": {"customer_id": {"type": "string"}},
                "required": ["customer_id"],
            },
        ),
        DiscoveredTool(
            server="billing_legacy_us",
            name="list_us_invoices",
            qualified_name="billing_legacy_us__list_us_invoices",
            description="List US invoices with optional status filter.",
            input_schema={"type": "object", "properties": {"status": {"type": "string"}}},
        ),
        DiscoveredTool(
            server="billing_legacy_us",
            name="get_us_customer",
            qualified_name="billing_legacy_us__get_us_customer",
            description="Get US merchant profile and contact email by customer_id.",
            input_schema={
                "type": "object",
                "properties": {"customer_id": {"type": "string"}},
                "required": ["customer_id"],
            },
        ),
        DiscoveredTool(
            server="ops_ticketing",
            name="list_terminal_incidents",
            qualified_name="ops_ticketing__list_terminal_incidents",
            description="List payment terminal incidents across EU and US fleets.",
            input_schema={"type": "object", "properties": {"status": {"type": "string"}}},
        ),
        DiscoveredTool(
            server="ops_ticketing",
            name="get_terminal_health",
            qualified_name="ops_ticketing__get_terminal_health",
            description="Inspect health telemetry for a specific terminal ID.",
            input_schema={
                "type": "object",
                "properties": {"terminal_id": {"type": "string"}},
                "required": ["terminal_id"],
            },
        ),
        DiscoveredTool(
            server="ops_ticketing",
            name="create_collection_case",
            qualified_name="ops_ticketing__create_collection_case",
            description="Create an outbound collections escalation case for a customer.",
            input_schema={
                "type": "object",
                "properties": {
                    "customer_id": {"type": "string"},
                    "reason": {"type": "string"},
                    "priority": {"type": "string"},
                },
                "required": ["customer_id", "reason"],
            },
            mutating=True,
        ),
    ]


def _catalog_for_size(extra_synthetic: int) -> list[DiscoveredTool]:
    tools = _base_enterprise_tools()
    if extra_synthetic > 0:
        for raw in build_synthetic_tools(extra_synthetic):
            tools.append(
                DiscoveredTool(
                    server="synthetic_erp",
                    name=raw["name"],
                    qualified_name=f"synthetic_erp__{raw['name']}",
                    description=raw["description"],
                    input_schema=raw["inputSchema"],
                )
            )
    return tools


async def run_discovery_benchmark(
    *,
    backends: list[DiscoveryBackendMode] | None = None,
    extra_counts: tuple[int, ...] = (0, 50, 150),
    embedding_url: str | None = None,
    embedding_model: str = "text-embedding-qwen3-embedding-0.6b",
    system1_url: str | None = None,
    system1_model: str = "laya",
    output_path: Path | None = Path("benchmarks/discovery_results.json"),
) -> dict[str, Any]:
    """Evaluate Recall@5, Precision@5, MRR, server routing accuracy, and latency."""
    chosen_backends: list[DiscoveryBackendMode] = backends or ["keyword", "bm25"]
    settings = GatewaySettings(
        embedding_url=embedding_url,
        embedding_model=embedding_model,
        system1_url=system1_url,
        system1_model=system1_model,
    )
    engine = DiscoveryEngine(settings)
    rows: list[dict[str, Any]] = []

    for extra in extra_counts:
        tools = _catalog_for_size(extra)
        cat_ver = f"bench-{len(tools)}"
        for backend in chosen_backends:
            recalls: list[float] = []
            direct_recalls: list[float] = []
            semantic_recalls: list[float] = []
            precisions: list[float] = []
            mrrs: list[float] = []
            srv_hits = 0
            fallbacks = 0
            last_fallback_reason: str | None = None
            latencies: list[float] = []

            for item in _EVAL_QUERIES:
                t0 = time.perf_counter()
                outcome = await engine.discover(
                    item["query"],
                    tools,
                    catalog_version=cat_ver,
                    backend=backend,
                    limit=5,
                )
                elapsed_ms = (time.perf_counter() - t0) * 1000.0
                latencies.append(elapsed_ms)
                if outcome.fallback_triggered:
                    fallbacks += 1
                    last_fallback_reason = outcome.fallback_reason

                predicted = [st.tool.qualified_name for st in outcome.tools[:5]]
                expected: set[str] = item["expected"]
                hits = len(set(predicted) & expected)
                recall = hits / len(expected)
                precision = hits / max(len(predicted), 1)
                recalls.append(recall)
                precisions.append(precision)
                if item["kind"] == "direct":
                    direct_recalls.append(recall)
                else:
                    semantic_recalls.append(recall)

                rr = 0.0
                for rank, qname in enumerate(predicted, start=1):
                    if qname in expected:
                        rr = 1.0 / rank
                        break
                mrrs.append(rr)

                expected_servers = {q.split("__", 1)[0] for q in expected}
                pred_servers = {st.tool.server for st in outcome.tools[:5]}
                if expected_servers <= pred_servers:
                    srv_hits += 1

            sorted_lat = sorted(latencies)
            p50 = sorted_lat[min(len(sorted_lat) - 1, math.ceil(0.5 * len(sorted_lat)) - 1)]
            n_q = len(_EVAL_QUERIES)
            rows.append(
                {
                    "tool_count": len(tools),
                    "backend_requested": backend,
                    "fallback_count": fallbacks,
                    "fallback_rate_pct": round((fallbacks / n_q) * 100.0, 2),
                    "fallback_reason": last_fallback_reason,
                    "recall_at_5": round(sum(recalls) / n_q, 4),
                    "direct_recall_at_5": round(sum(direct_recalls) / len(direct_recalls), 4),
                    "semantic_recall_at_5": round(sum(semantic_recalls) / len(semantic_recalls), 4),
                    "precision_at_5": round(sum(precisions) / n_q, 4),
                    "mrr": round(sum(mrrs) / n_q, 4),
                    "server_routing_acc": round(srv_hits / n_q, 4),
                    "mean_latency_ms": round(sum(latencies) / n_q, 3),
                    "p50_latency_ms": round(p50, 3),
                }
            )

    report = {
        "generated_at": datetime.now(UTC).isoformat(),
        "query_count": len(_EVAL_QUERIES),
        "results": rows,
    }
    if output_path is not None:
        output_path.parent.mkdir(parents=True, exist_ok=True)
        output_path.write_text(json.dumps(report, indent=2), encoding="utf-8")
    return report


def _print_table(report: dict[str, Any]) -> None:
    print("\n=== Discovery Accuracy & Latency: Regular vs. Embedding vs. System 1 ===")
    print(
        "| Tools | Backend | Recall@5 | Direct | Semantic | MRR | Srv Acc | Fallback % | Mean ms |"
    )
    print("| ---: | :--- | ---: | ---: | ---: | ---: | ---: | ---: | ---: |")
    for r in report["results"]:
        print(
            f"| {r['tool_count']} | {r['backend_requested']} | {r['recall_at_5']:.3f} | "
            f"{r['direct_recall_at_5']:.3f} | {r['semantic_recall_at_5']:.3f} | "
            f"{r['mrr']:.3f} | {r['server_routing_acc']:.3f} | "
            f"{r['fallback_rate_pct']}% | {r['mean_latency_ms']} |"
        )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Benchmark tool discovery backends")
    parser.add_argument("--live-lmstudio", action="store_true")
    parser.add_argument("--lmstudio-base", default="http://127.0.0.1:1234/v1")
    parser.add_argument("--embedding-model", default="text-embedding-qwen3-embedding-0.6b")
    parser.add_argument("--system1-model", default="laya")
    parser.add_argument("--output", default="benchmarks/discovery_results.json")
    args = parser.parse_args(argv)

    backends: list[DiscoveryBackendMode] = ["keyword", "bm25"]
    emb_url: str | None = None
    s1_url: str | None = None
    if args.live_lmstudio:
        backends.extend(["embedding", "system1", "hybrid_system1"])
        emb_url = f"{args.lmstudio_base.rstrip('/')}/embeddings"
        s1_url = f"{args.lmstudio_base.rstrip('/')}/systemone"

    report = asyncio.run(
        run_discovery_benchmark(
            backends=backends,
            embedding_url=emb_url,
            embedding_model=args.embedding_model,
            system1_url=s1_url,
            system1_model=args.system1_model,
            output_path=Path(args.output),
        )
    )
    _print_table(report)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
