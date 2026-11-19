# MCPcodeaway Benchmarks: SOTA Discovery & Execution Quadrant (`qwen/qwen3.5-9b` vs. `flash` Agent)

This document reports live benchmarks of **MCPcodeaway** ([`src/gateway/server.py`](src/gateway/server.py)) against the fake enterprise MCP and OpenAPI servers ([`examples/mock_backends.py`](examples/mock_backends.py): `snowflake_ledger`, `billing_legacy_eu`, `billing_legacy_us`, `ops_ticketing`).

Because MCPcodeaway separates **Discovery** (`upfront`, `deferred`, or first-connect `x_intent`) from **Execution** (`classic` vs. `codemode`), the wins break down cleanly into **two orthogonal stages**:
1. **Stage 1 — SOTA Discovery Compression (Applies to BOTH Classic MCP & Code Mode):** Keeping initial schema tokens flat (`~463` tokens) even when your enterprise catalog grows from 16 to 500+ tools.
2. **Stage 2 — Execution & Payload Win Quadrant (Classic MCP vs. Code Mode, by Model Tier & Data Size):** Comparing a local non-reasoning 9B model (`qwen/qwen3.5-9b`, `reasoning_effort=none`, concurrency 3 in LM Studio) against a capable coding model (`antigravity_flash_mcp` / Gemini Flash subagent).

---

## Stage 1: The Discovery Win (Shared by Both Execution Modes)

Before an agent executes any tool, MCPcodeaway’s deferred discovery (`list_servers` + `search_tools` backed by `BM25`, `Embedding`, or `System 1` sidecar—or zero-round-trip `X-Intent` on first connect) avoids dumping the full enterprise catalog into `tools/list` on every turn:

| Enterprise Catalog Size | Standard Upfront MCP (`tools/list` on every turn) | MCPcodeaway Deferred Discovery (`list_servers` + `search_tools` + `execute_code`) | Upfront Schema Savings |
| :--- | :---: | :---: | :---: |
| **16 tools** (Base 4 servers) | ~1,600 tokens | **~463 tokens** (3 meta-tools) | **71.1% fewer tokens** |
| **66 tools** (+50 synthetic tools) | ~5,073 tokens | **~463 tokens** (3 meta-tools) | **90.9% fewer tokens** |
| **500 tools** (Enterprise scale) | ~50,726 tokens | **~463 tokens** (3 meta-tools) | **99.1% fewer tokens** |

> **Key Takeaway:** Even when a task routes to **Classic MCP execution** (`traditional_discover`), MCPcodeaway’s SOTA discovery cuts initial schema bloat by **71%–99%**. Furthermore, when the agent harness sends `X-Intent` (or `tools/list` `params.query`) on the initial connection, discovery happens at **zero extra round-trips** (Turn 1).

---

## Stage 2: The Execution Win Quadrant (Classic MCP vs. Code Mode)

Once the right tools are discovered, the winner between **Classic MCP** (`traditional` / `traditional_discover`) and **Code Mode** (`codemode` / Monty Python sandbox) depends on **(A) task shape / payload size** and **(B) model coding capability**:

