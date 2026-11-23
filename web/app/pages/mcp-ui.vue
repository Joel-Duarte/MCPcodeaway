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
import { Tabs, TabsList, TabsPanel, TabsTab } from "@ui-sdk/vue/tabs";
import { Textarea } from "@ui-sdk/vue/textarea";
import { computed, onMounted, ref, watch } from "vue";
import McpAppHostPreview from "~/components/McpAppHostPreview.vue";
import { extractErrorMessage } from "~/composables/useAdminApi";

interface McpUiExperienceEntry {
  upstream_name: string;
  definition_revision: number | null;
  experience: Record<string, any>;
  tool_input_schema?: Record<string, any> | null;
  tool_description?: string | null;
}

interface CatalogToolSummary {
  qualified_name: string;
  name: string;
  description?: string;
  mutating?: boolean;
  input_schema?: Record<string, any> | null;
}

const { apiFetch } = useAdminApi();

const loading = ref(true);
const loadError = ref("");
const statusMessage = ref("");
const actionError = ref("");

const upstreams = ref<string[]>([]);
const experiences = ref<McpUiExperienceEntry[]>([]);
const toolsByUpstream = ref<Record<string, CatalogToolSummary[]>>({});
const upstreamFilter = ref("__all__");
const searchQuery = ref("");

const selectedKey = ref("");
const previewBusy = ref(false);
const previewHtml = ref("");
const previewMeta = ref<Record<string, any> | null>(null);
const previewTab = ref("rendered");

const createDrawerOpen = ref(false);
const createSaving = ref(false);
const createForm = ref({
  upstream: "",
  tool: "",
  id: "",
  resource_uri: "",
  heading: "",
  description: "",
  fallback: "",
  button_label: "Run Tool",
  input_id: "",
  input_label: "",
  input_required: false,
  select_options_text: "",
});

const editDrawerOpen = ref(false);
const editSaving = ref(false);
const editTarget = ref<McpUiExperienceEntry | null>(null);
const editForm = ref({
  fallback: "",
  componentsJson: "",
});

const deleteDialogOpen = ref(false);
const deleteBusy = ref(false);
const deleteTarget = ref<McpUiExperienceEntry | null>(null);

const upstreamSelectItems = computed(() => [
  { value: "__all__", label: "All upstreams" },
  ...upstreams.value.map((name) => ({ value: name, label: name })),
]);

function experienceHeading(exp: Record<string, any> | undefined): string {
  if (!exp) return "MCP App UI";
  if (typeof exp.title === "string" && exp.title.trim()) return exp.title;
  const comps = Array.isArray(exp.components) ? exp.components : [];
  const heading = comps.find((item: any) => item?.type === "heading" && item?.text);
  if (heading?.text) return String(heading.text);
  return String(exp.id || exp.tool || "MCP App UI");
}

const filteredExperiences = computed(() => {
  const q = searchQuery.value.trim().toLowerCase();
  return experiences.value.filter((entry) => {
    if (upstreamFilter.value !== "__all__" && entry.upstream_name !== upstreamFilter.value) {
      return false;
    }
    if (!q) return true;
    const exp = entry.experience ?? {};
    return (
      experienceHeading(exp).toLowerCase().includes(q) ||
      String(exp.tool ?? "").toLowerCase().includes(q) ||
      String(exp.resource_uri ?? "").toLowerCase().includes(q) ||
      entry.upstream_name.toLowerCase().includes(q)
    );
  });
});

function entryKey(entry: McpUiExperienceEntry): string {
  return `${entry.upstream_name}:${entry.experience?.id || ""}`;
}

const selectedEntry = computed(
  () => filteredExperiences.value.find((entry) => entryKey(entry) === selectedKey.value) ?? filteredExperiences.value[0] ?? null,
);

const boundToolsSet = computed(
  () => new Set(experiences.value.map((entry) => String(entry.experience?.tool ?? ""))),
);

const createUpstreamOptions = computed(() =>
  upstreams.value.map((name) => {
    const toolCount = (toolsByUpstream.value[name] ?? []).length;
    return {
      value: name,
      label: toolCount ? `${name} (${toolCount} tools)` : name,
    };
  }),
);

const createToolOptions = computed(() =>
  (toolsByUpstream.value[createForm.value.upstream] ?? []).map((t) => ({
    value: t.qualified_name,
    label: boundToolsSet.value.has(t.qualified_name) ? `${t.name} (has UI)` : t.name,
  })),
);

