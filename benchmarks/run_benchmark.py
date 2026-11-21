"""Deterministic offline benchmark comparing Classic MCP vs. Code Mode."""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
import tempfile
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import httpx

_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from examples.mock_backends import build_synthetic_tools, create_mock_backends_app  # noqa: E402
from gateway.auth import LocalDevKeyManager
from gateway.catalog import ToolCatalog, estimate_tokens, to_mcp_tool_definition
from gateway.comparator import run_side_by_side_comparison
from gateway.config import GatewaySettings, UpstreamConfig, UpstreamRegistry
from gateway.server import (
    EXECUTE_CODE_DEF,
    LIST_SERVERS_DEF,
    SEARCH_TOOLS_DEF,
    GatewayRuntime,
)
from gateway.state import SQLiteAuditStore
from gateway.upstream_clients import DiscoveredTool


def run_scaling_sweep(
    tool_counts: tuple[int, ...] = (20, 50, 150, 300, 500),
) -> list[dict[str, Any]]:
    """Measure Classic tools/list bloat vs. Code Mode constant 3-tool surface."""
    surface_tokens = estimate_tokens([LIST_SERVERS_DEF, SEARCH_TOOLS_DEF, EXECUTE_CODE_DEF])
    rows: list[dict[str, Any]] = []
    for count in tool_counts:
        catalog = ToolCatalog()
        cfg = UpstreamConfig(name="synthetic_erp", type="mcp_http", url="http://synthetic.local")
        discovered = [
            DiscoveredTool(
                server="synthetic_erp",
                name=raw["name"],
                qualified_name=f"synthetic_erp__{raw['name']}",
                description=raw["description"],
                input_schema=raw["inputSchema"],
            )
            for raw in build_synthetic_tools(count)
        ]
        catalog.set_server_tools(cfg, discovered)
        _ = UpstreamRegistry(GatewaySettings())
        classic_defs = [to_mcp_tool_definition(t) for t in discovered]
        classic_bytes = len(json.dumps(classic_defs, separators=(",", ":")).encode("utf-8"))
        classic_tokens = estimate_tokens(classic_defs)

        stub_preview = [
            {"qualified_name": t.qualified_name, "description": t.description}
            for t in discovered[:5]
        ]
        search_result_tokens = estimate_tokens(stub_preview)
        reduction_pct = round((1.0 - (surface_tokens / classic_tokens)) * 100.0, 2)
        rows.append(
            {
                "tool_count": count,
                "classic_schema_bytes": classic_bytes,
                "classic_schema_tokens": classic_tokens,
                "codemode_surface_tokens": surface_tokens,
                "codemode_search_top5_tokens": search_result_tokens,
                "reduction_pct": reduction_pct,
            }
        )
    return rows


async def run_offline_benchmark(
    output_path: Path | None = Path("benchmarks/results.json"),
) -> dict[str, Any]:
    """Run scaling sweep and 3 enterprise workloads without requiring external API keys."""
    scaling = run_scaling_sweep()

    with tempfile.TemporaryDirectory() as tmpdir:
        settings = GatewaySettings(auth_mode="dev", sqlite_path=f"{tmpdir}/bench.db")
        dev_keys = LocalDevKeyManager(issuer=settings.oidc_issuer)
        backends_app = create_mock_backends_app(
            public_key_pem=dev_keys.public_pem, expected_issuer=settings.oidc_issuer
        )
        backend_client = httpx.AsyncClient(
            transport=httpx.ASGITransport(app=backends_app), base_url="http://127.0.0.1:8001"
        )
        runtime = GatewayRuntime(
            settings=settings,
            store=SQLiteAuditStore(settings.sqlite_path),
            dev_keys=dev_keys,
            http_client=backend_client,
        )
        await runtime.startup()
        try:
            token = dev_keys.issue_user_token(persona="finance_analyst")
            identity = await runtime.validator.validate_token(token)
            scenarios = []
            for name in (
                "reconcile_overdue",
                "reconcile_bulk_200",
                "trial_balance_audit",
                "single_lookup",
            ):
                scenarios.append(
                    await run_side_by_side_comparison(runtime, identity, scenario=name)
                )
        finally:
            await runtime.shutdown()
            await backend_client.aclose()

    report = {
        "generated_at": datetime.now(UTC).isoformat(),
        "scaling": scaling,
        "scenarios": scenarios,
    }
    if output_path is not None:
        output_path.parent.mkdir(parents=True, exist_ok=True)
        output_path.write_text(json.dumps(report, indent=2), encoding="utf-8")
    return report


