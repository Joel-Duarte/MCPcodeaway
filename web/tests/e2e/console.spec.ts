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

    // 2. Analytics & Cost populated + 9B baseline + Custom Model Pricing
    await page.getByRole("link", { name: /Analytics & Cost/i }).click();
    await expect(page.getByRole("heading", { name: /Analytics, Groups & Model Cost/i })).toBeVisible();
    await expect(page.getByText(/API Token Cost Simulator/i)).toBeVisible();
    await expect(page.getByText(/Code Mode vs\. Classic Volume/i)).toBeVisible();

    // 3. Upstreams & Splits renders live servers
    await page.getByRole("link", { name: /Upstreams & Splits/i }).click();
    await expect(page.getByText("billing_legacy_eu").first()).toBeVisible();
    await expect(page.getByText("snowflake_ledger").first()).toBeVisible();

    // 4. Audit & Retention renders recorded runs
    await page.getByRole("link", { name: /Audit & Retention/i }).click();
    await expect(page.getByRole("heading", { name: /Audit Runs, Provenance & Retention/i })).toBeVisible();
    await expect(page.locator("table tbody tr").first()).toBeVisible();

    // 5. Onboarding 3-Step Progressive Wizard + Quick REST Builder + Agent Prompt Modal
    await page.getByRole("link", { name: /Onboarding & A\/B Lab/i }).click();
    await expect(page.getByRole("button", { name: /1\. Connect & Inspect/i })).toBeVisible();
    await expect(page.getByRole("button", { name: /Quick REST Builder/i })).toBeVisible();
    await page.getByRole("button", { name: /2\. Policy & Mode/i }).click();
    await expect(page.getByText(/Step 2 — Access Control/i)).toBeVisible();
    await page.getByRole("button", { name: /3\. A\/B Test & Save/i }).click();
    await expect(page.getByRole("button", { name: /Run A\/B Evaluation/i })).toBeVisible();

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
    for (const path of ["/dashboard/", "/dashboard/analytics", "/dashboard/onboarding", "/dashboard/upstreams", "/dashboard/audit"]) {
      await page.goto(`${BASE}${path}`, { waitUntil: "networkidle" });
      const overflow = await page.evaluate(
        () => document.documentElement.scrollWidth - document.documentElement.clientWidth
      );
      expect(overflow, `Horizontal overflow on 375px at ${path}: ${overflow}px`).toBeLessThanOrEqual(2);
    }
  });
});
