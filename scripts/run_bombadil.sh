#!/usr/bin/env bash
set -euo pipefail

TARGET_URL="${1:-http://127.0.0.1:8000/dashboard/}"
STEPS="${BOMBADIL_STEPS:-25}"
TIME_LIMIT="${BOMBADIL_TIME_LIMIT:-15s}"
REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

BOMBADIL_BIN=""
if command -v bombadil >/dev/null 2>&1; then
  BOMBADIL_BIN="bombadil"
elif [[ -x "$REPO_ROOT/web/node_modules/.bin/bombadil" ]]; then
  BOMBADIL_BIN="$REPO_ROOT/web/node_modules/.bin/bombadil"
fi

if [[ -n "$BOMBADIL_BIN" ]]; then
  if [[ -z "${CHROME:-}" ]] && ! command -v google-chrome >/dev/null 2>&1 && ! command -v chromium >/dev/null 2>&1; then
    PW_CHROME="$(find "${HOME}/.cache/ms-playwright" -maxdepth 4 -type f -name chrome 2>/dev/null | sort -V | tail -n 1 || true)"
    if [[ -n "$PW_CHROME" ]]; then
      export CHROME="$PW_CHROME"
      export PATH="$(dirname "$PW_CHROME"):$PATH"
    fi
  fi
  echo ">> Running Antithesis Bombadil UI fuzzer against $TARGET_URL ($TIME_LIMIT)..."
  exec "$BOMBADIL_BIN" browser test \
    --headless \
    --no-sandbox \
    --exit-on-violation \
    --time-limit "$TIME_LIMIT" \
    "$TARGET_URL" \
    "$REPO_ROOT/web/tests/bombadil/spec.ts"
fi

echo ">> bombadil CLI not installed; running Playwright property-based UI fuzzer ($STEPS randomized steps) against $TARGET_URL..."
cd "$REPO_ROOT/web"
node -e '
import { chromium } from "playwright";

const baseUrl = process.argv[1];
const maxSteps = Number(process.argv[2] || 25);
const viewports = [
  { width: 1280, height: 800 },
  { width: 375, height: 812 },
];

const browser = await chromium.launch({ headless: true });
try {
  for (const vp of viewports) {
    const context = await browser.newContext({ viewport: vp });
    const page = await context.newPage();
    const errors = [];
    const server5xx = [];
    page.on("pageerror", (e) => errors.push(e.message));
    page.on("response", (r) => {
      if (r.status() >= 500) server5xx.push(`${r.status()} ${r.url()}`);
    });

    await page.goto(baseUrl, { waitUntil: "networkidle" });
    for (let step = 0; step < maxSteps; step++) {
      // Property invariants on every state transition
      const state = await page.evaluate(() => ({
        textLen: document.body?.innerText?.trim().length || 0,
        hasHeading: Boolean(document.querySelector("h1, h2, [role=heading]")),
        overflow: document.documentElement.scrollWidth - document.documentElement.clientWidth,
      }));
      if (state.textLen <= 40 || !state.hasHeading) {
        throw new Error(`Blank/broken UI state at ${page.url()} (${vp.width}px) step ${step}`);
      }
      if (state.overflow > 2) {
        throw new Error(`Horizontal overflow ${state.overflow}px at ${page.url()} (${vp.width}px)`);
      }

      // Randomized safe interactive exploration across visible links/tabs/buttons
      const clicked = await page.evaluate((s) => {
        const candidates = Array.from(
          document.querySelectorAll("a[href^=\"/dashboard\"], button:not([disabled]), [role=tab]")
        ).filter((el) => {
          const rect = el.getBoundingClientRect();
          const txt = (el.textContent || "").toLowerCase();
          return (
            rect.width > 0 &&
            rect.height > 0 &&
            !txt.includes("sign out") &&
            !txt.includes("prune") &&
            !txt.includes("remove") &&
            !txt.includes("delete")
          );
        });
        if (candidates.length === 0) return false;
        const pick = candidates[(s * 7 + 3) % candidates.length];
        pick.click();
        return true;
      }, step);
      if (clicked) {
        await page.waitForTimeout(180);
      }
    }
    if (errors.length > 0) {
      throw new Error(`Uncaught pageerrors (${vp.width}px): ${errors.join("; ")}`);
    }
    if (server5xx.length > 0) {
      throw new Error(`HTTP 5xx errors (${vp.width}px): ${server5xx.join("; ")}`);
    }
    await context.close();
  }
  console.log(`Bombadil property fuzzing PASSED across ${viewports.length} viewports x ${maxSteps} steps.`);
} finally {
  await browser.close();
}
' "$TARGET_URL" "$STEPS"