def verify_benchmark_thresholds(
    report: dict[str, Any],
    thresholds: dict[str, Any],
) -> list[str]:
    """Check measured scaling & scenario metrics against locked threshold invariants."""
    failures: list[str] = []
    scaling_by_count = {str(r["tool_count"]): r for r in report.get("scaling", [])}
    for count_key, bounds in (thresholds.get("scaling") or {}).items():
        row = scaling_by_count.get(str(count_key))
        if row is None:
            failures.append(f"scaling[{count_key}]: missing benchmark row")
            continue
        min_red = bounds.get("min_reduction_pct")
        if min_red is not None and row["reduction_pct"] < float(min_red):
            failures.append(
                f"scaling[{count_key}].reduction_pct={row['reduction_pct']} < {min_red}"
            )
        max_surf = bounds.get("max_codemode_surface_tokens")
        if max_surf is not None and row["codemode_surface_tokens"] > int(max_surf):
            failures.append(
                f"scaling[{count_key}].codemode_surface_tokens="
                f"{row['codemode_surface_tokens']} > {max_surf}"
            )

    scenarios_by_name = {s["scenario"]: s for s in report.get("scenarios", [])}
    for sc_name, bounds in (thresholds.get("scenarios") or {}).items():
        sc = scenarios_by_name.get(sc_name)
        if sc is None:
            failures.append(f"scenarios[{sc_name}]: missing benchmark scenario")
            continue
        cm = sc.get("code_mode") or {}
        if bounds.get("require_ok") and not cm.get("ok"):
            failures.append(f"scenarios[{sc_name}].code_mode.ok is False")
        req_winner = bounds.get("require_winner")
        if req_winner is not None and sc.get("winner") != req_winner:
            failures.append(f"scenarios[{sc_name}].winner={sc.get('winner')} != {req_winner}")
        min_sav = bounds.get("min_context_token_savings_pct")
        sav = (sc.get("deltas") or {}).get("context_token_savings_pct", 0.0)
        if min_sav is not None and sav < float(min_sav):
            failures.append(f"scenarios[{sc_name}].context_token_savings_pct={sav} < {min_sav}")
        min_raw = bounds.get("min_raw_upstream_bytes")
        raw_b = int(cm.get("raw_upstream_bytes") or 0)
        if min_raw is not None and raw_b < int(min_raw):
            failures.append(f"scenarios[{sc_name}].raw_upstream_bytes={raw_b} < {min_raw}")
        max_ret = bounds.get("max_payload_bytes")
        ret_b = int(cm.get("payload_bytes") or cm.get("returned_bytes") or 0)
        if max_ret is not None and ret_b > int(max_ret):
            failures.append(f"scenarios[{sc_name}].payload_bytes={ret_b} > {max_ret}")
    return failures


def _print_summary(report: dict[str, Any]) -> None:
    print("\n=== Catalog Scaling: Classic MCP vs. Code Mode ===")
    print("| Tools | Classic Tokens | Code Mode Surface Tokens | Reduction |")
    print("| ---: | ---: | ---: | ---: |")
    for row in report["scaling"]:
        print(
            f"| {row['tool_count']} | {row['classic_schema_tokens']} | "
            f"{row['codemode_surface_tokens']} | {row['reduction_pct']}% |"
        )

    print("\n=== Enterprise Workloads: Classic MCP vs. Code Mode ===")
    print("| Scenario | Classic Tokens | Code Mode Tokens | Savings | Round Trips | Winner |")
    print("| :--- | ---: | ---: | ---: | :---: | :--- |")
    for sc in report["scenarios"]:
        c = sc["classic"]
        m = sc["code_mode"]
        print(
            f"| {sc['scenario']} | {c['total_context_tokens']} | {m['total_context_tokens']} | "
            f"{sc['deltas']['context_token_savings_pct']}% | "
            f"{c['round_trips']} -> {m['round_trips']} | {sc['winner']} |"
        )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Run deterministic MCP vs Code Mode benchmark")
    parser.add_argument("--output", default="benchmarks/results.json")
    parser.add_argument("--verify-thresholds", action="store_true")
    parser.add_argument("--thresholds", default="benchmarks/thresholds.json")
    args = parser.parse_args(argv)
    report = asyncio.run(run_offline_benchmark(output_path=Path(args.output)))
    _print_summary(report)
    if args.verify_thresholds:
        thresholds = json.loads(Path(args.thresholds).read_text(encoding="utf-8"))
        failures = verify_benchmark_thresholds(report, thresholds)
        if failures:
            print("\nTHRESHOLD VIOLATIONS:", file=sys.stderr)
            for item in failures:
                print(f"  - {item}", file=sys.stderr)
            return 1
        print("\nAll benchmark thresholds PASSED.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
