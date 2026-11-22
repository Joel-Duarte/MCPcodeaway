import { mount } from "@vue/test-utils";
import { createAgent } from "@ui-sdk/vue/agent";
import { Button } from "@ui-sdk/vue/button";
import { expect, test, vi } from "vitest";
import DemoPage from "~/pages/demo.vue";

const recoveryResult = {
  status: "passed" as const,
  isolation: "Fresh in-process mock API and temporary SQLite database; no configured upstream was called.",
  scenarios: [
    {
      id: "task_recovers_lost_response",
      status: "passed" as const,
      detail: "The mock response was lost and recovered through MCP Tasks.",
      operation_id: "operation-1",
      task_id: "operation-1",
      side_effect_count: 1,
    },
    {
      id: "same_key_replay_is_deduplicated",
      status: "passed" as const,
      detail: "The same key reused the prior result.",
      operation_id: "operation-1",
      task_id: null,
      side_effect_count: 1,
    },
    {
      id: "changed_payload_is_rejected",
      status: "passed" as const,
      detail: "Changed arguments were rejected.",
      operation_id: "operation-1",
      task_id: null,
      side_effect_count: 1,
    },
    {
      id: "legacy_client_uses_status_fallback",
      status: "passed" as const,
      detail: "Legacy operation status was retrieved.",
      operation_id: "operation-2",
      task_id: null,
      side_effect_count: 2,
    },
  ],
};

test("Demo Control runs the Recovery Lab and presents the gateway proof", async () => {
  const apiFetch = vi.fn(async (path: string) => {
    if (path === "/admin/demo/models") return { models: [] };
    if (path === "/admin/demo/recovery/run") return recoveryResult;
    if (path === "/admin/demo/golden-run") {
      return {
        evidence_rows: 200,
        models: 4,
        workloads_per_model: 5,
        traffic_requests: 24,
        traffic_errors: 5,
        discovery_requests: 2,
        task_polls: 1,
        recovery_operations_created: 4,
        recovery_operations_pending: 1,
        recovery_operations_needs_attention: 0,
      };
    }
    throw new Error(`Unexpected API request: ${path}`);
  });
  const demoStatus = {
    value: {
      enabled: false,
      running: false,
      tier: "enterprise",
      requests_sent: 0,
      errors_count: 0,
      rpc_errors_count: 0,
      http_errors_count: 0,
      write_calls_count: 0,
      discovery_requests_count: 0,
      task_polls_count: 0,
      golden_runs_count: 0,
      last_target: "",
      last_error: "",
    },
  };
  vi.stubGlobal("useAdminApi", () => ({ apiFetch }));
  vi.stubGlobal("useDemoTraffic", () => ({
    status: demoStatus,
    busy: { value: false },
    statusError: { value: "" },
    startTraffic: vi.fn(),
    stopTraffic: vi.fn(),
    setTier: vi.fn(),
    runBurst: vi.fn(),
    refreshStatus: vi.fn(),
  }));
  vi.stubGlobal("extractErrorMessage", (error: unknown) => String(error));

  const agent = createAgent();
  const wrapper = mount(DemoPage, {
    attachTo: document.body,
    global: {
      components: { Button },
      stubs: {
        AppIcon: true,
        Field: true,
        FieldLabel: true,
        Input: true,
        Select: true,
        SelectContent: true,
        SelectItem: true,
        SelectItemIndicator: true,
        SelectTrigger: true,
        SelectValue: true,
      },
    },
  });

  try {
    await vi.waitFor(() => expect(apiFetch).toHaveBeenCalledWith("/admin/demo/models"));
    expect(wrapper.text()).toContain("Run the lab to verify recovery behavior");
    const runButton = agent.snapshot().find(
      (entry) => entry.component === "button" && entry.label === "Run recovery checks"
    );
    expect(runButton).toBeDefined();
    const result = await agent.act({ steps: [{ ref: runButton!.ref, action: "press" }] });
    expect(result.ok, result.error?.message).toBe(true);

    await vi.waitFor(() => expect(wrapper.text()).toContain("Recovery lab passed: 4 of 4 scenarios passed."));
    expect(apiFetch).toHaveBeenCalledWith(
      "/admin/demo/recovery/run",
      expect.objectContaining({ method: "POST", timeout: 20000 })
    );
    for (const label of [
      "task recovers lost response",
      "same key replay is deduplicated",
      "changed payload is rejected",
      "legacy client uses status fallback",
    ]) {
      expect(wrapper.text()).toContain(label);
    }
    expect(wrapper.text()).toContain("Side effects");
    expect(wrapper.text()).toContain("Task operation-1");
    const evidenceButton = agent.snapshot().find(
      (entry) => entry.component === "button" && entry.label === "Run Golden Demo Traffic"
    );
    expect(evidenceButton).toBeDefined();
    const generated = await agent.act({ steps: [{ ref: evidenceButton!.ref, action: "press" }] });
    expect(generated.ok, generated.error?.message).toBe(true);
    await vi.waitFor(() => expect(wrapper.text()).toContain("Golden run sent 24 mock MCP calls"));
    expect(apiFetch).toHaveBeenCalledWith(
      "/admin/demo/golden-run",
      { method: "POST", timeout: 60000 }
    );
    expect(wrapper.text()).toContain("Write Calls");
    expect(wrapper.text()).toContain("MCP Errors");
  } finally {
    wrapper.unmount();
    vi.unstubAllGlobals();
  }
});