const unconfiguredToolsForView = computed(() => {
  const targetUpstreams =
    upstreamFilter.value !== "__all__"
      ? [upstreamFilter.value]
      : upstreams.value.filter(
          (name) => !experiences.value.some((exp) => exp.upstream_name === name),
        );
  const rows: Array<{ upstream: string; tool: CatalogToolSummary }> = [];
  const q = searchQuery.value.trim().toLowerCase();
  for (const upstream of targetUpstreams) {
    for (const tool of toolsByUpstream.value[upstream] ?? []) {
      if (boundToolsSet.value.has(tool.qualified_name)) continue;
      if (
        q &&
        !tool.qualified_name.toLowerCase().includes(q) &&
        !(tool.description || "").toLowerCase().includes(q)
      ) {
        continue;
      }
      rows.push({ upstream, tool });
    }
  }
  return rows.slice(0, 18);
});

function cleanSlug(value: string): string {
  return value.toLowerCase().replace(/[^a-z0-9]+/g, "_").replace(/^_+|_+$/g, "").slice(0, 48);
}

function humanizeSlug(value: string): string {
  return value
    .split(/[_-]+/)
    .filter(Boolean)
    .map((part) => part.charAt(0).toUpperCase() + part.slice(1))
    .join(" ");
}

function applyToolDefaults(upstream: string, toolQualifiedName: string) {
  const toolList = toolsByUpstream.value[upstream] ?? [];
  const toolObj = toolList.find((t) => t.qualified_name === toolQualifiedName) ?? null;
  const shortName = toolObj?.name || toolQualifiedName.split("__").pop() || "tool";
  const slug = cleanSlug(`${shortName}_ui`) || "mcp_ui_view";
  const schema = toolObj?.input_schema ?? {};
  const props =
    schema && typeof schema.properties === "object" && schema.properties
      ? (schema.properties as Record<string, any>)
      : {};
  const requiredList: string[] = Array.isArray(schema?.required) ? schema.required : [];
  const stringFields = Object.entries(props).filter(
    ([, propSchema]) => !propSchema || propSchema.type === "string" || !propSchema.type,
  );
  const preferredField =
    stringFields.find(([fname]) => requiredList.includes(fname)) ?? stringFields[0] ?? null;
  const fieldName = preferredField ? preferredField[0] : "";
  const fieldSchema = preferredField ? preferredField[1] : null;
  const enumVals = Array.isArray(fieldSchema?.enum) ? fieldSchema.enum.map(String) : [];

  createForm.value = {
    upstream,
    tool: toolQualifiedName,
    id: slug,
    resource_uri: `ui://mcpcodeaway/${upstream}/${slug}/r1`,
    heading: humanizeSlug(shortName),
    description:
      (toolObj?.description || "").trim() ||
      `Interactive MCP App card for ${toolQualifiedName}.`,
    fallback: `Invoke ${toolQualifiedName} cleanly via MCP.`,
    button_label: toolObj?.mutating ? `Submit ${humanizeSlug(shortName)}` : "Run Lookup",
    input_id: fieldName ? cleanSlug(fieldName) : "",
    input_label: fieldName ? humanizeSlug(fieldName) : "",
    input_required: Boolean(fieldName && requiredList.includes(fieldName)),
    select_options_text: enumVals.join(", "),
  };
}

function openCreateDrawer(prefillUpstream?: string, prefillTool?: string) {
  actionError.value = "";
  const chosenUpstream =
    prefillUpstream ||
    (upstreamFilter.value !== "__all__" ? upstreamFilter.value : "") ||
    upstreams.value.find((u) => (toolsByUpstream.value[u] ?? []).length > 0) ||
    upstreams.value[0] ||
    "";
  const candidateTools = toolsByUpstream.value[chosenUpstream] ?? [];
  const unboundTool =
    candidateTools.find((t) => !boundToolsSet.value.has(t.qualified_name)) ?? candidateTools[0];
  const chosenTool = prefillTool || unboundTool?.qualified_name || "";
  if (chosenUpstream && chosenTool) {
    applyToolDefaults(chosenUpstream, chosenTool);
  } else {
    createForm.value = {
      upstream: chosenUpstream,
      tool: "",
      id: "mcp_ui_widget",
      resource_uri: `ui://mcpcodeaway/${chosenUpstream || "upstream"}/mcp_ui_widget/r1`,
      heading: "Interactive Tool View",
      description: "Inspect live records cleanly inside compatible MCP Apps hosts.",
      fallback: "Run this upstream tool cleanly via MCP.",
      button_label: "Run Tool",
      input_id: "",
      input_label: "",
      input_required: false,
      select_options_text: "",
    };
  }
  createDrawerOpen.value = true;
}

function handleCreateUpstreamChange(nextUpstream: string) {
  const candidateTools = toolsByUpstream.value[nextUpstream] ?? [];
  const unbound =
    candidateTools.find((t) => !boundToolsSet.value.has(t.qualified_name)) ?? candidateTools[0];
  if (unbound) {
    applyToolDefaults(nextUpstream, unbound.qualified_name);
  } else {
    createForm.value.upstream = nextUpstream;
    createForm.value.tool = "";
  }
}

