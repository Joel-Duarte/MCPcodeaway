# MCPcodeaway Benchmarks: Classic MCP vs. Code Mode

This document reports live benchmarks of **MCPcodeaway** ([`src/gateway/server.py`](src/gateway/server.py)) against 4 enterprise MCP and OpenAPI servers ([`examples/mock_backends.py`](examples/mock_backends.py): `snowflake_ledger`, `billing_legacy_eu`, `billing_legacy_us`, `ops_ticketing`).

We tested **two very different model tiers** on the exact same 4 tasks:
1. **Small Local Non-Reasoning Model:** `qwen/qwen3.5-9b` (`reasoning_effort=none`, concurrency 3 in LM Studio) — [benchmarks/live_llm_results.json](benchmarks/live_llm_results.json)
2. **Capable Frontier Coding Agent:** `antigravity_flash_mcp` (Gemini Flash subagent) — [benchmarks/subagent_flash_results.json](benchmarks/subagent_flash_results.json)

---

## 1. Executive Summary: What Wins Where?

### Win #1 — SOTA Discovery Saves 71%–99% of Initial Schema Tokens (In BOTH Modes)

Before an agent runs any tool, MCPcodeaway's deferred discovery (`list_servers` + `search_tools` with `BM25`—or zero-round-trip `X-Intent` on first connect) avoids loading the entire enterprise tool catalog on every turn:

| Enterprise Catalog Size | Standard Upfront MCP (`tools/list`) | MCPcodeaway Deferred Surface (3 Meta-Tools) | Schema Tokens Saved |
| :--- | :---: | :---: | :---: |
| **16 tools** (4 servers) | ~1,600 tokens | **~463 tokens** | **71.1% saved** |
| **66 tools** (+50 synthetic) | ~5,073 tokens | **~463 tokens** | **90.9% saved** |
| **500 tools** (Enterprise scale) | ~50,726 tokens | **~463 tokens** | **99.1% saved** |

---

### Win #2 — Execution Mode Comparison (Classic MCP vs. Code Mode Side-by-Side)

Instead of scrolling through raw columns, here is the direct **Classic MCP vs. Code Mode** comparison for each task:

#### A. Capable Coding Model (`flash` Subagent)

| Task | Classic MCP (Tokens / Time) | Code Mode (Tokens / Time) | Winner & Impact |
| :--- | :--- | :--- | :--- |
| **1. Bulk 200-Row Join**<br>*(`reconcile_bulk_200`: ~200 KB JSON)* | `49,955 tok` (`199.8 KB`)<br>`23.8 ms` · `4` trips · ✅ Pass | **`3,149 tok` (`12.6 KB`)**<br>`28.9 ms` · `3` trips · ✅ Pass | **Code Mode Wins**<br>**93.7% fewer tokens** (`46,806` tokens saved) |
| **2. Medium 30-Row Join**<br>*(`reconcile_overdue`: 3 servers)* | `3,550 tok` (`14.2 KB`)<br>`112.8 ms` · `4` trips · ✅ Pass | **`3,123 tok` (`12.5 KB`)**<br>**`25.7 ms`** · `3` trips · ✅ Pass | **Code Mode Wins**<br>**4.4x faster** (`-12.0%` tokens, parallel `asyncio.gather`) |
| **3. Small 4-Call Fan-Out**<br>*(`trial_balance_audit`: `<9 KB`)* | **`2,235 tok` (`8.9 KB`)**<br>`15.1 ms` · `5` trips · ✅ Pass | `2,866 tok` (`11.5 KB`)<br>**`13.6 ms`** · **`3` trips** · ✅ Pass | **Tie**<br>Code Mode saves **2 HTTP trips**; Classic saves `631` stub tokens |
| **4. Single Point Lookup**<br>*(`single_lookup`: 1 contract)* | **`1,619 tok` (`6.5 KB`)**<br>**`4.7 ms`** · **`2` trips** · ✅ Pass | `2,383 tok` (`9.5 KB`)<br>`8.2 ms` · `3` trips · ✅ Pass | **Classic Wins**<br>**1.5x fewer tokens** & **1 fewer trip** (no script needed) |

#### B. Small Non-Reasoning Model (`qwen/qwen3.5-9b`, `reasoning_effort=none`, Concurrency 3)

