# MCPcodeaway Benchmarks: Classic MCP vs. Code Mode Across 6 Enterprise Workloads

This document reports live benchmarks of **MCPcodeaway** ([`src/gateway/server.py`](src/gateway/server.py)) against fake enterprise MCP and OpenAPI servers ([`examples/mock_backends.py`](examples/mock_backends.py): `snowflake_ledger`, `billing_legacy_eu`, `billing_legacy_us`, `ops_ticketing`).

We benchmarked **two very different model tiers** across **6 distinct MCP workload types** (from 1-call point lookups to 200-row joins, batch financial compute, and 5-step Sentry -> GitHub -> Jira -> Slack incident triage):
1. **Small Local Non-Reasoning Model:** `qwen/qwen3.5-9b` (`reasoning_effort=none`, concurrency 3 in LM Studio) — [benchmarks/live_llm_results.json](benchmarks/live_llm_results.json)
2. **Capable Frontier Coding Agent:** `antigravity_flash_mcp` (Gemini Flash subagent) — [benchmarks/subagent_flash_results.json](benchmarks/subagent_flash_results.json)

---

## 1. What Are the 6 Benchmark Workloads?

To reflect how enterprises actually use MCP servers (**Data Warehouse / SQL**, **REST Billing APIs**, **Financial Compute**, **Sentry**, **GitHub**, **Jira/Ticketing**, and **Slack**), the suite tests 6 concrete workload shapes:

| # | Workload ID | Category | What the Agent Has to Do | Upstream Tool Calls Required |
| :-: | :--- | :--- | :--- | :--- |
| **1** | **`reconcile_bulk_200`**<br>*(Bulk 200-Row Join)* | **High-Volume Data Warehouse Join** | Fetch **200 overdue debt ledger rows** from Snowflake (`dataset="bulk"`), **100 EU invoices**, and **100 US invoices** (`~200 KB` raw JSON total), join them by `customer_id`, and return the **3 customers** whose ledger balance differs from their invoice amount (`CUST-2041: +615`, `CUST-2118: -480`, `CUST-2199: +1250`). | **3 bulk reads** across 3 servers (`400` total records joined) |
| **2** | **`reconcile_overdue`**<br>*(Medium 30-Row Join)* | **Cross-System Table Join** | Same cross-system reconciliation across `snowflake_ledger`, `billing_legacy_eu`, and `billing_legacy_us` on the standard **30-customer dataset** (`10` overdue rows, `~14 KB` raw JSON), returning the **2 discrepancies** (`CUST-1003: +450`, `CUST-1024: -320`). | **3 list reads** across 3 servers |
| **3** | **`compute_batch_penalties`**<br>*(6-Call Batch Compute)* | **Data Fetch + Per-Row Financial Compute** | Query overdue rows from Snowflake (`query_debt_ledger`), filter to the **5 overdue EU customers**, and call `compute_interest_penalty(principal, days_past_due, annual_rate_pct=18.0)` for each of the 5 customers to compute individual late fees and their exact sum (**`399.08 EUR`**). | **1 list read + 5 compute calls** (`6` subcalls total) |
| **4** | **`mutating_ticket_workflow`**<br>*(5-Step Incident Triage Chain)* | **Sequential Multi-Tool Action Chain (Sentry -> GitHub -> Ticketing -> Slack)** | 1) List open payment terminal incidents (`list_terminal_incidents`) and find the `high` severity outage (`TERM-EU-44`, `CUST-1003`).<br>2) Query **Sentry** (`search_sentry_errors`) for that terminal's failing `release_sha` (`sha-9f4c21a`).<br>3) Query **GitHub** (`get_github_release_commit`) for the PR number (`#412`) and author (`alice-sre`).<br>4) Create an escalation case (`create_collection_case` -> `CASE-0001`).<br>5) Post the alert summary to **Slack** (`post_slack_incident_alert` -> `#ops-incidents`). | **3 dependent reads + 2 mutating writes** (`5` sequential steps) |
| **5** | **`trial_balance_audit`**<br>*(4-Call N+1 Fan-Out)* | **1-to-N Record Enrichment** | Fetch trial balance lines with non-zero variance (`get_trial_balance_lines(only_variances=True)` -> 3 rows), then route each customer by account code (`-EU-` vs `-US-`) to `get_eu_customer` or `get_us_customer` to look up their `contact_email`. | **1 list read + 3 point lookups** (`4` subcalls total) |
| **6** | **`single_lookup`**<br>*(1-Call Point Lookup)* | **Simple Point Read** | Fetch a single customer's debt ledger balance (`get_customer_balance(customer_id="CUST-1003")`) and report `1825.0 EUR`. | **1 point lookup** |

