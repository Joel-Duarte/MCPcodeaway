# Company SSO and Control-Plane Access

This guide configures one company identity provider for one MCPcodeaway deployment. The gateway currently trusts **one OIDC issuer per deployment**. Entra ID, Okta, Keycloak, and other providers can be used when they issue RS256-signed JWT access tokens with the expected issuer, gateway API audience, expiry, subject, and role claims. The gateway uses configured endpoints and JWKS; it does not discover providers dynamically or offer a provider chooser.

There is no gateway-managed employee directory or production password login. The company's identity provider owns users, groups, MFA, conditional access, and user lifecycle. If local passwords are required, put them in an OIDC identity provider such as Keycloak and let that provider authenticate users.

## Responsibility split

| Configure in the company identity provider | Configure in the gateway deployment |
| --- | --- |
| Register the gateway API/resource and expose a delegated scope for the management UI. | Pin the exact issuer, JWKS URL, token endpoint, audience, and allowed provider tenant/realm. |
| Register the confidential web client and its exact HTTPS callback URL. Grant it the API scope. | Set the web client ID and keep its client secret in a secret manager. |
| Define app roles/clients' role claims and assign controlled groups or service principals. | Map provider claims and role values to `gateway_admin`, `operator_viewer`, and any metrics roles. |
| Enforce MFA/Conditional Access and provision/deprovision users. | Enforce authorization on each protected API; the UI is not the security boundary. |