| Task | Classic MCP (Tokens / Time / Accuracy) | Code Mode (Tokens / Time / Accuracy) | Winner & Impact |
| :--- | :--- | :--- | :--- |
| **1. Bulk 200-Row Join**<br>*(`reconcile_bulk_200`: ~200 KB JSON)* | `39,230 tok`<br>`62.0 s` · `2` turns · **❌ FAILED** *(lost in 200-row JSON dump)* | **`7,336 tok`**<br>**`45.7 s`** · `3` turns · **✅ PASSED** (`0` retries) | **Code Mode Wins Decisively**<br>**81.3% fewer tokens**, **26.3% faster**, and **prevents accuracy failure** |
| **2. Medium 30-Row Join**<br>*(`reconcile_overdue`: 3 servers)* | **`10,968 tok`**<br>**`95.6 s`** · `3` turns · ✅ Pass | `47,373 tok`<br>`179.3 s` · `10` turns · ✅ Pass *(4 Python dict-key retries)* | **Classic Wins**<br>**4.3x fewer tokens** (small non-reasoning model fumbles Python keys) |
| **3. Small 4-Call Fan-Out**<br>*(`trial_balance_audit`: `<9 KB`)* | **`4,304 tok`**<br>**`16.2 s`** · `3` turns · ✅ Pass | `10,323 tok`<br>`34.5 s` · `3` turns · ✅ Pass | **Classic Wins**<br>**2.4x fewer tokens** & **2.1x faster** |
| **4. Single Point Lookup**<br>*(`single_lookup`: 1 contract)* | **`2,065 tok`**<br>**`6.6 s`** · `2` turns · ✅ Pass | `4,866 tok`<br>`10.0 s` · `3` turns · ✅ Pass | **Classic Wins**<br>**2.4x fewer tokens** & **1.5x faster** |

---

## 2. How Programmatic Model & Per-Tool Learning Uses These Results

We do **not** hardcode model names inside the router. Instead, MCPcodeaway learns programmatically ([`src/gateway/ab_testing.py`](src/gateway/ab_testing.py) & [`src/gateway/server.py`](src/gateway/server.py)):

1. **Agent Self-Identification on First Connect:** The agent sends its model name via `X-Model-Name` (or `?model=...` or MCP `initialize` `params.clientInfo.model`) and optional `X-Intent`.
2. **Per-Tool + Per-Server + Per-Model A/B Exploration:** When `ab_experiment.enabled: true` is active on an upstream, the gateway splits initial traffic between **Classic MCP** and **Code Mode** and records:
   - **Quality:** Success vs. error rate (`ok`)
   - **Efficiency:** Tokens saved (`estimated_tokens_saved + schema_tokens_saved`)
   - **Speed:** End-to-end execution latency (`duration_ms`)
3. **Automatic Promotion (`resolve_ab_experiment_variant` & `compute_empirical_model_delta`):**
   - Once a `(model_name, server__tool)` pair reaches `auto_promote_min_runs`, the gateway automatically switches 100% of traffic for **that model on that specific tool** to the winning mode (prioritizing **Quality / Error Rate first**, **Token Savings second**, and **Speed / Latency third**).
   - Operators can also inspect learned model scores or set manual overrides anytime via `GET /admin/routing/models` and `POST /admin/routing/models`.

---

<details>
<summary><strong>Appendix A: Full Raw Telemetry Tables (Turns, Subcalls, Wire Bytes & Grounding)</strong></summary>

### Raw Table 1 — `qwen/qwen3.5-9b` (`reasoning_effort=none`, Concurrency 3 in LM Studio)

| Task | Mode | Turns | MCP Tool Calls | Upstream Subcalls | Total LLM Tokens | Wall Time | Python Retries | HMAC Grounded | Accuracy |
| :--- | :--- | :---: | :---: | :---: | ---: | ---: | :---: | :---: | :---: |
| `reconcile_bulk_200` | `traditional` | 2 | 3 | 3 | 39,230 | 62.03 s | 0 | — | ❌ |
| `reconcile_bulk_200` | `codemode` | **3** | **2** | **3** | **7,336** | **45.70 s** | **0** | ✅ | ✅ |
| `reconcile_overdue` | `traditional` | **3** | 12 | 12 | **10,968** | **95.64 s** | **0** | — | ✅ |
| `reconcile_overdue` | `codemode` | 10 | 9 | 7 | 47,373 | 179.25 s | 4 | ✅ | ✅ |
| `trial_balance_audit` | `traditional` | **3** | 4 | 4 | **4,304** | **16.21 s** | **0** | — | ✅ |
| `trial_balance_audit` | `codemode` | 3 | 4 | 4 | 10,323 | 34.52 s | 0 | ✅ | ✅ |
| `single_lookup` | `traditional` | **2** | **1** | 1 | **2,065** | **6.59 s** | **0** | — | ✅ |
| `single_lookup` | `codemode` | 3 | 2 | 1 | 4,866 | 10.04 s | 0 | ✅ | ✅ |

### Raw Table 2 — `flash` Subagent (`antigravity_flash_mcp`)

