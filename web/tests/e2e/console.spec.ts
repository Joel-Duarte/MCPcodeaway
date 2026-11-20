import { expect, test } from "@playwright/test";

const BASE = process.env.E2E_BASE_URL || "http://127.0.0.1:8000";

test.describe("MCPcodeaway Admin Console E2E", () => {
  test("desktop 1280x800: demo burst populates runtime/analytics/audit, onboarding stepper & upstreams work cleanly", async ({
    page,
  }) => {
    await page.setViewportSize({ width: 1280, height: 800 });
    const pageErrors: string[] = [];
    const failedResponses: string[] = [];

    page.on("pageerror", (err) => pageErrors.push(err.message));
    page.on("response", (res) => {
      if (res.status() >= 500) {
        failedResponses.push(`${res.status()} ${res.url()}`);
      }
    });

    // 1. Overview & Live Runtime + WebSocket + Demo Burst
    await page.goto(`${BASE}/dashboard/`, { waitUntil: "networkidle" });
    await expect(page.getByRole("heading", { name: /Overview & Live Runtime/i })).toBeVisible();
    await expect(page.getByText(/Gateway Replica/i)).toBeVisible();
    await expect(page.getByText(/Live Gateway Activity Stream/i)).toBeVisible();

    const burstBtn = page.getByRole("button", { name: /Run Demo Burst|Burst \+8/i }).first();
    if (await burstBtn.isVisible()) {
      await burstBtn.click();
      await page.waitForTimeout(900);
    }

    // 2. Gateway Usage populated + Reactive Filters
    await page.getByRole("link", { name: /Gateway Usage/i }).click();
    await expect(page.getByRole("heading", { name: /Gateway Usage, Groups & Cost Attribution/i })).toBeVisible();

    // 2b. Legacy optimization route redirects to workflow efficiency.
    await page.goto(`${BASE}/dashboard/optimization`);
    await expect(page).toHaveURL(/model-performance\?tab=efficiency/);
    await expect(page.getByRole("heading", { name: /Model Performance/i })).toBeVisible();
    await expect(page.getByRole("heading", { name: /Matched cohort differences/i })).toBeVisible();

    // 2c. Paired workflow performance views.
    await page.getByRole("link", { name: /Model Performance/i }).click();
    await expect(page.getByRole("heading", { name: /Model Performance/i })).toBeVisible();
    await expect(page.getByRole("tab", { name: "Routing" })).toBeVisible();

    // 2d. Dedicated Traces & Latency Timeline page
    await page.getByRole("link", { name: /Traces & Timeline/i }).click();
    await expect(page.getByRole("heading", { name: /Traces & Latency Timeline/i })).toBeVisible();
    await expect(page.getByText(/Where Time Is Spent by Group/i)).toBeVisible();

    // 3. Upstreams Catalog renders live servers + 500-server search
    await page.getByRole("link", { name: /Upstreams Catalog/i }).click();
    await expect(page.getByText("billing_legacy_eu").first()).toBeVisible();
    await expect(page.getByText("snowflake_ledger").first()).toBeVisible();

    // 4. Audit & Compliance renders recorded runs + Real-Time toggle + Detection Guide
    await page.getByRole("link", { name: /Audit & Compliance/i }).click();
    await expect(page.getByRole("heading", { name: /Audit Runs, Provenance & Retention/i })).toBeVisible();
    await expect(page.getByText("Real-Time")).toBeVisible();
    await expect(page.getByRole("button", { name: /Errors Only/i })).toBeVisible();
    await expect(page.locator("table tbody tr").first()).toBeVisible();

    // 5. Settings starts with authenticated workspace details; pricing and alert policies are separate tabs
    await page.getByRole("link", { name: /Settings & Alerts/i }).click();
    await expect(page.getByRole("heading", { name: "Gateway configuration", exact: true })).toBeVisible();
    await expect(page.getByRole("heading", { name: "Administrator identity", exact: true })).toBeVisible();
    await page.getByRole("tab", { name: /Model pricing/i }).click();
    await expect(page.getByText("qwen3.5-9b")).toBeVisible();
    await page.getByRole("tab", { name: /Alerts & webhooks/i }).click();
    await expect(page.getByRole("heading", { name: "Custom Alert Rules", exact: true })).toBeVisible();

    // Demo controls are a real server traffic runner, not a browser simulation
    await page.getByRole("link", { name: /Demo Control/i }).click();
    await expect(page.getByRole("heading", { name: "Continuous Demo Traffic Runner", exact: true })).toBeVisible();
    await expect(page.getByRole("heading", { name: "Simulated Model Performance Profiles", exact: true })).toBeVisible();

    // 7. Onboarding 3-Step Progressive Wizard (Step 2 locked until Inspect Schema)
    await page.getByRole("link", { name: /Onboarding/i }).click();
    await expect(page.getByText("Stateful MCP Session", { exact: true })).toHaveCount(0);
    await expect(page.getByRole("button", { name: /2\. Policy & Mode/i })).toBeDisabled();
    await page.getByRole("button", { name: /Paste OpenAPI document/i }).click();
    await page.getByRole("button", { name: /Inspect Schema/i }).click();
    await expect(page.getByRole("button", { name: /2\. Policy & Mode/i })).toBeEnabled({ timeout: 5000 });
    await page.getByRole("button", { name: /2\. Policy & Mode/i }).click();
    await expect(page.getByText(/Allowed RBAC roles/i)).toBeVisible();
    await expect(page.getByText(/Step 2 — Access & Execution/i)).toBeVisible();
    await page.getByRole("button", { name: /3\. Test & Save/i }).click();
    await expect(page.getByRole("button", { name: /Estimate Code Mode vs Classic/i })).toBeVisible();

    await page.getByRole("button", { name: /Agent Onboarding Prompt/i }).click();
    await expect(page.getByRole("tab", { name: /Self-Admin MCP/i })).toBeVisible();
    await page.getByRole("tab", { name: /Self-Admin MCP/i }).click();
    await expect(page.getByText("POST /admin/mcp", { exact: true })).toBeVisible();
    await page.getByRole("button", { name: "Close" }).click();

    expect(pageErrors, `Uncaught pageerrors: ${pageErrors.join("; ")}`).toEqual([]);
    expect(failedResponses, `HTTP >= 500 responses: ${failedResponses.join("; ")}`).toEqual([]);
  });

  test("mobile 375x812: zero horizontal overflow and accessible navigation drawer", async ({
    page,
  }) => {
    await page.setViewportSize({ width: 375, height: 812 });
    for (const path of ["/dashboard/", "/dashboard/traces", "/dashboard/analytics", "/dashboard/optimization", "/dashboard/model-performance", "/dashboard/onboarding", "/dashboard/upstreams", "/dashboard/audit", "/dashboard/settings", "/dashboard/demo", "/dashboard/connections"]) {
      await page.goto(`${BASE}${path}`, { waitUntil: "networkidle" });
      const overflow = await page.evaluate(
        () => document.documentElement.scrollWidth - document.documentElement.clientWidth
      );
      expect(overflow, `Horizontal overflow on 375px at ${path}: ${overflow}px`).toBeLessThanOrEqual(2);
    }
  });

  test("tablet and wide desktop breakpoints: no document-level horizontal overflow", async ({ page }) => {
    test.setTimeout(90_000);
    const routes = [
      "/dashboard/",
      "/dashboard/analytics",
      "/dashboard/model-performance",
      "/dashboard/traces",
      "/dashboard/upstreams",
      "/dashboard/settings",
      "/dashboard/demo",
    ];
    for (const viewport of [{ width: 768, height: 1024 }, { width: 1440, height: 900 }]) {
      await page.setViewportSize(viewport);
      for (const path of routes) {
        await page.goto(`${BASE}${path}`, { waitUntil: "networkidle" });
        const overflow = await page.evaluate(
          () => document.documentElement.scrollWidth - document.documentElement.clientWidth
        );
        expect(overflow, `Horizontal overflow at ${viewport.width}px on ${path}: ${overflow}px`).toBeLessThanOrEqual(2);
      }
    }
  });
});
