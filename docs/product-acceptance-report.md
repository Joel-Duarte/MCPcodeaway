# MCPcodeaway Product Acceptance Report

**Review date:** 2026-11-22
**Scope:** Repository-backed product review of authentication, execution, discovery, policy, credentials, approvals, administration, UI, and operational journeys. This report distinguishes mock-backed and local acceptance evidence from behavior that still requires a customer deployment.

## Assessment

The repository implements and locally verifies the central gateway journeys: four execution modes, OIDC-backed caller and admin authentication, delegated and application credentials, user-managed credential APIs, discovery and invocation, authorization and effect checks at dispatch, approval gates, encrypted credential handling, and durable sensitive-write recovery. The full backend suite and fast/full pre-CI gates passed. Local PostgreSQL/process-crash and TCP/TLS acceptance also passed after correcting a test-runner DSN mismatch.

No confirmed P0 or P1 runtime defect was found in the reviewed paths. Personal credential management is intentionally API/integration-driven; the flow map was corrected to remove its inaccurate console-view claim. Manager-approval expiry and several persistence/race edge cases remain insufficiently covered by the approval golden suite. Real identity-provider configuration, provider-specific claims, and downstream contracts remain deployment acceptance work.

The change made during this review is limited to `scripts/local_acceptance.py`: process-crash checks now run separately with the configured isolated test-role DSN required by that suite.

## Product capability matrix

| Capability | Classification | Evidence and boundary |
|---|---|---|
| Classic direct tool listing/invocation | Implemented and verified | `tests/test_gateway_modes_e2e.py`, discovery and TCP/TLS acceptance; direct calls pass through the host dispatcher. |
| Code Mode catalog, multi-tool execution, scoped authorization, and failures | Implemented and verified | `tests/test_gateway_modes_e2e.py`, `tests/test_discovery_system1.py`, `tests/test_sandbox_security.py`; tool calls are reauthorized by the host. |
| Hybrid direct and programmatic paths | Implemented and verified | Mode E2E and TCP/TLS cases verify direct writes and reject protected writes through Code Mode. |
| Auto assignment, experiment eligibility, preferences, and adaptive routing | Implemented and verified | `tests/test_adaptive_execution.py` covers deterministic assignment, explicit-mode precedence, per-tool read-only eligibility, quality floor, and tenant/viewer boundaries. |
| Mode-invariant identity, authorization, effects, and approvals | Implemented and verified | Modes converge on shared discovery/dispatcher policy; golden approval and mode tests exercise protected writes. Tool visibility is not treated as authorization. |
| OIDC caller authentication, PKCE admin login, callback, session, logout, invalid/expired identity, tenant checks | Implemented and verified against local mocks | `tests/test_mock_oidc_provider.py`, `tests/test_oidc_golden_flows.py`, `tests/test_auth_spectrum_golden.py`, `tests/test_authorization_freshness.py`. External IdP claims remain unverified. |
| Delegated OBO/RFC 8693, audience/scope, claims challenge, Graph group lookup and failure | Implemented and verified against mocks | `tests/test_auth_obo.py`, auth-spectrum and authorization-freshness tests. Provider-specific behavior needs staging verification. |
| Application client credentials and shared API/bearer secrets | Implemented and verified against mocks | `tests/test_auth_spectrum_golden.py`, `tests/test_user_connect.py`, `tests/test_crypto_secrets.py`. Secret resolution is host-side. |
| User OAuth/API-key lifecycle and isolation | Implemented and verified at API level | `tests/test_user_connect.py`, auth-spectrum, and crypto tests cover connect, refresh failure, save/list/delete, and tenant/subject separation. PostgreSQL OAuth tokens are process-local by design. |
| Personal credential management portal | Deliberately deferred | Lifecycle is API/integration-driven through `/me/connections*` and `/auth/connect/*`; no identity-management portal is intended. |
| Upstream onboarding, schema inspection, catalog refresh, effect policy, access policy | Implemented and verified | `tests/test_upstream_onboarding.py`, `tests/test_openapi_generation_golden.py`, onboarding UI and Playwright source. Production mode validates operation/access classifications. |
| Duplicate names, partial discovery, stale catalog, upstream outage and refresh behavior | Implemented and verified in covered scenarios | Discovery, monitoring, recovery, golden-flow and onboarding suites exercise scoped catalog refresh, errors, and retry boundaries. Invocation uses current dispatch policy. |
| Requester confirmation, manager approval, self/group checks, replay prevention, Code Mode enforcement | Implemented and verified for golden journeys | `tests/test_approval_golden.py`; manager flow covers exact-action approval, self-approval and wrong-group denial. Durable write journal tests cover claim and ambiguous-outcome behavior. |
| Approval expiry, persistence failure, duplicate decision, cross-replica decision race | Implemented but insufficiently tested | State and route logic contain expiry/atomic claim checks. `docs/approval-gate-enhancements.md` explicitly lists several cases as follow-up; no approval golden case exercises expiry or independent-runtime approval races. |
| Admin RBAC, viewer read access, audit tenant isolation, settings and onboarding workflows | Implemented and verified at API level | `tests/test_security_hardening.py`, `tests/test_phase15_ops_and_compliance.py`, `tests/test_adaptive_execution.py`; page/API sources show settings and onboarding flows. |
| Frontend route build, unit tests, responsive checks in test source | Implemented; build/unit verified, browser E2E not executed | `npm run generate` generated 16 routes; six Vitest files passed. `web/tests/e2e/console.spec.ts` covers desktop/mobile/tablet but needs a running gateway and was not run in this review. |
| PostgreSQL migrations, roles, shared claims, process crash, TCP/TLS | Implemented and locally verified | Opt-in local acceptance results below. These do not establish managed-provider HA, restore, or real network behavior. |
| Multi-stage/quorum approvals, notifications, escalation, automatic workflow resume | Deliberately deferred | `docs/approval-gate-enhancements.md` limits MVP to a durable one-approver group decision; no requirement or evidence supports expanding that workflow in this review. |
| Real customer IdP, real upstream API contracts, deployment networking/SLOs | Deliberately deferred to deployment acceptance | Mocks verify gateway contracts; `docs/mvp-readiness.md` and `docs/oidc_deployment.md` state that provider/client/upstream-specific checks require staging. |

