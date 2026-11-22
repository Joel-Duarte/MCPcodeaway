# Local Integration Testing

This suite uses the gateway's existing local IdP, mock upstreams, MCP protocol paths, SQLite journal, DNS-pinning controls, and recovery fixtures. Optional PostgreSQL tests exercise the real `PostgresAuditStore` against a disposable PostgreSQL 16 container. No test needs a production credential or external service.

## Environment assessment

The development environment checked for this validation had Docker 29.10.0, Docker Compose 5.8.0, system Python 3.14.10, `uv` 0.13.9, and npm 12.2.3. The project virtual environment used by `uv run` is Python 3.13.18. The PostgreSQL client (`psql`) and Poetry were absent. The repository has a Python `uv.lock`, pytest/pytest-asyncio fixtures, in-process HTTP services via HTTPX ASGI/Mock transports, local mock OIDC routes, MCP protocol fixtures, DNS-pinning unit fixtures, `docker-compose.yml` PostgreSQL/Redis services, and `scripts/pre-ci.sh`. PostgreSQL 16's Alpine image was present locally. An existing Compose stack was running during inspection; the acceptance Compose file uses a separate project name, host-only port, test-only credentials, and a project-scoped named volume.

## Run

The current acceptance Compose service uses a project-scoped named PostgreSQL volume so interruption tests can stop and restart the server without losing database contents. The runner removes this volume during cleanup.

Fast local acceptance checks reuse existing deterministic tests and need no Docker or external network:

```sh
uv run python scripts/local_acceptance.py
```

To include live PostgreSQL journal checks (Docker required):

```sh
uv run python scripts/local_acceptance.py --with-postgres
```

To include cross-process PostgreSQL claims and database-interruption tests (Docker required):

    uv run python scripts/local_acceptance.py --with-postgres-processes

To include the local TCP/TLS OIDC/OBO and MCP SDK tests:

```sh
uv run python scripts/local_acceptance.py --with-tcp-tls
```

For only the TCP/TLS tests:

```sh
MCP_RUN_TCP_TLS_TESTS=1 uv run pytest -q tests/test_tcp_tls_acceptance.py
```

The script waits for PostgreSQL health, runs the same local acceptance set plus `tests/test_postgres_acceptance.py`, and tears down only the isolated `mcpcodeaway-local-acceptance` project and its volumes on exit. The database is exposed only on `127.0.0.1:55432`. To select another available port, set `MCP_TEST_POSTGRES_PORT` before running. To connect manually, set `MCP_TEST_POSTGRES_DSN`; when running the script with PostgreSQL, that DSN is set automatically unless supplied by the caller.

Direct PostgreSQL invocation is also available after starting the service:

```sh
docker compose -f docker-compose.acceptance.yml -p mcpcodeaway-local-acceptance up -d --wait postgres
MCP_TEST_POSTGRES_DSN='postgresql://gateway_test:local_acceptance_only@127.0.0.1:55432/mcpcodeaway_acceptance' uv run --extra postgres pytest -m postgres -q
docker compose -f docker-compose.acceptance.yml -p mcpcodeaway-local-acceptance down --volumes --remove-orphans
```

If Docker or `asyncpg` is unavailable, PostgreSQL tests are skipped with a reason. SQLite journal tests remain runnable and are not reported as PostgreSQL evidence. The normal fast pre-CI gate remains unchanged. Run `./scripts/pre-ci.sh --fast` or `./scripts/pre-ci.sh --full` for the repository's existing gates.

## Coverage matrix

| Boundary | Existing/new checks and implementation exercised | Infrastructure | Docker / external network | Result in this run | Evidence limit |
| --- | --- | --- | --- | --- | --- |
| OIDC/JWT validation and local provider | `test_auth_obo.py`, `test_mock_oidc_provider.py`, `test_oidc_golden_flows.py`; actual `OidcTokenValidator` and local RS256 OIDC routes | In-process FastAPI/HTTPX; generated RSA keys | No / No | Passed | Local provider behavior does not establish Entra compatibility |
| OBO and OAuth failures | `test_auth_obo.py`; actual `DelegatedTokenExchanger` against captured local/mock token responses | HTTPX mock transport and local service fixtures | No / No | Passed | No live Entra token endpoint or claims-challenge interoperability |
| Graph group overage and cache scope | `test_auth_obo.py`; actual `EntraGroupMembershipResolver` and cache backend | Stateful in-process Graph-shaped responses | No / No | Passed | Not a live Microsoft Graph permission or API contract test |
| Gateway modes and adaptive Auto | `test_gateway_modes_e2e.py`, `test_phase11_scaling_analytics_ab.py`, `test_adaptive_execution.py`; actual gateway `/mcp` dispatch and policy | In-process gateway/upstreams | No / No | Passed | Existing gateway tests cover all modes across cases, but don't run a standalone MCP SDK server over a local TCP/TLS connection |
| Write journal and recovery | `test_sensitive_write_audit.py`, `test_upstream_recovery_golden.py`, `test_mcp_task_recovery.py`; actual dispatcher and SQLite store | SQLite plus failure-injected dispatch calls | No / No | Passed | SQLite does not prove Postgres transaction/concurrency behavior |
| Postgres claims and durable state | New `test_postgres_acceptance.py`; actual `PostgresAuditStore`, two independent pools | Disposable PostgreSQL 16 | Yes / No | Passed: 2 tests; skipped in default full-suite run | Separate pools model replicas but do not exercise process scheduling or production HA/failover |
| DNS pinning / SSRF | `test_security_hardening.py`; actual URL validation and `_PinnedNetworkBackend` with controlled resolver/peer | Simulated DNS and backend peer stream | No / No | Passed | Resolver/peer tests are simulated; no live local TLS certificate/connection test, corporate DNS, or production egress path |
| Failure injection and audit | Recovery, journal, and auth tests above; local timeouts/errors, invalid responses, and persistence states | In-process and mocked failure controls | No / No | Passed | Diagnostics are asserted only where each test explicitly checks them |

