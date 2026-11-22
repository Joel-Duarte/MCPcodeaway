import { mount } from "@vue/test-utils";
import { Button } from "@ui-sdk/vue/button";
import { Checkbox, CheckboxIndicator } from "@ui-sdk/vue/checkbox";
import { Dialog, DialogBackdrop, DialogClose, DialogContent, DialogDescription, DialogTitle } from "@ui-sdk/vue/dialog";
import { Field, FieldDescription, FieldLabel } from "@ui-sdk/vue/field";
import { Input } from "@ui-sdk/vue/input";
import { Select, SelectContent, SelectItem, SelectItemIndicator, SelectTrigger, SelectValue } from "@ui-sdk/vue/select";
import { Tabs, TabsList, TabsPanel, TabsTab } from "@ui-sdk/vue/tabs";
import { Textarea } from "@ui-sdk/vue/textarea";
import { defineComponent, h, nextTick } from "vue";
import { expect, test, vi } from "vitest";
import OnboardingPage from "~/pages/onboarding.vue";

Element.prototype.getAnimations = () => [];
Element.prototype.checkVisibility = () => true;
Element.prototype.getClientRects = () => [] as unknown as DOMRectList;
document.elementFromPoint = () => null;
document.elementsFromPoint = () => [];

const tools = [
  {
    qualified_name: "inventory_api__list_users",
    name: "list_users",
    schema_sha256: "a".repeat(64),
    description: "List users",
    mutating: false,
    input_schema: { properties: {} },
    output_schema: { type: "object", properties: { users: { type: "array" } } },
  },
  {
    qualified_name: "inventory_api__update_user",
    name: "update_user",
    schema_sha256: "b".repeat(64),
    description: "Update user information",
    mutating: true,
    input_schema: { properties: {} },
    output_schema: { type: "object", properties: { updated: { type: "boolean" } } },
  },
  {
    qualified_name: "inventory_api__verify_user",
    name: "verify_user",
    schema_sha256: "c".repeat(64),
    description: "Read back updated user",
    mutating: false,
    input_schema: { properties: {} },
    output_schema: { type: "object", properties: { status: { type: "string" } } },
  },
];

const AppIconStub = defineComponent({
  name: "AppIcon",
  props: ["name", "size"],
  render: () => h("span", { "aria-hidden": "true" }),
});
const NuxtLinkStub = defineComponent({
  name: "NuxtLink",
  inheritAttrs: false,
  render: () => h("a"),
});

const components = {
  AppIcon: AppIconStub,
  Button,
  Checkbox,
  CheckboxIndicator,
  Dialog,
  DialogBackdrop,
  DialogClose,
  DialogContent,
  DialogDescription,
  DialogTitle,
  Field,
  FieldDescription,
  FieldLabel,
  Input,
  Select,
  SelectContent,
  SelectItem,
  SelectItemIndicator,
  SelectTrigger,
  SelectValue,
  Tabs,
  TabsList,
  TabsPanel,
  TabsTab,
  Textarea,
  NuxtLink: NuxtLinkStub,
};

function mockApi() {
  const apiFetch = vi.fn(async (path: string, options?: { method?: string; body?: unknown }) => {
    if (path === "/admin/upstreams" && options?.method !== "POST") return { dynamic_upstreams_permitted: true, upstreams: [] };
    if (path === "/admin/roles") return { roles: [], groups: [] };
    if (path === "/admin/upstreams/inspect") {
      return {
        inspection: { name: "inventory_api", type: "openapi", tool_count: tools.length, tools },
        onboarding_audit: { summary: "Catalog verified", warnings: [] },
      };
    }
    if (path === "/admin/upstreams" && options?.method === "POST") return { registered: true, catalog_version: "catalog-1" };
    if (path === "/admin/integration-definitions/inventory_api") return { current: null, versions: [] };
    if (path === "/admin/integration-definitions/validate") return { valid: true, definition_digest: "digest-1" };
    if (path === "/admin/integration-definitions/drafts") return { revision: "draft-1", definition_digest: "digest-1" };
    if (path === "/admin/integration-definitions/inventory_api/publish") return { revision: "published-1" };
    throw new Error(`Unexpected API request: ${path}`);
  });
  vi.stubGlobal("useAdminApi", () => ({ apiFetch }));
  return apiFetch;
}

async function openWorkflowBuilder(wrapper: ReturnType<typeof mount>) {
  await wrapper.findAll("button").find((button) => button.text().includes("Inspect Schema"))!.trigger("click");
  await vi.waitFor(() => expect(wrapper.text()).toContain("Inspected 'inventory_api'"));
  await wrapper.findAll("button").find((button) => button.text().includes("2. Policy & Mode"))!.trigger("click");
  await wrapper.find("summary").trigger("click");
  await wrapper.get('[aria-label="Allow all authenticated callers"]').trigger("click");
  for (const tool of ["list_users", "update_user", "verify_user"]) {
    const trigger = wrapper.get(`[aria-label="Access classification for ${tool}"]`);
    await trigger.trigger("click");
    await nextTick();
    const contentId = trigger.attributes("aria-controls");
    const content = contentId ? document.getElementById(contentId) : null;
    const readOnlyOption = content ? [...content.querySelectorAll('[role="option"]')].find((option) => option.textContent?.includes("Read only")) : null;
    expect(readOnlyOption, `Read only option for ${tool} is visible`).toBeDefined();
    (readOnlyOption as HTMLElement).click();
    await nextTick();
  }
  await wrapper.findAll("button").find((button) => button.text().includes("3. Build workflows"))!.trigger("click");
  await nextTick();
}

