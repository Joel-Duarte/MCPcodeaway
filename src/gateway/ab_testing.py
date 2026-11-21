"""Algorithmic auto-mode selector, live A/B splitter, and Onboarding A/B Test Lab."""

from __future__ import annotations

import hashlib
import json
import time
from typing import TYPE_CHECKING, Any, Literal

from pydantic import BaseModel, Field

from gateway.auth import CallerIdentity
from gateway.catalog import estimate_tokens, render_python_stub, to_mcp_tool_definition
from gateway.comparator import run_side_by_side_comparison
from gateway.config import (
    DiscoveryMode,
    ExecutionMode,
    GatewayMode,
    GatewaySettings,
    UpstreamConfig,
)
from gateway.state import InMemoryCacheBackend, SQLiteAuditStore
from gateway.upstream_clients import DiscoveredTool, inspect_upstream, validate_upstream_url

if TYPE_CHECKING:
    from gateway.server import GatewayRuntime

_BATCH_INTENT_KEYWORDS = (
    "reconcile",
    "join",
    "compare",
    "all",
    "bulk",
    "audit",
    "filter",
    "aggregate",
    "summarize",
    "report",
    "cross",
    "correlate",
)


class ModeFactor(BaseModel):
    """Single scored factor explaining an algorithmic execution mode choice."""

    name: str
    delta: float
    detail: str


class ModeDecision(BaseModel):
    """Explainable decision from the algorithmic Classic vs. Code Mode scorer."""

    execution: ExecutionMode
    discovery_mode: DiscoveryMode
    gateway_mode: GatewayMode
    score: float
    reason: str
    factors: list[ModeFactor] = Field(default_factory=list)


class OnboardingLlmConfig(BaseModel):
    """Optional Admin/Dev OpenAI-compatible LLM target for live onboarding A/B tests."""

    base_url: str
    model: str
    api_key: str = ""
    prompt: str
    expected_substrings: list[str] = Field(default_factory=list)
    max_turns: int = Field(default=4, ge=1, le=8)


class OnboardingAbTestRequest(BaseModel):
    """Request payload for in-wizard Classic vs. Code Mode onboarding A/B tests."""

    upstream: UpstreamConfig | None = None
    upstream_name: str | None = None
    classic_calls: list[dict[str, Any]] | None = None
    codemode_code: str | None = None
    sample_payload_bytes: int = Field(default=0, ge=0)
    intent: str | None = None
    dry_run: bool = False
    llm_config: OnboardingLlmConfig | None = None


def compute_empirical_model_delta(
    runs: list[dict[str, Any]],
    model_name: str,
    *,
    target_tool: str | None = None,
    turn_penalty_pts: float = 8.0,
) -> tuple[float, str] | None:
    """Derive a data-driven [-25, +25] Code Mode score delta from quality, speed & tokens."""
    m_norm = model_name.strip().lower()
    if not m_norm or not runs:
        return None
    model_runs = [r for r in runs if str(r.get("model_name") or "").strip().lower() == m_norm]
    if not model_runs:
        return None
    if target_tool:
        tool_runs = [r for r in model_runs if _run_matches_tool(r, target_tool)]
        if len(tool_runs) >= 2:
            model_runs = tool_runs

    cm_runs = [
        r
        for r in model_runs
        if str(r.get("ab_variant") or "") == "codemode"
        or str(r.get("mode") or "").startswith("codemode")
        or str(r.get("mode") or "") == "hybrid"
    ]
    cl_runs = [r for r in model_runs if r not in cm_runs]
    if not cm_runs and not cl_runs:
        return None

    cm_n = len(cm_runs)
    cl_n = len(cl_runs)
    cm_ok_rate = (sum(1 for r in cm_runs if r.get("ok")) / cm_n) if cm_n else None
    cl_ok_rate = (sum(1 for r in cl_runs if r.get("ok")) / cl_n) if cl_n else None
    cm_saved = (
        sum(
            float(r.get("estimated_tokens_saved", 0)) + float(r.get("schema_tokens_saved", 0))
            for r in cm_runs
        )
        / cm_n
        if cm_n
        else 0.0
    )
    cm_ms = (sum(float(r.get("duration_ms", 0.0)) for r in cm_runs) / cm_n) if cm_n else 0.0
    cl_ms = (sum(float(r.get("duration_ms", 0.0)) for r in cl_runs) / cl_n) if cl_n else 0.0

    delta = 0.0
    if cm_ok_rate is not None and cl_ok_rate is not None:
        delta += (cm_ok_rate - cl_ok_rate) * 25.0
    elif cm_ok_rate is not None:
        delta += (cm_ok_rate - 0.75) * 30.0
    if cm_saved > 500:
        delta += min(12.0, cm_saved / 400.0)
    if cm_ms > 0 and cl_ms > 0:
        if cm_ms < cl_ms:
            delta += 5.0
        elif cm_ms > cl_ms * 1.5:
            delta -= 5.0
    if cl_n and turn_penalty_pts > 0:
        cl_turns = sum(float(r.get("turns") or 1.0) for r in cl_runs) / cl_n
        if cl_turns > 1.0:
            delta += min(24.0, turn_penalty_pts * (cl_turns - 1.0))

    delta = max(-25.0, min(25.0, round(delta, 1)))
    if abs(delta) < 0.5:
        return None
    ok_pct = round((cm_ok_rate if cm_ok_rate is not None else 1.0) * 100.0, 1)
    return (
        delta,
        (
            f"Learned from {len(model_runs)} runs for '{model_name}' "
            f"(cm_success={ok_pct}%, avg_saved={round(cm_saved, 1)} tok, "
            f"cm_ms={round(cm_ms, 1)})"
        ),
    )


