"""Classic MCP vs. Code Mode onboarding audit and side-by-side task comparator."""

from __future__ import annotations

import json
from collections import defaultdict
from typing import TYPE_CHECKING, Any

from gateway.auth import CallerIdentity
from gateway.catalog import (
    ToolCatalog,
    estimate_tokens,
    render_python_stub,
    to_mcp_tool_definition,
)
from gateway.config import UpstreamRegistry
from gateway.upstream_clients import DiscoveredTool

if TYPE_CHECKING:
    from gateway.server import GatewayRuntime

_GATEWAY_SURFACE_OVERHEAD_TOKENS = 360


def audit_catalog_comparison(
    catalog: ToolCatalog,
    registry: UpstreamRegistry,
    *,
    candidate_name: str | None = None,
    candidate_tools: list[DiscoveredTool] | None = None,
) -> dict[str, Any]:
    """Compare Classic MCP vs. Code Mode catalog footprint, collisions, and ergonomics."""
    by_server: dict[str, list[DiscoveredTool]] = {}
    for upstream in registry.list_upstreams():
        by_server[upstream.name] = catalog.get_server_tools(upstream.name)
    if candidate_name is not None and candidate_tools is not None:
        by_server[candidate_name] = list(candidate_tools)

    all_tools: list[DiscoveredTool] = [tool for tools in by_server.values() for tool in tools]

    classic_defs = [to_mcp_tool_definition(tool) for tool in all_tools]
    classic_json = json.dumps(classic_defs, separators=(",", ":"))
    classic_bytes = len(classic_json.encode("utf-8"))
    classic_tokens = estimate_tokens(classic_json) if all_tools else 0

    flat_name_map: dict[str, list[str]] = defaultdict(list)
    for tool in all_tools:
        flat_name_map[tool.name].append(tool.server)
    collisions = [
        {"tool_name": name, "servers": sorted(set(servers))}
        for name, servers in sorted(flat_name_map.items())
        if len(set(servers)) > 1
    ]

    tool_count = len(all_tools)
    if tool_count > 100:
        limit_status = "critical"
        limit_note = (
            f"{tool_count} tools exceeds 100-tool ceiling where LLM tool accuracy "
            "and client limits (e.g., Copilot/OpenAI 128-tool cap) degrade sharply."
        )
    elif tool_count > 30:
        limit_status = "warning"
        limit_note = (
            f"{tool_count} tools exceeds ~30-tool sweet spot; prompt bloat slows tool selection."
        )
    else:
        limit_status = "ok"
        limit_note = f"{tool_count} tools fits comfortably within single-turn client tool budgets."

    stubs_text = "\n".join(render_python_stub(tool) for tool in all_tools)
    all_stubs_tokens = estimate_tokens(stubs_text) if all_tools else 0
    typical_search_tokens = (
        estimate_tokens("\n".join(render_python_stub(t) for t in all_tools[:5])) if all_tools else 0
    )

    reduction_pct = (
        round((1.0 - (_GATEWAY_SURFACE_OVERHEAD_TOKENS / classic_tokens)) * 100.0, 2)
        if classic_tokens > _GATEWAY_SURFACE_OVERHEAD_TOKENS
        else 0.0
    )

    per_server = []
    for srv_name, tools in sorted(by_server.items()):
        srv_defs = [to_mcp_tool_definition(t) for t in tools]
        srv_stubs = "\n".join(render_python_stub(t) for t in tools)
        per_server.append(
            {
                "server": srv_name,
                "tool_count": len(tools),
                "classic_schema_tokens": estimate_tokens(srv_defs) if tools else 0,
                "codemode_stub_tokens": estimate_tokens(srv_stubs) if tools else 0,
                "mutating_count": sum(1 for t in tools if t.mutating),
            }
        )

    return {
        "catalog_version": catalog.catalog_version,
        "total_tools": tool_count,
        "classic_mcp": {
            "exposed_tool_count": tool_count,
            "schema_bytes": classic_bytes,
            "schema_tokens": classic_tokens,
            "flat_name_collisions": collisions,
            "tool_limit_status": limit_status,
            "tool_limit_note": limit_note,
        },
        "code_mode": {
            "exposed_tool_count": 3,
            "gateway_surface_tokens": _GATEWAY_SURFACE_OVERHEAD_TOKENS,
            "all_stubs_tokens": all_stubs_tokens,
            "typical_search_tokens": typical_search_tokens,
            "flat_name_collisions": [],
            "schema_token_reduction_pct": reduction_pct,
        },
        "per_server": per_server,
        "ergonomics": {
            "when_classic_wins": (
                "Single-step small lookups or explicit human-approved mutating writes "
                "(preserved in hybrid mode) with <15 total tools."
            ),
            "when_codemode_wins": (
                "30+ enterprise tools, multi-system joins/reconciliations, loops/filtering, "
                "or large API payloads (e.g., Swagger/OpenAPI specs) kept out of LLM context."
            ),
        },
    }