test("workflow builder turns an arranged catalog flow into evidence, completion, and baseline rules", async () => {
  const apiFetch = mockApi();
  const wrapper = mount(OnboardingPage, { attachTo: document.body, global: { components } });
  await openWorkflowBuilder(wrapper);

  expect(wrapper.text()).toContain("calls may happen in any order");
  expect(wrapper.text()).toContain("Required evidence and the terminal result determine whether a run receives credit");

  await wrapper.get('[aria-label="Workflow 1 name"]').setValue("Update and verify user");
  await wrapper.get('[aria-label="Workflow 1 description"]').setValue("Read the user, update their record, and verify the saved information.");
  await wrapper.get('[aria-label="Workflow 1 human baseline minutes"]').setValue("8.5");
  await wrapper.get('[aria-label="Workflow 1 baseline effective date"]').setValue("2026-11-22");

  await wrapper.findAll("button").find((button) => button.text().includes("Add list_users"))!.trigger("click");
  await wrapper.findAll("button").find((button) => button.text().includes("Add update_user"))!.trigger("click");
  await wrapper.findAll("button").find((button) => button.text().includes("Add verify_user"))!.trigger("click");
  await wrapper.get('[aria-label="Require inventory_api__list_users as completion evidence"]').trigger("click");
  await wrapper.get('[aria-label="Check returned result from inventory_api__list_users"]').trigger("click");
  await wrapper.get('[aria-label="Required evidence result field for inventory_api__list_users"]').setValue("/users");
  await wrapper.get('[aria-label="Required evidence result type for inventory_api__list_users"]').setValue("non_empty_array");
  await wrapper.get('[aria-label="Require inventory_api__update_user as completion evidence"]').trigger("click");
  await wrapper.get('[aria-label="Workflow 1 outcome tool"]').setValue("inventory_api__verify_user");
  await wrapper.get('[aria-label="Workflow 1 completion result field"]').setValue("/status");
  await wrapper.get('[aria-label="Workflow 1 completion result type"]').setValue("non_empty_string");

  await wrapper.get('[aria-label="Move inventory_api__verify_user earlier"]').trigger("click");
  const orderedTools = wrapper.get('[aria-label="Workflow 1 flow order"]');
  expect(orderedTools.text().indexOf("inventory_api__list_users")).toBeLessThan(orderedTools.text().indexOf("inventory_api__verify_user"));
  expect(orderedTools.text().indexOf("inventory_api__verify_user")).toBeLessThan(orderedTools.text().indexOf("inventory_api__update_user"));

  await wrapper.findAll("button").find((button) => button.text().includes("Save and publish workflows"))!.trigger("click");
  await vi.waitFor(() => expect(wrapper.text()).toContain("Published 1 workflow for inventory_api."));

  const validationCall = apiFetch.mock.calls.find(([path]) => path === "/admin/integration-definitions/validate");
  expect(validationCall).toBeDefined();
  const definition = validationCall![1]?.body as {
    tool_refs: { name: string; schema_sha256: string }[];
    workflows: {
      participating_tools: string[];
      required_evidence: { tool: string; min_successful_calls: number; result_when?: { path: string; type: string } }[];
      completion: { tool: string; success_when: { path: string; type: string } };
      business_value: { manual_baseline_minutes: number; baseline_source: string; confidence: string };
    }[];
  };
  expect(definition.tool_refs).toEqual([
    { name: "inventory_api__list_users", schema_sha256: "a".repeat(64) },
    { name: "inventory_api__verify_user", schema_sha256: "c".repeat(64) },
    { name: "inventory_api__update_user", schema_sha256: "b".repeat(64) },
  ]);
  expect(definition.workflows[0].participating_tools).toEqual([
    "inventory_api__list_users",
    "inventory_api__verify_user",
    "inventory_api__update_user",
  ]);
  expect(definition.workflows[0].required_evidence).toEqual([
    { tool: "inventory_api__list_users", min_successful_calls: 1, result_when: { path: "/users", type: "non_empty_array" } },
    { tool: "inventory_api__update_user", min_successful_calls: 1 },
  ]);
  expect(definition.workflows[0].completion).toEqual({
    kind: "terminal_tool_result",
    tool: "inventory_api__verify_user",
    success_when: { path: "/status", type: "non_empty_string" },
  });
  expect(definition.workflows[0].business_value).toMatchObject({
    manual_baseline_minutes: 8.5,
    baseline_source: "administrator_estimate",
    confidence: "unvalidated",
  });
  expect(apiFetch).toHaveBeenCalledWith("/admin/integration-definitions/drafts", expect.objectContaining({ method: "POST", body: { definition } }));
  expect(apiFetch).toHaveBeenCalledWith("/admin/integration-definitions/inventory_api/publish", expect.objectContaining({ method: "POST", body: { revision: "draft-1" } }));
  wrapper.unmount();
});

test("workflow builder rejects incomplete activities before registering the upstream", async () => {
  const apiFetch = mockApi();
  const wrapper = mount(OnboardingPage, { attachTo: document.body, global: { components } });
  await openWorkflowBuilder(wrapper);
  await wrapper.get('[aria-label="Workflow 1 name"]').setValue("Incomplete workflow");
  await wrapper.findAll("button").find((button) => button.text().includes("Save and publish workflows"))!.trigger("click");
  await vi.waitFor(() => expect(wrapper.get('[role="alert"]').text()).toContain("explain what business activity it measures"));
  expect(apiFetch).not.toHaveBeenCalledWith("/admin/upstreams", expect.objectContaining({ method: "POST" }));
  expect(apiFetch).not.toHaveBeenCalledWith("/admin/integration-definitions/validate", expect.anything());
  wrapper.unmount();
});
