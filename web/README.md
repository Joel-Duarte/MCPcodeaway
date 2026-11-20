# MCPcodeaway Admin Console (`web/`)

Responsive Nuxt 4 + Vue 3.5+ + `@ui-sdk/vue` + Tailwind CSS v4 control-plane UI for MCPcodeaway.

## Governance & Design Standards

- Conform strictly to [../DESIGN.md](../DESIGN.md) (Precision Light Operate palette, typography scale, `tabular-nums`, and layout shell) and [AGENTS.md](AGENTS.md).
- Follow the mandatory `@ui-sdk/vue` usage rules in [../AGENTS.md](../AGENTS.md) before modifying components.

## Setup & Commands

Use Node.js `22.22.3` or newer within the supported Nuxt engine range (`^22.22.3 || ^24.15.0 || >=26.0.0`). The repository includes `.nvmrc` for Node version managers.

```bash
# Select the project runtime (with nvm)
nvm use

# Install dependencies cleanly
npm ci

# Run dev server (proxies API routes to http://127.0.0.1:8000)
npm run dev

# Build static SPA into .output/public (served at /dashboard by gateway-ui / gateway-demo)
NUXT_TELEMETRY_DISABLED=1 npm run generate
```

## Standalone Frontend Deployment

When hosting `web/.output/public` independently from the Python gateway containers:
- Set `NUXT_PUBLIC_API_BASE=https://gateway.example.com` before running `npm run generate`.
- Configure `GATEWAY_CORS_ALLOWED_ORIGINS='["https://console.example.com"]'` (and appropriate `admin_cookie_samesite` settings) on the gateway replicas.
