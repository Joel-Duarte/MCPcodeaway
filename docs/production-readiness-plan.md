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

### Phase 1 — Prove identity, cache, and protocol-state isolation

Add deterministic fake IdP/upstream tests with unique tenant/user sentinels and barriers that force requests to overlap. Exercise:

- Same subject in two tenants; two subjects in one tenant; many interleaved requests.
- Delegated-token cache keys and outbound token claims, audience, and scope.
- Current MCP protocol metadata/header validation, unsupported-version rejection, and request handling without a gateway-issued MCP session ID.
- Outbound MCP client interoperability. The gateway's client-facing `/mcp` surface now speaks 2026-07-28, but the upstream client manager still uses handshake-era 2025-03-26 framing for legacy/stateful calls. Add modern outbound requests and a tested, explicit fallback policy for upstream servers that do not support 2026-07-28 before claiming end-to-end current-protocol interoperability.
- Legacy stateful upstream session mappings across identities and gateway instances, including upstream session rejection/reinitialization and cache loss.
- Remote sandbox callbacks retaining the request's verified identity while other users execute concurrently.
- Audit and telemetry ownership for success, failure, cancellation, and timeout paths.
- Same user in separate conversations. MCP 2026-07-28 carries no conversation session ID; the gateway must not infer conversation state from a connection or protocol session. The legacy downstream mapping remains principal-scoped (`tenant_id:sub`), so an upstream that stores conversation-specific state in its legacy session may share that state across the same principal. Keep that adapter disabled unless the upstream's behavior is acceptable or it provides explicit application handles.

Run deterministic tests repeatedly and with randomized scheduling. A pass must assert exact owners for every response, downstream session, token, and audit row; “no exception” is not sufficient.

**Gate:** No cross-tenant/user sentinel or credential appears in another principal's response, callback, session, audit data, or tenant-scoped telemetry.

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

- Verify OIDC/session/rate-limit/catalog state is correctly shared and scoped.
- Route the same principal across replicas; restart one replica during requests and between requests.
- Verify stale worker or upstream sessions are rejected/recreated only within their owner scope.
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

## Progress

- Initial isolation evidence is in place: a mocked upstream forces overlapping requests for the same subject in different tenants; stateful upstream session mappings are checked for that identity pair; remote worker reverse-RPC callbacks are interleaved; and worker saturation is checked for bounded rejection. Provisional code-size, pool, worker, end-to-end, and callback bounds are implemented with targeted size/timeout/cancellation tests. The client-facing MCP endpoints advertise 2026-07-28 and validate per-request metadata and routing headers; tests cover discovery, private zero-TTL `tools/list` cache hints, unsupported versions, mismatched headers, notification rejection, origin rejection, and the legacy handshake fallback. The outbound upstream client remains on its earlier wire protocol and is an explicit follow-up before claiming end-to-end 2026 interoperability. The complete Python suite passes (76 tests), and `./scripts/pre-ci.sh --full` passes. A scaled Compose smoke run brought up two gateway replicas and two workers; both gateway replicas reported ready, and Postgres/Redis were reachable. Tenant-isolation under modern-protocol concurrency and cross-replica request execution remain required evidence before deployment readiness.
- This is a first slice of Phase 1 and Phase 2, not a release gate. Same-user conversation isolation, real shared Redis/Postgres across separate gateway instances, failure/restart behavior, network policy, broader resource-limit cases, load measurements, and recovery exercises remain open.
- No Kubernetes manifests or deployment target work has started. The adversarial review document remains separate and uncommitted.