## TCP/TLS test details

`tests/test_tcp_tls_acceptance.py` is skipped unless `MCP_RUN_TCP_TLS_TESTS=1` is set; it is not part of fast pre-CI. It generates a short-lived test CA and a `localhost` server certificate under pytest's temporary directory, then runs Uvicorn HTTPS services bound to loopback sockets. Fixture teardown stops the servers and pytest removes the certificates.

| Tests | Client and server | Real socket / TLS verification | Failure cases |
| --- | --- | --- | --- |
| `test_local_https_oidc_discovery_jwks_and_rotation`, `test_local_https_tls_verification_rejects_untrusted_and_wrong_host` | Actual gateway `OidcTokenValidator` using its configured `httpx.AsyncClient`; local FastAPI OIDC metadata/JWKS service | Yes / CA trust is explicitly configured for success; default trust rejects the test CA; SAN mismatch is rejected | Key rotation, unknown `kid`, untrusted CA, hostname mismatch |
| `test_local_https_obo_success_challenge_validation_and_timeout`, `test_local_https_obo_connection_failure` | Actual gateway `DelegatedTokenExchanger` and configured `httpx.AsyncClient`; local FastAPI OAuth/OBO service | Yes / test CA verification is active; connection refusal follows stopping the local listener | Success, invalid scope, invalid audience response, claims challenge, tenant authority mismatch, HTTP 503, delay timeout, connection refusal |
| `test_mcp_sdk_client_initializes_lists_and_invokes_over_tcp_tls` | MCP SDK `ClientSession` and `streamable_http_client`; SDK `MCPServer.streamable_http_app` served by Uvicorn | Yes / SDK HTTP client trusts only the generated CA | Initialization, tool listing/call, unknown-tool protocol error |
| `test_gateway_upstream_client_uses_tcp_tls_mcp_sdk_boundary`, `test_gateway_mcp_tcp_tls_timeout_tls_verification_and_disconnect` | Actual gateway `UpstreamClientManager` and gateway dispatcher; SDK MCP server | Upstream traffic crosses TCP/TLS; gateway HTTPX client verifies the generated CA. Gateway front door itself uses ASGI in this test. | Discovery, invocation across Classic/Code/Hybrid/Auto, approval-required write, slow tool timeout, untrusted certificate, server shutdown/connection failure |

The OIDC metadata request uses the same real configured HTTPX client supplied to the gateway validator; endpoint URLs remain explicitly configured because the gateway does not auto-discover issuer endpoints. The MCP SDK server is a separate Uvicorn listener in the test process. These tests do not validate corporate IdP behavior, a production certificate chain, or every MCP client implementation. The SDK session test exercises standard MCP initialization; the gateway's upstream client exercises the gateway's sessionless discovery/list/call contract.

## Cross-process PostgreSQL tests

Run the process suite with uv run python scripts/local_acceptance.py --with-postgres-processes. It requires Docker and the optional asyncpg dependency. The script starts the disposable PostgreSQL service, runs the existing PostgreSQL checks and the new process suite, then removes the container, volume, and network even if pytest fails.

For direct execution after starting the same Compose service, set MCP_TEST_POSTGRES_DSN, MCP_TEST_POSTGRES_COMPOSE_PROJECT=mcpcodeaway-local-acceptance, and MCP_RUN_POSTGRES_PROCESS_TESTS=1, then run:

    uv run pytest -q tests/test_postgres_process_acceptance.py

Each gateway worker uses Python multiprocessing spawn and runs in a distinct OS process. It builds a real GatewayRuntime, initializes the actual PostgresAuditStore, and calls the actual HostDispatcher.invoke_tool. The dispatcher sends its MCP-shaped JSON-RPC request through UpstreamClientManager over a real loopback TCP connection to a controllable local HTTP service. The service records writes and can wait on a process event before returning.

Workers are synchronized with multiprocessing events. For same-correlation and distinct-correlation races, both workers signal readiness after journal preparation and before the real atomic claim; the parent then releases both claims together. The tests do not use sleeps to create claim contention.

