<script setup lang="ts">
import { Button } from "@ui-sdk/vue/button";
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
import { Textarea } from "@ui-sdk/vue/textarea";
import { computed, ref, watch } from "vue";
import AppIcon from "~/components/AppIcon.vue";
import McpAppHostPreview from "~/components/McpAppHostPreview.vue";
import { extractErrorMessage } from "~/composables/useAdminApi";

const props = withDefaults(
  defineProps<{
    open: boolean;
    upstream?: string;
    upstreams?: string[];
    focus?: "workflows" | "mcp_ui" | "all";
  }>(),
  {
    upstream: "",
    upstreams: undefined,
    focus: "all",
  },
);

const emit = defineEmits<{
  (event: "update:open", value: boolean): void;
  (event: "applied", upstreamName: string): void;
}>();

const { apiFetch } = useAdminApi();

const selectedUpstream = ref("");
const generatedPrompt = ref("");
const promptLoading = ref(false);
const bundleJsonText = ref("");
const bundleValidating = ref(false);
const bundleApplying = ref(false);
const errorMessage = ref("");
const statusMessage = ref("");

interface EditableBundleWorkflow {
  id: string;
  name: string;
  description: string;
  enabled: boolean;
  manualMinutes: number;
  intentsText: string;
  completionTool: string;
  participatingTools: string[];
}

interface ExperiencePreviewItem {
  id: string;
  title: string;
  tool: string;
  resource_uri: string;
  html: string;
  error?: string;
}

const bundlePreview = ref<{
  upstream: Record<string, any>;
  integration_definition: Record<string, any> | null;
  warnings: string[];
  parsedBundle: Record<string, any>;
  editableWorkflows: EditableBundleWorkflow[];
  experiencePreviews: ExperiencePreviewItem[];
} | null>(null);

const hasSelectableUpstreams = computed(
  () => Array.isArray(props.upstreams) && props.upstreams.length > 0,
);
const isUpstreamLocked = computed(
  () => !hasSelectableUpstreams.value && Boolean(props.upstream),
);
const upstreamSelectItems = computed(() =>
  (props.upstreams ?? []).map((name) => ({ value: name, label: name })),
);

const drawerTitleText = computed(() => {
  if (props.focus === "workflows") return "Generate Workflows via Prompt";
  if (props.focus === "mcp_ui") return "Generate MCP Apps UI via Prompt";
  return "Generate Integration Bundle via Prompt";
});

const drawerDescriptionText = computed(() => {
  if (props.focus === "workflows") {
    return "Copy a workflow-focused prompt for your agent, paste its OnboardingBundle JSON, tune baselines or triggers, and publish directly without leaving this page.";
  }
  if (props.focus === "mcp_ui") {
    return "Copy an MCP Apps UI prompt for your agent, paste its OnboardingBundle JSON, preview live sandboxed ui:// cards, and publish directly without leaving this page.";
  }
  return "Copy a structured onboarding prompt for your agent, validate and preview the returned OnboardingBundle JSON, and publish directly.";
});

watch(
  () => [props.open, props.upstream, props.upstreams] as const,
  ([isOpen]) => {
    if (!isOpen) return;
    const nextUpstream = props.upstream || props.upstreams?.[0] || "";
    if (selectedUpstream.value !== nextUpstream) {
      selectedUpstream.value = nextUpstream;
      generatedPrompt.value = "";
      bundlePreview.value = null;
    }
    errorMessage.value = "";
    statusMessage.value = "";
  },
  { immediate: true },
);

function handleUpstreamChange(value: string) {
  selectedUpstream.value = value;
  generatedPrompt.value = "";
  bundlePreview.value = null;
  errorMessage.value = "";
  statusMessage.value = "";
}

function buildFocusSuffix(target: string): string {
  const scopeLabel = target || "this upstream";
  if (props.focus === "workflows") {
    return (
      `\n\nTarget Focus (Workflows):\n` +
      `Focus specifically on \`integration_definition.workflows\` for '${scopeLabel}'. ` +
      `Define participating_tools, required_evidence, terminal completion predicates, ` +
      `manual_baseline_minutes, and intent_examples triggers. Return valid OnboardingBundle JSON with "publish": true.`
    );
  }
  if (props.focus === "mcp_ui") {
    return (
      `\n\nTarget Focus (MCP Apps UI):\n` +
      `Focus specifically on \`integration_definition.experiences\` (\`mcpcodeaway-ui/1\`) for '${scopeLabel}'. ` +
      `Attach \`ui://\` resource_uri views, input_mapping, fallback summaries, and declarative components. ` +
      `Return valid OnboardingBundle JSON with "publish": true.`
    );
  }
  return "";
}

