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
    for (const label of ["Tool details", "Tool description", "customer_id hint", "Save guidance"]) {
      expect(labels).toContain(label);
    }
    const result = await agent.act({ steps: [
      { ref: "Tool description", action: "setValue", args: ["List active accounts."] },
      { ref: "customer_id hint", action: "setValue", args: ["Use the ID from get_customer."] },
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
