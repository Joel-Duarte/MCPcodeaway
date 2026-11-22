import { mount } from "@vue/test-utils";
import { createAgent } from "@ui-sdk/vue/agent";
import {
  Select, SelectContent, SelectItem, SelectLabel, SelectTrigger, SelectValue,
} from "@ui-sdk/vue/select";
import { Field, FieldLabel } from "@ui-sdk/vue/field";
import { Input } from "@ui-sdk/vue/input";
import { Button } from "@ui-sdk/vue/button";
import { Tabs, TabsList, TabsPanel, TabsTab } from "@ui-sdk/vue/tabs";
import { nextTick, ref } from "vue";
import { expect, test, vi } from "vitest";
import ModelPerformancePage from "~/pages/model-performance.vue";

Element.prototype.getAnimations = () => [];
Element.prototype.checkVisibility = () => true;
Element.prototype.getClientRects = () => [] as unknown as DOMRectList;
document.elementFromPoint = () => null;
document.elementsFromPoint = () => [];

const pairedRows = [{
  model: "local/model-a", workload: "large_data_reconciliation", pairs: 5,
  traditional: {
    correct_pct: 100, grounded_pct: 100, verified_pct: 100, mean_cost_usd: 0.01,
    mean_provider_tokens: 500, p95_wall_ms: 100, mean_upstream_calls: 4, mean_recovery: 0,
  },
  codemode: {
    correct_pct: 100, grounded_pct: 100, verified_pct: 100, mean_cost_usd: 0.008,
    mean_provider_tokens: 400, p95_wall_ms: 120, mean_upstream_calls: 4, mean_recovery: 0,
  },
  recommended_mode: "codemode", reason: "Lower cost within regression limits.",
}];

test("Model Performance exposes named filters, expandable workload details, and signed efficiency metrics", async () => {
  const apiFetch = vi.fn().mockResolvedValue({ rows: pairedRows, adaptive_mode: "observe" });
  vi.stubGlobal("useAdminApi", () => ({ apiFetch }));
  vi.stubGlobal("useReportRange", () => ({ selectedRange: ref("all") }));
  vi.stubGlobal("useRoute", () => ({ query: {} }));
  vi.stubGlobal("navigateTo", vi.fn(async () => undefined));
  const agent = createAgent();
  const wrapper = mount(ModelPerformancePage, {
    attachTo: document.body,
    global: {
      components: { Button, Field, FieldLabel, Input, Select, SelectContent, SelectItem, SelectLabel, SelectTrigger, SelectValue, Tabs, TabsList, TabsPanel, TabsTab },
    },
  });
  await vi.waitFor(() => expect(apiFetch).toHaveBeenCalled());
  await nextTick();

  const labels = agent.snapshot().filter((entry) => !entry.offPage).map((entry) => entry.label);
  for (const label of ["Search model or workload", "Model", "Selected-window analysis", "Evidence arm", "Model performance views"]) {
    expect(labels, `Missing named control '${label}'; snapshot: ${JSON.stringify(labels)}`).toContain(label);
  }
  expect(wrapper.text()).toContain("Large data reconciliation");
  expect(wrapper.text()).toContain("Recommended: Code Mode");
  const filter = agent.snapshot().find(
    (entry) => entry.component === "select" && entry.label === "Selected-window analysis"
  );
  const filterResult = await agent.act({
    steps: [{ ref: filter!.ref, action: "setValue", args: ["codemode"] }],
  });
  expect(filterResult.ok, filterResult.error?.message).toBe(true);

  const rowControl = agent.snapshot().find((entry) =>
    entry.component === "button" && entry.label.includes("Large data reconciliation details")
  );
  expect(rowControl).toBeDefined();
  const details = await agent.act({ steps: [{ ref: rowControl!.ref, action: "press" }] });
  expect(details.ok, details.error?.message).toBe(true);
  expect(wrapper.text()).toContain("Selected-window analysis:");

  const tabs = agent.snapshot().find((entry) => entry.component === "tabs");
  const efficiency = await agent.act({
    steps: [{ ref: tabs!.ref, action: "setValue", args: ["efficiency"] }],
  });
  expect(efficiency.ok, efficiency.error?.message).toBe(true);
  expect(wrapper.text()).toContain("Cost per completion (Code − Classic)");
  expect(wrapper.text()).toContain("-$0.002000");
  expect(wrapper.text()).toContain("+20 ms");
  wrapper.unmount();
  vi.unstubAllGlobals();
});

test("range reload keeps the selected tab and ignores stale responses", async () => {
  const range = ref("30d");
  const resolvers: Array<(value: { rows: typeof pairedRows; adaptive_mode: string }) => void> = [];
  const apiFetch = vi.fn(() => new Promise((resolve) => resolvers.push(resolve)));
  vi.stubGlobal("useAdminApi", () => ({ apiFetch }));
  vi.stubGlobal("useReportRange", () => ({ selectedRange: range }));
  vi.stubGlobal("useRoute", () => ({ query: {} }));
  vi.stubGlobal("navigateTo", vi.fn(async () => undefined));
  const wrapper = mount(ModelPerformancePage, {
    global: {
      components: { Button, Field, FieldLabel, Input, Select, SelectContent, SelectItem, SelectLabel, SelectTrigger, SelectValue, Tabs, TabsList, TabsPanel, TabsTab },
    },
  });
  await nextTick();
  range.value = "all";
  await nextTick();
  const tabs = wrapper.findComponent(Tabs);
  expect(tabs.exists()).toBe(true);
  tabs.vm.$emit("update:value", "efficiency");
  await nextTick();
  expect(wrapper.findComponent(Tabs).props("value")).toBe("efficiency");

  const newest = [{ ...pairedRows[0], model: "new-result" }];
  resolvers[1]({ rows: newest, adaptive_mode: "active" });
  await vi.waitFor(() => expect(wrapper.text()).toContain("new-result"));
  resolvers[0]({ rows: pairedRows, adaptive_mode: "observe" });
  await nextTick();
  expect(wrapper.text()).toContain("new-result");
  expect(wrapper.text()).not.toContain("local/model-a");
  expect(wrapper.text()).toContain("Matched cohort differences");
  wrapper.unmount();
  vi.unstubAllGlobals();
});
