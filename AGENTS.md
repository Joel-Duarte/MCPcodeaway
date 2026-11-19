# Repository Instructions & UI Standards

## UI SDK Rule (Mandatory)

Before using `ui-sdk`, follow the `ui-sdk` skill if it's installed, and read `node_modules/@ui-sdk/vue/docs/llms.txt` (in `web/node_modules/@ui-sdk/vue/docs/llms.txt`). To build a new component, also search `node_modules/@ui-sdk/vue/docs/core.md` by heading, and read `rendering.md` next to it.

## Anti-Slop UI Craft & Visual Consistency Rules

All admin console and frontend edits in `web/` **must** conform to:
- [DESIGN.md](DESIGN.md) — Precision Light Operate Control Plane palette, fixed typography scale, `tabular-nums`, border/radius tokens, and layout structure.
- [web/AGENTS.md](web/AGENTS.md) — Non-negotiable anti-slop rules, state completeness checklists, responsive breakpoint requirements (`375px`, `768px`, `1280px+`), and verification gates.
- [.agents/rules/ui-craft.md](.agents/rules/ui-craft.md) — Workspace rule enforcing these standards on every UI task.

Do **not** bundle `web/` static assets into the barebones `production` gateway Docker target (`Dockerfile` target `gateway`), and do **not** bypass or modify npm release-age policies.
