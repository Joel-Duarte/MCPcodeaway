# MCPcodeaway — Vendor-Agnostic Code-Mode Universal MCP Gateway

**MCPcodeaway** is a stateless, vendor-agnostic MCP gateway that bridges enterprise Identity Providers (Entra ID, Okta, Keycloak, or local dev RS256 OIDC) to heterogeneous backends (`mcp_http`, `mcp_stdio`, and `openapi` / Swagger) while executing multi-step workflows inside an ephemeral, networkless **Monty (`pydantic-monty`)** Python sandbox.

## Why MCPcodeaway? (Platform Value Proposition)

- **Up to 99% Catalog Surface Reduction & 71–93% Multi-Step Token Savings:** Exposes a constant ~463-token deferred discovery + `execute_code` surface instead of stuffing 50–500 tool schemas into every prompt, and executes joins/filters inside Monty before returning compact grounded results. See [BENCHMARKS.md](BENCHMARKS.md) for live `qwen/qwen3.5-9b` A/B results, wins, and honest single-lookup tradeoffs.
- **Zero-Trust Enterprise Security:** Per-user OIDC JWT validation, RFC 8693 / OBO delegated token exchange, host-side ABAC row filtering, PII/secret redaction, SSRF guards, and HMAC provenance grounding verification.
- **Stateless-First Cloud-Native Design:** Built around stateless Streamable HTTP MCP and ephemeral Monty sandboxes—no sticky sessions required for horizontal scaling—while transparently handling legacy `Mcp-Session-Id` upstreams when needed.
- **Full Operate Control Plane (`/dashboard`):** Nuxt 4 + `@ui-sdk/vue` console with real-time WebSocket telemetry, Linear-style Request Traces (`/traces`), Gateway Usage & Group Timeline Aggregation (`/analytics`), 1-click Mode Optimization (`/optimization`), 3-step HITL Onboarding + A/B Lab (`/onboarding`), Audit & S3/JSONL Compliance Archival (`/audit`), and Custom Alert Rules (`/settings`).

## Architecture

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
  - `GET /admin/upstreams/agent-prompt`, `POST /admin/upstreams/inspect`, `POST /admin/upstreams`, `DELETE /admin/upstreams/{name}`, and the browser `/dashboard` provide OpenAPI `securitySchemes` detection, Gateway/IdP/Backend checklists, `allowed_operations` / `excluded_operations` curation, and schema drift pinning.
  - Control-plane RBAC (`require_admin_auth`, default `True` in `auth_mode="oidc"`) restricts `/admin/*` to `admin_roles` (`gateway_admin`, `platform_admin`) and isolates `/audit/*` (`GET /audit/runs`, `/audit/runs/{run_id}`, `/audit/summary`) strictly by `tenant_id` for non-admin tokens.
  - SSRF URL guard (`validate_upstream_url`, `ssrf_protection_enabled`) blocks non-HTTP(S) schemes and cloud metadata hosts in all environments, and blocks loopback/private/internal hosts in `prod` unless allowlisted via `allowed_upstream_hosts`.
- **4-Pillar Observability, Value Proof & Horizontal Scaling**:
  - **Structured JSON Event Logging** (`log_format="json"`) correlated by `request_id`, `tenant_id`, `user_sub`, `profile`, and OTel `trace_id`/`span_id` across `mcp.request.completed`, `auth.denied`, `auth.obo.exchanged`, `policy.denied`, `abac.filtered`, `redaction.applied`, `grounding.blocked`, `sandbox.executed`, and `upstream.failed` (with sensitive key redaction).
  - **End-to-End OTel Tracing + W3C `traceparent`** propagation across `/mcp`, discovery, OBO token exchange, Monty sandbox execution, and downstream HTTP calls, with optional OTLP export (`mcpcodeaway[otlp]`, `GATEWAY_OTLP_ENDPOINT`).
  - **Prometheus `/metrics` & Executive ROI API (`GET /audit/value`)**: Exposes counters/histograms (`gateway_mcp_requests_total`, `gateway_tokens_saved_total`, `gateway_abac_rows_filtered_total`, `gateway_redactions_total`, `gateway_turns_avoided_total`, `gateway_execution_duration_seconds`) and computes estimated LLM USD saved (`cost_per_1m_input_tokens_usd`), context-overflow preventions, OBO cache hit rates, ABAC/PII governance counts, and classic-to-codemode migration recommendations.
  - **Multi-Replica Persistence, Redis Sessions & Load Balancing**: Supports `PostgresAuditStore` (`mcpcodeaway[postgres]`, `GATEWAY_POSTGRES_DSN`), Redis-shared `Mcp-Session-Id` state (`stateful_session: true`, `mcp_session_ttl_seconds`), Redis `catalog_epoch` synchronization across replicas (`POST /admin/catalog/refresh`), zero-downtime comma-separated multi-key secret rotation (`sandbox_worker_secret`, `provenance_signing_secret`, `admin_session_secret`), audit retention pruning (`POST /admin/audit/prune`, `audit_retention_days`), remote `sandbox-worker` pods, and optional cloud-agnostic Nginx reverse proxy ([deploy/nginx/nginx.conf](deploy/nginx/nginx.conf) via `docker compose --profile prod up` / `--profile scaled up`).
  - **Dual-Mode Admin Auth & Responsive `@ui-sdk/vue` Control Plane**: Secures the responsive Nuxt 4 + `@ui-sdk/vue` Admin Console (`web/`, served at `/dashboard` in `gateway-ui` / `gateway-demo` or hosted standalone with `NUXT_PUBLIC_API_BASE` + `GATEWAY_CORS_ALLOWED_ORIGINS`) via OIDC Authorization Code + PKCE (`HttpOnly` HMAC-signed session cookie) or Dev Persona / Bearer exchange, exposing live gateway & worker CPU/RAM telemetry (`GET /admin/telemetry/runtime`) and company-group, client-group, user, and model cost attribution (`GET /audit/analytics`).
  - **3-Layer Mode Selection & A/B Engine**: Combines deterministic algorithmic `?execution=auto` scoring, in-wizard Onboarding A/B Testing (`POST /admin/upstreams/ab-test` with instant replay and optional Admin/Dev OpenAI-compatible LLM runs), and per-upstream live `ab_experiment` traffic splits (`GET /admin/ab/experiments`).