| Task Shape | Small Non-Reasoning Model (`qwen/qwen3.5-9b`, `reasoning_effort=none`, Concurrency 3) | Capable Coding Model (`flash` Subagent) | Why? |
| :--- | :--- | :--- | :--- |
| **1-Call Point Lookup**<br>*(`single_lookup`: fetch 1 contract)* | **Classic Wins**<br>`2,065` vs `4,866` tok (**2.4x cheaper**, `2` vs `3` turns) | **Classic Wins**<br>`1,619` vs `2,383` tok (**1.5x cheaper**, `4.68 ms` vs `8.15 ms`) | No loop or filtering is needed; a direct JSON-RPC tool call has lower overhead than generating a Python script. |
| **Small Fan-Out**<br>*(`trial_balance_audit`: 4 small reads)* | **Classic Wins**<br>`4,304` vs `10,323` tok (`16.2 s` vs `34.5 s`) | **Tie / Slight Classic Token Edge**<br>`2,235` vs `2,866` tok, but Code Mode saves **2 HTTP trips** (`13.62 ms` vs `15.08 ms`) | Payloads are tiny (`<9 KB`), so stub discovery overhead slightly exceeds raw response size unless network RTT is high. |
| **Medium Multi-Step Join**<br>*(`reconcile_overdue`: 30 rows + cross-system join)* | **Classic Wins**<br>`10,968` vs `47,373` tok *(non-reasoning 9B hit 4 Python dict-key retries in Monty)* | **Code Mode Wins**<br>**`3,123` vs `3,550` tok (-12.0%)** and **4.4x faster** (`25.73 ms` vs `112.81 ms`) | Small non-reasoning models fumble nested dict keys in Python without reasoning tokens; capable coding models write 1 clean `asyncio.gather` script with `0` retries. |
| **Large Bulk Join**<br>*(`reconcile_bulk_200`: 200 rows, ~200 KB raw JSON)* | **Code Mode Wins Decisively**<br>**`7,336` tok (`✅ Pass`)** vs `39,230` tok (`❌ Fail`) (**81.3% fewer tokens**, **26.3% faster**) | **Code Mode Wins Decisively**<br>**`3,149` tok (`✅ Pass`)** vs `49,955` tok (`✅ Pass`) (**93.7% fewer tokens**, `12.6 KB` vs `199.8 KB`) | Raw 200-row JSON dumps flood the LLM context in Classic MCP (causing 9B to lose track of arithmetic and fail), whereas Monty filters 200 KB in-memory and returns 120 bytes. |

---

## Detailed Run Data

### 1. Local `qwen/qwen3.5-9b` (`reasoning_effort=none`, Concurrency 3 in LM Studio)

Executed against LM Studio (`http://127.0.0.1:1234/v1`) with 3 concurrent slots:
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

### 2. Frontier Coding Agent (`flash` Subagent) on the Same Fake MCP Servers

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

## How Auto-Routing (`?execution=auto`) & Live A/B Testing Use This Quadrant

MCPcodeaway turns these benchmark findings into automated runtime routing ([`src/gateway/ab_testing.py`](src/gateway/ab_testing.py) and [`src/gateway/server.py`](src/gateway/server.py)):
1. **Zero-Round-Trip Intent Discovery (`X-Intent` / `discovery=x_intent`):** When the agent harness passes `X-Intent` on connect (or `tools/list` `params.query`), the gateway pre-hydrates only the matching Python stubs or Classic tool schemas on Turn 1—eliminating the extra `search_tools` round-trip.
2. **Algorithmic Auto-Router (`?execution=auto`):** Scores each request (`0–100`) using accessible tool count, historical payload size (`raw_upstream_bytes`), mutating tool ratio, and intent keywords (`reconcile`, `join`, `bulk`, `compare`)—routing 1-call point lookups to **Classic MCP** and multi-tool/large-payload joins to **Code Mode**.
3. **Per-Upstream A/B Traffic Splitting & Model Analytics:** Operators can enable `ab_experiment` (`codemode_traffic_pct: 50`) on any upstream and pass `X-Model-Name` (or `client_id`) to compare error rates, latency, and token savings per model/group at `/admin/analytics/usage` (`ab_experiments[exp_id].recommended_winner`).

---

<details>
<summary><strong>Appendix: SOTA Discovery Backend Benchmarks (BM25 vs. Embeddings vs. Local System 1 Sidecar) & Pros/Cons</strong></summary>

Measured live against local LM Studio (`http://127.0.0.1:1234/v1`) across 20 queries (10 direct keyword queries + 10 paraphrased semantic queries with zero identifier overlap) at 10, 60, and 160 tools using `benchmarks/discovery_benchmark.py --live-lmstudio`:
- **Embedding model:** `text-embedding-qwen3-embedding-0.6b` (cached per `catalog_version`)
- **System 1 sidecar model:** `laya` (`POST /v1/systemone` using Stage-1 server `noul` routing + Stage-2 single-call `type: "choice"` probability distribution across candidate tools instead of 40+ parallel `noul` questions)

