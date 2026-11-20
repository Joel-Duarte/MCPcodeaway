# MCPcodeaway — Vendor-Agnostic Code-Mode Universal MCP Gateway

**MCPcodeaway** is a stateless, vendor-agnostic MCP gateway that bridges enterprise Identity Providers (Entra ID, Okta, Keycloak, or local dev RS256 OIDC) to heterogeneous backends (`mcp_http`, `mcp_stdio`, and `openapi` / Swagger) while executing multi-step workflows inside an ephemeral, networkless **Monty (`pydantic-monty`)** Python sandbox.

## Why MCPcodeaway? (Platform Value Proposition)

- **Up to 93.7% Multi-Step Payload Token Savings & Adaptive Classic vs. Code Mode Routing:** Exposes a constant 3-tool deferred discovery + `execute_code` surface and executes joins/filters inside Monty before returning compact grounded results—while letting smaller models or single-tool lookups use Classic MCP when direct calls are cheaper. See [BENCHMARKS.md](BENCHMARKS.md) for live benchmarks comparing local `qwen/qwen3.5-9b` (`reasoning_effort=none`, concurrency 3 in LM Studio) against an Antigravity `flash` agent.
- **Zero-Trust Enterprise Security:** Per-user OIDC JWT validation, RFC 8693 / OBO delegated token exchange, host-side ABAC row filtering, PII/secret redaction, SSRF guards, and HMAC provenance grounding verification.
- **Sessionless MCP Design:** Every gateway and upstream MCP HTTP operation uses the current per-request protocol contract; the gateway never creates, stores, forwards, or depends on MCP session IDs, and upstream HTTP requests never retain or reuse cookies. Multi-step business operations pass explicit resource IDs through independently authorized tool calls.
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
- **MCP 2026-07-28 & Multi-Replica Persistence**: The gateway validates per-request protocol metadata plus Streamable HTTP routing headers on both gateway MCP endpoints, and requires modern discovery from HTTP MCP upstreams. Redis supports identity-scoped delegated tokens, rate limits, admin OIDC PKCE state, and catalog epoch synchronization; it stores no MCP session state. OpenAPI integrations that require cookie authentication are rejected. Supports `PostgresAuditStore` (`mcpcodeaway[postgres]`, `GATEWAY_POSTGRES_DSN`), secret rotation, audit retention, remote `sandbox-worker` replicas, and optional cloud-agnostic Nginx reverse proxy ([deploy/nginx/nginx.conf](deploy/nginx/nginx.conf) via `docker compose --profile prod up` / `--profile scaled up`).
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

Full breakdowns and raw telemetry live in [BENCHMARKS.md](BENCHMARKS.md) ([benchmarks/live_llm_results.json](benchmarks/live_llm_results.json) and [benchmarks/subagent_flash_results.json](benchmarks/subagent_flash_results.json)).

### Win #1: SOTA Discovery Saves 71%–99% of Initial Schema Tokens (In BOTH Modes)

Before an agent executes any tool, deferred discovery (`list_servers` + `search_tools` with `0.1–1.4 ms` `BM25`—or zero-round-trip `X-Intent` on initial connect) keeps the initial prompt surface constant at 3 meta-tools (`~463` tokens):

| Enterprise Catalog Size | Standard Upfront MCP (`tools/list`) | MCPcodeaway Deferred Surface (3 Meta-Tools) | Schema Tokens Saved |
| :--- | :---: | :---: | :---: |
| **16 tools** (4 servers) | ~1,600 tokens | **~463 tokens** | **71.1% saved** |
| **66 tools** (+50 synthetic) | ~5,073 tokens | **~463 tokens** | **90.9% saved** |
| **500 tools** (Enterprise scale) | ~50,726 tokens | **~463 tokens** | **99.1% saved** |

### Win #2: Classic MCP vs. Code Mode (Side-by-Side Across 6 Enterprise Workloads)

