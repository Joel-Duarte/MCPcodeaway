import { mount } from "@vue/test-utils";
import { Button } from "@ui-sdk/vue/button";
import { Checkbox, CheckboxIndicator } from "@ui-sdk/vue/checkbox";
import { Drawer, DrawerBackdrop, DrawerClose, DrawerContent, DrawerDescription, DrawerTitle } from "@ui-sdk/vue/drawer";
import { Field, FieldDescription, FieldLabel } from "@ui-sdk/vue/field";
import { Input } from "@ui-sdk/vue/input";
import { Select, SelectContent, SelectItem, SelectItemIndicator, SelectTrigger, SelectValue } from "@ui-sdk/vue/select";
import { Switch, SwitchThumb } from "@ui-sdk/vue/switch";
import { Textarea } from "@ui-sdk/vue/textarea";
import { defineComponent, h, nextTick } from "vue";
import { expect, test, vi } from "vitest";
import UpstreamWorkflowCatalog from "~/components/UpstreamWorkflowCatalog.vue";

Element.prototype.getAnimations = () => [];
Element.prototype.checkVisibility = () => true;
Element.prototype.getClientRects = () => [] as unknown as DOMRectList;
document.elementFromPoint = () => null;
document.elementsFromPoint = () => [];

const base = {
  manifest_version: "1.0",
  integration: { id: "support_desk", display_name: "Support", upstream_name: "support_api", status: "published", revision: 8 },
  tool_refs: [{ name: "support_api__find_customer", schema_sha256: "a".repeat(64) }, { name: "support_api__create_ticket", schema_sha256: "b".repeat(64) }],
  experiences: [{ id: "support_app", resource_uri: "ui://support", renderer_schema: "1", tool: "support_api__create_ticket", fallback: "Use the tools" }],
  standalone_tool_values: [{ id: "ticket_create", name: "Ticket", description: "Create a ticket", tool: "support_api__create_ticket", completion: { kind: "terminal_tool_result", tool: "support_api__create_ticket", success_when: { path: "/id", type: "non_empty_string" } }, business_value: { manual_baseline_minutes: 2, baseline_source: "administrator_estimate", confidence: "unvalidated", effective_from: "2026-01-01" } }],
  workflows: [], publication: { validator_version: "1.0", content_sha256: "c".repeat(64) },
};
const schema = "d".repeat(64);
const passthrough = defineComponent({ props: ["open"], setup(props, { slots }) { return () => h("div", props.open === false ? [] : slots.default?.()); } });
const NuxtLinkStub = defineComponent({ name: "NuxtLink", props: ["to"], render() { return h("a", {}, this.$slots.default?.()); } });
function setup({ current = base, canEdit = true, autoDiscoveryEnabled = true }: { current?: any; canEdit?: boolean; autoDiscoveryEnabled?: boolean } = {}) {
  let currentDefinition = current;
  const apiFetch = vi.fn(async (path: string, options?: { method?: string; body?: any }) => {
    if (path === "/admin/integration-definitions/support_api") return { current: currentDefinition ? { definition: currentDefinition } : null };
    if (path === "/admin/upstreams/support_api/auto-discovery" && options?.method === "PATCH") {
      return { upstream: "support_api", auto_discovery_enabled: Boolean(options.body?.enabled) };
    }
    if (path.endsWith("/guidance")) return { schema_sha256: schema };
    if (path.endsWith("/validate")) return { valid: true };
    if (path.endsWith("/drafts")) return { revision: 11 };
    if (path.endsWith("/publish")) return { revision: 11 };
    throw new Error(`Unexpected ${options?.method ?? "GET"} ${path}`);
  });
  vi.stubGlobal("useAdminApi", () => ({ apiFetch }));
  const wrapper = mount(UpstreamWorkflowCatalog, {
    attachTo: document.body,
    props: { upstream: "support_api", tools: ["support_api__find_customer", "support_api__create_ticket"], canEdit, refreshVersion: 0, autoDiscoveryEnabled },
    global: { components: { Button, Checkbox, CheckboxIndicator, Drawer, DrawerBackdrop, DrawerClose, DrawerContent, DrawerDescription, DrawerTitle, Field, FieldDescription, FieldLabel, Input, NuxtLink: NuxtLinkStub, Select, SelectContent, SelectItem, SelectItemIndicator, SelectTrigger, SelectValue, Switch, SwitchThumb, Textarea }, stubs: { Drawer: passthrough, DrawerBackdrop: true, DrawerClose: passthrough, DrawerContent: passthrough, DrawerDescription: passthrough, DrawerTitle: passthrough } },
  });
  return { apiFetch, wrapper, setCurrent: (next: typeof current) => { currentDefinition = next; } };
}

