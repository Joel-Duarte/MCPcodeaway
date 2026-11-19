# Multi-Provider Identity, Auto-Grouping & Non-SSO Harness Guide

`MCPcodeaway` is **100% vendor-agnostic**. It uses standard **OIDC / OAuth 2.1 + RFC 9728 Protected Resource Metadata + Delegated Token Exchange (`entra_v2_obo` or `rfc8693`)**, plus configurable claim mapping ([`ClaimMappingConfig`](../../src/gateway/config.py)) so you can integrate **Microsoft Entra ID**, **AWS**, **Google Cloud**, **Okta**, **Keycloak**, or even **non-SSO CLI/local agent harnesses**.

---

## 1. Configuring Identity & Auto-Grouping Across Cloud Providers

Every OIDC provider issues JWTs with slightly different claim names for **User ID**, **Tenant**, **Department Groups**, **Per-Person Roles**, and **Calling Agent ID**. Configure `GATEWAY_CLAIM_MAPPING` (or `claim_mapping` in YAML) to match your provider:

| Provider | `GATEWAY_OIDC_ISSUER` Example | `GATEWAY_TOKEN_EXCHANGE_DIALECT` | `GATEWAY_CLAIM_MAPPING` JSON |
| :--- | :--- | :--- | :--- |
| **Microsoft Entra ID** *(Default)* | `https://login.microsoftonline.com/<TENANT_ID>/v2.0` | `entra_v2_obo` | `{"subject":"sub","tenant":"tid","email":"email","roles":"roles","groups":"groups","scopes":"scp","client_id":"azp","client_groups":"client_groups"}` |
| **AWS Cognito / IAM Identity Center** | `https://cognito-idp.<REGION>.amazonaws.com/<USER_POOL_ID>` | `rfc8693` | `{"subject":"sub","tenant":"custom:tenant_id","email":"email","roles":"custom:roles","groups":"cognito:groups","scopes":"scope","client_id":"client_id","client_groups":"custom:region"}` |
| **Google Cloud / Google Workspace** | `https://accounts.google.com` *(or Workforce Pool Issuer URL)* | `rfc8693` | `{"subject":"sub","tenant":"hd","email":"email","roles":"roles","groups":"groups","scopes":"scope","client_id":"azp","client_groups":"client_groups"}` |
| **Okta / Auth0 / Keycloak** | `https://sso.company.com/realms/enterprise` | `rfc8693` | `{"subject":"sub","tenant":"org_id","email":"email","roles":"roles","groups":"groups","scopes":"scope","client_id":"azp","client_groups":"client_groups"}` |

### How Company-Level Auto-Grouping & Per-Person Extra Permissions Work

In [`config/upstreams.yaml`](../../config/upstreams.yaml), each upstream MCP server declares who can discover and execute it:

```yaml
upstreams:
  - name: snowflake_ledger
    url: http://snowflake-mcp:9101/mcp
    allowed_groups: ["finance"]                         # Company department group (from IdP)
    allowed_roles: ["finance_analyst", "snowflake_reader"] # Role or per-person exception
    allowed_clients: ["copilot-finance-agent", "claude-code"] # Optional agent harness allowlist
    allowed_scopes: ["mcp.tools", "finance.read"]
```

1. **Automatic Department Grouping (`groups`):** When an employee logs in via SSO, their IdP token automatically includes their security groups (e.g., `groups: ["finance"]`). They immediately see all Finance MCP servers (`list_servers` / `search_tools`) without creating any account in MCPcodeaway.
2. **Per-Person Extra Permissions (`roles` or secondary `groups`):** If one specific person in Finance *also* needs access to an Operations tool (`ops_ticketing`), IT assigns that individual the `ticketing_writer` App Role (or adds them to an `ops-escalation` group) in Entra / Okta / AWS / Google Cloud. Because `PolicyEvaluator` checks the union of a user's `groups` and `roles`, that user keeps their Finance tools and gains the extra permission immediately.

---

## 2. What If an Agent or Harness Does NOT Have Entra / Interactive SSO?

If an agent or CLI harness does not support interactive browser OAuth/Entra SSO (for example, a headless background worker, a CI pipeline agent, or a local coding harness like Cursor/Claude Code using static config), there are **four supported patterns**:

| Scenario | What Happens & How to Connect | Security & Grouping Behavior |
| :--- | :--- | :--- |
| **A. Missing Token (`No Authorization Header`)** | **Fails Closed (`HTTP 401 Unauthorized`).** The gateway returns a standard RFC 9728 header: `WWW-Authenticate: Bearer resource_metadata=".../.well-known/oauth-protected-resource"`. | Standard MCP clients (e.g., Copilot Studio, MCP Inspector, OAuth-capable harnesses) read `/.well-known/oauth-protected-resource`, auto-discover the IdP, and prompt the user to sign in. |
| **B. Headless / Autonomous Service Agent (No Human User)** | **OAuth 2.0 Client Credentials Flow (`client_id` + `client_secret` or Federated Workload Identity).** The agent authenticates directly with Entra / AWS / GCP / Keycloak as an application and receives a service JWT (`sub = app_object_id`, `azp = agent_client_id`, `roles = ["finance_bot_role"]`). | The agent gets its **own dedicated grouping and App Roles** in the IdP (`allowed_clients: ["nightly-reconcile-bot"]`, `allowed_roles: ["finance_bot_role"]`) and cannot access tools outside its assigned role. |
| **C. Personal CLI / Desktop Harness (Static Config, e.g., `clients.json`)** | **Short-Lived CLI Bearer Token (`az account get-access-token`, `gcloud auth print-identity-token`, or OIDC device-code helper).** The developer passes `Authorization: Bearer ${MCP_GATEWAY_TOKEN}` in `clients.json` ([`examples/integrations/mcp_clients/clients.json`](mcp_clients/clients.json)). | Carries the developer's real corporate identity and groups. |
| **D. Downstream MCP Server Has No OIDC/Entra Support** | Configure the upstream in `config/upstreams.yaml` with `auth_type: bearer_secret` or `auth_type: api_key` (injected from `secret_env_var`). | **Gateway still enforces user RBAC & ABAC at the front door** using the caller's JWT, and then injects the service API key on the backend hop so the raw API key is never exposed to the agent. |

---

## 3. Agent & Model Self-Identification on First Connect

An agent harness can identify **both its model and the user's intent on the very first HTTP call** (`initialize`, `tools/list`, or `tools/call`) so MCPcodeaway can run zero-round-trip discovery and model-aware Classic vs. Code Mode routing:

1. **HTTP Headers on `POST /mcp`:**
   - `X-Model-Name: qwen/qwen3.5-9b` (or `gemini-2.5-flash`, `claude-sonnet-4-5`, etc.) — tells the gateway which LLM is driving the agent.
   - `X-Intent: Reconcile overdue EU and US invoices` — triggers zero-round-trip `x_intent` discovery during `tools/list`.
2. **Standard MCP `initialize` Handshake (`params.clientInfo`):**
   - Standard MCP clients send `params.clientInfo = {"name": "copilot-finance-agent", "version": "1.0", "model": "..."}` on `initialize`.
3. **JWT `azp` / `client_id` Claim:**
   - Even if the harness sends no custom headers, the gateway extracts `identity.client_id` from the OAuth token (`azp` / `appid`) and correlates every run in `/admin/analytics/usage`.
