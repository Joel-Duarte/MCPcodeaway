# MCPcodeaway Benchmarks

## External benchmarks

Cloudflare published two comparisons of upfront MCP tool-definition tokens with server-side Code Mode.

- **MCP Server Portal.** The definitions of 52 tools across four internal MCP servers took about 9,400 tokens. The portal exposed two tools in Code Mode, which took about 600 tokens. [Cloudflare MCP Server Portals](https://blog.cloudflare.com/enterprise-mcp/#code-mode-with-mcp-server-portals-reduces-costs)
- **Cloudflare API MCP server.** The two Code Mode tool definitions took about 1,000 tokens. Cloudflare estimated 1.17 million tokens to expose the full API as individual tools. [Cloudflare API MCP server](https://blog.cloudflare.com/code-mode-mcp/)

### What this suggests for the gateway and MCP servers

- **Gateway.** Benchmark current Auto and adaptive Auto on held-out workloads against forced Classic and Code Mode. Keep the model, discovery settings, and accessible tools fixed. Include short lookups, parallel reads with large results, and dependent calls. Track upfront tool-definition tokens separately from end-to-end usage so changes in discovery and execution are visible. Compare verified completion rate, cost per verified completion, p95 latency, and upstream calls.
- **Upstream MCP servers.** Start with frequently used tools that lack an `outputSchema`. Return structured results that match it, with stable field names and compact defaults. Add filters, field selection, or summary operations where clients regularly fetch records just to discard most of them. The gateway already uses output schemas to show return shapes in Code Mode stubs. [MCP Python SDK structured output](https://py.sdk.modelcontextprotocol.io/v2/servers/structured-output/)

## Paired pilot results

The paired pilot compared Classic MCP and Code Mode with upfront discovery on deterministic mock Snowflake and EU/US billing services. A verified completion passed the expected-answer and grounding checks.

### GPT-6 Luna at low reasoning

We ran one pair per workload in fresh Codex CLI sessions. Both arms used the same mock data and a read-only stdio adapter to the gateway. Codex reported input and output tokens for the full turn, including session overhead. Cached input is included in the total and shown separately. Wall time covers the CLI invocation.

| Workload | Classic tokens (cached input) | Code Mode tokens (cached input) | Wall time, Classic / Code | Gateway calls, Classic / Code | Upstream calls, Classic / Code |
| --- | ---: | ---: | ---: | ---: | ---: |
| Single lookup | 46,673 (43,776) | 46,802 (29,952) | 7.5 s / 7.6 s | 1 / 1 | 1 / 1 |
| Standard reconciliation | 49,374 (30,336) | 64,937 (38,784) | 17.3 s / 11.1 s | 3 / 1 | 3 / 3 |
| Bulk reconciliation | 88,179 (58,496) | 47,272 (30,208) | 17.3 s / 12.7 s | 6 / 1 | 6 / 3 |

All six runs verified. The single lookup had nearly equal token totals. Code Mode bundled standard reconciliation into one gateway call and finished faster while using 31.5% more tokens. In the bulk pair, Code Mode called the three upstream tools concurrently with `asyncio.gather`, joined the data in the sandbox, and returned the discrepancies. It used 46.4% fewer reported tokens in that pair.

### Local Qwen models

We ran 30 workflow executions in LM Studio. The table shows selected results using input and output tokens reported by the local model endpoint. The 9B lookup averages five pairs, and the 1.5B row totals three pairs. Each remaining row shows one pair. The 4B rows use the rerun with MTP enabled, a 32,768-token context, and one request at a time. The local runner and Codex have different session overhead, so compare token totals within each setup.

| Model and workload | Classic MCP | Code Mode |
| --- | --- | --- |
| Qwen2.5 Coder 1.5B, three workloads | 0/3 verified, 2,861 tokens spent | 0/3 verified, 4,475 tokens spent |
| Qwen3.5 4B MTP, single lookup | 2,307 tokens, verified | 5,298 tokens, failed grounding |
| Qwen3.5 4B MTP, standard reconciliation | 13,200 tokens, failed grounding, 6 upstream calls | 4,636 tokens, verified, 31 upstream calls |
| Qwen3.5 9B, single lookup | 2,287 tokens per verified completion, 5/5 verified | 3,160 tokens per verified completion, 5/5 verified |
| Qwen3.5 9B, standard reconciliation | 39,332 tokens, incorrect answer | 4,194 tokens, verified |
| Qwen3.5 9B, bulk reconciliation | 39,615 tokens, verified | 62,297 tokens, incorrect answer |

Classic used 27.6% fewer tokens per verified 9B lookup. The same model produced the only verified output in Code Mode on standard reconciliation and in Classic on bulk reconciliation. The 1.5B made no tool calls in these attempts. The 4B standard run shows why we track upstream calls alongside model tokens.

## Why bundle tools in Code Mode?

A single `execute_code` call can invoke several distinct tools on one MCP server or span multiple servers. For example, a script could fetch user details and accounts concurrently, then pass both results to a calculation tool. The completed reconciliation runs exercised the multi-server case.

In Classic MCP, the client invokes each tool separately and can submit independent calls in parallel. Code Mode can also handle dependent steps inside the script, reducing model round trips and keeping intermediate results in the sandbox. The benefit depends on payload size, script generation, retries, and upstream calls. The Luna standard and bulk pairs show different tradeoffs.

## Why we moved from tool scores to workload scores

The previous learner scored each model and tool together. In a three-tool workflow, attributing the full model spend to each tool could count it three times. It also mixed different workflows that happened to share a tool. The current learner compares **model × workload** and counts the model spend, retries, and verified outcome once for the workflow. Each upstream tool keeps its own call counts, bytes, latency, and failures.

This gives the router evidence to favor the faster or cheaper strategy for a particular job while checking completion quality. A model can favor Classic on a lookup and Code Mode on a join. Routing starts in Observe mode. In Active mode, eligible read-only Auto requests can use a recommendation based on recent matched evidence, with at least five pairs and a 90% verified-completion floor. At equal verified quality, the router requires at least 5% lower cost per verified completion within its latency, upstream-call, and recovery limits. This pilot compared the execution strategies. A held-out comparison with the previous routing policy would measure the incremental effect of adaptive routing.

## Measuring ROI

Use **cost per verified completion**. Add model input and output charges, upstream charges, and execution costs across all attempts, including failures and retries, then divide by verified completions. Compare against the existing workflow, including its usual recovery steps. If an arm has zero verified completions, report its spend and failure count. For local deployments, GPU time and energy per verified completion help translate token efficiency into operating cost. The results above report observed usage on mock workloads.

## Upfront versus deferred workflow pilot

### GPT-6 Luna at low reasoning

We ran five matched pairs in fresh Codex CLI sessions against the same mock balance lookup. Both arms used Code Mode. Upfront supplied all authorized Python stubs at listing time, and deferred found the matching tool through BM25. We alternated arm order and added 50 synthetic tools to the expanded catalog. All ten runs passed the expected-answer and grounding checks.

| Authorized tools | Pairs | Upfront median tokens (cached input) | Deferred median tokens (cached input) | Median wall time, upfront / deferred |
| ---: | ---: | ---: | ---: | ---: |
| 8 | 3 | 46,797 (29,952) | 65,188 (46,592) | 6.9 s / 10.4 s |
| 58 | 2 | 54,685 (33,920) | 69,929.5 (41,664) | 8.4 s / 8.6 s |

Upfront used 28% fewer reported tokens at eight tools and 22% fewer at 58. Each deferred run added one search call. Both arms made one upstream call per workflow. The totals include Codex session overhead and cached input.

The gateway estimated 867 upfront tool-definition tokens at eight tools and 3,614 at 58. Its 1,024-token budget would recommend deferred for the expanded catalog. This lookup favored upfront even there, so the policy starts in Observe. The pilot covers one task with two or three pairs per catalog size.

### Local Qwen3.5 9B

We ran one matched Qwen3.5 9B lookup pair per catalog size and execution mode in LM Studio. The finance caller could access eight tools in the small fixture and 58 in the expanded fixture. The table shows model-reported tokens and end-to-end wall time.

| Authorized tools | Execution | Upfront | Deferred |
| ---: | --- | --- | --- |
| 8 | Classic MCP | 2,361 tokens, 6.8 s, incorrect answer | 5,633 tokens, 10.6 s, verified |
| 8 | Code Mode | 3,372 tokens, 9.4 s, verified | 11,952 tokens, 23.2 s, audit evidence unverified |
| 58 | Classic MCP | 13,868 tokens, 9.5 s, verified | 13,363 tokens, 14.8 s, verified |
| 58 | Code Mode | 9,385 tokens, 10.1 s, verified | 8,288 tokens, 14.3 s, verified |

Deferred used fewer tokens on both expanded-catalog pairs and took longer in each. The small-catalog pairs had different verification outcomes. The discovery policy defaults to Observe while repeated runs establish the cost and latency trade-off.

## Discovery at catalog scale

The current gateway ships BM25 and keyword search. Workload routing picks how to run a job. Discovery ranks the tools available to the caller.

The latest runs used the current gateway's `search_tools` endpoint and mock MCP servers that advertised their tools. Recall@5 is the average share of expected tools found in the first five results. The small catalog used 20 queries, and each scaled catalog used 36. Gateway latency includes authorization, ranking, and response handling in an in-process run.

| Mock servers / tools | Search method | Direct recall@5 | Paraphrase recall@5 | Gateway p95 |
| --- | --- | ---: | ---: | ---: |
| 4 / 10 | Keyword | 100% | 46.7% | 3.0 ms |
| 4 / 10 | BM25 | 100% | 66.7% | 3.1 ms |
| 36 / 778 | Keyword | 100% | 0% | 6.0 ms |
| 36 / 778 | BM25 | 100% | 37.0% | 15.5 ms |
| 104 / 10,010 | Keyword | 94.4% | 0% | 56.9 ms |
| 104 / 10,010 | BM25 | 100% | 37.0% | 199.4 ms |

**BM25 remains the default.** It ranks locally without a separate model service and found all expected tools on the direct queries in this fixture. Keyword search was faster at larger sizes, and BM25 found more tools on paraphrased requests. BM25 scans the catalog on each search, so its latency grows with the number of tools available to the caller.

A second BM25 search can help when the first query has vague wording. At 778 tools, scripted rewrites using tool names and server terms recovered all expected tools for the 18 paraphrases. The two gateway searches together took 27.7 ms at p95, rising to 331.8 ms at 10,010 tools. These times cover the searches. An agent's ability to supply a useful rewrite and the cost of generating it depend on the client.

### Historical embedding and System 1 results

These saved figures are from the earlier 10–160-tool discovery benchmark. Both methods had 100% direct recall@5 on that fixture. The latency column reports mean search time from the earlier harness. The current table reports gateway p95 at different catalog sizes.

| Tools | Embedding paraphrase recall@5 | Embedding mean | Hybrid System 1 paraphrase recall@5 | Hybrid System 1 mean |
| ---: | ---: | ---: | ---: | ---: |
| 10 | 91.7% | 63.16 ms | 80.0% | 71.06 ms |
| 60 | 85.0% | 95.89 ms | 68.3% | 83.84 ms |
| 160 | 85.0% | 162.21 ms | 65.0% | 86.01 ms |

The embedding results used Qwen3 Embedding 0.6B. It recovered more paraphrased queries in the earlier fixture and needed a model service and catalog vectors. Hybrid System 1 used the stock `laya` model served by LM Studio at `/v1/systemone`, with no project-specific fine-tuning. It routed servers first, then scored candidate tools. The historical figures give context for the latency and recall trade-off alongside today's built-in methods.

## Workload fixtures

| Workload | Task |
| --- | --- |
| Single lookup | Fetch one customer's exact Snowflake ledger balance. |
| Standard reconciliation | Read the overdue ledger and EU/US invoices, join by customer ID, and return only discrepancies. |
| Bulk reconciliation | Run the same join on the bulk fixture, using the debt ledger and both regional invoice lists. |