---

## 2. Win #1 — SOTA Discovery Saves 71%–99% of Initial Schema Tokens (In BOTH Modes)

Before an agent runs any tool, MCPcodeaway's deferred `BM25` discovery (`list_servers` + `search_tools`—or zero-round-trip `X-Intent` on first connect) avoids loading the entire enterprise tool catalog on every turn:

| Enterprise Catalog Size | Standard Upfront MCP (`tools/list`) | MCPcodeaway Deferred Surface (3 Meta-Tools) | Schema Tokens Saved |
| :--- | :---: | :---: | :---: |
| **16 tools** (4 servers) | ~1,600 tokens | **~463 tokens** | **71.1% saved** |
| **66 tools** (+50 synthetic) | ~5,073 tokens | **~463 tokens** | **90.9% saved** |
| **500 tools** (Enterprise scale) | ~50,726 tokens | **~463 tokens** | **99.1% saved** |

---

## 3. Win #2 — Classic MCP vs. Code Mode (Side-by-Side Across All 6 Workloads)

### A. Capable Coding Model (`flash` Subagent)

| Workload | Classic MCP | Code Mode (`execute_code`) | Winner & Why |
| :--- | :--- | :--- | :--- |
| **1. Bulk 200-Row Join**<br>*(`reconcile_bulk_200`: ~200 KB)* | `49,955 tok` (`199.8 KB`)<br>`23.8 ms` · `4` HTTP trips · ✅ | **`3,149 tok` (`12.6 KB`)**<br>`28.9 ms` · **`3` HTTP trips** · ✅ | **Code Mode Wins Big**<br>**93.7% fewer tokens** (`46,806` tokens saved by filtering 200 rows in Monty) |
| **2. Medium 30-Row Join**<br>*(`reconcile_overdue`: 3 servers)* | `3,550 tok` (`14.2 KB`)<br>`112.8 ms` · `4` HTTP trips · ✅ | **`3,123 tok` (`12.5 KB`)**<br>**`25.7 ms`** · **`3` HTTP trips** · ✅ | **Code Mode Wins**<br>**4.4x faster** (`-12.0%` tokens, parallel `asyncio.gather`) |
| **3. Batch Financial Compute**<br>*(`compute_batch_penalties`: 6 calls)* | `3,216 tok` (`12.9 KB`)<br>`63.5 ms` · `7` HTTP trips · ✅ | **`2,484 tok` (`9.9 KB`)**<br>**`28.9 ms`** · **`3` HTTP trips** · ✅ | **Code Mode Wins**<br>**22.8% fewer tokens**, **2.2x faster**, and **saves 4 HTTP round-trips** |
| **4. 5-Step Incident Triage Chain**<br>*(`mutating_ticket_workflow`: Sentry->GH->Ticket->Slack)* | **`1,756 tok` (`7.0 KB`)**<br>`49.6 ms` · `6` HTTP trips · ✅ | `2,856 tok` (`11.4 KB`)<br>**`17.7 ms`** · **`3` HTTP trips** · ✅ | **Code Mode 2.8x Faster (-3 HTTP trips)**<br>Chains 5 dependent steps in 1 Monty script (Classic uses slightly fewer wire bytes because upfront mode skips `search_tools`) |
| **5. Small 4-Call Fan-Out**<br>*(`trial_balance_audit`: `<9 KB`)* | **`2,235 tok` (`8.9 KB`)**<br>`15.1 ms` · `5` HTTP trips · ✅ | `2,866 tok` (`11.5 KB`)<br>**`13.6 ms`** · **`3` HTTP trips** · ✅ | **Tie**<br>Code Mode saves **2 HTTP trips**; Classic saves `631` stub discovery tokens |
| **6. Single Point Lookup**<br>*(`single_lookup`: 1 contract)* | **`1,619 tok` (`6.5 KB`)**<br>**`4.7 ms`** · **`2` HTTP trips** · ✅ | `2,383 tok` (`9.5 KB`)<br>`8.2 ms` · `3` HTTP trips · ✅ | **Classic Wins**<br>**1.5x fewer tokens** & **1 fewer trip** (no Python script needed for 1 read) |

