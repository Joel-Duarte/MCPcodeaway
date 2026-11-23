import { mount } from "@vue/test-utils";
import { Button } from "@ui-sdk/vue/button";
import { Checkbox, CheckboxIndicator } from "@ui-sdk/vue/checkbox";
import {
  Dialog,
  DialogBackdrop,
  DialogClose,
  DialogContent,
  DialogDescription,
  DialogTitle,
} from "@ui-sdk/vue/dialog";
import {
  Drawer,
  DrawerBackdrop,
  DrawerClose,
  DrawerContent,
  DrawerDescription,
  DrawerTitle,
} from "@ui-sdk/vue/drawer";
import { Field, FieldDescription, FieldLabel } from "@ui-sdk/vue/field";
import { Input } from "@ui-sdk/vue/input";
import {
  Select,
  SelectContent,
  SelectItem,
  SelectItemIndicator,
  SelectTrigger,
  SelectValue,
} from "@ui-sdk/vue/select";
import { Switch, SwitchThumb } from "@ui-sdk/vue/switch";
import { Tabs, TabsList, TabsPanel, TabsTab } from "@ui-sdk/vue/tabs";
import { Textarea } from "@ui-sdk/vue/textarea";
import { defineComponent, h, nextTick, ref } from "vue";
import { afterEach, expect, test, vi } from "vitest";
import UpstreamMcpUiCatalog from "~/components/UpstreamMcpUiCatalog.vue";
import McpUiPage from "~/pages/mcp-ui.vue";
import OnboardingPage from "~/pages/onboarding.vue";
import WorkflowsPage from "~/pages/workflows.vue";

afterEach(() => {
  vi.unstubAllGlobals();
});

Element.prototype.getAnimations = () => [];
Element.prototype.checkVisibility = () => true;
Element.prototype.getClientRects = () => [] as unknown as DOMRectList;

const AppIconStub = defineComponent({
  name: "AppIcon",
  props: ["name", "size"],
  render: () => h("span", { "aria-hidden": "true" }),
});
const NuxtLinkStub = defineComponent({
  name: "NuxtLink",
  props: ["to"],
  render() {
    return h("a", {}, this.$slots.default?.());
  },
});

const sharedComponents = {
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
  Drawer,
  DrawerBackdrop,
  DrawerClose,
  DrawerContent,
  DrawerDescription,
  DrawerTitle,
  Field,
  FieldDescription,
  FieldLabel,
  Input,
  NuxtLink: NuxtLinkStub,
  Select,
  SelectContent,
  SelectItem,
  SelectItemIndicator,
  SelectTrigger,
  SelectValue,
  Switch,
  SwitchThumb,
  Tabs,
  TabsList,
  TabsPanel,
  TabsTab,
  Textarea,
};

test("Workflows page toggles workflow active state, dismisses candidates, and promotes with baseline", async () => {
  const apiFetch = vi.fn(async (path: string, options?: { method?: string; body?: any }) => {
    if (path === "/admin/workflows/catalog") {
      return {
        upstreams: ["zendesk_support"],
        workflows: [
          {
            upstream_name: "zendesk_support",
            definition_revision: 2,
            workflow: {
              id: "resolve_ticket",
              name: "Resolve support ticket",
              description: "Verify ticket creation",
              enabled: true,
              participating_tools: ["zendesk_support__create_ticket"],
              intent_examples: ["Open a ticket", "Resolve issue"],
              completion: {
                kind: "terminal_tool_result",
                tool: "zendesk_support__create_ticket",
                success_when: { path: "/status", type: "non_empty_string" },
              },
              business_value: {
                manual_baseline_minutes: 12,
                baseline_source: "administrator_estimate",
                confidence: "unvalidated",
                effective_from: "2026-11-22",
              },
            },
          },
        ],
        candidates: [
          {
            candidate_id: "cand-1",
            upstream_name: "zendesk_support",
            workflow_slug: "auto_escalation",
            origin: "auto_mined",
            status: "pending_review",
            observed_count: 4,
            verified_count: 2,
            sample_intents: ["Escalate billing dispute"],
            draft_definition: {
              id: "auto_escalation",
              name: "Escalate ticket",
              description: "Verified multi-step escalation",
              participating_tools: ["zendesk_support__create_ticket"],
            },
          },
        ],
        episodes: [],
      };
    }
    if (path === "/admin/integration-definitions/zendesk_support/workflows/resolve_ticket" && options?.method === "PATCH") {
      return { updated: true, workflow: { id: "resolve_ticket", enabled: options.body.enabled } };
    }
    if (path === "/admin/workflows/discovered/cand-1/dismiss" && options?.method === "POST") {
      return { dismissed: true };
    }
    throw new Error(`Unexpected call: ${path}`);
  });
  vi.stubGlobal("useAdminApi", () => ({ apiFetch }));

  const wrapper = mount(WorkflowsPage, { attachTo: document.body, global: { components: sharedComponents } });
  await vi.waitFor(() => expect(wrapper.text()).toContain("Resolve support ticket"));
  expect(wrapper.text()).toContain("Verified by traffic (2×)");

  await wrapper.get('[aria-label="Toggle Resolve support ticket active state"]').trigger("click");
  await vi.waitFor(() =>
    expect(apiFetch).toHaveBeenCalledWith(
      "/admin/integration-definitions/zendesk_support/workflows/resolve_ticket",
      expect.objectContaining({ method: "PATCH", body: { enabled: false } }),
    ),
  );

  await wrapper.findAll("button").find((b) => b.text().includes("Edit Timing"))!.trigger("click");
  await vi.waitFor(() => {
    const sheet = document.querySelector('[data-slot="drawer-content"]');
    expect(sheet?.getAttribute("data-side")).toBe("right");
  });

  await wrapper.findAll("button").find((b) => b.text().includes("Dismiss"))!.trigger("click");
  await vi.waitFor(() =>
    expect(apiFetch).toHaveBeenCalledWith(
      "/admin/workflows/discovered/cand-1/dismiss",
      expect.objectContaining({ method: "POST" }),
    ),
  );
  wrapper.unmount();
});