async function fetchPrompt(copyAfter: boolean) {
  errorMessage.value = "";
  statusMessage.value = "";
  promptLoading.value = true;
  try {
    const target = selectedUpstream.value.trim();
    const query = target ? `?upstream_name=${encodeURIComponent(target)}` : "";
    const res = await apiFetch<{ prompt: string }>(`/admin/onboarding/prompt-pack${query}`);
    const fullPrompt = `${res.prompt}${buildFocusSuffix(target)}`;
    generatedPrompt.value = fullPrompt;
    if (copyAfter && typeof navigator !== "undefined" && navigator.clipboard?.writeText) {
      await navigator.clipboard.writeText(fullPrompt);
      statusMessage.value = "Copied prompt to clipboard. Paste it into your agent, then paste the returned JSON below.";
    } else {
      statusMessage.value = target
        ? `Built prompt for '${target}'.`
        : "Built prompt. Copy it to your agent, then paste the returned JSON below.";
    }
  } catch (err) {
    errorMessage.value = extractErrorMessage(err);
  } finally {
    promptLoading.value = false;
  }
}

async function validateAndPreviewBundle() {
  errorMessage.value = "";
  statusMessage.value = "";
  bundleValidating.value = true;
  try {
    let parsed: Record<string, any>;
    try {
      parsed = JSON.parse(bundleJsonText.value) as Record<string, any>;
    } catch {
      throw new Error("Paste valid JSON for the OnboardingBundle before validating.");
    }
    const payload = { ...parsed, publish: true };
    const validated = await apiFetch<{
      valid: boolean;
      upstream?: Record<string, any>;
      integration_definition?: Record<string, any> | null;
      warnings?: string[];
    }>("/admin/onboarding/bundle/validate", {
      method: "POST",
      body: payload,
    });
    const def = validated.integration_definition ?? null;
    const rawWorkflows = Array.isArray(def?.workflows) ? def.workflows : [];
    const editableWorkflows: EditableBundleWorkflow[] = rawWorkflows.map((wf: Record<string, any>) => ({
      id: String(wf.id ?? ""),
      name: String(wf.name ?? wf.id ?? ""),
      description: String(wf.description ?? ""),
      enabled: wf.enabled !== false,
      manualMinutes: Number(wf.business_value?.manual_baseline_minutes ?? 10),
      intentsText: Array.isArray(wf.intent_examples) ? wf.intent_examples.join(", ") : "",
      completionTool: String(wf.completion?.tool ?? ""),
      participatingTools: Array.isArray(wf.participating_tools)
        ? wf.participating_tools.map((t: any) => String(t))
        : [],
    }));
    const rawExperiences = Array.isArray(def?.experiences) ? def.experiences : [];
    const experiencePreviews: ExperiencePreviewItem[] = [];
    for (const exp of rawExperiences) {
      const headingNode = Array.isArray(exp.components)
        ? exp.components.find((c: any) => c?.type === "heading" && c?.text)
        : null;
      const expTitle = String(exp.title ?? headingNode?.text ?? exp.id ?? "MCP App UI");
      try {
        const preview = await apiFetch<{ html: string }>("/admin/mcp-ui/preview", {
          method: "POST",
          body: { experience: exp },
        });
        experiencePreviews.push({
          id: String(exp.id ?? ""),
          title: expTitle,
          tool: String(exp.tool ?? ""),
          resource_uri: String(exp.resource_uri ?? ""),
          html: preview.html,
        });
      } catch (err) {
        experiencePreviews.push({
          id: String(exp.id ?? ""),
          title: expTitle,
          tool: String(exp.tool ?? ""),
          resource_uri: String(exp.resource_uri ?? ""),
          html: "",
          error: extractErrorMessage(err),
        });
      }
    }
    bundlePreview.value = {
      upstream: validated.upstream ?? {},
      integration_definition: def,
      warnings: validated.warnings ?? [],
      parsedBundle: parsed,
      editableWorkflows,
      experiencePreviews,
    };
    const previewName = String(
      validated.upstream?.name ??
        def?.integration?.upstream_name ??
        selectedUpstream.value ??
        "bundle",
    );
    statusMessage.value = `Validated bundle for '${previewName}' (${editableWorkflows.length} workflow(s), ${experiencePreviews.length} UI view(s)). Review below and click Apply & Publish.`;
  } catch (err) {
    bundlePreview.value = null;
    errorMessage.value = extractErrorMessage(err);
  } finally {
    bundleValidating.value = false;
  }
}

