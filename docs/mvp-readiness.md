# MVP readiness

This page describes the evidence we have for the current MVP and the shortest path to a pilot. It replaces the former local lab guide and production readiness plan, which repeated setup details and carried decisions that a customer deployment must make later.

## What the MVP proves

- The gateway accepts authenticated, stateless MCP requests and enforces tenant, role, and group policy.
- The repository's OIDC mock exercises discovery, authorization code with PKCE, signed tokens, JWKS validation, userinfo, scope checks, and replay/redirect failures. Golden tests also run the gateway login, callback, session, and access-denial flow against that mock.
- Mock REST and MCP services exercise generated tools, request/response handling, and approval gates without requiring vendor accounts or extra identity-provider containers.
- The optional local Compose lab exercises OIDC login, a REST-backed tool, multiple gateway replicas, shared Redis/Postgres, and a requester/approver flow.
- The live benchmark can report request success, latency percentiles, throughput, and response size against the local CRM mock. Results describe that local setup only; they are not an SLO or a capacity promise.

The local identity provider, downstream services, and approval fixture are test doubles. They prove the gateway's implemented contracts and control flow. They do not prove a particular customer's IdP claims, an MCP client host's behavior, a partner API contract, production networking, or production operations. The current approval example has one approver group and one decision; it does not model a full manager hierarchy.

## Run the checks

The focused OIDC tests run in normal CI and need no containers:

```sh
uv run pytest tests/test_mock_oidc_provider.py tests/test_oidc_golden_flows.py
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

Open `http://localhost:8080` for the gateway and `http://localhost:8081/oidc` for the local issuer. The smoke checks identity and access boundaries, a CRM tool call, requester confirmation, wrong-group and self-approval denial, an eligible approver, and requester retry through the shared route. The local signing key is ignored by Git and is not a production credential.

To stop the lab:

```sh
docker compose -f docker-compose.yml -f docker-compose.readiness.yml --profile scaled down
```

For an observational CRM baseline, run `uv run python benchmarks/live_gateway_benchmark.py --mode traditional --requests 200 --concurrency 10 --warmup 20 --output /tmp/mvp-baseline.json`, then inspect `docker stats --no-stream`. Record the revision, host, and Compose settings with the output. Increase load gradually and watch latency, errors, CPU, memory, and worker saturation. There is no agreed workload or SLO gate for this MVP, so do not present a local mock result as a capacity claim.

## Current decisions and next steps

- Keep integrations as local mocks until a pilot selects a real IdP, MCP client host, and upstream. Add cases when a contract gap appears; do not add vendor containers or SDKs just to mirror a provider.
- Use a canary rollout for a customer pilot: start with one user, observe errors and resource use, then increase access in steps. Set deployment-specific traffic limits and objectives with the host team when the environment is known.
- Choose hosting, network/egress rules, secrets, and managed Redis/Postgres based on the customer's environment. The repository's Compose stack is a local exercise, not a deployment blueprint.
- Before customer use, verify the actual client's login and confirmation experience, customer claim mapping, upstream permissions and error behavior, and the customer's approver groups. For production operations, also rehearse backup/restore and image rollback in the selected environment.

These items are deployment-specific follow-up, not blockers to developing and demonstrating the mock-backed MVP. The current mock and golden tests are the repeatable readiness checks to keep passing as code changes.