test("MCP Apps UI page loads experiences, renders sandboxed iframe preview, and updates specs", async () => {
  const apiFetch = vi.fn(async (path: string, options?: { method?: string; body?: any }) => {
    if (path === "/admin/mcp-ui/experiences") {
      return {
        upstreams: ["hr_directory"],
        experiences: [
          {
            upstream_name: "hr_directory",
            definition_revision: 1,
            tool_description: "Look up employee profile",
            experience: {
              id: "emp_ui",
              tool: "hr_directory__get_hr_employee",
              resource_uri: "ui://mcpcodeaway/hr/emp/r1",
              renderer_schema: "mcpcodeaway-ui/1",
              fallback: "Display employee profile card",
              components: [{ type: "heading", text: "Employee Profile Card" }],
            },
          },
        ],
      };
    }
    if (path === "/admin/mcp-ui/preview") {
      return {
        valid: true,
        html: "<!doctype html><html><body><h1>Employee Profile Card</h1></body></html>",
      };
    }
    throw new Error(`Unexpected call: ${path}`);
  });
  vi.stubGlobal("useAdminApi", () => ({ apiFetch }));

  const wrapper = mount(McpUiPage, { attachTo: document.body, global: { components: sharedComponents } });
  await vi.waitFor(() => expect(wrapper.text()).toContain("Employee Profile Card"));
  const iframe = wrapper.find("iframe");
  expect(iframe.exists()).toBe(true);
  expect(iframe.attributes("srcdoc")).toContain("Employee Profile Card");
  wrapper.unmount();
});