async function saveCreatedExperience() {
  createSaving.value = true;
  actionError.value = "";
  try {
    const upstream = createForm.value.upstream.trim();
    const id = cleanSlug(createForm.value.id);
    if (!upstream || !createForm.value.tool.trim() || !id) {
      throw new Error("Choose an upstream, target tool, and valid experience ID.");
    }
    const components: Array<Record<string, any>> = [];
    if (createForm.value.heading.trim()) {
      components.push({ type: "heading", text: createForm.value.heading.trim() });
    }
    if (createForm.value.description.trim()) {
      components.push({ type: "text", text: createForm.value.description.trim() });
    }
    const inputMapping: Record<string, string> = {};
    const toolObj = (toolsByUpstream.value[upstream] ?? []).find(
      (t) => t.qualified_name === createForm.value.tool,
    );
    const schemaProps =
      toolObj?.input_schema && typeof toolObj.input_schema.properties === "object"
        ? toolObj.input_schema.properties
        : {};
    const requiredProps: string[] = Array.isArray(toolObj?.input_schema?.required)
      ? toolObj.input_schema.required
      : [];

    const mappedFields = new Set<string>();
    const addFieldNode = (fieldId: string, label: string, required: boolean, enumChoices?: string[]) => {
      const cleanId = cleanSlug(fieldId);
      if (!cleanId || mappedFields.has(cleanId)) return;
      mappedFields.add(cleanId);
      if (enumChoices && enumChoices.length > 0) {
        components.push({
          type: "select",
          id: cleanId,
          label: label || humanizeSlug(cleanId),
          required,
          options: enumChoices.map((v) => ({ value: v, label: v })),
        });
      } else {
        components.push({
          type: "text_input",
          id: cleanId,
          label: label || humanizeSlug(cleanId),
          required,
          max_length: 256,
        });
      }
      inputMapping[fieldId] = `/form/${cleanId}`;
    };

    if (createForm.value.input_id.trim()) {
      const enumChoices = createForm.value.select_options_text
        .split(",")
        .map((s) => s.trim())
        .filter(Boolean);
      addFieldNode(
        createForm.value.input_id.trim(),
        createForm.value.input_label.trim(),
        createForm.value.input_required,
        enumChoices.length ? enumChoices : undefined,
      );
    }
    for (const reqField of requiredProps) {
      const propSchema = schemaProps[reqField] as Record<string, any> | undefined;
      if (propSchema?.type === "string" || !propSchema?.type) {
        const enumVals = Array.isArray(propSchema?.enum) ? propSchema.enum.map(String) : undefined;
        addFieldNode(reqField, humanizeSlug(reqField), true, enumVals);
      }
    }

    components.push({
      type: "button",
      label: createForm.value.button_label.trim() || "Run Tool",
      action: "invoke_tool",
    });
    components.push({ type: "record", data: "tool_result" });

    const expPayload = {
      id,
      tool: createForm.value.tool.trim(),
      resource_uri:
        createForm.value.resource_uri.trim() || `ui://mcpcodeaway/${upstream}/${id}/r1`,
      renderer_schema: "mcpcodeaway-ui/1",
      fallback: createForm.value.fallback.trim() || `Invoke ${createForm.value.tool} cleanly via MCP.`,
      input_mapping: inputMapping,
      components,
      csp: { connect_domains: [], resource_domains: [], frame_domains: [] },
    };

    await apiFetch(
      `/admin/integration-definitions/${encodeURIComponent(upstream)}/experiences/${encodeURIComponent(id)}`,
      { method: "PUT", body: { experience: expPayload } },
    );
    createDrawerOpen.value = false;
    if (upstreamFilter.value !== "__all__" && upstreamFilter.value !== upstream) {
      upstreamFilter.value = upstream;
    }
    selectedKey.value = `${upstream}:${id}`;
    statusMessage.value = `Created and published MCP App UI '${expPayload.components[0]?.text || id}' for '${expPayload.tool}'.`;
    await loadExperiences();
  } catch (err) {
    actionError.value = extractErrorMessage(err);
  } finally {
    createSaving.value = false;
  }
}

async function renderPreview(entry: McpUiExperienceEntry | null) {
  if (!entry) {
    previewHtml.value = "";
    previewMeta.value = null;
    return;
  }
  previewBusy.value = true;
  actionError.value = "";
  try {
    const res = await apiFetch<{ html: string; meta?: Record<string, any> }>("/admin/mcp-ui/preview", {
      method: "POST",
      body: {
        experience: entry.experience,
        tool_input_schema: entry.tool_input_schema ?? undefined,
      },
    });
    previewHtml.value = res.html;
    previewMeta.value = res.meta ?? null;
  } catch (err) {
    previewHtml.value = "";
    actionError.value = extractErrorMessage(err);
  } finally {
    previewBusy.value = false;
  }
}

