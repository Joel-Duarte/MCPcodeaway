# Repository Instructions & UI Standards

## UI SDK Rule (Mandatory)

Before using `ui-sdk`, follow the `ui-sdk` skill if it's installed, and read `node_modules/@ui-sdk/vue/docs/llms.txt` (in `web/node_modules/@ui-sdk/vue/docs/llms.txt`). To build a new component, also search `node_modules/@ui-sdk/vue/docs/core.md` by heading, and read `rendering.md` next to it.

## Anti-Slop UI Craft & Visual Consistency Rules

All admin console and frontend edits in `web/` **must** conform to:
- [DESIGN.md](DESIGN.md) — Precision Light Operate Control Plane palette, fixed typography scale, `tabular-nums`, border/radius tokens, and layout structure.
- [web/AGENTS.md](web/AGENTS.md) — Non-negotiable anti-slop rules, state completeness checklists, responsive breakpoint requirements (`375px`, `768px`, `1280px+`), and verification gates.
- [.agents/rules/ui-craft.md](.agents/rules/ui-craft.md) — Workspace rule enforcing these standards on every UI task.

Do **not** bundle `web/` static assets into the barebones `production` gateway Docker target (`Dockerfile` target `gateway`), and do **not** bypass or modify npm release-age policies.

## Code Navigation & Pre-CI Standards

- **Two-Tier Navigation (`Graphify` + `Ripwire`):** Prefer `ripwire . --for="<symbol>"`, `--callers`, `--uses`, `--impact`, and post-edit `ripwire . --situ` for deterministic AST symbol/blast-radius checks (`<100ms`), and `graphify query` / `graphify path` (`graphify-out/` kept untracked) for macro cross-module queries; fall back to `rg` only for exact literals, configs, or Vue templates.
- **Pre-CI Gates & Working Conditions:**
  - Run `./scripts/pre-ci.sh --fast` during iteration and `./scripts/pre-ci.sh --full` before commits/PRs (`scripts/agent_preflight.py`, `ruff format --check`, `ruff check`, `pytest`, `web/` `npm run generate`, and `benchmarks/run_benchmark.py --verify-thresholds`).
  - Never add bare `# noqa`, bare `# type: ignore`, unjustified `eslint-disable`, or `src/` imports of `tests`. Keep `.agents/` local-only (never commit new `.agents/` additions).
  - Keep `tests/golden/flows.json` and `benchmarks/thresholds.json` strictly additive; stop and escalate after 3 consecutive identical failures or 2 failed identical approaches.
