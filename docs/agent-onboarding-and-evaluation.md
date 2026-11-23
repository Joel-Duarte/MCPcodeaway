# Agent-Driven Onboarding, Mock Evaluation & Golden Testing

MCPcodeaway uses an **AI-agent-assisted onboarding pipeline** (`Prompt -> Clarify -> Validate -> Simulate/Preview -> Apply`) so administrators do not need to author raw `IntegrationDefinition`, RBAC, or MCP Apps UI manifests by hand.

## 1. End-to-End Agent Onboarding Workflow

1. **Choose Target (`POST /admin/onboarding/prompt-pack`):**
   - On `/dashboard/onboarding` (`Step 1`), the administrator picks either an existing registered upstream or defines a new REST/OpenAPI or MCP server (`target_upstream_name`, URL, OpenAPI/Docs URL).
   - `GET /admin/onboarding/prompt-pack` generates a structured prompt containing the `OnboardingBundle` and `IntegrationDefinition` JSON Schemas plus any live discovered tools and workflow candidates.

2. **Interactive Clarification (Before JSON Generation):**
   - The prompt instructs the external/frontier agent to inspect the OpenAPI/MCP schema first and **ask concise clarifying questions** before emitting JSON:
     - Downstream auth (`delegated_token` OBO audience/scopes, `user_oauth`, `api_key`, or `none`) and RBAC role/group allowlists.
     - Which mutating endpoints require human confirmation (`tool_effects` + matching `tool_approval_policies`).
     - Which multi-step workflows matter most and their estimated human baseline minutes (`manual_baseline_minutes`).
     - Which lookup/metrics endpoints should attach constrained MCP Apps UI views (`mcpcodeaway-ui/1`).

3. **Strict Bundle Validation & Live Pre-Apply Preview (`POST /admin/onboarding/bundle/validate`):**
   - `validate_onboarding_bundle_payload()` validates:
     - SSRF safety on `upstream.url`, `openapi_spec_url`, and `health_check.url`.
     - Exact policy alignment (`tool_effects` `"read"` -> `"none"`, `"write"` -> `"requester_confirmation"`, `"high_impact"` -> `"manager_approval"` in `tool_approval_policies`).
     - `IntegrationDefinition` rules (`workflows`, `enabled`, `intent_examples`, RFC 6901 JSON Pointers, typed output schema warnings, and `experiences` `mcpcodeaway-ui/1` structure).
   - Every proposed `ui://` experience is pre-rendered via `POST /admin/mcp-ui/preview` (`<iframe sandbox="allow-scripts">`) and can be exercised interactively via `POST /admin/mcp-ui/simulate` (`ui/initialize` + `tools/call` `postMessage` bridge) before publishing.

4. **Atomic Apply & Publish (`POST /admin/onboarding/bundle/apply`):**
   - Registers/updates the upstream (`save_upstream`), inspects schemas into the catalog, computes live `schema_sha256` `tool_refs`, saves a new `IntegrationDefinition` version, and publishes it.

---

## 2. Golden Mock API & E2E Golden Test

To continuously test realistic agent-driven onboarding (including IDP OBO auth and UI delivery), the repository ships a **Global Ticketing System** golden mock and E2E test:

- **Golden Mock Service ([`examples/mock_backends.py`](../examples/mock_backends.py)):**
  - Mounted at `/ticketing` (`http://mock-backends:8001/ticketing`, OpenAPI 3.1 at `/ticketing/openapi.json`).
  - Exposes **18 typed endpoints across 15 paths** (queues, ticket search/CRUD, assignment, escalation, resolution, close/reopen, comments, SLA status, watchers, tags, approvals, and regional metrics across `EU`, `US`, `APAC`, `LATAM`).
  - Uses `_DownstreamTokenVerifier` against the gateway's dev OIDC JWKS (`HTTPBearer`, `expected_audience="api://ticketing-system"`, read/write/approver roles) so unauthenticated calls fail with `401`.
  - Branded/typed Pydantic `response_model` definitions provide deterministic OpenAPI output schemas for `IntegrationDefinition` result predicates and Code Mode execution.

- **Golden Fixture & E2E Test ([`tests/test_golden_agent_onboarding.py`](../tests/test_golden_agent_onboarding.py) & [`tests/golden/ticketing_onboarding_bundle.json`](../tests/golden/ticketing_onboarding_bundle.json)):**
  - Asserts unauthenticated `/ticketing` rejection (`401`) and `422` failure on bundles omitting `tool_approval_policies`.
  - Runs `prompt-pack` -> `/admin/upstreams/inspect` -> `bundle/validate` -> `/admin/mcp-ui/preview` -> `bundle/apply`.
  - Verifies unauthorized roles see `0` `global_ticketing__*` tools, while authorized callers exchange real IDP OBO JWTs, list all `18` tools with `_meta.ui.resourceUri`, read rendered `ui://` HTML (`resources/read`), execute multi-step `triage_and_resolve_ticket` through requester confirmation elicitation to `completed_verified`, and execute multi-endpoint Code Mode compositions.

- **Pre-CI Integration:** Included in `./scripts/pre-ci.sh --fast` and `./scripts/pre-ci.sh --full` (`tests/test_golden_agent_onboarding.py`) and pre-seeded in `demo` mode (`config/upstreams.docker.yaml` + `src/gateway/demo_traffic.py`).