def evaluate_calibration_arm(
    runs: list[dict[str, Any]],
    model_name: str,
    *,
    target_tool: str | None = None,
    threshold: int = 100,
    turn_penalty_pts: float = 8.0,
) -> tuple[str, str]:
    """Run 50/50 traditional vs codemode for the first `threshold` calls, then lock winner."""
    m_norm = model_name.strip().lower()
    matching = [r for r in runs if str(r.get("model_name") or "").strip().lower() == m_norm]
    if target_tool:
        matching = [r for r in matching if _run_matches_tool(r, target_tool)]
    count = len(matching)
    if count < threshold:
        arm = "traditional" if count % 2 == 0 else "codemode"
        return arm, f"calibrating_50_50 ({count}/{threshold})"
    learned = compute_empirical_model_delta(
        matching, model_name, target_tool=target_tool, turn_penalty_pts=turn_penalty_pts
    )
    delta = learned[0] if learned is not None else 0.0
    winner = "codemode" if delta >= 0.0 else "traditional"
    return winner, f"calibrated_lock:{winner} ({count}/{threshold}, delta={delta:+.1f})"


def summarize_model_tool_performance(
    runs: list[dict[str, Any]],
    *,
    turn_penalty_pts: float = 8.0,
    tokens_per_penalty_pt: int = 150,
) -> list[dict[str, Any]]:
    """Aggregate performance per (model, tool, mode_family) for the Model Performance UI."""
    buckets: dict[tuple[str, str, str], dict[str, Any]] = {}
    for r in runs:
        model = str(r.get("model_name") or "unknown")
        raw_mode = str(r.get("ab_variant") or r.get("mode") or "traditional")
        mode_family = (
            "codemode" if raw_mode.startswith("codemode") or raw_mode == "hybrid" else "traditional"
        )
        subcalls = r.get("subcalls") if isinstance(r.get("subcalls"), list) else []
        if r.get("tool_name") == "execute_code" and subcalls:
            targets = list(
                dict.fromkeys(
                    (
                        str(sc.get("server") or "gateway"),
                        f"{sc.get('server')}__{sc.get('tool')}"
                        if "__" not in str(sc.get("tool") or "") and sc.get("server")
                        else str(sc.get("tool") or "execute_code"),
                    )
                    for sc in subcalls
                    if isinstance(sc, dict) and sc.get("tool")
                )
            )
            if not targets:
                targets = [(str(r.get("server_name") or "gateway"), "execute_code")]
        else:
            tool = str(r.get("tool_name") or "unknown")
            server = str(
                r.get("server_name") or (tool.split("__", 1)[0] if "__" in tool else "gateway")
            )
            targets = [(server, tool)]

        for server, tool in targets:
            key = (model, tool, mode_family)
            b = buckets.setdefault(
                key,
                {
                    "model": model,
                    "server": server,
                    "tool": tool,
                    "mode": mode_family,
                    "runs": 0,
                    "ok": 0,
                    "ms": 0.0,
                    "tok": 0,
                    "turns": 0.0,
                },
            )
            b["runs"] += 1
            if r.get("ok"):
                b["ok"] += 1
            b["ms"] += float(r.get("duration_ms") or 0.0)
            b["tok"] += int(r.get("total_tokens") or r.get("tokens_used_est") or 0)
            b["turns"] += float(r.get("turns") or 1.0)

    rows: list[dict[str, Any]] = []
    for (model, tool, mode_family), b in sorted(
        buckets.items(), key=lambda kv: kv[1]["runs"], reverse=True
    ):
        n = b["runs"]
        learned = compute_empirical_model_delta(
            runs, model, target_tool=tool, turn_penalty_pts=turn_penalty_pts
        )
        winner = "codemode" if (learned is None or learned[0] >= 0.0) else "traditional"
        avg_tok = round(b["tok"] / n) if n else 0
        avg_turns = (b["turns"] / n) if n else 1.0
        penalty_tok = (
            round(max(0.0, avg_turns - 1.0) * turn_penalty_pts * tokens_per_penalty_pt)
            if mode_family == "traditional"
            else 0
        )
        rows.append(
            {
                "model": model,
                "server": b["server"],
                "tool": tool,
                "mode": mode_family,
                "runs": n,
                "success_rate_pct": round((b["ok"] / n) * 100.0, 1) if n else 0.0,
                "avg_duration_ms": round(b["ms"] / n, 1) if n else 0.0,
                "avg_tokens": avg_tok,
                "effective_tokens": avg_tok + penalty_tok,
                "recommended_mode": winner,
            }
        )
    return rows


