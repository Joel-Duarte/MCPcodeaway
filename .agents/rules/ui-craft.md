# UI Craft & Anti-Slop Enforcement Rule

When creating, modifying, or reviewing any file under `web/` or HTML/CSS dashboard templates:
1. Read `/home/joel/Projects/MCPcodeaway/DESIGN.md` and `/home/joel/Projects/MCPcodeaway/web/AGENTS.md`, plus `web/node_modules/@ui-sdk/vue/docs/llms.txt`, `core.md`, and `rendering.md`.
2. Strictly enforce every banned slop pattern (no gradient text, no neon/purple hues, no decorative blur, no eyebrow/kicker headings, no nested cards, no `>1px` colored side borders, no fake metrics/emojis as icons, no fluid `clamp()` product headings).
3. Ensure every component is responsive across `375px` mobile, `768px` tablet, and `1280px+` desktop without horizontal page overflow, uses `tabular-nums` on all numeric/cost/CPU/token values, and passes `cd web && npm run generate`.
