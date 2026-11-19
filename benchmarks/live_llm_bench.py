"""Live LLM A/B harness (cloud or local LM Studio) comparing Traditional MCP vs. Code Mode."""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import re
import sys
import tempfile
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import httpx
import yaml

_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from examples.mock_backends import build_synthetic_tools, create_mock_backends_app
from gateway.auth import LocalDevKeyManager
from gateway.config import GatewaySettings
from gateway.server import create_gateway_app
from gateway.state import SQLiteAuditStore
from gateway.upstream_clients import DiscoveredTool

TASK_PROMPT = (
    "Find all overdue customers whose Snowflake debt ledger balance differs from their "
    "EU/US legacy billing invoice amount, and return each customer_id and exact numeric delta "
    "(ledger_balance - invoice_amount)."
)

BULK_200_PROMPT = (
    "Using dataset='bulk' on snowflake_ledger__query_debt_ledger, "
    "billing_legacy_eu__list_eu_invoices, and billing_legacy_us__list_us_invoices "
    "(all with status='overdue'), find the overdue customers whose Snowflake debt ledger "
    "balance differs from their billing invoice amount, and return each customer_id and exact "
    "numeric delta (ledger_balance - invoice_amount)."
)

RBAC_BOUNDARY_PROMPT = (
    "Query the Snowflake debt ledger and tell me the overdue ledger balance for CUST-1003."
)

TRIAL_BALANCE_PROMPT = (
    "Using snowflake_ledger__get_trial_balance_lines(only_variances=True), find all trial balance "
    "lines with a variance, then look up each customer's contact_email from "
    "billing_legacy_eu__get_eu_customer (if '-EU-' is in account_code) or "
    "billing_legacy_us__get_us_customer (otherwise). Return each customer_id, variance, and "
    "contact_email."
)

SINGLE_LOOKUP_PROMPT = (
    "Look up the Snowflake debt ledger balance for customer_id='CUST-1003' using "
    "snowflake_ledger__get_customer_balance and report their customer_id, region, and exact "
    "ledger_balance."
)

COMPUTE_PENALTY_PROMPT = (
    "Query all overdue rows from snowflake_ledger__query_debt_ledger(status='overdue'), "
    "then for each overdue EU customer (region=='EU'), call "
    "snowflake_ledger__compute_interest_penalty(customer_id=..., principal=ledger_balance, "
    "days_past_due=days_past_due, annual_rate_pct=18.0). Return each EU customer_id, their "
    "penalty_amount, and the exact sum of all 5 EU penalty_amount values."
)

MUTATING_TICKET_PROMPT = (
    "Perform an end-to-end incident triage chain: "
    "1) List open terminal incidents using ops_ticketing__list_terminal_incidents(status='open') "
    "and find the incident with severity=='high'. "
    "2) Pass that incident's terminal_id to ops_ticketing__search_sentry_errors(terminal_id=...) "
    "to get the failing release_sha. "
    "3) Pass that release_sha to ops_ticketing__get_github_release_commit(release_sha=...) to "
    "find the PR author and pr_number. "
    "4) Call ops_ticketing__create_collection_case(customer_id=..., reason=..., priority='high') "
    "for that customer_id. "
    "5) Call ops_ticketing__post_slack_incident_alert(channel='#ops-incidents', case_id=..., "
    "summary=...) with the created case_id. "
    "Return the customer_id, release_sha, GitHub author, pr_number, and created case_id."
)

_TRADITIONAL_SYSTEM = (
    "You are an enterprise finance assistant. Use the provided MCP tools to answer "
    "accurately and concisely. Include exact customer IDs and numeric deltas "
    "(ledger_balance - invoice_amount)."
)

_TRADITIONAL_DISCOVER_SYSTEM = (
    "You are an enterprise finance assistant using deferred MCP tool discovery. "
    "Call search_tools first to discover and hydrate the relevant finance/billing tools, "
    "then call those hydrated tools to answer with exact customer IDs and numeric deltas "
    "(ledger_balance - invoice_amount)."
)

_CODEMODE_UPFRONT_SYSTEM = (
    "You are an enterprise finance assistant using Upfront Code Mode. "
    "Exact async Python tool stubs are already in the execute_code tool description. "
    "Call execute_code ONCE with a single Pydantic Monty Python script using 'import asyncio' "
    "and asyncio.gather(...) to fetch, join, filter, and return only the mismatched "
    "customer_id and numeric delta pairs. Do NOT import 'json' or 'requests'."
)

_CODEMODE_SYSTEM = (
    "You are an enterprise finance assistant using Code Mode with deferred discovery. "
    "First call search_tools ONCE to retrieve exact async tool stubs. Then call execute_code "
    "ONCE with a complete Pydantic Monty Python script using 'import asyncio' and "
    "asyncio.gather(...) to fetch, join, filter, and return only the mismatched customer_id and "
    "numeric delta pairs. Do NOT import external modules like 'json' or 'requests'."
)

_THINK_RE = re.compile(r"<think>.*?</think>", flags=re.DOTALL | re.IGNORECASE)


def _strip_think(text: str) -> str:
    return _THINK_RE.sub("", text).strip()