def summarize_rollups(
    rollup_rows: list[dict[str, Any]],
    *,
    turn_penalty_pts: float = 8.0,
    tokens_per_penalty_pt: int = 150,
) -> list[dict[str, Any]]:
    """Compute Model x Tool x Mode performance from pre-aggregated DB rollup rows."""
    by_pair: dict[tuple[str, str], dict[str, dict[str, float]]] = {}
    for row in rollup_rows:
        n = int(row.get("runs") or 0)
        if n <= 0:
            continue
        model = str(row.get("model") or "unknown")
        tool = str(row.get("tool") or "unknown")
        mode = str(row.get("mode") or "traditional")
        avg_tok = float(row.get("sum_tokens") or 0) / n
        avg_turns = float(row.get("sum_turns") or n) / n
        eff_tok = avg_tok + (
            max(0.0, avg_turns - 1.0) * turn_penalty_pts * tokens_per_penalty_pt
            if mode == "traditional"
            else 0.0
        )
        ok_rate = float(row.get("ok_count") or 0) / n
        by_pair.setdefault((model, tool), {})[mode] = {
            "eff_tok": eff_tok,
            "ok_rate": ok_rate,
        }

    out: list[dict[str, Any]] = []
    for row in rollup_rows:
        n = int(row.get("runs") or 0)
        if n <= 0:
            continue
        model = str(row.get("model") or "unknown")
        server = str(row.get("server") or "gateway")
        tool = str(row.get("tool") or "unknown")
        mode = str(row.get("mode") or "traditional")
        avg_tok = round(float(row.get("sum_tokens") or 0) / n)
        avg_turns = float(row.get("sum_turns") or n) / n
        penalty_tok = (
            round(max(0.0, avg_turns - 1.0) * turn_penalty_pts * tokens_per_penalty_pt)
            if mode == "traditional"
            else 0
        )
        pair = by_pair.get((model, tool), {})
        cm = pair.get("codemode")
        tr = pair.get("traditional")
        if cm and tr:
            if abs(cm["ok_rate"] - tr["ok_rate"]) >= 0.15:
                winner = "codemode" if cm["ok_rate"] >= tr["ok_rate"] else "traditional"
            else:
                winner = "codemode" if cm["eff_tok"] <= tr["eff_tok"] else "traditional"
        elif tr and not cm:
            winner = "traditional" if tr["ok_rate"] >= 0.5 and avg_turns <= 1.2 else "codemode"
        else:
            winner = "codemode"
        out.append(
            {
                "model": model,
                "server": server,
                "tool": tool,
                "mode": mode,
                "runs": n,
                "success_rate_pct": round((float(row.get("ok_count") or 0) / n) * 100.0, 1),
                "avg_duration_ms": round(float(row.get("sum_duration_ms") or 0) / n, 1),
                "avg_tokens": avg_tok,
                "effective_tokens": avg_tok + penalty_tok,
                "recommended_mode": winner,
            }
        )
    return out


