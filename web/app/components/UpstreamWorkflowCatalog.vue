<script setup lang="ts">
import { Button } from "@ui-sdk/vue/button";
import { Checkbox, CheckboxIndicator } from "@ui-sdk/vue/checkbox";
import { Drawer, DrawerBackdrop, DrawerClose, DrawerContent, DrawerDescription, DrawerTitle } from "@ui-sdk/vue/drawer";
import { Field, FieldDescription, FieldLabel } from "@ui-sdk/vue/field";
import { Input } from "@ui-sdk/vue/input";
import { Select, SelectContent, SelectItem, SelectItemIndicator, SelectTrigger, SelectValue } from "@ui-sdk/vue/select";
import { Textarea } from "@ui-sdk/vue/textarea";
import { computed, ref, watch } from "vue";
import { extractErrorMessage } from "~/composables/useAdminApi";

type Workflow = Record<string, any>;
type Definition = Record<string, any>;
const props = defineProps<{ upstream: string; tools: string[]; canEdit: boolean; refreshVersion: number }>();
const emit = defineEmits<{ (event: "refresh-catalog"): void }>();
const { apiFetch } = useAdminApi();
const definition = ref<Definition | null>(null);
const loading = ref(true);
const loadError = ref("");
const open = ref(false);
const editingId = ref<string | null>(null);
const saving = ref(false);
const error = ref("");
const opener = ref<HTMLElement | null>(null);
const details = ref("");
const form = ref({ id: "", name: "", description: "", tools: [] as string[], requiredTools: [] as string[], evidenceMinCalls: {} as Record<string, number>, evidencePredicates: {} as Record<string, { path: string; type: string }>, completionTool: "", completionPath: "/status", completionType: "non_empty_string", minutes: 5, source: "administrator_estimate", effectiveFrom: new Date().toISOString().slice(0, 10), evidenceNote: "" });
const workflows = computed<Workflow[]>(() => definition.value?.workflows ?? []);

async function load() {
  loading.value = true; loadError.value = "";
  try {
    const response = await apiFetch<{ current?: Definition | { definition?: Definition } | null }>(`/admin/integration-definitions/${encodeURIComponent(props.upstream)}`);
    const current = response.current;
    definition.value = current && "definition" in current ? current.definition ?? null : current ?? null;
  } catch (err) { loadError.value = extractErrorMessage(err); }
  finally { loading.value = false; }
}
watch(() => [props.upstream, props.refreshVersion] as const, () => { void load(); }, { immediate: true });
watch(open, (value, previous) => { if (previous && !value) requestAnimationFrame(() => opener.value?.focus()); });