Assign at least two named administrators through the identity provider's controlled admin group before enabling production SSO, so routine staff changes do not lock the company out. Do not let a user self-assign an admin role in the gateway UI. Entra supports app roles assigned to users and groups; see [Microsoft's app-role guide](https://learn.microsoft.com/en-us/entra/identity-platform/howto-add-app-roles-in-apps).

## Identity-provider setup

Set up two app registrations where the provider supports this separation:

1. **Gateway API/resource** — expose a delegated scope such as `gateway.access`. Define role values for console administrators and viewers. Use values that match the gateway role allowlists, such as `gateway_admin`, `platform_admin`, and `operator_viewer`.
2. **Management web client** — configure a confidential server-side OIDC client. Add the exact redirect URI `https://<gateway-host>/auth/admin/callback`, grant it the gateway API scope, and create a client secret or certificate. Consent only to the scopes required for sign-in and the gateway API.

The gateway uses the web-client ID to start the login flow, but it validates the returned **access token for the gateway API**. The access token must have the gateway API as its audience (`aud`); an ID token for the web client is not accepted as API authorization. Copy the actual issuer and audience from the provider metadata and a test access token rather than guessing URL or audience formats. Do not use a provider's `common`/multi-tenant issuer for a single-company deployment unless multi-tenant trust is deliberately designed and reviewed.

For Entra, define the app roles on the API/resource app and assign them to managed groups. Ensure the resulting gateway API access token carries the expected `roles` values. Require user assignment to the management client where available. For other providers, configure a mapper so the signed API access token contains equivalent roles and a stable tenant/realm claim. Never grant console access by email domain or by an unverified browser-supplied field.

### Endpoint templates

Treat these as templates, not values to copy blindly. Use the provider's issuer metadata and verify the exact `iss`, `aud`, and tenant/realm claims from a staging access token.

| Provider | Issuer | JWKS | Authorization | Token |
| --- | --- | --- | --- | --- |
| Entra ID v2 | `https://login.microsoftonline.com/<tenant-guid>/v2.0` | `https://login.microsoftonline.com/<tenant-guid>/discovery/v2.0/keys` | `https://login.microsoftonline.com/<tenant-guid>/oauth2/v2.0/authorize` | `https://login.microsoftonline.com/<tenant-guid>/oauth2/v2.0/token` |
| Keycloak | `https://<host>/realms/<realm>` | `<issuer>/protocol/openid-connect/certs` | `<issuer>/protocol/openid-connect/auth` | `<issuer>/protocol/openid-connect/token` |

For Keycloak, add a protocol mapper that emits a stable realm identifier as a token claim (for example `gateway_tenant`) if the token does not contain `tid`, then set `GATEWAY_CLAIM_MAPPING` to map `tenant` to that claim and allowlist its exact value. Also map client ID or role claims if the token uses different claim names. For Entra, use the tenant-specific v2 endpoints and verify the API access token's actual `aud`; do not assume the client application's ID is the API audience.

## Gateway deployment configuration

Configure these values on the **gateway service**, not in the browser bundle or frontend container. Compose forwards the OIDC and role settings to `gateway-prod`; Kubernetes deployments should use ConfigMaps for non-secret values and a secret manager/External Secrets integration for credentials.

| Environment variable | Purpose |
| --- | --- |
| `GATEWAY_PUBLIC_URL` | Public HTTPS origin, e.g. `https://mcp.example.com`. |
| `GATEWAY_OIDC_ISSUER` | Exact `iss` expected in gateway API access tokens. |
| `GATEWAY_OIDC_JWKS_URI` | Trusted provider signing-key endpoint. |
| `GATEWAY_OIDC_AUDIENCE` | Exact `aud` expected in gateway API access tokens. |
| `GATEWAY_OIDC_TOKEN_ENDPOINT` | Provider token endpoint for code exchange and configured OBO flows. |
| `GATEWAY_OIDC_CLIENT_ID` | Confidential management web-client ID. |
| `GATEWAY_OIDC_CLIENT_SECRET` | Web-client secret; inject from a secret manager, never commit it or place it in frontend config. |
| `GATEWAY_ADMIN_OIDC_AUTHORIZATION_ENDPOINT` | Provider authorization endpoint. |
| `GATEWAY_ADMIN_OIDC_REDIRECT_URI` | Exact public callback URL: `https://<gateway-host>/auth/admin/callback`. Register the same URI at the provider. |
| `GATEWAY_ADMIN_OIDC_API_SCOPE` | Delegated scope for the gateway API, e.g. `api://<api-app-id>/gateway.access`. |
| `GATEWAY_ALLOWED_TENANTS` | JSON array of the exact provider tenant/realm claim values trusted by this deployment. |
| `GATEWAY_CLAIM_MAPPING` | Optional JSON mapping when provider claim names differ from defaults (`sub`, `tid`, `email`, `roles`, `groups`, `scp`, `azp`, `client_groups`). |
| `GATEWAY_ADMIN_ROLES` | JSON array of provider role values allowed to administer the gateway. Defaults to `gateway_admin` and `platform_admin`. |
| `GATEWAY_VIEWER_ROLES` | JSON array of provider role values allowed read-only console access. Defaults to `operator_viewer`. |
| `GATEWAY_METRICS_ROLES` | JSON array of role values allowed to read protected Prometheus metrics. Defaults include gateway admins and `ops_observer`. |

Compose environment values are strings; use JSON arrays/objects, for example:

```dotenv
GATEWAY_ALLOWED_TENANTS='["<tenant-guid>"]'
GATEWAY_ADMIN_ROLES='["gateway_admin"]'
GATEWAY_VIEWER_ROLES='["operator_viewer"]'
GATEWAY_METRICS_ROLES='["gateway_admin","ops_observer"]'
# Only needed when the provider uses different claim names:
GATEWAY_CLAIM_MAPPING='{"tenant":"org_id","roles":"roles","client_id":"client_id"}'
```

Set role values to match the provider's token exactly. If changing `GATEWAY_ADMIN_ROLES` or `GATEWAY_VIEWER_ROLES`, also ensure the IdP emits those exact strings. Do not put secrets in `GATEWAY_CLAIM_MAPPING`, command-line arguments, Docker image layers, browser variables, or committed `.env` files. Restrict access to deployment configuration and rotate OIDC client credentials through the provider and secret manager.

`docker-compose.yml` forwards `GATEWAY_CLAIM_MAPPING`, `GATEWAY_ADMIN_ROLES`, `GATEWAY_VIEWER_ROLES`, and `GATEWAY_METRICS_ROLES` to `gateway-prod`; the gateway validates production mode, HTTPS callback/origin, required OIDC endpoints, explicit audience and tenant allowlist, and credentials at startup. Keep `GATEWAY_APP_ENV=prod`, `GATEWAY_AUTH_MODE=oidc`, and `GATEWAY_REQUIRE_ADMIN_AUTH=true` in production. Other required production secrets and storage settings are described in [Cloud & Sandbox Scalability](cloud_scalability.md).

The frontend needs only its public gateway API base URL (when hosted separately) and the sign-in link. Do not set the OIDC client secret or use an OIDC token as a frontend configuration value. The gateway callback validates the access token and issues an HttpOnly session cookie; every management API request must still be authorized server-side.

## Provider-independent acceptance checks

Before enabling user access, validate these cases in a staging deployment against the actual provider and client registration:

1. A user assigned the admin role can sign in and perform an explicitly authorized administrative action.
2. A user assigned only the viewer role can read permitted console data but receives `403` for every mutation.
3. A valid user without either console role is denied, receives a clear access-denied message, and has no admin session cookie afterward, including when a prior session existed; signing in must not grant a role.
4. Tokens with the wrong issuer, audience, tenant/realm, signature, expiry, or missing role are rejected.
5. An ID token is rejected as an API token; only the gateway API access token is accepted.
6. The callback URI mismatch, invalid/expired/replayed state, and failed code exchange do not create a session.
7. The browser session cookie is Secure, HttpOnly, appropriately SameSite, and expires no later than its source access token.
8. Removing a user's IdP group/role prevents new sessions from gaining that role. Set session TTL and revocation expectations accordingly; current gateway sessions are bounded by token expiry but may remain active until expiry unless explicitly revoked.
9. A dedicated `ops_observer` workload can read only its intended metrics endpoint and cannot access admin mutation APIs or the admin MCP endpoint.

Test with one assigned admin, one assigned viewer, and one authenticated-but-unassigned user. Do not use production credentials or real privileged accounts for negative tests. Review gateway audit logs for denial events without logging bearer tokens, authorization codes, or client secrets.

## Current implementation limits to close before calling this production-ready

- The provider integration is a **single configured issuer** and supports RS256 JWT signatures only. Validate a target provider's access-token format in staging; providers using another signing algorithm need an explicit, reviewed implementation change.
- OIDC endpoints are configured explicitly; discovery and multi-provider login selection are not implemented.
- Manual Bearer-token exchange is available only outside production: the production configuration omits the endpoint and the UI hides the form. OIDC callback users without an allowed role are redirected to the console login page with a generic access-denied message, and any existing admin session cookie is cleared.
- Role checks on APIs are authoritative. Hiding UI controls alone does not secure an endpoint; preserve backend authorization checks for all reads and writes.

This guide describes configuration; it does not mean an external Entra/Okta/Keycloak tenant has already been registered or configured. The deployer and the company's identity administrator must complete those steps together.