async function selectEntry(entry: McpUiExperienceEntry) {
  selectedKey.value = entryKey(entry);
  await renderPreview(entry);
}

async function loadExperiences() {
  loading.value = true;
  loadError.value = "";
  try {
    const res = await apiFetch<{
      upstreams?: string[];
      experiences?: McpUiExperienceEntry[];
      tools_by_upstream?: Record<string, CatalogToolSummary[]>;
    }>("/admin/mcp-ui/experiences");
    upstreams.value = res.upstreams ?? [];
    experiences.value = res.experiences ?? [];
    toolsByUpstream.value = res.tools_by_upstream ?? {};
    if (filteredExperiences.value.length) {
      const target = selectedEntry.value ?? filteredExperiences.value[0] ?? null;
      if (target) {
        selectedKey.value = entryKey(target);
        await renderPreview(target);
      }
    } else {
      previewHtml.value = "";
      previewMeta.value = null;
    }
  } catch (err) {
    loadError.value = extractErrorMessage(err);
  } finally {
    loading.value = false;
  }
}

watch(upstreamFilter, () => {
  if (filteredExperiences.value.length) {
    const nextTarget = selectedEntry.value ?? filteredExperiences.value[0] ?? null;
    if (nextTarget) {
      selectedKey.value = entryKey(nextTarget);
      void renderPreview(nextTarget);
    }
  } else {
    previewHtml.value = "";
    previewMeta.value = null;
  }
});

onMounted(() => {
  void loadExperiences();
});

function openEditDrawer(entry: McpUiExperienceEntry) {
  editTarget.value = entry;
  editForm.value = {
    fallback: String(entry.experience.fallback ?? ""),
    componentsJson: JSON.stringify(entry.experience.components ?? [], null, 2),
  };
  editDrawerOpen.value = true;
}

async function saveExperienceEdit() {
  if (!editTarget.value) return;
  editSaving.value = true;
  actionError.value = "";
  try {
    const parsedComponents = JSON.parse(editForm.value.componentsJson || "[]") as Array<Record<string, any>>;
    if (!Array.isArray(parsedComponents)) {
      throw new Error("Components JSON must be an array of component nodes.");
    }
    const updatedExperience = {
      ...editTarget.value.experience,
      fallback: editForm.value.fallback.trim() || "Interactive MCP App UI",
      components: parsedComponents,
    };
    const expId = String(editTarget.value.experience.id);
    await apiFetch(
      `/admin/integration-definitions/${encodeURIComponent(editTarget.value.upstream_name)}/experiences/${encodeURIComponent(expId)}`,
      {
        method: "PUT",
        body: { experience: updatedExperience },
      },
    );
    editDrawerOpen.value = false;
    statusMessage.value = `Saved MCP App UI '${experienceHeading(updatedExperience)}' for '${updatedExperience.tool}'.`;
    await loadExperiences();
  } catch (err) {
    actionError.value = extractErrorMessage(err);
  } finally {
    editSaving.value = false;
  }
}

function confirmDelete(entry: McpUiExperienceEntry) {
  deleteTarget.value = entry;
  deleteDialogOpen.value = true;
}

async function performDelete() {
  if (!deleteTarget.value) return;
  const target = deleteTarget.value;
  deleteBusy.value = true;
  actionError.value = "";
  try {
    const expId = String(target.experience.id);
    await apiFetch(
      `/admin/integration-definitions/${encodeURIComponent(target.upstream_name)}/experiences/${encodeURIComponent(expId)}`,
      { method: "DELETE" },
    );
    deleteDialogOpen.value = false;
    statusMessage.value = `Removed MCP App UI '${target.experience.title}' from '${target.upstream_name}'.`;
    await loadExperiences();
  } catch (err) {
    actionError.value = extractErrorMessage(err);
  } finally {
    deleteBusy.value = false;
  }
}
</script>

