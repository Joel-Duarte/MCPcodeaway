# Production Readiness Plan

This plan establishes evidence for a production release candidate before choosing a deployment platform. Kubernetes is a later decision gate; this plan proves the application and container behavior first.

## Release goal

Demonstrate that authenticated requests remain isolated across tenants and users under concurrent execution, resource limits are bounded and observable, overload fails predictably, and replicas can share state and recover without leaking or duplicating work. Record a measured operating envelope and the remaining operational requirements before selecting Kubernetes or another managed container platform.

## Queue decision

**Do not add a durable work queue to the synchronous MCP request path now.** A client expects a tool result in the same request. Deferring that work changes the API contract and requires job IDs, polling or notifications, cancellation, idempotency, durable status, and retry rules. Retrying a mutating upstream call after a timeout can duplicate side effects.

Use bounded admission instead:

- Bound gateway in-flight MCP requests per process and return `503` with `Retry-After` on overload.
- Bound worker executions and Monty subprocesses; wait only for a short, configured interval before rejecting.
- Bound each execution's wall time, feed time, memory, output, tool-call count, callback concurrency, and per-upstream concurrency.
- Make overload and timeout outcomes visible in metrics and audit records without tenant IDs as Prometheus labels.
- Do not transparently retry mutating calls. For safe reads, retries need explicit upstream-specific policy and budgets.

Current code has per-process gateway/worker admission limits, a 100 ms remote-worker admission wait, and provisional bounds of 256 KiB of Code Mode source, 250 ms for Monty pool checkout, 15 seconds for a Monty worker request, 60 seconds for an end-to-end Code Mode execution, and eight concurrent tool callbacks per execution. Monty also retains its existing feed-time, memory, recursion, output, and tool-call limits. There is no durable queue. These are protective defaults, not measured production capacity settings; measure latency and saturation before tuning them. Add a separate durable job service only when the product explicitly needs long-running work that outlives an MCP request; then design tenant quotas, idempotency, cancellation, job authorization, leases, dead-letter handling, and audit history as a separate API.

## Phases and gates

### Phase 0 — Set service targets

Record expected tenants, concurrent users, peak MCP requests, maximum Code Mode fan-out, upstream rate limits, request latency objectives, and availability/recovery objectives. Set explicit maximum script duration, tool calls, output size, worker memory/CPU, and maximum queued wait. Do not claim capacity until these values are agreed and measured.

**Gate:** A workload profile and measurable latency/error/availability objectives are written down.

### Phase 1 — Prove identity, cache, and sessionless protocol isolation

The gateway and its HTTP MCP upstream adapter use only MCP `2026-07-28`. Every request is independently authorized and carries its protocol metadata and routing headers; the gateway never creates, stores, forwards, or accepts MCP session IDs. It does not perform an older initialize handshake or fall back to an older protocol. OpenAPI generation rejects cookie-based auth requirements, and the pooled HTTP adapter does not reuse response cookies.

Add deterministic fake IdP/upstream tests with unique tenant/user sentinels and barriers that force requests to overlap. Exercise:

- Same subject in two tenants; two subjects in one tenant; many interleaved requests routed across two independent gateway runtimes sharing cache state.
- Delegated-token cache keys and outbound token claims, audience, and scope.
- Current MCP metadata/header validation, unsupported-version rejection, discovery, tools/list, and tools/call; prove no protocol session ID or cookie appears on any hop.
- Outbound MCP HTTP framing and current-version advertisement from the server before tool discovery.
- Golden OpenAPI input-to-generated-tool mapping, including path/query/body mapping, URL encoding, per-request credentials, and rejection of cookie session requirements.
- Remote sandbox callbacks retaining the request's verified identity while other users execute concurrently.
- Audit and telemetry ownership for success, failure, cancellation, and timeout paths.
- Same user in separate client conversations: each request is authenticated and authorized independently; there is no implicit gateway conversation state. The gateway already records each execution under its own audit `run_id`. Only workflows that actually carry business state across calls need an explicit application resource ID in their arguments, authorized independently on every call; do not add generic conversation IDs for stateless calls.