async function chooseSelect(wrapper: ReturnType<typeof mount>, label: string, choice: string) {
  const trigger = wrapper.get(`[aria-label="${label}"]`);
  await trigger.trigger("click");
  await nextTick();
  const option = [...document.querySelectorAll('[role="option"]')].find((node) => node.textContent?.includes(choice));
  expect(option, `option ${choice} is available`).toBeDefined();
  (option as HTMLElement).click();
  await nextTick();
}

test("workflow catalog shows empty and read-only states and toggles auto-discovery", async () => {
  const { apiFetch, wrapper, setCurrent } = setup({ canEdit: false });
  await vi.waitFor(() => expect(wrapper.text()).toContain("No workflows are defined"));
  expect(wrapper.text()).toContain("Read-only");
  expect(wrapper.text()).not.toContain("Refresh API tools");
  expect(wrapper.text()).toContain("Manage & review unconfigured workflows");
  setCurrent({ ...base, workflows: [{ id: "refreshed_flow", name: "Refreshed flow", description: "Visible after reload", participating_tools: [], required_evidence: [], completion: { tool: "tool", success_when: { path: "/ok", type: "boolean", equals: true } }, business_value: { manual_baseline_minutes: 1, baseline_source: "administrator_estimate", confidence: "unvalidated" } }] });
  await wrapper.setProps({ refreshVersion: 1 });
  await vi.waitFor(() => expect(wrapper.text()).toContain("Refreshed flow"));
  expect(apiFetch.mock.calls.filter(([path]) => path === "/admin/integration-definitions/support_api")).toHaveLength(2);
  expect(wrapper.text()).not.toContain("Create workflow");
  expect(wrapper.text()).not.toContain("Generate Workflows via Prompt");
  wrapper.unmount();

  const editable = setup({ canEdit: true, autoDiscoveryEnabled: true });
  await vi.waitFor(() => expect(editable.wrapper.text()).toContain("Auto-discover traffic (On)"));
  expect(editable.wrapper.text()).toContain("Auto-discover workflows");
  await editable.wrapper.get('[aria-label="Toggle background auto-discovery for support_api"]').trigger("click");
  await vi.waitFor(() =>
    expect(editable.apiFetch).toHaveBeenCalledWith(
      "/admin/upstreams/support_api/auto-discovery",
      expect.objectContaining({ method: "PATCH", body: { enabled: false } }),
    ),
  );
  await vi.waitFor(() => expect(editable.wrapper.text()).toContain("Auto-discover traffic (Off)"));
  expect(editable.wrapper.text()).not.toContain("Auto-discover workflows");
  editable.wrapper.unmount();
});