def _scenario_plan(
    scenario: str,
) -> tuple[list[dict[str, Any]], str]:
    if scenario == "reconcile_overdue":
        classic = [
            {
                "tool": "snowflake_ledger__query_debt_ledger",
                "arguments": {"status": "overdue"},
            },
            {
                "tool": "billing_legacy_eu__list_eu_invoices",
                "arguments": {"status": "overdue"},
            },
            {
                "tool": "billing_legacy_us__list_us_invoices",
                "arguments": {"status": "overdue"},
            },
        ]
        code = (
            "import asyncio\n"
            "ledger, eu, us = await asyncio.gather(\n"
            "    snowflake_ledger__query_debt_ledger(status='overdue'),\n"
            "    billing_legacy_eu__list_eu_invoices(status='overdue'),\n"
            "    billing_legacy_us__list_us_invoices(status='overdue'),\n"
            ")\n"
            "inv_map = {i['customer_id']: i['amount'] "
            "for i in eu['invoices'] + us['invoices']}\n"
            "[\n"
            "    {'customer_id': r['customer_id'], "
            "'delta': round(r['ledger_balance'] - inv_map[r['customer_id']], 2)}\n"
            "    for r in ledger['rows']\n"
            "    if r['customer_id'] in inv_map "
            "and r['ledger_balance'] != inv_map[r['customer_id']]\n"
            "]"
        )
        return classic, code

    if scenario == "reconcile_bulk_200":
        classic = [
            {
                "tool": "snowflake_ledger__query_debt_ledger",
                "arguments": {"status": "overdue", "dataset": "bulk"},
            },
            {
                "tool": "billing_legacy_eu__list_eu_invoices",
                "arguments": {"status": "overdue", "dataset": "bulk"},
            },
            {
                "tool": "billing_legacy_us__list_us_invoices",
                "arguments": {"status": "overdue", "dataset": "bulk"},
            },
        ]
        code = (
            "import asyncio\n"
            "ledger, eu, us = await asyncio.gather(\n"
            "    snowflake_ledger__query_debt_ledger(status='overdue', dataset='bulk'),\n"
            "    billing_legacy_eu__list_eu_invoices(status='overdue', dataset='bulk'),\n"
            "    billing_legacy_us__list_us_invoices(status='overdue', dataset='bulk'),\n"
            ")\n"
            "inv_map = {i['customer_id']: i['amount'] "
            "for i in eu['invoices'] + us['invoices']}\n"
            "[\n"
            "    {'customer_id': r['customer_id'], "
            "'delta': round(r['ledger_balance'] - inv_map[r['customer_id']], 2)}\n"
            "    for r in ledger['rows']\n"
            "    if r['customer_id'] in inv_map "
            "and r['ledger_balance'] != inv_map[r['customer_id']]\n"
            "]"
        )
        return classic, code

    if scenario == "trial_balance_audit":
        classic = [
            {
                "tool": "snowflake_ledger__get_trial_balance_lines",
                "arguments": {"only_variances": True},
            },
            {
                "tool": "billing_legacy_eu__get_eu_customer",
                "arguments": {"customer_id": "CUST-1003"},
            },
            {
                "tool": "billing_legacy_eu__get_eu_customer",
                "arguments": {"customer_id": "CUST-1011"},
            },
            {
                "tool": "billing_legacy_us__get_us_customer",
                "arguments": {"customer_id": "CUST-1024"},
            },
        ]
        code = (
            "import asyncio\n"
            "tb = await snowflake_ledger__get_trial_balance_lines(only_variances=True)\n"
            "async def _lookup(line):\n"
            "    cid = line['customer_id']\n"
            "    if '-EU-' in line['account_code']:\n"
            "        c = await billing_legacy_eu__get_eu_customer(customer_id=cid)\n"
            "    else:\n"
            "        c = await billing_legacy_us__get_us_customer(customer_id=cid)\n"
            "    return {'customer_id': cid, 'variance': line['variance'], "
            "'contact': c['contact_email']}\n"
            "await asyncio.gather(*[_lookup(line) for line in tb['lines']])"
        )
        return classic, code

    if scenario == "single_lookup":
        classic = [
            {
                "tool": "snowflake_ledger__get_customer_balance",
                "arguments": {"customer_id": "CUST-1003"},
            }
        ]
        code = "await snowflake_ledger__get_customer_balance(customer_id='CUST-1003')"
        return classic, code

    raise ValueError(f"Unknown scenario '{scenario}'")