Database outages are injected by stopping and restarting only the named Compose PostgreSQL service. The data volume preserves database contents across the restart. Scenarios tested:

- Before preparation: the worker store pool is initialized, PostgreSQL is stopped, then the write invocation is released. Preparation fails closed, no journal row is created, and the upstream receives no write.
- During preparation: a separate connection holds an ACCESS EXCLUSIVE table lock. The parent waits until PostgreSQL reports the worker's actual journal insert blocked on that lock, then stops the service. After restart, the incomplete transaction has no journal row and no upstream write occurred.
- After preparation and before claim: the test waits for the durable insert and claim-ready signal, stops PostgreSQL, and releases the claim. The claim fails, the upstream is not called, and the row remains prepared after restart.
- After upstream dispatch: the local service records the write and holds its response. PostgreSQL is stopped after receipt; then the upstream response is released. After restart, the operation remains executing, the same correlation cannot dispatch again, and a fresh correlation can complete a write. Raw arguments and credentials are not stored in the journal row.

The focused process suite passed all 6 tests in this run. The post-dispatch PostgreSQL failure now returns an error with outcome_unknown=true, while the durable row remains executing and same-correlation retry remains blocked. The service log records the attempted outcome_unknown terminal update and its persistence failure. A fresh correlation can still proceed after PostgreSQL recovers.

Definitive upstream failures remain outcome_unknown=false. If saving their terminal journal state also fails, the error preserves the upstream failure and states that the journal status could not be saved.

The Compose PostgreSQL service uses a project-scoped named volume so data survives a service stop/start during failure injection. Runner teardown removes the volume with Compose down --volumes.

## Phase 2C: gateway process death and restart

Run the hard process-termination acceptance suite with:

```sh
uv run python scripts/local_acceptance.py --with-process-crash
```

This opt-in option starts the same disposable, project-scoped PostgreSQL Compose service and persistent volume as the Phase 2B runner, executes the local acceptance tests and both PostgreSQL process suites, then removes only the `mcpcodeaway-local-acceptance` project and its volume. It requires Docker and the optional `asyncpg` dependency. The suite uses no production credentials or external services.

`tests/test_process_crash_acceptance.py` starts each gateway worker with Python multiprocessing `spawn`, so the controller and gateway have independent OS processes. Every worker creates a real `GatewayRuntime`, initializes `PostgresAuditStore`, and invokes `HostDispatcher.invoke_tool`. The controller and restarted worker use the same DSN. A parent-owned loopback HTTP upstream records each received write and completion in multiprocessing queues, so observations survive gateway death. `Process.kill()` is used; on POSIX the test asserts the exit status is `SIGKILL` (`-9`).

Barriers are attached to the actual journal prepare/claim and terminal persistence calls, the actual upstream client call, and the upstream response. They let the controller inspect PostgreSQL and upstream observations before terminating a worker without using sleeps to reach a lifecycle stage. The five test cases cover:

- Death after durable preparation and before claim: row stays `prepared`, no write has arrived; a fresh worker can claim and execute once.
- Death after claim/execution transition and before the upstream call: row stays `executing`, no write has arrived; same-correlation retry is rejected without dispatch.
- Death after the upstream has received and recorded the write but before its response: row stays `executing`; retry is rejected and a distinct correlation executes independently.
- Death after upstream success and before terminal persistence: row stays `executing`; retry is rejected and a distinct correlation executes independently.
- Death after terminal persistence and before returning the result: row stays `succeeded`; retry is rejected without another dispatch.

Each case checks that the restarted worker has a different PID, raw arguments and credential fields are absent from the journal row, and no same-correlation retry creates a second upstream request. A hard-killed worker cannot return a caller response; the suite validates durable journal and retry behavior separately. The tests establish this local journal contract only; they do not prove host/kernel crash durability, power-loss behavior, production PostgreSQL HA, or exactly-once effects for an external service.

## Known limitations

These tests establish local behavior against controlled protocol fixtures. They do not prove a real Entra tenant's app registration, issuer metadata, token claims, tenant policy, delegated Microsoft Graph permission consent, OBO configuration, or claims-challenge behavior. They do not exercise corporate network routing, firewall rules, production DNS, TLS interception, actual upstream API compatibility, or service-level performance and availability. The Postgres acceptance service is a single local instance without production TLS, backups, failover, or HA topology. Process termination tests use local SIGKILL and a local HTTP upstream; production host failure modes and real external side effects remain unverified. No local mock result should be treated as production-readiness evidence.

## Troubleshooting

- If port 55432 is occupied, set `MCP_TEST_POSTGRES_PORT` to a free local port.
- If the database image is unavailable and network pulls are restricted, use `uv run python scripts/local_acceptance.py` and retain the explicit PostgreSQL skip.
- If a prior run was interrupted, clean only the acceptance project with `docker compose -f docker-compose.acceptance.yml -p mcpcodeaway-local-acceptance down --volumes --remove-orphans`.
- The `--with-postgres` option installs/uses the optional Python extra through `uv`; it does not install host system packages.
