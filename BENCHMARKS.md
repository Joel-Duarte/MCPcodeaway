# MCPcodeaway Benchmarks & Honest Tradeoffs

This document summarizes the measured performance of **MCPcodeaway** comparing **Classic MCP** against **Code Mode (Monty Python sandbox + Deferred Schema Search)** across catalog scaling, enterprise multi-step reconciliation, and a live local 9B model (`qwen/qwen3.5-9b` via LM Studio).

> **Reproducibility:** Raw benchmark output JSON files are gitignored; regression gates remain locked in [benchmarks/thresholds.json](benchmarks/thresholds.json) and verified on every CI run via `uv run python benchmarks/run_benchmark.py --verify-thresholds`.

---

## 1. Live Local 9B LLM A/B Benchmark (`qwen/qwen3.5-9b`)

Executed against the live gateway + mock enterprise backends using `benchmarks/live_llm_bench.py` (`--provider openai_compatible --model qwen/qwen3.5-9b --modes traditional,codemode --reasoning-effort none`):

| Scenario | Extra Synthetic Tools | Mode | Tool Surface (`Init -> Final`) | Turns | Total Tokens | Wall Time (ms) | Retries | Grounded | Correct |
|---|---:|---|---|---:|---:|---:|---:|:---:|:---:|
| `standard` | 0 | `traditional` | `7 -> 7` | 3 | 10,958 | 44,423.52 | 0 | — | ✅ |
| `standard` | 0 | `codemode` | `3 -> 3` | 3 | **6,749 (-38.4%)** | **25,039.62 (-43.6%)** | 0 | ✅ | ✅ |
| `standard` | 50 | `traditional` | `57 -> 57` | 5 | 41,253 | 41,766.86 | 0 | — | ✅ |
| `standard` | 50 | `codemode` | `3 -> 3` | 3 | **9,235 (-77.6%)** | **22,224.31 (-46.8%)** | 0 | ✅ | ✅ |

**Security Boundary Check (Live 9B):**
- `support_agent` querying Snowflake ledger: `finance_subcalls: 0`, `leaked_finance_balance: false`, `blocked_cleanly: true`.

---

## 2. Catalog Scaling: Classic MCP vs. Code Mode (`run_benchmark.py`)

In Classic MCP, every tool's full JSON schema is loaded into the model prompt upfront. In Code Mode with deferred search (`discover_tools` + `execute_code`), the gateway exposes a constant **463-token** 3-tool surface regardless of catalog size:

| Registered Tools | Classic Surface Tokens | Code Mode Surface Tokens | Token Reduction |
|---:|---:|---:|---:|
| 20 | 2,030 | 463 | **77.19%** |
| 50 | 5,073 | 463 | **90.87%** |
| 150 | 15,218 | 463 | **96.96%** |
| 300 | 30,436 | 463 | **98.48%** |
| 500 | 50,726 | 463 | **99.09%** |

---

## 3. Enterprise Workloads: Wins vs. Honest Losses (`run_benchmark.py`)

| Scenario | Classic Tokens | Code Mode Tokens | Token Savings | LLM Round Trips | Winner |
|---|---:|---:|---:|:---:|---|
| `reconcile_overdue` (multi-backend join) | 6,569 | 1,870 | **71.53%** | `4 -> 2` | **Code Mode** |
| `reconcile_bulk_200` (200-row ledger/invoices) | 27,365 | 1,881 | **93.13%** | `4 -> 2` | **Code Mode** |
| `trial_balance_audit` (4-backend audit) | 7,385 | 1,908 | **74.16%** | `5 -> 2` | **Code Mode** |
| `single_lookup` (1 small read call) | 2,915 | 1,878 | 35.57% | `2 -> 2` | **Classic / Tie** |

### Where Code Mode Wins
1. **Large Catalogs (50–500+ tools):** Eliminates prompt schema bloat (`90.87%–99.09%` surface reduction) and prevents context overflow on smaller/faster models like 9B.
2. **Multi-Backend Joins & Bulk Payloads:** Joins, filters, and aggregates happen inside the ephemeral Monty sandbox instead of shipping 100+ raw JSON rows back through the LLM context window (`71.5%–93.1%` token savings, cutting round trips from `4–5` down to `2`).
3. **Verifiable Grounding:** Every response can be checked against HMAC-signed upstream subcall provenance (`require_tool_grounding`), blocking fabricated IDs/numbers before they reach the caller.

### Where Code Mode Loses (Honest Tradeoffs)
1. **Simple Single-Tool Lookups (`single_lookup`):** When an agent only needs 1 small tool call with a tiny response, there are **0 round-trips saved** (`2 -> 2`), and spinning up a Monty sandbox + generating Python code adds unnecessary latency compared to a direct Classic JSON-RPC call.
2. **Very Small Models (<4B):** Smaller models (e.g. `4B` tiers) are less reliable at writing multi-line Python against unfamiliar schemas without retries; **`9B+` models** (`qwen3.5-9b` and above) or frontier models are recommended for Code Mode.
3. **Mutating Writes:** Blindly batching destructive writes inside generated Python is risky—use `auto` (algorithmic routing) or `hybrid` (Code Mode reads + confirmed Classic writes).