## Quickstart (Zero External Dependencies)

```bash
# 1. Install dependencies (Python 3.12+ + uv)
uv sync --extra dev

# 2. Start mock enterprise backends (Snowflake MCP, EU/US Billing OpenAPI, Ops Ticketing MCP)
uv run uvicorn examples.mock_backends:app --port 8001

# 3. Start the gateway in another terminal
uv run uvicorn gateway.server:app --port 8000
```

Open `http://127.0.0.1:8000/dashboard` for the interactive side-by-side playground, HITL onboarding wizard, and agent prompt exporter.

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

## Proof of Performance (Measured Results)

### 1. Catalog Scaling (`tools/list` Schema Bloat vs. Code Mode)

From [benchmarks/results.json](benchmarks/results.json): Code Mode keeps its MCP surface constant at **463 tokens** (3 meta-tools) regardless of catalog size:

| Total Tools | Classic Surface Tokens | Code Mode Surface Tokens | Token Reduction |
|---:|---:|---:|---:|
| 20 | 2,030 | 463 | **77.19%** |
| 50 | 5,073 | 463 | **90.87%** |
| 150 | 15,218 | 463 | **96.96%** |
| 300 | 30,436 | 463 | **98.48%** |
| 500 | 50,726 | 463 | **99.09%** |

### 2. Offline Enterprise Workloads (4-Backend Finance & Ops Catalog)

From [benchmarks/results.json](benchmarks/results.json):

| Workload | Classic Tokens (Turns) | Code Mode Tokens (Turns) | Savings | Notes |
|---|---:|---:|---:|---|
| `reconcile_overdue` | 6,569 (4) | 1,870 (2) | **71.53%** | Cross-system invoice vs. Snowflake join |
| `reconcile_bulk_200` | 27,365 (4) | 1,881 (2) | **93.13%** | `93,593` raw bytes -> `780` returned bytes |
| `trial_balance_audit` | 7,385 (5) | 1,908 (2) | **74.16%** | Multi-step Snowflake + ticket correlation |
| `single_lookup` | 2,915 (2) | 1,878 (2) | **35.57%** | Adaptive router: `classic_or_tie` |

### 3. Discovery Backend Accuracy (160 Tools, 20 Queries)

From [benchmarks/discovery_results.json](benchmarks/discovery_results.json) on live LM Studio (`laya` + `text-embedding-qwen3-embedding-0.6b`, `0.0%` fallback rate):

| Backend | Recall@5 | Semantic Hit | Avg Latency |
|---|---:|---:|---:|
| `keyword` | 0.700 | 0.400 | 0.233 ms |
| `bm25` | 0.800 | 0.600 | 1.559 ms |
| `embedding` | **0.925** | **0.850** | 129.591 ms |
| `system1` | 0.675 | — | — |
| `hybrid_system1` | 0.825 | 0.650 | 144.353 ms |

### 4. Live SLM Evaluation (`qwen/qwen3.5-9b`, 32,768 Context, `reasoning_effort=none`)

- **200-Row Overflow Prevention** ([benchmarks/matrix_2x2_results.json](benchmarks/matrix_2x2_results.json)): `bulk_200` crashed **all 4** Classic runs (`HTTP 400 exceed_context_size_error`: `37,710–43,441 > 32,768` tokens), while both Code Mode runs completed (`65,450` / `68,288` tokens, `grounded=True`).
- **Typed `outputSchema` Stubs** ([benchmarks/typed_stubs_results.json](benchmarks/typed_stubs_results.json)):
  - `codemode_upfront`: completed all 4 workloads in **2 turns (`0` retries)** (`3,808` / `10,865` / `10,579` / `9,936` tokens; `3/4` accurate).
  - Deferred `codemode`: completed all 4 workloads in **3 turns (`0` retries, `4/4` accurate)** (`6,282` / `7,283` / `9,231` / `20,071` tokens; up to **9.0x** fewer tokens).