test("Prompt-first onboarding validates, previews MCP UI, edits workflow baseline, and applies bundle", async () => {
  const appliedBodies: any[] = [];
  const apiFetch = vi.fn(async (path: string, options?: { method?: string; body?: any }) => {
    if (path === "/admin/upstreams") return { dynamic_upstreams_permitted: true, upstreams: [] };
    if (path === "/admin/roles") return { roles: [], groups: [] };
    if (path.startsWith("/admin/onboarding/prompt-pack")) {
      return { prompt: "Ask clarifying questions before outputting OnboardingBundle JSON." };
    }
    if (path === "/admin/onboarding/bundle/validate") {
      return {
        valid: true,
        upstream: options?.body?.upstream,
        integration_definition: options?.body?.integration_definition,
        warnings: [],
      };
    }
    if (path === "/admin/mcp-ui/preview") {
      return { valid: true, html: "<div>Ticket UI Preview</div>" };
    }
    if (path === "/admin/onboarding/bundle/apply") {
      appliedBodies.push(options?.body);
      return { applied: true, upstream: { upstream: { name: "support_api" } }, integration_definition: { published: true } };
    }
    throw new Error(`Unexpected call: ${path}`);
  });
  vi.stubGlobal("useAdminApi", () => ({ apiFetch }));

  const wrapper = mount(OnboardingPage, { attachTo: document.body, global: { components: sharedComponents } });
  expect(wrapper.text()).not.toContain("Agent Onboarding Prompt");
  expect(wrapper.text()).not.toContain("Import Agent JSON");
  expect(wrapper.text()).not.toContain("Manage Workflows");
  expect(wrapper.text()).not.toContain("Preview MCP Apps UI");
  expect(wrapper.find('[aria-label="Choose target integration"]').exists()).toBe(true);
  await wrapper.findAll("button").find((b) => b.text().includes("Preview Prompt"))!.trigger("click");
  await vi.waitFor(() =>
    expect(wrapper.text()).toContain("Ask clarifying questions before outputting OnboardingBundle JSON."),
  );

  const sampleBundle = {
    upstream: { name: "support_api", type: "openapi", url: "https://api.example.com" },
    integration_definition: {
      manifest_version: "1.0",
      integration: { id: "support_api", display_name: "Support", upstream_name: "support_api" },
      workflows: [
        {
          id: "create_ticket",
          name: "Create Support Ticket",
          description: "Create and verify ticket",
          enabled: true,
          participating_tools: ["support_api__create_ticket"],
          completion: {
            kind: "terminal_tool_result",
            tool: "support_api__create_ticket",
            success_when: { path: "/id", type: "non_empty_string" },
          },
          business_value: {
            manual_baseline_minutes: 10,
            baseline_source: "administrator_estimate",
            confidence: "unvalidated",
            effective_from: "2026-11-22",
          },
        },
      ],
      experiences: [
        {
          id: "ticket_ui",
          resource_uri: "ui://mcpcodeaway/support/ticket/r1",
          renderer_schema: "mcpcodeaway-ui/1",
          tool: "support_api__create_ticket",
          fallback: "Show ticket confirmation card",
          components: [{ type: "heading", text: "Ticket Creation Card" }],
        },
      ],
    },
  };

  await wrapper.get('[aria-label="Paste agent OnboardingBundle JSON"]').setValue(JSON.stringify(sampleBundle));
  await wrapper.findAll("button").find((b) => b.text().includes("Validate & Preview Bundle"))!.trigger("click");
  await vi.waitFor(() => expect(wrapper.text()).toContain("Ticket Creation Card"));
  expect(wrapper.text()).toContain("Manage Workflows");
  expect(wrapper.text()).toContain("Preview MCP Apps UI");
  expect(wrapper.find('iframe[title="Preview Ticket Creation Card"]').attributes("srcdoc")).toContain("Ticket UI Preview");

  await wrapper.get('[aria-label="Activate Create Support Ticket"]').trigger("click");
  await wrapper.findAll("button").find((b) => b.text().includes("Apply & Publish Bundle"))!.trigger("click");
  await vi.waitFor(() => expect(appliedBodies.length).toBe(1));
  expect(appliedBodies[0].integration_definition.workflows[0].enabled).toBe(false);
  wrapper.unmount();
});

test("UpstreamMcpUiCatalog previews live postMessage simulation and saves new MCP App UI", async () => {
  const experiences = [
    {
      id: "ticket_inspector_ui",
      tool: "global_ticketing__get_ticket",
      resource_uri: "ui://mcpcodeaway/global_ticketing/ticket_inspector/r1",
      renderer_schema: "mcpcodeaway-ui/1",
      fallback: "Display ticket inspector",
      components: [{ type: "heading", text: "Ticket Inspector" }],
    },
  ];
  const apiFetch = vi.fn(async (path: string, options?: { method?: string; body?: any }) => {
    if (path === "/admin/integration-definitions/global_ticketing") {
      return { current: { definition: { experiences } } };
    }
    if (path === "/admin/mcp-ui/preview") {
      return { valid: true, html: "<div>Interactive Ticket Preview</div>" };
    }
    if (path === "/admin/mcp-ui/simulate" && options?.method === "POST") {
      return {
        simulated: true,
        tool: options.body?.tool,
        arguments: options.body?.arguments,
        structuredContent: { ok: true, id: "TCK-42" },
        isError: false,
      };
    }
    if (
      path.startsWith("/admin/integration-definitions/global_ticketing/experiences/") &&
      options?.method === "PUT"
    ) {
      experiences.push(options.body.experience);
      return { saved: true };
    }
    throw new Error(`Unexpected ${options?.method ?? "GET"} ${path}`);
  });
  vi.stubGlobal("useAdminApi", () => ({ apiFetch }));

  const wrapper = mount(UpstreamMcpUiCatalog, {
    attachTo: document.body,
    props: {
      upstream: "global_ticketing",
      tools: ["global_ticketing__get_ticket", "global_ticketing__list_tickets"],
      canEdit: true,
      refreshVersion: 0,
    },
    global: { components: sharedComponents },
  });

  await vi.waitFor(() => expect(wrapper.text()).toContain("Ticket Inspector"));
  await wrapper.findAll("button").find((b) => b.text().trim() === "Preview")!.trigger("click");
  await vi.waitFor(() =>
    expect(document.querySelector('iframe[title="Preview ticket_inspector_ui"]')).not.toBeNull(),
  );

  const postedReplies: any[] = [];
  const fakeWindow = { postMessage: (msg: any) => postedReplies.push(msg) };
  window.dispatchEvent(
    new MessageEvent("message", {
      data: {
        jsonrpc: "2.0",
        id: 11,
        method: "tools/call",
        params: { name: "global_ticketing__get_ticket", arguments: { ticket_id: "TCK-42" } },
      },
      source: fakeWindow as unknown as Window,
    }),
  );

  await vi.waitFor(() => expect(postedReplies.length).toBe(1));
  expect(postedReplies[0]).toMatchObject({
    jsonrpc: "2.0",
    id: 11,
    result: { structuredContent: { ok: true, id: "TCK-42" }, isError: false },
  });

  await wrapper.findAll("button").find((b) => b.text().includes("Create MCP UI"))!.trigger("click");
  await vi.waitFor(() => expect(document.querySelector("#mcp-ui-id")).not.toBeNull());
  const saveForm = document.querySelector("form")!;
  saveForm.dispatchEvent(new Event("submit", { cancelable: true, bubbles: true }));
  await vi.waitFor(() =>
    expect(apiFetch).toHaveBeenCalledWith(
      expect.stringContaining("/admin/integration-definitions/global_ticketing/experiences/"),
      expect.objectContaining({ method: "PUT" }),
    ),
  );
  wrapper.unmount();
});

