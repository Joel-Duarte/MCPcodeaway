<script setup lang="ts">
import { Button } from "@ui-sdk/vue/button";
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
import { Textarea } from "@ui-sdk/vue/textarea";
import { computed, ref, watch } from "vue";
import McpAppHostPreview from "~/components/McpAppHostPreview.vue";
import PromptBundleDrawer from "~/components/PromptBundleDrawer.vue";
import { extractErrorMessage } from "~/composables/useAdminApi";

const props = defineProps<{
  upstream: string;
  tools: string[];
  canEdit: boolean;
  refreshVersion: number;
}>();

const { apiFetch } = useAdminApi();

const loading = ref(true);
const loadError = ref("");
const experiences = ref<Array<Record<string, any>>>([]);
const actionError = ref("");
const statusMessage = ref("");
const promptDrawerOpen = ref(false);

const previewOpen = ref(false);
const previewExperience = ref<Record<string, any> | null>(null);
const previewHtml = ref("");
const previewBusy = ref(false);

const editOpen = ref(false);
const editingId = ref<string | null>(null);
const saving = ref(false);
const editDrawerError = ref("");
const form = ref({
  id: "",
  tool: "",
  resource_uri: "",
  heading: "",
  description: "",
  fallback: "",
  input_id: "",
  input_label: "",
  button_label: "Run Action",
});

const deleteOpen = ref(false);
const deleteTarget = ref<Record<string, any> | null>(null);
const deleteBusy = ref(false);

const toolOptions = computed(() =>
  props.tools.map((name) => ({
    value: name,
    label: name.startsWith(`${props.upstream}__`) ? name.slice(props.upstream.length + 2) : name,
  })),
);

function experienceHeading(exp: Record<string, any>): string {
  const comps = Array.isArray(exp.components) ? exp.components : [];
  const heading = comps.find((item: any) => item?.type === "heading" && item?.text);
  return String(heading?.text || exp.id || exp.tool || "MCP App UI");
}

async function load() {
  loading.value = true;
  loadError.value = "";
  try {
    const res = await apiFetch<{ current?: { definition?: Record<string, any> } | Record<string, any> | null }>(
      `/admin/integration-definitions/${encodeURIComponent(props.upstream)}`,
    );
    const current = res?.current;
    const def = current && "definition" in current ? current.definition : current;
    experiences.value = Array.isArray(def?.experiences) ? def.experiences : [];
  } catch (err) {
    loadError.value = extractErrorMessage(err);
  } finally {
    loading.value = false;
  }
}

watch(
  () => [props.upstream, props.refreshVersion] as const,
  () => {
    void load();
  },
  { immediate: true },
);

async function openPreview(exp: Record<string, any>) {
  previewExperience.value = exp;
  previewHtml.value = "";
  previewOpen.value = true;
  previewBusy.value = true;
  actionError.value = "";
  try {
    const res = await apiFetch<{ html: string }>("/admin/mcp-ui/preview", {
      method: "POST",
      body: { experience: exp },
    });
    previewHtml.value = res.html;
  } catch (err) {
    actionError.value = extractErrorMessage(err);
  } finally {
    previewBusy.value = false;
  }
}

function cleanSlug(value: string) {
  return value.toLowerCase().replace(/[^a-z0-9]+/g, "_").replace(/^_+|_+$/g, "").slice(0, 48);
}

function beginCreate() {
  editingId.value = null;
  actionError.value = "";
  editDrawerError.value = "";
  const firstTool = props.tools[0] || "";
  const slug = cleanSlug(`${firstTool.split("__").pop() || "widget"}_ui`) || "mcp_ui_widget";
  form.value = {
    id: slug,
    tool: firstTool,
    resource_uri: `ui://mcpcodeaway/${props.upstream}/${slug}/r1`,
    heading: "Interactive Tool View",
    description: "Inspect live records and submit bounded tool lookups.",
    fallback: `Invoke ${firstTool} cleanly via MCP.`,
    input_id: "",
    input_label: "",
    button_label: "Load Data",
  };
  editOpen.value = true;
}