See [BENCHMARKS.md](BENCHMARKS.md#1-what-are-the-6-benchmark-workloads) for full explanations of each workload (**Snowflake** data warehouse joins, **Financial Compute** loops, **Sentry -> GitHub -> Jira -> Slack** 5-step incident triage chains, and point lookups).

#### A. Capable Coding Model (`flash` Subagent)

| Workload | Classic MCP | Code Mode (`execute_code`) | Winner & Why |
| :--- | :--- | :--- | :--- |
| **1. Bulk 200-Row Join**<br>*(`reconcile_bulk_200`: ~200 KB)* | `49,955 tok` · `23.8 ms` · `4` trips · ✅ | **`3,149 tok`** · `28.9 ms` · **`3` trips** · ✅ | **Code Mode (-93.7% tokens)** — filters 200 KB in Monty sandbox |
| **2. Medium 30-Row Join**<br>*(`reconcile_overdue`: 3 servers)* | `3,550 tok` · `112.8 ms` · `4` trips · ✅ | **`3,123 tok`** · **`25.7 ms`** · **`3` trips** · ✅ | **Code Mode (4.4x faster)** — parallel `asyncio.gather` in 1 script |
| **3. Batch Financial Compute**<br>*(`compute_batch_penalties`: 6 calls)* | `3,216 tok` · `63.5 ms` · `7` trips · ✅ | **`2,484 tok`** · **`28.9 ms`** · **`3` trips** · ✅ | **Code Mode (-22.8% tokens, 2.2x faster, -4 HTTP trips)** |
| **4. 5-Step Incident Triage Chain**<br>*(`mutating_ticket_workflow`: Sentry->GH->Ticket->Slack)* | **`1,756 tok`** · `49.6 ms` · `6` trips · ✅ | `2,856 tok` · **`17.7 ms`** · **`3` trips** · ✅ | **Code Mode (2.8x faster, -3 HTTP trips)** — chains 5 steps in 1 script |
| **5. Small 4-Call Fan-Out**<br>*(`trial_balance_audit`: `<9 KB`)* | **`2,235 tok`** · `15.1 ms` · `5` trips · ✅ | `2,866 tok` · **`13.6 ms`** · **`3` trips** · ✅ | **Tie** — Code Mode saves 2 HTTP trips; Classic saves 631 stub tokens |
| **6. Single Point Lookup**<br>*(`single_lookup`: 1 contract)* | **`1,619 tok`** · **`4.7 ms`** · **`2` trips** · ✅ | `2,383 tok` · `8.2 ms` · `3` trips · ✅ | **Classic (-32% tokens)** — direct JSON-RPC call needs no script |

#### B. Small Local Non-Reasoning Model (`qwen/qwen3.5-9b`, `reasoning_effort=none`, Concurrency 3)

| Workload | Classic MCP | Code Mode (`execute_code`) | Winner & Why |
| :--- | :--- | :--- | :--- |
| **1. Bulk 200-Row Join**<br>*(`reconcile_bulk_200`: ~200 KB)* | `39,230 tok` · `62.0 s` · **❌ FAIL** | **`7,336 tok`** · **`45.7 s`** · **✅ PASS** | **Code Mode (-81.3% tokens)** — prevents 9B context/math collapse |
| **2. 5-Step Incident Triage Chain**<br>*(`mutating_ticket_workflow`: Sentry->GH->Ticket->Slack)* | `9,268 tok` · `25.2 s` · **`6` turns** · ✅ | **`6,907 tok`** · **`23.3 s`** · **`3` turns** · ✅ | **Code Mode (-25.5% tokens, 6 -> 3 turns)** — avoids 5 sequential LLM turns |
| **3. Batch Financial Compute**<br>*(`compute_batch_penalties`: 6 calls)* | **`5,783 tok`** · `30.5 s` (`6` OBO hops) · ✅ | `5,908 tok` · **`29.4 s`** (**`1` OBO hop**) · ✅ | **Tie (`~5.8k` tok, `~29.5s`)** — 9B writes 0-retry loop & saves 5 OBO hops |
| **4. Medium 30-Row Join**<br>*(`reconcile_overdue`: 3 servers)* | **`10,968 tok`** · **`95.6 s`** · ✅ | `47,373 tok` · `179.3 s` · ✅ *(4 retries)* | **Classic (4.3x cheaper)** — non-reasoning 9B fumbles Python dict keys |
| **5. Small 4-Call Fan-Out**<br>*(`trial_balance_audit`: `<9 KB`)* | **`4,304 tok`** · **`16.2 s`** · ✅ | `10,323 tok` · `34.5 s` · ✅ | **Classic (2.4x cheaper & 2.1x faster)** |
| **6. Single Point Lookup**<br>*(`single_lookup`: 1 contract)* | **`2,065 tok`** · **`6.6 s`** · ✅ | `4,866 tok` · `10.0 s` · ✅ | **Classic (2.4x cheaper & 1.5x faster)** |

> **Programmatic Per-Tool & Per-Model Learning (`?execution=auto`):** MCPcodeaway does **not** hardcode model names. Agents identify their model via `X-Model-Name` (or `clientInfo.model`), and [`src/gateway/ab_testing.py`](src/gateway/ab_testing.py) learns from live A/B runs (`ok` quality, tokens saved, and `duration_ms` speed) per `(model_name, server__tool)` to auto-switch each model and tool to its winning mode (with optional admin overrides at `/admin/routing/models`).

## Vendor-Agnostic Integrations & Scaling

- **Enterprise IdP & MCP Client Examples**: See [examples/integrations/](examples/integrations/) (`entra_copilot_studio`, `keycloak_oidc`, `mcp_clients`). For production management-console OIDC registration, role assignment, gateway environment variables, and acceptance checks, follow [Company SSO and Control-Plane Access](docs/oidc_deployment.md).
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
