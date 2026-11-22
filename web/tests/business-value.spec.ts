import { mount } from "@vue/test-utils";
import { createAgent } from "@ui-sdk/vue/agent";
import { Button } from "@ui-sdk/vue/button";
import { Field, FieldLabel } from "@ui-sdk/vue/field";
import { Input } from "@ui-sdk/vue/input";
import { Select, SelectContent, SelectItem, SelectLabel, SelectTrigger, SelectValue } from "@ui-sdk/vue/select";
import { Textarea } from "@ui-sdk/vue/textarea";
import { nextTick, ref } from "vue";
import { afterEach, expect, test, vi } from "vitest";
import BusinessValuePage from "~/pages/business-value.vue";

Element.prototype.getAnimations = () => [];
Element.prototype.checkVisibility = () => true;
Element.prototype.getClientRects = () => [] as unknown as DOMRectList;
document.elementFromPoint = () => null;
document.elementsFromPoint = () => [];

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

const definition = {
  manifest_version: "1.0",
  integration: { id: "support", display_name: "Support", upstream_name: "support_api", status: "draft", revision: 1 },
  tool_refs: [], experiences: [], workflows: [], standalone_tool_values: [],
};

function installComponents() {
  return {
    global: { components: { Button, Field, FieldLabel, Input, Select, SelectContent, SelectItem, SelectLabel, SelectTrigger, SelectValue, Textarea } },
  };
}

afterEach(() => vi.unstubAllGlobals());