async function applyAndPublish() {
  if (!bundlePreview.value) return;
  errorMessage.value = "";
  statusMessage.value = "";
  bundleApplying.value = true;
  try {
    const bundleToSend = JSON.parse(
      JSON.stringify(bundlePreview.value.parsedBundle),
    ) as Record<string, any>;
    if (
      bundleToSend.integration_definition &&
      Array.isArray(bundleToSend.integration_definition.workflows)
    ) {
      const editsById = new Map(
        bundlePreview.value.editableWorkflows.map((item) => [item.id, item]),
      );
      bundleToSend.integration_definition.workflows =
        bundleToSend.integration_definition.workflows.map((wf: Record<string, any>) => {
          const edited = editsById.get(String(wf.id));
          if (!edited) return wf;
          const nextBv = {
            ...(wf.business_value ?? {}),
            manual_baseline_minutes: Math.max(0.1, Number(edited.manualMinutes) || 10),
          };
          const parsedIntents = edited.intentsText
            .split(",")
            .map((s) => s.trim())
            .filter(Boolean);
          return {
            ...wf,
            enabled: edited.enabled,
            business_value: nextBv,
            ...(parsedIntents.length ? { intent_examples: parsedIntents } : {}),
          };
        });
    }
    const res = await apiFetch<{
      applied: boolean;
      upstream?: { upstream?: { name?: string } };
      integration_definition?: { upstream_name?: string; published?: boolean };
    }>("/admin/onboarding/bundle/apply", {
      method: "POST",
      body: { ...bundleToSend, publish: true },
    });
    const upstreamName = String(
      res.upstream?.upstream?.name ??
        res.integration_definition?.upstream_name ??
        bundlePreview.value.upstream?.name ??
        selectedUpstream.value ??
        "",
    );
    bundlePreview.value = null;
    bundleJsonText.value = "";
    emit("applied", upstreamName);
    emit("update:open", false);
  } catch (err) {
    errorMessage.value = extractErrorMessage(err);
  } finally {
    bundleApplying.value = false;
  }
}
</script>