### 5. Concurrent Multi-User Load (30 Requests, 10 Users, Concurrency 15)

From [benchmarks/load_results.json](benchmarks/load_results.json):

| Mode | Wall Time | Throughput | p50 / p95 Latency | Success Rate | Delegated Subcalls / Cache Hit Rate |
|---|---:|---:|---:|---:|---:|
| `traditional` | 1.107 s | 27.1 rps | 7.568 / 100.692 ms | 100.0% | 90 subcalls / 66.67% (`60` hits) |
| `codemode` | 1.127 s | 26.63 rps | 475.03 / 1019.045 ms | 100.0% | 90 subcalls / 66.67% (`60` hits) |

### 6. Live External MCP Client Trial (`POST /mcp` over HTTP)

Measured by an external HTTP subagent client ([benchmarks/subagent_mcp_trial.json](benchmarks/subagent_mcp_trial.json)) against live `examples.mock_backends` (`:8001`) + `gateway.server` (`:8000`, `startup_warnings=[]`):

- **Bulk Join (`reconcile_bulk_200`, 200 rows):** Classic `4` RT (`199,817 B`, `49,954` est. tokens, `38.55 ms`) vs Code Mode `3` RT (`11,622 B`, `2,906` est. tokens, `32.52 ms`, HMAC `provenance_verified=true`) -> **94.18% byte & token reduction**.
- **Overdue Reconciliation (`reconcile_overdue`):** `14,193 B` -> `11,336 B` (**20.13%** byte / **20.12%** token savings; `4` -> `3` RT).
- **Small Single-Call Audit (`trial_balance_audit`):** `6,925 B` -> `6,799 B` (**1.82%** byte / **1.79%** token savings).
- **Routing & Policy Enforcement:** `execution=auto` emitted `X-Gateway-Mode-Decision: auto->codemode (score=100.0): Avg payload 17020 bytes risks prompt overflow`; EU/US OBO RLS returned `0` cross-region leaks (`5` rows each); `support_agent` RBAC denied `snowflake_ledger` (`-32003`); `require_grounding=true` blocked fabricated `CUST-9999` (`UngroundedResultError`).

## Vendor-Agnostic Integrations & Scaling

- **Enterprise IdP & MCP Client Examples**: See [examples/integrations/](examples/integrations/) (`entra_copilot_studio`, `keycloak_oidc`, `mcp_clients`).
- **Horizontal Scaling & Barebones Production**: Build the barebones `gateway` image target (`docker build --target gateway .`) or run `docker compose --profile prod up` / `--profile scaled up` (using [docker-compose.yml](docker-compose.yml)) for multi-replica stateless gateways, Postgres audit persistence (`GATEWAY_POSTGRES_DSN`), Redis caching (`GATEWAY_REDIS_URL`), and egress-isolated `sandbox-worker` pods authenticated with `GATEWAY_SANDBOX_WORKER_SECRET` and `GATEWAY_PROVENANCE_SIGNING_SECRET`. See [docs/cloud_scalability.md](docs/cloud_scalability.md).

## CI/CD, Security QA & Pre-CI Gates

- **GitHub Actions CI/CD (SHA-pinned, least-privilege):**
  - [backend-ci.yml](.github/workflows/backend-ci.yml): `scripts/agent_preflight.py`, Ruff format & lint, full `pytest -v`, and multi-target Docker builds (`gateway`, `sandbox-worker`, `gateway-ui`).
  - [frontend-ci.yml](.github/workflows/frontend-ci.yml): `npm ci`, `nuxt prepare`, `npm run generate`, and live `/dashboard/` + `_nuxt/` asset verification.
  - [qa-ci.yml](.github/workflows/qa-ci.yml): Golden E2E flow suite + locked offline benchmark verification (`--verify-thresholds`).
  - [security-ci.yml](.github/workflows/security-ci.yml) + [dependabot.yml](.github/dependabot.yml): CodeQL (`python`, `javascript-typescript`, `actions`), Dependency Review, Zizmor workflow audit, pinned Gitleaks (`.gitleaks.toml`), and scheduled `npm`/`uv` audits.
- **Golden Flows & Locked Benchmarks:** Deterministic end-to-end policy/grounding flows live in [tests/golden/flows.json](tests/golden/flows.json) ([tests/test_golden_flows.py](tests/test_golden_flows.py)), and minimum/maximum benchmark bounds live in [benchmarks/thresholds.json](benchmarks/thresholds.json) ([tests/test_benchmark_regression.py](tests/test_benchmark_regression.py)).
- **Agent Navigation & Local Gates:** Follow [AGENTS.md](AGENTS.md) ([CLAUDE.md](CLAUDE.md)) for two-tier `ripwire` + `graphify` navigation, install git hooks via `pre-commit` (`.pre-commit-config.yaml`) or wire `hooks/agent-post-edit.sh`, and run `./scripts/pre-ci.sh`.

## Verification

```bash
./scripts/pre-ci.sh --fast   # agent preflight, ruff, golden flows & regression tests
./scripts/pre-ci.sh --full   # full pytest, Nuxt generate & --verify-thresholds (add --docker for images)
```