## End-to-end user journey coverage

| Journey | Coverage observed | Result |
|---|---|---|
| Admin signs in with OIDC, receives bounded session, and logs out | Mock provider discovery/JWKS/PKCE, callback/session and role denial tests | Verified against local provider; real issuer is deployment-owned. |
| Caller presents valid/invalid/expired or wrong-tenant token | Auth spectrum and authorization freshness suites | Verified; invalid identities and incomplete group resolution fail closed. |
| Caller gets a delegated token for a downstream audience | OBO success, scopes/audience, claims challenge, timeout/unavailable provider tests | Verified against mock providers and local TLS fixture. |
| Admin registers upstream, inspects schema, curates access/effects, publishes and refreshes catalog | Onboarding routes, generated OpenAPI tools, dynamic hot reload and production config tests | Verified at API and UI build level; Playwright runtime was not executed. |
| User discovers and invokes authorized tools in Classic, Code, Hybrid, and Auto | Mode E2E, adaptive assignment, discovery, TCP/TLS mode tests | Verified locally with representative mock services. |
| Unauthorized, stale, disabled, or out-of-scope tool call is rejected | Tenant scoping, server ceilings, authorization freshness, viewer and dispatch-policy tests | Verified in covered scenarios; discovery is not an invocation grant. |
| User links/updates/removes a personal API key or OAuth connection | `/auth/connect/*`, `/me/connections*`, user-connect and crypto tests | Backend API journey verified; no first-party identity/credential portal is intended. |
| Protected write is confirmed/approved and retried once | Requester and manager approval golden flows; sensitive-write journal tests | Verified for covered cases. Expiry and independent-runtime approval races need more acceptance coverage. |
| Upstream/Graph/database fails during a journey | Recovery, auth freshness, monitoring, audit failure, PostgreSQL interruption tests | Verified for locally simulated failures and opt-in local PostgreSQL cases; real provider outages not exercised. |
| Gateway process/replica changes while state is active | PostgreSQL process and crash acceptance; shared journal claim tests | Verified for write journal state. Per-user OAuth reconnect is expected after restart/replica change. |
| Admin uses analytics, traces, audit, approvals, settings and onboarding | Source pages, API RBAC tests, Vitest, static generation | Build and unit behavior verified. Visual and browser journeys await Playwright run against a live local gateway. |

## Prioritized defect register

### P2 — Approval expiry and durable race/failure cases lack end-to-end coverage

- **Evidence:** `tests/golden/approval_flows.json` has one manager approval journey. `docs/approval-gate-enhancements.md` names expiry, persistence failure, duplicate decision, stale policy, and two-replica cases as follow-up. State and route code implement expiry and atomic claims, but this review did not verify those cases end to end.
- **Affected journey:** Requester retries after a manager decision or an approval expires while a gateway replica is unavailable.
- **Actual vs expected:** The current tests prove exact-action approval and single-use behavior for the covered flow; the requested expiry/replica guarantees remain assumed from implementation for these uncovered cases.
- **Proposed correction:** Add deterministic expiry, failed persistence, duplicate decision, stale-policy, and independent-runtime race cases using the existing approval/PostgreSQL fixtures before broad production rollout.
- **Relevant tests:** `tests/test_approval_golden.py`, `tests/test_postgres_acceptance.py`, `tests/test_postgres_process_acceptance.py`.
- **Status:** Open test coverage gap; no runtime failure was reproduced.

