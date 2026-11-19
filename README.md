# MCPcodeaway — Vendor-Agnostic Code-Mode Universal MCP Gateway

**MCPcodeaway** is a stateless, vendor-agnostic MCP gateway that bridges enterprise Identity Providers (Entra ID, Okta, Keycloak, or local dev RS256 OIDC) to heterogeneous backends (`mcp_http`, `mcp_stdio`, and `openapi` / Swagger) while executing multi-step workflows inside an ephemeral, networkless **Monty (`pydantic-monty`)** Python sandbox.

## Why MCPcodeaway? (Platform Value Proposition)

- **Up to 93.7% Multi-Step Payload Token Savings & Adaptive Classic vs. Code Mode Routing:** Exposes a constant 3-tool deferred discovery + `execute_code` surface and executes joins/filters inside Monty before returning compact grounded results—while letting smaller models or single-tool lookups use Classic MCP when direct calls are cheaper. See [BENCHMARKS.md](BENCHMARKS.md) for live benchmarks comparing local `qwen/qwen3.5-9b` (`reasoning_effort=none`, concurrency 3 in LM Studio) against an Antigravity `flash` agent.
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

Full methodology and raw breakdowns live in [BENCHMARKS.md](BENCHMARKS.md) ([benchmarks/live_llm_results.json](benchmarks/live_llm_results.json) and [benchmarks/subagent_flash_results.json](benchmarks/subagent_flash_results.json)).

### 1. Local `qwen/qwen3.5-9b` (`reasoning_effort=none`, Concurrency 3 in LM Studio)

Measured via `benchmarks/live_llm_bench.py` (`--model qwen/qwen3.5-9b --reasoning-effort none --concurrency 3`) against the 4 fake enterprise MCP/OpenAPI servers:

| Workload | Mode | Turns | MCP Calls | Subcalls | Total Tokens | Wall Time (ms) | Retries | Grounded | Correct |
| :--- | :--- | ---: | ---: | ---: | ---: | ---: | ---: | :---: | :---: |
| `reconcile_overdue` (30-row join) | `traditional` | **3** | 12 | 12 | **10,968** | **95,636.15** | **0** | — | ✅ |
| `reconcile_overdue` (30-row join) | `codemode` | 10 | 9 | 7 | 47,373 | 179,253.67 | 4 | ✅ | ✅ |
| `reconcile_bulk_200` (200-row join) | `traditional` | 2 | 3 | 3 | 39,230 | 62,034.48 | 0 | — | ❌ |
| `reconcile_bulk_200` (200-row join) | `codemode` | **3** | **2** | **3** | **7,336 (-81.3%)** | **45,700.87 (-26.3%)** | **0** | ✅ | ✅ |
| `trial_balance_audit` (variance + lookup) | `traditional` | **3** | 4 | 4 | **4,304** | **16,213.88** | **0** | — | ✅ |
| `trial_balance_audit` (variance + lookup) | `codemode` | 3 | 4 | 4 | 10,323 | 34,515.09 | 0 | ✅ | ✅ |
| `single_lookup` (1 point read) | `traditional` | **2** | **1** | 1 | **2,065** | **6,590.28** | **0** | — | ✅ |
| `single_lookup` (1 point read) | `codemode` | 3 | 2 | 1 | 4,866 | 10,039.20 | 0 | ✅ | ✅ |

### 2. Frontier `flash` Agent (`antigravity_flash_mcp`) on the Same Fake MCP Servers

Measured over live HTTP JSON-RPC (`POST /mcp?mode=traditional` vs. `POST /mcp?mode=codemode&require_grounding=true`):

| Workload | Mode | HTTP Trips | MCP Calls | Subcalls | Wire Bytes | Est. Tokens | Latency (ms) | Grounded | Correct | Code Mode Delta |
| :--- | :--- | ---: | ---: | ---: | ---: | ---: | ---: | :---: | :---: | :--- |
| `reconcile_overdue` (30-row join) | `traditional` | 4 | 3 | 3 | 14,199 B | 3,550 | 112.81 | — | ✅ | — |
| `reconcile_overdue` (30-row join) | `codemode` | **3** | **2** | 3 | **12,491 B** | **3,123** | **25.73** | ✅ | ✅ | **-12.03% tokens / -1 trip (4.4x faster)** |
| `reconcile_bulk_200` (200-row join) | `traditional` | 4 | 3 | 3 | 199,817 B | 49,955 | 23.84 | — | ✅ | — |
| `reconcile_bulk_200` (200-row join) | `codemode` | **3** | **2** | 3 | **12,593 B** | **3,149** | **28.88** | ✅ | ✅ | **-93.70% tokens / -93.70% bytes / -1 trip** |
| `trial_balance_audit` (4-call fanout) | `traditional` | 5 | 4 | 4 | **8,937 B** | **2,235** | 15.08 | — | ✅ | — |
| `trial_balance_audit` (4-call fanout) | `codemode` | **3** | **2** | 4 | 11,464 B | 2,866 | **13.62** | ✅ | ✅ | **-2 HTTP trips (+28.23% stub tokens)** |
| `single_lookup` (1 point read) | `traditional` | **2** | **1** | 1 | **6,474 B** | **1,619** | **4.68** | — | ✅ | — |
| `single_lookup` (1 point read) | `codemode` | 3 | 2 | 1 | 9,529 B | 2,383 | 8.15 | ✅ | ✅ | **Classic wins (+47.19% tokens in codemode)** |

### 3. When to Use Classic MCP vs. Code Mode (Model & Workload Guide)

- **Smaller / Non-Reasoning Models (`qwen/qwen3.5-9b`, `reasoning_effort=none`) on Small Payloads:** Classic MCP (`traditional`) is more token-efficient on small catalogs and small responses (`single_lookup`: `2,065` vs `4,866` tokens; `trial_balance_audit`: `4,304` vs `10,323` tokens; `reconcile_overdue`: `10,968` vs `47,373` tokens where 9B took 4 code retries).
- **Large Payloads & Multi-System Joins (`reconcile_bulk_200`, 200 rows):** Classic MCP dumped `39,230` tokens of raw JSON into `qwen/qwen3.5-9b` and **failed accuracy (`correct=False`)**, whereas Code Mode filtered the 200 rows inside Monty in 1 script (`0` retries), succeeded (`correct=True`, `grounded=True`), and cut tokens by **81.3%** on `qwen/qwen3.5-9b` (`7,336` tokens) and **93.7%** on `flash` (`3,149` vs `49,955` tokens).
- **Stronger Coding Models (`flash`+):** Write zero-retry `asyncio.gather` Monty scripts immediately, winning on multi-backend joins (`reconcile_overdue` and `reconcile_bulk_200`) while Classic MCP remains ideal for 1-call point lookups (`single_lookup`) via `?execution=auto`.

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
