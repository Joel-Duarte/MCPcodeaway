# Kubernetes K2: hardening and multi-replica boundaries

K2 extends the opt-in K1 kind acceptance workflow. It is a bounded local validation milestone,
not a production-readiness claim. See [K1](kubernetes-k1.md) for the ordinary two-replica
acceptance commands.

The follow-on acceptance evidence is recorded in [security freshness acceptance](security-freshness-acceptance.md),
[managed PostgreSQL acceptance](managed-postgres-acceptance.md), and
[operations acceptance](operations-acceptance.md). Those documents supersede provisional contract
notes below where they include later code and test results; none claim managed-provider validation.

## Process-local state classification

| State | Contents and scope | Classification | Replica/restart effect and security boundary |
|---|---|---|---|
| Request rate windows | Sliding window keyed by tenant and subject; memory backend unless Redis is configured | Local by design; operational throttle | Each process counts independently and resets on restart. A caller can distribute load across replicas; this is not an authorization or approval control and is not a cluster-wide quota. The configured production profile currently requires the existing Redis cache backend for shared cache behavior. |
| Upstream rate windows and concurrency permits | Requests and active-call leases keyed by upstream | Local by design unless Redis is configured | Local windows/permits can multiply with replicas. These protect capacity, not policy. Do not use them as a global contractual quota. |
| OIDC/OBO and client-credential caches | Token values scoped to the relevant tenant/user/client, upstream, endpoint and scope | Security-sensitive, bounded cache | Values can differ between replicas and disappear on restart. Authorization and policy checks still run for each request; cache key scope and token expiry constrain reuse. A shared cache is optional, not required for authorization correctness. |
| OIDC JWKS cache | Issuer signing keys cached in the validator for 300 seconds | Security-sensitive, local key cache | Replicas refresh independently; an unknown key ID forces refresh, but a removed key already cached can remain usable for up to five minutes. This is an explicit key-revocation freshness window; configure issuer key rotation with overlap and account for the TTL. |
| Entra group membership cache | Resolved group IDs keyed by tenant and object ID, with configured TTL | Security-sensitive authorization input | Replicas can hold different data until refresh/expiry; a changed Graph membership can affect decisions for up to the configured TTL (default 300 seconds). Tenant/object scoping is present, but this is an explicit revocation-freshness limitation. K2 does not change the Entra OIDC/OBO path; production must set an acceptable revocation window and test against the corporate Graph setup. |
| External secret resolver cache | Resolved external secret values keyed by full reference, 60-second TTL | Security-sensitive, bounded cache | Secret rotation can take up to the cache TTL to reach a process. Restart clears the cache. Keep references scoped and rotate credentials through the deployment's secret delivery path. |
| Admin session manager | Stateless signed cookie containing identity and role/group claims, verified using configured session signing keys | Security-sensitive authorization input | No local session table exists. Replicas verify consistently only when they receive the same ordered signing-key set. Role/group revocation is not rechecked per request; a cookie can retain signed claims until its TTL expires or its key is rotated. Configure a suitable session TTL and rotation policy. |
| Policy evaluator | Settings and configured rules in the runtime; no decision-result cache | Security-sensitive, process configuration | Each request re-evaluates policy. Deployment config must be consistent across replicas; policy changes require a rollout or the existing persisted settings path. |
| Sensitive-write journal, approvals, settings, dynamic upstream records, adaptive evidence | Durable audit-store records and atomic claims | Shared consistency and durable state required | PostgreSQL is authoritative. Both replicas use the same database. Claim semantics prevent duplicate dispatch; unresolved operations remain blocked through restart. |
| Tool catalog and registry view | Discovered tools and compiled stubs in memory; registry configuration is loaded from persistent configuration/store | Correctness-relevant local view; refresh notification is cache-backed | Different replicas can temporarily expose different discovery results. Catalog visibility is not an authorization grant: current policy is checked again during invocation. Catalog epoch notifications are not durable and are not guaranteed without shared cache; restart triggers reload. |
| Upstream health monitor | Per-upstream probe status and consecutive-result counters | Local operational state | Replicas can disagree and independently open a local circuit, affecting availability but not authorization. Status resets on restart and is rebuilt from probes. |
| Experiment assignment | Deterministic hash of experiment ID, tenant, subject and request ID | Local computation; stable for identical assignment inputs | No durable assignment row is required for a repeated request with identical inputs. A new request ID can select a different arm. Auto routing uses measured evidence stored in PostgreSQL and only selects execution strategy; it cannot override authorization, credential selection, effect classification or approval. |
| Discovery samples | Recent in-memory samples used by local discovery diagnostics | Observability only | Samples differ by process and disappear on restart. They are not the adaptive evidence store and do not affect policy. |
| Metrics registry and process statistics | Per-process counters, histograms, gauges and runtime snapshots | Observability only | Values are per process and reset on restart. Scrape each pod or aggregate externally; no monitoring stack is added here. |
| Alert evaluator cooldown and dispatcher refresh locks | Recent webhook-send timestamps and per-upstream async locks | Local operational state | Alerts can repeat on another replica; duplicate refresh work can occur across replicas. Neither is an authorization or journal coordination mechanism. |
| Telemetry reports | Aggregations calculated from durable audit runs | Observability over durable data | The report calculation is in memory, but source audit rows are persisted in PostgreSQL. Metrics do not replace audit/journal durability. |
| Background tasks and HTTP clients | Per-process worker loops, connection pools, upstream clients and health snapshots | Local by design | Shutdown cancels tasks and closes pools/clients; the PostgreSQL journal remains authoritative for recoverable work. |