test("business value report shows attributable data, provenance, and unavailable net/ROI values", async () => {
  const apiFetch = vi.fn().mockResolvedValue(report);
  vi.stubGlobal("useAdminApi", () => ({ apiFetch, session: ref({ is_admin: true }) }));
  const wrapper = mount(BusinessValuePage, installComponents());
  await vi.waitFor(() => expect(apiFetch).toHaveBeenCalledWith("/admin/business-value/report"));
  await nextTick();

  expect(wrapper.text()).toContain("Completed outcomes");
  expect(wrapper.text()).toContain("Gross estimated baseline");
  expect(wrapper.text()).toContain("Validated baseline");
  expect(wrapper.text()).toContain("Net savings / ROI");
  expect(wrapper.text()).toContain("Unavailable");
  expect(wrapper.text()).toContain("sampled manual measurement");
  expect(wrapper.text()).toContain("Gross baseline is not net savings");
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

test("report has explicit empty and actionable error states", async () => {
  const apiFetch = vi.fn().mockResolvedValue({ ...report, executions_returned: 0, completed_count: 0, execution_status_counts: { active: 0, completed: 0, outcome_unknown: 0, failed: 0, expired: 0 }, by_integration_workflow_day: [] });
  vi.stubGlobal("useAdminApi", () => ({ apiFetch, session: ref({ is_admin: false }) }));
  const wrapper = mount(BusinessValuePage, installComponents());
  await vi.waitFor(() => expect(wrapper.text()).toContain("No completed outcomes are available"));
  expect(wrapper.text()).toContain("Read-only access");
  wrapper.unmount();

  vi.stubGlobal("useAdminApi", () => ({ apiFetch: vi.fn().mockRejectedValue(new Error("permission denied")), session: ref({ is_admin: false }) }));
  const failed = mount(BusinessValuePage, installComponents());
  await vi.waitFor(() => expect(failed.get('[role="alert"]').text()).toContain("permission denied"));
  expect(failed.text()).toContain("Check your admin or viewer access and retry");
  failed.unmount();
});

test("administrator can validate, save, publish and roll back a definition", async () => {
  const detail = {
    upstream_name: "support_api", current_revision: "rev-1",
    current: { definition },
    versions: [
      { revision: "rev-2", status: "draft", created_at: "2026-11-22T10:00:00Z", is_current: false },
      { revision: "rev-1", status: "published", created_at: "2026-11-21T10:00:00Z", is_current: true },
    ],
  };
  const apiFetch = vi.fn(async (path: string) => {
    if (path === "/admin/business-value/report") return report;
    if (path.endsWith("/validate")) return { valid: true, upstream_name: "support_api", definition_digest: "cafe", tool_count: 4, workflow_count: 1, standalone_tool_value_count: 0 };
    if (path.endsWith("/drafts")) return { revision: "rev-2", definition_digest: "cafe" };
    if (path === "/admin/integration-definitions/support_api") return detail;
    if (path.endsWith("/publish")) return { revision: "rev-2", previous_revision: "rev-1" };
    if (path.endsWith("/rollback")) return { revision: "rev-3", previous_revision: "rev-2" };
    throw new Error(`Unexpected API request: ${path}`);
  });
  vi.stubGlobal("useAdminApi", () => ({ apiFetch, session: ref({ is_admin: true }) }));
  const agent = createAgent();
  const wrapper = mount(BusinessValuePage, { attachTo: document.body, ...installComponents() });
  await vi.waitFor(() => expect(apiFetch).toHaveBeenCalledWith("/admin/business-value/report"));
  await nextTick();

  const snapshot = agent.snapshot().filter((entry) => !entry.offPage && entry.component !== "more");
  for (const control of snapshot) expect(control.label, `${control.component} lacks a name`).toBeTruthy();
  const labels = snapshot.map((entry) => entry.label);
  for (const label of ["Definition JSON", "Upstream name", "Validate manifest", "Save draft", "Load versions"]) {
    expect(labels).toContain(label);
  }

  const setManifest = await agent.act({ steps: [{ ref: "Definition JSON", action: "setValue", args: [JSON.stringify(definition)] }] });
  expect(setManifest.ok, setManifest.error?.message).toBe(true);
  const validate = await agent.act({ steps: [{ ref: "Validate manifest", action: "press" }] });
  expect(validate.ok, validate.error?.message).toBe(true);
  await vi.waitFor(() => expect(wrapper.text()).toContain("Valid · 4 tools · 1 workflows"));
  const save = await agent.act({ steps: [{ ref: "Save draft", action: "press" }] });
  expect(save.ok, save.error?.message).toBe(true);
  await vi.waitFor(() => expect(wrapper.text()).toContain("Draft rev-2 saved"));
  const load = await agent.act({ steps: [{ ref: "Load versions", action: "press" }] });
  expect(load.ok, load.error?.message).toBe(true);
  await vi.waitFor(() => expect(wrapper.text()).toContain("Revision history"));
  const revisionSelect = agent.snapshot().find((entry) => entry.label === "Choose a revision" && !entry.offPage);
  expect(revisionSelect).toBeDefined();
  const chooseDraft = await agent.act({ steps: [{ ref: revisionSelect!.ref, action: "setValue", args: ["rev-2"] }] });
  expect(chooseDraft.ok, chooseDraft.error?.message).toBe(true);
  const publish = await agent.act({ steps: [{ ref: "Publish revision", action: "press" }] });
  expect(publish.ok, publish.error?.message).toBe(true);
  await vi.waitFor(() => expect(wrapper.text()).toContain("Published rev-2"));
  const rollbackChoice = agent.snapshot().find((entry) => entry.label === "Choose a revision" && !entry.offPage);
  const chooseRollbackTarget = await agent.act({ steps: [{ ref: rollbackChoice!.ref, action: "setValue", args: ["rev-2"] }] });
  expect(chooseRollbackTarget.ok, chooseRollbackTarget.error?.message).toBe(true);
  const rollback = await agent.act({ steps: [{ ref: "Roll back to revision", action: "press" }] });
  expect(rollback.ok, rollback.error?.message).toBe(true);
  await vi.waitFor(() => expect(wrapper.text()).toContain("Rolled back to rev-3"));

  expect(apiFetch).toHaveBeenCalledWith("/admin/integration-definitions/validate", expect.objectContaining({ method: "POST", body: definition }));
  expect(apiFetch).toHaveBeenCalledWith("/admin/integration-definitions/drafts", expect.objectContaining({ method: "POST", body: { definition } }));
  expect(apiFetch).toHaveBeenCalledWith("/admin/integration-definitions/support_api/publish", expect.objectContaining({ method: "POST", body: { revision: "rev-2" } }));
  expect(apiFetch).toHaveBeenCalledWith("/admin/integration-definitions/support_api/rollback", expect.objectContaining({ method: "POST", body: { revision: "rev-2" } }));
  wrapper.unmount();
});

test("invalid JSON blocks server actions with an inline correction message", async () => {
  const apiFetch = vi.fn().mockResolvedValue(report);
  vi.stubGlobal("useAdminApi", () => ({ apiFetch, session: ref({ is_admin: true }) }));
  const wrapper = mount(BusinessValuePage, { attachTo: document.body, ...installComponents() });
  await vi.waitFor(() => expect(apiFetch).toHaveBeenCalled());
  await wrapper.findComponent(Textarea).setValue("{");
  await wrapper.findAll("button").find((button) => button.text() === "Validate manifest")!.trigger("click");
  await nextTick();
  expect(wrapper.get('[role="alert"]').text()).toContain("Invalid JSON");
  expect(apiFetch).toHaveBeenCalledTimes(1);
  wrapper.unmount();
});