function cleanId(value: string) { return value.toLowerCase().replace(/[^a-z0-9]+/g, "_").replace(/^_+|_+$/g, "").slice(0, 48); }
function beginCreate(event: MouseEvent) {
  opener.value = event.currentTarget instanceof HTMLElement ? event.currentTarget : null;
  editingId.value = null; error.value = "";
  form.value = { id: "", name: "", description: "", tools: [], requiredTools: [], evidenceMinCalls: {}, evidencePredicates: {}, completionTool: "", completionPath: "/status", completionType: "non_empty_string", minutes: 5, source: "administrator_estimate", effectiveFrom: new Date().toISOString().slice(0, 10), evidenceNote: "" };
  open.value = true;
}
function beginEdit(workflow: Workflow, event: MouseEvent) {
  opener.value = event.currentTarget instanceof HTMLElement ? event.currentTarget : null;
  editingId.value = workflow.id; error.value = "";
  form.value = { id: workflow.id, name: workflow.name, description: workflow.description, tools: [...workflow.participating_tools], requiredTools: workflow.required_evidence.map((item: any) => item.tool), evidenceMinCalls: Object.fromEntries(workflow.required_evidence.map((item: any) => [item.tool, item.min_successful_calls])), evidencePredicates: Object.fromEntries(workflow.required_evidence.filter((item: any) => item.result_when).map((item: any) => [item.tool, { path: item.result_when.path, type: item.result_when.type }])), completionTool: workflow.completion.tool, completionPath: workflow.completion.success_when.path, completionType: workflow.completion.success_when.type, minutes: workflow.business_value.manual_baseline_minutes, source: workflow.business_value.baseline_source, effectiveFrom: workflow.business_value.effective_from, evidenceNote: workflow.business_value.evidence_note ?? "" };
  open.value = true;
}
function close() { open.value = false; }
function toggleTool(tool: string) {
  form.value.tools = form.value.tools.includes(tool) ? form.value.tools.filter((item) => item !== tool) : [...form.value.tools, tool];
  form.value.requiredTools = form.value.requiredTools.filter((item) => form.value.tools.includes(item));
  if (!form.value.tools.includes(tool)) { delete form.value.evidencePredicates[tool]; delete form.value.evidenceMinCalls[tool]; }
  if (!form.value.tools.includes(tool)) delete form.value.evidencePredicates[tool];
  if (!form.value.tools.includes(form.value.completionTool)) form.value.completionTool = "";
}
function toggleRequiredEvidence(tool: string, checked: boolean) {
  form.value.requiredTools = checked ? [...form.value.requiredTools, tool] : form.value.requiredTools.filter((item) => item !== tool);
  if (checked) form.value.evidenceMinCalls[tool] ??= 1;
}
function toggleEvidencePredicate(tool: string, checked: boolean) {
  if (checked) form.value.evidencePredicates[tool] = { path: "/status", type: "non_empty_string" };
  else delete form.value.evidencePredicates[tool];
}
async function save() {
  if (!form.value.name.trim() || !form.value.description.trim() || !form.value.tools.length || !form.value.completionTool || !form.value.completionPath.trim() || !form.value.effectiveFrom || !Number.isFinite(form.value.minutes) || form.value.minutes <= 0) {
    error.value = "Complete the name, description, participating tools, terminal result, positive human baseline and effective date."; return;
  }
  saving.value = true; error.value = "";
  try {
    const next = definition.value
      ? JSON.parse(JSON.stringify(definition.value)) as Definition
      : {
        manifest_version: "1.0",
        integration: { id: cleanId(props.upstream) || "integration", display_name: props.upstream, upstream_name: props.upstream, status: "draft", revision: 1 },
        tool_refs: [], experiences: [], workflows: [], standalone_tool_values: [], publication: null,
      } satisfies Definition;
    const id = editingId.value ?? cleanId(form.value.name);
    if (!id) throw new Error("Use a name that produces a valid workflow ID.");
    for (const tool of [...new Set([...form.value.tools, ...next.tool_refs.map((ref: any) => ref.name)])]) {
      if (!props.tools.includes(tool)) continue;
      const detail = await apiFetch<{ schema_sha256: string }>(`/admin/upstreams/${encodeURIComponent(props.upstream)}/tools/${encodeURIComponent(tool)}/guidance`);
      const index = next.tool_refs.findIndex((ref: any) => ref.name === tool);
      const item = { name: tool, schema_sha256: detail.schema_sha256 };
      if (index >= 0) next.tool_refs[index] = item; else next.tool_refs.push(item);
    }
    const existingWorkflow = next.workflows.find((item: Workflow) => item.id === id);
    const previousCompletionPredicate = existingWorkflow?.completion?.success_when;
    const completionPredicate = {
      ...(previousCompletionPredicate?.type === form.value.completionType ? previousCompletionPredicate : {}),
      path: form.value.completionPath.trim(), type: form.value.completionType,
      ...(form.value.completionType === "boolean" ? { equals: true } : {}),
    };
    const previousEvidence = new Map<string, any>((existingWorkflow?.required_evidence ?? []).map((item: any) => [item.tool, item]));
    const workflow = {
      ...(existingWorkflow ?? {}),
      id, name: form.value.name.trim(), description: form.value.description.trim(), participating_tools: [...form.value.tools],
      required_evidence: form.value.requiredTools.map((tool) => {
        const predicate = form.value.evidencePredicates[tool];
        const previous = previousEvidence.get(tool);
        const priorPredicate = previous?.result_when;
        const resultWhen = predicate?.path.trim()
          ? { ...(priorPredicate?.path === predicate.path.trim() && priorPredicate?.type === predicate.type ? priorPredicate : {}), path: predicate.path.trim(), type: predicate.type, ...(predicate.type === "boolean" ? { equals: true } : {}) }
          : undefined;
        return { ...(previous ?? {}), tool, min_successful_calls: form.value.evidenceMinCalls[tool] ?? 1, ...(resultWhen ? { result_when: resultWhen } : {}) };
      }),
      completion: { ...(existingWorkflow?.completion ?? {}), kind: "terminal_tool_result", tool: form.value.completionTool, success_when: completionPredicate },
      business_value: { ...(existingWorkflow?.business_value ?? {}), manual_baseline_minutes: form.value.minutes, baseline_source: form.value.source, confidence: form.value.source === "administrator_estimate" ? "unvalidated" : form.value.source === "sampled_manual_measurement" ? "sampled" : "validated", effective_from: form.value.effectiveFrom, evidence_note: form.value.evidenceNote.trim() || undefined },
    };
    const index = next.workflows.findIndex((item: Workflow) => item.id === id);
    if (index >= 0) next.workflows[index] = workflow; else next.workflows.push(workflow);
    next.integration.status = "draft"; next.integration.revision = 1; next.publication = null;
    await apiFetch("/admin/integration-definitions/validate", { method: "POST", body: { definition: next } });
    const draft = await apiFetch<{ revision: number }>("/admin/integration-definitions/drafts", { method: "POST", body: { definition: next } });
    await apiFetch(`/admin/integration-definitions/${encodeURIComponent(props.upstream)}/publish`, { method: "POST", body: { revision: draft.revision } });
    close(); await load();
  } catch (err) { error.value = extractErrorMessage(err); }
  finally { saving.value = false; }
}
</script>

