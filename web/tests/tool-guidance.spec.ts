import { mount } from "@vue/test-utils";
import { createAgent } from "@ui-sdk/vue/agent";
import { expect, test, vi } from "vitest";
import ToolGuidanceDrawer from "~/components/ToolGuidanceDrawer.vue";

Element.prototype.getAnimations = () => [];
Element.prototype.checkVisibility = () => true;
Element.prototype.getClientRects = () => [] as unknown as DOMRectList;
document.elementFromPoint = () => null;
document.elementsFromPoint = () => [];

const agent = createAgent();
const endpoint = "/admin/upstreams/accounts/tools/accounts__list/guidance";
const imported = {
  description: "List accounts.",
  input_schema: { properties: { customer_id: { type: "string" } } },
  output_schema: null,
  guidance: null,
  mutating: false,
  effect: "read" as const,
  approval_policy: {
    mode: "none" as const,
    version: 1,
    confirmation_message: "",
    approver_groups: [],
  },
};

test("agent edits tool guidance and saves through the catalog API", async () => {
  const apiFetch = vi.fn().mockResolvedValue({ ...imported, can_edit: true });
  vi.stubGlobal("useAdminApi", () => ({ apiFetch }));
  const wrapper = mount(ToolGuidanceDrawer, {
    attachTo: document.body,
    props: { open: true, upstream: "accounts", tool: "accounts__list", opener: null },
  });
  try {
    await vi.waitFor(() => expect(document.body.textContent).toContain("Save guidance"));
    const labels = agent.snapshot().filter((entry) => !entry.offPage).map((entry) => entry.label);
    for (const label of ["Tool details", "Gateway tool description", "customer_id · string", "Save guidance"]) {
      expect(labels).toContain(label);
    }
    const result = await agent.act({ steps: [
      { ref: "Gateway tool description", action: "setValue", args: ["List active accounts."] },
      { ref: "customer_id · string", action: "setValue", args: ["Use the ID from get_customer."] },
      { ref: "Save guidance", action: "press" },
    ] });
    expect(result.ok, result.error?.message).toBe(true);
    expect(apiFetch).toHaveBeenLastCalledWith(endpoint, {
      method: "PUT",
      body: { description: "List active accounts.", parameter_hints: { customer_id: "Use the ID from get_customer." } },
    });
    await vi.waitFor(() => expect(document.body.textContent).toContain("Tool guidance saved."));
  } finally {
    wrapper.unmount();
    vi.unstubAllGlobals();
  }
});

test("a retry can open read-only schema details", async () => {
  const apiFetch = vi.fn().mockRejectedValueOnce(new Error("Gateway unavailable."))
    .mockResolvedValue({ ...imported, can_edit: false });
  vi.stubGlobal("useAdminApi", () => ({ apiFetch }));
  const wrapper = mount(ToolGuidanceDrawer, {
    attachTo: document.body,
    props: { open: true, upstream: "accounts", tool: "accounts__list", opener: null },
  });
  try {
    await vi.waitFor(() => expect(document.body.textContent).toContain("Gateway unavailable."));
    const result = await agent.act({ steps: [{ ref: "Retry loading tool", action: "press" }] });
    expect(result.ok, result.error?.message).toBe(true);
    await vi.waitFor(() => expect(document.body.textContent).toContain("Read-only tool details."));
    expect(document.body.querySelector("textarea")?.readOnly).toBe(true);
    expect(document.body.textContent).toContain("Imported input schema");
    expect(document.body.textContent).not.toContain("Save guidance");
  } finally {
    wrapper.unmount();
    vi.unstubAllGlobals();
  }
});

test("runs a seeded mock read example and renders its result", async () => {
  const endpoint = "/admin/upstreams/snowflake_ledger/tools/snowflake_ledger__query_debt_ledger/guidance";
  const apiFetch = vi.fn(async (path: string) => path === endpoint
    ? { ...imported, input_schema: { properties: { status: { type: "string" } } }, can_edit: false }
    : { result: { rows: [{ customer_id: "CUST-1003", region: "EU" }] } });
  vi.stubGlobal("useAdminApi", () => ({ apiFetch }));
  const wrapper = mount(ToolGuidanceDrawer, {
    attachTo: document.body,
    props: {
      open: true,
      upstream: "snowflake_ledger",
      tool: "snowflake_ledger__query_debt_ledger",
      opener: null,
      demoMode: true,
      upstreamConfig: { name: "snowflake_ledger", type: "mcp_http", url: "http://127.0.0.1:8001/snowflake/mcp" },
    },
  });
  try {
    await vi.waitFor(() => expect(document.body.textContent).toContain("Run read-only example"));
    expect(document.body.textContent).toContain("status · string");
    const result = await agent.act({ steps: [{ ref: "Run read-only example", action: "press" }] });
    expect(result.ok, result.error?.message).toBe(true);
    await vi.waitFor(() => expect(document.body.textContent).toContain("CUST-1003"));
    expect(apiFetch).toHaveBeenLastCalledWith("/admin/upstreams/test-call", expect.objectContaining({
      method: "POST",
      body: expect.objectContaining({
        tool_name: "snowflake_ledger__query_debt_ledger",
        arguments: { status: "overdue", region: "EU" },
      }),
    }));
  } finally {
    wrapper.unmount();
    vi.unstubAllGlobals();
  }
});

test("shows manager approval separately from requester confirmation", async () => {
  const apiFetch = vi.fn().mockResolvedValue({
    ...imported,
    mutating: true,
    effect: "high_impact",
    approval_policy: {
      mode: "manager_approval",
      version: 2,
      confirmation_message: "Approve the demo case creation.",
      approver_groups: ["operations-approvers"],
    },
    can_edit: true,
  });
  vi.stubGlobal("useAdminApi", () => ({ apiFetch }));
  const wrapper = mount(ToolGuidanceDrawer, {
    attachTo: document.body,
    props: {
      open: true,
      upstream: "ops_ticketing",
      tool: "ops_ticketing__create_collection_case",
      opener: null,
      demoMode: true,
    },
  });
  try {
    await vi.waitFor(() => expect(document.body.textContent).toContain("Manager approval required"));
    expect(document.body.textContent).toContain("operations-approvers");
    expect(document.body.textContent).toContain("Approve the demo case creation.");
    expect(document.body.textContent).not.toContain("Ask caller before each call");
    expect(document.body.textContent).toContain("Write tools cannot be executed from this example panel.");
  } finally {
    wrapper.unmount();
    vi.unstubAllGlobals();
  }
});
