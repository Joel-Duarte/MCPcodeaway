# PostgreSQL state architecture

## Decision

PostgreSQL is the shared, durable state store for multi-replica gateway execution. It is required for production deployments that enable sensitive-write journaling, durable approvals, shared nonce replay prevention, or database-backed administrative configuration. Its primary safety value is cross-replica write coordination and approval durability; Entra and other configured identity providers remain authoritative for identity and business authorization.

Gateway replicas use a DML-only database role. Schema creation and upgrades are explicit deployment work:

```sh
MCP_POSTGRES_MIGRATION_DSN='postgresql://<migration-user>:<password>@<host>/<database>' \
  uv run python -m gateway.migrations upgrade
```

The command reports the before and after versions and exits nonzero on failure. It takes a transaction-scoped PostgreSQL advisory lock, so concurrent deploy jobs serialize. It never falls back to the gateway DSN. Do not put a DSN in command history or logs; inject it through the deployment secret manager. Before deploying a new gateway version, apply its migrations once, then start replicas with the runtime DSN.

The current schema version is **2**. Version 1 establishes all PostgreSQL tables and indexes; version 2 removes previously persisted `user_oauth` token envelopes and creates a limited operations view. Migrations are in `src/gateway/migrations.py`. To add one, append the next integer version and ordered SQL statements to `MIGRATIONS`, extend the expected schema contract when needed, and add migration coverage. Migrations are transactional and already recorded versions are skipped.

Gateway startup opens PostgreSQL, checks connectivity, validates the schema version and required columns, and fails with an instruction to run the migration command if the schema is absent, incomplete, or newer than supported. Startup, readiness, and normal store methods issue no PostgreSQL DDL. SQLite retains its existing local-development bootstrap behavior.

## Ownership and persistence inventory

“Restart” below means a gateway-process restart; “cluster restart” means a PostgreSQL service restart with its durable volume intact.