---

### B. Small Local Non-Reasoning Model (`qwen/qwen3.5-9b`, `reasoning_effort=none`, Concurrency 3)

| Workload | Classic MCP (LLM Tokens / Time / Accuracy) | Code Mode (LLM Tokens / Time / Accuracy) | Winner & Why |
| :--- | :--- | :--- | :--- |
| **1. Bulk 200-Row Join**<br>*(`reconcile_bulk_200`: ~200 KB)* | `39,230 tok`<br>`62.0 s` · `2` turns · **❌ FAILED** *(lost in 200-row JSON dump)* | **`7,336 tok`**<br>**`45.7 s`** · `3` turns · **✅ PASSED** (`0` retries) | **Code Mode Wins Decisively**<br>**81.3% fewer tokens**, **26.3% faster**, and **prevents 9B context collapse** |
| **2. 5-Step Incident Triage Chain**<br>*(`mutating_ticket_workflow`: Sentry->GH->Ticket->Slack)* | `9,268 tok`<br>`25.2 s` · **`6` LLM turns** (`5` sequential tool turns) · ✅ | **`6,907 tok`**<br>**`23.3 s`** · **`3` LLM turns** (`1` Monty script chains all 5 steps) · ✅ | **Code Mode Wins**<br>**25.5% fewer LLM tokens** and **cuts LLM turns in half (`6 -> 3`)** because sequential chains re-send growing chat history on every Classic turn! |
| **3. Batch Financial Compute**<br>*(`compute_batch_penalties`: 6 calls)* | **`5,783 tok`**<br>`30.5 s` · `3` turns (`6` OBO hops) · ✅ | `5,908 tok`<br>**`29.4 s`** · `3` turns (**`1` OBO hop**, `0` retries) · ✅ | **Tie (`~5.8k` tokens, `~29.5s`)**<br>9B writes a clean Monty loop (`0` retries) and cuts downstream OBO token exchanges from `6 -> 1` |
| **4. Medium 30-Row Join**<br>*(`reconcile_overdue`: 3 servers)* | **`10,968 tok`**<br>**`95.6 s`** · `3` turns · ✅ | `47,373 tok`<br>`179.3 s` · `10` turns · ✅ *(4 Python dict-key retries)* | **Classic Wins**<br>**4.3x fewer tokens** (non-reasoning 9B guessed wrong dict keys 4 times before succeeding) |
| **5. Small 4-Call Fan-Out**<br>*(`trial_balance_audit`: `<9 KB`)* | **`4,304 tok`**<br>**`16.2 s`** · `3` turns · ✅ | `10,323 tok`<br>`34.5 s` · `3` turns · ✅ | **Classic Wins**<br>**2.4x fewer tokens** & **2.1x faster** on small fan-outs |
| **6. Single Point Lookup**<br>*(`single_lookup`: 1 contract)* | **`2,065 tok`**<br>**`6.6 s`** · `2` turns · ✅ | `4,866 tok`<br>`10.0 s` · `3` turns · ✅ | **Classic Wins**<br>**2.4x fewer tokens** & **1.5x faster** on 1-call point reads |

---

## 4. What the 6 Workloads Prove (And Why Per-Tool + Per-Model Learning Works)

Look at what the 6 workloads reveal across both models:
1. **Large Data Joins (`reconcile_bulk_200`):** **Code Mode wins on EVERY model** (`81.3%` token savings + fixing accuracy failure on `qwen3.5-9b`; `93.7%` token savings on `flash`).
2. **Multi-Step Sequential Action Chains (`mutating_ticket_workflow`: Sentry -> GitHub -> Jira -> Slack):** **Code Mode wins on BOTH models!** Because step 2 depends on step 1's output, step 3 depends on step 2, etc., Classic MCP forces the LLM to run **6 sequential turns** (re-billing the entire conversation history on every turn = `9,268` tokens on 9B), whereas Code Mode chains `await search_sentry_errors(...) -> await get_github_release_commit(...) -> await create_collection_case(...) -> await post_slack_incident_alert(...)` in **1 single Monty execution** (`6,907` tokens on 9B = **25.5% cheaper**, and **2.8x faster** on `flash`).
3. **Batch Compute Loops (`compute_batch_penalties`):** **Code Mode wins on `flash` (-22.8% tokens, 2.2x faster, -4 HTTP trips)** and **ties on `qwen3.5-9b`** (`29.4 s` vs `30.5 s`) while cutting OBO token exchanges from `6 -> 1`.
4. **Single Point Lookups (`single_lookup`):** **Classic MCP wins on EVERY model** (`1.5x–2.4x` cheaper and faster).

