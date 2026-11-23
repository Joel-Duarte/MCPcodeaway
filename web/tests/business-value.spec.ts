import { mount } from "@vue/test-utils";
import { Button } from "@ui-sdk/vue/button";
import { nextTick, ref } from "vue";
import { afterEach, expect, test, vi } from "vitest";
import BusinessValuePage from "~/pages/business-value.vue";

const report = {
  tenant_scope: "tenant-a", since: null, events_returned: 2, truncated: false,
  executions_returned: 2, executions_truncated: false, completed_count: 2,
  execution_status_counts: { active: 0, completed: 2, outcome_unknown: 0, failed: 0, expired: 0 },
  by_integration_workflow_day: [{
    integration_id: "support", integration_display_name: "Support", upstream_name: "support_api",
    selected_kind: "workflow", selected_id: "support_ticket", definition_revision: 3,
    period_utc: "2026-11-22", baseline_source: "sampled_manual_measurement", baseline_confidence: "sampled",
    currency: "EUR", completed_count: 2, estimated_manual_baseline_minutes: 12,
    validated_manual_baseline_minutes: null, estimated_minutes_saved: null,
    estimated_labor_value: { amount: 9.6, currency: "EUR" },
  }],
  totals_by_currency: { EUR: { completed_count: 2, estimated_manual_baseline_minutes: 24, validated_manual_baseline_minutes: 0, estimated_minutes_saved: null } },
  validated_minutes_saved: null,
  cost_coverage: { captured_categories: [], missing_categories: ["llm_inference", "gateway_operations", "upstream_or_vendor_costs"], roi_available: false },
};

const installComponents = () => ({ global: { components: { Button } } });

afterEach(() => vi.unstubAllGlobals());

test("business value report explains completed workflows, baselines, provenance, and unavailable ROI", async () => {
  const apiFetch = vi.fn().mockResolvedValue(report);
  vi.stubGlobal("useAdminApi", () => ({ apiFetch, session: ref({ is_admin: true }) }));
  const wrapper = mount(BusinessValuePage, installComponents());
  await vi.waitFor(() => expect(apiFetch).toHaveBeenCalledWith("/admin/business-value/report"));
  await nextTick();

  expect(wrapper.text()).toContain("Outcome report");
  expect(wrapper.text()).toContain("Completed outcomes");
  expect(wrapper.text()).toContain("Gross estimated baseline");
  expect(wrapper.text()).toContain("Validated baseline");
  expect(wrapper.text()).toContain("Validated minutes saved");
  expect(wrapper.text()).toContain("Dollar net savings and ROI unavailable");
  expect(wrapper.text()).toContain("Unavailable");
  expect(wrapper.text()).toContain("sampled manual measurement");
  expect(wrapper.text()).toContain("Gross baseline is not net savings");
  expect(wrapper.text()).not.toContain("Definition JSON");
  expect(wrapper.text()).not.toContain("Validate manifest");
  wrapper.unmount();
});

test("report exposes a neutral loading skeleton until the API responds", async () => {
  let resolveReport!: (value: typeof report) => void;
  const apiFetch = vi.fn(() => new Promise<typeof report>((resolve) => { resolveReport = resolve; }));
  vi.stubGlobal("useAdminApi", () => ({ apiFetch, session: ref({ is_admin: false }) }));
  const wrapper = mount(BusinessValuePage, installComponents());
  expect(wrapper.get('[aria-label="Loading business value report"]').exists()).toBe(true);
  resolveReport(report);
  await vi.waitFor(() => expect(wrapper.text()).toContain("Gross estimated baseline"));
  wrapper.unmount();
});

test("report has an empty state and actionable access error", async () => {
  const emptyReport = {
    ...report,
    executions_returned: 0,
    completed_count: 0,
    execution_status_counts: { active: 0, completed: 0, outcome_unknown: 0, failed: 0, expired: 0 },
    by_integration_workflow_day: [],
  };
  const apiFetch = vi.fn().mockResolvedValue(emptyReport);
  vi.stubGlobal("useAdminApi", () => ({ apiFetch, session: ref({ is_admin: false }) }));
  const wrapper = mount(BusinessValuePage, installComponents());
  await vi.waitFor(() => expect(wrapper.text()).toContain("No completed outcomes are available"));
  expect(wrapper.text()).toContain("current workflow population");
  wrapper.unmount();

  vi.stubGlobal("useAdminApi", () => ({ apiFetch: vi.fn().mockRejectedValue(new Error("permission denied")), session: ref({ is_admin: false }) }));
  const failed = mount(BusinessValuePage, installComponents());
  await vi.waitFor(() => expect(failed.get('[role="alert"]').text()).toContain("permission denied"));
  expect(failed.text()).toContain("Check your access or connection, then retry.");
  failed.unmount();
});