### P2 — Local acceptance runner supplied the wrong DSN to crash tests (corrected)

- **Evidence:** Cross-process acceptance requires `gateway_limited`, while `tests/test_process_crash_acceptance.py` explicitly permits only the isolated `gateway_test` DSN. `scripts/local_acceptance.py` originally ran both suites with one DSN, so crash tests failed during fixture setup.
- **Affected journey:** Maintainer runs the documented PostgreSQL/process-crash acceptance command.
- **Actual vs expected:** The command failed before crash cases executed. The runner now executes crash tests in a separate pytest invocation using `MCP_TEST_POSTGRES_MIGRATION_DSN`.
- **Correction:** `scripts/local_acceptance.py` selects the test-role DSN for the crash suite only; cross-process tests continue using the runtime role.
- **Relevant test:** `uv run python scripts/local_acceptance.py --with-postgres --with-postgres-processes --with-process-crash --with-tcp-tls`.
- **Status:** Corrected and verified: first suite 107 passed; crash suite 5 passed.

No P0 or P1 defect was substantiated by the reviewed implementation or executed test suites.

## Implemented corrections

- Updated `scripts/local_acceptance.py` to run hard process-crash tests separately with the test-role DSN. This fixed a test harness defect without changing gateway runtime behavior or weakening test-role restrictions.

## Deliberate MVP limitations

- In PostgreSQL deployments, per-user OAuth access and refresh tokens are process-local. Users reconnect after restart or when requests move to another replica. PostgreSQL persists encrypted per-user API keys, but arbitrary OAuth tokens are intentionally not stored there.
- OIDC deployment supports one explicitly configured issuer and RS256 access-token validation. Endpoint discovery and multi-provider login selection are not implemented. A customer provider has not been registered or tested by this repository review.
- Manager approval is a single decision by one eligible member of configured groups. There is no notification delivery, escalation, quorum, manager-hierarchy resolution, automatic requester/workflow resume, or arbitrary Code Mode continuation.
- Approval records are not included in audit-run JSONL/S3 archive or audit-run pruning. The retention/export contract is an explicit customer decision in `docs/approval-gate-enhancements.md`.
- Without optional Redis, rate and concurrency limits and several observations are process-local; see `docs/mvp-readiness.md` and `docs/kubernetes-k1.md`.
- Mock/provider tests do not establish customer IdP claims, production egress/TLS, managed PostgreSQL failover/restore, downstream API idempotency, production SLOs, or compatibility with every MCP client.

## Exact verification commands and results

| Command | Result |
|---|---|
| `uv run pytest -q` | **Passed:** 220 passed, 26 skipped in 54.53s. Skips are opt-in PostgreSQL/TCP/process acceptance. |
| `npm run test:unit` in `web/` | **Passed:** 6 test files, 10 tests. |
| `NUXT_TELEMETRY_DISABLED=1 npm run generate` in `web/` | **Passed:** Nuxt static generation, 16 routes; no type/build warnings or errors. |
| `./scripts/pre-ci.sh --fast` | **Passed:** preflight, Ruff format/lint, golden flows and benchmark regression; 14 tests passed. |
| `./scripts/pre-ci.sh --full` | **Passed:** full pre-CI, including 220 backend tests passed / 26 skipped, frontend unit/build, and benchmark thresholds. |
| `uv run python scripts/local_acceptance.py --with-postgres --with-postgres-processes --with-process-crash --with-tcp-tls` | **Passed:** 107 tests passed, then 5 hard process-crash tests passed. Disposable local PostgreSQL was removed by the runner. |
| `uv run python scripts/local_acceptance.py --with-postgres-tls --with-postgres-processes --with-process-crash --with-tcp-tls` | **Partial:** first run reached tests (111 passed, 5 crash tests failed at setup due DSN mismatch; fixed afterward). Two reruns then hit `ConnectionResetError` during PostgreSQL migration startup. TLS-specific process/crash rerun remains outstanding; the first run's TLS test cases passed. |
| `git diff --check` | **Passed** after the acceptance-runner correction. |
| `web` Playwright E2E | **Not run:** requires a running gateway/browser target. E2E test source exists but was not treated as executed evidence. |

## Outstanding product-owner decisions

1. What customer retention and export policy applies to approval records, which are currently excluded from audit-run archive/prune?
2. Which production IdP/client, claim mapping, signing-token format, and delegated scopes are in the first pilot? Current support assumes one configured RS256 issuer and must be confirmed in staging.
3. Does a validated customer workflow require approval notifications or lifecycle automation beyond requester retry? Quorum, escalation, and automatic workflow resume remain separate product scope.
4. Which deployment owns shared rate/concurrency limiting and operational metrics when replicas scale beyond process-local limits?