| Catalog Size | Discovery Backend | Overall Recall@5 | Direct Recall@5 | Semantic Recall@5 | MRR | Server Routing Acc | Mean Latency (ms) | Extra Token / GPU Cost |
| ---: | :--- | ---: | ---: | ---: | ---: | ---: | ---: | :--- |
| **10 tools** | `bm25` (Default) | 0.833 | **1.000** | 0.667 | 0.777 | 0.800 | **0.09 ms** | **0 (CPU-only, stdlib)** |
| **10 tools** | `embedding` | **0.958** | **1.000** | **0.917** | **0.975** | 0.900 | 63.16 ms | Low (cached vectors) |
| **10 tools** | `hybrid_system1` (`choice`) | 0.900 | **1.000** | 0.800 | 0.756 | **0.950** | 71.06 ms | Moderate (Stage-1 `noul` + 1 Stage-2 `choice` distribution) |
| **60 tools** | `bm25` (Default) | 0.800 | **1.000** | 0.600 | 0.742 | 0.800 | **0.50 ms** | **0 (CPU-only, stdlib)** |
| **60 tools** | `embedding` | **0.925** | **1.000** | **0.850** | **0.925** | **0.900** | 95.89 ms | Low (1 query embedding after warm cache) |
| **60 tools** | `hybrid_system1` (`choice`) | 0.842 | **1.000** | 0.683 | 0.768 | 0.800 | **83.84 ms** | Moderate (BM25 top-25 + 1 `choice` probability distribution) |
| **60 tools** | `system1` (raw `choice`) | 0.625 | 0.900 | 0.350 | 0.525 | 0.650 | 128.74 ms | Moderate (2.3x faster than 40 `noul` prompts, but 40-choice softmax dilutes probability) |
| **160 tools** | `bm25` (Default) | 0.800 | **1.000** | 0.600 | 0.742 | 0.800 | **1.40 ms** | **0 (CPU-only, stdlib)** |
| **160 tools** | `embedding` | **0.925** | **1.000** | **0.850** | **0.925** | **0.900** | 162.21 ms | Low (1 query embedding after warm cache) |
| **160 tools** | `hybrid_system1` (`choice`) | 0.825 | **1.000** | 0.650 | 0.758 | 0.800 | **86.01 ms** | Moderate (BM25 top-25 + 1 `choice` probability distribution) |
| **160 tools** | `system1` (raw `choice`) | 0.625 | 0.900 | 0.350 | 0.525 | 0.650 | 130.82 ms | Moderate (2.4x faster than 40 `noul` prompts, but truncates past 40 tools) |

### Pros & Cons of Each Discovery Strategy

1. **`bm25` (Default Production Choice — `GATEWAY_DISCOVERY_BACKEND=bm25`)**
   - **Pros:** Sub-millisecond (`0.09–1.40 ms` even at 160 tools), **zero token cost**, zero external GPU/API dependency, `100%` recall when the agent uses domain keywords (which agents almost always do when calling `search_tools(query="overdue invoices snowflake")`).
   - **Cons:** Lower recall (`60%`) on purely conversational user queries that share zero words with the tool names (e.g., *"Why cannot shoppers tap their credit cards?"* -> `list_terminal_incidents`).
2. **`embedding` (Recommended SOTA Upgrade for Raw Natural-Language `X-Intent` — `GATEWAY_DISCOVERY_BACKEND=embedding`)**
   - **Pros:** Highest accuracy across all catalog sizes (**`92.5%–95.8%` Recall@5, `0.925–0.975` MRR**), caches tool vectors in-memory per `catalog_version` so runtime searches only embed the short query string, and automatically falls back to `bm25` if the embedding server is unreachable.
   - **Cons:** Requires an OpenAI-compatible `/v1/embeddings` endpoint (`text-embedding-qwen3-embedding-0.6b`, `nomic-embed-text`, etc.) and adds `~60–160 ms` per uncached query.
3. **`system1` / `hybrid_system1` (Single-Call `type: "choice"` Confidence Distribution — Optional)**
   - **Pros:** Switching Stage 2 from 40+ parallel `noul` yes/no prompts to a **single `type: "choice"` probability distribution** cuts latency by **more than half** (`311 ms -> 130 ms` raw, `140 ms -> 83.8 ms` hybrid) and achieves `95%` Stage-1 server routing accuracy at 10 tools and `84.2%` Recall@5 in `hybrid_system1` at 60 tools (faster than embeddings at 60–160 tools: `83–86 ms` vs `95–162 ms`).
   - **Cons:** Softmax probability over large criteria lists (`>25` tools) dilutes multi-tool recall compared to independent cosine-similarity embeddings (`84.2%` vs `92.5%` Recall@5), and still requires an active `/v1/systemone` LLM sidecar. Keep **`bm25` + `embedding`** as the primary defaults, with `hybrid_system1` (`choice`) available when a local System 1 sidecar is deployed.

</details>

