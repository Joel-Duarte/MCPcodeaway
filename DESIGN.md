# DESIGN.md — MCPcodeaway Admin Console Design System

## 1. Visual World & Intent

- **Mode:** **Operate** (infrastructure control plane, analytics console, onboarding wizard). The interface disappears into the workflow so engineers and platform admins can verify runtime health, track group/user spend, A/B-test Classic vs. Code Mode, and onboard services accurately without visual noise.
- **Appearance:** Precision Light Control Plane (`#FAFAFA` canvas, `#FFFFFF` structured work surfaces, `#F4F4F5` navigation chrome) paired with crisp `1px solid #E4E4E7` borders. Built for sustained readability alongside terminal windows, cloud consoles, and spreadsheets under bright office lighting.

---

## 2. Semantic Palette Tokens

Never invent unaligned hex values, gradients, or neon glows. Use these semantic tokens:

| Role | Token / Hex | Usage |
|---|---|---|
| **Canvas** | `#FAFAFA` | Page background (`bg-zinc-50`) |
| **Surface Primary** | `#FFFFFF` | Forms, data tables, workspace panels (`bg-white`) |
| **Surface Chrome** | `#F4F4F5` | Sidebar, table headers, quiet secondary containers (`bg-zinc-100`) |
| **Border Hairline** | `#E4E4E7` | Structural dividers and container edges (`1px solid`, never thick) |
| **Text Primary** | `#09090B` | Headings, active labels, table cell primary text (`text-zinc-950`) |
| **Text Secondary** | `#52525B` | Descriptions, metadata, table column headers (`text-zinc-600`) |
| **Text Muted** | `#71717A` | Placeholders, secondary timestamps (`text-zinc-500`) |
| **Accent (Primary)** | `#0F766E` / Hover `#115E59` | Primary CTA buttons, active nav link, Code Mode efficiency bars (`teal-700`/`800`) |
| **Accent Tint** | `#F0FDFA` / Border `#99F6E4` | Active recommendation banner, selected toggle tint |
| **Status Success** | `#059669` (`#ECFDF5` bg) | Healthy worker, HMAC grounded, passed A/B check |
| **Status Warning** | `#D97706` (`#FFFBEB` bg) | Missing `outputSchema`, high Classic payload warning, drift alert |
| **Status Danger** | `#DC2626` (`#FEF2F2` bg) | Auth failure, SSRF block, failed execution, delete confirmation |
| **Comparison Classic** | `#2563EB` (`#EFF6FF` bg) | Strictly paired opposite Teal when comparing Classic vs. Code Mode arms |

---

## 3. Typography & Data Alignment

- **UI Font Family:** `'Inter', -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif` for all headings, labels, navigation, buttons, and table text.
- **Monospace (`'JetBrains Mono', ui-monospace, SFMono-Regular, monospace`):** Used **only** for code snippets, JSON payloads, API URLs, run IDs, schema digests, and raw token indicators — never as decorative styling on UI headings or navigation links.
- **Fixed Scale (No fluid `clamp()` headings):**
  - Page title: `1.25rem` (`20px`), `font-semibold`, `tracking-[-0.015em]`
  - Section title: `0.9375rem` (`15px`), `font-semibold`
  - Body / Table cell: `0.875rem` (`14px`) / `0.8125rem` (`13px`)
  - Compact label / Badge / Table column header: `0.75rem` (`12px`), `font-medium`
- **Tabular Numerals:** Apply `font-variant-numeric: tabular-nums` on every metric, token counter, latency ms column, percentage, CPU gauge, and USD cost figure so columns align vertically.

---

## 4. Layout, Components & Responsiveness

- **Shell:** Fixed `240px` left navigation sidebar on desktop (`>= 1024px`) + `56px` sticky top utility bar (`Gateway Profile`, `App Env`, `Catalog Version`, signed-in admin identity & logout). Below `1024px`, the sidebar collapses into an accessible slide-over drawer triggered by the top bar menu button.
- **Responsive Grids & Tables:**
  - Breakpoints: Mobile (`< 640px`, single-column stacked panels, `44px` minimum button/link tap height), Tablet (`640px–1023px`, 2-column summary grids), Desktop (`>= 1024px`, 12-column / 4-column operate grids).
  - Wrap every wide data table in `<div class="overflow-x-auto">` so `375px` viewports never suffer page-level horizontal scroll.
- **Elevation & Borders:** Flat hairline surfaces (`1px solid #E4E4E7`, `rounded-lg` `8px` panels / `6px` controls) by default; soft shadow (`0 4px 12px rgb(9 9 11 / 0.08)`) reserved strictly for slide-over drawers, dropdowns, and dialogs.
- **Consistent Control Vocabulary:**
  - Primary button: `#0F766E` bg, white text, `rounded-md`, `px-3.5 py-2`, disabled opacity `0.5`.
  - Secondary button: `#FFFFFF` bg, `1px solid #D4D4D8`, `#09090B` text, `#F4F4F5` hover.
  - Ghost / nav button: transparent bg, `#F4F4F5` hover, `#F0FDFA` + `#0F766E` active.
- **Motion:** Transitions limited to `150ms–200ms cubic-bezier(0.16, 1, 0.3, 1)` on hover, focus, drawer slide, and tab/filter state feedback.