function beginEdit(exp: Record<string, any>) {
  editingId.value = String(exp.id || "");
  actionError.value = "";
  editDrawerError.value = "";
  const comps = Array.isArray(exp.components) ? exp.components : [];
  const heading = comps.find((c: any) => c?.type === "heading");
  const text = comps.find((c: any) => c?.type === "text");
  const input = comps.find((c: any) => c?.type === "text_input");
  const btn = comps.find((c: any) => c?.type === "button");
  form.value = {
    id: String(exp.id || ""),
    tool: String(exp.tool || ""),
    resource_uri: String(exp.resource_uri || ""),
    heading: String(heading?.text || ""),
    description: String(text?.text || ""),
    fallback: String(exp.fallback || ""),
    input_id: String(input?.id || ""),
    input_label: String(input?.label || ""),
    button_label: String(btn?.label || "Load Data"),
  };
  editOpen.value = true;
}

async function saveExperience() {
  saving.value = true;
  actionError.value = "";
  editDrawerError.value = "";
  try {
    const id = editingId.value || cleanSlug(form.value.id);
    if (!id || !form.value.tool || !form.value.resource_uri.trim() || !form.value.fallback.trim()) {
      throw new Error("ID, target tool, ui:// URI, and fallback text are required.");
    }
    const components: Array<Record<string, any>> = [];
    if (form.value.heading.trim()) {
      components.push({ type: "heading", text: form.value.heading.trim() });
    }
    if (form.value.description.trim()) {
      components.push({ type: "text", text: form.value.description.trim() });
    }
    const inputMapping: Record<string, string> = {};
    if (form.value.input_id.trim()) {
      const fid = cleanSlug(form.value.input_id.trim());
      components.push({
        type: "text_input",
        id: fid,
        label: form.value.input_label.trim() || fid,
        required: true,
      });
      inputMapping[fid] = `/form/${fid}`;
    }
    if (form.value.button_label.trim()) {
      components.push({
        type: "button",
        label: form.value.button_label.trim(),
        action: "invoke_tool",
      });
    }
    components.push({ type: "record", data: "tool_result" });

    const payload = {
      id,
      tool: form.value.tool,
      resource_uri: form.value.resource_uri.trim(),
      renderer_schema: "mcpcodeaway-ui/1",
      fallback: form.value.fallback.trim(),
      input_mapping: inputMapping,
      components,
    };
    await apiFetch(
      `/admin/integration-definitions/${encodeURIComponent(props.upstream)}/experiences/${encodeURIComponent(id)}`,
      { method: "PUT", body: { experience: payload } },
    );
    editOpen.value = false;
    await load();
  } catch (err) {
    editDrawerError.value = extractErrorMessage(err);
  } finally {
    saving.value = false;
  }
}

async function handleBundleApplied(upstreamName: string) {
  statusMessage.value = `Applied and published MCP Apps UI bundle for “${upstreamName || props.upstream}”.`;
  await load();
}

function promptRemove(exp: Record<string, any>) {
  deleteTarget.value = exp;
  deleteOpen.value = true;
}

async function confirmRemove() {
  if (!deleteTarget.value) return;
  deleteBusy.value = true;
  actionError.value = "";
  try {
    const id = String(deleteTarget.value.id);
    await apiFetch(
      `/admin/integration-definitions/${encodeURIComponent(props.upstream)}/experiences/${encodeURIComponent(id)}`,
      { method: "DELETE" },
    );
    deleteOpen.value = false;
    await load();
  } catch (err) {
    actionError.value = extractErrorMessage(err);
  } finally {
    deleteBusy.value = false;
  }
}
</script>