<template>
  <section class="mb-3 max-w-[calc(100vw-4rem)] border-b border-zinc-200 pb-3 lg:max-w-none" :aria-label="`Workflow catalog for ${upstream}`">
    <div class="mb-2 flex flex-wrap items-start justify-between gap-2">
      <div><h3 class="text-[0.9375rem] font-semibold text-zinc-950">Workflow Catalog</h3><p class="mt-0.5 text-xs text-zinc-600">Business processes measured for this upstream. Listed tool order is descriptive and is not enforced.</p></div>
      <Button v-if="canEdit" @click="beginCreate">Create workflow</Button>
    </div>
    <p v-if="!canEdit" class="mb-2 rounded-md border border-[#E4E4E7] bg-white px-3 py-2 text-xs text-[#52525B]">Read-only: workflow changes are disabled because dynamic upstream writes are not permitted.</p>
    <div v-if="loading" class="py-3 text-sm text-[#52525B]" role="status">Loading workflows…</div>
    <div v-else-if="loadError" class="rounded-md border border-red-200 bg-red-50 px-3 py-2 text-sm text-red-800" role="alert"><p>{{ loadError }}</p><Button class="mt-2" @click="load">Retry</Button></div>
    <p v-else-if="!definition" class="py-2 text-sm text-[#52525B]">No integration definition or workflows exist for this upstream yet. Create a workflow to establish its first definition.</p>
    <p v-else-if="!workflows.length" class="py-2 text-sm text-[#52525B]">No workflows are defined for this upstream.</p>
    <ul v-else class="grid grid-cols-1 gap-2 xl:grid-cols-2">
      <li v-for="workflow in workflows" :key="workflow.id" class="rounded-md border border-[#E4E4E7] bg-white p-3">
        <div class="flex flex-wrap items-start justify-between gap-2"><div><h4 class="text-sm font-semibold text-[#09090B]">{{ workflow.name }}</h4><p class="font-mono-data text-[11px] text-[#71717A]">{{ workflow.id }}</p></div><Button v-if="canEdit" :aria-label="`Edit ${workflow.name}`" @click="beginEdit(workflow, $event)">Edit</Button></div>
        <p class="mt-2 text-xs text-[#52525B]">{{ workflow.description }}</p>
        <dl class="mt-2 grid gap-1 text-xs sm:grid-cols-2"><div><dt class="font-medium text-[#71717A]">Participating tools</dt><dd class="break-words">{{ workflow.participating_tools.join(" → ") }}</dd></div><div><dt class="font-medium text-[#71717A]">Required evidence</dt><dd>{{ workflow.required_evidence.length ? workflow.required_evidence.map((item: any) => `${item.tool} (${item.min_successful_calls})`).join(", ") : "None" }}</dd></div><div><dt class="font-medium text-[#71717A]">Terminal completion</dt><dd class="break-words">{{ workflow.completion.tool }} · {{ workflow.completion.success_when.path }} {{ workflow.completion.success_when.type }}</dd></div><div><dt class="font-medium text-[#71717A]">Human baseline</dt><dd class="font-mono-data">{{ workflow.business_value.manual_baseline_minutes }} min · {{ workflow.business_value.baseline_source }} · {{ workflow.business_value.confidence }}</dd></div></dl>
      </li>
    </ul>
    <div class="mt-3 flex flex-wrap items-center gap-2"><Button @click="emit('refresh-catalog')">Refresh API tools</Button><span class="text-xs text-[#71717A]">Refreshes tools and schemas for every registered upstream.</span></div>
    <Drawer v-model:open="open">
      <DrawerBackdrop />
      <DrawerContent side="right" class="w-full max-w-2xl overflow-y-auto bg-white p-5 sm:p-7">
        <DrawerClose aria-label="Close workflow editor" class="absolute right-4 top-4"><AppIcon name="x" :size="16" /></DrawerClose>
        <DrawerTitle>{{ editingId ? "Edit workflow" : "Create workflow" }}</DrawerTitle>
        <DrawerDescription>Describe which business activity these tool calls represent and the result that proves it completed.</DrawerDescription>
        <form class="mt-5 space-y-4" @submit.prevent="save">
          <Field><FieldLabel for="workflow-name">Workflow name</FieldLabel><Input id="workflow-name" v-model="form.name" @input="!editingId && (form.id = cleanId(form.name))" required /><FieldDescription>Stable ID: {{ editingId || form.id || "generated from name" }}</FieldDescription></Field>
          <Field><FieldLabel for="workflow-description">Business activity</FieldLabel><Textarea id="workflow-description" v-model="form.description" required /></Field>
          <fieldset class="rounded-md border border-[#E4E4E7] p-3"><legend class="px-1 text-sm font-medium">Participating tools</legend><p class="mb-2 text-xs text-[#71717A]">Select tools that may be attributed to this workflow. Their display order is only for administrator organization.</p><div class="max-h-40 space-y-1 overflow-y-auto"><label v-for="tool in tools" :key="tool" class="flex min-h-9 items-center gap-2 break-all text-xs"><Checkbox :checked="form.tools.includes(tool)" :aria-label="`Participating tool ${tool}`" @checked-change="() => toggleTool(tool)"><CheckboxIndicator><AppIcon name="check" :size="13" /></CheckboxIndicator></Checkbox>{{ tool }}</label></div></fieldset>
          <fieldset class="rounded-md border border-[#E4E4E7] p-3"><legend class="px-1 text-sm font-medium">Required completion evidence</legend><p class="mb-2 text-xs text-[#71717A]">Each selected tool call must succeed at least once before this workflow can count. A result check is optional.</p><div v-if="form.tools.length" class="space-y-2"><div v-for="tool in form.tools" :key="tool" class="rounded border border-[#E4E4E7] p-2"><label class="flex min-h-9 items-center gap-2 break-all text-xs"><Checkbox :checked="form.requiredTools.includes(tool)" :aria-label="`Required evidence tool ${tool}`" @checked-change="toggleRequiredEvidence(tool, $event === true)"><CheckboxIndicator><AppIcon name="check" :size="13" /></CheckboxIndicator></Checkbox>Require successful call: {{ tool }}</label><template v-if="form.requiredTools.includes(tool)"><label class="mb-2 block text-xs">Minimum successful calls<Input :model-value="String(form.evidenceMinCalls[tool] ?? 1)" :aria-label="`Minimum successful calls for ${tool}`" type="number" min="1" max="10" step="1" @update:model-value="form.evidenceMinCalls[tool] = Number($event)" /></label><label class="flex min-h-8 items-center gap-2 text-xs"><Checkbox :checked="Boolean(form.evidencePredicates[tool])" :aria-label="`Require result predicate for ${tool}`" @checked-change="toggleEvidencePredicate(tool, $event === true)"><CheckboxIndicator><AppIcon name="check" :size="13" /></CheckboxIndicator></Checkbox>Also require a matching result</label><div v-if="form.evidencePredicates[tool]" class="grid grid-cols-1 gap-2 sm:grid-cols-2"><Input v-model="form.evidencePredicates[tool].path" :aria-label="`Required evidence result JSON pointer for ${tool}`" placeholder="/status"/><Select :value="form.evidencePredicates[tool].type" @value-change="(value) => { if (value) form.evidencePredicates[tool].type = String(value) }"><SelectTrigger :aria-label="`Required evidence result type for ${tool}`" class="min-h-10"><SelectValue /><AppIcon name="chevron-down" :size="14" /></SelectTrigger><SelectContent><SelectItem v-for="predicate in ['non_empty_string', 'string', 'boolean', 'object', 'non_empty_object', 'array', 'non_empty_array', 'number', 'integer', 'null']" :key="predicate" :value="predicate">{{ predicate.replaceAll('_', ' ') }}<SelectItemIndicator><AppIcon name="check" :size="14" /></SelectItemIndicator></SelectItem></SelectContent></Select></div></template></div></div><p v-else class="text-xs text-[#71717A]">Choose participating tools first.</p></fieldset>
          <fieldset class="grid grid-cols-1 gap-3 rounded-md border border-[#E4E4E7] p-3 sm:grid-cols-2"><legend class="px-1 text-sm font-medium">Terminal completion</legend><Field><FieldLabel>Tool proving completion</FieldLabel><Select :value="form.completionTool" @value-change="(value) => { form.completionTool = String(value ?? '') }"><SelectTrigger aria-label="Tool proving completion" class="min-h-10"><SelectValue placeholder="Choose a tool" /><AppIcon name="chevron-down" :size="14" /></SelectTrigger><SelectContent><SelectItem v-for="tool in form.tools" :key="tool" :value="tool">{{ tool }}<SelectItemIndicator><AppIcon name="check" :size="14" /></SelectItemIndicator></SelectItem></SelectContent></Select></Field><Field><FieldLabel for="completion-path">Result JSON pointer</FieldLabel><Input id="completion-path" v-model="form.completionPath" placeholder="/status" /></Field><Field><FieldLabel>Expected result type</FieldLabel><Select :value="form.completionType" @value-change="(value) => { if (value) form.completionType = String(value) }"><SelectTrigger aria-label="Expected completion result type" class="min-h-10"><SelectValue /><AppIcon name="chevron-down" :size="14" /></SelectTrigger><SelectContent><SelectItem v-for="predicate in ['non_empty_string', 'string', 'boolean', 'object', 'non_empty_object', 'array', 'non_empty_array', 'number', 'integer', 'null']" :key="predicate" :value="predicate">{{ predicate.replaceAll('_', ' ') }}<SelectItemIndicator><AppIcon name="check" :size="14" /></SelectItemIndicator></SelectItem></SelectContent></Select></Field></fieldset>
          <fieldset class="grid grid-cols-1 gap-3 rounded-md border border-[#E4E4E7] p-3 sm:grid-cols-2"><legend class="px-1 text-sm font-medium">Human baseline</legend><Field><FieldLabel for="baseline-minutes">Manual workflow time (minutes)</FieldLabel><Input id="baseline-minutes" :model-value="String(form.minutes)" type="number" min="0.01" step="0.01" required @update:model-value="form.minutes = Number($event)" /></Field><Field><FieldLabel>Evidence source</FieldLabel><Select :value="form.source" @value-change="(value) => { if (value) form.source = String(value) }"><SelectTrigger aria-label="Baseline evidence source" class="min-h-10"><SelectValue /><AppIcon name="chevron-down" :size="14" /></SelectTrigger><SelectContent><SelectItem value="administrator_estimate">Administrator estimate</SelectItem><SelectItem value="sampled_manual_measurement">Sampled manual measurement</SelectItem><SelectItem value="validated_external_measurement">Validated external measurement</SelectItem></SelectContent></Select></Field><Field><FieldLabel for="baseline-effective">Effective date</FieldLabel><Input id="baseline-effective" v-model="form.effectiveFrom" type="date" required /></Field><Field class="sm:col-span-2"><FieldLabel for="baseline-note">Evidence note</FieldLabel><Textarea id="baseline-note" v-model="form.evidenceNote" maxlength="1000" /></Field></fieldset>
          <p v-if="error" role="alert" class="rounded-md border border-red-200 bg-red-50 p-3 text-sm text-red-800">{{ error }}</p>
          <div class="flex flex-wrap justify-end gap-2 border-t border-[#E4E4E7] pt-4"><Button type="button" @click="close">Cancel</Button><Button type="submit" :disabled="saving" pending-announcement="Validating and publishing workflow">{{ saving ? "Saving…" : "Validate and publish" }}</Button></div>
        </form>
      </DrawerContent>
    </Drawer>
  </section>
</template>