async def run_side_by_side_comparison(
    runtime: GatewayRuntime,
    identity: CallerIdentity,
    *,
    scenario: str = "reconcile_overdue",
    custom_classic_calls: list[dict[str, Any]] | None = None,
    custom_code: str | None = None,
) -> dict[str, Any]:
    """Execute the same workload via Classic MCP and Code Mode and compare metrics."""
    await runtime.ensure_catalog_for_identity(identity)

    if scenario == "custom":
        if not custom_classic_calls or not custom_code:
            raise ValueError("custom scenario requires custom_classic_calls and custom_code")
        classic_calls = custom_classic_calls
        codemode_code = custom_code
    else:
        classic_calls, codemode_code = _scenario_plan(scenario)

    classic_schema_tokens = estimate_tokens(runtime.tools_for_mode(identity, "traditional"))
    codemode_schema_tokens = estimate_tokens(runtime.tools_for_mode(identity, "codemode"))

    classic_runs: list[dict[str, Any]] = []
    classic_total_ms = 0.0
    classic_payload_bytes = 0
    classic_exchanges = 0
    for call in classic_calls:
        res = await runtime.call_direct_tool_for_identity(
            identity,
            str(call["tool"]),
            dict(call.get("arguments") or {}),
            mode="traditional",
        )
        classic_runs.append(res)
        classic_total_ms += float(res["duration_ms"])
        classic_payload_bytes += int(res["returned_bytes"])
        run_doc = await runtime.store.get_run(res["run_id"])
        if run_doc:
            for sc in run_doc.get("subcalls", []):
                if sc.get("auth_type") == "delegated_token" and not sc.get("token_cache_hit"):
                    classic_exchanges += 1

    classic_round_trips = len(classic_calls) + 1
    classic_payload_tokens = estimate_tokens([r.get("result") for r in classic_runs])
    classic_total_tokens = classic_schema_tokens * classic_round_trips + classic_payload_tokens

    search_res = runtime.catalog.search_tools(
        identity, runtime.policy, runtime.registry, query="", limit=10
    )
    search_tokens = estimate_tokens(search_res)
    cm_exec = await runtime.execute_code_for_identity(identity, codemode_code, mode="codemode")
    cm_run_doc = await runtime.store.get_run(cm_exec["run_id"])
    cm_exchanges = 0
    if cm_run_doc:
        for sc in cm_run_doc.get("subcalls", []):
            if sc.get("auth_type") == "delegated_token" and not sc.get("token_cache_hit"):
                cm_exchanges += 1

    codemode_round_trips = 2
    codemode_payload_tokens = search_tokens + estimate_tokens(cm_exec.get("result"))
    codemode_total_tokens = codemode_schema_tokens * codemode_round_trips + codemode_payload_tokens

    token_savings_pct = (
        round((1.0 - (codemode_total_tokens / classic_total_tokens)) * 100.0, 2)
        if classic_total_tokens
        else 0.0
    )

    if len(classic_calls) == 1 and classic_payload_bytes < 512:
        winner = "classic_or_tie"
        notes = (
            "For a single small read call with a modest catalog, Classic MCP takes 1 turn "
            "without writing Python; Code Mode still saves repeated schema tokens at scale."
        )
    else:
        winner = "code_mode" if codemode_total_tokens <= classic_total_tokens else "classic"
        notes = (
            "Code Mode batches multi-system calls in Monty via asyncio.gather and filters "
            "intermediate payloads before returning to LLM context."
        )

    return {
        "scenario": scenario,
        "classic": {
            "schema_tokens_per_turn": classic_schema_tokens,
            "round_trips": classic_round_trips,
            "payload_bytes": classic_payload_bytes,
            "payload_tokens": classic_payload_tokens,
            "total_context_tokens": classic_total_tokens,
            "token_exchanges": classic_exchanges,
            "duration_ms": round(classic_total_ms, 3),
            "results": [r.get("result") for r in classic_runs],
        },
        "code_mode": {
            "schema_tokens_per_turn": codemode_schema_tokens,
            "round_trips": codemode_round_trips,
            "raw_upstream_bytes": cm_exec.get("raw_upstream_bytes", 0),
            "payload_bytes": cm_exec.get("returned_bytes", 0),
            "payload_tokens": codemode_payload_tokens,
            "total_context_tokens": codemode_total_tokens,
            "token_exchanges": cm_exchanges,
            "duration_ms": cm_exec.get("duration_ms", 0.0),
            "ok": cm_exec.get("ok", False),
            "error": cm_exec.get("error"),
            "result": cm_exec.get("result"),
            "run_id": cm_exec.get("run_id"),
        },
        "deltas": {
            "context_token_savings_pct": token_savings_pct,
            "round_trips_saved": classic_round_trips - codemode_round_trips,
            "payload_bytes_saved": max(
                0,
                int(cm_exec.get("raw_upstream_bytes", 0)) - int(cm_exec.get("returned_bytes", 0)),
            ),
        },
        "winner": winner,
        "tradeoff_notes": notes,
    }
