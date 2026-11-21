# Local production-readiness lab

This lab exercises the gateway against a real local OIDC issuer (Keycloak), the repository's REST mock, and a dedicated approval fixture. It is useful for validating integration wiring and gathering a repeatable local baseline. It does not validate a customer IdP, partner REST contract, production capacity, availability, or an SLO.

## Start and stop

Prerequisites: Docker Compose v2, Python dependencies installed for this repository, and free host ports `8080` and `8081`. The mock backend is internal to the Compose network in this overlay; it does not claim host port `8001`.

```sh
docker compose -f docker-compose.yml -f docker-compose.readiness.yml --profile scaled config -q
docker compose -f docker-compose.yml -f docker-compose.readiness.yml --profile scaled up -d --scale gateway-scaled=2
python scripts/readiness_smoke.py
```

The gateway is at `http://localhost:8080`; Keycloak issuer is `http://localhost:8081/realms/mcp-readiness`. Use `readiness-admin`, `readiness-viewer`, `readiness-finance`, `readiness-ops`, `readiness-approver`, `readiness-unassigned`, or `readiness-other-tenant`. All fixture passwords are `readiness-local-only`. They and Keycloak `start-dev` are for this isolated lab only; never reuse them outside it. The UI uses the authorization-code flow with PKCE. The scripted checks use Keycloak's local-only password grant for repeatability.

`readiness_smoke.py` verifies OIDC claims and tenant/RBAC boundaries, read-only viewer behavior, a real generated REST-backed tool call, and the approval lifecycle. The requester confirms a high-impact action; self-approval and an approver outside the configured group are denied; an eligible approver sees and approves it; and the requester replays it. Every request goes through the load balancer; approval state is stored in the configured shared Postgres service. The unauthenticated approval fixture is reachable only on the internal Compose network and exists solely to exercise gateway approval controls; it is not an upstream security model.

For repeatable load runs, this overlay raises the per-user gateway rate limit to 6,000 requests/minute. The repository default is 120/minute. Record the effective setting with each run and agree the real per-user/per-tenant limits for the pilot; do not carry the lab override into production without that decision.

Stop only this Compose project when finished (named volumes remain):

```sh
docker compose -f docker-compose.yml -f docker-compose.readiness.yml --profile scaled down
```

## Workload measurements

Start with read-only workloads. Use a fresh JSON file for each run and keep the command, git revision, Compose configuration, and host details with the output:

```sh
python benchmarks/live_gateway_benchmark.py --mode traditional --requests 200 --concurrency 10 --warmup 20 --output /tmp/readiness-traditional-baseline.json
python benchmarks/live_gateway_benchmark.py --mode traditional --requests 1000 --concurrency 25 --warmup 50 --output /tmp/readiness-traditional-burst.json
python benchmarks/live_gateway_benchmark.py --mode codemode --requests 200 --concurrency 10 --warmup 20 --output /tmp/readiness-codemode-baseline.json
docker stats --no-stream
```

Repeat the baseline for at least 15 minutes for a soak run, recording a time series of p50/p95/p99 latency, success rate, RPS, CPU, memory, and restart counts. The harness records request counts, latency percentiles, throughput, HTTP/MCP errors, response bytes, and confirms there were no automatic retries. Run each mode and load level separately. It only measures the CRM mock route; measure Code Mode fan-out, remote worker saturation, and each real upstream with representative data and limits before capacity decisions. Avoid concurrent mutating load until the upstream's idempotency and retry behavior are known.

Initial local run on 2026-11-21 (two gateway replicas; CRM mock; local rate limit 6,000/minute):

| Mode | Requests / concurrency / warmup | Success | p50 / p95 / p99 | Throughput |
| --- | --- | --- | --- | --- |
| Traditional | 200 / 10 / 20 | 100% | 50.082 / 106.387 / 119.165 ms | 183.235 RPS |
| Code Mode | 100 / 5 / 10 | 100% | 33.682 / 49.324 / 51.155 ms | 143.964 RPS |

An earlier run using the repository's default 120/minute per-user limit returned rate-limit errors after previous requests had consumed the window. That behavior is why the load overlay has a higher test-only ceiling; it should be a separate product decision for customer deployments.

Write approved targets before judging the results:

| Target | Agreed value | Evidence / owner |
| --- | --- | --- |
| Peak concurrent users and MCP requests | TBD | Product / pilot owner |
| p95 and p99 latency by operation and mode | TBD | Product + SRE |
| Availability window and error budget | TBD | Service owner |
| Recovery time and data loss objective | TBD | Service owner |
| Worker CPU/memory and maximum fan-out | TBD | Platform + SRE |
| Upstream rate limits and timeout budget | TBD | Partner owner |

Local runs are a baseline for comparison only. They are not SLO evidence and must not be presented as a capacity claim.

## Recovery drills

Capture `/ready`, `/metrics` (with an authorized metrics identity), application logs, container health, and benchmark/smoke output before, during, and after each drill. Run one fault at a time. For mutating calls, do not retry automatically; reconcile the approval and upstream result before any manual replay.

```sh
COMPOSE='docker compose -f docker-compose.yml -f docker-compose.readiness.yml --profile scaled'
$COMPOSE ps
$COMPOSE logs --since=10m gateway-scaled sandbox-worker postgres redis
```

1. **Gateway replica loss:** identify one `gateway-scaled` container with `$COMPOSE ps -q gateway-scaled`, stop that container with `docker stop <container-id>`, and run the read-only smoke/benchmark against the load balancer. Confirm service continues on the other replica, then restore the stopped replica with `$COMPOSE up -d --scale gateway-scaled=2 gateway-scaled` and verify readiness. Record failed requests and recovery time.
2. **Worker loss:** stop one sandbox-worker container, then separately stop both only in a disposable run. Check that regular REST tool calls stay available, Code Mode degrades predictably, readiness/metrics expose the condition, and worker recovery does not replay a mutation. Restore with `$COMPOSE up -d --scale sandbox-worker=2 sandbox-worker`.
3. **Redis loss:** stop Redis, observe cache-dependent behavior and readiness, then restore it. Confirm authentication/authorization remains correct and cache recovery does not serve a token or identity across users/tenants.
4. **Postgres loss:** stop Postgres while no mutation is in progress, check that approval creation/decision fail closed and do not execute the action, then restore Postgres and verify pending records and new approvals. Do not infer backup recovery from a restart.
5. **Restore and rollback:** separately exercise a real backup restore and a previously built image rollback after selecting the target hosting platform, backup schedule, restore point, image registry, and operator procedure. This repo/local Compose run does not yet provide production backup evidence or a production rollback path.

The local lab runs two gateway containers behind Nginx with shared Postgres and Redis, so the approval smoke checks exercise requests through the shared route. Record container identity in logs/metrics if a deterministic per-replica trace is needed; the current smoke does not promise which replica handles an individual request.

Initial recovery drills on 2026-11-21: stopping one gateway replica left the smoke and a 40-request read-only run at 100% success (71.893 RPS, p95 84.775 ms); stopping one worker left a 40-request Code Mode run at 100% success (95.147 RPS, p95 58.070 ms). Redis loss made `/ready` return 503. The first recovery run exposed stale pooled Redis sockets, which caused HTTP 500s; the gateway now health-checks pooled connections and reconnects once for its idempotent rate-limit counter. After rebuilding the gateway, a 40-request read-only run following a Redis restart passed at 100% success (181.621 RPS, p95 38.663 ms). During Postgres loss, `/ready` returned 503 and MCP returned HTTP 503 with `Retry-After`; after Postgres restart, readiness and the approval smoke recovered. The approved mutation was never confirmed/executed during the outage.

## Approval and production gates

The local approval scenario validates the current single-group, single-decision manager approval path. It does not model an organizational manager hierarchy, multi-stage approval, quorum, delegation, escalations, or customer-specific segregation of duties. Before piloting, map the real approver groups and test requester/approver separation, wrong-group denial, expiration, policy change, Postgres outage, duplicate decisions, and exactly-once execution against a representative upstream. Define approval record retention/export and deletion before production use.

The REST mock is suitable for local protocol and gateway-flow testing. The upstream pilot still needs the partner's sandbox or a contract-faithful mock, credentials, scopes, rate limits, error semantics, and side-effect reconciliation. Production readiness also requires the selected IdP and actual claims, workload and SLO targets, hosting/ingress, egress policy, managed shared services, backup/restore proof, and rollback procedure.