Because MCPcodeaway's programmatic A/B learner ([`src/gateway/ab_testing.py`](src/gateway/ab_testing.py)) tracks **Quality (`ok`)**, **Token Savings**, and **Speed (`duration_ms`)** per **`(model_name, server__tool)`**, `?execution=auto` automatically routes each model and tool to its winning column without any hardcoded model lists.

---

<details>
<summary><strong>Appendix A: Full Raw Telemetry Tables (All 6 Workloads)</strong></summary>

### Raw Table 1 — `qwen/qwen3.5-9b` (`reasoning_effort=none`, Concurrency 3 in LM Studio)

| Workload | Mode | Turns | MCP Calls | Subcalls | OBO Token Hops | Total LLM Tokens | Wall Time | Python Retries | Grounded | Correct |
| :--- | :--- | :---: | :---: | :---: | :---: | ---: | ---: | :---: | :---: | :---: |
| `reconcile_bulk_200` | `traditional` | 2 | 3 | 3 | 3 | 39,230 | 62.03 s | 0 | — | ❌ |
| `reconcile_bulk_200` | `codemode` | **3** | **2** | **3** | 3 | **7,336** | **45.70 s** | **0** | ✅ | ✅ |
| `mutating_ticket_workflow` | `traditional` | 6 | 5 | 5 | 5 | 9,268 | 25.18 s | 0 | — | ✅ |
| `mutating_ticket_workflow` | `codemode` | **3** | **2** | 5 | **1** | **6,907** | **23.26 s** | **0** | ✅ | ✅ |
| `compute_batch_penalties` | `traditional` | 3 | 6 | 6 | 6 | **5,783** | 30.52 s | 0 | — | ✅ |
| `compute_batch_penalties` | `codemode` | 3 | **2** | 6 | **1** | 5,908 | **29.41 s** | **0** | ✅ | ✅ |
| `reconcile_overdue` | `traditional` | **3** | 12 | 12 | 12 | **10,968** | **95.64 s** | **0** | — | ✅ |
| `reconcile_overdue` | `codemode` | 10 | 9 | 7 | 7 | 47,373 | 179.25 s | 4 | ✅ | ✅ |
| `trial_balance_audit` | `traditional` | **3** | 4 | 4 | 4 | **4,304** | **16.21 s** | **0** | — | ✅ |
| `trial_balance_audit` | `codemode` | 3 | 4 | 4 | 3 | 10,323 | 34.52 s | 0 | ✅ | ✅ |
| `single_lookup` | `traditional` | **2** | **1** | 1 | 1 | **2,065** | **6.59 s** | **0** | — | ✅ |
| `single_lookup` | `codemode` | 3 | 2 | 1 | 1 | 4,866 | 10.04 s | 0 | ✅ | ✅ |

### Raw Table 2 — `flash` Subagent (`antigravity_flash_mcp`)

