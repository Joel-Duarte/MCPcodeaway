# MVP readiness

This page describes the evidence we have for the current MVP and the shortest path to a pilot. It replaces the former local lab guide and production readiness plan, which repeated setup details and carried decisions that a customer deployment must make later.

## What the MVP proves

- The gateway accepts authenticated, stateless MCP requests and enforces tenant, role, and group policy.
- The repository's OIDC mock exercises discovery, authorization code with PKCE, signed tokens, JWKS validation, userinfo, scope checks, and replay/redirect failures. Golden tests also run the gateway login, callback, session, and access-denial flow against that mock.
- Mock REST and MCP services exercise generated tools, request/response handling, and approval gates without requiring vendor accounts or extra identity-provider containers.
- Golden caller flows include fail-closed invalid-token, unsupported-protocol, missing-argument, authorization, and grounding cases, alongside successful discovery and execution journeys.
- Golden recovery journeys exercise a transient read failure and retry, an exhausted read retry, a committed write with a lost response, and an upstream health circuit that blocks then recovers. Failure cases assert the typed result and that the gateway does not replay a write.
- Golden reconciliation journeys exercise an API-key protected mock that commits exactly once, loses a write response, confirms the result through a status endpoint, replays the same idempotency key, and rejects reuse with a different payload.
- Durable recovery tests exercise per-request MCP Tasks opt-in, legacy operation-status fallback, confirmed lost-response recovery, same-key replay with one side effect, mismatched-payload rejection, caller isolation, shared SQLite claims, and worker recovery after lease expiry. They use a deterministic mock API; they do not prove a specific production API's idempotency contract.
- Upstream controls are configured per integration: 15 seconds by default, 120 requests/minute, and 10 in-flight calls. Redis shares the sliding-window rate limit and leased concurrency permits across replicas; without Redis, those limits are local to each gateway process. Excess calls are rejected rather than queued. Safe read retries use bounded jitter and honor a numeric `Retry-After`; ambiguous writes are retried only when explicit recovery and upstream idempotency are configured.
- Downstream requests originate from gateway egress addresses, not the user's workstation. Delegated credentials can still represent the user, but IP allowlists and upstream rate limits see the gateway network.
- The optional local Compose lab exercises OIDC login, a REST-backed tool, multiple gateway replicas, shared Redis/Postgres, and a requester/approver flow.
- To enable write recovery for a pilot, provide persistent `GATEWAY_CREDENTIAL_ENCRYPTION_KEY` and stable `GATEWAY_TASK_DEDUPE_SECRET` values, use Postgres for production replicas, and configure an upstream API that documents and honors the selected idempotency key for at least the configured retention window. Delegated-token-only upstreams and generated Code Mode workflows are not eligible.
- The live benchmark can report request success, latency percentiles, throughput, and response size against the local CRM mock. Results describe that local setup only; they are not an SLO or a capacity promise.

The local identity provider, downstream services, and approval fixture are test doubles. They prove the gateway's implemented contracts and control flow. They do not prove a particular customer's IdP claims, an MCP client host's behavior, a partner API contract, production networking, or production operations. The current approval example has one approver group and one decision; it does not model a full manager hierarchy.

## Run the checks

The focused OIDC tests run in normal CI and need no containers:

```sh
uv run pytest tests/test_mock_oidc_provider.py tests/test_oidc_golden_flows.py tests/test_upstream_recovery_golden.py
```

The optional Compose lab adds shared-service and end-to-end evidence. It requires Docker Compose v2, OpenSSL, installed Python dependencies, and free ports 8080 and 8081. It is not part of normal app startup or the production image.

```sh
mkdir -p .readiness
openssl genpkey -algorithm RSA -pkeyopt rsa_keygen_bits:2048 -out .readiness/dev-oidc-private.pem
chmod 644 .readiness/dev-oidc-private.pem
docker compose -f docker-compose.yml -f docker-compose.readiness.yml --profile scaled config -q
docker compose -f docker-compose.yml -f docker-compose.readiness.yml --profile scaled up -d --build --scale gateway-scaled=2
uv run python scripts/readiness_smoke.py
```

Open `http://localhost:8080` for the gateway and `http://localhost:8081/oidc` for the local issuer. The smoke checks identity and access boundaries, a CRM tool call, API-key protected idempotent write/status recovery, requester confirmation, wrong-group and self-approval denial, an eligible approver, requester retry through the shared route, and a controlled CRM outage/recovery through the load balancer. It verifies that the gateway remains ready during an upstream-only outage. The mock failure-control endpoint is enabled only by this readiness overlay and is not a gateway API. The local signing key and mock control are for local use only.

With the lab running and LM Studio serving `qwen/qwen3.5-9b`, run `uv run python scripts/mcp_llm_spectrum.py`. This obtains finance and support identities through the mock IdP's PKCE flow, uses the MCP Python SDK session to list/call gateway tools, and asks the local model to retrieve the seeded Acme account through the mock CRM. It then verifies that support cannot see or call the finance tool. The adapter handles the gateway's current stateless request metadata and locally answers the SDK initialization handshake; `tools/list` and `tools/call` still traverse the gateway. This is real local integration evidence, not a claim of out-of-the-box compatibility with every standard MCP client.

The readiness OpenAPI fixture also exposes `idempotency_api` with `X-API-Key` authentication, a write endpoint accepting an idempotency key, a status read, and a side-effect counter. `tests/test_upstream_reconciliation_golden.py` calls it through the gateway and asserts ambiguous-write readback, safe replay, payload-conflict rejection, and the one-effect invariant.

To stop the lab:

```sh
docker compose -f docker-compose.yml -f docker-compose.readiness.yml --profile scaled down
```

For an observational CRM baseline, run `uv run python benchmarks/live_gateway_benchmark.py --mode traditional --requests 200 --concurrency 10 --warmup 20 --output /tmp/mvp-baseline.json`, then inspect `docker stats --no-stream`. Record the revision, host, and Compose settings with the output. Increase load gradually and watch latency, errors, CPU, memory, and worker saturation. There is no agreed workload or SLO gate for this MVP, so do not present a local mock result as a capacity claim.

## Current decisions and next steps

- Keep integrations as local mocks until a pilot selects a real IdP, MCP client host, and upstream. Add cases when a contract gap appears; do not add vendor containers or SDKs just to mirror a provider.
- Use a canary rollout for a customer pilot: start with one user, observe errors and resource use, then increase access in steps. Set deployment-specific traffic limits and objectives with the host team when the environment is known.
- For a canary, record the deployed revision and configuration, start with one user or a small allowlisted group, observe authentication/policy denials, upstream errors, latency, and resource use, and pause expansion or roll back if the agreed limits are exceeded. Expand only after the current step meets the customer-defined objectives; this mock-backed MVP does not prescribe numeric customer SLOs.
- Choose hosting, network/egress rules, secrets, and managed Redis/Postgres based on the customer's environment. The repository's Compose stack is a local exercise, not a deployment blueprint.
- Before customer use, verify the actual client's login and confirmation experience, customer claim mapping, upstream permissions and error behavior, and the customer's approver groups. For production operations, also rehearse backup/restore and image rollback in the selected environment.

These items are deployment-specific follow-up, not blockers to developing and demonstrating the mock-backed MVP. The current mock and golden tests are the repeatable readiness checks to keep passing as code changes.
