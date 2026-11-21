import { createAgent } from "@ui-sdk/vue/agent";
import { mount } from "@vue/test-utils";
import { expect, test, vi } from "vitest";
import DiscoveryReport from "~/components/DiscoveryReport.vue";

test("shows discovery measurements and refreshes through a named agent action", async () => {
  const apiFetch = vi.fn().mockResolvedValue({
    policy: "observe",
    token_budget: 1024,
    samples: [{
      request_id: "req-test",
      operation: "listing",
      status: "ok",
      duration_ms: 1.5,
      applied_discovery: "deferred",
      recommended_discovery: "upfront",
      authorized_tools: 8,
      estimated_upfront_tokens: 867,
      estimated_discovery_tokens: 300,
      token_budget: 1024,
      reason: "fits_budget",
    }],
  });
  vi.stubGlobal("useAdminApi", () => ({ apiFetch }));
  const agent = createAgent();
  const wrapper = mount(DiscoveryReport, { attachTo: document.body });
  try {
    await vi.waitFor(() => expect(wrapper.text()).toContain("0 searches across 1 discovery requests"));
    expect(wrapper.text()).toContain("Fits the upfront budget");
    expect(wrapper.text()).toContain("867 estimated upfront tokens");
    expect(agent.snapshot().some((entry) => entry.label === "Refresh discovery report")).toBe(true);
    const result = await agent.act({ steps: [{ ref: "Refresh discovery report", action: "press" }] });
    expect(result.ok, result.error?.message).toBe(true);
    expect(apiFetch).toHaveBeenCalledTimes(2);
  } finally {
    wrapper.unmount();
    vi.unstubAllGlobals();
  }
});