def evaluate_mode_algorithm(
    *,
    tool_count: int,
    classic_schema_tokens: int,
    avg_payload_bytes: float = 0.0,
    mutating_ratio: float = 0.0,
    typed_output_ratio: float = 0.0,
    intent: str | None = None,
    server_count: int = 1,
    model_name: str | None = None,
    model_score_delta: float | None = None,
    model_score_reason: str | None = None,
) -> ModeDecision:
    """Compute a deterministic 0-100 Code Mode suitability score and execution decision."""
    score = 40.0
    factors: list[ModeFactor] = []

    if tool_count > 100 or classic_schema_tokens > 12000:
        score += 35.0
        factors.append(
            ModeFactor(
                name="catalog_bloat",
                delta=35.0,
                detail=f"{tool_count} tools ({classic_schema_tokens} tokens) risks context bloat",
            )
        )
    elif tool_count > 25 or classic_schema_tokens > 3000:
        score += 20.0
        factors.append(
            ModeFactor(
                name="catalog_moderate",
                delta=20.0,
                detail=f"{tool_count} tools benefits from compact Python stub discovery",
            )
        )
    elif tool_count <= 5:
        score -= 15.0
        factors.append(
            ModeFactor(
                name="small_catalog",
                delta=-15.0,
                detail=f"Only {tool_count} tools fits in a single direct prompt",
            )
        )

    if avg_payload_bytes >= 16384:
        score += 30.0
        factors.append(
            ModeFactor(
                name="heavy_payload_risk",
                delta=30.0,
                detail=f"Avg payload {round(avg_payload_bytes)} bytes risks prompt overflow",
            )
        )
    elif avg_payload_bytes >= 4096:
        score += 18.0
        factors.append(
            ModeFactor(
                name="medium_payload_filter",
                delta=18.0,
                detail=f"Avg payload {round(avg_payload_bytes)} bytes rewards sandbox filtering",
            )
        )
    elif 0 < avg_payload_bytes < 512:
        score -= 15.0
        factors.append(
            ModeFactor(
                name="tiny_payload",
                delta=-15.0,
                detail=f"Avg payload {round(avg_payload_bytes)} bytes adds minimal transfer cost",
            )
        )

    intent_lower = (intent or "").lower()
    matched_kw = [kw for kw in _BATCH_INTENT_KEYWORDS if kw in intent_lower]
    if matched_kw or server_count > 1:
        score += 20.0
        detail = (
            f"Intent batch keywords ({', '.join(matched_kw[:3])})"
            if matched_kw
            else f"Multi-upstream scope ({server_count} servers)"
        )
        factors.append(ModeFactor(name="multi_step_batching", delta=20.0, detail=detail))

    if mutating_ratio >= 0.8 and tool_count <= 10:
        score -= 20.0
        factors.append(
            ModeFactor(
                name="single_action_mutating",
                delta=-20.0,
                detail="Write-heavy small surface works best with explicit Classic calls",
            )
        )

    if typed_output_ratio >= 0.5:
        score += 10.0
        factors.append(
            ModeFactor(
                name="typed_output_schemas",
                delta=10.0,
                detail="Typed return schemas enable reliable zero-retry Monty code generation",
            )
        )

    if model_score_delta is not None and abs(model_score_delta) > 0.0:
        score += float(model_score_delta)
        factors.append(
            ModeFactor(
                name="model_profile_score",
                delta=float(model_score_delta),
                detail=model_score_reason
                or f"Model '{model_name or 'configured'}' profile delta ({model_score_delta:+.1f})",
            )
        )

    score = max(0.0, min(100.0, round(score, 1)))
    if score >= 55.0:
        disc: DiscoveryMode = (
            "x_intent"
            if intent_lower
            else ("deferred" if tool_count > 25 or server_count > 2 else "upfront")
        )
        gw_mode: GatewayMode = "codemode_upfront" if disc == "upfront" else "codemode"
        top = sorted(factors, key=lambda f: f.delta, reverse=True)
        reason = top[0].detail if top else "Score favors batched sandbox execution"
        return ModeDecision(
            execution="codemode",
            discovery_mode=disc,
            gateway_mode=gw_mode,
            score=score,
            reason=f"auto->{gw_mode} (score={score}): {reason}",
            factors=factors,
        )

    disc_c: DiscoveryMode = (
        "x_intent" if intent_lower else ("deferred" if tool_count > 30 else "upfront")
    )
    gw_mode_c: GatewayMode = "traditional_discover" if disc_c == "deferred" else "traditional"
    lowest = sorted(factors, key=lambda f: f.delta)
    reason_c = lowest[0].detail if lowest else "Small single-step surface favors Classic MCP"
    return ModeDecision(
        execution="classic",
        discovery_mode=disc_c,
        gateway_mode=gw_mode_c,
        score=score,
        reason=f"auto->{gw_mode_c} (score={score}): {reason_c}",
        factors=factors,
    )