test("Workflows page dismisses schema suggestions and Onboarding honors ?upstream= query", async () => {
  const apiFetch = vi.fn(async (path: string, options?: { method?: string; body?: any }) => {
    if (path === "/admin/workflows/catalog") {
      return {
        upstreams: ["global_ticketing"],
        workflows: [],
        candidates: [
          {
            candidate_id: "cand-schema-1",
            upstream_name: "global_ticketing",
            workflow_slug: "schema_guess",
            origin: "auto_schema",
            status: "pending_review",
            observed_count: 0,
            verified_count: 0,
            sample_intents: [],
            draft_definition: { id: "schema_guess", name: "Schema Guess" },
          },
        ],
        episodes: [],
      };
    }
    if (path === "/admin/workflows/discovered/dismiss-schema" && options?.method === "POST") {
      return { dismissed_count: 1, dismissed_ids: ["cand-schema-1"] };
    }
    if (path === "/admin/upstreams") {
      return { dynamic_upstreams_permitted: true, upstreams: [{ name: "global_ticketing" }] };
    }
    if (path === "/admin/roles") return { roles: [], groups: [] };
    throw new Error(`Unexpected ${options?.method ?? "GET"} ${path}`);
  });
  vi.stubGlobal("useAdminApi", () => ({ apiFetch }));
  vi.stubGlobal("useRoute", () => ({ query: { upstream: "global_ticketing" } }));

  const wfWrapper = mount(WorkflowsPage, { attachTo: document.body, global: { components: sharedComponents } });
  await vi.waitFor(() => expect(wfWrapper.text()).toContain("Dismiss Schema Suggestions"));
  await wfWrapper.findAll("button").find((b) => b.text().includes("Dismiss Schema Suggestions"))!.trigger("click");
  await vi.waitFor(() =>
    expect(apiFetch).toHaveBeenCalledWith(
      "/admin/workflows/discovered/dismiss-schema",
      expect.objectContaining({ method: "POST", body: { upstream_name: "global_ticketing" } }),
    ),
  );
  wfWrapper.unmount();

  const onbWrapper = mount(OnboardingPage, { attachTo: document.body, global: { components: sharedComponents } });
  await vi.waitFor(() =>
    expect(onbWrapper.get('[aria-label="Choose target integration"]').text()).toContain(
      "Existing upstream: global_ticketing",
    ),
  );
  onbWrapper.unmount();
});

