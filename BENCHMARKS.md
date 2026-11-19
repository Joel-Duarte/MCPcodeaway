# MCPcodeaway Benchmarks: Local 9B (`qwen/qwen3.5-9b`, `reasoning_effort=none`, Concurrency 3) vs. Frontier `flash` Agent

This document reports live benchmarks of **MCPcodeaway** (`gateway.server`) against the fake enterprise MCP and OpenAPI servers (`examples.mock_backends`: `snowflake_ledger`, `billing_legacy_eu`, `billing_legacy_us`, `ops_ticketing`) comparing:

1. **Local 9B Non-Reasoning Model (`qwen/qwen3.5-9b`, `reasoning_effort=none`)** running in LM Studio (`http://127.0.0.1:1234/v1`) with **3 concurrent slots (`--concurrency 3`)** ([benchmarks/live_llm_results.json](benchmarks/live_llm_results.json)).
2. **Frontier Coding Agent (`antigravity_flash_mcp` / Gemini Flash subagent)** driving the exact same 4 scenarios over live HTTP JSON-RPC (`POST /mcp`) ([benchmarks/subagent_flash_results.json](benchmarks/subagent_flash_results.json)).

---

## 1. Local `qwen/qwen3.5-9b` (`reasoning_effort=none`, Concurrency 3)

Executed via:
```bash
uv run python benchmarks/live_llm_bench.py \
  --provider openai_compatible \
  --base-url http://127.0.0.1:1234/v1 \
  --model qwen/qwen3.5-9b \
  --modes traditional,codemode \
  --tasks reconcile_overdue,reconcile_bulk_200,trial_balance_audit,single_lookup \
  --synthetic-tools 0 \
  --reasoning-effort none \
  --concurrency 3
```

| Workload | Mode | Tool Surface (`Init -> Final`) | Turns | MCP Tool Calls | Subcalls | Total Tokens | Wall Time (ms) | Self-Corrections | Grounded | Correct |
| :--- | :--- | :---: | ---: | ---: | ---: | ---: | ---: | ---: | :---: | :---: |
| `reconcile_overdue` (30-row join) | `traditional` (Classic) | `7 -> 7` | **3** | 12 | 12 | **10,968** | **95,636.15** | **0** | — | ✅ |
| `reconcile_overdue` (30-row join) | `codemode` | `3 -> 3` | 10 | 9 | 7 | 47,373 | 179,253.67 | 4 | ✅ | ✅ |
| `reconcile_bulk_200` (200-row join) | `traditional` (Classic) | `7 -> 7` | 2 | 3 | 3 | 39,230 | 62,034.48 | 0 | — | ❌ (lost in 200-row dump) |
| `reconcile_bulk_200` (200-row join) | `codemode` | `3 -> 3` | **3** | **2** | **3** | **7,336 (-81.3%)** | **45,700.87 (-26.3%)** | **0** | ✅ | ✅ |
| `trial_balance_audit` (variance + lookup) | `traditional` (Classic) | `7 -> 7` | **3** | 4 | 4 | **4,304** | **16,213.88** | **0** | — | ✅ |
| `trial_balance_audit` (variance + lookup) | `codemode` | `3 -> 3` | 3 | 4 | 4 | 10,323 | 34,515.09 | 0 | ✅ | ✅ |
| `single_lookup` (1 point read) | `traditional` (Classic) | `7 -> 7` | **2** | **1** | 1 | **2,065** | **6,590.28** | **0** | — | ✅ |
| `single_lookup` (1 point read) | `codemode` | `3 -> 3` | 3 | 2 | 1 | 4,866 | 10,039.20 | 0 | ✅ | ✅ |

**Security & RBAC Preflight (`qwen/qwen3.5-9b`):**
- OAuth 2.1 Discovery (`/.well-known/oauth-protected-resource`, `/.well-known/oauth-authorization-server`), `401` unauthenticated challenge, and `403` untrusted tenant checks: **100% passed**.
- Live `support_agent` RBAC boundary probe (`snowflake_ledger` query): `finance_subcalls: 0`, `leaked_finance_balance: false`, `blocked_cleanly: true`.

---

## 2. Frontier Coding Agent (`flash` Subagent) on the Same Fake MCP Servers

Executed over live ASGI HTTP (`POST /mcp?mode=traditional` vs. `POST /mcp?mode=codemode&require_grounding=true`):