Run deterministic tests repeatedly and with randomized scheduling. A pass must assert exact owners for every response, callback, token, and audit row; “no exception” is not sufficient.

**Gate:** No cross-tenant/user sentinel or credential appears in another principal's response, callback, audit data, or tenant-scoped telemetry. A legacy handshake, MCP session ID, or implicit cookie must be rejected or ignored safely.

### Phase 2 — Prove resource bounds and overload behavior

Test local and remote sandbox paths with a controlled fake backend and real Monty subprocesses:

- Verify maximum simultaneous requests, worker executions, subprocesses, and tool callbacks.
- Hold all worker slots, exceed capacity, and assert bounded wait plus a predictable overload response.
- Exercise infinite loops, slow host callbacks, repeated host calls, large source, large results, memory exhaustion, client disconnects, and worker crashes.
- Exercise the source-size cap, finite pool checkout timeout, host-side worker request timeout, end-to-end wall timeout, callback concurrency bound, and actual memory/output/feed limits.
- Verify every acquired slot is released on errors, cancellation, disconnect, and timeout.
- Verify that overload does not turn into an unbounded in-memory queue and that the gateway does not retry side-effecting calls.
- Check per-upstream concurrency/rate limits so one slow or rate-limited service cannot consume every worker callback.

**Gate:** Resource use and wait time remain bounded; callers get a documented timeout/overload result; the service recovers and accepts later work.

### Phase 3 — Prove shared-state and replica behavior

Use two gateway instances, shared Redis, and shared Postgres (first with local containers, then the chosen managed services):

- Verify OIDC PKCE/rate-limit/catalog state is correctly shared and scoped. Admin browser login sessions are separate control-plane sessions; they are not MCP protocol sessions.
- Route the same principal across replicas; restart one replica during requests and between requests.
- Verify no MCP protocol session or cookie state is needed to move a request between replicas.
- Test Redis/Postgres unavailability and recovery. Liveness remains process-level; readiness removes a replica only when a required dependency is unavailable.
- Move retention work to a singleton/lease before production to avoid every replica running the same job.
- Verify graceful termination/draining and that incomplete mutating calls are not silently replayed.

**Gate:** Replica changes and one-component failures cause bounded errors/recovery, never cross-scope state reuse or duplicated writes.

### Phase 4 — Prove network and operational controls

Run container-level checks rather than relying only on ASGI mocks:

- Worker can call only the gateway callback path; it cannot access public Internet, metadata, Redis/Postgres, or arbitrary gateway admin routes.
- Gateway egress is constrained to explicitly permitted upstreams or a controlled egress proxy.
- Confirm TLS at ingress, secret-manager injection and rotation, least-privilege workload identities, private metrics scraping, and no public worker port.
- Exercise backup and restore, schema migration, image rollback, and a failed deployment recovery.
- Confirm `/ready` checks required dependencies (including the remote worker when required for the advertised service) while `/health` remains cheap and does not cause dependency-outage restart storms.

**Gate:** A clean environment deploys with no development credentials/default secrets, and a documented restore/rollback exercise succeeds.

### Phase 5 — Measure capacity and set operating policy

Load-test gateway and workers independently, then together. Include normal traffic, maximum fan-out, slow upstreams, large responses, overloaded workers, shared-state latency, replica restart, and rolling deployment. Record throughput, p50/p95/p99 latency, memory/CPU, admission wait/rejections, worker saturation, upstream connection counts, and downstream rate-limit behavior.

Set requests/limits, replica floors, concurrency, timeouts, connection pools, and alert thresholds from the results. Use the results to decide whether traffic needs horizontal autoscaling and which signals should drive it.

**Gate:** The measured envelope meets Phase 0 targets with documented headroom and clear overload behavior.