test("MCP Apps UI page simulates host results and creates a new UI for an unconfigured upstream tool", async () => {
  const experiences: any[] = [
    {
      upstream_name: "global_ticketing",
      definition_revision: 1,
      tool_description: "Fetch ticket details",
      experience: {
        id: "ticket_inspector_ui",
        tool: "global_ticketing__get_ticket",
        resource_uri: "ui://mcpcodeaway/global_ticketing/ticket_inspector/r1",
        renderer_schema: "mcpcodeaway-ui/1",
        fallback: "Inspect ticket details",
        components: [{ type: "heading", text: "Ticket Inspector" }],
      },
    },
  ];
  const apiFetch = vi.fn(async (path: string, options?: { method?: string; body?: any }) => {
    if (path === "/admin/mcp-ui/experiences") {
      return {
        upstreams: ["global_ticketing", "hr_directory"],
        experiences: [...experiences],
        tools_by_upstream: {
          global_ticketing: [
            {
              qualified_name: "global_ticketing__get_ticket",
              name: "get_ticket",
              description: "Fetch ticket details",
              mutating: false,
              input_schema: { type: "object", properties: { ticket_id: { type: "string" } }, required: ["ticket_id"] },
            },
          ],
          hr_directory: [
            {
              qualified_name: "hr_directory__get_hr_employee",
              name: "get_hr_employee",
              description: "Look up an employee record",
              mutating: false,
              input_schema: {
                type: "object",
                properties: {
                  employee_id: { type: "string" },
                  region: { type: "string", enum: ["EU", "US"] },
                },
                required: ["employee_id"],
              },
            },
          ],
        },
      };
    }
    if (path === "/admin/mcp-ui/preview") {
      return { valid: true, html: "<!doctype html><html><body><div>Host Rendered Card</div></body></html>" };
    }
    if (path === "/admin/mcp-ui/simulate" && options?.method === "POST") {
      return {
        simulated: true,
        tool: options.body?.tool,
        structuredContent: { ticket_id: "TCK-1042", status: "open" },
        isError: false,
      };
    }
    if (
      path.startsWith("/admin/integration-definitions/hr_directory/experiences/") &&
      options?.method === "PUT"
    ) {
      experiences.push({
        upstream_name: "hr_directory",
        definition_revision: 1,
        tool_description: "Look up an employee record",
        experience: options.body.experience,
      });
      return { saved: true };
    }
    throw new Error(`Unexpected ${options?.method ?? "GET"} ${path}`);
  });
  vi.stubGlobal("useAdminApi", () => ({ apiFetch }));

  const wrapper = mount(McpUiPage, { attachTo: document.body, global: { components: sharedComponents } });
  await vi.waitFor(() => expect(wrapper.text()).toContain("Ticket Inspector"));
  await vi.waitFor(() => expect(wrapper.text()).toContain("get_hr_employee"));

  await wrapper.findAll("button").find((b) => b.text().includes("Simulate Sample Result"))!.trigger("click");
  await vi.waitFor(() =>
    expect(apiFetch).toHaveBeenCalledWith(
      "/admin/mcp-ui/simulate",
      expect.objectContaining({
        method: "POST",
        body: { tool: "global_ticketing__get_ticket", arguments: {} },
      }),
    ),
  );
  await vi.waitFor(() => expect(wrapper.text()).toContain("Injected simulated result for global_ticketing__get_ticket"));

  await wrapper.findAll("button").find((b) => b.text().trim() === "Create UI")!.trigger("click");
  await vi.waitFor(() => expect(document.querySelector("#create-mcp-ui-id")).not.toBeNull());
  expect((document.querySelector("#create-mcp-ui-input-id") as HTMLInputElement).value).toBe("employee_id");

  const saveForm = document.querySelector("form")!;
  saveForm.dispatchEvent(new Event("submit", { cancelable: true, bubbles: true }));
  await vi.waitFor(() =>
    expect(apiFetch).toHaveBeenCalledWith(
      "/admin/integration-definitions/hr_directory/experiences/get_hr_employee_ui",
      expect.objectContaining({ method: "PUT" }),
    ),
  );
  wrapper.unmount();
});