def _run_matches_tool(run: dict[str, Any], target_tool: str) -> bool:
    if run.get("tool_name") == target_tool:
        return True
    for sc in run.get("subcalls") or []:
        if isinstance(sc, dict):
            sc_tool = str(sc.get("tool") or "")
            if sc_tool == target_tool or f"{sc.get('server')}__{sc_tool}" == target_tool:
                return True
    return False


def _evaluate_learned_arms(
    runs: list[dict[str, Any]],
) -> tuple[Literal["codemode", "classic"], float, float] | None:
    arms: dict[str, dict[str, float]] = {}
    for r in runs:
        v = str(r.get("ab_variant") or "")
        if v not in {"codemode", "classic"}:
            m = str(r.get("mode") or "")
            v = "codemode" if (m.startswith("codemode") or m == "hybrid") else "classic"
        b = arms.setdefault(
            v,
            {
                "runs": 0.0,
                "errors": 0.0,
                "tokens_saved": 0.0,
                "tokens_used": 0.0,
                "dur_sum": 0.0,
            },
        )
        b["runs"] += 1.0
        if not r.get("ok"):
            b["errors"] += 1.0
        b["tokens_saved"] += float(r.get("estimated_tokens_saved", 0)) + float(
            r.get("schema_tokens_saved", 0)
        )
        b["tokens_used"] += float(r.get("tokens_used_est", 0))
        b["dur_sum"] += float(r.get("duration_ms", 0.0))
    cm = arms.get("codemode")
    cl = arms.get("classic")
    if not (cm and cl and cm["runs"] >= 1.0 and cl["runs"] >= 1.0):
        return None
    cm_err = (cm["errors"] / cm["runs"]) * 100.0
    cl_err = (cl["errors"] / cl["runs"]) * 100.0
    cm_avg_saved = cm["tokens_saved"] / cm["runs"]
    cl_avg_saved = cl["tokens_saved"] / cl["runs"]
    cm_avg_ms = cm["dur_sum"] / cm["runs"]
    cl_avg_ms = cl["dur_sum"] / cl["runs"]

    if cm_err > cl_err + 5.0:
        return "classic", cl_avg_saved, cm_err
    if cl_err > cm_err + 5.0:
        return "codemode", cm_avg_saved, cm_err
    if cm_avg_saved > cl_avg_saved:
        return "codemode", cm_avg_saved, cm_err
    if cl_avg_saved > cm_avg_saved:
        return "classic", cl_avg_saved, cm_err
    if cm_avg_ms > 0 and cl_avg_ms > 0 and cl_avg_ms < cm_avg_ms:
        return "classic", cl_avg_saved, cm_err
    return "codemode", cm_avg_saved, cm_err


def resolve_ab_experiment_variant(
    upstream: UpstreamConfig,
    identity: CallerIdentity,
    request_id: str,
    *,
    historical_runs: list[dict[str, Any]] | None = None,
    model_name: str | None = None,
    target_tool: str | None = None,
) -> tuple[GatewayMode, str, str, str] | None:
    """Assign a request to the configured Classic vs. Code Mode split."""
    exp = upstream.ab_experiment
    if exp is None or not exp.enabled:
        return None
    exp_id = exp.experiment_id or f"exp_{upstream.name}"

    seed = f"{exp_id}:{identity.tenant_id}:{identity.sub}:{request_id}".encode()
    bucket = int.from_bytes(hashlib.sha256(seed).digest()[:4], "big") % 100
    if bucket < exp.codemode_traffic_pct:
        return (
            "codemode",
            exp_id,
            "codemode",
            f"ab_experiment:{exp_id}:codemode (bucket={bucket}<{exp.codemode_traffic_pct})",
        )
    return (
        "traditional",
        exp_id,
        "classic",
        f"ab_experiment:{exp_id}:classic (bucket={bucket}>={exp.codemode_traffic_pct})",
    )


