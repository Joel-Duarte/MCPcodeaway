# Security Policy

## Supported Versions

Security updates are applied to the latest release and the `main` branch (`0.1.x`).

| Version | Supported |
| --- | --- |
| `0.1.x` / `main` | Yes |

## Reporting a Vulnerability

Please **do not** open a public GitHub issue for security vulnerabilities.

Instead, report vulnerabilities privately through [GitHub Security Advisories](https://github.com/Joel-Duarte/MCPcodeaway/security/advisories/new). Include:

- A clear description of the vulnerability and affected component(s)
- Reproduction steps or a minimal proof of concept
- Potential impact (e.g., auth bypass, SSRF, sandbox escape, cross-tenant leakage)

We aim to acknowledge reports within 48 hours and provide an initial remediation plan within 7 days.

## Security Architecture & Invariants

MCPcodeaway is designed around zero-trust, fail-closed defaults:

- **Control-Plane RBAC:** `/admin/*` routes enforce role-based authorization (`require_admin_auth=true` in staging/production).
- **OIDC & Delegated OBO Auth:** RS256 JWKS verification, strict tenant allowlisting, and RFC 8693 / Microsoft Entra ID OBO token exchange.
- **Sandboxed Code Mode:** Generated Python runs in an ephemeral, networkless `pydantic-monty` sandbox with step/memory/wall-time bounds and host-mediated callbacks.
- **SSRF Protection:** All admin- or caller-supplied URLs pass host/IP validation (`validate_upstream_url`) before outbound HTTP requests.
- **Audit Redaction & Provenance:** Sensitive fields pass through `OutputRedactor.redact_text()` before persistence, and responses can be verified against HMAC-SHA256 tool provenance envelopes.
- **CI Security Gates:** Pull requests and pushes are checked with Gitleaks, Zizmor, dependency audits (`uv audit`, `npm audit`), and CodeQL / dependency review (on public repositories).