test("UpstreamMcpUiCatalog opens PromptBundleDrawer in-page and applies bundle", async () => {
  const experiences: any[] = [];
  const apiFetch = vi.fn(async (path: string, options?: { method?: string; body?: any }) => {
    if (path === "/admin/integration-definitions/global_ticketing") {
      return { current: { definition: { experiences: [...experiences] } } };
    }
    if (path === "/admin/onboarding/prompt-pack?upstream_name=global_ticketing") {
      return { prompt: "Design MCP Apps UI for global_ticketing." };
    }
    if (path === "/admin/onboarding/bundle/validate" && options?.method === "POST") {
      return {
        valid: true,
        upstream: { name: "global_ticketing", type: "openapi" },
        integration_definition: options.body?.integration_definition,
        warnings: [],
      };
    }
    if (path === "/admin/mcp-ui/preview") {
      return { valid: true, html: "<div>Generated Ticket Card</div>" };
    }
    if (path === "/admin/onboarding/bundle/apply" && options?.method === "POST") {
      experiences.push(...(options.body?.integration_definition?.experiences ?? []));
      return {
        applied: true,
        integration_definition: { upstream_name: "global_ticketing", published: true },
      };
    }
    throw new Error(`Unexpected ${options?.method ?? "GET"} ${path}`);
  });
  vi.stubGlobal("useAdminApi", () => ({ apiFetch }));

  const wrapper = mount(UpstreamMcpUiCatalog, {
    attachTo: document.body,
    props: {
      upstream: "global_ticketing",
      tools: ["global_ticketing__get_ticket"],
      canEdit: true,
      refreshVersion: 0,
    },
    global: { components: sharedComponents },
  });

  try {
    await vi.waitFor(() =>
      expect(wrapper.text()).toContain("No MCP Apps UI views attached to tools on this upstream yet."),
    );
    await wrapper.findAll("button").find((b) => b.text().includes("Generate UI via Prompt"))!.trigger("click");
    await vi.waitFor(() =>
      expect(document.body.textContent).toContain("1. Target Upstream & Agent Prompt"),
    );

    const previewBtn = [...document.querySelectorAll("button")].find((b) =>
      b.textContent?.includes("Preview Prompt"),
    )!;
    previewBtn.click();
    await vi.waitFor(() =>
      expect(document.body.textContent).toContain("Design MCP Apps UI for global_ticketing."),
    );
    expect(document.body.textContent).toContain("Target Focus (MCP Apps UI)");

    const bundle = {
      integration_definition: {
        manifest_version: "1.0",
        integration: {
          id: "global_ticketing",
          display_name: "Global Ticketing",
          upstream_name: "global_ticketing",
        },
        workflows: [],
        experiences: [
          {
            id: "ticket_card_ui",
            resource_uri: "ui://mcpcodeaway/global_ticketing/ticket_card/r1",
            renderer_schema: "mcpcodeaway-ui/1",
            tool: "global_ticketing__get_ticket",
            fallback: "Ticket summary",
            components: [{ type: "heading", text: "Generated Ticket Card" }],
          },
        ],
      },
    };

    const textarea = document.querySelector(
      'textarea[aria-label="Paste agent OnboardingBundle JSON"]',
    ) as HTMLTextAreaElement;
    textarea.value = JSON.stringify(bundle);
    textarea.dispatchEvent(new Event("input", { bubbles: true }));
    await nextTick();

    const validateBtn = [...document.querySelectorAll("button")].find((b) =>
      b.textContent?.includes("Validate & Preview Bundle"),
    ) as HTMLButtonElement;
    await vi.waitFor(() => expect(validateBtn.disabled).toBe(false));
    validateBtn.click();
    await vi.waitFor(() =>
      expect(document.querySelector('iframe[title="Preview Generated Ticket Card"]')).not.toBeNull(),
    );

    const applyBtn = [...document.querySelectorAll("button")].find(
      (b) => b.textContent?.trim() === "Apply & Publish",
    ) as HTMLButtonElement;
    await vi.waitFor(() => expect(applyBtn.disabled).toBe(false));
    applyBtn.click();
    await vi.waitFor(() => expect(wrapper.text()).toContain("Generated Ticket Card"));
  } finally {
    wrapper.unmount();
  }
});

