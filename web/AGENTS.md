# `web/` Frontend Engineering & Anti-Slop UI Rules

Before writing or modifying any Vue component or page here:
1. Follow the `ui-sdk` skill if installed, and read `node_modules/@ui-sdk/vue/docs/llms.txt`, `node_modules/@ui-sdk/vue/docs/core.md` (relevant component headings), and `node_modules/@ui-sdk/vue/docs/rendering.md`.
2. Read `../DESIGN.md` completely and treat its palette, fixed `rem` type scale, button hierarchy, and layout shell as hard invariants.
3. Preserve CSS layer ordering in `assets/css/main.css`:
   ```css
   @layer ui-sdk;
   @import "tailwindcss";
   @import "@ui-sdk/core/web.css" layer(components);
   ```

## Hard Anti-Slop Bans (Never Ship These)

- **No gradient text, neon glows, or dark glassmorphism.** Stay strictly within the Precision Light Operate palette (`#FAFAFA` canvas, `#FFFFFF` panels, `#0F766E` Deep Teal accent).
- **No decorative `backdrop-filter: blur()`** unless serving a real modal/drawer backdrop overlay.
- **No colored `border-left` or `border-right` stripes (`> 1px`)** on cards, callouts, or table rows.
- **No hard-offset neobrutalist block shadows (`4px 4px 0`)** or glowing colored box-shadows.
- **No eyebrow / kicker labels above headings** (`"SYSTEM TELEMETRY"` small caps above `<h1>` / `<h2>`). Headings speak for themselves.
- **No decorative `01 / 02 / 03` section numbering** unless ordering actual multi-step wizard stages where sequence carries meaning.
- **No nested cards inside cards.** Use `1px solid #E4E4E7` hairline dividers, whitespace, or table rows inside a single surface panel instead.
- **No generic AI "hero metric + icon + marketing blurb" card grids.** Present dense, scannable operational data with `tabular-nums`, units, and actionable deltas.
- **No emojis or unicode arrows standing in for an icon system.** Render uniform inline SVG icons (`16px–20px`, `1.5px` stroke weight) consistently.
- **No fake sparklines or empty progress rings** standing in for real telemetry. Every bar or chart must map directly to real API numbers (`ratio * 100%`).
- **No unnecessary modals.** Prefer inline drawers, split workspaces, or progressive panels for editing configs and inspecting A/B runs; reserve dialogs strictly for destructive confirmations or focused code/JSON inspection.

## Mandatory Quality & Responsiveness Checklist

- **All Interactive States Included:** Default, hover, `focus-visible` ring, active, disabled, loading skeleton (`animate-pulse` neutral block — no full-page centered spinners), actionable error banners (naming cause + next step), and clear empty states.
- **True Multi-Device Responsiveness:** Every page must render without horizontal page overflow at **mobile (`375×844`)**, **tablet (`768×1024`)**, and **desktop (`1280×800` / `1440×900`)**:
  - Top bar + slide-over navigation drawer on `< lg`, persistent compact sidebar on `lg+`.
  - Every data table wrapped in `overflow-x-auto` with readable `whitespace-nowrap` columns or responsive mobile summary rows.
  - Minimum `44px` tap targets on mobile buttons/links.
- **Purposeful Fast Motion Only:** `150ms–200ms` transitions strictly on state/drawer changes. Zero staggered page-load entrance animations.
- **Browser Surface Polish:** Explicitly style text selection (`bg-teal-700/15 text-teal-950`), `focus-visible` outline (`2px solid #0F766E`), and clean table scrollbars.
- **Verify After Every UI Change:** Run `npm run generate` inside `web/` (must exit `0` with zero warnings/errors) and visually inspect desktop + mobile breakpoints.