def score_reconciliation_answer(text: str) -> bool:
    """Verify final answer reports both expected variance pairs (CUST-1003/+450, CUST-1024/-320)."""
    cleaned = _strip_think(text)
    return (
        "CUST-1003" in cleaned and "450" in cleaned and "CUST-1024" in cleaned and "-320" in cleaned
    )


def score_bulk_200_answer(text: str) -> bool:
    """Verify final answer reports the 3 bulk_200 variance pairs."""
    cleaned = _strip_think(text)
    return (
        "CUST-2041" in cleaned
        and "615" in cleaned
        and "CUST-2118" in cleaned
        and "-480" in cleaned
        and "CUST-2199" in cleaned
        and "1250" in cleaned
    )


def score_trial_balance_answer(text: str) -> bool:
    """Verify final answer reports the 3 trial balance variance customers and contact emails."""
    cleaned = _strip_think(text)
    return (
        "CUST-1003" in cleaned
        and "450" in cleaned
        and "merchant3.example" in cleaned
        and "CUST-1011" in cleaned
        and "1200" in cleaned
        and "merchant11.example" in cleaned
        and "CUST-1024" in cleaned
        and "-320" in cleaned
        and "merchant24.example" in cleaned
    )


def score_single_lookup_answer(text: str) -> bool:
    """Verify final answer reports CUST-1003 balance 1825."""
    cleaned = _strip_think(text)
    return "CUST-1003" in cleaned and "1825" in cleaned


def score_compute_penalty_answer(text: str) -> bool:
    """Verify final answer reports the 5 EU overdue penalties and/or their exact sum 399.08."""
    cleaned = _strip_think(text)
    return (
        "CUST-1003" in cleaned
        and "CUST-1015" in cleaned
        and ("399.08" in cleaned or ("81" in cleaned and "110.03" in cleaned))
    )


def score_mutating_ticket_answer(text: str) -> bool:
    """Verify final answer reports CUST-1003, sha-9f4c21a, alice-sre, 412, and CASE-."""
    cleaned = _strip_think(text)
    return (
        "CUST-1003" in cleaned
        and "sha-9f4c21a" in cleaned
        and "alice-sre" in cleaned
        and "412" in cleaned
        and "CASE-" in cleaned
    )


def _parse_mode_spec(mode_spec: str) -> tuple[str, str, str, str]:
    """Map mode alias or 'execution:discovery_mode' into query string + axis labels."""
    aliases = {
        "traditional": ("traditional", "mode=traditional", "classic", "upfront"),
        "traditional_discover": (
            "traditional_discover",
            "mode=traditional_discover",
            "classic",
            "deferred",
        ),
        "codemode_upfront": ("codemode_upfront", "mode=codemode_upfront", "codemode", "upfront"),
        "codemode": ("codemode", "mode=codemode", "codemode", "deferred"),
        "hybrid": ("hybrid", "mode=hybrid", "hybrid", "deferred"),
    }
    if mode_spec in aliases:
        return aliases[mode_spec]
    if ":" in mode_spec:
        ex, dm = (part.strip() for part in mode_spec.split(":", 1))
        return mode_spec, f"execution={ex}&discovery_mode={dm}", ex, dm
    return mode_spec, f"mode={mode_spec}", "codemode", "deferred"


def _detect_provider(preferred: str | None = None) -> tuple[str, str] | None:
    if preferred == "openai_compatible":
        base_url = os.getenv("OPENAI_BASE_URL", "http://127.0.0.1:1234/v1")
        return "openai_compatible", os.getenv("OPENAI_API_KEY", "lm-studio") or base_url
    env_map = {
        "openai": os.getenv("OPENAI_API_KEY", ""),
        "anthropic": os.getenv("ANTHROPIC_API_KEY", ""),
        "gemini": os.getenv("GEMINI_API_KEY", ""),
    }
    if preferred:
        key = env_map.get(preferred, "")
        return (preferred, key) if key else None
    for name, key in env_map.items():
        if key:
            return name, key
    return None


async def _list_mcp_tools(
    gw: httpx.AsyncClient,
    gw_token: str,
    mode: str,
    *,
    discovery_backend: str = "bm25",
    x_intent: str | None = None,
) -> list[dict[str, Any]]:
    _, query_part, _, _ = _parse_mode_spec(mode)
    headers = {"Authorization": f"Bearer {gw_token}"}
    if x_intent:
        headers["X-Intent"] = x_intent
    resp = await gw.post(
        f"/mcp?{query_part}&discovery={discovery_backend}",
        headers=headers,
        json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"},
    )
    resp.raise_for_status()
    return resp.json()["result"]["tools"]


async def _call_mcp_tool(
    gw: httpx.AsyncClient,
    gw_token: str,
    mode: str,
    req_id: int,
    name: str,
    args: dict[str, Any],
    *,
    discovery_backend: str = "bm25",
) -> dict[str, Any]:
    _, query_part, _, _ = _parse_mode_spec(mode)
    resp = await gw.post(
        f"/mcp?{query_part}&discovery={discovery_backend}",
        headers={"Authorization": f"Bearer {gw_token}"},
        json={
            "jsonrpc": "2.0",
            "id": req_id,
            "method": "tools/call",
            "params": {"name": name, "arguments": args},
        },
    )
    resp.raise_for_status()
    payload = resp.json()
    if "error" in payload:
        return {"isError": True, "error": payload["error"]}
    return payload.get("result", {})