| Data/table or index | Classification and purpose | Authoritative owner | Durability and replica consistency | Loss or inconsistent copies | Recommended storage |
|---|---|---|---|---|---|
| `mcpcodeaway_schema_migrations` primary key | **Operational schema state.** Records which ordered migrations committed. | Migration source in `src/gateway/migrations.py`; the table is its applied-version ledger. | Must survive gateway and database restarts; shared deployment database. | Loss makes the migration command treat the schema as unversioned and recheck/reapply idempotent baseline operations; inconsistent history can block startup or cause incorrect upgrades. | PostgreSQL; required by migration and runtime version checks. It contains no identity or business records. |
| `sensitive_write_audits`, its primary/unique indexes | **Execution safety state.** Claims correlation/dedupe keys atomically and records prepared, executing, and terminal status without raw arguments. `dedupe_key` uniqueness is the cross-replica claim; the primary key binds one operation. | Gateway execution journal; upstream remains authoritative for the business effect. | Must survive process and PostgreSQL cluster restarts; all replicas must see the same unique constraints and terminal state. | Loss can admit a duplicate write after a retry; divergent copies can independently claim the same request. | Shared durable PostgreSQL; essential. |
| `recovery_operations`, claim and owner indexes | **Execution safety state.** Durable state for accepted operations, encrypted payload/result envelopes, deduplication, leases, cancellation, retry bookkeeping, and status. | Gateway recovery workflow; upstream remains authoritative for result reconciliation. | Must survive process restarts and be shared. PostgreSQL row claims and leases coordinate workers. | Loss can orphan accepted work or lose reconciliation state; divergent copies can run work more than once. | Shared durable PostgreSQL where recovery operations are enabled; essential to that feature. The worker's existing idempotency/reconciliation rules still govern retries; unresolved writes must not be blindly replayed. |
| `tool_approvals`, active-action unique and tenant/status indexes | **Approval state.** Binds the decision to tenant, requester, operation, argument digest, policy version, expiry, approver groups, and one-time execution status. | Gateway approval workflow; the approver identity is validated by the configured identity provider. | Must survive process and cluster restarts and be shared across replicas. | Loss can discard pending decisions and weaken replay prevention; inconsistent copies can permit duplicate or stale decisions. | Shared durable PostgreSQL; essential when human approval is enabled. |
| `consumed_request_state_nonces` primary key | **Execution safety state.** One-time request/OAuth state consumption with expiry. | Gateway protocol state; identity provider is authoritative for identity. | Must be shared while a nonce is live; expiry pruning is safe. | Loss can permit replay of an unexpired state nonce; replicas with separate copies do not coordinate. | PostgreSQL for multi-replica deployments; essential to the nonce replay guarantee. |
| `dynamic_upstreams` primary key | **Administrative configuration.** Admin-selected upstream definitions, including upstream-preferred execution mode, loaded into the registry. | Gateway administrators/configuration. | Must survive restarts and be consistent across replicas when database-managed onboarding is enabled. | Loss removes configured upstreams; stale replica copies produce different catalogs and routing. | PostgreSQL, or a single authoritative deployment configuration source if database onboarding is disabled. |
| `gateway_settings` primary key | **Administrative configuration.** Persisted gateway-owned settings including alert/retention policy, adaptive observe/active mode, and model-routing score state. It does not replace deployment configuration. | Gateway administrators and the adaptive evidence/routing subsystem. | Must survive restarts; replicas should read the same version. | Loss resets database-managed settings and adaptive score state; inconsistent replicas can evaluate different settings or routing evidence. | PostgreSQL when these settings are administered through the gateway or used by shared routing; otherwise config/evidence source. |
| `gateway_secrets` primary key | **Administrative configuration**, security-sensitive. Stores envelope-encrypted gateway secrets; ciphertext is not equivalent to safe-to-disclose data. | Configured secret provider or gateway secret administrator, depending on the secret reference. | Persists across restarts and is shared if used. Key material must remain outside PostgreSQL. | Loss requires secret reprovisioning; inconsistent copies can cause upstream auth failures. | Prefer environment or a dedicated secret manager for production secrets. OAuth client-secret resolution now rejects `vault://`; existing generic secret rows cannot be classified by content and must be reviewed by operators. Do not put access tokens, refresh tokens, or client secrets here. |
| `user_upstream_credentials` primary key | **Administrative/user connection state**, security-sensitive. Encrypted user API-key credentials remain available for supported user connections. | The user and upstream credential issuer. | API-key records currently survive restarts and are shared. Encryption key remains outside PostgreSQL. | Loss requires reconnecting; inconsistent copies make user credentials appear unavailable. | PostgreSQL only for encrypted non-OAuth credentials required by the user-connect feature. For PostgreSQL deployments, OAuth access and refresh tokens are process-local and are purged from PostgreSQL by migration v2; users reconnect after a restart or replica change. SQLite retains its existing local encrypted credential behavior. |
| `adaptive_evidence` composite primary key and tenant/created index | **Adaptive execution evidence.** Tenant-scoped evidence consumed by Auto's existing routing logic. | Evidence source/import process; gateway consumes it. | Evidence used for evidence-based Auto decisions must survive restarts and be visible to every replica. | Loss removes that evidence and can change Auto's available evidence/routing decisions; divergent evidence can make replicas disagree. | PostgreSQL for shared production evidence. The Auto algorithm and mode selection are unchanged. |
| `tool_guidance` composite primary key | **Administrative configuration.** Tenant/tool hints used by the gateway. | Gateway administrators. | Persist if admin-managed guidance must survive restart; shared for consistent outputs. | Loss removes hints; divergent copies cause differing guidance. | PostgreSQL for admin-managed configuration, otherwise deploy configuration. |
| `model_pricing` primary key | **Operational/configuration state.** Pricing inputs used for usage estimates, not authorization or write safety. | Gateway operators/pricing source. | Restart durability is useful but not needed for safe execution; can be reloaded from configured pricing data. | Loss affects cost estimates only; divergence changes reported estimates. | Optional PostgreSQL or a deployment-maintained pricing file. |
| `audit_runs`, created and tenant/created indexes | **Operational/observational state.** Redacted execution audit and inspection history. | Gateway audit subsystem; not identity authority. | Retention-bound; restart durability supports admin investigation, not routing or write claims. | Loss removes investigation history; inconsistent copies fragment audit search. | Optional PostgreSQL when audit retention is required; otherwise an approved external audit sink. |
| `sensitive_write_operations` view | **Operational/observational state.** Read-only projection of journal identifiers, upstream/tool names, status, and timestamps; excludes digests, payloads, ciphertext, and caller subject. | Derived from `sensitive_write_audits`. | Recreated by migration; does not duplicate state. | View loss affects operations inspection only. | PostgreSQL view, access only through the operations role. |
| SQLite-only `tool_model_rollups` table and its composite key | **Operational/observational state.** Aggregate metrics in the local SQLite backend. The PostgreSQL backend does not currently write this table. | Derived from audit runs. | Recomputable from retained audit data, but history is retention-dependent. | Loss affects dashboards/aggregates only. | SQLite local mode; optional external analytics for production. |
| `idx_audit_runs_created`, `idx_audit_runs_tenant_created`, `idx_tool_approvals_tenant_status_created`, `idx_recovery_operations_claim`, `idx_recovery_operations_owner` | Query/claim indexes; no independently authoritative data. | Derived from their tables. | Rebuilt by migrations; consistency follows the table. | Missing indexes degrade query and worker performance, not logical correctness (the approval uniqueness index is separately safety-critical). | PostgreSQL indexes. |
| Primary-key and unique indexes on all listed PostgreSQL tables; `idx_tool_approvals_active_action` | Enforce row identity, nonce uniqueness, operation dedupe, and one active approval per bound action. | Derived constraints. | Must exist and be consistent on the shared database. | Missing uniqueness can invalidate nonce, dedupe, and approval replay guarantees. | PostgreSQL constraints/indexes; migrations create them. |

