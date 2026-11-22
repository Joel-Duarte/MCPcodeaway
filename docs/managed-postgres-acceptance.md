# Managed PostgreSQL acceptance

This acceptance establishes the gateway-side connection and journal contract. The database used
for local evidence is disposable PostgreSQL in Docker Compose; no managed provider endpoint or
credentials were available, and no cloud infrastructure was provisioned.

## Gateway contract

- Production configuration validation requires `GATEWAY_POSTGRES_DSN` and the existing Redis URL.
  Production does not silently substitute SQLite or in-memory persistence.
- The application requires a DSN but does not identify or enforce that its host is a managed,
  external database. The external-service requirement must be enforced by deployment configuration
  and network policy.
- If `sslmode` is omitted, the gateway adds `verify-full`. asyncpg validates the certificate chain
  and DNS hostname for this mode. An explicitly weaker mode is not prevented by the generic DSN
  helper, so production secret review must reject `sslmode=disable`, `require`, or other modes that
  do not verify the managed server identity.
- Each process creates an asyncpg pool with minimum 1, maximum 10 connections, a 10-second
  connection timeout, and a 30-second command timeout. Readiness bounds the dependency health
  check to one second. Pool acquisition itself has no separately configured queue timeout.
- PostgreSQL readiness returns false while the dependency is unavailable and returns true after
  recovery. Structured transition events are emitted as `database.postgres_readiness_changed`;
  `/ready` exposes the resulting 503/200 state without returning connection details.
- The journal requires PostgreSQL atomic claims. A write is not dispatched before successful
  journal preparation and claim; an uncertain claimed write is never automatically replayed.

## Schema and database role gate

PostgreSQL DDL is applied only by the explicit versioned migration command:

```sh
MCP_POSTGRES_MIGRATION_DSN='<migration DSN from deployment secret storage>' \
  uv run python -m gateway.migrations upgrade
```

Run it as a one-shot deployment step before rolling out gateway replicas. The current schema is
version 2. The command serializes concurrent migration jobs with a PostgreSQL advisory lock,
reports the version transition, and exits nonzero if migration fails. The migration DSN and
runtime DSN must resolve to the same configured `search_path` schema.

`PostgresAuditStore.init()` now verifies connectivity, schema version, and required columns. It
does not create or alter tables or indexes. The runtime role needs only application DML and
`SELECT` on the migration version table; the operations role can inspect the restricted
`sensitive_write_operations` view. Migration ownership and grants for an existing installation
must be prepared by a DBA. Local TLS acceptance still needs to be rerun with a migrated database
and the restricted runtime role; its prior findings below predate this separation.

## Local evidence

The opt-in TLS suite uses a disposable local PostgreSQL service and verifies `verify-full`, trusted
CA and hostname, untrusted CA, hostname mismatch, invalid credentials, non-superuser access,
insufficient schema privileges, independent connection pools, and readiness recovery after a
database restart. The TLS-configured process acceptance reuses the base and TLS Compose files when
restarting the service, so subprocesses keep using the certificate-backed hostname and DSN.
Process barriers exercise journal preparation/claim and upstream dispatch boundaries, including
database interruption after an upstream write and before terminal journal persistence. The
caller-visible result remains uncertain and a duplicate same-correlation dispatch is blocked.

Expired-certificate behavior is not tested. Pool acquisition queue timeout is not configured.
Local containers do not validate provider networking, provider-issued certificates, maintenance
failover, backup, or restore behavior.

## Managed-provider deployment checks remaining

Run a separate, approved acceptance against a disposable managed database before production:

1. Run the migration job against a disposable managed database using the migration role; then
   start a gateway using the DML-only runtime role and verify startup succeeds without DDL rights.
2. Connect from the production network path using the provider-issued CA and DNS name with
   `sslmode=verify-full`.
3. Verify the chosen runtime and migration grants against the reviewed schema lifecycle.
4. Confirm pool sizing against replica count, rollout surge, worker count and provider connection
   limits; observe connection and command timeout behavior.
5. Exercise a safe maintenance/failover event and confirm readiness recovery and journal safety.
6. Restore a backup into a disposable database and verify required journal and approval records.

These provider-specific checks require a real endpoint and approved test environment. They are not
represented by the local simulation or Kubernetes tests.
