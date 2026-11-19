"""Optional live LLM A/B harness comparing Traditional MCP vs. Code Mode with real models."""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
import tempfile
import time
from pathlib import Path
from typing import Any

import httpx

_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from examples.mock_backends import create_mock_backends_app  # noqa: E402
from gateway.auth import LocalDevKeyManager  # noqa: E402
from gateway.config import GatewaySettings  # noqa: E402
from gateway.server import create_gateway_app  # noqa: E402
from gateway.state import SQLiteAuditStore  # noqa: E402

TASK_PROMPT = (
    "Find all overdue customers whose Snowflake debt ledger balance differs from their "
    "EU/US legacy billing invoice amount, and return each customer_id and delta."
)


def _detect_provider(preferred: str | None = None) -> tuple[str, str] | None:
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


async def _list_mcp_tools(gw: httpx.AsyncClient, gw_token: str, mode: str) -> list[dict[str, Any]]:
    resp = await gw.post(
        f"/mcp?mode={mode}",
        headers={"Authorization": f"Bearer {gw_token}"},
        json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"},
    )
    resp.raise_for_status()
    return resp.json()["result"]["tools"]


async def _call_mcp_tool(
    gw: httpx.AsyncClient, gw_token: str, mode: str, req_id: int, name: str, args: dict[str, Any]
) -> dict[str, Any]:
    resp = await gw.post(
        f"/mcp?mode={mode}",
        headers={"Authorization": f"Bearer {gw_token}"},
        json={
            "jsonrpc": "2.0",
            "id": req_id,
            "method": "tools/call",
            "params": {"name": name, "arguments": args},
        },
    )
    resp.raise_for_status()
    return resp.json().get("result", {})


async def _call_openai_loop(
    gw: httpx.AsyncClient, gw_token: str, api_key: str, mode: str, model: str
) -> dict[str, Any]:
    mcp_tools = await _list_mcp_tools(gw, gw_token, mode)
    oai_tools = [
        {
            "type": "function",
            "function": {
                "name": t["name"],
                "description": t.get("description", ""),
                "parameters": t.get("inputSchema", {"type": "object", "properties": {}}),
            },
        }
        for t in mcp_tools
    ]
    messages: list[dict[str, Any]] = [
        {"role": "system", "content": "Use the provided MCP tools accurately."},
        {"role": "user", "content": TASK_PROMPT},
    ]
    prompt_toks, comp_toks, turns = 0, 0, 0
    started = time.perf_counter()

    async with httpx.AsyncClient(timeout=60.0) as llm:
        for _ in range(8):
            turns += 1
            resp = await llm.post(
                "https://api.openai.com/v1/chat/completions",
                headers={"Authorization": f"Bearer {api_key}"},
                json={"model": model, "messages": messages, "tools": oai_tools},
            )
            resp.raise_for_status()
            data = resp.json()
            usage = data.get("usage") or {}
            prompt_toks += int(usage.get("prompt_tokens", 0))
            comp_toks += int(usage.get("completion_tokens", 0))
            choice = data["choices"][0]["message"]
            messages.append(choice)
            tool_calls = choice.get("tool_calls") or []
            if not tool_calls:
                return {
                    "mode": mode,
                    "turns": turns,
                    "prompt_tokens": prompt_toks,
                    "completion_tokens": comp_toks,
                    "duration_ms": round((time.perf_counter() - started) * 1000, 2),
                    "answer": choice.get("content", ""),
                }
            for tc in tool_calls:
                fn = tc["function"]
                res = await _call_mcp_tool(
                    gw, gw_token, mode, turns, fn["name"], json.loads(fn.get("arguments") or "{}")
                )
                messages.append(
                    {"role": "tool", "tool_call_id": tc["id"], "content": json.dumps(res)}
                )
    return {"mode": mode, "turns": turns, "error": "Max turns reached"}


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

    async with httpx.AsyncClient(timeout=60.0) as llm:
        for _ in range(8):
            turns += 1
            resp = await llm.post(
                "https://api.anthropic.com/v1/messages",
                headers={"x-api-key": api_key, "anthropic-version": "2023-06-01"},
                json={"model": model, "max_tokens": 1024, "messages": messages, "tools": ant_tools},
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
                    "mode": mode,
                    "turns": turns,
                    "prompt_tokens": in_toks,
                    "completion_tokens": out_toks,
                    "duration_ms": round((time.perf_counter() - started) * 1000, 2),
                    "answer": text,
                }
            tool_results = []
            for u in uses:
                res = await _call_mcp_tool(gw, gw_token, mode, turns, u["name"], u.get("input", {}))
                tool_results.append(
                    {"type": "tool_result", "tool_use_id": u["id"], "content": json.dumps(res)}
                )
            messages.append({"role": "user", "content": tool_results})
    return {"mode": mode, "turns": turns, "error": "Max turns reached"}


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

    async with httpx.AsyncClient(timeout=60.0) as llm:
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
                    "mode": mode,
                    "turns": turns,
                    "prompt_tokens": in_toks,
                    "completion_tokens": out_toks,
                    "duration_ms": round((time.perf_counter() - started) * 1000, 2),
                    "answer": text,
                }
            resp_parts = []
            for c in calls:
                res = await _call_mcp_tool(gw, gw_token, mode, turns, c["name"], c.get("args", {}))
                resp_parts.append(
                    {"functionResponse": {"name": c["name"], "response": {"result": res}}}
                )
            contents.append({"role": "user", "parts": resp_parts})
    return {"mode": mode, "turns": turns, "error": "Max turns reached"}


async def run_live_bench(provider: str | None, model: str | None, dry_run: bool) -> dict[str, Any]:
    detected = _detect_provider(provider)
    if dry_run or detected is None:
        return {
            "dry_run": True,
            "provider_detected": detected[0] if detected else None,
            "prompt": TASK_PROMPT,
            "note": (
                "No LLM API key configured (or --dry-run requested). "
                "Set OPENAI_API_KEY, ANTHROPIC_API_KEY, or GEMINI_API_KEY to run live."
            ),
        }

    prov_name, api_key = detected
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


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Optional live LLM A/B benchmark")
    parser.add_argument("--provider", choices=["openai", "anthropic", "gemini"], default=None)
    parser.add_argument("--model", default=None)
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args(argv)
    result = asyncio.run(run_live_bench(args.provider, args.model, args.dry_run))
    print(json.dumps(result, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
