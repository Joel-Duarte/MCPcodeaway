# Authentication & Authorization Spectrum Flows

This document illustrates how **MCPcodeaway** bridges a single caller identity (Microsoft Entra ID, Keycloak, or a headless OAuth client) across every downstream authentication and authorization variation—both when the downstream service trusts Entra and when it does not.

---

## 1. Full Authentication & Authorization Spectrum Flow Board

```mermaid
flowchart TD
    Caller["User or Agent Harness"] -->|"1. Obtains JWT from IdP"| IdP["Entra ID / Keycloak / OIDC IdP"]
    IdP -->|"Bearer JWT (sub, tid, groups, roles, scp, azp)"| Caller
    Caller -->|"2. POST /mcp + Authorization: Bearer JWT"| FrontDoor["OIDCValidator (JWKS RS256 + Tenant Allowlist)"]

    FrontDoor -->|"Missing or Invalid JWT"| Reject401["HTTP 401 + RFC 9728 WWW-Authenticate"]
    FrontDoor -->|"Unapproved Tenant"| Reject403["HTTP 403 Forbidden"]
    FrontDoor -->|"Verified CallerIdentity"| Policy["PolicyEvaluator (RBAC: groups, roles, clients, scopes, read_only_prod)"]

    Policy -->|"Denied"| PolicyErr["JSON-RPC Policy Error"]
    Policy -->|"Allowed"| ABACArg["Pre-Call ABAC Argument Check (e.g. client_groups)"]
    ABACArg --> Dispatch["HostDispatcher.resolve_upstream_headers()"]

    Dispatch -->|"delegated_token (Entra OBO)"| EntraOBO["DelegatedTokenExchanger (entra_v2_obo)"]
    EntraOBO -->|"User-Scoped Entra JWT"| SvcEntra["Entra-Protected MCP / REST API"]

    Dispatch -->|"delegated_token (Keycloak RFC 8693)"| Kc8693["DelegatedTokenExchanger (rfc8693)"]
    Kc8693 -->|"Brokered Keycloak JWT"| SvcKc["Keycloak-Protected MCP / REST API"]

    Dispatch -->|"client_credentials"| M2M["ClientCredentialsProvider + CacheBackend"]
    M2M -->|"M2M Service JWT"| SvcM2M["OAuth2 Machine-to-Machine API"]

    Dispatch -->|"api_key / bearer_secret"| SecRes["SecretResolver (env://, file://, vault://, akv://, hcv://)"]
    SecRes -->|"Injected Host Secret Header"| SvcShared["Shared-Secret REST / MCP API"]

    Dispatch -->|"user_oauth / user_api_key"| Vault["CredentialVault (AES-256-GCM in SQLite/Postgres)"]
    Vault -->|"Credential Present (Auto-Refreshed if Expiring)"| SvcUser["Per-User Non-Entra OAuth / PAT Service"]
    Vault -->|"Missing or Revoked"| ConnectLink["Actionable Tool Error + Signed /auth/connect/{upstream} URL"]

    Dispatch -->|"none"| SvcOpen["Open Internal Sandbox Service"]

    SvcEntra --> PostFilter["Post-Call ABAC Row Filter + OutputRedactor"]
    SvcKc --> PostFilter
    SvcM2M --> PostFilter
    SvcShared --> PostFilter
    SvcUser --> PostFilter
    SvcOpen --> PostFilter
    PostFilter -->|"Redacted & Grounded Result (No Secrets Exposed)"| Caller
```

---

## 2. Non-Entra Per-User Credential Linking (`user_oauth` / `user_api_key`)

When a downstream service does **not** use Entra ID and requires each user's own OAuth consent or Personal Access Token (PAT), Entra never holds those credentials. Instead, the gateway uses an encrypted per-user `CredentialVault`:

```mermaid
sequenceDiagram
    participant Agent as User / Agent Harness
    participant GW as MCPcodeaway Gateway
    participant Vault as CredentialVault (AES-GCM)
    participant Ext as External OAuth / PAT Service

    Agent->>GW: POST /mcp (Bearer Entra JWT) -> tools/call external_oauth
    GW->>GW: Validate Entra JWT & PolicyEvaluator RBAC
    GW->>Vault: Load credential for (tenant_id, user_sub, external_oauth)
    Vault-->>GW: Not found
    GW-->>Agent: Tool Error + Signed URL (/auth/connect/external_oauth?ticket=...)
    Note over Agent,Ext: User opens signed URL in browser (never pastes secrets into chat)
    Agent->>GW: GET /auth/connect/external_oauth?ticket=...
    GW-->>Agent: 302 Redirect to External OAuth Authorize + PKCE (S256)
    Agent->>Ext: User approves consent
    Ext-->>GW: GET /auth/connect/external_oauth/callback?code=...&state=...
    GW->>Ext: POST /token (code + code_verifier)
    Ext-->>GW: access_token + refresh_token
    GW->>Vault: Encrypt (AES-256-GCM + AAD) & store per (tenant_id, user_sub, upstream)
    Agent->>GW: Retry POST /mcp (same Entra JWT)
    GW->>Vault: Decrypt user token (auto-refresh if near expiry)
    GW->>Ext: Call API with user's external Bearer token
    Ext-->>GW: User-scoped response
    GW-->>Agent: Redacted response
```

---

## 3. Spectrum Reference Matrix

| Scenario | `auth_type` | How Identity Flows | Where Verified |
| :--- | :--- | :--- | :--- |
| **Downstream uses Entra ID** | `delegated_token` (`entra_v2_obo`) | Entra JWT exchanged on-behalf-of user for downstream Entra audience | [`test_auth_spectrum_golden.py`](../tests/test_auth_spectrum_golden.py) (`inventory_obo`) |
| **Downstream uses Keycloak (Entra brokered)** | `delegated_token` (`rfc8693`) | Entra or Keycloak JWT exchanged via RFC 8693 against Keycloak token endpoint | [`test_auth_spectrum_golden.py`](../tests/test_auth_spectrum_golden.py) (`tax_keycloak`) |
| **Downstream uses OAuth M2M** | `client_credentials` | Gateway enforces caller RBAC, then fetches cached M2M token | [`test_auth_spectrum_golden.py`](../tests/test_auth_spectrum_golden.py) (`metrics_m2m`) |
| **Downstream uses shared API key / Bearer** | `api_key` / `bearer_secret` | Gateway enforces caller RBAC, resolves `vault://` / `file://` / `env://` / `akv://` / `hcv://` | [`test_auth_spectrum_golden.py`](../tests/test_auth_spectrum_golden.py) (`inventory_apikey`, `tax_bearer`) |
| **Downstream uses per-user OAuth (non-Entra)** | `user_oauth` | User links once via PKCE `/auth/connect/{upstream}`; gateway auto-refreshes | [`test_auth_spectrum_golden.py`](../tests/test_auth_spectrum_golden.py) (`external_oauth`) |
| **Downstream uses per-user PAT** | `user_api_key` | User saves PAT in `/auth/connect/{upstream}` or `/connections` UI; isolated per `(tenant, sub)` | [`test_auth_spectrum_golden.py`](../tests/test_auth_spectrum_golden.py) (`external_pat`) |
| **Open internal sandbox** | `none` | No downstream creds; gated strictly by gateway OIDC + RBAC | [`test_auth_spectrum_golden.py`](../tests/test_auth_spectrum_golden.py) (`sandbox_open`) |