## Kubernetes decision gate

Only after Phases 0–5 pass, decide whether to build Kubernetes manifests/Helm charts or use another managed container platform. Kubernetes is justified if the operating requirements need multi-zone placement, independent gateway/worker autoscaling, enforced NetworkPolicies, controlled rollouts, and the team can own cluster operations. It is not a substitute for the isolation, limits, shared-state, egress, backup, or recovery proofs above.

If Kubernetes is selected, separately test rendered manifests, security policies, probes, autoscaling signals, disruption/spread behavior, secret references, migration/rollback, worker isolation, and an end-to-end multi-pod isolation scenario in a real cluster. Do not call the Kubernetes target production-ready merely because pods become Ready.

## Exit checklist before deployment-platform work

- [ ] Workload and SLO targets agreed.
- [ ] Tenant/user/protocol-state concurrency isolation tests pass.
- [ ] Sandbox limits, pool waits, callback bounds, and overload tests pass.
- [ ] Multi-replica shared-state and restart tests pass.
- [ ] Network egress boundary is enforced and verified.
- [ ] Backup/restore and rollout/rollback exercises pass.
- [ ] Capacity results and configuration values are recorded.
- [ ] No unresolved P0/P1 isolation, authorization, or resource-bound issue remains.

## MVP approval and launch blockers

The repository now implements a narrow, durable approval path, but it does not
make a customer deployment production-ready by itself. The release owner still
needs to supply and verify these deployment-specific inputs:

- A single production OIDC issuer and API audience for the deployment, plus the
  customer's stable role and group claim mapping. The `tool_approver` role and
  groups such as `operations-approvers` are examples; they must exist in the
  chosen IdP and be tested with real tokens.
- One real upstream and reviewed `allowed_operations` / `tool_effects` policy.
  Production startup now rejects enabled upstreams that omit operation
  allowlists, effects, matching mutation declarations, or required gate policy.
- Workload profile, peak concurrency, response sizes, rate limits, SLOs, and
  recovery objectives. Gateway and sandbox workers must be measured and scaled
  independently; this repository contains no evidence-backed capacity claim.
- A named hosting target, TLS ingress, network egress enforcement, secret
  manager, shared Redis/Postgres services, backup and restore evidence, image
  promotion, and rollback procedure. SSRF application checks do not prove DNS
  pinning or an egress boundary.
- A pilot proving the chosen MCP client can render requester confirmation and
  retry after manager approval, plus expiry, policy-change, persistence outage,
  duplicate-decision, and two-replica recovery behavior.
- An approval-record retention and archive policy. Current audit archive/prune
  endpoints cover execution runs; they do not yet export or prune approval
  decisions. Decide the required retention, export format, and deletion rules
  before storing real customer action summaries in production.

These are release blockers, not reasons to add a speculative platform or
notification provider to the codebase before a customer selects one.

## Progress

### Local readiness lab (2026-11-21)