### Sensitivity and recovery notes

- `sensitive_write_audits` holds tenant/caller identifiers, tool/upstream names, correlation IDs, and argument digests. It excludes raw arguments, but the digest and identifiers are still security-relevant. It cannot be rebuilt safely from upstream state because the claim and gateway-side uncertainty history may be the only evidence that execution was attempted.
- `recovery_operations` holds encrypted request and result envelopes plus status and error metadata. The ciphertext can represent sensitive arguments. Upstream reconciliation may establish an effect, but it cannot recreate a missing accepted operation or its exact request; unresolved work is never blindly replayed.
- `tool_approvals` holds requester and approver-group identifiers, a redacted policy-selected action summary, and a digest binding the request. Approval and one-time execution history cannot be recomputed; a lost decision requires a new approval.
- `consumed_request_state_nonces` holds replay-prevention markers. The nonce itself is security-sensitive and only expires naturally; loss during its validity window weakens replay prevention.
- `dynamic_upstreams`, `gateway_settings`, and `tool_guidance` contain operator configuration and may expose internal endpoints or policy text. Restore them from an authoritative configuration export if one exists; otherwise they require administrator reprovisioning.
- `gateway_secrets` contains encrypted secret material. Ciphertext remains sensitive and must not be treated as harmless. Values cannot be reconstructed from PostgreSQL if lost. Because rows are arbitrary named values, migration code cannot identify historical OAuth client secrets or tokens by content; operators must review existing rows and delete prohibited values. OAuth client-secret references can no longer use `vault://`.
- `user_upstream_credentials` contains encrypted credentials in existing schemas. Migration v2 removes persisted OAuth rows; supported API-key records remain encrypted. Those credentials are user-owned and must be re-entered if lost. OAuth tokens are held only in process memory for PostgreSQL deployments.
- `adaptive_evidence` is tenant-scoped derived evidence, not an identity authority. It can be re-imported only if its evidence source/export remains available; otherwise Auto's evidence base is reduced. `model_pricing` can be reloaded from the operator's pricing source. Neither table contains authentication tokens.
- `audit_runs` contains redacted operational records and caller/tenant identifiers. It may be recomputed only from an approved external audit sink, if configured; otherwise retention/pruning makes it intentionally non-recoverable. Indexes and the operations view contain no independent data and can be rebuilt from migrations/source tables.

PostgreSQL contains **no OIDC/JWKS cache, Entra group-membership cache, OBO token cache, admin-session store, or authoritative role/group claims**. OIDC/JWKS and token exchange caching use the configured cache backend; Entra group resolution is derived from Entra; admin sessions use signed session mechanisms in process/configuration. These caches may be evicted or recomputed and are not authorization authorities. The migration v2 purge is required for old `user_oauth` ciphertext records; token envelopes are never decrypted from PostgreSQL by the updated runtime.

## Why the write journal remains durable

The sensitive-write journal is not replaceable by authentication or upstream authorization. For a repeated correlation ID, the unique dedupe key makes one replica's claim visible to all replicas. If a process exits after claim, another process can inspect the durable nonterminal status and apply the existing recovery/reconciliation policy. If an upstream performed the write but the response or terminal database write was lost, the journal preserves uncertainty so the gateway can reconcile or report an uncertain result rather than issue an unsafe duplicate. If PostgreSQL is unavailable, journal-dependent writes must fail closed before dispatch; after a dispatch has begun, the outcome can be uncertain and must not be automatically replayed. The journal records digests and status rather than raw tool arguments.

Approval durability follows the same cross-replica requirement: expiry, one-time claim, request binding, and replay prevention all depend on shared atomic updates. PostgreSQL outage therefore blocks approval-backed execution and can temporarily block writes requiring journal claims. It does not make Entra validation or upstream policy authoritative state.

## Identity, policy, delegation, and execution state

