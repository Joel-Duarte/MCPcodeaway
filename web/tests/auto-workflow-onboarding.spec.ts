import { mount } from "@vue/test-utils";
import { Button } from "@ui-sdk/vue/button";
import { defineComponent, h } from "vue";
import { expect, test, vi } from "vitest";
import BusinessValuePage from "~/pages/business-value.vue";
import UpstreamWorkflowCatalog from "~/components/UpstreamWorkflowCatalog.vue";

const NuxtLinkStub = defineComponent({
  name: "NuxtLink",
  props: ["to"],
  render() {
    return h("a", { href: this.to }, this.$slots.default?.());
  },
});

test("business-value page renders auto-detected episode activity and pending baseline count", async () => {
  const apiFetch = vi.fn(async (path: string) => {
    if (path === "/admin/business-value/report") {
      return {
        tenant_scope: "tenant-acme-dev",
        since: null,
        events_returned: 1,
        truncated: false,
        executions_returned: 1,
        executions_truncated: false,
        completed_count: 1,
        execution_status_counts: { active: 0, completed: 1, outcome_unknown: 0, failed: 0, expired: 0 },
        attribution_mode_counts: { passive_matched: 1, explicit_legacy: 0 },
        episode_summary: {
          total: 3,
          status_counts: { completed_verified: 2, completed_unverified: 1 },
          pending_baseline_candidates: 2,
        },
        by_integration_workflow_day: [],
        totals_by_currency: {},
        validated_minutes_saved: null,
        cost_coverage: { captured_categories: [], missing_categories: ["llm_inference"], roi_available: false },
      };
    }
    throw new Error(`Unexpected ${path}`);
  });
  vi.stubGlobal("useAdminApi", () => ({ apiFetch }));
  const wrapper = mount(BusinessValuePage, {
    global: { components: { Button, NuxtLink: NuxtLinkStub } },
  });
  await vi.waitFor(() => expect(wrapper.text()).toContain("Auto-detected workflow activity"));
  expect(wrapper.text()).toContain("Passive episodes observed");
  expect(wrapper.text()).toContain("Pending baseline candidates");
  expect(wrapper.text()).toContain("Review & confirm baselines");
  expect(wrapper.find('a[href="/workflows"]').exists()).toBe(true);
  expect(wrapper.find('a[href="/onboarding"]').exists()).toBe(false);
  wrapper.unmount();
});

test("upstream workflow catalog can auto-discover candidate drafts", async () => {
  const apiFetch = vi.fn(async (path: string, options?: { method?: string }) => {
    if (path === "/admin/integration-definitions/hr_directory") return { current: null };
    if (path === "/admin/integration-definitions/hr_directory/discover" && options?.method === "POST") {
      return {
        candidates: [
          {
            candidate_id: "cand-1",
            workflow_slug: "update_hr_employee_workflow",
            origin: "auto_schema",
            observed_count: 1,
            draft_definition: {
              id: "update_hr_employee_workflow",
              name: "Update hr employee workflow",
              description: "Auto-synthesized from hr_directory tool schemas",
              participating_tools: ["hr_directory__get_hr_employee", "hr_directory__update_hr_employee"],
              required_evidence: [{ tool: "hr_directory__get_hr_employee", min_successful_calls: 1 }],
              completion: { tool: "hr_directory__get_hr_employee", success_when: { path: "/version", type: "integer" } },
            },
          },
        ],
      };
    }
    throw new Error(`Unexpected ${path}`);
  });
  vi.stubGlobal("useAdminApi", () => ({ apiFetch }));
  const wrapper = mount(UpstreamWorkflowCatalog, {
    props: {
      upstream: "hr_directory",
      tools: ["hr_directory__get_hr_employee", "hr_directory__update_hr_employee"],
      canEdit: true,
      refreshVersion: 0,
    },
    global: {
      components: { Button },
      stubs: { Drawer: true, DrawerBackdrop: true, DrawerClose: true, DrawerContent: true, DrawerDescription: true, DrawerTitle: true, Field: true, FieldDescription: true, FieldLabel: true, Input: true, Select: true, SelectContent: true, SelectItem: true, SelectItemIndicator: true, SelectTrigger: true, SelectValue: true, Textarea: true, AppIcon: true, Checkbox: true, CheckboxIndicator: true },
    },
  });
  await vi.waitFor(() => expect(wrapper.text()).toContain("Auto-discover workflows"));
  const discoverBtn = wrapper.findAll("button").find((b) => b.text().includes("Auto-discover workflows"))!;
  await discoverBtn.trigger("click");
  await vi.waitFor(() => expect(wrapper.text()).toContain("Update hr employee workflow"));
  expect(wrapper.text()).toContain("Review & set baseline");
  wrapper.unmount();
});

test("upstream workflow catalog shows inline discoverError without losing loaded catalog", async () => {
  const apiFetch = vi.fn(async (path: string, options?: { method?: string }) => {
    if (path === "/admin/integration-definitions/hr_directory") return { current: null };
    if (path === "/admin/integration-definitions/hr_directory/discover" && options?.method === "POST") {
      throw new Error("Discovery temporarily unavailable");
    }
    throw new Error(`Unexpected ${path}`);
  });
  vi.stubGlobal("useAdminApi", () => ({ apiFetch }));
  const wrapper = mount(UpstreamWorkflowCatalog, {
    props: {
      upstream: "hr_directory",
      tools: ["hr_directory__get_hr_employee"],
      canEdit: true,
      refreshVersion: 0,
    },
    global: {
      components: { Button },
      stubs: { Drawer: true, DrawerBackdrop: true, DrawerClose: true, DrawerContent: true, DrawerDescription: true, DrawerTitle: true, Field: true, FieldDescription: true, FieldLabel: true, Input: true, Select: true, SelectContent: true, SelectItem: true, SelectItemIndicator: true, SelectTrigger: true, SelectValue: true, Textarea: true, AppIcon: true, Checkbox: true, CheckboxIndicator: true },
    },
  });
  await vi.waitFor(() => expect(wrapper.text()).toContain("Auto-discover workflows"));
  const discoverBtn = wrapper.findAll("button").find((b) => b.text().includes("Auto-discover workflows"))!;
  await discoverBtn.trigger("click");
  await vi.waitFor(() => expect(wrapper.text()).toContain("Discovery temporarily unavailable"));
  expect(wrapper.text()).not.toContain("Retry");
  wrapper.unmount();
});
