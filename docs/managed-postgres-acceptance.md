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

`PostgresAuditStore.init()` executes `CREATE TABLE IF NOT EXISTS` and `CREATE INDEX IF NOT EXISTS`
for application tables at process startup. There is no versioned schema migration command or
separate migration role in this repository. Therefore the runtime role currently needs schema
creation privileges during runtime startup. Local TLS acceptance uses a non-superuser role,
and confirms that a role lacking schema creation rights cannot initialize the store. This is not a
DML-only least-privilege runtime contract. Before production deployment, the service owner must
choose a reviewed schema ownership/migration procedure and restrict the runtime grants accordingly;
the application currently cannot use that restricted role without a separate schema initialization
step. This is an explicit deployment gate, not a claim of completed least-privilege validation.

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

1. Connect from the production network path using the provider-issued CA and DNS name with
   `sslmode=verify-full`.
2. Verify the chosen runtime and schema-initialization grants against the reviewed schema lifecycle.
3. Confirm pool sizing against replica count, rollout surge, worker count and provider connection
   limits; observe connection and command timeout behavior.
4. Exercise a safe maintenance/failover event and confirm readiness recovery and journal safety.
5. Restore a backup into a disposable database and verify required journal and approval records.

These provider-specific checks require a real endpoint and approved test environment. They are not
represented by the local simulation or Kubernetes tests.
