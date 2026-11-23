# MCPcodeaway — Vendor-Agnostic Code-Mode Universal MCP Gateway

**MCPcodeaway** is a stateless, vendor-agnostic MCP gateway that bridges enterprise Identity Providers (Entra ID, Okta, Keycloak, or local dev RS256 OIDC) to heterogeneous backends (`mcp_http`, `mcp_stdio`, and `openapi` / Swagger) while executing multi-step workflows inside an ephemeral, networkless **Monty (`pydantic-monty`)** Python sandbox.

## Why MCPcodeaway? (Platform Value Proposition)

- **Adaptive workflow routing:** Use Classic MCP for direct calls or Code Mode to bundle multiple tools, joins, and filtering in one sandbox execution. The gateway can recommend a strategy for each model and workload. See the [local and GPT-6 Luna results](BENCHMARKS.md#paired-pilot-results).
- **Zero-Trust Enterprise Security:** Per-user OIDC JWT validation, RFC 8693 / OBO delegated token exchange, host-side ABAC row filtering, PII/secret redaction, SSRF guards, and HMAC provenance grounding verification.
- **Requester Confirmation Gates:** Administrators can require a caller to confirm a specific tool call before the gateway sends it upstream. The gateway binds the confirmation to the caller, exact arguments, and current policy, and prevents reuse. This is a caller confirmation prompt, not approval by a manager or proof of the user's identity beyond the authenticated request.
- **Approval API (limited existing capability):** The gateway has a durable one-approver API for configured high-impact direct calls. The built-in inbox is hidden and onboarding no longer configures manager approvals; use the upstream's own approval workflow by default. The gateway API is available for external workflow integrations.
- **Sessionless MCP Design:** Every gateway and upstream MCP HTTP operation uses the current per-request protocol contract; the gateway never creates, stores, forwards, or depends on MCP session IDs, and upstream HTTP requests never retain or reuse cookies. Multi-step business operations pass explicit resource IDs through independently authorized tool calls.
- **Full Operate Control Plane (`/dashboard`):** Nuxt 4 + `@ui-sdk/vue` console with real-time WebSocket telemetry, Linear-style Request Traces (`/traces`), Gateway Usage & Group Timeline Aggregation (`/analytics`), Model Performance and Efficiency (`/model-performance`), 4-step API Onboarding + A/B Lab (`/onboarding`), Audit & S3/JSONL Compliance Archival (`/audit`), and Custom Alert Rules (`/settings`).

## Architecture

For an agent-oriented map of caller, authentication, execution, approval,
operator, and runtime journeys, start with the [Gateway Flow Map](docs/gateway-flow-map.md).
Detailed lifecycle transitions are in [Application State Machines](docs/state-machines.md).

### Decoupled Execution × Discovery Matrix

Callers select execution and discovery strategies independently via query parameters or HTTP headers (`?execution=...&discovery_mode=...`, `X-Execution-Mode`, `X-Discovery-Mode`, `X-Intent`) or legacy `?mode=` presets (`traditional`, `traditional_discover`, `codemode`, `codemode_upfront`, `hybrid`):

| Execution \ Discovery | `upfront` | `deferred` | `x_intent` |
|---|---|---|---|
| `classic` | Traditional upfront `tools/list` + direct JSON-RPC calls | `discover_tools` meta-tool + `call_upstream_tool` | Pre-filtered direct schemas using `X-Intent` header |
| `codemode` | `execute_code` + upfront tool catalog (`codemode_upfront`, 2 turns) | `discover_tools` + `execute_code` (3-tool ~463-token surface) | Intent pre-filtered stubs injected into `execute_code` |
| `hybrid` | Upfront catalog + both `call_upstream_tool` & `execute_code` | Deferred discovery + both execution paths | Intent pre-filtered hybrid surface |

### Core Capabilities

- **Typed Stubs & Shape Recovery**: Extracts `outputSchema` from MCP tools and OpenAPI `200` JSON responses into typed Python sandbox stub docstrings (`Returns: ...`), paired with Monty `KeyError` runtime `observed_output_shapes` hints to eliminate trial-and-error code retries.
- **Enterprise AuthZ, Delegated OBO, RLS & Host-Side ABAC**:
  - Validates RS256 JWTs (`OidcTokenValidator` / `LocalDevKeyManager`) with tenant allowlisting (`allowed_tenants`).
  - Performs per-user delegated OBO token exchange (`DelegatedTokenExchanger`) with isolated `(sub, tenant_id, downstream_audience)` caching, propagating claims such as `client_groups` (`eu_accountant` -> `["EU"]`, `us_accountant` -> `["US"]`) for downstream row-level security.
  - Enforces host-side `AbacRuleConfig` (`_apply_pre_call_abac`, `_apply_post_call_abac`) before dispatching and after response receipt when talking to shared `api_key` or `bearer_secret` upstreams.
- **Sandbox Security & Response Grounding**: Monty blocks filesystem, environment variables, sockets, and imports; enforces `max_calls_per_execution`, timeouts, and per-user rate limits; and optionally requires HMAC-signed host tool provenance (`require_tool_grounding`, `provenance_signing_secret`).
- **Composable Barebones-First Profiles (`GATEWAY_PROFILE`)**: Custom and built-in profiles in [config/profiles.yaml](config/profiles.yaml) build atop barebones `production` (which mounts only `/health`, `/ready`, `/.well-known/*`, `/auth/me`, `/mcp`, `/metrics`, tenant-isolated `/audit/*` + `/audit/value`, and read-only `/admin/upstreams`, returning `404` on `/dashboard`, `/compare/run`, `/benchmarks/latest`, `/dev/*`, and dynamic writes). `staging`, `demo`, and custom `extends:` profiles selectively layer onboarding/eval features on top, backed by multi-target builds (`gateway`, `sandbox-worker`, `gateway-demo`) in [Dockerfile](Dockerfile).
- **HITL Onboarding & Control-Plane Hardening**:
  - `GET /admin/upstreams/agent-prompt`, `POST /admin/upstreams/inspect`, `POST /admin/upstreams`, `DELETE /admin/upstreams/{name}`, and the browser `/dashboard` provide OpenAPI `securitySchemes` detection, Gateway/IdP/Backend checklists, operation curation, schema drift pinning, and per-tool effect settings during onboarding and editing. Onboarding supports `read` and caller-confirmed `write` operations. Manager approval workflows are out of scope for onboarding; use the upstream's approval process or an external workflow integration.
  - Control-plane RBAC (`require_admin_auth`, default `True` in `auth_mode="oidc"`) restricts `/admin/*` to `admin_roles` (`gateway_admin`, `platform_admin`) and isolates `/audit/*` (`GET /audit/runs`, `/audit/runs/{run_id}`, `/audit/summary`) strictly by `tenant_id` for non-admin tokens.
  - SSRF URL guard (`validate_upstream_url`, `ssrf_protection_enabled`) blocks non-HTTP(S) schemes and cloud metadata hosts in all environments, and blocks loopback/private/internal hosts in `prod` unless allowlisted via `allowed_upstream_hosts`.
- **4-Pillar Observability, Value Proof & Horizontal Scaling**:
  - **Structured JSON Event Logging** (`log_format="json"`) correlated by `request_id`, `tenant_id`, `user_sub`, `profile`, and OTel `trace_id`/`span_id` across `mcp.request.completed`, `auth.denied`, `auth.obo.exchanged`, `policy.denied`, `abac.filtered`, `redaction.applied`, `grounding.blocked`, `sandbox.executed`, and `upstream.failed` (with sensitive key redaction).
  - **End-to-End OTel Tracing + W3C `traceparent`** propagation across `/mcp`, discovery, OBO token exchange, Monty sandbox execution, and downstream HTTP calls, with optional OTLP export (`mcpcodeaway[otlp]`, `GATEWAY_OTLP_ENDPOINT`).
  - **Prometheus `/metrics` & Executive ROI API (`GET /audit/value`)**: Exposes counters/histograms (`gateway_mcp_requests_total`, `gateway_tokens_saved_total`, `gateway_abac_rows_filtered_total`, `gateway_redactions_total`, `gateway_turns_avoided_total`, `gateway_execution_duration_seconds`) and computes estimated LLM USD saved (`cost_per_1m_input_tokens_usd`), context-overflow preventions, OBO cache hit rates, ABAC/PII governance counts, and classic-to-codemode migration recommendations.
- **MCP 2026-07-28 & Multi-Replica Persistence**: The gateway validates per-request protocol metadata plus Streamable HTTP routing headers on both gateway MCP endpoints, and requires modern discovery from HTTP MCP upstreams. Redis supports identity-scoped delegated tokens, rate limits, admin OIDC PKCE state, and catalog epoch synchronization; it stores no MCP session state. OpenAPI integrations that require cookie authentication are rejected. Supports `PostgresAuditStore` (`mcpcodeaway[postgres]`, `GATEWAY_POSTGRES_DSN`), secret rotation, audit retention, remote `sandbox-worker` replicas, and optional cloud-agnostic Nginx reverse proxy ([deploy/nginx/nginx.conf](deploy/nginx/nginx.conf) via `docker compose --profile prod up` / `--profile scaled up`).
  - **Dual-Mode Admin Auth & Responsive `@ui-sdk/vue` Control Plane**: Secures the responsive Nuxt 4 + `@ui-sdk/vue` Admin Console (`web/`, served at `/dashboard` in `gateway-ui` / `gateway-demo` or hosted standalone with `NUXT_PUBLIC_API_BASE` + `GATEWAY_CORS_ALLOWED_ORIGINS`) via OIDC Authorization Code + PKCE (`HttpOnly` HMAC-signed session cookie) or Dev Persona / Bearer exchange, exposing live gateway & worker CPU/RAM telemetry (`GET /admin/telemetry/runtime`) and company-group, client-group, user, and model cost attribution (`GET /audit/analytics`).
  - **3-Layer Mode Selection & A/B Engine**: Combines deterministic algorithmic `?execution=auto` scoring, in-wizard Onboarding A/B Testing (`POST /admin/upstreams/ab-test` with instant replay and optional Admin/Dev OpenAI-compatible LLM runs), and per-upstream live `ab_experiment` traffic splits (`GET /admin/ab/experiments`).

## Quickstart (Zero External Dependencies)

For the current scope, test evidence, known limits, and the optional local
OIDC/shared-service lab, see [MVP readiness](docs/mvp-readiness.md).

```bash
# 1. Install dependencies (Python 3.12+ + uv)
uv sync --extra dev

# 2. Start mock enterprise backends (Snowflake MCP, EU/US Billing OpenAPI, Ops Ticketing MCP)
uv run uvicorn examples.mock_backends:app --port 8001

# 3. Start the gateway in another terminal
uv run uvicorn gateway.server:app --port 8000
```

Open `http://127.0.0.1:8000/dashboard` for the interactive playground, HITL onboarding wizard, and agent prompt exporter. In the demo profile, **Run Golden Demo Traffic** sends discovery and MCP calls through the gateway to the local mock upstreams, including reads, delayed writes, idempotent replay, payload conflict, upstream rejection, a lost response, a write that remains processing, status reconciliation, task polling, and RBAC denial. Customer-configured upstreams are excluded. Mock writes use the demo-only `recovery_workbench` upstream and create visible records on **Operations Recovery**; that view refreshes automatically while open. The dashboard also seeds clearly labeled simulated paired model evidence for report exploration; it is not used for routing recommendations. The local sandbox creates an owner-only recovery key file beside its SQLite database; production recovery continues to require explicitly managed persistent keys. **MCP Write Recovery Lab** remains isolated and does not call registered upstreams.

### CLI Onboarding & Comparison

```bash
# Inspect an OpenAPI/Swagger spec and export a PR-ready config/upstreams.yaml snippet
uv run gateway add-mcp \
  --name billing_eu \
  --type openapi \
  --url http://127.0.0.1:8001/billing \
  --openapi-spec-url http://127.0.0.1:8001/billing/openapi.json \
  --auth-type delegated_token \
  --downstream-audience api://billing-mcp \
  --export-yaml

# Run Classic MCP vs. Code Mode side-by-side comparison
uv run gateway compare --scenario reconcile_overdue --persona finance_analyst
```

You can connect supported APIs directly by registering an OpenAPI spec. The gateway turns each allowed operation into a callable tool. In deferred Code Mode, start with `search_tools` to find matching operations across authorized upstreams, then use their Python stubs in `execute_code`. Use `list_servers` when you want to browse the upstreams.

Use clear operation IDs and summaries, typed parameters, and JSON response schemas in the spec. Document details that affect a call, such as which ID to pass, units, and pagination. Configure credentials and tool access on the gateway.

### Require caller confirmation before a write tool call

In onboarding, inspect an upstream and explicitly enable each operation. Classify writes as `write` to require caller confirmation; reads stay ungated. The operation allowlist starts empty, so discovered operations do not become callable until selected. For business manager approvals, use the upstream's workflow. The gateway's existing durable approval API can integrate with an external workflow, but it has no built-in inbox or notification service and is not configured by onboarding.

```yaml
upstreams:
  - name: inventory_api
    type: mcp_http
    url: https://inventory.example.com/mcp
    auth_type: none
    allowed_roles: [inventory_reader, inventory_writer]
    allowed_groups: [inventory-team]
    allowed_operations: [list_items, delete_item]
    tool_effects:
      list_items: read
      delete_item: write
    mutating_tools: [delete_item]
    tool_approval_policies:
      delete_item:
        mode: requester_confirmation
        version: 1
        confirmation_message: "Confirm deleting this item."
```

When a caller invokes a protected operation, normal authentication and authorization still run first. The gateway then pauses the call and returns an MCP `input_required` response asking the connected client to collect confirmation. The client retries with its response; the gateway checks that the caller, tool arguments, and policy still match, and accepts the continuation only once. A decline, stale or changed request, reused confirmation, or missing client form-elicitation capability does not dispatch the protected tool. The policy version changes when an administrator edits the setting, which invalidates outstanding confirmations.

The gateway also retains a narrow durable approval API for external workflow integrations. It does not provide an approver inbox, notifications, or automatic agent resume, and onboarding does not expose manager-approval setup. Treat this as an integration surface, not a gateway-owned business approval workflow.

Requester confirmation is an optional gateway gate in addition to upstream authorization; it does not replace the upstream's business permissions or approval workflow. Conditional manager-approval rules based on the requester's group or argument values (for example, requiring approval for transfers above a configured amount) are deferred. See the [approval gate design and roadmap](docs/approval-gate-enhancements.md#business-authorization-boundary-and-deferred-conditional-approvals).

These gates apply to individual, direct MCP tool calls. A gated tool cannot be invoked from Code Mode; the callback is rejected before that upstream action. Code Mode scripts are not transactional, so ungated calls that ran earlier in a script are not undone if a later callback reaches a gated tool. Use separate direct calls when a gate is required.

The gateway cannot prove that a person physically clicked the requester prompt: it trusts the MCP client to present the request and return the answer. Do not use this prompt to collect passwords, tokens, or other secrets. The existing external approval API supports one eligible approver and requester-driven retry; it has no quorum, escalation, notifications, or automatic agent resume. See the [approval gate design and roadmap](docs/approval-gate-enhancements.md).

## Benchmark results

See [Cloudflare's external benchmarks](BENCHMARKS.md#external-benchmarks) for the upfront cost in tokens of exposing MCP tools directly or through server-side Code Mode.

These pilot runs used mock Snowflake and EU/US billing services. A verified result passed the answer and grounding checks.

| Model and workload | Classic MCP | Code Mode |
| --- | --- | --- |
| GPT-6 Luna low, bulk reconciliation | 88,179 tokens, verified | 47,272 tokens, verified |
| GPT-6 Luna low, standard reconciliation | 49,374 tokens, 17.3 s, verified | 64,937 tokens, 11.1 s, verified |
| Local Qwen3.5 9B, single lookup | 2,287 tokens per verified completion, 5/5 verified | 3,160 tokens per verified completion, 5/5 verified |

In the Luna bulk pair, Code Mode used **46.4% fewer reported tokens** and made one gateway tool call versus six. In standard reconciliation, it was faster and used **31.5% more tokens**. Classic used **27.6% fewer tokens** in the five local 9B lookup pairs. The [full benchmark results](BENCHMARKS.md#paired-pilot-results) include the local 1.5B and 4B MTP runs, token accounting, and sample sizes.

### Why routing learns by workload

We previously scored each model and tool together. A workflow can call several tools while incurring one shared model cost, so attributing the full spend to each tool inflated per-tool totals. Now the gateway compares complete workflows by **model and workload**, counts model usage once, and records calls, bytes, latency, and failures for each tool. That lets the same model favor different strategies for a point lookup and a reconciliation job.

For ROI, compare **cost per verified completion** against the current workflow, including failed attempts and retries. Use actual input and output prices, cache pricing where applicable, and upstream charges. For local models, measure GPU time and energy per verified completion. These pilots report token use and outcomes.

### Discovery at scale

Discovery controls when clients receive tool definitions. Upfront includes them in the initial listing. Deferred lets clients call `search_tools` to load relevant definitions. For automatic requests, the gateway estimates the complete listing after applying the caller's permissions and server scope. It recommends upfront if the listing fits within 1,024 estimated tokens and deferred for larger listings. Classic definitions and Code Mode stubs are sized independently. Set `GATEWAY_AUTO_DISCOVERY_TOKEN_BUDGET` to change the threshold.

In [five matched GPT-6 Luna low Code Mode pairs](BENCHMARKS.md#upfront-versus-deferred-workflow-pilot) on a mock lookup, all ten answers verified. Upfront used 28% fewer reported tokens at eight authorized tools and 22% fewer at 58. Those totals include cached input. The expanded catalog exceeded the upfront budget, so this result is a reason to calibrate the policy on more workloads before enabling it.

The policy defaults to Observe, which keeps the current discovery behavior and logs applied and recommended discovery in `mcp.discovery.decision` events. Estimates reported as lower bounds stop at the first tool that exceeds the budget. Use `GATEWAY_AUTO_DISCOVERY_POLICY=active` to apply recommendations, and `observe` to roll back. This setting controls discovery independently of adaptive execution. The `mcp.request.completed` events record response bytes, estimated listing or search tokens, returned definitions, and search latency where available. These are gateway measurements. Workflow verification still uses paired evidence.

BM25 remains the default and ranks tools inside the gateway. In the latest synthetic run it found all expected tools for direct queries at 10,010 tools across 104 mock servers, with about 199 ms p95 gateway latency. Paraphrased queries were harder. The [discovery results](BENCHMARKS.md#discovery-at-catalog-scale) show the effect of a second query and summarize the earlier embedding and System 1 measurements.

### Adaptive Execution Intelligence

The adaptive router compares paired runs of the same model, workload, fixture, and runner configuration. It needs at least five matched pairs, then requires at least 90% of each arm's answers to be both correct and grounded. If only one arm clears that floor, that arm wins. If both clear it, higher verified quality wins; tied quality may be broken by a cost advantage of at least 5%, provided p95 time and upstream calls are no more than 20% higher and recovery is no more than 0.25 events per workflow higher. Set model input/output prices under **Settings → Model pricing** to compare USD cost; without complete prices, the report can compare provider-token usage when available. If evidence is sparse, stale, conflicting, or fails the gates, the router abstains and normal Auto scoring remains in control.

Evidence is considered from most specific to broadest: exact model and workload, a consistent qualified workload-family result when that workload has a family, then a consistent qualified result across that model's workloads. Fallbacks need a single winner; if qualified cohorts disagree, the router abstains instead of choosing a winner by majority.

#### Reproduce the routing decision without a model account

Run the controlled routing tests:

```sh
uv run pytest -q \
  tests/test_adaptive_execution.py::test_live_resolver_returns_exact_recommendation_details \
  tests/test_adaptive_execution.py::test_live_resolver_reports_workload_family_fallback \
  tests/test_adaptive_execution.py::test_live_resolver_explains_sparse_and_conflicting_evidence \
  tests/test_adaptive_execution.py::test_active_mcp_auto_routes_two_mock_models_to_opposite_winners \
  tests/test_adaptive_execution.py::test_adaptive_route_context_survives_discovery_execution_and_rollback \
  tests/test_benchmark.py::test_classic_parallel_tool_calls_are_dispatched_concurrently \
  tests/test_benchmark.py::test_parallel_classic_calls_excludes_codemode_and_mutating_workflows
```

The runtime test feeds five matched repeats into the same resolver used by live Auto requests. Both arms are 100% correct and grounded, each takes 100 ms, uses one upstream call, and has no recovery. Its controlled fixture assigns Code Mode 100 input tokens and Classic 200, with equal configured token prices, so Code Mode's fixture cost is 50% lower. Observe records the exact Code Mode recommendation without applying it; Active returns Code Mode and explains that the cost advantage clears the 5% gate within regression limits. The test also checks fallback for mutating tools and that an explicit execution choice is not overridden. This proves the selection and application paths on a controlled fixture; it is not a measured claim that Code Mode is faster or better for a real model. The demo's generated evidence is simulated and cannot affect live routing.

| Controlled test fixture | Verified quality | Input tokens per run | p95 wall time | Upstream calls | Result |
| --- | ---: | ---: | ---: | ---: | --- |
| Classic | 100% | 200 | 100 ms | 1 | Eligible |
| Code Mode | 100% | 100 | 100 ms | 1 | Selected in Active mode |

The router can select Classic as well. When Code Mode falls below the joint correctness-and-grounding floor while Classic clears it, the quality gate chooses Classic even if its token cost is higher. A workload-specific result can also differ from another workload for the same model; there is no global “Code Mode always wins” rule.

#### Two-model mock request proof

`test_active_mcp_auto_routes_two_mock_models_to_opposite_winners` runs the real Auto MCP request path against an isolated gateway and temporary store. It imports five paired, prompt-free fixture repeats per mock model. Both strategies have identical 100% verified quality, 100 ms latency, one upstream call, and no recovery; only provider-token usage differs:

| Mock model | Classic input tokens | Code Mode input tokens | Active Auto result |
| --- | ---: | ---: | --- |
| `mock-classic-wins` | 100 | 200 | Traditional; direct tool schema, no `execute_code` |
| `mock-codemode-wins` | 200 | 100 | Code Mode; `execute_code` schema |

The test sends `tools/list` with `execution=auto`, the model name, and the same recognized read-only intent for each model. It asserts that the decision header names the expected exact-workload winner and that the returned tool surface changes accordingly. This demonstrates that Active applies different evidence-backed winners per model; the token values are controlled test inputs, not observed model performance and not persisted into the live demo tenant.

| Workflow shape | Classic MCP | Code Mode |
| --- | --- | --- |
| Independent calls | A model and client that support parallel tool calls can send several `tools/call` requests at once. The gateway authorizes and limits each request separately. | One `execute_code` script can use `asyncio.gather(...)` for independent tool calls. |
| Dependent calls | The client sends a call, returns its result to the model, then sends the next call the model chooses. This adds a model turn when the next arguments depend on that result. | The script can await the first result, compute the next arguments, and call again in the same execution without asking the model to plan another turn. |
| Access and writes | The gateway checks each call against the caller's current roles, groups, and tool policy. | Each callback remains scoped to the caller. Gated writes are rejected from Code Mode, and live adaptive execution only routes recognized read-only workloads. |

#### Compare execution, discovery, authorization, and parallel calls

For an actual model, benchmark both execution modes while holding discovery fixed, then repeat with the other discovery mode. The runner also supports single lookups, reconciliations, bulk data, trial-balance lookups, batch calculations, and a separate mutating workflow. Run the same command five times and save each report separately so each cohort has five matched repeats:

```sh
for n in 1 2 3 4 5; do
  uv run python -m benchmarks.live_llm_bench \
    --provider openai_compatible \
    --base-url http://127.0.0.1:1234/v1 \
    --models qwen/qwen3.5-9b \
    --modes classic:upfront,codemode:upfront,classic:deferred,codemode:deferred \
    --tasks single_lookup,standard,bulk_200,trial_balance_audit,compute_batch_penalties,mutating_ticket_workflow \
    --synthetic-tools 0,50 \
    --output "/tmp/adaptive-run-${n}.json"
done
```

Each report contains a prompt-free `adaptive_evidence` array with correctness, grounding, provider usage where reported, latency, call/recovery counts, and stable matching metadata. It also records AuthN/RBAC preflight checks separately: the finance persona must not see the support-only surface, and the support persona must not see finance tools. Authorization checks do not make unauthorized calls eligible for adaptive routing; each actual request is still scoped by the caller's current permissions.

For non-mutating Classic tasks, the benchmark asks the model provider to allow parallel tool calls and dispatches multiple calls from the same response concurrently. It deliberately keeps the mutating workflow out of that path. The gateway still authorizes and limits each request separately. Dependent Classic steps remain turn-by-turn, while Code Mode can also perform a result-dependent next call inside its script. The harness tests cover concurrent dispatch and verify that Code Mode and mutating workflows are excluded from the Classic-parallel path.

Merge report arrays with `jq -s '{"evidence": map(.adaptive_evidence[]) }' /tmp/adaptive-run-*.json > /tmp/adaptive-evidence.json`, then POST that file to `/admin/adaptive-execution/evidence` as an admin in the tenant where the benchmark will be used. Imports are idempotent and tenant-scoped. This endpoint is a trusted operator import for verified benchmark runner artifacts: it validates the versioned evidence shape, timestamps (allowing at most five minutes of clock skew), and nonnegative finite metrics, but it does not cryptographically attest provenance across databases. Do not treat arbitrary client-submitted evidence as verified benchmark output.

Review **Model Performance → Adaptive routing** in Observe mode first. Live routing uses qualified evidence from the last 30 days, with at least five matched pairs and a 90% jointly correct-and-grounded floor. Active can then be enabled under **Settings → Adaptive routing**. It applies only to Auto requests with a known model, recognized workload, and read-only accessible tools. The report separates this live 30-day decision from metrics shown for the selected analysis window. Return to Observe at any time to stop applying adaptive decisions.

## Vendor-Agnostic Integrations & Scaling

- **Enterprise IdP & MCP Client Examples**: See [examples/integrations/](examples/integrations/) (`entra_copilot_studio`, `keycloak_oidc`, `mcp_clients`). For production management-console OIDC registration, role assignment, gateway environment variables, and acceptance checks, follow [Company SSO and Control-Plane Access](docs/oidc_deployment.md).
- **Horizontal Scaling & Barebones Production**: Build the barebones `gateway` image target (`docker build --target gateway .`) or run `docker compose --profile prod up` / `--profile scaled up` (using [docker-compose.yml](docker-compose.yml)) for multi-replica stateless gateways, Postgres audit persistence (`GATEWAY_POSTGRES_DSN`), Redis caching (`GATEWAY_REDIS_URL`), and egress-isolated `sandbox-worker` pods authenticated with `GATEWAY_SANDBOX_WORKER_SECRET` and `GATEWAY_PROVENANCE_SIGNING_SECRET`. See [docs/cloud_scalability.md](docs/cloud_scalability.md).

## CI/CD, Security QA & Pre-CI Gates

- **GitHub Actions CI/CD (SHA-pinned, least-privilege):**
  - [backend-ci.yml](.github/workflows/backend-ci.yml): `scripts/agent_preflight.py`, Ruff format & lint, full `pytest -v`, and multi-target Docker builds (`gateway`, `sandbox-worker`, `gateway-ui`).
  - [frontend-ci.yml](.github/workflows/frontend-ci.yml): `npm ci`, `nuxt prepare`, `npm run generate`, and live `/dashboard/` + `_nuxt/` asset verification.
  - [qa-ci.yml](.github/workflows/qa-ci.yml): Golden E2E flow suite + locked offline benchmark verification (`--verify-thresholds`).
  - [security-ci.yml](.github/workflows/security-ci.yml) + [dependabot.yml](.github/dependabot.yml): CodeQL (`python`, `javascript-typescript`, `actions`), Dependency Review, Zizmor workflow audit, pinned Gitleaks (`.gitleaks.toml`), and scheduled `npm`/`uv` audits.
- **Golden Flows & Locked Benchmarks:** Deterministic end-to-end policy/grounding flows live in [tests/golden/flows.json](tests/golden/flows.json) ([tests/test_golden_flows.py](tests/test_golden_flows.py)); requester-confirmation journeys, including replay, policy-change, Code Mode, and concurrent-call cases, live in [tests/golden/approval_flows.json](tests/golden/approval_flows.json) ([tests/test_approval_golden.py](tests/test_approval_golden.py)). Minimum/maximum benchmark bounds live in [benchmarks/thresholds.json](benchmarks/thresholds.json) ([tests/test_benchmark_regression.py](tests/test_benchmark_regression.py)).
- **Agent Navigation & Local Gates:** Follow [AGENTS.md](AGENTS.md) ([CLAUDE.md](CLAUDE.md)) for two-tier `ripwire` + `graphify` navigation, install git hooks via `pre-commit` (`.pre-commit-config.yaml`) or wire `hooks/agent-post-edit.sh`, and run `./scripts/pre-ci.sh`.

## Verification

```bash
./scripts/pre-ci.sh --fast   # agent preflight, ruff, golden flows & regression tests
./scripts/pre-ci.sh --full   # full pytest, Nuxt generate & --verify-thresholds (add --docker for images)
```

For the focused local security/integration suite and optional disposable PostgreSQL checks, see [Local Integration Testing](docs/local-integration-testing.md).