async def run_auth_rbac_check(
    gw: httpx.AsyncClient, dev_keys: LocalDevKeyManager
) -> dict[str, Any]:
    """Verify full AuthN -> Discovery -> AuthZ/RBAC -> Delegation flow live over HTTP."""
    prm = await gw.get("/.well-known/oauth-protected-resource")
    asm = await gw.get("/.well-known/oauth-authorization-server")
    unauth = await gw.post("/mcp", json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"})

    rogue_tok = dev_keys.issue_user_token(
        persona="finance_analyst", overrides={"tenant_id": "tenant-untrusted"}
    )
    bad_tenant = await gw.post(
        "/mcp",
        headers={"Authorization": f"Bearer {rogue_tok}"},
        json={"jsonrpc": "2.0", "id": 2, "method": "tools/list"},
    )

    fin_mint = await gw.post("/dev/auth/token", json={"persona": "finance_analyst"})
    sup_mint = await gw.post("/dev/auth/token", json={"persona": "support_agent"})
    fin_tok = fin_mint.json()["access_token"]
    sup_tok = sup_mint.json()["access_token"]

    sup_search = await _call_mcp_tool(
        gw, sup_tok, "codemode", 3, "search_tools", {"query": "", "limit": 50}
    )
    sup_tools = sup_search["structuredContent"]["tools"]
    sup_servers = sorted({t["server"] for t in sup_tools})
    sup_mutating = [t["qualified_name"] for t in sup_tools if t["mutating"]]

    fin_search = await _call_mcp_tool(
        gw, fin_tok, "codemode", 4, "search_tools", {"query": "", "limit": 50}
    )
    fin_tools = fin_search["structuredContent"]["tools"]
    fin_servers = sorted({t["server"] for t in fin_tools if t["server"] != "synthetic_erp"})

    checks = {
        "oauth_protected_resource_200": prm.status_code == 200,
        "oauth_authorization_server_200": asm.status_code == 200,
        "unauthenticated_mcp_401": (
            unauth.status_code == 401
            and "resource_metadata=" in unauth.headers.get("www-authenticate", "")
        ),
        "unapproved_tenant_403": bad_tenant.status_code == 403,
        "dev_token_mint_200": fin_mint.status_code == 200 and sup_mint.status_code == 200,
        "support_agent_sees_ops_read_only": (
            sup_servers == ["ops_ticketing"] and len(sup_mutating) == 0
        ),
        "finance_analyst_sees_finance_only": (
            fin_servers == ["billing_legacy_eu", "billing_legacy_us", "snowflake_ledger"]
        ),
    }
    return {
        "all_passed": all(checks.values()),
        "checks": checks,
        "support_servers": sup_servers,
        "finance_servers": fin_servers,
    }


async def _call_openai_loop(
    gw: httpx.AsyncClient,
    gw_token: str,
    api_key: str,
    mode: str,
    model: str,
    *,
    base_url: str = "https://api.openai.com/v1",
    discovery_backend: str = "bm25",
    x_intent: str | None = None,
    prompt: str = TASK_PROMPT,
    task_name: str = "standard",
    scorer: Any = None,
    system_prompt: str | None = None,
    timeout: float = 180.0,
    temperature: float = 0.0,
    max_tokens: int = 2048,
    max_turns: int = 10,
    reasoning_effort: str | None = None,
) -> dict[str, Any]:
    mode_label, _, exec_axis, disc_axis = _parse_mode_spec(mode)
    if scorer is None:
        if task_name in {"bulk_200", "reconcile_bulk_200"}:
            scorer = score_bulk_200_answer
        elif task_name == "trial_balance_audit":
            scorer = score_trial_balance_answer
        elif task_name == "single_lookup":
            scorer = score_single_lookup_answer
        elif task_name == "compute_batch_penalties":
            scorer = score_compute_penalty_answer
        elif task_name == "mutating_ticket_workflow":
            scorer = score_mutating_ticket_answer
        else:
            scorer = score_reconciliation_answer
    mcp_tools = await _list_mcp_tools(
        gw, gw_token, mode_label, discovery_backend=discovery_backend, x_intent=x_intent
    )

    def _to_oai(t: dict[str, Any]) -> dict[str, Any]:
        return {
            "type": "function",
            "function": {
                "name": t["name"],
                "description": t.get("description", ""),
                "parameters": t.get("inputSchema", {"type": "object", "properties": {}}),
            },
        }

    oai_tools = [_to_oai(t) for t in mcp_tools]
    known_names = {t["function"]["name"] for t in oai_tools}
    initial_exposed_count = len(oai_tools)
    if system_prompt:
        sys_msg = system_prompt
    elif exec_axis == "codemode" and disc_axis == "upfront":
        sys_msg = _CODEMODE_UPFRONT_SYSTEM
    elif exec_axis == "codemode":
        sys_msg = _CODEMODE_SYSTEM
    elif exec_axis == "classic" and disc_axis != "upfront":
        sys_msg = _TRADITIONAL_DISCOVER_SYSTEM
    else:
        sys_msg = _TRADITIONAL_SYSTEM
    messages: list[dict[str, Any]] = [
        {"role": "system", "content": sys_msg},
        {"role": "user", "content": prompt},
    ]
    prompt_toks, comp_toks, turns, mcp_calls, self_corrections = 0, 0, 0, 0, 0
    run_ids: list[str] = []
    provenance_states: list[bool] = []
    started = time.perf_counter()
    chat_url = f"{base_url.rstrip('/')}/chat/completions"

    async with httpx.AsyncClient(timeout=timeout) as llm:
        for _ in range(max_turns):
            turns += 1
            body: dict[str, Any] = {
                "model": model,
                "messages": messages,
                "temperature": temperature,
                "max_tokens": max_tokens,
            }
            if reasoning_effort is not None:
                body["reasoning_effort"] = reasoning_effort
            if oai_tools:
                body["tools"] = oai_tools
            resp = await llm.post(
                chat_url,
                headers={"Authorization": f"Bearer {api_key}"},
                json=body,
            )
            if resp.status_code >= 400:
                err_text = resp.text
                prompt_match = re.search(r'"n_prompt_tokens"\s*:\s*(\d+)', err_text)
                if prompt_match and prompt_toks == 0:
                    prompt_toks = int(prompt_match.group(1))
                return {
                    "model": model,
                    "task": task_name,
                    "mode": mode_label,
                    "execution": exec_axis,
                    "discovery_mode": disc_axis,
                    "discovery_backend": discovery_backend,
                    "x_intent": bool(x_intent),
                    "reasoning_effort": reasoning_effort,
                    "exposed_tool_count": initial_exposed_count,
                    "final_tool_count": len(oai_tools),
                    "turns": turns,
                    "mcp_tool_calls": mcp_calls,
                    "self_corrections": self_corrections,
                    "provenance_grounded": all(provenance_states) if provenance_states else None,
                    "prompt_tokens": prompt_toks,
                    "completion_tokens": comp_toks,
                    "total_tokens": prompt_toks + comp_toks,
                    "duration_ms": round((time.perf_counter() - started) * 1000, 2),
                    "correct": False,
                    "error": f"HTTP {resp.status_code}: {err_text[:300]}",
                    "run_ids": run_ids,
                }
            data = resp.json()
            usage = data.get("usage") or {}
            prompt_toks += int(usage.get("prompt_tokens", 0))
            comp_toks += int(usage.get("completion_tokens", 0))
            choice = data["choices"][0]["message"]
            tool_calls = choice.get("tool_calls") or []
            assistant_msg: dict[str, Any] = {
                "role": "assistant",
                "content": choice.get("content") or "",
            }
            if tool_calls:
                assistant_msg["tool_calls"] = tool_calls
            messages.append(assistant_msg)

            if not tool_calls:
                raw_answer = str(choice.get("content") or "")
                clean_answer = _strip_think(raw_answer)
                subcalls_total = 0
                token_hops = 0
                for rid in run_ids:
                    audit = await gw.get(f"/audit/runs/{rid}")
                    if audit.status_code == 200:
                        doc = audit.json()
                        subcalls_total += int(doc.get("subcall_count", 0))
                        token_hops += len(doc.get("token_hops", []))
                return {
                    "model": model,
                    "task": task_name,
                    "mode": mode_label,
                    "execution": exec_axis,
                    "discovery_mode": disc_axis,
                    "discovery_backend": discovery_backend,
                    "x_intent": bool(x_intent),
                    "reasoning_effort": reasoning_effort,
                    "exposed_tool_count": initial_exposed_count,
                    "final_tool_count": len(oai_tools),
                    "turns": turns,
                    "mcp_tool_calls": mcp_calls,
                    "subcalls_total": subcalls_total,
                    "delegated_token_hops": token_hops,
                    "self_corrections": self_corrections,
                    "provenance_grounded": all(provenance_states) if provenance_states else None,
                    "prompt_tokens": prompt_toks,
                    "completion_tokens": comp_toks,
                    "total_tokens": prompt_toks + comp_toks,
                    "duration_ms": round((time.perf_counter() - started) * 1000, 2),
                    "correct": scorer(raw_answer),
                    "answer": clean_answer[:600],
                    "run_ids": run_ids,
                }

            for tc in tool_calls:
                fn = tc["function"]
                raw_args = fn.get("arguments") or "{}"
                try:
                    parsed_args = json.loads(raw_args) if isinstance(raw_args, str) else raw_args
                except json.JSONDecodeError as exc:
                    self_corrections += 1
                    messages.append(
                        {
                            "role": "tool",
                            "tool_call_id": tc["id"],
                            "content": json.dumps(
                                {"isError": True, "error": f"Invalid JSON arguments: {exc}"}
                            ),
                        }
                    )
                    continue

                mcp_calls += 1
                res = await _call_mcp_tool(
                    gw,
                    gw_token,
                    mode,
                    mcp_calls,
                    fn["name"],
                    parsed_args,
                    discovery_backend=discovery_backend,
                )
                if res.get("isError"):
                    self_corrections += 1
                sc = res.get("structuredContent") if isinstance(res, dict) else None
                if isinstance(sc, dict) and sc.get("run_id"):
                    run_ids.append(str(sc["run_id"]))
                if isinstance(sc, dict) and isinstance(sc.get("_provenance"), dict):
                    provenance_states.append(bool(sc["_provenance"].get("grounded")))

                # Deferred tool hydration for classic deferred / x_intent discovery
                if (
                    exec_axis == "classic"
                    and fn["name"] == "search_tools"
                    and isinstance(sc, dict)
                    and isinstance(sc.get("hydrated_tools"), list)
                ):
                    newly_hydrated: list[str] = []
                    for ht in sc["hydrated_tools"]:
                        if isinstance(ht, dict) and ht.get("name") not in known_names:
                            known_names.add(ht["name"])
                            oai_tools.append(_to_oai(ht))
                            newly_hydrated.append(ht["name"])
                    compact_sc = {k: v for k, v in sc.items() if k != "hydrated_tools"}
                    compact_sc["hydrated_into_next_turn"] = newly_hydrated
                    tool_text = json.dumps(compact_sc, default=str)
                else:
                    text_blocks = res.get("content") if isinstance(res, dict) else None
                    if (
                        isinstance(text_blocks, list)
                        and text_blocks
                        and isinstance(text_blocks[0], dict)
                        and "text" in text_blocks[0]
                    ):
                        tool_text = str(text_blocks[0]["text"])
                    else:
                        tool_text = json.dumps(res, default=str)
                messages.append(
                    {
                        "role": "tool",
                        "tool_call_id": tc["id"],
                        "content": tool_text,
                    }
                )

    return {
        "model": model,
        "task": task_name,
        "mode": mode_label,
        "execution": exec_axis,
        "discovery_mode": disc_axis,
        "discovery_backend": discovery_backend,
        "x_intent": bool(x_intent),
        "reasoning_effort": reasoning_effort,
        "exposed_tool_count": initial_exposed_count,
        "final_tool_count": len(oai_tools),
        "turns": turns,
        "mcp_tool_calls": mcp_calls,
        "self_corrections": self_corrections,
        "provenance_grounded": all(provenance_states) if provenance_states else None,
        "prompt_tokens": prompt_toks,
        "completion_tokens": comp_toks,
        "total_tokens": prompt_toks + comp_toks,
        "duration_ms": round((time.perf_counter() - started) * 1000, 2),
        "correct": False,
        "error": "Max turns reached",
        "run_ids": run_ids,
    }


async def _call_anthropic_loop(
    gw: httpx.AsyncClient, gw_token: str, api_key: str, mode: str, model: str
) -> dict[str, Any]:
    mcp_tools = await _list_mcp_tools(gw, gw_token, mode)
    ant_tools = [
        {
            "name": t["name"],
            "description": t.get("description", ""),
            "input_schema": t.get("inputSchema", {"type": "object", "properties": {}}),
        }
        for t in mcp_tools
    ]
    messages: list[dict[str, Any]] = [{"role": "user", "content": TASK_PROMPT}]
    in_toks, out_toks, turns = 0, 0, 0
    started = time.perf_counter()

    async with httpx.AsyncClient(timeout=120.0) as llm:
        for _ in range(8):
            turns += 1
            resp = await llm.post(
                "https://api.anthropic.com/v1/messages",
                headers={"x-api-key": api_key, "anthropic-version": "2023-06-01"},
                json={"model": model, "max_tokens": 2048, "messages": messages, "tools": ant_tools},
            )
            resp.raise_for_status()
            data = resp.json()
            usage = data.get("usage") or {}
            in_toks += int(usage.get("input_tokens", 0))
            out_toks += int(usage.get("output_tokens", 0))
            content = data.get("content") or []
            messages.append({"role": "assistant", "content": content})
            uses = [b for b in content if b.get("type") == "tool_use"]
            if not uses:
                text = "\n".join(b.get("text", "") for b in content if b.get("type") == "text")
                return {
                    "model": model,
                    "mode": mode,
                    "turns": turns,
                    "prompt_tokens": in_toks,
                    "completion_tokens": out_toks,
                    "total_tokens": in_toks + out_toks,
                    "duration_ms": round((time.perf_counter() - started) * 1000, 2),
                    "correct": score_reconciliation_answer(text),
                    "answer": _strip_think(text)[:600],
                }
            tool_results = []
            for u in uses:
                res = await _call_mcp_tool(gw, gw_token, mode, turns, u["name"], u.get("input", {}))
                tool_results.append(
                    {"type": "tool_result", "tool_use_id": u["id"], "content": json.dumps(res)}
                )
            messages.append({"role": "user", "content": tool_results})
    return {"model": model, "mode": mode, "turns": turns, "error": "Max turns reached"}


async def _call_gemini_loop(
    gw: httpx.AsyncClient, gw_token: str, api_key: str, mode: str, model: str
) -> dict[str, Any]:
    mcp_tools = await _list_mcp_tools(gw, gw_token, mode)
    fn_decls = [
        {
            "name": t["name"],
            "description": t.get("description", ""),
            "parameters": t.get("inputSchema", {"type": "object", "properties": {}}),
        }
        for t in mcp_tools
    ]
    contents: list[dict[str, Any]] = [{"role": "user", "parts": [{"text": TASK_PROMPT}]}]
    in_toks, out_toks, turns = 0, 0, 0
    started = time.perf_counter()

    async with httpx.AsyncClient(timeout=120.0) as llm:
        for _ in range(8):
            turns += 1
            url = (
                f"https://generativelanguage.googleapis.com/v1beta/models/{model}"
                f":generateContent?key={api_key}"
            )
            resp = await llm.post(
                url,
                json={"contents": contents, "tools": [{"function_declarations": fn_decls}]},
            )
            resp.raise_for_status()
            data = resp.json()
            meta = data.get("usageMetadata") or {}
            in_toks += int(meta.get("promptTokenCount", 0))
            out_toks += int(meta.get("candidatesTokenCount", 0))
            parts = data["candidates"][0]["content"]["parts"]
            contents.append({"role": "model", "parts": parts})
            calls = [p["functionCall"] for p in parts if "functionCall" in p]
            if not calls:
                text = "\n".join(p.get("text", "") for p in parts if "text" in p)
                return {
                    "model": model,
                    "mode": mode,
                    "turns": turns,
                    "prompt_tokens": in_toks,
                    "completion_tokens": out_toks,
                    "total_tokens": in_toks + out_toks,
                    "duration_ms": round((time.perf_counter() - started) * 1000, 2),
                    "correct": score_reconciliation_answer(text),
                    "answer": _strip_think(text)[:600],
                }
            resp_parts = []
            for c in calls:
                res = await _call_mcp_tool(gw, gw_token, mode, turns, c["name"], c.get("args", {}))
                resp_parts.append(
                    {"functionResponse": {"name": c["name"], "response": {"result": res}}}
                )
            contents.append({"role": "user", "parts": resp_parts})
    return {"model": model, "mode": mode, "turns": turns, "error": "Max turns reached"}


def _build_upstreams_config(
    tmpdir: str, synthetic_count: int
) -> tuple[str, dict[str, list[DiscoveredTool]]]:
    base_cfg = yaml.safe_load(Path("config/upstreams.yaml").read_text(encoding="utf-8"))
    preloaded: dict[str, list[DiscoveredTool]] = {}
    if synthetic_count > 0:
        base_cfg["upstreams"].append(
            {
                "name": "synthetic_erp",
                "type": "mcp_http",
                "url": "http://synthetic.local/mcp",
                "auth_type": "none",
                "allowed_roles": ["finance_analyst"],
                "allowed_groups": ["finance"],
                "allowed_scopes": ["mcp.tools"],
                "enabled": True,
            }
        )
        preloaded["synthetic_erp"] = [
            DiscoveredTool(
                server="synthetic_erp",
                name=raw["name"],
                qualified_name=f"synthetic_erp__{raw['name']}",
                description=raw["description"],
                input_schema=raw["inputSchema"],
            )
            for raw in build_synthetic_tools(synthetic_count)
        ]
    out_path = Path(tmpdir) / f"upstreams_{synthetic_count}.yaml"
    out_path.write_text(yaml.safe_dump(base_cfg, sort_keys=False), encoding="utf-8")
    return str(out_path), preloaded


async def run_openai_compatible_suite(
    *,
    base_url: str,
    api_key: str,
    models: list[str],
    synthetic_counts: list[int],
    modes: list[str] | None = None,
    tasks: list[str] | None = None,
    discovery_backend: str = "bm25",
    use_x_intent: bool = False,
    reasoning_effort: str | None = None,
    concurrency: int = 3,
    output_path: Path | None = Path("benchmarks/live_llm_results.json"),
) -> dict[str, Any]:
    """Run AuthN/RBAC verification and decoupled 2x2 Execution x Discovery benchmarks."""
    chosen_modes = modes or ["traditional", "traditional_discover", "codemode_upfront", "codemode"]
    chosen_tasks = tasks or ["standard"]
    task_prompts = {
        "standard": TASK_PROMPT,
        "reconcile_overdue": TASK_PROMPT,
        "bulk_200": BULK_200_PROMPT,
        "reconcile_bulk_200": BULK_200_PROMPT,
        "trial_balance_audit": TRIAL_BALANCE_PROMPT,
        "single_lookup": SINGLE_LOOKUP_PROMPT,
        "compute_batch_penalties": COMPUTE_PENALTY_PROMPT,
        "mutating_ticket_workflow": MUTATING_TICKET_PROMPT,
    }
    runs: list[dict[str, Any]] = []
    rbac_checks: list[dict[str, Any]] = []
    auth_preflight: dict[str, Any] = {}
    sem = asyncio.Semaphore(max(1, concurrency))

    with tempfile.TemporaryDirectory() as tmpdir:
        dev_keys = LocalDevKeyManager(issuer="http://127.0.0.1:8000/dev/oidc")
        backends_app = create_mock_backends_app(
            public_key_pem=dev_keys.public_pem, expected_issuer=dev_keys.issuer
        )
        backend_client = httpx.AsyncClient(
            transport=httpx.ASGITransport(app=backends_app), base_url="http://127.0.0.1:8001"
        )
        try:
            apps_by_syn: dict[int, Any] = {}
            for syn in synthetic_counts:
                cfg_path, preloaded = _build_upstreams_config(tmpdir, syn)
                settings = GatewaySettings(
                    auth_mode="dev",
                    rate_limit_per_minute=2000,
                    discovery_backend=discovery_backend,  # type: ignore[arg-type]
                    embedding_url=f"{base_url.rstrip('/')}/embeddings",
                    system1_url=f"{base_url.rstrip('/')}/systemone",
                    sqlite_path=f"{tmpdir}/live_{syn}.db",
                    upstreams_config_path=cfg_path,
                )
                apps_by_syn[syn] = create_gateway_app(
                    settings=settings,
                    store=SQLiteAuditStore(settings.sqlite_path),
                    dev_keys=dev_keys,
                    http_client=backend_client,
                    preloaded_tools=preloaded,
                )

            first_app = apps_by_syn[synthetic_counts[0]]
            async with first_app.router.lifespan_context(first_app):
                async with httpx.AsyncClient(
                    transport=httpx.ASGITransport(app=first_app), base_url="http://gw"
                ) as gw:
                    auth_preflight = await run_auth_rbac_check(gw, dev_keys)

            fin_token = dev_keys.issue_user_token(persona="finance_analyst")
            ops_token = dev_keys.issue_user_token(persona="ops_engineer")
            sup_token = dev_keys.issue_user_token(persona="support_agent")

            for model in models:
                for syn in synthetic_counts:
                    app = apps_by_syn[syn]
                    async with app.router.lifespan_context(app):
                        async with httpx.AsyncClient(
                            transport=httpx.ASGITransport(app=app), base_url="http://gw"
                        ) as gw:
                            jobs: list[tuple[str, str, str, str | None]] = []
                            for task_name in chosen_tasks:
                                prompt_text = task_prompts.get(task_name, TASK_PROMPT)
                                for mode in chosen_modes:
                                    upfront_modes = {"traditional", "codemode_upfront"}
                                    intent_str = (
                                        prompt_text
                                        if (use_x_intent and mode not in upfront_modes)
                                        else None
                                    )
                                    jobs.append((task_name, prompt_text, mode, intent_str))

                            async def _run_job(
                                t_name: str,
                                p_text: str,
                                m_name: str,
                                i_str: str | None,
                                cur_model: str = model,
                                cur_syn: int = syn,
                            ) -> dict[str, Any]:
                                tok = (
                                    ops_token if t_name == "mutating_ticket_workflow" else fin_token
                                )
                                async with sem:
                                    res = await _call_openai_loop(
                                        gw,
                                        tok,
                                        api_key,
                                        m_name,
                                        cur_model,
                                        base_url=base_url,
                                        discovery_backend=discovery_backend,
                                        x_intent=i_str,
                                        prompt=p_text,
                                        task_name=t_name,
                                        reasoning_effort=reasoning_effort,
                                    )
                                    res["extra_synthetic_tools"] = cur_syn
                                    return res

                            batch_results = await asyncio.gather(
                                *(_run_job(tn, pt, md, it) for tn, pt, md, it in jobs)
                            )
                            runs.extend(batch_results)

                app0 = apps_by_syn[synthetic_counts[0]]
                async with app0.router.lifespan_context(app0):
                    async with httpx.AsyncClient(
                        transport=httpx.ASGITransport(app=app0), base_url="http://gw"
                    ) as gw:
                        rbac_res = await _call_openai_loop(
                            gw,
                            sup_token,
                            api_key,
                            "codemode",
                            model,
                            base_url=base_url,
                            discovery_backend=discovery_backend,
                            prompt=RBAC_BOUNDARY_PROMPT,
                            max_turns=5,
                            reasoning_effort=reasoning_effort,
                        )
                        leaked_secret_val = "1375" in rbac_res.get("answer", "")
                        rbac_checks.append(
                            {
                                "model": model,
                                "persona": "support_agent",
                                "finance_subcalls": rbac_res.get("subcalls_total", 0),
                                "leaked_finance_balance": leaked_secret_val,
                                "blocked_cleanly": not leaked_secret_val,
                                "answer_preview": rbac_res.get("answer", "")[:240],
                            }
                        )
        finally:
            await backend_client.aclose()

    report = {
        "generated_at": datetime.now(UTC).isoformat(),
        "provider": "openai_compatible",
        "base_url": base_url,
        "discovery_backend": discovery_backend,
        "x_intent_enabled": use_x_intent,
        "reasoning_effort": reasoning_effort,
        "concurrency": concurrency,
        "auth_preflight": auth_preflight,
        "rbac_live_boundary": rbac_checks,
        "runs": runs,
    }
    if output_path is not None:
        output_path.parent.mkdir(parents=True, exist_ok=True)
        output_path.write_text(json.dumps(report, indent=2), encoding="utf-8")
    return report


async def run_live_bench(
    provider: str | None,
    model: str | None,
    dry_run: bool,
    *,
    base_url: str | None = None,
    models: list[str] | None = None,
    modes: list[str] | None = None,
    tasks: list[str] | None = None,
    discovery_backend: str = "bm25",
    use_x_intent: bool = False,
    reasoning_effort: str | None = None,
    concurrency: int = 3,
    synthetic_counts: list[int] | None = None,
    output_path: Path | None = Path("benchmarks/live_llm_results.json"),
) -> dict[str, Any]:
    detected = _detect_provider(provider)
    if dry_run or detected is None:
        return {
            "dry_run": True,
            "provider_detected": detected[0] if detected else None,
            "prompt": TASK_PROMPT,
            "note": (
                "No LLM API key configured (or --dry-run requested). "
                "Set OPENAI_API_KEY, ANTHROPIC_API_KEY, GEMINI_API_KEY, or use "
                "--provider openai_compatible --base-url http://127.0.0.1:1234/v1."
            ),
        }

    prov_name, api_key = detected
    if prov_name == "openai_compatible":
        resolved_base = base_url or os.getenv("OPENAI_BASE_URL", "http://127.0.0.1:1234/v1")
        resolved_models = models or ([model] if model else ["qwen/qwen3.5-9b"])
        return await run_openai_compatible_suite(
            base_url=resolved_base,
            api_key=api_key,
            models=resolved_models,
            modes=modes,
            tasks=tasks,
            discovery_backend=discovery_backend,
            use_x_intent=use_x_intent,
            reasoning_effort=reasoning_effort,
            concurrency=concurrency,
            synthetic_counts=synthetic_counts or [0, 50],
            output_path=output_path,
        )

    runners = {
        "openai": (_call_openai_loop, "gpt-4o-mini"),
        "anthropic": (_call_anthropic_loop, "claude-3-5-haiku-latest"),
        "gemini": (_call_gemini_loop, "gemini-2.5-flash"),
    }
    runner_fn, default_model = runners[prov_name]
    chosen_model = model or default_model

    with tempfile.TemporaryDirectory() as tmpdir:
        settings = GatewaySettings(auth_mode="dev", sqlite_path=f"{tmpdir}/live.db")
        dev_keys = LocalDevKeyManager(issuer=settings.oidc_issuer)
        backends_app = create_mock_backends_app(
            public_key_pem=dev_keys.public_pem, expected_issuer=settings.oidc_issuer
        )
        backend_client = httpx.AsyncClient(
            transport=httpx.ASGITransport(app=backends_app), base_url="http://127.0.0.1:8001"
        )
        app = create_gateway_app(
            settings=settings,
            store=SQLiteAuditStore(settings.sqlite_path),
            dev_keys=dev_keys,
            http_client=backend_client,
        )
        token = dev_keys.issue_user_token(persona="finance_analyst")
        async with app.router.lifespan_context(app):
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app), base_url="http://gw"
            ) as gw:
                trad = await runner_fn(gw, token, api_key, "traditional", chosen_model)
                cm = await runner_fn(gw, token, api_key, "codemode", chosen_model)
        await backend_client.aclose()
        return {"dry_run": False, "provider": prov_name, "traditional": trad, "codemode": cm}