| Task | Mode | HTTP Trips | MCP Tool Calls | Upstream Subcalls | Wire Bytes | Est. Context Tokens | Gateway Latency | HMAC Grounded | Accuracy |
| :--- | :--- | :---: | :---: | :---: | ---: | ---: | ---: | :---: | :---: |
| `reconcile_bulk_200` | `traditional` | 4 | 3 | 3 | 199,817 B | 49,955 | 23.84 ms | — | ✅ |
| `reconcile_bulk_200` | `codemode` | **3** | **2** | 3 | **12,593 B** | **3,149** | **28.88 ms** | ✅ | ✅ |
| `reconcile_overdue` | `traditional` | 4 | 3 | 3 | 14,199 B | 3,550 | 112.81 ms | — | ✅ |
| `reconcile_overdue` | `codemode` | **3** | **2** | 3 | **12,491 B** | **3,123** | **25.73 ms** | ✅ | ✅ |
| `trial_balance_audit` | `traditional` | 5 | 4 | 4 | **8,937 B** | **2,235** | 15.08 ms | — | ✅ |
| `trial_balance_audit` | `codemode` | **3** | **2** | 4 | 11,464 B | 2,866 | **13.62 ms** | ✅ | ✅ |
| `single_lookup` | `traditional` | **2** | **1** | 1 | **6,474 B** | **1,619** | **4.68 ms** | — | ✅ |
| `single_lookup` | `codemode` | 3 | 2 | 1 | 9,529 B | 2,383 | 8.15 ms | ✅ | ✅ |

</details>

---

<details>
<summary><strong>Appendix B: Discovery Strategy Benchmarks (BM25 vs. Embeddings vs. System 1 Choice) & Admin Recommendation</strong></summary>

Measured live against local LM Studio (`http://127.0.0.1:1234/v1`) across 20 queries (10 direct keyword queries + 10 paraphrased semantic queries with zero identifier overlap) at 10, 60, and 160 tools using `benchmarks/discovery_benchmark.py --live-lmstudio`:

| Catalog Size | Discovery Backend | Overall Recall@5 | Direct Keyword Recall@5 | Paraphrased Semantic Recall@5 | Mean Latency | Token / Compute Cost |
| ---: | :--- | ---: | ---: | ---: | ---: | :--- |
| **10 tools** | **`bm25` (Default)** | 83.3% | **100.0%** | 66.7% | **0.09 ms** | **0 (stdlib CPU-only)** |
| **10 tools** | `embedding` (`qwen3-0.6b`) | **95.8%** | **100.0%** | **91.7%** | 63.16 ms | Low (cached vectors) |
| **10 tools** | `hybrid_system1` (`laya` `choice`) | 90.0% | **100.0%** | 80.0% | 71.06 ms | Moderate (LLM sidecar) |
| **60 tools** | **`bm25` (Default)** | 80.0% | **100.0%** | 60.0% | **0.50 ms** | **0 (stdlib CPU-only)** |
| **60 tools** | `embedding` (`qwen3-0.6b`) | **92.5%** | **100.0%** | **85.0%** | 95.89 ms | Low (cached vectors) |
| **60 tools** | `hybrid_system1` (`laya` `choice`) | 84.2% | **100.0%** | 68.3% | 83.84 ms | Moderate (BM25 top-25 + 1 `choice` call) |
| **160 tools** | **`bm25` (Default)** | 80.0% | **100.0%** | 60.0% | **1.40 ms** | **0 (stdlib CPU-only)** |
| **160 tools** | `embedding` (`qwen3-0.6b`) | **92.5%** | **100.0%** | **85.0%** | 162.21 ms | Low (cached vectors) |
| **160 tools** | `hybrid_system1` (`laya` `choice`) | 82.5% | **100.0%** | 65.0% | 86.01 ms | Moderate (BM25 top-25 + 1 `choice` call) |

### Which Discovery Backend Should Admins Choose?

1. **Use `bm25` (Default) for 95% of Deployments:**
   - It runs in **`0.09–1.40 ms`** (100x faster than embeddings or System 1), costs **zero tokens**, has **zero external dependencies**, and hits **`100%` recall** whenever an LLM agent searches with domain keywords (`search_tools(query="overdue invoices snowflake")`). Even if an agent's first query is overly vague, a second `1 ms` `bm25` search is still faster and cheaper than running a sidecar model on every turn.
2. **Add `embedding` Only When Passing Raw Human `X-Intent` Across 100+ Tools:**
   - If your harness forwards the raw end-user chat message in `X-Intent` on Turn 1 (where a human might write *"Why can't shoppers tap their cards?"* instead of `"terminal incidents"`), `embedding` lifts semantic recall from `60% -> 85%` (`92.5%` overall) with cached tool vectors.
3. **When Does `hybrid_system1` Make Sense?**
   - Only as an optional experiment when you already host a local `/v1/systemone` model and cannot host an embedding model, or want calibrated server-level routing confidence (`stage1_servers`). Otherwise, **`bm25` (default) + optional `embedding`** is faster, simpler, and more accurate.

</details>