| Workload | Mode | HTTP Trips | MCP Tool Calls | Subcalls | Wire Response Bytes | Est. Context Tokens | Latency (ms) | Grounded | Correct | Code Mode Delta (Tokens / Bytes / Trips) |
| :--- | :--- | ---: | ---: | ---: | ---: | ---: | ---: | :---: | :---: | :--- |
| `reconcile_overdue` (30-row join) | `traditional` (Classic) | 4 | 3 | 3 | 14,199 B | 3,550 | 112.81 | — | ✅ | — |
| `reconcile_overdue` (30-row join) | `codemode` | **3** | **2** | 3 | **12,491 B** | **3,123** | **25.73** | ✅ | ✅ | **-12.03% tokens / -12.03% bytes / -1 trip (4.4x faster)** |
| `reconcile_bulk_200` (200-row join) | `traditional` (Classic) | 4 | 3 | 3 | 199,817 B | 49,955 | 23.84 | — | ✅ | — |
| `reconcile_bulk_200` (200-row join) | `codemode` | **3** | **2** | 3 | **12,593 B** | **3,149** | **28.88** | ✅ | ✅ | **-93.70% tokens / -93.70% bytes / -1 trip** |
| `trial_balance_audit` (4-call fanout) | `traditional` (Classic) | 5 | 4 | 4 | **8,937 B** | **2,235** | 15.08 | — | ✅ | — |
| `trial_balance_audit` (4-call fanout) | `codemode` | **3** | **2** | 4 | 11,464 B | 2,866 | **13.62** | ✅ | ✅ | **+28.23% tokens (stub discovery overhead), but -2 HTTP trips** |
| `single_lookup` (1 point read) | `traditional` (Classic) | **2** | **1** | 1 | **6,474 B** | **1,619** | **4.68** | — | ✅ | — |
| `single_lookup` (1 point read) | `codemode` | 3 | 2 | 1 | 9,529 B | 2,383 | 8.15 | ✅ | ✅ | **+47.19% tokens / +1 trip (`traditional` wins on 1-call lookup)** |

---

## 3. Side-by-Side Comparison: Local Non-Reasoning 9B vs. Frontier `flash` Agent

| Dimension | Local `qwen/qwen3.5-9b` (`reasoning_effort=none`, Concurrency 3) | Frontier `flash` Agent (`antigravity_flash_mcp`) | Key Takeaway |
| :--- | :--- | :--- | :--- |
| **Small / Medium Payloads (`single_lookup`, `trial_balance_audit`, `reconcile_overdue` with 7 tools)** | **Classic MCP wins on tokens & latency:** With only 7 tools in the catalog, `qwen3.5-9b` (`reasoning_effort=none`) executes direct tool calls in `2–3` turns with `0` retries (`2,065–10,968` tokens), whereas in `codemode` it took `4` self-correction retries on `reconcile_overdue` (`47,373` tokens) and paid an extra `search_tools` turn on small lookups. | **Zero-retry Python generation:** `flash` wrote working `asyncio.gather` Monty Python on the first attempt (`0` retries across all 4 workloads), cutting `reconcile_overdue` latency by **4.4x** (`112.8 ms -> 25.7 ms`) and saving **12.03%** wire tokens. Even on `flash`, `single_lookup` (`1,619` vs `2,383` tokens) favors **Classic MCP** because deferred `search_tools` adds 1 extra turn when only 1 small tool call is needed. | **Model capability & workload size both matter:** Non-reasoning 9B models handle flat JSON tool calls more easily than multi-step Python synthesis on small datasets, while stronger coding models (`flash`+) write zero-retry Monty code immediately. |
| **Bulk Multi-System Joins (`reconcile_bulk_200`, 200 rows / ~200 KB raw JSON)** | **Code Mode is essential (`100%` accuracy vs. Classic failure):** In `traditional` mode, dumping 200 Snowflake + EU/US invoice rows (`39,230` prompt tokens) into `qwen3.5-9b` (`reasoning_effort=none`) overwhelmed the model's in-context arithmetic and **failed accuracy (`correct=False`)**. In `codemode`, `qwen3.5-9b` wrote a Python join in Monty (`0` retries), used **81.3% fewer tokens (`7,336` vs `39,230`)**, finished **26.3% faster**, and returned the exact 3 discrepancies (`correct=True`, `grounded=True`). | **Code Mode saves 93.7% of context tokens:** `flash` solved `reconcile_bulk_200` in both modes, but Classic MCP shipped **199,817 bytes (`49,955` est. tokens)** across the wire vs. **12,593 bytes (`3,149` est. tokens)** in Code Mode (**93.70% reduction**) with HMAC-verified provenance (`grounded=True`). | Once payloads exceed a few dozen rows (or catalogs exceed dozens of tools), **Code Mode wins decisively on every model tier**—preventing reasoning/context collapse on 9B models and cutting token transfer by **81%–94%**. |

---

## 4. Practical Routing Guidance for Operators

1. **Use `traditional` (Classic MCP) or `?execution=auto` when:**
   - The caller is a smaller or non-reasoning local model (`<=9B` with `reasoning_effort=none`), the tool catalog is small (`<15` tools), and responses are small point lookups (`single_lookup`, `trial_balance_audit`).
   - The workload is a single-tool lookup where deferred `search_tools` + `execute_code` would turn 2 HTTP round-trips into 3.
2. **Use `codemode` (or `codemode_upfront`) when:**
   - The caller is a capable coding/agent model (`flash` / frontier models) orchestrating multi-backend joins (`asyncio.gather`).
   - Downstream tools return large tables/lists (`reconcile_bulk_200`), where filtering inside the Monty sandbox cuts context tokens by **81%–94%** and prevents smaller models from losing track of rows in raw JSON dumps.
   - High-assurance provenance grounding (`require_tool_grounding=true`) is required so results are cryptographically tied to upstream tool outputs.