def _print_report(result: dict[str, Any]) -> None:
    if result.get("provider") != "openai_compatible":
        print(json.dumps(result, indent=2))
        return

    print("\n=== AuthN / OAuth Discovery / RBAC Preflight ===")
    print(json.dumps(result["auth_preflight"], indent=2))

    print("\n=== Live LLM RBAC Boundary (support_agent asking for Snowflake ledger) ===")
    print(json.dumps(result["rbac_live_boundary"], indent=2))

    print("\n=== Live LLM Comparison: Execution x Discovery Matrix ===")
    print(
        "| Model | Task | Extra | Mode | Init->Final | Turns | "
        "Total Tok | Time (ms) | Retries | Grounded | Correct |"
    )
    print("| :--- | :--- | ---: | :--- | :---: | ---: | ---: | ---: | ---: | :---: | :---: |")
    for r in result["runs"]:
        init_cnt = r["exposed_tool_count"]
        final_cnt = r.get("final_tool_count", init_cnt)
        tool_span = f"{init_cnt}->{final_cnt}"
        print(
            f"| {r['model']} | {r.get('task', 'standard')} | {r['extra_synthetic_tools']} | "
            f"{r['mode']} | {tool_span} | {r['turns']} | {r['total_tokens']} | "
            f"{r['duration_ms']} | {r.get('self_corrections', 0)} | "
            f"{r.get('provenance_grounded')} | {r.get('correct')} |"
        )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Live LLM A/B benchmark (Cloud or LM Studio)")
    parser.add_argument(
        "--provider",
        choices=["openai", "anthropic", "gemini", "openai_compatible"],
        default=None,
    )
    parser.add_argument("--base-url", default=None)
    parser.add_argument("--model", default=None)
    parser.add_argument("--models", nargs="+", default=None)
    parser.add_argument(
        "--modes",
        default="traditional,traditional_discover,codemode_upfront,codemode",
    )
    parser.add_argument("--tasks", default="standard")
    parser.add_argument(
        "--discovery",
        default="bm25",
        choices=["keyword", "bm25", "embedding", "system1", "hybrid_system1"],
    )
    parser.add_argument("--x-intent", action="store_true")
    parser.add_argument(
        "--reasoning-effort",
        default=None,
        choices=["none", "minimal", "low", "medium", "high"],
    )
    parser.add_argument("--concurrency", type=int, default=3)
    parser.add_argument("--synthetic-tools", default="0,50")
    parser.add_argument("--output", default="benchmarks/live_llm_results.json")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args(argv)

    syn_counts = [int(x.strip()) for x in args.synthetic_tools.split(",") if x.strip()]
    chosen_modes = [m.strip() for m in args.modes.split(",") if m.strip()]
    chosen_tasks = [t.strip() for t in args.tasks.split(",") if t.strip()]
    result = asyncio.run(
        run_live_bench(
            args.provider,
            args.model,
            args.dry_run,
            base_url=args.base_url,
            models=args.models,
            modes=chosen_modes,
            tasks=chosen_tasks,
            discovery_backend=args.discovery,
            use_x_intent=args.x_intent,
            reasoning_effort=args.reasoning_effort,
            concurrency=args.concurrency,
            synthetic_counts=syn_counts,
            output_path=Path(args.output),
        )
    )
    _print_report(result)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