The rate limiter runs after `can_execute_tool` and approval checks. It is therefore an operational
per-caller throttle, not a security decision. Redis is not added by K2. Do not describe a local
limit as global or use it to enforce a tenant contract. The production profile's existing Redis
requirement is a pre-existing deployment constraint; this milestone does not provide Redis or a
shared rate-limit test.

## External PostgreSQL profile

`deploy/k8s/external/gateway.yaml` is an example two-replica Deployment and ClusterIP Service for
an external PostgreSQL deployment. Copy and tailor
`deploy/k8s/external/gateway-config.example.yaml`, supply a `gateway-runtime-secrets` Secret out
of band, replace the image and namespace, then apply. The Secret is expected to provide
environment keys consumed by the app, including `GATEWAY_POSTGRES_DSN`, OIDC client credentials,
session/provenance/dedupe/encryption keys, and the existing required `GATEWAY_REDIS_URL`. No
Secret object or real credential is committed. Set the PostgreSQL DSN's `sslmode=verify-full`;
the application now adds this mode if the connection string omits `sslmode`. An operator may
mount a private CA at `/run/postgres-ca/ca.crt` and use
`sslrootcert=/run/postgres-ca/ca.crt` in the DSN. An explicit `sslmode=disable` is supported for
the disposable local fixture only. Do not use `require`, which encrypts without verifying the
server identity. asyncpg's documented default is `prefer`, which may fall back to plaintext;
the gateway overrides that unsafe default for unspecified modes. [asyncpg SSL and pool API](https://magicstack.github.io/asyncpg/current/api/index.html)

Each gateway process uses a pool with minimum size 1 and maximum size 10 (20 maximum connections
for two replicas, plus rollout surge and other clients). Size the managed database connection
budget for the peak rollout and recovery population. Both replicas must share the same DSN and
database. The application initializes its existing schema at startup; use a database role that
can create and alter the gateway's application tables during initialization, then restrict it
according to your migration/operations policy. Readiness checks the PostgreSQL dependency. The
pool can acquire connections again after an outage, but K2 has not measured recovery time against
a managed database. The existing PostgreSQL integration and interruption tests remain opt-in.

The local K1 `postgres` Pod remains disposable acceptance infrastructure with `emptyDir`; it is
not installed by the external profile and is not publicly exposed. For an external service, use
the local acceptance PostgreSQL service only as a connectivity smoke target if the cluster can
reach it; actual TLS, certificate rotation, failover, permissions and recovery must be verified
against the managed service. Database backups, HA and restore procedures belong to that service.

Kubernetes Secrets are base64 encoded API objects, not encrypted by that encoding. Enable
encryption at rest for Secrets in the production cluster, restrict RBAC, and use the organization's
approved secret delivery and rotation process. No secrets are baked into the image or ConfigMap.
The gateway disables ServiceAccount token mounting and has no Kubernetes API permissions.

## Pod and network hardening

Both local and external gateway manifests set UID/GID 10001, RuntimeDefault seccomp, a read-only
root filesystem, no privilege escalation, all capabilities dropped, explicit `/tmp`, startup,
readiness and liveness probes, and a 45-second termination grace period. Requests/limits in the
external example are initial guardrails, not validated production sizing. The K1 PostgreSQL
permission init container remains local-only and adds only `CHOWN` beyond dropped capabilities.

The optional `deploy/k8s/local/ingress-tls.example.yaml` shows an Ingress using a Kubernetes TLS
Secret and HTTP-to-HTTPS redirect annotation. K2.1's opt-in traffic suite installs ingress-nginx
controller v1.15.1 and exercises an ephemeral CA/certificate through a local host port. The
optional local NetworkPolicy is exercised on a separate Calico-enforced kind cluster, not the
default K1 cluster. For managed PostgreSQL and real upstreams, the platform team must add only the
required egress destinations; if an ingress controller is in another namespace, its exact pod and
namespace selectors must also be allowed. Application SSRF validation, DNS pinning, peer
verification and redirect rejection remain the actual outbound URL controls.

The external example includes a PodDisruptionBudget (`minAvailable: 1`) and rolling update
`maxUnavailable: 0`, `maxSurge: 1`. A PDB governs voluntary evictions when honored; it cannot
prevent node failure or guarantee application availability. Direct deletion and abrupt failures
remain possible. The local acceptance checks currently test replacement and rolling restart, not
a node drain under PDB enforcement.

## K2.1 authorization freshness evidence

`tests/test_authorization_freshness.py` uses signed RSA tokens, deterministic JWKS and Graph
fixtures, and independently populated in-memory caches. It advances a monotonic test clock to
measure behavior at each configured expiration boundary; the local fixture represents provider
responses and does not emulate corporate identity infrastructure.

| Security input | Configured TTL | Effective maximum staleness | Failure behavior | Revocation behavior |
|---|---:|---|---|---|
| OIDC JWKS keys | Fixed 300 seconds per validator | A removed key can remain accepted until its local cache expires: at most 300 seconds after successful retrieval. An unknown `kid` triggers immediate refresh. Replicas expire independently. | Expired-cache refresh errors propagate and token validation fails closed. There is no stale-key fallback. | A removed key is rejected when a replica refreshes; tests show two replicas can temporarily differ until the older cache expires. |
| Entra transitive group membership | `entra_group_cache_ttl_seconds`, default 300 seconds, allowed range 30–3600 | Removed membership can remain cached until that replica's entry expires. Tests confirm the configured 30-second minimum and independent refresh. The configured upper bound is 3600 seconds. | Graph HTTP errors, malformed responses and timeout raise from the resolver. Token validation catches resolver errors, marks membership incomplete, and group-dependent policy denies access. A 401 challenge header exists at resolver level but is not forwarded by the validator's catch path. | Fresh Graph results replace cached membership at expiry. Stale membership is not returned after a refresh failure. |
| Stateless admin-session claims | Default 28,800 seconds (8 hours); capped by source-token `exp` | Roles and groups in a signed session can remain effective until session expiry, source-token expiry or signing-key removal. | Invalid signatures and expired sessions verify as absent. There is no server-side lookup or revocation table. | Role/group changes do not alter an issued cookie. Removing its signing key invalidates it immediately; retaining the old key for overlap preserves acceptance until expiry. |

The tests cover JWKS key rotation, unknown `kid`, expired-cache refresh, outage failure, and
different refresh times on two validator instances. They cover group membership changes, cache
hits and expiry on two resolver instances, `401`, `403`, `429`, `5xx`, timeout, malformed response,
tenant scoping and fail-closed policy decisions. Existing OBO tests cover an OBO claims challenge.
The validator currently suppresses group-resolver challenges while marking membership incomplete,
so the request is denied without a forwarded challenge. The tests advance virtual clocks to measure
configured bounds deterministically. This is two-instance cache simulation, not two live K8s pods;
it establishes convergence at the configured cache boundary but does not measure wall-clock
managed-provider propagation or per-pod HTTP routing.

The admin-session format has no immediate per-user revocation mechanism. Key rotation invalidates
every session signed only by removed keys; otherwise a privilege change waits for cookie expiry.
This milestone does not introduce a session store.

The current security acceptance extends these tests to two independently constructed
`GatewayRuntime` instances. It remains a cache simulation and does not claim live pod routing.

Optional local manifests can be schema-checked after `deploy` with
`./scripts/k8s-local.sh validate-optional`. K2.1 also applies the network policy on Calico and sends
real permitted and prohibited connections. The ingress suite uses ingress-nginx v1.15.1 and a
kind host-port route; its controller and certificate are temporary and are not part of the default
local deployment. This setup does not reproduce a managed ingress, cloud load balancer, DNS, WAF,
or certificate lifecycle.

`scripts/k8s_k21_external_postgres.py` ran two real gateway pods against a disposable TLS
PostgreSQL container outside the Deployment, attached through the kind Docker network. Both pods
performed accepted journal writes; direct SQL verified durable terminal rows and separate
correlations. The two-replica acceptance also retried a shared correlation across pods and verified
atomic exclusion. Stopping the external database made both pods unready (503); restoring it returned
both to Ready. The local Compose TLS suite separately verified `verify-full`, an untrusted CA, a
hostname mismatch, a bad password, non-superuser credentials, and that a role without schema
creation rights cannot initialize the store. Two independent store pools raced the journal claim
and produced exactly one winner. A prepared write remained blocked after a database interruption;
the test does not simulate an in-flight request at the exact moment the database is stopped.
Expired-certificate behavior was not exercised by this fixture. These are disposable local tests,
not a managed PostgreSQL failover or backup/restore test. The one-shot example connectivity pod
remains a separate operator diagnostic and was not used for this K2.1 run.

## Workload sizing and verification boundaries

Run the following on the opt-in Calico cluster. The script records JSON under `.k8s-local/`,
measures cgroup CPU/memory during each workload, captures startup/rollout samples, and probes
PostgreSQL latency/connections. The client runs 200
operations per read/discovery mode, 10 approved writes, and 200 concurrent Classic reads. Gateway
pods are restarted between read-mode samples to give each mode a fresh per-process upstream-rate
window. The deterministic local recovery-workbench upstream is used; its small in-process limit and
kind networking shape this test.

Observed on 2026-11-22 with image `mcpcodeaway:k1` (kind runtime image ID
`docker.io/library/import-2026-11-22@sha256:a1058abbc951b0b60e943b2fed3c13bcdf087137367b2d70c81583bd853b2926`),
2 desired replicas, requests `250m` CPU/`512Mi` memory, limits `2` CPU/`2Gi`, concurrency 16,
5-second cgroup sample windows:

| Workload | Mode | Completed | Throughput | p50 / p95 / p99 latency |
|---|---|---:|---:|---:|
| Discovery, 200 calls | Classic | 200/200 | 143.4 ops/s | 6.7 / 8.7 / 9.2 ms |
| Discovery, 200 calls | Code | 200/200 | 144.4 ops/s | 6.3 / 9.6 / 12.1 ms |
| Discovery, 200 calls | Hybrid | 200/200 | 125.7 ops/s | 7.1 / 9.6 / 13.6 ms |
| Discovery, 200 calls | Auto | 200/200 | 130.5 ops/s | 7.1 / 10.2 / 11.9 ms |
| Read-only invocation, 200 calls | Classic | 200/200 | 128.4 ops/s | 101.6 / 219.2 / 225.3 ms |
| Read-only invocation, 200 calls | Code | 200/200 | 133.3 ops/s | 120.7 / 182.3 / 309.6 ms |
| Read-only invocation, 200 calls | Hybrid | 200/200 | 121.1 ops/s | 119.4 / 241.4 / 258.7 ms |
| Read-only invocation, 200 calls | Auto | 200/200 | 137.5 ops/s | 121.2 / 187.8 / 203.8 ms |
| Concurrent read-only, 200 calls | Classic | 200/200 | 148.3 ops/s | 105.1 / 154.1 / 184.5 ms |
| Sensitive approved writes, 10 operations | Classic | 10/10 | 24.3 ops/s | 36.1 / 69.1 / 69.1 ms |
| PostgreSQL `SELECT 1`, 200 probes | N/A | 200/200 | N/A | 0.113 / 0.237 / 1.303 ms |

Idle pods used 31–33m average CPU and 82–92 MiB current memory, with 83–96 MiB sampled
peaks. The highest sampled workload memory was 140 MiB; sampled per-pod average CPU during read
invocations ranged 163–305m. Startup rollout took 17.6 seconds; a new pod averaged 558m CPU in
its short startup sample and used 135.5 MiB. PostgreSQL reported 18 current gateway-role sessions
at one probe, including transient rollout/pool activity. No container restarts were observed.
The workload supports keeping the current local request profile of 250m/512Mi with 2 CPU/2Gi
limits as a provisional starting point; the observed peak is well below those limits. Do not
interpret these short runs, two kind pods on one node, p95/p99 from 200 calls, or the local mock as
production capacity or statistically reliable tail latency. There was no managed upstream, node
failure, kernel OOM-event scrape, or external database failover. Auto did not outperform Classic
uniformly, and this evidence does not justify changing Auto's existing strategy.

K1's 64 MiB allocator OOM fixture still verifies Kubernetes enforcement only. The standalone
`scripts/k8s_resource_snapshot.py` remains useful for an explicitly chosen interval; the workload
runner adds request percentiles and a PostgreSQL probe, not kernel OOM event telemetry. `kubectl
top pods` is not required and kind has no Metrics API by default. The external example's 500m/1Gi
requests remain unmeasured guardrails. No production sizing claim is supported by a kind benchmark.

## K2.1 opt-in test prerequisites and commands

The default developer workflow does not create the extra cluster, ingress controller, fixtures,
TLS certificates, database, or temporary services. The local network/ingress/workload suites need
Docker, kind, kubectl, `uv`, a working container registry connection for the pinned Calico and
ingress-nginx manifests, and enough local CPU/memory for two clusters only if K1 is kept alive
while K2.1 runs. The scripts use the project-specific cluster name `kind-mcpcodeaway-k21` and
namespace `mcpcodeaway-k1`.

| Acceptance group | Command | External dependency or setup |
|---|---|---|
| PostgreSQL TLS and pool restart | `uv run python scripts/local_acceptance.py --with-postgres-tls` | Docker Compose; generates and removes local CA/server fixtures and disposable TLS PostgreSQL |
| Process acceptance, crash recovery and TCP/TLS | `uv run python scripts/local_acceptance.py --with-postgres-processes --with-process-crash --with-tcp-tls` | Docker Compose and local test binaries/fixtures |
| Combined TLS, process interruption, crash and TCP/TLS | `uv run python scripts/local_acceptance.py --with-postgres-tls --with-postgres-processes --with-process-crash --with-tcp-tls` | Docker Compose base plus TLS overlay; child fixtures inherit both Compose files and the TLS DSN |
| Authorization freshness | `uv run pytest -q tests/test_authorization_freshness.py` | Deterministic local HTTP fixtures; no identity provider |
| Calico cluster | `MCP_K8S_CLUSTER=mcpcodeaway-k21 ./scripts/k8s-local.sh create-netpol` | kind plus network access to pinned Calico manifest |
| Build, deploy and K1 acceptance | `MCP_K8S_CLUSTER=mcpcodeaway-k21 ./scripts/k8s-local.sh build`, then `deploy`, then `test` | Existing kind/Calico cluster; local image build |
| Ingress and policy traffic | `uv run python scripts/k8s_k21_traffic_acceptance.py` | Existing K21 cluster; script installs temporary ingress-nginx and probes, then removes them |
| External PostgreSQL and two pods | `uv run python scripts/k8s_k21_external_postgres.py` | Existing K21 cluster, Docker Compose, local generated certificates |
| Workload baseline | `uv run python scripts/k8s_workload_baseline.py --context kind-mcpcodeaway-k21 --namespace mcpcodeaway-k1 --requests 200 --concurrency 16 --duration 5` | Existing deployed two-replica K21 cluster and a disposable Python client pod |
| K21 cleanup | `MCP_K8S_CLUSTER=mcpcodeaway-k21 ./scripts/k8s-local.sh destroy` | Removes only the project-named cluster and state |

The ingress suite assumes ingress-nginx v1.15.1's local NodePort/host-port behavior. The Calico
suite uses Calico v3.33.0 with the K21 pod CIDR `10.244.0.0/16`. These controller/CNI versions and
local routing are acceptance assumptions, not recommendations for a managed cluster. The network
test demonstrates allowed DNS, gateway-to-PostgreSQL, gateway-to-required mock upstream, and
ingress-controller traffic; it records timeouts for unapproved egress, cross-namespace ingress,
and PostgreSQL-initiated gateway traffic. It does not prove every possible host route or replace
application SSRF controls.

K1 already covers four-mode smoke checks, shared journal behavior, replacement, rolling restart,
PostgreSQL startup outage/readiness and SIGTERM smoke behavior in its opt-in acceptance script.
It does not prove zero downtime, real managed-PostgreSQL recovery, in-flight sensitive-write
SIGTERM behavior in Kubernetes, NetworkPolicy enforcement, ingress TLS, or Entra OIDC/OBO.
`tests/test_postgres_acceptance.py`, `tests/test_postgres_process_acceptance.py`,
`tests/test_process_crash_acceptance.py`, and `tests/test_tcp_tls_acceptance.py` provide separate
opt-in evidence when their dependencies and environment are available.

Unresolved sensitive writes remain blocked and are not automatically replayed. Operators should
check the durable journal and upstream state before any manual action; do not change an unresolved
operation to retryable based only on a missing client response. Completed operations remain
deduplicated by correlation and claim state.

## Remaining production prerequisites

Use a managed PostgreSQL service with verified TLS, backup/restore and connection limits; satisfy
the existing Redis configuration requirement in the production settings; provision real OIDC/OBO
secrets and tenant/audience/endpoint settings; deliver and rotate Secrets securely; configure
ingress/certificates and enforceable NetworkPolicy in the target CNI; load-test against real
upstreams and database behavior; and define availability, alerting, incident response and
unresolved-write procedures. Those are external prerequisites, not claims established by local
kind.
