import { mount } from "@vue/test-utils";
import { createAgent } from "@ui-sdk/vue/agent";
import { Button } from "@ui-sdk/vue/button";
import { Dialog, DialogBackdrop, DialogClose, DialogContent, DialogDescription, DialogTitle } from "@ui-sdk/vue/dialog";
import { Field, FieldDescription, FieldLabel } from "@ui-sdk/vue/field";
import { Input } from "@ui-sdk/vue/input";
import { Meter, MeterIndicator, MeterLabel, MeterTrack, MeterValue } from "@ui-sdk/vue/meter";
import { Select, SelectContent, SelectItem, SelectItemIndicator, SelectTrigger, SelectValue } from "@ui-sdk/vue/select";
import { Switch, SwitchThumb } from "@ui-sdk/vue/switch";
import { Tabs, TabsList, TabsPanel, TabsTab } from "@ui-sdk/vue/tabs";
import { Textarea } from "@ui-sdk/vue/textarea";
import { defineComponent, h } from "vue";
import { expect, test, vi } from "vitest";
import { useAdminApi } from "~/composables/useAdminApi";
import OnboardingPage from "~/pages/onboarding.vue";

vi.stubGlobal("useAdminApi", useAdminApi);

const AppIconStub = defineComponent({
  name: "AppIcon",
  props: ["name", "size"],
  render: () => h("span", { "aria-hidden": "true" }),
});

const uiSdkGlobalComponents = {
  AppIcon: AppIconStub,
  Button,
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
  Meter,
  MeterIndicator,
  MeterLabel,
  MeterTrack,
  MeterValue,
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

test("UI agent discovers named controls, sets onboarding fields, and respects data-agent-confirm", async () => {
  let approveMutation = false;
  const agent = createAgent({
    allow: (step) => !step.confirm || approveMutation,
  });

  const wrapper = mount(OnboardingPage, {
    attachTo: document.body,
    global: { components: uiSdkGlobalComponents },
  });

  const components = agent
    .snapshot()
    .filter((entry) => !entry.offPage && entry.component !== "more");
  const labels = components.map((entry) => entry.label);

  for (const requiredLabel of [
    "Upstream Name",
    "Base / MCP Endpoint URL",
    "Inspect Schema",
    "Quick REST Builder",
    "3. A/B Test & Save",
  ]) {
    expect(labels, `Missing control '${requiredLabel}' in agent snapshot`).toContain(requiredLabel);
  }

  const editRes = await agent.act({
    steps: [
      { ref: "Upstream Name", action: "setValue", args: ["crm_partner_api"] },
      { ref: "Base / MCP Endpoint URL", action: "setValue", args: ["http://localhost:8001/crm"] },
    ],
  });
  expect(editRes.ok, editRes.error?.message).toBe(true);
  expect((wrapper.find("input").element as HTMLInputElement).value).toBe("crm_partner_api");

  // Inspect Schema first so Step 2 & Step 3 unlock cleanly
  const inspectStep = await agent.act({
    steps: [{ ref: "Inspect Schema", action: "press" }],
  });
  expect(inspectStep.ok, inspectStep.error?.message).toBe(true);
  await Promise.resolve();

  // Advance to Step 2 and verify auth-specific fields mount when delegated_token is selected
  const navStep2 = await agent.act({
    steps: [{ ref: "2. Policy & Mode", action: "press" }],
  });
  expect(navStep2.ok, navStep2.error?.message).toBe(true);
  const step2Labels = agent
    .snapshot()
    .filter((entry) => !entry.offPage && entry.component !== "more")
    .map((entry) => entry.label);
  expect(step2Labels).toContain("Custom / Selected Roles");
  expect(step2Labels).toContain("Allowed Groups (comma-separated)");

  // Advance to Step 3 so Register Upstream mounts cleanly
  const navStep3 = await agent.act({
    steps: [{ ref: "3. A/B Test & Save", action: "press" }],
  });
  expect(navStep3.ok, navStep3.error?.message).toBe(true);
  const step3Labels = agent
    .snapshot()
    .filter((entry) => !entry.offPage && entry.component !== "more")
    .map((entry) => entry.label);
  expect(step3Labels).toContain("Register Upstream");

  // Mutating Register Upstream must be blocked when allow() denies confirmation.
  const denied = await agent.act({
    steps: [{ ref: "Register Upstream", action: "press" }],
  });
  expect(denied.ok).toBe(false);

  // Allowed once operator approves confirmation.
  approveMutation = true;
  const approved = await agent.act({
    steps: [{ ref: "Register Upstream", action: "press" }],
  });
  expect(approved.ok, approved.error?.message).toBe(true);

  wrapper.unmount();
});