1. **Identity validation** verifies a presented identity/token with the configured identity provider and its signing metadata.
2. **Gateway policy evaluation** applies MCPcodeaway policy to the validated identity and requested operation. Entra remains the source for identity/group membership; gateway policy is local policy, not an enterprise permissions database.
3. **Credential delegation/selection** obtains or selects the credential required by the configured upstream. Access/refresh tokens are not persisted in PostgreSQL. PostgreSQL-backed user OAuth credentials are volatile and require reconnect after process restart or replica change; durable encrypted user API-key envelopes are separate from OAuth tokens. OAuth client secrets must resolve from an external secret provider, not `vault://` gateway storage.
4. **Gateway execution state** persists dedupe claims, approval decisions, recovery leases, and explicitly administered configuration. It coordinates gateway replicas and does not authorize business actions on behalf of Entra.

## Database roles

Use separate credentials and inject each only into its intended workload. Replace the placeholder role names as needed. Run grants as a database administrator. The application uses the connection's configured `search_path` (normally `public`; existing installations may use a dedicated schema such as `gateway`). The migration and runtime DSNs must resolve to the same schema. Migration roles for an existing installation must own the application tables (or be explicitly granted ownership-level migration capability). Do not grant superuser or database-owner to either application role.

```sql
-- Provision as a DBA; use separate login credentials outside this SQL file.
CREATE ROLE mcpcodeaway_migrator LOGIN;
CREATE ROLE mcpcodeaway_runtime LOGIN;
CREATE ROLE mcpcodeaway_operations NOLOGIN;
GRANT CONNECT ON DATABASE mcpcodeaway TO
    mcpcodeaway_migrator, mcpcodeaway_runtime, mcpcodeaway_operations;
GRANT USAGE, CREATE ON SCHEMA public TO mcpcodeaway_migrator;

-- For a pre-existing installation, transfer ownership of the MCPcodeaway
-- tables to mcpcodeaway_migrator in a DBA-controlled preparation step. New
-- objects are owned by the migration role, so later migrations can alter them.

REVOKE CREATE ON SCHEMA public FROM mcpcodeaway_runtime;
GRANT USAGE ON SCHEMA public TO mcpcodeaway_runtime;
GRANT SELECT ON mcpcodeaway_schema_migrations TO mcpcodeaway_runtime;
GRANT SELECT ON
    audit_runs, dynamic_upstreams, model_pricing, gateway_settings,
    adaptive_evidence, consumed_request_state_nonces, tool_approvals,
    tool_guidance, gateway_secrets, recovery_operations,
    sensitive_write_audits, user_upstream_credentials
TO mcpcodeaway_runtime;
GRANT INSERT ON
    audit_runs, dynamic_upstreams, model_pricing, gateway_settings,
    adaptive_evidence, consumed_request_state_nonces, tool_approvals,
    tool_guidance, gateway_secrets, recovery_operations,
    sensitive_write_audits, user_upstream_credentials
TO mcpcodeaway_runtime;
GRANT UPDATE ON
    audit_runs, dynamic_upstreams, model_pricing, gateway_settings,
    tool_approvals, tool_guidance, gateway_secrets, recovery_operations,
    sensitive_write_audits, user_upstream_credentials
TO mcpcodeaway_runtime;
GRANT DELETE ON
    audit_runs, dynamic_upstreams, model_pricing, consumed_request_state_nonces,
    tool_guidance, gateway_secrets, recovery_operations, user_upstream_credentials
TO mcpcodeaway_runtime;

-- Inspection exposes identifiers, upstream/tool names, status, and timestamps.
GRANT USAGE ON SCHEMA public TO mcpcodeaway_operations;
GRANT SELECT ON sensitive_write_operations TO mcpcodeaway_operations;
```

The runtime role gets `DELETE` only on tables where the store prunes or removes records, and does not receive `UPDATE` on append/claim-only adaptive evidence or nonce state. It gets no `CREATE`, `ALTER`, `DROP`, ownership, or migration-history write permissions. The operations role receives no base-table access and cannot update journal records. Reapply runtime grants for newly added tables as part of each deployment migration. For a dedicated schema, replace `public` in the example with the schema used by both DSNs.

## Unavailable PostgreSQL

If PostgreSQL is unavailable, production startup fails when schema connectivity/verification cannot be completed. Readiness subsequently reports PostgreSQL unavailable. Safe behavior takes precedence over write availability: operations that need the shared journal, approval, or nonce guarantees cannot safely execute on independent per-replica copies. Read-only features backed by durable audit/configuration data may also be unavailable. Local SQLite mode remains intended for local development and single-process use; it is not a substitute for shared multi-replica coordination.