test("creating a workflow validates then publishes while preserving unrelated manifest data", async () => {
  const { apiFetch, wrapper } = setup({ current: null });
  await vi.waitFor(() => expect(wrapper.text()).toContain("No integration definition or workflows"));
  await wrapper.get("button").trigger("click");
  await nextTick();
  await wrapper.get("#workflow-name").setValue("Customer support resolution");
  await wrapper.get("#workflow-description").setValue("Look up the customer and create a support ticket.");
  await wrapper.get('[aria-label="Participating tool support_api__find_customer"]').trigger("click");
  await wrapper.get('[aria-label="Participating tool support_api__create_ticket"]').trigger("click");
  await wrapper.get('[aria-label="Required evidence tool support_api__find_customer"]').trigger("click");
  await chooseSelect(wrapper, "Tool proving completion", "support_api__create_ticket");
  await wrapper.get("#baseline-minutes").setValue("14");
  await wrapper.get("#baseline-effective").setValue("2026-11-23");
  await wrapper.findAll("button").find((button) => button.text().includes("Validate and publish"))!.trigger("click");
  await vi.waitFor(() => expect(apiFetch).toHaveBeenCalledWith("/admin/integration-definitions/support_api/publish", expect.objectContaining({ method: "POST", body: { revision: 11 } })));
  const validationBody = apiFetch.mock.calls.find(([path]) => path.endsWith("/validate"))?.[1]?.body as any;
  expect(validationBody.definition.integration).toMatchObject({ id: "support_api", display_name: "support_api", upstream_name: "support_api", status: "draft", revision: 1 });
  expect(validationBody.definition.experiences).toEqual([]);
  expect(validationBody.definition.standalone_tool_values).toEqual([]);
  expect(validationBody.definition.workflows[0]).toMatchObject({ id: "customer_support_resolution", participating_tools: ["support_api__find_customer", "support_api__create_ticket"], required_evidence: [{ tool: "support_api__find_customer", min_successful_calls: 1 }] });
  expect(validationBody.definition.tool_refs).toContainEqual({ name: "support_api__find_customer", schema_sha256: schema });
  expect(validationBody.definition.publication).toBeNull();
  expect(apiFetch.mock.calls.find(([path]) => path.endsWith("/drafts"))?.[1]?.body).toMatchObject({ definition: validationBody.definition });
  wrapper.unmount();
});

test("editing replaces the matching workflow by stable ID without duplicating it", async () => {
  const old = { id: "customer_lookup", name: "Customer lookup", description: "Read customer info.", participating_tools: ["support_api__find_customer"], required_evidence: [{ tool: "support_api__find_customer", min_successful_calls: 2, result_when: { path: "/id", type: "non_empty_string" } }], completion: { kind: "terminal_tool_result", tool: "support_api__find_customer", success_when: { path: "/id", type: "non_empty_string" } }, business_value: { manual_baseline_minutes: 6, baseline_source: "sampled_manual_measurement", confidence: "sampled", effective_from: "2026-02-01" } };
  const original = { ...base, workflows: [old, { ...old, id: "other_workflow", name: "Other workflow" }] };
  const { apiFetch, wrapper } = setup({ current: original });
  await vi.waitFor(() => expect(wrapper.text()).toContain("Customer lookup"));
  await wrapper.get('[aria-label="Edit Customer lookup"]').trigger("click");
  await wrapper.get("#workflow-description").setValue("Read the customer profile for support.");
  await wrapper.findAll("button").find((button) => button.text().includes("Validate and publish"))!.trigger("click");
  await vi.waitFor(() => expect(apiFetch.mock.calls.some(([path]) => path.endsWith("/validate"))).toBe(true));
  const payload = apiFetch.mock.calls.find(([path]) => path.endsWith("/validate"))?.[1]?.body as any;
  expect(payload.definition.workflows).toHaveLength(2);
  expect(payload.definition.experiences).toEqual(base.experiences);
  expect(payload.definition.standalone_tool_values).toEqual(base.standalone_tool_values);
  expect(payload.definition.workflows[0]).toMatchObject({ id: "customer_lookup", description: "Read the customer profile for support." });
  expect(payload.definition.workflows[0].required_evidence).toEqual([{ tool: "support_api__find_customer", min_successful_calls: 2, result_when: { path: "/id", type: "non_empty_string" } }]);
  expect(payload.definition.workflows[1]).toMatchObject({ id: "other_workflow", name: "Other workflow" });
  wrapper.unmount();
});