<template>
  <div class="space-y-6">
    <div class="flex flex-col gap-3 sm:flex-row sm:items-center sm:justify-between">
      <div>
        <h1 class="text-[20px] font-semibold tracking-[-0.015em] text-[#09090B]">MCP Apps UI</h1>
        <p class="mt-0.5 text-[13px] text-[#52525B]">
          Preview interactive <code class="font-mono-data">ui://</code> views attached to upstream endpoints and edit their layout before clients render them.
        </p>
      </div>
      <div class="flex flex-wrap items-center gap-2">
        <Button class="min-h-11 sm:min-h-9" @click="loadExperiences">
          <AppIcon name="refresh" :size="14" />
          <span>Refresh</span>
        </Button>
        <NuxtLink
          to="/onboarding"
          class="inline-flex min-h-11 items-center rounded-md border border-[#E4E4E7] bg-white px-3 text-[13px] font-medium text-[#09090B] hover:bg-[#F4F4F5] sm:min-h-9"
        >
          Generate via Agent Prompt
        </NuxtLink>
        <Button
          v-if="upstreams.length"
          data-variant="primary"
          class="min-h-11 sm:min-h-9"
          @click="openCreateDrawer()"
        >
          Create MCP UI
        </Button>
      </div>
    </div>

    <div
      v-if="statusMessage"
      role="status"
      class="rounded-lg border border-[#99F6E4] bg-[#F0FDFA] px-4 py-2.5 text-[13px] text-[#0F766E]"
    >
      {{ statusMessage }}
    </div>
    <div
      v-if="actionError"
      role="alert"
      class="flex items-start gap-2 rounded-lg border border-[#FECACA] bg-[#FEF2F2] px-4 py-3 text-[13px] text-[#DC2626]"
    >
      <AppIcon name="alert" :size="16" class="mt-0.5" />
      <span>{{ actionError }}</span>
    </div>
    <div
      v-if="loadError"
      role="alert"
      class="flex flex-wrap items-center justify-between gap-2 rounded-lg border border-[#FECACA] bg-[#FEF2F2] px-4 py-3 text-[13px] text-[#DC2626]"
    >
      <span>Failed to load MCP Apps UI registry: {{ loadError }}</span>
      <Button class="min-h-11" @click="loadExperiences">Retry</Button>
    </div>

    <section class="flex flex-col gap-3 rounded-lg border border-[#E4E4E7] bg-white p-4 sm:flex-row sm:items-end">
      <Field class="sm:w-64">
        <FieldLabel>Filter by upstream</FieldLabel>
        <Select
          :items="upstreamSelectItems"
          :value="upstreamFilter"
          @value-change="(value) => { if (value) upstreamFilter = String(value); }"
        >
          <SelectTrigger aria-label="Filter UI experiences by upstream" class="min-h-11 sm:min-h-9">
            <SelectValue />
            <AppIcon name="chevron-down" :size="14" />
          </SelectTrigger>
          <SelectContent>
            <SelectItem v-for="item in upstreamSelectItems" :key="item.value" :value="item.value">
              {{ item.label }}
              <SelectItemIndicator><AppIcon name="check" :size="14" /></SelectItemIndicator>
            </SelectItem>
          </SelectContent>
        </Select>
      </Field>
      <Field class="flex-1">
        <FieldLabel for="mcp-ui-search">Search tools or UI resources</FieldLabel>
        <Input
          id="mcp-ui-search"
          v-model="searchQuery"
          placeholder="Search by endpoint/tool name, ui:// URI, or title..."
        />
      </Field>
    </section>

    <div v-if="loading" class="rounded-lg border border-[#E4E4E7] bg-white p-6 text-[13px] text-[#52525B]" role="status">
      Loading bound MCP Apps UI experiences…
    </div>
    <div v-else class="space-y-6">
      <div
        v-if="!filteredExperiences.length"
        class="rounded-lg border border-[#E4E4E7] bg-white p-6 text-[13px] text-[#52525B] space-y-3"
      >
        <p>
          No endpoints currently have MCP Apps UI views attached{{ upstreamFilter !== "__all__" ? ` for '${upstreamFilter}'` : "" }}.
          Pick a tool below to create a visual card right here, or generate an OnboardingBundle JSON with
          <code class="font-mono-data">experiences</code> on
          <NuxtLink to="/onboarding" class="font-medium text-[#0F766E] underline">Onboarding</NuxtLink>.
        </p>
        <Button
          v-if="upstreams.length"
          data-variant="primary"
          class="min-h-9 text-xs"
          @click="openCreateDrawer(upstreamFilter !== '__all__' ? upstreamFilter : undefined)"
        >
          Create UI for {{ upstreamFilter !== "__all__" ? upstreamFilter : "an Upstream Tool" }}
        </Button>
      </div>

      <div v-if="filteredExperiences.length" class="grid grid-cols-1 gap-6 lg:grid-cols-12">
        <section class="rounded-lg border border-[#E4E4E7] bg-white p-4 lg:col-span-5">
          <div class="flex items-center justify-between border-b border-[#E4E4E7] pb-2.5">
            <h2 class="text-[15px] font-semibold text-[#09090B]">Endpoints with UI Triggers</h2>
            <span class="text-[12px] tabular-nums text-[#71717A]">
              {{ filteredExperiences.length }} bound
            </span>
          </div>
          <div class="divide-y divide-[#E4E4E7]">
            <div
              v-for="entry in filteredExperiences"
              :key="entryKey(entry)"
              class="py-3 transition-colors"
              :class="selectedEntry && entryKey(selectedEntry) === entryKey(entry) ? 'bg-[#F0FDFA] -mx-2 px-2 rounded' : ''"
            >
              <button
                type="button"
                class="w-full text-left space-y-1"
                @click="selectEntry(entry)"
              >
                <div class="flex flex-wrap items-center justify-between gap-2">
                  <span class="text-[14px] font-semibold text-[#09090B]">
                    {{ experienceHeading(entry.experience) }}
                  </span>
                  <span class="rounded border border-[#E4E4E7] bg-[#F4F4F5] px-1.5 py-0.5 font-mono-data text-[11px] text-[#52525B]">
                    {{ entry.upstream_name }}
                  </span>
                </div>
                <div class="font-mono-data text-[12px] text-[#0F766E] break-all">
                  {{ entry.experience.tool }}
                </div>
                <div class="font-mono-data text-[11px] text-[#71717A] break-all">
                  {{ entry.experience.resource_uri }}
                </div>
              </button>
              <div class="mt-2 flex flex-wrap items-center gap-2">
                <Button class="text-xs min-h-9" @click="openEditDrawer(entry)">Edit UI Spec</Button>
                <Button class="text-xs min-h-9" @click="confirmDelete(entry)">Remove</Button>
              </div>
            </div>
          </div>
        </section>

        <section v-if="selectedEntry" class="rounded-lg border border-[#E4E4E7] bg-white p-5 lg:col-span-7 space-y-4">
          <div class="flex flex-wrap items-start justify-between gap-2 border-b border-[#E4E4E7] pb-3">
            <div class="space-y-1">
              <h2 class="text-[16px] font-semibold text-[#09090B]">
                {{ experienceHeading(selectedEntry.experience) }}
              </h2>
              <p class="font-mono-data text-[12px] text-[#52525B] break-all">
                Tool: {{ selectedEntry.experience.tool }} · Resource: {{ selectedEntry.experience.resource_uri }}
              </p>
              <p v-if="selectedEntry.tool_description" class="text-[12px] text-[#52525B]">
                {{ selectedEntry.tool_description }}
              </p>
            </div>
            <span class="rounded border border-[#99F6E4] bg-[#F0FDFA] px-2 py-0.5 text-[11px] font-medium text-[#0F766E]">
              {{ selectedEntry.experience.renderer_schema || "mcpcodeaway-ui/1" }}
            </span>
          </div>

          <Tabs v-model:value="previewTab">
            <TabsList aria-label="MCP App UI views">
              <TabsTab value="rendered">Live Presentation Preview</TabsTab>
              <TabsTab value="spec">Declarative UI Spec &amp; Mapping</TabsTab>
            </TabsList>
            <TabsPanel value="rendered" class="pt-3">
              <McpAppHostPreview
                :title="experienceHeading(selectedEntry.experience)"
                iframe-title="MCP App UI Preview"
                :tool="String(selectedEntry.experience.tool || '')"
                :resource-uri="String(selectedEntry.experience.resource_uri || '')"
                :html="previewHtml"
                :busy="previewBusy"
                :error="actionError"
              />
            </TabsPanel>
            <TabsPanel value="spec" class="pt-3 space-y-3">
              <div v-if="Object.keys(selectedEntry.experience.input_mapping || {}).length" class="space-y-1">
                <div class="text-[12px] font-semibold text-[#09090B]">Form Input Mapping</div>
                <div class="divide-y divide-[#E4E4E7] border-y border-[#E4E4E7] text-[12px]">
                  <div
                    v-for="(pointer, field) in selectedEntry.experience.input_mapping"
                    :key="String(field)"
                    class="flex justify-between py-1.5 font-mono-data"
                  >
                    <span class="text-[#09090B]">{{ field }}</span>
                    <span class="text-[#0F766E]">{{ pointer }}</span>
                  </div>
                </div>
              </div>
              <pre class="max-h-80 overflow-auto rounded border border-[#E4E4E7] bg-[#F4F4F5] p-3 font-mono-data text-[11px] text-[#09090B]">{{ JSON.stringify(selectedEntry.experience, null, 2) }}</pre>
            </TabsPanel>
          </Tabs>
        </section>
      </div>

      <section
        v-if="unconfiguredToolsForView.length"
        class="rounded-lg border border-[#E4E4E7] bg-white p-5 space-y-3"
        aria-label="Tools without MCP Apps UI"
      >
        <div class="flex flex-wrap items-center justify-between gap-2 border-b border-[#E4E4E7] pb-2.5">
          <div>
            <h2 class="text-[15px] font-semibold text-[#09090B]">
              Attach UI to Unconfigured Tools
            </h2>
            <p class="text-[12px] text-[#52525B]">
              Choose any tool from an upstream without an existing widget to build a bespoke MCP App view.
            </p>
          </div>
          <span class="text-[12px] tabular-nums text-[#71717A]">
            {{ unconfiguredToolsForView.length }} shown
          </span>
        </div>
        <div class="grid grid-cols-1 gap-2 md:grid-cols-2 xl:grid-cols-3">
          <div
            v-for="item in unconfiguredToolsForView"
            :key="item.tool.qualified_name"
            class="flex items-start justify-between gap-2 rounded-md border border-[#E4E4E7] bg-[#FAFAFA] p-3 text-xs"
          >
            <div class="min-w-0 space-y-0.5">
              <div class="font-semibold text-[#09090B] truncate">{{ item.tool.name }}</div>
              <div class="font-mono-data text-[11px] text-[#71717A] truncate">{{ item.upstream }}</div>
              <p v-if="item.tool.description" class="line-clamp-2 text-[11px] text-[#52525B]">
                {{ item.tool.description }}
              </p>
            </div>
            <Button
              class="min-h-8 shrink-0 text-xs"
              @click="openCreateDrawer(item.upstream, item.tool.qualified_name)"
            >
              Create UI
            </Button>
          </div>
        </div>
      </section>
    </div>

    <Drawer v-model:open="createDrawerOpen" side="right" prevent-edge-swipe>
      <DrawerBackdrop />
      <DrawerContent class="flex h-dvh w-full max-w-[95vw] flex-col gap-0 border-l border-[#E4E4E7] bg-white p-0 sm:w-[38rem]">
        <div class="min-h-0 flex-1 overflow-y-auto overflow-x-hidden p-5 sm:p-7">
          <DrawerClose aria-label="Close MCP UI creator" class="absolute right-4 top-4">
            <AppIcon name="x" :size="16" />
          </DrawerClose>
          <DrawerTitle>Create MCP App UI</DrawerTitle>
          <DrawerDescription>
            Select any registered upstream and tool to generate a safe declarative <code class="font-mono-data">mcpcodeaway-ui/1</code> widget.
          </DrawerDescription>
          <form class="mt-5 min-w-0 space-y-4" @submit.prevent="saveCreatedExperience">
            <div class="grid grid-cols-1 gap-3 sm:grid-cols-2">
              <Field class="min-w-0">
                <FieldLabel>Upstream</FieldLabel>
                <Select
                  :items="createUpstreamOptions"
                  :value="createForm.upstream"
                  @value-change="(val) => { if (val) handleCreateUpstreamChange(String(val)); }"
                >
                  <SelectTrigger aria-label="Create MCP UI upstream" class="min-h-10 min-w-0">
                    <SelectValue placeholder="Choose upstream" />
                    <AppIcon name="chevron-down" :size="14" />
                  </SelectTrigger>
                  <SelectContent>
                    <SelectItem v-for="opt in createUpstreamOptions" :key="opt.value" :value="opt.value">
                      {{ opt.label }}
                      <SelectItemIndicator><AppIcon name="check" :size="14" /></SelectItemIndicator>
                    </SelectItem>
                  </SelectContent>
                </Select>
              </Field>
              <Field class="min-w-0">
                <FieldLabel>Target tool</FieldLabel>
                <Select
                  :items="createToolOptions"
                  :value="createForm.tool"
                  @value-change="(val) => { if (val) applyToolDefaults(createForm.upstream, String(val)); }"
                >
                  <SelectTrigger aria-label="Create MCP UI target tool" class="min-h-10 min-w-0">
                    <SelectValue placeholder="Choose tool" />
                    <AppIcon name="chevron-down" :size="14" />
                  </SelectTrigger>
                  <SelectContent>
                    <SelectItem v-for="opt in createToolOptions" :key="opt.value" :value="opt.value">
                      {{ opt.label }}
                      <SelectItemIndicator><AppIcon name="check" :size="14" /></SelectItemIndicator>
                    </SelectItem>
                  </SelectContent>
                </Select>
              </Field>
            </div>
            <div class="grid grid-cols-1 gap-3 sm:grid-cols-2">
              <Field>
                <FieldLabel for="create-mcp-ui-id">Experience ID</FieldLabel>
                <Input id="create-mcp-ui-id" v-model="createForm.id" class="font-mono-data text-xs" required />
              </Field>
              <Field>
                <FieldLabel for="create-mcp-ui-uri">Resource URI</FieldLabel>
                <Input id="create-mcp-ui-uri" v-model="createForm.resource_uri" class="font-mono-data text-xs" required />
              </Field>
            </div>
            <Field>
              <FieldLabel for="create-mcp-ui-heading">Widget Heading</FieldLabel>
              <Input id="create-mcp-ui-heading" v-model="createForm.heading" required />
            </Field>
            <Field>
              <FieldLabel for="create-mcp-ui-desc">Helper Copy</FieldLabel>
              <Textarea id="create-mcp-ui-desc" v-model="createForm.description" rows="2" />
            </Field>
            <div class="grid grid-cols-1 gap-3 sm:grid-cols-2">
              <Field>
                <FieldLabel for="create-mcp-ui-input-id">Primary input field (optional)</FieldLabel>
                <Input id="create-mcp-ui-input-id" v-model="createForm.input_id" class="font-mono-data text-xs" placeholder="e.g. ticket_id" />
              </Field>
              <Field>
                <FieldLabel for="create-mcp-ui-input-label">Primary input label</FieldLabel>
                <Input id="create-mcp-ui-input-label" v-model="createForm.input_label" placeholder="e.g. Ticket ID" />
              </Field>
            </div>
            <Field v-if="createForm.input_id">
              <FieldLabel for="create-mcp-ui-select-opts">Dropdown options (comma-separated, optional)</FieldLabel>
              <Input id="create-mcp-ui-select-opts" v-model="createForm.select_options_text" placeholder="Leave blank for free text input, or e.g. EU, US, APAC" />
            </Field>
            <div class="grid grid-cols-1 gap-3 sm:grid-cols-2">
              <Field>
                <FieldLabel for="create-mcp-ui-btn">Action button label</FieldLabel>
                <Input id="create-mcp-ui-btn" v-model="createForm.button_label" required />
              </Field>
              <Field>
                <FieldLabel for="create-mcp-ui-fallback">Text fallback</FieldLabel>
                <Input id="create-mcp-ui-fallback" v-model="createForm.fallback" required />
              </Field>
            </div>
            <div class="flex justify-end gap-2 border-t border-[#E4E4E7] pt-4">
              <Button type="button" @click="createDrawerOpen = false">Cancel</Button>
              <Button type="submit" data-variant="primary" :disabled="createSaving || !createForm.tool">
                {{ createSaving ? "Saving…" : "Create & Preview UI" }}
              </Button>
            </div>
          </form>
        </div>
      </DrawerContent>
    </Drawer>

    <Drawer v-model:open="editDrawerOpen" side="right" prevent-edge-swipe>
      <DrawerBackdrop />
      <DrawerContent class="flex h-dvh w-full max-w-[95vw] flex-col gap-0 border-l border-[#E4E4E7] bg-white p-0 sm:w-[36rem]">
        <div class="min-h-0 flex-1 overflow-y-auto overflow-x-hidden p-5 sm:p-7">
          <DrawerClose aria-label="Close UI editor" class="absolute right-4 top-4">
            <AppIcon name="x" :size="16" />
          </DrawerClose>
          <DrawerTitle>Edit MCP App UI Experience</DrawerTitle>
          <DrawerDescription>
            Update the declarative layout (<code class="font-mono-data">mcpcodeaway-ui/1</code>) rendered for '{{ editTarget?.experience?.tool }}'.
          </DrawerDescription>
          <form class="mt-5 min-w-0 space-y-4" @submit.prevent="saveExperienceEdit">
            <Field>
              <FieldLabel for="edit-ui-fallback">Text fallback summary</FieldLabel>
              <Input id="edit-ui-fallback" v-model="editForm.fallback" required />
              <FieldDescription>
                Shown when a connected MCP client does not support interactive UI views.
              </FieldDescription>
            </Field>
            <Field>
              <FieldLabel for="edit-ui-spec">Declarative Components JSON</FieldLabel>
              <Textarea
                id="edit-ui-spec"
                v-model="editForm.componentsJson"
                rows="12"
                class="font-mono-data text-xs"
              />
              <FieldDescription>
                JSON array of nodes (<code class="font-mono-data">heading</code>, <code class="font-mono-data">text</code>, <code class="font-mono-data">text_input</code>, <code class="font-mono-data">select</code>, <code class="font-mono-data">button</code>, <code class="font-mono-data">record</code>).
              </FieldDescription>
            </Field>
            <div class="flex justify-end gap-2 border-t border-[#E4E4E7] pt-4">
              <Button type="button" @click="editDrawerOpen = false">Cancel</Button>
              <Button type="submit" data-variant="primary" :disabled="editSaving">
                {{ editSaving ? "Saving…" : "Save & Republish UI" }}
              </Button>
            </div>
          </form>
        </div>
      </DrawerContent>
    </Drawer>

    <Dialog v-model:open="deleteDialogOpen">
      <DialogBackdrop />
      <DialogContent class="sm:max-w-md w-[calc(100%-2rem)]">
        <DialogTitle>Remove MCP App UI Experience?</DialogTitle>
        <DialogDescription>
          Detach '{{ experienceHeading(deleteTarget?.experience) }}' from '{{ deleteTarget?.experience?.tool }}' and publish a new definition revision.
        </DialogDescription>
        <div class="mt-4 flex justify-end gap-2">
          <DialogClose>Cancel</DialogClose>
          <Button data-variant="primary" :disabled="deleteBusy" @click="performDelete">
            {{ deleteBusy ? "Removing…" : "Remove UI" }}
          </Button>
        </div>
      </DialogContent>
    </Dialog>
  </div>
</template>