async def _run_openai_llm_arm(
    *,
    llm: OnboardingLlmConfig,
    ephemeral_runtime: Any,
    identity: CallerIdentity,
    mode: GatewayMode,
    security_settings: GatewaySettings,
) -> dict[str, Any]:
    """Run a bounded OpenAI-compatible tool-calling A/B arm against an ephemeral runtime."""
    started = time.perf_counter()
    tools = ephemeral_runtime.tools_for_mode(identity, mode)
    openai_tools = [
        {
            "type": "function",
            "function": {
                "name": t["name"],
                "description": t.get("description", ""),
                "parameters": t.get("inputSchema") or {"type": "object", "properties": {}},
            },
        }
        for t in tools
    ]
    messages: list[dict[str, Any]] = [
        {
            "role": "system",
            "content": (
                "Answer concisely using the provided MCP tools only. "
                "For execute_code, pass top-level async Python using await."
            ),
        },
        {"role": "user", "content": llm.prompt},
    ]

    headers: dict[str, str] = {"Content-Type": "application/json"}
    if llm.api_key:
        headers["Authorization"] = f"Bearer {llm.api_key}"
    endpoint = f"{llm.base_url.rstrip('/')}/chat/completions"

    prompt_tokens = 0
    completion_tokens = 0
    turns = 0
    grounded = False
    final_answer = ""
    client = ephemeral_runtime.client_manager._get_client()

    for _ in range(llm.max_turns):
        turns += 1
        if security_settings.ssrf_protection_enabled:
            validate_upstream_url(
                llm.base_url,
                app_env=security_settings.app_env,
                block_private_ips_in_prod=security_settings.block_private_ips_in_prod,
                allowed_hosts=security_settings.allowed_upstream_hosts,
            )
        resp = await client.post(
            endpoint,
            headers=headers,
            json={
                "model": llm.model,
                "messages": messages,
                "tools": openai_tools,
                "temperature": 0,
            },
            timeout=30.0,
            follow_redirects=False,
        )
        resp.raise_for_status()
        body = resp.json()
        usage = body.get("usage") or {}
        prompt_tokens += int(usage.get("prompt_tokens", 0))
        completion_tokens += int(usage.get("completion_tokens", 0))

        choice = (body.get("choices") or [{}])[0]
        msg = choice.get("message") or {}
        tool_calls = msg.get("tool_calls") or []
        messages.append(msg)

        if not tool_calls:
            final_answer = str(msg.get("content") or "")
            break

        for tc in tool_calls:
            fn = tc.get("function") or {}
            name = str(fn.get("name") or "")
            raw_args = fn.get("arguments") or "{}"
            args = json.loads(raw_args) if isinstance(raw_args, str) else dict(raw_args)
            if name == "search_tools":
                res = await ephemeral_runtime.catalog.discover_tools_for_caller(
                    identity,
                    ephemeral_runtime.policy,
                    ephemeral_runtime.registry,
                    ephemeral_runtime.discovery_engine,
                    query=str(args.get("query", "")),
                    include_schemas=(mode != "codemode"),
                )
            elif name == "execute_code":
                res = await ephemeral_runtime.execute_code_for_identity(
                    identity,
                    str(args.get("code") or ""),
                    mode=mode,
                )
                prov = res.get("_provenance") or {}
                if prov.get("grounded"):
                    grounded = True
            else:
                res = await ephemeral_runtime.call_direct_tool_for_identity(
                    identity,
                    name,
                    args,
                    mode=mode,
                )
                if res.get("ok"):
                    grounded = True
            messages.append(
                {
                    "role": "tool",
                    "tool_call_id": tc.get("id", name),
                    "content": json.dumps(res, default=str),
                }
            )

    expected = [s for s in llm.expected_substrings if s]
    accuracy = all(sub in final_answer for sub in expected) if expected else bool(final_answer)
    return {
        "mode": mode,
        "turns": turns,
        "prompt_tokens": prompt_tokens,
        "completion_tokens": completion_tokens,
        "total_tokens": prompt_tokens + completion_tokens,
        "duration_ms": round((time.perf_counter() - started) * 1000, 2),
        "grounded": grounded,
        "accuracy_passed": accuracy,
        "answer_preview": final_answer[:300],
    }