- Added `docker-compose.readiness.yml` with Keycloak 26.8.2 importing a local OIDC realm, two gateway replicas, and the existing shared local Postgres/Redis services. The overlay removes the mock backend's host port mapping so an unrelated host process on 8001 is left untouched.
- Added `scripts/readiness_smoke.py` to check real OIDC tokens, tenant and admin/viewer RBAC, a generated REST-backed CRM call, and a requester/approver/replay flow through the load balancer against shared approval state.
- Added an internal-network-only, unauthenticated readiness mutation fixture. It validates gateway approval enforcement and shared state; it is deliberately not evidence about upstream authentication or authorization.
- Added `benchmarks/live_gateway_benchmark.py` for repeatable traditional and Code Mode read-only HTTP workloads with latency, throughput, status/error, response-size, and no-retry reporting. Added operator instructions, drill procedure, and still-open SLO targets to `docs/local-readiness-lab.md`.
- Local Keycloak, CRM mock, tenant/RBAC, viewer-read-only, self-approval/wrong-group denial, and full requester/approver/replay smoke passed. Sequential live CRM mock runs passed at 100% success: traditional 200 requests / concurrency 10 / 20 warmups (p50 50.082 ms, p95 106.387 ms, p99 119.165 ms, 183.235 RPS); Code Mode 100 requests / concurrency 5 / 10 warmups (p50 33.682 ms, p95 49.324 ms, p99 51.155 ms, 143.964 RPS). A preceding run hit the default 120/minute per-user rate limit; the lab overlay now sets 6,000/minute to permit load measurements. These are short local mock baselines only, not capacity or SLO evidence.
- Shared-service drills: one gateway replica stopped with 100% smoke and 40/40 read-only requests; one worker stopped with 40/40 Code Mode requests; Redis loss returned `/ready` 503. Recovery first exposed stale Redis sockets causing 500s. Added Redis pool health checks and one safe retry for the idempotent rate-limit counter; after rebuilding, 40/40 calls passed following a Redis restart. Postgres loss returned 503 from readiness and MCP; after restart, readiness and approval smoke passed. The outage call failed before requester confirmation, so no mutation was authorized or dispatched.
- Remaining release blockers: customer IdP/group mapping, partner sandbox or contract-faithful upstream, agreed workload/SLO/RTO/RPO targets, production hosting and egress boundary, managed shared-service configuration, backup restore and image rollback proof, customer approval hierarchy and retention/export policy, and sustained/representative load evidence.

### Lean mock-only readiness path (2026-11-21)

- Replaced the optional Keycloak container in the local lab with the repository's OIDC provider in the existing mock-backends service. The local OIDC mock now exercises discovery, JWKS, authorization code + PKCE, signed access/ID tokens, nonce, userinfo, one-time codes, exact client/redirect checks, and role/group/tenant claims.
- Added CI protocol tests and an end-to-end OIDC login golden journey against the actual mock provider. The full readiness smoke remains an opt-in local container exercise.
- Added an optional local signing-key file shared by the mock provider and gateway replicas for local downstream delegation. The key path is only read for a non-production local exchange profile; the generated key is ignored by Git. Added a regression test for signing-key reuse.
- Moved the readiness upstream fixture under `deploy/readiness/`, mount it only in the local Compose overlay, and assert in CI that it is absent from the production gateway image. Existing pytest CI continues to check the auth/provider and approval fixtures without starting Docker or external IdPs.
- The latest smoke passed through the two-replica route against the repository OIDC mock, including group-based approval authorization and successful delegated calls to the mock APIs. A five-request benchmark sanity run passed at 100% (p50 11.438 ms, p95 45.774 ms, 55.954 RPS); this small run validates the harness only and is not a latency baseline or capacity claim.

### Phase 1 review against the current project

