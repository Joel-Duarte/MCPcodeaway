# MCPcodeaway — Vendor-Agnostic Code-Mode Universal MCP Gateway

**MCPcodeaway** is a stateless, vendor-agnostic MCP gateway that bridges enterprise Identity Providers (Entra ID, Okta, Keycloak, or local dev RS256 OIDC) to heterogeneous backends (`mcp_http`, `mcp_stdio`, and `openapi` / Swagger) while executing multi-step workflows inside an ephemeral, networkless **Monty (`pydantic-monty`)** Python sandbox.

## Why MCPcodeaway? (Platform Value Proposition)

- **Adaptive workflow routing:** Use Classic MCP for direct calls or Code Mode to bundle multiple tools, joins, and filtering in one sandbox execution. The gateway can recommend a strategy for each model and workload. See the [local and GPT-6 Luna results](BENCHMARKS.md#paired-pilot-results).
- **Zero-Trust Enterprise Security:** Per-user OIDC JWT validation, RFC 8693 / OBO delegated token exchange, host-side ABAC row filtering, PII/secret redaction, SSRF guards, and HMAC provenance grounding verification.
- **Requester Confirmation Gates:** Administrators can require a caller to confirm a specific tool call before the gateway sends it upstream. The gateway binds the confirmation to the caller, exact arguments, and current policy, and prevents reuse. This is a caller confirmation prompt, not approval by a manager or proof of the user's identity beyond the authenticated request.
- **Durable Manager Approval:** High-impact direct tool calls can require requester confirmation and one approval by an authenticated member of a customer-configured OIDC group. The gateway stores a tenant-scoped action digest and policy version, exposes an approval API and console inbox, prevents self-approval, and claims an approved action once when the requester retries it.
- **Sessionless MCP Design:** Every gateway and upstream MCP HTTP operation uses the current per-request protocol contract; the gateway never creates, stores, forwards, or depends on MCP session IDs, and upstream HTTP requests never retain or reuse cookies. Multi-step business operations pass explicit resource IDs through independently authorized tool calls.
- **Full Operate Control Plane (`/dashboard`):** Nuxt 4 + `@ui-sdk/vue` console with real-time WebSocket telemetry, Linear-style Request Traces (`/traces`), Gateway Usage & Group Timeline Aggregation (`/analytics`), Model Performance and Efficiency (`/model-performance`), 3-step HITL Onboarding + A/B Lab (`/onboarding`), Audit & S3/JSONL Compliance Archival (`/audit`), and Custom Alert Rules (`/settings`).

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
  - `GET /admin/upstreams/agent-prompt`, `POST /admin/upstreams/inspect`, `POST /admin/upstreams`, `DELETE /admin/upstreams/{name}`, and the browser `/dashboard` provide OpenAPI `securitySchemes` detection, Gateway/IdP/Backend checklists, operation curation, schema drift pinning, and per-tool effect/approval settings during onboarding and editing. Production YAML must list each enabled operation and classify it as `read`, `write`, or `high_impact`; startup rejects incomplete production policy.
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

Open `http://127.0.0.1:8000/dashboard` for the interactive playground, HITL onboarding wizard, and agent prompt exporter. In the demo profile, **Demo Control → MCP Write Recovery Lab** runs isolated golden recovery journeys against an in-process mock; it does not call registered upstreams.

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

### Require confirmation or manager approval before a tool call

In onboarding, inspect an upstream and explicitly enable each operation. Classify writes as `write` to require requester confirmation, or `high_impact` to also require a customer-defined approver group. Reads stay ungated. The operation allowlist starts empty, so discovered operations do not become callable until selected. In GitOps or production, configure the same policy in the upstream YAML; `tool_approver` is the default approval role and can be changed with `GATEWAY_APPROVAL_ROLES`.

```yaml
upstreams:
  - name: inventory_api
    type: mcp_http
    url: https://inventory.example.com/mcp
    auth_type: none
    allowed_roles: [inventory_reader, inventory_writer]
    allowed_groups: [inventory-team]
    allowed_operations: [list_items, delete_item, issue_refund]
    tool_effects:
      list_items: read
      delete_item: write
      issue_refund: high_impact
    mutating_tools: [delete_item, issue_refund]
    tool_approval_policies:
      delete_item:
        mode: requester_confirmation
        version: 1
        confirmation_message: "Confirm deleting this item."
      issue_refund:
        mode: manager_approval
        version: 1
        confirmation_message: "Approve this refund for the stated customer and amount."
        approver_groups: [finance-approvers]
```

When a caller invokes a protected operation, normal authentication and authorization still run first. The gateway then pauses the call and returns an MCP `input_required` response asking the connected client to collect confirmation. The client retries with its response; the gateway checks that the caller, tool arguments, and policy still match, and accepts the continuation only once. A decline, stale or changed request, reused confirmation, or missing client form-elicitation capability does not dispatch the protected tool. The policy version changes when an administrator edits the setting, which invalidates outstanding confirmations.

For manager approval, the requester confirms first; the gateway records the pending action without dispatch. An eligible approver can approve or deny at `/admin/approvals`. After approval, the requester retries the same exact call and the gateway dispatches it once. The approval API can be used by an existing workflow UI; v1 does not send notifications or resume an unattended agent automatically.

These gates apply to individual, direct MCP tool calls. A gated tool cannot be invoked from Code Mode; the callback is rejected before that upstream action. Code Mode scripts are not transactional, so ungated calls that ran earlier in a script are not undone if a later callback reaches a gated tool. Use separate direct calls when a gate is required.

The gateway cannot prove that a person physically clicked the requester prompt: it trusts the MCP client to present the request and return the answer. Do not use this prompt to collect passwords, tokens, or other secrets. V1 manager approval supports one eligible approver, no quorum or escalation, and requester-driven retry. Notifications, automatic agent resume, external workflow adapters, and model-based automatic approvals are future work; see the [approval gate design and roadmap](docs/approval-gate-enhancements.md).

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

Run the live benchmark in at least five distinct invocations with the same model, workload, fixture, runner settings, and discovery configuration to build five matched repeats; save each report to a different file. For example: `python -m benchmarks.live_llm_bench --provider openai_compatible --base-url http://127.0.0.1:1234/v1 --models qwen/qwen3.5-9b --tasks standard --synthetic-tools 0,50 --output /tmp/workflow-1.json` and repeat with `/tmp/workflow-2.json`. Each report includes a versioned `adaptive_evidence` array with correctness, grounding, provider usage where reported, latency, call/recovery counts, and stable matching metadata; prompts and answers are excluded. Merge report arrays with `jq -s '{"evidence": map(.adaptive_evidence[]) }' /tmp/workflow-*.json > /tmp/adaptive-evidence.json`, configure model input/output prices in **Settings → Model pricing**, then POST that JSON to `/admin/adaptive-execution/evidence` with an admin bearer token. Imports are idempotent and scoped to the signed-in admin tenant. This admin endpoint is a trusted operator import for verified benchmark runner artifacts: it validates the versioned evidence shape, timestamps (allowing at most five minutes of clock skew), and nonnegative finite metrics, but it does not provide cryptographic provenance attestation across databases. Do not treat arbitrary client-submitted evidence as verified benchmark output.

Review **Model Performance → Routing** in Observe mode first. The router uses recent qualified evidence only after five matched pairs and a 90% jointly correct and grounded floor; Active can then be enabled under **Settings → Routing & calibration**. Active routing applies only to Auto requests with an explicitly supplied or deterministically inferred workload and entirely read-only accessible tools. Return to Observe at any time to roll back adaptive decisions.

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