test("Workflows page syncs confidence and baseline_source, shows drawer errors inline, and opens Create Workflow & Prompt drawers in-page", async () => {
  let failNextPatch = true;
  const patchBodies: any[] = [];
  const promoteBodies: any[] = [];
  const apiFetch = vi.fn(async (path: string, options?: { method?: string; body?: any }) => {
    if (path === "/admin/workflows/catalog") {
      return {
        upstreams: ["zendesk_support"],
        workflows: [
          {
            upstream_name: "zendesk_support",
            definition_revision: 2,
            workflow: {
              id: "resolve_ticket",
              name: "Resolve support ticket",
              description: "Verify ticket creation",
              enabled: true,
              participating_tools: ["zendesk_support__create_ticket"],
              intent_examples: ["Open a ticket"],
              completion: {
                kind: "terminal_tool_result",
                tool: "zendesk_support__create_ticket",
                success_when: { path: "/status", type: "non_empty_string" },
              },
              business_value: {
                manual_baseline_minutes: 12,
                baseline_source: "administrator_estimate",
                confidence: "unvalidated",
                effective_from: "2026-11-22",
              },
            },
          },
        ],
        candidates: [
          {
            candidate_id: "cand-1",
            upstream_name: "zendesk_support",
            workflow_slug: "auto_escalation",
            origin: "auto_mined",
            status: "pending_review",
            observed_count: 4,
            verified_count: 2,
            sample_intents: ["Escalate billing dispute"],
            draft_definition: {
              id: "auto_escalation",
              name: "Escalate ticket",
              description: "Verified multi-step escalation",
              participating_tools: ["zendesk_support__create_ticket"],
            },
          },
        ],
        episodes: [],
      };
    }
    if (
      path === "/admin/integration-definitions/zendesk_support/workflows/resolve_ticket" &&
      options?.method === "PATCH"
    ) {
      if (failNextPatch) {
        failNextPatch = false;
        throw new Error("Inline validation failure from gateway");
      }
      patchBodies.push(options.body);
      return { updated: true, workflow: { id: "resolve_ticket" } };
    }
    if (path === "/admin/workflows/discovered/cand-1/promote" && options?.method === "POST") {
      promoteBodies.push(options.body);
      return { promoted: true };
    }
    if (path === "/admin/upstreams") {
      return {
        upstreams: [{ name: "zendesk_support", tools: ["zendesk_support__create_ticket"] }],
      };
    }
    if (path === "/admin/integration-definitions/zendesk_support") {
      return { current: null };
    }
    throw new Error(`Unexpected ${options?.method ?? "GET"} ${path}`);
  });
  vi.stubGlobal("useAdminApi", () => ({ apiFetch }));

  const wrapper = mount(WorkflowsPage, {
    attachTo: document.body,
    global: { components: sharedComponents },
  });
  try {
    await vi.waitFor(() => expect(wrapper.text()).toContain("Resolve support ticket"));

    await wrapper.findAll("button").find((b) => b.text().includes("Edit Timing"))!.trigger("click");
    await vi.waitFor(() => expect(document.querySelector('[aria-label="Workflow confidence"]')).not.toBeNull());

    const confTrigger = document.querySelector('[aria-label="Workflow confidence"]') as HTMLElement;
    confTrigger.click();
    await vi.waitFor(() =>
      expect([...document.querySelectorAll('[role="option"]')].some((n) => n.textContent?.includes("Validated"))).toBe(true),
    );
    const validatedOption = [...document.querySelectorAll('[role="option"]')].find(
      (n) => n.textContent?.trim() === "Validated",
    ) as HTMLElement;
    validatedOption.click();

    await vi.waitFor(() =>
      expect(document.querySelector('[aria-label="Baseline source"]')?.textContent).toContain(
        "Validated external measurement",
      ),
    );

    const editFormEl = document.querySelector('[data-slot="drawer-content"] form') as HTMLFormElement;
    editFormEl.dispatchEvent(new Event("submit", { cancelable: true, bubbles: true }));
    await vi.waitFor(() =>
      expect(document.querySelector('[data-slot="drawer-content"]')?.textContent).toContain(
        "Inline validation failure from gateway",
      ),
    );

    editFormEl.dispatchEvent(new Event("submit", { cancelable: true, bubbles: true }));
    await vi.waitFor(() => expect(patchBodies.length).toBe(1));
    expect(patchBodies[0]).toMatchObject({
      confidence: "validated",
      baseline_source: "validated_external_measurement",
    });

    await wrapper.findAll("button").find((b) => b.text().includes("Set Baseline & Activate"))!.trigger("click");
    await vi.waitFor(() =>
      expect(document.querySelector('[aria-label="Candidate baseline source"]')).not.toBeNull(),
    );
    const candSourceTrigger = document.querySelector(
      '[aria-label="Candidate baseline source"]',
    ) as HTMLElement;
    candSourceTrigger.click();
    await vi.waitFor(() =>
      expect(
        [...document.querySelectorAll('[role="option"]')].some((n) =>
          n.textContent?.includes("Sampled manual measurement"),
        ),
      ).toBe(true),
    );
    (
      [...document.querySelectorAll('[role="option"]')].find((n) =>
        n.textContent?.includes("Sampled manual measurement"),
      ) as HTMLElement
    ).click();

    const promoteFormEl = document.querySelector('[data-slot="drawer-content"] form') as HTMLFormElement;
    promoteFormEl.dispatchEvent(new Event("submit", { cancelable: true, bubbles: true }));
    await vi.waitFor(() => expect(promoteBodies.length).toBe(1));
    expect(promoteBodies[0]).toMatchObject({
      baseline_source: "sampled_manual_measurement",
      confidence: "sampled",
      publish: true,
    });

    await wrapper.findAll("button").find((b) => b.text().trim() === "Create Workflow")!.trigger("click");
    await vi.waitFor(() => expect(document.querySelector("#workflow-name")).not.toBeNull());
    expect(document.querySelector('[aria-label="Workflow upstream"]')).not.toBeNull();
    (document.querySelector('[aria-label="Close workflow editor"]') as HTMLElement).click();
    await vi.waitFor(() => expect(document.querySelector("#workflow-name")).toBeNull());

    await wrapper.findAll("button").find((b) => b.text().includes("Generate Workflows via Prompt"))!.trigger("click");
    await vi.waitFor(() =>
      expect(document.body.textContent).toContain("1. Target Upstream & Agent Prompt"),
    );
  } finally {
    wrapper.unmount();
  }
});