| Workload | Mode | HTTP Trips | MCP Calls | Subcalls | Wire Bytes | Est. Context Tokens | Gateway Latency | Grounded | Correct |
| :--- | :--- | :---: | :---: | :---: | ---: | ---: | ---: | :---: | :---: |
| `reconcile_bulk_200` | `traditional` | 4 | 3 | 3 | 199,817 B | 49,955 | 23.84 ms | — | ✅ |
| `reconcile_bulk_200` | `codemode` | **3** | **2** | 3 | **12,593 B** | **3,149** | **28.88 ms** | ✅ | ✅ |
| `reconcile_overdue` | `traditional` | 4 | 3 | 3 | 14,199 B | 3,550 | 112.81 ms | — | ✅ |
| `reconcile_overdue` | `codemode` | **3** | **2** | 3 | **12,491 B** | **3,123** | **25.73 ms** | ✅ | ✅ |
| `compute_batch_penalties` | `traditional` | 7 | 6 | 6 | 12,863 B | 3,216 | 63.53 ms | — | ✅ |
| `compute_batch_penalties` | `codemode` | **3** | **2** | 6 | **9,934 B** | **2,484** | **28.89 ms** | ✅ | ✅ |
| `mutating_ticket_workflow` | `traditional` | 6 | 5 | 5 | **7,023 B** | **1,756** | 49.58 ms | — | ✅ |
| `mutating_ticket_workflow` | `codemode` | **3** | **2** | 5 | 11,421 B | 2,856 | **17.71 ms** | ✅ | ✅ |
| `trial_balance_audit` | `traditional` | 5 | 4 | 4 | **8,937 B** | **2,235** | 15.08 ms | — | ✅ |
| `trial_balance_audit` | `codemode` | **3** | **2** | 4 | 11,464 B | 2,866 | **13.62 ms** | ✅ | ✅ |
| `single_lookup` | `traditional` | **2** | **1** | 1 | **6,474 B** | **1,619** | **4.68 ms** | — | ✅ |
| `single_lookup` | `codemode` | 3 | 2 | 1 | 9,529 B | 2,383 | 8.15 ms | ✅ | ✅ |

</details>

---

<details>
<summary><strong>Appendix B: Why MCPcodeaway Uses Zero-Dependency BM25 for Discovery (vs. Embeddings & System 1 Sidecars)</strong></summary>

We benchmarked our in-process Okapi **`bm25`** discovery engine against local LM Studio vector **`embedding`** (`text-embedding-qwen3-embedding-0.6b`) and **`system1`** (`laya` `/v1/systemone` single-call `type: "choice"` probability distribution) across 10, 60, and 160 tools:

| Catalog Size | Discovery Backend | Direct Keyword Recall@5 | Paraphrased Semantic Recall@5 | Overall Recall@5 | Mean Latency | External GPU / Sidecar Cost |
| ---: | :--- | ---: | ---: | ---: | ---: | :--- |
| **10 tools** | **`bm25` (Default)** | **100.0%** | 66.7% | 83.3% | **0.09 ms** | **0 (stdlib CPU-only)** |
| **10 tools** | `embedding` (`qwen3-0.6b`) | 100.0% | 91.7% | 95.8% | 63.16 ms | Requires embedding server |
| **10 tools** | `hybrid_system1` (`laya` `choice`) | 100.0% | 80.0% | 90.0% | 71.06 ms | Requires `/v1/systemone` LLM |
| **60 tools** | **`bm25` (Default)** | **100.0%** | 60.0% | 80.0% | **0.50 ms** | **0 (stdlib CPU-only)** |
| **60 tools** | `embedding` (`qwen3-0.6b`) | 100.0% | 85.0% | 92.5% | 95.89 ms | Requires embedding server |
| **60 tools** | `hybrid_system1` (`laya` `choice`) | 100.0% | 68.3% | 84.2% | 83.84 ms | Requires `/v1/systemone` LLM |
| **160 tools** | **`bm25` (Default)** | **100.0%** | 60.0% | 80.0% | **1.40 ms** | **0 (stdlib CPU-only)** |
| **160 tools** | `embedding` (`qwen3-0.6b`) | 100.0% | 85.0% | 92.5% | 162.21 ms | Requires embedding server |
| **160 tools** | `hybrid_system1` (`laya` `choice`) | 100.0% | 65.0% | 82.5% | 86.01 ms | Requires `/v1/systemone` LLM |

### Why `BM25` Is the Right Production Choice (and Why Sidecar Models Are Unnecessary Overhead)

1. **100x Faster (`0.09–1.40 ms` vs. `83–162 ms`):** `bm25` runs in-memory in pure Python stdlib with **zero network hops, zero GPU memory, and zero token cost**.
2. **`100%` Recall on Agent Queries:** When an LLM agent calls `search_tools`, it translates the user's request into domain keywords (e.g. `query="overdue invoices snowflake"` or `"sentry github slack incident"`), where `bm25` achieves **`100.0%` recall**.
3. **Even a Retry Is Faster Than a Sidecar:** In the rare case where an agent's first search is too broad and it issues a second `search_tools` call, two `1 ms` `bm25` calls still take **`< 3 ms` total**—far faster and simpler than operating a dedicated embedding or System 1 sidecar container on every turn.

</details>