| Phase 1 item | Status | Project evidence / remaining work |
| --- | --- | --- |
| Same subject across tenants, multiple subjects per tenant, interleaved calls on two runtimes sharing cache | Covered | `tests/test_scalability.py::test_concurrent_multi_user_cache_isolation_and_rate_limit` asserts each identity echo under three reproducible, seeded launch schedules. |
| Delegated-token cache keys and outbound claims, audience, and scope | Covered | `src/gateway/auth.py` keys by tenant, subject, raw-token fingerprint, audience, and scope. `tests/test_auth_obo.py::test_delegated_token_exchanger_dev_cache_isolation_and_dialects` verifies separation and claims; the focused scalability test verifies downstream identity. |
| Current MCP metadata/headers, unsupported versions, discovery, tools/list, tools/call, and no protocol session/cookie | Covered | `tests/test_discovery_system1.py::test_modern_mcp_metadata_headers_and_discovery_contract` and `tests/test_profiles_and_observability.py::test_current_mcp_http_framing_is_sessionless_and_does_not_reuse_cookies`. |
| Outbound framing and server version advertisement before tool discovery | Covered | The HTTP adapter sends current version metadata and validates `supportedVersions` before `tools/list`; covered by the current framing test. |
| OpenAPI generated-tool mapping, path/query/body, URL encoding, per-request credentials, cookie-session rejection | Covered | `tests/test_openapi_generation_golden.py` covers generated tools, `/a%2Fb`, query/body, separate credentials, and cookie requirements. |
| Remote worker callbacks remain bound to their concurrent executions | Covered | `tests/test_scalability.py::test_remote_monty_worker_reverse_rpc_isolation` interleaves callbacks and asserts each execution receives its own sentinel. |
| Audit and tenant-telemetry ownership on success, failure, cancellation, and timeout | Covered | `tests/test_sandbox_security.py::test_audit_and_live_telemetry_keep_owner_for_all_execution_outcomes` checks exact tenant, subject, and run ID for each outcome and tenant-scoped summaries. Cancellation now records telemetry before propagating cancellation. |
| Same user in separate client conversations; explicit resource IDs when business state requires them | Conditional / no generic ID needed | Each call is independently identified, authorized, and recorded with a per-execution `run_id`. No stateful workflow contract was found that needs a cross-call resource ID. Add and authorize domain resource IDs only if a specific workflow carries business state between calls. |

- Phase 1 code and focused test evidence currently cover: same-subject/different-tenant and different-subject concurrency across two gateway runtimes sharing cache state; delegated-token cache separation and claims; current-version MCP request metadata, routing headers, discovery, tool listing and calls; rejection of unsupported versions and protocol session IDs; outbound HTTP framing/version negotiation with no cookie reuse; OpenAPI tool generation, path/query/body mapping, encoded path parameters, per-request credentials, and cookie-auth rejection; and concurrent remote-worker callback isolation. The outbound MCP HTTP adapter is on the pinned `2026-07-28` contract; the earlier note that it still used an older wire protocol is stale.
- Focused verification on 2026-11-21: 9 tests passed across protocol framing, discovery, OpenAPI generation, delegated-token cache isolation, remote callback isolation, multi-user/two-runtime concurrency, and cross-replica identity checks. This is a focused subset, not a full Phase 1 release gate or a full-suite run.
- Follow-up verification on 2026-11-21: 5 targeted test cases passed for audit/live-telemetry ownership, timeout cancellation, and the three seeded concurrency schedules. `./scripts/pre-ci.sh --fast` passed, including preflight, Ruff checks, and 11 golden/regression tests.
- Phase 1 remains open. The concurrent isolation test now varies reproducible launch timing across three recorded seeds, and audit/live-telemetry ownership is checked for success, failure, cancellation, and timeout. These focused checks do not replace randomized scheduling over the full suite. No generic conversation ID is needed for the current stateless call model: each execution has caller identity and an audit `run_id`; domain resource IDs should be introduced only for a concrete workflow that retains business state across calls.
- Phase 2 bounds have implementation and targeted evidence, but broader overload/resource-limit proofs remain open. Real shared Redis/Postgres across separate gateway instances, restart/failure behavior, network policy, load measurements, and recovery exercises are later-phase work. No Kubernetes manifests or deployment target work has started. The adversarial review document remains separate and uncommitted.
- Approval MVP implementation: SQLite and Postgres durable action records, tenant-scoped list/decision routes, group membership checks, requester/approver separation, policy version validation, atomic single-use execution claim, configurable TTL, and `/admin/approvals` console page. Onboarding starts all discovered operations disabled and requires explicit role/group access plus effect classification. Production static config now requires a complete operation allowlist and matching approval policy. The requester and manager golden journey file covers requester flows and the first approval/execute/retry flow; broader failure and multi-replica evidence remains open.
- Deployment readiness is still blocked on the customer IdP and actual group claims, real upstream pilot, workload/SLO targets, named hosting target, enforced network egress, managed shared services, backup/restore and rollback evidence, and measured capacity. Gateway and worker scaling budgets must be verified separately.
