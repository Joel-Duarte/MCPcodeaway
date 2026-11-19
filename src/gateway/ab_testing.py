"""Algorithmic auto-mode selector, live A/B splitter, and Onboarding A/B Test Lab."""

from __future__ import annotations

import hashlib
import json
import time
from typing import TYPE_CHECKING, Any

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


def evaluate_mode_algorithm(
    *,
    tool_count: int,
    classic_schema_tokens: int,
    avg_payload_bytes: float = 0.0,
    mutating_ratio: float = 0.0,
    typed_output_ratio: float = 0.0,
    intent: str | None = None,
    server_count: int = 1,
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


def resolve_ab_experiment_variant(
    upstream: UpstreamConfig,
    identity: CallerIdentity,
    request_id: str,
) -> tuple[GatewayMode, str, str, str] | None:
    """Deterministically assign a caller request into an upstream Classic vs Code Mode split."""
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
    safe_tools = [
        t
        for t in tools
        if not t.mutating and not (t.input_schema or {}).get("required")
    ]
    chosen = safe_tools[:2] if safe_tools else tools[:1]
    classic_calls = [{"tool": t.qualified_name, "arguments": {}} for t in chosen]
    if not chosen:
        return [], "{'empty': True}"
    lines = ["import asyncio"]
    lines.append(
        "res = await asyncio.gather("
        + ", ".join(f"{t.qualified_name}()" for t in chosen)
        + ")"
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

    # Build ephemeral staging runtime isolated from live config/store.
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
    # Allow caller role access in ephemeral evaluation cleanly.
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
            cm_stub_tok = estimate_tokens(
                "\n".join(render_python_stub(t) for t in tools[:5])
            )
            cl_tot = max(classic_schema_tokens * 2 + round(est_payload / 4), 1)
            cm_tot = 360 * 2 + cm_stub_tok + 120
            replay_ab = {
                "mode": "dry_run_estimate",
                "classic": {"total_context_tokens": cl_tot, "payload_bytes": est_payload},
                "code_mode": {"total_context_tokens": cm_tot, "payload_bytes": 480},
                "deltas": {
                    "context_token_savings_pct": round((1 - cm_tot / cl_tot) * 100, 2)
                },
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
            )
            cl_arm = await _run_openai_llm_arm(
                llm=body.llm_config,
                ephemeral_runtime=ephemeral_rt,
                identity=identity,
                mode="traditional",
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
