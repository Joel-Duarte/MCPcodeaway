# Microsoft Entra ID & Copilot Studio Integration Preset

`MCPcodeaway` treats Microsoft Entra ID and Copilot Studio as a configuration preset over standard **OIDC + RFC 9728 Protected Resource Metadata + Delegated Token Exchange**.

## 1. Two-Tier Entra App Registration

1. **Backend API App Registration** (`mcp-code-gateway-api`):
   - Set **Application ID URI**: `api://mcp-code-gateway`
   - Expose delegated scope: `mcp.tools` (and optional `finance.read`, `ops.write`)
   - Add App Roles (`finance_analyst`, `ops_engineer`, `readonly_auditor`, `support_agent`)
   - Configure a client secret / federated credential and grant **API Permissions** to downstream APIs (e.g. `api://snowflake-ledger/.default`) for On-Behalf-Of (OBO) exchange.
2. **Client App Registration** (`copilot-studio-mcp-client`):
   - Add delegated permission to `mcp-code-gateway-api` (`mcp.tools`).
   - Add Web Redirect URI: `https://global.consent.azure-apim.net/redirect/<connector-id>` (used by Power Platform / Copilot Studio).

## 2. Gateway Environment Variables

```bash
GATEWAY_APP_ENV=uat
GATEWAY_AUTH_MODE=oidc
GATEWAY_TOKEN_EXCHANGE_DIALECT=entra_v2_obo
GATEWAY_GATEWAY_MODE=hybrid
GATEWAY_READ_ONLY_PROD=true

GATEWAY_PUBLIC_URL=https://mcp-gateway.company.internal
GATEWAY_OIDC_ISSUER=https://login.microsoftonline.com/<TENANT_ID>/v2.0
GATEWAY_OIDC_JWKS_URI=https://login.microsoftonline.com/<TENANT_ID>/discovery/v2.0/keys
GATEWAY_OIDC_AUDIENCE=api://mcp-code-gateway
GATEWAY_OIDC_TOKEN_ENDPOINT=https://login.microsoftonline.com/<TENANT_ID>/oauth2/v2.0/token
GATEWAY_OIDC_CLIENT_ID=<BACKEND_APP_CLIENT_ID>
GATEWAY_OIDC_CLIENT_SECRET=<BACKEND_APP_CLIENT_SECRET>
GATEWAY_ALLOWED_TENANTS='["<TENANT_ID>"]'
```

## 3. Common Copilot Studio Gotchas

This is a configuration sketch, not a verified Copilot Studio connector. The
repository's automated client evidence is currently based on its protocol
tests and local mocks; validate the actual MCP host before selecting it for a
pilot.

- **RFC 9728 `resource` indicator alignment**: Ensure `GATEWAY_OIDC_AUDIENCE` matches the exact `resource` advertised at `GET /.well-known/oauth-protected-resource`.
- **`HTTP 400` from `token.azure-apim.net`**: Usually indicates a scope format mismatch (`api://<APP_ID>/mcp.tools` vs bare `mcp.tools`) or missing offline_access/refresh token handling in the client registration.
- **Hybrid governance**: Setting `GATEWAY_GATEWAY_MODE=hybrid` keeps read/reconciliation orchestration inside `execute_code` while surfacing mutating actions (such as `ops_ticketing__create_collection_case`) as direct MCP tools so Copilot Studio renders an explicit user confirmation prompt.