<template>
  <section class="mb-3 max-w-[calc(100vw-4rem)] border-b border-zinc-200 pb-3 lg:max-w-none" :aria-label="`MCP Apps UI catalog for ${upstream}`">
    <div class="mb-2 flex flex-wrap items-start justify-between gap-2">
      <div>
        <h3 class="text-[0.9375rem] font-semibold text-zinc-950">MCP Apps UI</h3>
        <p class="mt-0.5 text-xs text-zinc-600">
          Interactive <code class="font-mono-data">ui://</code> widgets delivered when callers support MCP Apps UI.
        </p>
      </div>
      <div class="flex flex-wrap items-center gap-2">
        <Button v-if="canEdit && tools.length" @click="beginCreate">Create MCP UI</Button>
        <Button v-if="canEdit" class="min-h-9" @click="promptDrawerOpen = true">
          Generate UI via Prompt
        </Button>
      </div>
    </div>

    <p v-if="statusMessage" role="status" class="mb-2 text-xs leading-5 text-[#0F766E]">
      {{ statusMessage }}
    </p>
    <p v-if="actionError" role="alert" class="mb-2 rounded border border-red-200 bg-red-50 px-3 py-2 text-xs text-red-800">
      {{ actionError }}
    </p>
    <div v-if="loading" class="py-2 text-xs text-[#52525B]" role="status">Loading MCP Apps UI…</div>
    <p v-else-if="loadError" class="text-xs text-red-700" role="alert">{{ loadError }}</p>
    <p v-else-if="!experiences.length" class="py-1 text-xs text-[#52525B]">
      No MCP Apps UI views attached to tools on this upstream yet.
    </p>
    <ul v-else class="grid grid-cols-1 gap-2 xl:grid-cols-2">
      <li
        v-for="exp in experiences"
        :key="exp.id"
        class="flex flex-wrap items-center justify-between gap-2 rounded-md border border-[#E4E4E7] bg-white p-3 text-xs"
      >
        <div class="min-w-0 flex-1 space-y-0.5">
          <div class="font-semibold text-[#09090B]">{{ experienceHeading(exp) }}</div>
          <div class="font-mono-data text-[#0F766E] break-all">{{ exp.tool }}</div>
          <div class="font-mono-data text-[11px] text-[#71717A] break-all">{{ exp.resource_uri }}</div>
        </div>
        <div class="flex flex-wrap items-center gap-1.5">
          <Button @click="openPreview(exp)">Preview</Button>
          <Button v-if="canEdit" @click="beginEdit(exp)">Edit</Button>
          <Button v-if="canEdit" @click="promptRemove(exp)">Remove</Button>
        </div>
      </li>
    </ul>

    <Drawer v-model:open="previewOpen" side="right" prevent-edge-swipe>
      <DrawerBackdrop />
      <DrawerContent class="flex h-dvh w-full max-w-[95vw] flex-col gap-0 border-l border-[#E4E4E7] bg-white p-0 sm:w-[44rem]">
        <div class="shrink-0 flex items-start justify-between gap-3 border-b border-[#E4E4E7] px-5 py-4">
          <div class="min-w-0 space-y-1">
            <DrawerTitle>{{ previewExperience ? experienceHeading(previewExperience) : "MCP App UI Preview" }}</DrawerTitle>
            <DrawerDescription>
              Interactive host sandbox (<code class="font-mono-data">{{ previewExperience?.resource_uri }}</code>). Submit the form or click Simulate Sample Result to preview live structured cards and tables.
            </DrawerDescription>
          </div>
          <DrawerClose
            aria-label="Close UI preview"
            class="inline-flex min-h-9 min-w-9 shrink-0 items-center justify-center rounded-md border border-[#D4D4D8] bg-white text-[#09090B] hover:bg-[#F4F4F5] focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-[#0F766E]"
          >
            <AppIcon name="x" :size="16" />
          </DrawerClose>
        </div>
        <div class="min-h-0 flex-1 overflow-y-auto overflow-x-hidden p-5 sm:p-6 space-y-4">
          <McpAppHostPreview
            v-if="previewOpen && previewExperience"
            :title="experienceHeading(previewExperience)"
            :iframe-title="`Preview ${previewExperience.id || 'mcp-ui'}`"
            :tool="String(previewExperience.tool || '')"
            :resource-uri="String(previewExperience.resource_uri || '')"
            :html="previewHtml"
            :busy="previewBusy"
            :error="actionError"
          />
        </div>
      </DrawerContent>
    </Drawer>

    <Drawer v-model:open="editOpen" side="right" prevent-edge-swipe>
      <DrawerBackdrop />
      <DrawerContent class="flex h-dvh w-full max-w-[95vw] flex-col gap-0 border-l border-[#E4E4E7] bg-white p-0 sm:w-[34rem]">
        <div class="shrink-0 flex items-start justify-between gap-3 border-b border-[#E4E4E7] px-5 py-4">
          <div class="min-w-0 space-y-1">
            <DrawerTitle>{{ editingId ? "Edit MCP App UI" : "Create MCP App UI" }}</DrawerTitle>
            <DrawerDescription>
              Attach a constrained <code class="font-mono-data">mcpcodeaway-ui/1</code> widget directly to an upstream tool.
            </DrawerDescription>
          </div>
          <DrawerClose
            aria-label="Close MCP UI editor"
            class="inline-flex min-h-9 min-w-9 shrink-0 items-center justify-center rounded-md border border-[#D4D4D8] bg-white text-[#09090B] hover:bg-[#F4F4F5] focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-[#0F766E]"
          >
            <AppIcon name="x" :size="16" />
          </DrawerClose>
        </div>
        <div class="min-h-0 flex-1 overflow-y-auto overflow-x-hidden p-5 sm:p-6">
          <p v-if="editDrawerError" role="alert" class="mb-4 rounded border border-red-200 bg-red-50 px-3 py-2 text-xs text-red-800">
            {{ editDrawerError }}
          </p>
          <form class="min-w-0 space-y-4" @submit.prevent="saveExperience">
            <Field>
              <FieldLabel for="mcp-ui-id">Experience ID</FieldLabel>
              <Input id="mcp-ui-id" v-model="form.id" :disabled="Boolean(editingId)" class="font-mono-data text-xs" required />
            </Field>
            <Field>
              <FieldLabel>Target tool</FieldLabel>
              <Select
                :items="toolOptions"
                :value="form.tool"
                @value-change="(val) => { if (val) form.tool = String(val); }"
              >
                <SelectTrigger aria-label="MCP UI target tool" class="min-h-10 min-w-0">
                  <SelectValue />
                  <AppIcon name="chevron-down" :size="14" />
                </SelectTrigger>
                <SelectContent>
                  <SelectItem v-for="opt in toolOptions" :key="opt.value" :value="opt.value">
                    {{ opt.label }}
                    <SelectItemIndicator><AppIcon name="check" :size="14" /></SelectItemIndicator>
                  </SelectItem>
                </SelectContent>
              </Select>
            </Field>
            <Field>
              <FieldLabel for="mcp-ui-uri">Resource URI</FieldLabel>
              <Input id="mcp-ui-uri" v-model="form.resource_uri" class="font-mono-data text-xs" required />
            </Field>
            <Field>
              <FieldLabel for="mcp-ui-heading">Widget Heading</FieldLabel>
              <Input id="mcp-ui-heading" v-model="form.heading" required />
            </Field>
            <Field>
              <FieldLabel for="mcp-ui-desc">Helper Copy</FieldLabel>
              <Textarea id="mcp-ui-desc" v-model="form.description" rows="2" />
            </Field>
            <div class="grid grid-cols-1 gap-3 sm:grid-cols-2">
              <Field>
                <FieldLabel for="mcp-ui-input-id">Input field name (optional)</FieldLabel>
                <Input id="mcp-ui-input-id" v-model="form.input_id" class="font-mono-data text-xs" placeholder="e.g. ticket_id" />
              </Field>
              <Field>
                <FieldLabel for="mcp-ui-input-label">Input label</FieldLabel>
                <Input id="mcp-ui-input-label" v-model="form.input_label" placeholder="e.g. Ticket ID" />
              </Field>
            </div>
            <div class="grid grid-cols-1 gap-3 sm:grid-cols-2">
              <Field>
                <FieldLabel for="mcp-ui-button">Action button label</FieldLabel>
                <Input id="mcp-ui-button" v-model="form.button_label" />
              </Field>
              <Field>
                <FieldLabel for="mcp-ui-fallback">Text fallback</FieldLabel>
                <Input id="mcp-ui-fallback" v-model="form.fallback" required />
              </Field>
            </div>
            <FieldDescription>
              Saved experiences are compiled safely into <code class="font-mono-data">text/html;profile=mcp-app</code> without inline arbitrary JS.
            </FieldDescription>
            <div class="flex justify-end gap-2 border-t border-[#E4E4E7] pt-4">
              <Button type="button" @click="editOpen = false">Cancel</Button>
              <Button type="submit" data-variant="primary" :disabled="saving">
                {{ saving ? "Saving…" : "Save & Republish UI" }}
              </Button>
            </div>
          </form>
        </div>
      </DrawerContent>
    </Drawer>

    <Dialog v-model:open="deleteOpen">
      <DialogBackdrop />
      <DialogContent class="sm:max-w-md w-[calc(100%-2rem)]">
        <DialogTitle>Remove MCP App UI?</DialogTitle>
        <DialogDescription>
          Remove '{{ deleteTarget ? experienceHeading(deleteTarget) : "" }}' from '{{ upstream }}'?
        </DialogDescription>
        <div class="mt-4 flex justify-end gap-2">
          <DialogClose>Cancel</DialogClose>
          <Button data-variant="primary" :disabled="deleteBusy" @click="confirmRemove">
            {{ deleteBusy ? "Removing…" : "Remove UI" }}
          </Button>
        </div>
      </DialogContent>
    </Dialog>

    <PromptBundleDrawer
      v-model:open="promptDrawerOpen"
      focus="mcp_ui"
      :upstream="upstream"
      @applied="handleBundleApplied"
    />
  </section>
</template>