def _auto_readonly_replay_plan(
    upstream: UpstreamConfig, tools: list[DiscoveredTool]
) -> tuple[list[dict[str, Any]], str]:
    """Pick safe read-only no-required-arg tools for zero-config onboarding replay."""
    safe_tools = [t for t in tools if not t.mutating and not (t.input_schema or {}).get("required")]
    chosen = safe_tools[:2] if safe_tools else tools[:1]
    classic_calls = [{"tool": t.qualified_name, "arguments": {}} for t in chosen]
    if not chosen:
        return [], "{'empty': True}"
    lines = ["import asyncio"]
    lines.append(
        "res = await asyncio.gather(" + ", ".join(f"{t.qualified_name}()" for t in chosen) + ")"
    )
    lines.append("{'calls': len(res)}")
    return classic_calls, "\n".join(lines)


async def run_onboarding_ab_test(
    runtime: GatewayRuntime,
    identity: CallerIdentity,
    body: OnboardingAbTestRequest,
) -> dict[str, Any]:
    """Run Algorithmic + Replay + optional Live LLM A/B comparison during onboarding."""
    from gateway.server import GatewayRuntime

    candidate = body.upstream
    if candidate is None and body.upstream_name:
        candidate = runtime.registry.get(body.upstream_name)
    if candidate is None:
        raise ValueError("Provide either 'upstream' config or existing 'upstream_name'.")

    if runtime.settings.ssrf_protection_enabled and body.llm_config is not None:
        validate_upstream_url(
            body.llm_config.base_url,
            app_env=runtime.settings.app_env,
            block_private_ips_in_prod=runtime.settings.block_private_ips_in_prod,
            allowed_hosts=runtime.settings.allowed_upstream_hosts,
        )

    headers: dict[str, str] = {}
    if candidate.auth_type != "none":
        try:
            headers, _, _ = await runtime.dispatcher.resolve_upstream_headers(identity, candidate)
        except Exception:
            headers = {}

    if (
        body.dry_run
        and candidate.type in {"mcp_http", "openapi"}
        and candidate.openapi_spec is None
    ):
        tools = runtime.catalog.get_server_tools(candidate.name)
    else:
        inspection = await inspect_upstream(candidate, runtime.client_manager, auth_headers=headers)
        tools = [DiscoveredTool.model_validate(t) for t in inspection["tools"]]

    tool_count = len(tools)
    classic_defs = [to_mcp_tool_definition(t) for t in tools]
    classic_schema_tokens = estimate_tokens(classic_defs) if tools else 0
    mutating_ratio = (sum(1 for t in tools if t.mutating) / tool_count) if tool_count else 0.0
    typed_ratio = (
        sum(1 for t in tools if t.output_schema is not None) / tool_count if tool_count else 0.0
    )

    replay_ab: dict[str, Any] | None = None
    live_llm_ab: dict[str, Any] | None = None

    ephemeral_settings = GatewaySettings(
        app_env="uat",
        auth_mode="dev",
        allowed_tenants=runtime.settings.allowed_tenants,
        sqlite_path=":memory:",
        upstreams_config_path="__ephemeral_missing__.yaml",
    )
    ephemeral_rt = GatewayRuntime(
        settings=ephemeral_settings,
        store=SQLiteAuditStore(":memory:"),
        cache=InMemoryCacheBackend(),
        dev_keys=runtime.dev_keys,
        http_client=runtime.client_manager._http_client,
        preloaded_tools={candidate.name: tools},
    )
    await ephemeral_rt.store.init()
    eval_cfg = candidate.model_copy(update={"enabled": True})
    ephemeral_rt.registry._static[eval_cfg.name] = eval_cfg
    ephemeral_rt.catalog.set_server_tools(eval_cfg, tools)

    avg_bytes = float(body.sample_payload_bytes)
    try:
        classic_calls = body.classic_calls
        codemode_code = body.codemode_code
        if not classic_calls and not codemode_code and tools and not body.dry_run:
            classic_calls, codemode_code = _auto_readonly_replay_plan(eval_cfg, tools)

        if body.dry_run:
            est_payload = max(body.sample_payload_bytes, max(1, tool_count) * 512)
            avg_bytes = float(est_payload)
            cm_stub_tok = estimate_tokens("\n".join(render_python_stub(t) for t in tools[:5]))
            cl_tot = max(classic_schema_tokens * 2 + round(est_payload / 4), 1)
            cm_tot = 360 * 2 + cm_stub_tok + 120
            replay_ab = {
                "mode": "dry_run_estimate",
                "classic": {"total_context_tokens": cl_tot, "payload_bytes": est_payload},
                "code_mode": {"total_context_tokens": cm_tot, "payload_bytes": 480},
                "deltas": {"context_token_savings_pct": round((1 - cm_tot / cl_tot) * 100, 2)},
                "winner": "code_mode" if cm_tot <= cl_tot else "classic_or_tie",
            }
        elif classic_calls and codemode_code:
            replay_ab = await run_side_by_side_comparison(
                ephemeral_rt,
                identity,
                scenario="custom",
                custom_classic_calls=classic_calls,
                custom_code=codemode_code,
            )
            cm_raw = float(replay_ab["code_mode"].get("raw_upstream_bytes") or 0.0)
            avg_bytes = cm_raw / max(len(classic_calls), 1)

        if body.llm_config is not None:
            cm_arm = await _run_openai_llm_arm(
                llm=body.llm_config,
                ephemeral_runtime=ephemeral_rt,
                identity=identity,
                mode="codemode",
                security_settings=runtime.settings,
            )
            cl_arm = await _run_openai_llm_arm(
                llm=body.llm_config,
                ephemeral_runtime=ephemeral_rt,
                identity=identity,
                mode="traditional",
                security_settings=runtime.settings,
            )
            winner = (
                "code_mode"
                if (
                    cm_arm["accuracy_passed"] >= cl_arm["accuracy_passed"]
                    and cm_arm["total_tokens"] <= cl_arm["total_tokens"]
                )
                else "classic"
            )
            live_llm_ab = {
                "model": body.llm_config.model,
                "classic": cl_arm,
                "code_mode": cm_arm,
                "winner": winner,
                "token_savings_pct": (
                    round((1.0 - (cm_arm["total_tokens"] / cl_arm["total_tokens"])) * 100, 2)
                    if cl_arm["total_tokens"] > 0
                    else 0.0
                ),
            }
    finally:
        await ephemeral_rt.shutdown()

    algo = evaluate_mode_algorithm(
        tool_count=tool_count,
        classic_schema_tokens=classic_schema_tokens,
        avg_payload_bytes=avg_bytes,
        mutating_ratio=mutating_ratio,
        typed_output_ratio=typed_ratio,
        intent=body.intent,
        server_count=1,
    )

    reasons: list[str] = [algo.reason]
    rec_mode: GatewayMode = algo.gateway_mode
    confidence = "high" if abs(algo.score - 55.0) >= 15 else "medium"
    if replay_ab:
        reasons.append(
            f"Replay A/B winner={replay_ab['winner']} "
            f"(context savings={replay_ab['deltas']['context_token_savings_pct']}%)"
        )
        if replay_ab["winner"] == "code_mode":
            rec_mode = "codemode_upfront" if tool_count <= 25 else "codemode"
        elif replay_ab["winner"] == "classic":
            rec_mode = "traditional"
    if live_llm_ab:
        reasons.append(
            f"Live LLM ({live_llm_ab['model']}) winner={live_llm_ab['winner']} "
            f"(token savings={live_llm_ab['token_savings_pct']}%)"
        )
        if live_llm_ab["winner"] == "code_mode":
            rec_mode = "codemode_upfront" if tool_count <= 25 else "codemode"
        elif live_llm_ab["winner"] == "classic":
            rec_mode = "traditional"

    return {
        "upstream_name": candidate.name,
        "tool_count": tool_count,
        "classic_schema_tokens": classic_schema_tokens,
        "algorithmic": algo.model_dump(),
        "replay_ab": replay_ab,
        "live_llm_ab": live_llm_ab,
        "recommendation": {
            "recommended_mode": rec_mode,
            "confidence": confidence,
            "reasons": reasons,
            "ready_config_patch": {
                "preferred_mode": rec_mode,
                "ab_experiment": {
                    "enabled": False,
                    "experiment_id": f"ab_{candidate.name}_v1",
                    "codemode_traffic_pct": 50,
                },
            },
        },
    }
