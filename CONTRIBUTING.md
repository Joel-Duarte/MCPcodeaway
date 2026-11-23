# Contributing to MCPcodeaway

Thank you for your interest in contributing to **MCPcodeaway**!

## Development Setup

### Prerequisites

- **Python 3.12+** and [`uv`](https://docs.astral.sh/uv/)
- **Node.js 22+** and `npm` (for the Nuxt 4 + `@ui-sdk/vue` console in `web/`)
- **Docker** (optional, for container builds and Postgres/Redis integration tests)

### Install Dependencies

```bash
git clone https://github.com/Joel-Duarte/MCPcodeaway.git
cd MCPcodeaway

# Backend (including dev and PostgreSQL test dependencies)
uv sync --extra dev --extra postgres

# Frontend control plane
cd web && npm ci && cd ..
```

### Build the Admin Console & Run Locally

```bash
# Build static SPA assets served at /dashboard
(cd web && npm run generate)

# Terminal 1: start mock enterprise backends
uv run uvicorn examples.mock_backends:app --port 8001

# Terminal 2: start the gateway in demo profile
GATEWAY_PROFILE=demo uv run uvicorn gateway.server:app --port 8000
```

## Verification & Pre-CI Gates

Run the local verification suite before opening a pull request:

```bash
# Fast gate: preflight checks, Ruff lint/format, golden E2E flows
./scripts/pre-ci.sh --fast

# Full gate: complete pytest suite, Vitest unit tests, Nuxt build, locked benchmarks
./scripts/pre-ci.sh --full
```

To run Playwright end-to-end tests against a live gateway:

```bash
cd web
npx playwright install --with-deps chromium
npm run test:e2e
```

## Coding & Quality Standards

- **Formatting & Linting:** Python code is formatted and linted with `ruff` (`line-length = 100`, Python 3.12 target). Run `uv run ruff format` and `uv run ruff check` before committing.
- **Preflight Policy:** `scripts/agent_preflight.py` forbids bare `# noqa`, bare `# type: ignore`, unjustified `eslint-disable`, `src/` imports from `tests/`, and frontend/backend property drift.
- **Security Invariants:**
  - Every `/admin/*` endpoint must enforce control-plane RBAC (`_authorize_control_plane`).
  - Every external URL must pass `validate_upstream_url`.
  - Persisted audit payloads must pass through `OutputRedactor.redact_text()`.
- **UI Standards:** Frontend changes in `web/` must follow [DESIGN.md](DESIGN.md) and [web/AGENTS.md](web/AGENTS.md).
- **Golden Flows & Benchmarks:** Keep `tests/golden/flows.json` and `benchmarks/thresholds.json` strictly additive unless intentionally updating locked baselines.

## Pull Request Process

1. Fork the repository and create a topic branch from `main`.
2. Ensure `./scripts/pre-ci.sh --full` passes locally.
3. Add or update unit/golden tests and documentation for behavioral changes.
4. Open a pull request using the PR template and verify all GitHub Actions checks pass.