<template>
  <Drawer :open="open" side="right" prevent-edge-swipe @update:open="emit('update:open', $event)">
    <DrawerBackdrop />
    <DrawerContent
      class="flex h-dvh w-full max-w-[95vw] flex-col gap-0 border-l border-[#E4E4E7] bg-white p-0 sm:w-[42rem]"
    >
      <div class="shrink-0 flex items-start justify-between gap-3 border-b border-[#E4E4E7] px-5 py-4">
        <div class="min-w-0 space-y-1">
          <DrawerTitle>{{ drawerTitleText }}</DrawerTitle>
          <DrawerDescription>{{ drawerDescriptionText }}</DrawerDescription>
        </div>
        <DrawerClose
          aria-label="Close prompt bundle drawer"
          class="inline-flex min-h-9 min-w-9 shrink-0 items-center justify-center rounded-md border border-[#D4D4D8] bg-white text-[#09090B] hover:bg-[#F4F4F5] focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-[#0F766E]"
        >
          <AppIcon name="x" :size="16" />
        </DrawerClose>
      </div>

      <div class="min-h-0 flex-1 overflow-y-auto overflow-x-hidden p-5 sm:p-6 space-y-4">
        <div
          v-if="errorMessage"
          role="alert"
          class="flex items-start gap-2 rounded-md border border-[#FECACA] bg-[#FEF2F2] px-3 py-2.5 text-[12px] text-[#DC2626]"
        >
          <AppIcon name="alert" :size="15" class="mt-0.5 shrink-0" />
          <span>{{ errorMessage }}</span>
        </div>

        <div
          v-if="statusMessage"
          role="status"
          class="rounded-md border border-[#99F6E4] bg-[#F0FDFA] px-3 py-2 text-[12px] text-[#0F766E]"
        >
          {{ statusMessage }}
        </div>

        <section class="space-y-3 rounded-md border border-[#E4E4E7] p-4">
          <div class="flex flex-wrap items-start justify-between gap-2">
            <div>
              <h3 class="text-[13px] font-semibold text-[#09090B]">
                1. Target Upstream &amp; Agent Prompt
              </h3>
              <p class="text-[12px] text-[#52525B]">
                Generate a prompt tailored with current upstream tool schemas and pending candidates.
              </p>
            </div>
            <div class="flex flex-wrap items-center gap-2">
              <Button class="min-h-9 text-xs" :disabled="promptLoading" @click="fetchPrompt(false)">
                {{ promptLoading ? "Building…" : "Preview Prompt" }}
              </Button>
              <Button
                data-variant="primary"
                class="min-h-9 text-xs"
                :disabled="promptLoading"
                @click="fetchPrompt(true)"
              >
                <AppIcon name="copy" :size="13" />
                <span>Copy Prompt</span>
              </Button>
            </div>
          </div>

          <div v-if="isUpstreamLocked" class="flex items-center gap-2 text-[12px]">
            <span class="text-[#52525B]">Target upstream:</span>
            <span class="rounded border border-[#E4E4E7] bg-[#F4F4F5] px-2 py-0.5 font-mono-data text-[#09090B]">
              {{ selectedUpstream }}
            </span>
          </div>
          <Field v-else-if="hasSelectableUpstreams">
            <FieldLabel>Target upstream</FieldLabel>
            <Select
              :items="upstreamSelectItems"
              :value="selectedUpstream"
              @value-change="(val) => { if (val) handleUpstreamChange(String(val)); }"
            >
              <SelectTrigger aria-label="Prompt target upstream" class="min-h-10">
                <SelectValue placeholder="Choose upstream" />
                <AppIcon name="chevron-down" :size="14" />
              </SelectTrigger>
              <SelectContent>
                <SelectItem
                  v-for="item in upstreamSelectItems"
                  :key="item.value"
                  :value="item.value"
                >
                  {{ item.label }}
                  <SelectItemIndicator><AppIcon name="check" :size="14" /></SelectItemIndicator>
                </SelectItem>
              </SelectContent>
            </Select>
          </Field>
          <Field v-else>
            <FieldLabel for="prompt-bundle-upstream">Target upstream slug (optional)</FieldLabel>
            <Input
              id="prompt-bundle-upstream"
              v-model="selectedUpstream"
              class="font-mono-data text-xs"
              placeholder="e.g. support_api"
            />
          </Field>

          <pre
            v-if="generatedPrompt"
            class="max-h-52 overflow-auto rounded border border-[#E4E4E7] bg-[#F4F4F5] p-3 font-mono-data text-[11px] text-[#09090B]"
          >{{ generatedPrompt }}</pre>
        </section>

        <section class="space-y-3 rounded-md border border-[#E4E4E7] p-4">
          <div class="flex flex-wrap items-start justify-between gap-2">
            <div>
              <h3 class="text-[13px] font-semibold text-[#09090B]">
                2. Paste OnboardingBundle JSON &amp; Validate
              </h3>
              <p class="text-[12px] text-[#52525B]">
                Validate workflows, adjust human baselines or triggers, and preview any MCP Apps UI.
              </p>
            </div>
            <Button
              class="min-h-9 text-xs"
              :disabled="bundleValidating || !bundleJsonText.trim()"
              @click="validateAndPreviewBundle"
            >
              {{ bundleValidating ? "Validating…" : "Validate & Preview Bundle" }}
            </Button>
          </div>

          <Field>
            <FieldLabel for="prompt-bundle-json">Agent JSON bundle</FieldLabel>
            <Textarea
              id="prompt-bundle-json"
              v-model="bundleJsonText"
              aria-label="Paste agent OnboardingBundle JSON"
              rows="7"
              class="font-mono-data text-xs"
              placeholder='{"integration_definition": {"workflows": [...], "experiences": [...]}, "publish": true}'
            />
            <FieldDescription>
              Accepts full or definition-only <code class="font-mono-data">OnboardingBundle</code> JSON.
            </FieldDescription>
          </Field>

          <div v-if="bundlePreview" class="space-y-4 border-t border-[#E4E4E7] pt-3">
            <ul v-if="bundlePreview.warnings.length" class="space-y-1 text-[12px] text-[#D97706]">
              <li v-for="(warning, idx) in bundlePreview.warnings" :key="idx">• {{ warning }}</li>
            </ul>

            <div v-if="bundlePreview.editableWorkflows.length" class="space-y-2">
              <h4 class="text-[12px] font-semibold text-[#09090B]">
                Proposed Workflows ({{ bundlePreview.editableWorkflows.length }})
              </h4>
              <div class="divide-y divide-[#E4E4E7] border-y border-[#E4E4E7]">
                <div
                  v-for="wf in bundlePreview.editableWorkflows"
                  :key="wf.id"
                  class="space-y-2 py-3"
                >
                  <div class="flex flex-wrap items-start justify-between gap-2">
                    <div class="min-w-0">
                      <div class="text-[13px] font-semibold text-[#09090B]">{{ wf.name }}</div>
                      <div class="text-[12px] text-[#52525B]">{{ wf.description }}</div>
                    </div>
                    <label class="inline-flex min-h-9 items-center gap-2 text-[12px] font-medium text-[#09090B]">
                      <Switch
                        :checked="wf.enabled"
                        :aria-label="`Activate ${wf.name}`"
                        @checked-change="(checked) => { wf.enabled = checked === true; }"
                      >
                        <SwitchThumb />
                      </Switch>
                      <span>{{ wf.enabled ? "Active" : "Paused" }}</span>
                    </label>
                  </div>
                  <div class="grid grid-cols-1 gap-2 sm:grid-cols-2">
                    <label class="block text-[11px] font-medium text-[#52525B]">
                      Manual baseline (min)
                      <Input
                        :model-value="String(wf.manualMinutes)"
                        :aria-label="`Manual baseline minutes for ${wf.name}`"
                        type="number"
                        min="0.1"
                        step="0.1"
                        class="mt-1 tabular-nums"
                        @update:model-value="wf.manualMinutes = Number($event)"
                      />
                    </label>
                    <label class="block text-[11px] font-medium text-[#52525B]">
                      Triggers (comma-separated)
                      <Input
                        v-model="wf.intentsText"
                        :aria-label="`Triggers for ${wf.name}`"
                        class="mt-1 text-xs"
                        placeholder="comma separated"
                      />
                    </label>
                  </div>
                  <div
                    v-if="wf.participatingTools.length"
                    class="flex flex-wrap items-center gap-1 text-[11px] font-mono-data text-[#52525B]"
                  >
                    <span class="font-sans font-medium text-[#71717A]">Tools:</span>
                    <span
                      v-for="(toolName, idx) in wf.participatingTools"
                      :key="`${wf.id}-${toolName}-${idx}`"
                      class="rounded border border-[#E4E4E7] bg-[#FAFAFA] px-1.5 py-0.5 text-[#09090B]"
                    >
                      {{ toolName }}
                    </span>
                  </div>
                </div>
              </div>
            </div>

            <div v-if="bundlePreview.experiencePreviews.length" class="space-y-3">
              <h4 class="text-[12px] font-semibold text-[#09090B]">
                MCP Apps UI Previews ({{ bundlePreview.experiencePreviews.length }})
              </h4>
              <div
                v-for="exp in bundlePreview.experiencePreviews"
                :key="exp.id"
                class="space-y-2 border-t border-[#E4E4E7] pt-3"
              >
                <div class="flex flex-wrap items-center justify-between gap-2 text-[12px]">
                  <span class="font-semibold text-[#09090B]">{{ exp.title }}</span>
                  <span class="font-mono-data text-[#0F766E]">{{ exp.tool }}</span>
                </div>
                <McpAppHostPreview
                  :title="exp.title"
                  :iframe-title="`Preview ${exp.title}`"
                  :tool="exp.tool"
                  :resource-uri="exp.resource_uri"
                  :html="exp.html"
                  :error="exp.error"
                  compact
                />
              </div>
            </div>
          </div>
        </section>
      </div>

      <div class="shrink-0 flex flex-wrap items-center justify-between gap-3 border-t border-[#E4E4E7] bg-white px-5 py-3.5 sm:px-6">
        <span class="text-[12px] text-[#52525B]">
          Validate first, then publish directly without leaving this page.
        </span>
        <div class="flex items-center gap-2">
          <Button type="button" class="min-h-9" @click="emit('update:open', false)">Cancel</Button>
          <Button
            type="button"
            data-variant="primary"
            class="min-h-9"
            :disabled="bundleApplying || !bundlePreview"
            @click="applyAndPublish"
          >
            {{ bundleApplying ? "Applying…" : "Apply & Publish" }}
          </Button>
        </div>
      </div>
    </DrawerContent>
  </Drawer>
</template>