test("Workflows & ROI renders top ROI summary, validation badges, per-workflow evidences, and ledger tab", async () => {
  const catalog = {
    upstreams: ["crm_accounts"],
    workflows: [
      {
        upstream_name: "crm_accounts",
        definition_revision: 1,
        workflow: {
          id: "account_risk_review",
          name: "Account Risk Review",
          description: "Inspect enterprise accounts.",
          enabled: true,
          participating_tools: ["crm_accounts__list_crm_accounts"],
          completion: {
            kind: "verified_followup",
            tool: "crm_accounts__list_crm_accounts_metrics",
            success_when: { path: "/active_items", type: "integer" },
          },
          business_value: {
            manual_baseline_minutes: 12,
            baseline_source: "validated_external_measurement",
            confidence: "validated",
            loaded_labor_cost_per_hour: 110,
            currency: "USD",
            evidence_note: "Validated RevOps benchmark across 40 QBRs.",
          },
        },
      },
      {
        upstream_name: "crm_accounts",
        definition_revision: 1,
        workflow: {
          id: "account_draft_review",
          name: "Account Draft Review",
          description: "Draft unvalidated review flow.",
          enabled: false,
          participating_tools: ["crm_accounts__list_crm_accounts"],
          completion: {
            kind: "terminal_tool_result",
            tool: "crm_accounts__list_crm_accounts",
            success_when: { path: "/count", type: "integer" },
          },
          business_value: {
            manual_baseline_minutes: 8,
            baseline_source: "administrator_estimate",
            confidence: "unvalidated",
          },
        },
      },
    ],
    candidates: [],
    episodes: [
      {
        episode_id: "ep-101",
        upstream_name: "crm_accounts",
        matched_workflow_id: "crm_accounts/account_risk_review",
        status: "completed_verified",
        completion_tier: "predicate_matched",
        manual_baseline_minutes: 12,
        updated_at: "2026-11-23T14:00:00Z",
      },
    ],
  };

  const report = {
    tenant_scope: "tenant-acme-dev",
    since: null,
    events_returned: 2,
    truncated: false,
    executions_returned: 2,
    executions_truncated: false,
    completed_count: 2,
    execution_status_counts: { active: 0, completed: 2, outcome_unknown: 0, failed: 0, expired: 0 },
    by_integration_workflow_day: [
      {
        integration_id: "crm_accounts",
        integration_display_name: "Enterprise CRM Accounts",
        upstream_name: "crm_accounts",
        selected_kind: "workflow",
        selected_id: "crm_accounts/account_risk_review",
        definition_revision: 1,
        period_utc: "2026-11-23",
        baseline_source: "validated_external_measurement",
        baseline_confidence: "validated",
        currency: "USD",
        completed_count: 2,
        estimated_manual_baseline_minutes: 24,
        validated_manual_baseline_minutes: 24,
        estimated_minutes_saved: 20,
        estimated_labor_value: { amount: 36.67, currency: "USD" },
      },
    ],
    totals_by_currency: {
      USD: {
        completed_count: 2,
        estimated_manual_baseline_minutes: 24,
        validated_manual_baseline_minutes: 24,
        estimated_minutes_saved: 20,
      },
    },
    validated_minutes_saved: 20,
    cost_coverage: {
      captured_categories: [],
      missing_categories: ["llm_inference"],
      roi_available: false,
    },
  };

  const apiFetch = vi.fn(async (path: string) => {
    if (path === "/admin/workflows/catalog") return catalog;
    if (path === "/admin/business-value/report") return report;
    throw new Error(`Unexpected ${path}`);
  });
  vi.stubGlobal("useAdminApi", () => ({ apiFetch, session: ref({ is_admin: true }) }));

  const wrapper = mount(WorkflowsPage, {
    attachTo: document.body,
    global: { components: sharedComponents },
  });
  try {
    await vi.waitFor(() => expect(wrapper.text()).toContain("Account Risk Review"));
    await nextTick();
    expect(wrapper.text()).toContain("Workflows & ROI");
    expect(wrapper.text()).toContain("20 min");
    expect(wrapper.text()).toContain("USD 36.67");
    expect(wrapper.text()).toContain("1 validated");
    expect(wrapper.text()).toContain("1 est.");
    expect(wrapper.text()).toContain("Validated baseline");
    expect(wrapper.text()).toContain("Unvalidated estimate");
    expect(wrapper.text()).toContain("2 completed outcomes");
    expect(wrapper.text()).toContain("1 verified run");
    expect(wrapper.text()).toContain("Validated RevOps benchmark across 40 QBRs.");
    expect(wrapper.text()).toContain("No completed run evidence yet");
    expect(wrapper.text()).toContain("completed verified");

    const ledgerTab = wrapper.findAll('[role="tab"]').find((el) => el.text().includes("Outcome & ROI Ledger"));
    expect(ledgerTab).toBeDefined();
    await ledgerTab!.trigger("click");
    await vi.waitFor(() => expect(wrapper.text()).toContain("Outcome report"));
    expect(wrapper.text()).toContain("Enterprise CRM Accounts");
  } finally {
    wrapper.unmount();
  }
});
