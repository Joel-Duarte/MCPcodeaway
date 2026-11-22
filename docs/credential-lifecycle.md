# Credential lifecycle

## Authority and credential types

Identity and authorization remain with the configured identity provider and each upstream policy system. MCPcodeaway validates the caller and enforces applicable decisions for each request; it does not issue user identities or make upstream authorization decisions authoritative.

| Credential | Owner and scope | Storage and expiry | Revocation and deletion |
|---|---|---|---|
| Caller access token | Identity provider; caller and tenant scoped | Presented for requests and validated by the configured provider. OIDC/JWKS and OBO caches are transient; no caller token is written to PostgreSQL. | Provider revocation/expiry applies. Cache eviction is safe and does not revoke the provider token. |
| Entra OBO token | Issued by Entra for the caller and requested resource | Transient configured cache, never PostgreSQL; short-lived and refreshed through the existing OBO flow. | Expiry, cache eviction, or provider revocation. OBO semantics are unchanged. |
| Per-user OAuth access and refresh tokens | Issued by the downstream OAuth provider; scoped to the upstream connection, gateway tenant, and user subject | For PostgreSQL deployments, held only in the owning gateway process memory. Not encrypted or persisted in PostgreSQL. Access-token expiry drives refresh when a valid refresh token exists. SQLite development mode retains the existing encrypted local-database behavior. | Explicit disconnect removes the in-memory record on that process. Provider revocation is separate. Restart or another replica loses access to the PostgreSQL-mode record; the user must reconnect. |
| Per-user API key/PAT | User and upstream scoped | Encrypted envelope in `user_upstream_credentials`; ciphertext is tenant/user/upstream bound. The encryption key is deployment configuration outside PostgreSQL. | The user disconnect route deletes the row. Key rotation re-encrypts values as they are read. Lost keys require reconnection. |
| OAuth client secret | Application/upstream configuration, not user delegated identity | Resolve from environment, file, AKV, or HCV references. `vault://` gateway-secret resolution is rejected for OAuth client secrets. It is not copied into OAuth token records or PostgreSQL. | Rotate at the provider and configured secret source. Update deployment configuration and restart/reload as required. |
| OAuth client identifier and provider metadata | Public application configuration | Client ID, endpoints, scopes, and token header settings are upstream configuration. They are not credentials. | Update authoritative upstream configuration. |
| Client-credentials access token | Application identity for a particular upstream | Transient process/cache state. The application secret stays in its configured external secret source; access tokens are not persisted in PostgreSQL. | Token expiry/cache eviction or application credential rotation. |

## PostgreSQL multi-replica behavior

PostgreSQL mode does **not** provide persistent per-user OAuth connections. The OAuth callback says to reconnect if the process restarts or another replica handles a request. `/me/connections` reports only credentials visible to that process, so a different replica can report `not_connected`; this is not proof that the provider revoked the grant. Per-user API keys, settings, upstream configuration, adaptive evidence, approvals, replay nonces, journals, and recovery records are shared according to [the PostgreSQL state inventory](postgres-state-architecture.md).

This is a deliberate MVP limitation. Do not advertise persistent multi-replica OAuth connections. If product requirements change, add a credential-store interface backed by a shared encrypted provider, with encryption keys managed outside PostgreSQL, tenant/user scoped access, cross-replica refresh coordination, and explicit revocation/deletion semantics. Do not put OAuth tokens in `gateway_secrets` or restore the old plaintext/encrypted token rows in PostgreSQL without an approved architecture change.

OAuth refresh is serialized within one `HostDispatcher` per tenant/user/upstream. PostgreSQL-mode volatile credentials are not shared between dispatchers or replicas, so this does not provide cross-replica token refresh coordination. A refresh rejection is surfaced as a reconnect-required credential error; transient provider/network failures also fail the current call and do not log token values. Provider outages do not authorize a fallback identity or credential.

## Legacy `gateway_secrets` review

`gateway_secrets` stores arbitrary envelope-encrypted strings and metadata (`name`, updater, timestamp). Its ciphertext and names do not reliably disclose whether a value is a token, client secret, API key, or non-secret configuration. The safe audit therefore marks every row for operator review and never prints or decrypts its value.

Run a metadata-only report with the same configured database and encryption-independent store settings as the gateway:

```sh
uv run python scripts/audit_gateway_secrets.py
```

Use this review sequence for each row:

1. Match the reported name to deployment/upstream configuration and the operator or user who last updated it. Do not infer its type from its name.
2. If its purpose or owner remains unclear, leave it in place and ask the responsible owner. Do not decrypt it for this generic audit.
3. For a required credential, rotate or recreate it at its issuing system, store it in the approved external source, update the reference, and verify resolution. Do not copy a potentially revoked or expired token out of the database.
4. After the replacement works, delete the old row with an explicit matching confirmation and retain the approved backup according to the operator's retention policy.

Delete a reviewed database record with an explicit matching confirmation:

```sh
uv run python scripts/audit_gateway_secrets.py --delete-name <reviewed-name> \
  --confirm-delete-name <reviewed-name>
```

The command will not classify by name alone, print values, or delete ambiguous rows automatically. Keep an approved backup before any deletion. Do not migrate a token into another PostgreSQL table. OAuth client secrets must use an external secret reference; OAuth access/refresh tokens must not be stored in `gateway_secrets`.

## Verification boundary

Implemented and locally testable behavior is distinguished from provider behavior: tests use OAuth mocks and local PostgreSQL, not a live identity provider or downstream token endpoint. The MVP does not test provider-side grant revocation propagation or survive PostgreSQL-mode process restart/replica changes for OAuth connections; those connections explicitly require reconnect. SQLite persistence is local development behavior, not a multi-replica contract.
