<script setup lang="ts">
import { Button } from "@ui-sdk/vue/button";
import { Checkbox, CheckboxIndicator } from "@ui-sdk/vue/checkbox";
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
import AppIcon from "~/components/AppIcon.vue";
import { extractErrorMessage } from "~/composables/useAdminApi";

type Workflow = Record<string, any>;
type Definition = Record<string, any>;

const props = withDefaults(
  defineProps<{
    open: boolean;
    upstream?: string;
    upstreams?: string[];
    tools?: string[];
    toolsByUpstream?: Record<string, string[]>;
    definition?: Definition | null;
    editingWorkflow?: Workflow | null;
    promotingCandidate?: Record<string, any> | null;
    lockUpstream?: boolean;
    gatewayCurrency?: string;
  }>(),
  {
    upstream: "",
    upstreams: undefined,
    tools: undefined,
    toolsByUpstream: undefined,
    definition: undefined,
    editingWorkflow: null,
    promotingCandidate: null,
    lockUpstream: false,
    gatewayCurrency: "",
  },
);

const emit = defineEmits<{
  (event: "update:open", value: boolean): void;
  (
    event: "saved",
    payload: {
      statusMessage: string;
      upstream: string;
      promotedCandidateId?: string | null;
    },
  ): void;
}>();

const { apiFetch } = useAdminApi();

const selectedUpstream = ref("");
const editingId = ref<string | null>(null);
const promotingCandidateId = ref<string | null>(null);
const saving = ref(false);
const error = ref("");
const submissionStep = ref<"validate" | "save-draft" | "publish">("validate");

const loadingContext = ref(false);
const fetchedToolsByUpstream = ref<Record<string, string[]>>({});
const fetchedDefinitions = ref<Record<string, Definition | null>>({});
const fetchedGatewayCurrency = ref("USD");
const activeCurrency = computed(
  () => (props.gatewayCurrency || fetchedGatewayCurrency.value || "USD").trim().toUpperCase(),
);

const form = ref({
  id: "",
  name: "",
  description: "",
  tools: [] as string[],
  requiredTools: [] as string[],
  evidenceMinCalls: {} as Record<string, number>,
  evidencePredicates: {} as Record<string, { path: string; type: string }>,
  completionTool: "",
  completionPath: "/status",
  completionType: "non_empty_string",
  minutes: "",
  reviewMinutes: "",
  correctionMinutes: "",
  loadedHourlyCost: "",
  regionalRates: [] as { region: string; rate: string }[],
  source: "administrator_estimate",
  effectiveFrom: new Date().toISOString().slice(0, 10),
  evidenceNote: "",
});

const isUpstreamLocked = computed(
  () => props.lockUpstream || (!props.upstreams?.length && Boolean(props.upstream)),
);

const upstreamSelectItems = computed(() =>
  (props.upstreams ?? []).map((name) => ({ value: name, label: name })),
);

const activeTools = computed<string[]>(() => {
  const target = selectedUpstream.value;
  if (props.tools && target === props.upstream) return props.tools;
  if (props.toolsByUpstream?.[target]) return props.toolsByUpstream[target];
  return fetchedToolsByUpstream.value[target] ?? [];
});

const activeDefinition = computed<Definition | null>(() => {
  const target = selectedUpstream.value;
  if (props.definition !== undefined && target === props.upstream) {
    return props.definition;
  }
  return fetchedDefinitions.value[target] ?? null;
});

function cleanId(value: string) {
  return value
    .toLowerCase()
    .replace(/[^a-z0-9]+/g, "_")
    .replace(/^_+|_+$/g, "")
    .slice(0, 48);
}

function validJsonPointer(value: string) {
  return value === "" || (value.startsWith("/") && !/(~(?![01]))/.test(value));
}

function sourceToConfidence(source: string): "unvalidated" | "sampled" | "validated" {
  if (source === "validated_external_measurement") return "validated";
  if (source === "sampled_manual_measurement") return "sampled";
  return "unvalidated";
}

async function ensureUpstreamContext(targetUpstream: string) {
  if (!targetUpstream) return;
  const needsTools =
    !(props.tools && targetUpstream === props.upstream) &&
    !props.toolsByUpstream?.[targetUpstream] &&
    !fetchedToolsByUpstream.value[targetUpstream];
  const needsDefinition =
    !(props.definition !== undefined && targetUpstream === props.upstream) &&
    !(targetUpstream in fetchedDefinitions.value);

  if (!needsTools && !needsDefinition && !props.gatewayCurrency) {
    try {
      const curRes = await apiFetch<{ reporting_currency?: string }>("/admin/business-value/settings");
      if (curRes?.reporting_currency) {
        fetchedGatewayCurrency.value = curRes.reporting_currency;
      }
    } catch {
    }
  }
  if (!needsTools && !needsDefinition) return;
  loadingContext.value = true;
  try {
    if (needsTools) {
      const res = await apiFetch<{
        upstreams?: Array<{ name: string; tools?: string[] }>;
      }>("/admin/upstreams");
      const nextMap: Record<string, string[]> = { ...fetchedToolsByUpstream.value };
      for (const item of res.upstreams ?? []) {
        if (item?.name) {
          nextMap[item.name] = Array.isArray(item.tools) ? item.tools : [];
        }
      }
      if (!(targetUpstream in nextMap)) nextMap[targetUpstream] = [];
      fetchedToolsByUpstream.value = nextMap;
    }
    if (needsDefinition) {
      const defRes = await apiFetch<{ current?: Definition | { definition?: Definition } | null }>(
        `/admin/integration-definitions/${encodeURIComponent(targetUpstream)}`,
      );
      const current = defRes.current;
      fetchedDefinitions.value = {
        ...fetchedDefinitions.value,
        [targetUpstream]:
          current && "definition" in current ? current.definition ?? null : current ?? null,
      };
    }
  } catch (err) {
    error.value = extractErrorMessage(err);
  } finally {
    loadingContext.value = false;
  }
}

function initFormFromProps() {
  error.value = "";
  const targetUpstream = props.upstream || props.upstreams?.[0] || "";
  selectedUpstream.value = targetUpstream;

  if (props.editingWorkflow) {
    const workflow = props.editingWorkflow;
    editingId.value = workflow.id;
    promotingCandidateId.value = null;
    const existingBv = workflow.business_value ?? {};
    const regEntries = Object.entries(existingBv.regional_labor_rates ?? {}).map(([reg, rate]) => ({
      region: String(reg),
      rate: String(rate),
    }));
    form.value = {
      id: workflow.id,
      name: workflow.name,
      description: workflow.description,
      tools: [...(workflow.participating_tools ?? [])],
      requiredTools: (workflow.required_evidence ?? []).map((item: any) => item.tool),
      evidenceMinCalls: Object.fromEntries(
        (workflow.required_evidence ?? []).map((item: any) => [
          item.tool,
          item.min_successful_calls,
        ]),
      ),
      evidencePredicates: Object.fromEntries(
        (workflow.required_evidence ?? [])
          .filter((item: any) => item.result_when)
          .map((item: any) => [
            item.tool,
            { path: item.result_when.path, type: item.result_when.type },
          ]),
      ),
      completionTool: workflow.completion?.tool ?? "",
      completionPath: workflow.completion?.success_when?.path ?? "/status",
      completionType: workflow.completion?.success_when?.type ?? "non_empty_string",
      minutes: String(existingBv.manual_baseline_minutes ?? ""),
      reviewMinutes:
        existingBv.review_minutes?.minutes != null ? String(existingBv.review_minutes.minutes) : "",
      correctionMinutes:
        existingBv.correction_minutes?.minutes != null
          ? String(existingBv.correction_minutes.minutes)
          : "",
      loadedHourlyCost:
        existingBv.loaded_labor_cost_per_hour != null
          ? String(existingBv.loaded_labor_cost_per_hour)
          : "",
      regionalRates: regEntries,
      source: existingBv.baseline_source ?? "administrator_estimate",
      effectiveFrom: existingBv.effective_from ?? new Date().toISOString().slice(0, 10),
      evidenceNote: existingBv.evidence_note ?? "",
    };
    return;
  }

  if (props.promotingCandidate) {
    const candidate = props.promotingCandidate;
    const draft = candidate.draft_definition ?? {};
    editingId.value = null;
    promotingCandidateId.value = String(candidate.candidate_id || "") || null;
    const reqEv = Array.isArray(draft.required_evidence) ? draft.required_evidence : [];
    form.value = {
      id: String(draft.id ?? ""),
      name: String(draft.name ?? ""),
      description: String(draft.description ?? ""),
      tools: Array.isArray(draft.participating_tools) ? [...draft.participating_tools] : [],
      requiredTools: reqEv.map((item: any) => String(item.tool)),
      evidenceMinCalls: Object.fromEntries(
        reqEv.map((item: any) => [item.tool, Number(item.min_successful_calls ?? 1)]),
      ),
      evidencePredicates: Object.fromEntries(
        reqEv
          .filter((item: any) => item.result_when)
          .map((item: any) => [
            item.tool,
            { path: String(item.result_when.path), type: String(item.result_when.type) },
          ]),
      ),
      completionTool: String(draft.completion?.tool ?? ""),
      completionPath: String(draft.completion?.success_when?.path ?? "/status"),
      completionType: String(draft.completion?.success_when?.type ?? "non_empty_string"),
      minutes: "5",
      reviewMinutes: "",
      correctionMinutes: "",
      loadedHourlyCost: "",
      regionalRates: [],
      source: "administrator_estimate",
      effectiveFrom: new Date().toISOString().slice(0, 10),
      evidenceNote: "Confirmed from auto-discovered workflow candidate.",
    };
    return;
  }

  editingId.value = null;
  promotingCandidateId.value = null;
  form.value = {
    id: "",
    name: "",
    description: "",
    tools: [],
    requiredTools: [],
    evidenceMinCalls: {},
    evidencePredicates: {},
    completionTool: "",
    completionPath: "/status",
    completionType: "non_empty_string",
    minutes: "",
    reviewMinutes: "",
    correctionMinutes: "",
    loadedHourlyCost: "",
    regionalRates: [],
    source: "administrator_estimate",
    effectiveFrom: new Date().toISOString().slice(0, 10),
    evidenceNote: "",
  };
}

watch(
  () => props.open,
  (isOpen) => {
    if (!isOpen) return;
    initFormFromProps();
    if (selectedUpstream.value) {
      void ensureUpstreamContext(selectedUpstream.value);
    }
  },
  { immediate: true },
);

function handleUpstreamChange(nextUpstream: string) {
  selectedUpstream.value = nextUpstream;
  form.value.tools = [];
  form.value.requiredTools = [];
  form.value.evidenceMinCalls = {};
  form.value.evidencePredicates = {};
  form.value.completionTool = "";
  error.value = "";
  void ensureUpstreamContext(nextUpstream);
}

function close() {
  emit("update:open", false);
}

function toggleTool(tool: string) {
  form.value.tools = form.value.tools.includes(tool)
    ? form.value.tools.filter((item) => item !== tool)
    : [...form.value.tools, tool];
  form.value.requiredTools = form.value.requiredTools.filter((item) =>
    form.value.tools.includes(item),
  );
  if (!form.value.tools.includes(tool)) {
    delete form.value.evidencePredicates[tool];
    delete form.value.evidenceMinCalls[tool];
  }
  if (!form.value.tools.includes(form.value.completionTool)) {
    form.value.completionTool = "";
  }
}

function toggleRequiredEvidence(tool: string, checked: boolean) {
  form.value.requiredTools = checked
    ? [...form.value.requiredTools, tool]
    : form.value.requiredTools.filter((item) => item !== tool);
  if (checked) form.value.evidenceMinCalls[tool] ??= 1;
}

function toggleEvidencePredicate(tool: string, checked: boolean) {
  if (checked) form.value.evidencePredicates[tool] = { path: "/status", type: "non_empty_string" };
  else delete form.value.evidencePredicates[tool];
}

function validateForm() {
  const issues: string[] = [];
  if (!selectedUpstream.value) issues.push("Select a target upstream.");
  if (!form.value.name.trim()) issues.push("Add a workflow name.");
  else if (!cleanId(form.value.name))
    issues.push("Use a name with letters or numbers so the workflow can get a stable ID.");
  if (!form.value.description.trim())
    issues.push("Describe the business activity in plain language.");
  if (!form.value.tools.length) issues.push("Choose at least one participating tool.");
  if (form.value.tools.some((tool) => !activeTools.value.includes(tool)))
    issues.push(
      "One or more selected tools are no longer available. Refresh API tools and choose them again.",
    );
  if (form.value.requiredTools.some((tool) => !form.value.tools.includes(tool)))
    issues.push("Required evidence must use a participating tool.");
  if (!form.value.completionTool || !form.value.tools.includes(form.value.completionTool))
    issues.push("Choose the participating tool whose result proves this activity completed.");
  if (!validJsonPointer(form.value.completionPath.trim()))
    issues.push(
      "Use a valid JSON pointer for the completion result, such as /status. Escape ~ as ~0 and / as ~1.",
    );
  if (
    form.value.requiredTools.some(
      (tool) =>
        !Number.isInteger(form.value.evidenceMinCalls[tool] ?? 1) ||
        (form.value.evidenceMinCalls[tool] ?? 1) < 1 ||
        (form.value.evidenceMinCalls[tool] ?? 1) > 10,
    )
  )
    issues.push("Set required successful calls to a whole number from 1 to 10.");
  if (
    Object.values(form.value.evidencePredicates).some(
      (predicate) => !validJsonPointer(predicate.path.trim()),
    )
  )
    issues.push("Use a valid JSON pointer for each required evidence result.");
  const minutes = Number(form.value.minutes);
  if (!form.value.minutes.trim() || !Number.isFinite(minutes) || minutes <= 0)
    issues.push(
      "Enter the manual time in minutes. This baseline is required for the Business Value comparison.",
    );
  if (!form.value.effectiveFrom) issues.push("Choose the date this baseline became accurate.");
  return issues;
}

function validationErrorMessage(err: unknown) {
  const record = err && typeof err === "object" ? (err as Record<string, unknown>) : {};
  const response =
    record.response && typeof record.response === "object"
      ? (record.response as Record<string, unknown>)
      : {};
  const data = (record.data && typeof record.data === "object"
    ? record.data
    : response._data) as Record<string, unknown> | undefined;
  const detail = data?.detail;
  const status = Number(record.statusCode ?? record.status ?? response.status ?? 0);
  if (Array.isArray(detail)) {
    const messages = detail.flatMap((item) => {
      if (!item || typeof item !== "object") return [];
      const issue = item as Record<string, unknown>;
      const message =
        typeof issue.msg === "string"
          ? issue.msg
          : typeof issue.message === "string"
            ? issue.message
            : "";
      if (!message) return [];
      const location = Array.isArray(issue.loc)
        ? issue.loc
            .map(String)
            .filter((part) => !["body", "definition"].includes(part))
            .join(" · ")
        : "Workflow definition";
      return [`${location || "Workflow definition"}: ${message}`];
    });
    if (messages.length) return messages.join(" ");
  }
  if (
    typeof detail === "string" &&
    detail.trim() &&
    !/^(unprocessable content|validation error)$/i.test(detail.trim())
  ) {
    return detail.trim();
  }
  const message = extractErrorMessage(err);
  if (status === 422 || /^(unprocessable content|validation error)$/i.test(message.trim())) {
    if (submissionStep.value === "publish") {
      return "The definition was validated and saved as a draft, but publication failed. Your entries are still here; retry publishing or refresh the upstream details before trying again.";
    }
    if (submissionStep.value === "save-draft") {
      return "The definition passed validation, but the draft could not be saved. Your entries are still here; retry, or refresh API tools if a selected tool has changed.";
    }
    return "The gateway rejected this definition. Check that each selected tool is available, the completion tool is selected above, and its result pointer and expected type match the tool response. Your entries are still here; refresh API tools if a schema changed, then retry.";
  }
  return message;
}

async function save() {
  const issues = validateForm();
  if (issues.length) {
    error.value = issues.join(" ");
    return;
  }
  const targetUpstream = selectedUpstream.value;
  saving.value = true;
  error.value = "";
  try {
    const next = activeDefinition.value
      ? (JSON.parse(JSON.stringify(activeDefinition.value)) as Definition)
      : ({
          manifest_version: "1.0",
          integration: {
            id: cleanId(targetUpstream) || "integration",
            display_name: targetUpstream,
            upstream_name: targetUpstream,
            status: "draft",
            revision: 1,
          },
          tool_refs: [],
          experiences: [],
          workflows: [],
          standalone_tool_values: [],
          publication: null,
        } satisfies Definition);
    const id = editingId.value ?? cleanId(form.value.name);
    if (!id) throw new Error("Use a name that produces a valid workflow ID.");
    for (const tool of [
      ...new Set([...form.value.tools, ...next.tool_refs.map((ref: any) => ref.name)]),
    ]) {
      if (!activeTools.value.includes(tool)) continue;
      const detail = await apiFetch<{ schema_sha256: string }>(
        `/admin/upstreams/${encodeURIComponent(targetUpstream)}/tools/${encodeURIComponent(tool)}/guidance`,
      );
      const index = next.tool_refs.findIndex((ref: any) => ref.name === tool);
      const item = { name: tool, schema_sha256: detail.schema_sha256 };
      if (index >= 0) next.tool_refs[index] = item;
      else next.tool_refs.push(item);
    }
    const existingWorkflow = next.workflows.find((item: Workflow) => item.id === id);
    const previousCompletionPredicate = existingWorkflow?.completion?.success_when;
    const completionPredicate = {
      ...(previousCompletionPredicate?.type === form.value.completionType
        ? previousCompletionPredicate
        : {}),
      path: form.value.completionPath.trim(),
      type: form.value.completionType,
      ...(form.value.completionType === "boolean" ? { equals: true } : {}),
    };
    const previousEvidence = new Map<string, any>(
      (existingWorkflow?.required_evidence ?? []).map((item: any) => [item.tool, item]),
    );
    const confidence = sourceToConfidence(form.value.source);
    const regionalMap: Record<string, number> = {};
    for (const row of form.value.regionalRates) {
      const key = row.region.trim().toUpperCase();
      const val = Number(row.rate);
      if (key && Number.isFinite(val) && val >= 0) {
        regionalMap[key] = val;
      }
    }
    const hourlyRate =
      form.value.loadedHourlyCost.trim() !== "" ? Number(form.value.loadedHourlyCost) : null;
    const hasPricing = hourlyRate !== null || Object.keys(regionalMap).length > 0;
    const bvPayload: Record<string, any> = {
      ...(existingWorkflow?.business_value ?? {}),
      manual_baseline_minutes: Number(form.value.minutes),
      baseline_source: form.value.source,
      confidence,
      effective_from: form.value.effectiveFrom,
      evidence_note: form.value.evidenceNote.trim() || undefined,
    };
    if (form.value.reviewMinutes.trim() !== "") {
      bvPayload.review_minutes = { kind: "expected_average", minutes: Number(form.value.reviewMinutes) };
    } else {
      delete bvPayload.review_minutes;
    }
    if (form.value.correctionMinutes.trim() !== "") {
      bvPayload.correction_minutes = {
        kind: "expected_average",
        minutes: Number(form.value.correctionMinutes),
      };
    } else {
      delete bvPayload.correction_minutes;
    }
    if (hourlyRate !== null) {
      bvPayload.loaded_labor_cost_per_hour = hourlyRate;
    } else {
      delete bvPayload.loaded_labor_cost_per_hour;
    }
    if (Object.keys(regionalMap).length > 0) {
      bvPayload.regional_labor_rates = regionalMap;
    } else {
      delete bvPayload.regional_labor_rates;
    }
    if (hasPricing) {
      bvPayload.currency = bvPayload.currency || activeCurrency.value;
    } else {
      delete bvPayload.currency;
    }
    const workflow = {
      ...(existingWorkflow ?? {}),
      id,
      name: form.value.name.trim(),
      description: form.value.description.trim(),
      participating_tools: [...form.value.tools],
      required_evidence: form.value.requiredTools.map((tool) => {
        const predicate = form.value.evidencePredicates[tool];
        const previous = previousEvidence.get(tool);
        const priorPredicate = previous?.result_when;
        const resultWhen = predicate?.path.trim()
          ? {
              ...(priorPredicate?.path === predicate.path.trim() &&
              priorPredicate?.type === predicate.type
                ? priorPredicate
                : {}),
              path: predicate.path.trim(),
              type: predicate.type,
              ...(predicate.type === "boolean" ? { equals: true } : {}),
            }
          : undefined;
        return {
          ...(previous ?? {}),
          tool,
          min_successful_calls: form.value.evidenceMinCalls[tool] ?? 1,
          ...(resultWhen ? { result_when: resultWhen } : {}),
        };
      }),
      completion: {
        ...(existingWorkflow?.completion ?? {}),
        kind: "terminal_tool_result",
        tool: form.value.completionTool,
        success_when: completionPredicate,
      },
      business_value: bvPayload,
    };
    const index = next.workflows.findIndex((item: Workflow) => item.id === id);
    if (index >= 0) next.workflows[index] = workflow;
    else next.workflows.push(workflow);
    next.integration.status = "draft";
    next.integration.revision = 1;
    next.publication = null;
    submissionStep.value = "validate";
    await apiFetch("/admin/integration-definitions/validate", {
      method: "POST",
      body: { definition: next },
    });
    submissionStep.value = "save-draft";
    const draft = await apiFetch<{ revision: number }>("/admin/integration-definitions/drafts", {
      method: "POST",
      body: { definition: next },
    });
    submissionStep.value = "publish";
    await apiFetch(
      `/admin/integration-definitions/${encodeURIComponent(targetUpstream)}/publish`,
      { method: "POST", body: { revision: draft.revision } },
    );
    let promotedId: string | null = null;
    if (promotingCandidateId.value) {
      promotedId = promotingCandidateId.value;
      await apiFetch(
        `/admin/workflows/discovered/${encodeURIComponent(promotedId)}/promote`,
        {
          method: "POST",
          body: {
            manual_baseline_minutes: Number(form.value.minutes),
            baseline_source: form.value.source,
            confidence,
            effective_from: form.value.effectiveFrom,
            evidence_note: form.value.evidenceNote.trim() || undefined,
            publish: false,
            mark_promoted_only: true,
          },
        },
      );
      promotingCandidateId.value = null;
    }
    const message = `${editingId.value ? "Updated" : "Published"} “${form.value.name.trim()}”. This definition measures tool-call outcomes; it does not execute a workflow.`;
    emit("saved", {
      statusMessage: message,
      upstream: targetUpstream,
      promotedCandidateId: promotedId,
    });
    close();
  } catch (err) {
    error.value = validationErrorMessage(err);
  } finally {
    saving.value = false;
  }
}
</script>

<template>
  <Drawer :open="open" side="right" prevent-edge-swipe @update:open="emit('update:open', $event)">
    <DrawerBackdrop />
    <DrawerContent
      class="flex h-dvh w-full max-w-[95vw] flex-col gap-0 border-l border-[#E4E4E7] bg-white p-0 sm:w-[38rem]"
    >
      <div class="shrink-0 flex items-start justify-between gap-3 border-b border-[#E4E4E7] px-5 py-4">
        <div class="min-w-0 space-y-1">
          <DrawerTitle>{{ editingId ? "Edit workflow" : "Create workflow" }}</DrawerTitle>
          <DrawerDescription>
            Group calls that belong to one business activity and define the evidence that marks it
            complete. This does not create an executable workflow or change which tools callers can
            access.
          </DrawerDescription>
        </div>
        <DrawerClose
          aria-label="Close workflow editor"
          class="inline-flex min-h-11 min-w-11 shrink-0 items-center justify-center rounded-md border border-[#D4D4D8] bg-white text-[#09090B] hover:bg-[#F4F4F5] focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-[#0F766E]"
        >
          <AppIcon name="x" :size="16" />
        </DrawerClose>
      </div>

      <form class="flex min-h-0 flex-1 flex-col overflow-hidden" @submit.prevent="save">
        <div class="min-h-0 flex-1 overflow-y-auto overflow-x-hidden p-5 sm:p-6 space-y-4">
          <fieldset
            v-if="!isUpstreamLocked"
            class="min-w-0 max-w-full space-y-2 rounded-md border border-[#E4E4E7] p-3 sm:p-4"
          >
            <legend class="px-1 text-sm font-semibold text-[#09090B]">Target upstream</legend>
            <Field>
              <FieldLabel>Upstream service</FieldLabel>
              <Select
                :items="upstreamSelectItems"
                :value="selectedUpstream"
                :disabled="Boolean(editingId || promotingCandidateId)"
                @value-change="(value) => { if (value) handleUpstreamChange(String(value)); }"
              >
                <SelectTrigger aria-label="Workflow upstream" class="min-h-11 min-w-0">
                  <SelectValue placeholder="Choose an upstream" />
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
              <FieldDescription v-if="loadingContext">
                Loading upstream tools and current definition…
              </FieldDescription>
            </Field>
          </fieldset>

          <fieldset class="min-w-0 max-w-full space-y-3 rounded-md border border-[#E4E4E7] p-3 sm:p-4">
            <legend class="px-1 text-sm font-semibold text-[#09090B]">Name the activity</legend>
            <Field>
              <FieldLabel for="workflow-name">Workflow name</FieldLabel>
              <Input
                id="workflow-name"
                v-model="form.name"
                autocomplete="off"
                required
                @input="!editingId && (form.id = cleanId(form.name))"
              />
              <FieldDescription>
                Stable ID: {{ editingId || form.id || "generated from name" }}
              </FieldDescription>
            </Field>
            <Field>
              <FieldLabel for="workflow-description">Business activity</FieldLabel>
              <Textarea id="workflow-description" v-model="form.description" required />
              <FieldDescription>Describe the business result in everyday terms.</FieldDescription>
            </Field>
          </fieldset>

          <fieldset class="min-w-0 max-w-full space-y-3 rounded-md border border-[#E4E4E7] p-3 sm:p-4">
            <legend class="px-1 text-sm font-semibold text-[#09090B]">Participating tools</legend>
            <p class="text-xs leading-5 text-[#52525B]">
              Select tools that may be attributed to this workflow. Their display order is only for
              administrator organization.
            </p>
            <div class="max-h-52 divide-y divide-[#E4E4E7] overflow-y-auto overflow-x-hidden">
              <label
                v-for="tool in activeTools"
                :key="tool"
                class="flex min-h-11 min-w-0 items-center gap-3 break-all py-2 text-xs text-[#09090B] focus-within:outline-2 focus-within:outline-offset-2 focus-within:outline-[#0F766E]"
              >
                <Checkbox
                  :checked="form.tools.includes(tool)"
                  :aria-label="`Participating tool ${tool}`"
                  @checked-change="() => toggleTool(tool)"
                >
                  <CheckboxIndicator><AppIcon name="check" :size="13" /></CheckboxIndicator>
                </Checkbox>
                <span class="min-w-0 break-all">{{ tool }}</span>
              </label>
            </div>
            <p v-if="!activeTools.length" class="text-xs text-[#71717A]">
              No tools are available. Refresh the upstream catalog before creating a definition.
            </p>
          </fieldset>

          <fieldset class="min-w-0 max-w-full space-y-3 rounded-md border border-[#E4E4E7] p-3 sm:p-4">
            <legend class="px-1 text-sm font-semibold text-[#09090B]">
              Required completion evidence
            </legend>
            <p class="text-xs leading-5 text-[#52525B]">
              Each selected tool call must succeed at least once before this workflow can count.
              Result matching is optional.
            </p>
            <div v-if="form.tools.length" class="divide-y divide-[#E4E4E7]">
              <div v-for="tool in form.tools" :key="tool" class="py-3 first:pt-0 last:pb-0">
                <label
                  class="flex min-h-11 min-w-0 items-center gap-3 break-all text-xs text-[#09090B] focus-within:outline-2 focus-within:outline-offset-2 focus-within:outline-[#0F766E]"
                >
                  <Checkbox
                    :checked="form.requiredTools.includes(tool)"
                    :aria-label="`Required evidence tool ${tool}`"
                    @checked-change="toggleRequiredEvidence(tool, $event === true)"
                  >
                    <CheckboxIndicator><AppIcon name="check" :size="13" /></CheckboxIndicator>
                  </Checkbox>
                  <span class="min-w-0 break-all">Require successful call: {{ tool }}</span>
                </label>
                <div v-if="form.requiredTools.includes(tool)" class="mt-2 space-y-2 pl-8">
                  <Field>
                    <FieldLabel :for="`minimum-calls-${cleanId(tool)}`">
                      Minimum successful calls
                    </FieldLabel>
                    <Input
                      :id="`minimum-calls-${cleanId(tool)}`"
                      :model-value="String(form.evidenceMinCalls[tool] ?? 1)"
                      :aria-label="`Minimum successful calls for ${tool}`"
                      type="number"
                      min="1"
                      max="10"
                      step="1"
                      @update:model-value="form.evidenceMinCalls[tool] = Number($event)"
                    />
                  </Field>
                  <label
                    class="flex min-h-11 items-center gap-3 text-xs text-[#09090B] focus-within:outline-2 focus-within:outline-offset-2 focus-within:outline-[#0F766E]"
                  >
                    <Checkbox
                      :checked="Boolean(form.evidencePredicates[tool])"
                      :aria-label="`Require result predicate for ${tool}`"
                      @checked-change="toggleEvidencePredicate(tool, $event === true)"
                    >
                      <CheckboxIndicator><AppIcon name="check" :size="13" /></CheckboxIndicator>
                    </Checkbox>
                    <span>Also require a matching result</span>
                  </label>
                  <div
                    v-if="form.evidencePredicates[tool]"
                    class="grid grid-cols-1 gap-2 sm:grid-cols-2"
                  >
                    <Field class="min-w-0">
                      <FieldLabel :for="`evidence-path-${cleanId(tool)}`">
                        Result field (JSON pointer)
                      </FieldLabel>
                      <Input
                        :id="`evidence-path-${cleanId(tool)}`"
                        v-model="form.evidencePredicates[tool].path"
                        :aria-label="`Required evidence result JSON pointer for ${tool}`"
                        placeholder="/status"
                      />
                    </Field>
                    <Field class="min-w-0">
                      <FieldLabel>Expected value type</FieldLabel>
                      <Select
                        :value="form.evidencePredicates[tool].type"
                        @value-change="(value) => { if (value && form.evidencePredicates[tool]) form.evidencePredicates[tool].type = String(value); }"
                      >
                        <SelectTrigger
                          :aria-label="`Required evidence result type for ${tool}`"
                          class="min-h-11 min-w-0"
                        >
                          <SelectValue />
                          <AppIcon name="chevron-down" :size="14" />
                        </SelectTrigger>
                        <SelectContent>
                          <SelectItem
                            v-for="predicate in [
                              'non_empty_string',
                              'string',
                              'boolean',
                              'object',
                              'non_empty_object',
                              'array',
                              'non_empty_array',
                              'number',
                              'integer',
                              'null',
                            ]"
                            :key="predicate"
                            :value="predicate"
                          >
                            {{ predicate.replaceAll("_", " ") }}
                            <SelectItemIndicator>
                              <AppIcon name="check" :size="14" />
                            </SelectItemIndicator>
                          </SelectItem>
                        </SelectContent>
                      </Select>
                    </Field>
                  </div>
                </div>
              </div>
            </div>
            <p v-else class="text-xs text-[#71717A]">Choose participating tools first.</p>
          </fieldset>

          <fieldset class="min-w-0 max-w-full space-y-3 rounded-md border border-[#E4E4E7] p-3 sm:p-4">
            <legend class="px-1 text-sm font-semibold text-[#09090B]">Terminal completion</legend>
            <p class="text-xs leading-5 text-[#52525B]">
              The gateway checks the result of a call you select here; it does not call the tool or
              sequence the activity for you.
            </p>
            <div class="grid grid-cols-1 gap-3 sm:grid-cols-2">
              <Field class="min-w-0">
                <FieldLabel>Tool proving completion</FieldLabel>
                <Select
                  :value="form.completionTool"
                  @value-change="(value) => { form.completionTool = String(value ?? ''); }"
                >
                  <SelectTrigger aria-label="Tool proving completion" class="min-h-11 min-w-0">
                    <SelectValue placeholder="Choose a tool" />
                    <AppIcon name="chevron-down" :size="14" />
                  </SelectTrigger>
                  <SelectContent>
                    <SelectItem v-for="tool in form.tools" :key="tool" :value="tool">
                      {{ tool }}
                      <SelectItemIndicator><AppIcon name="check" :size="14" /></SelectItemIndicator>
                    </SelectItem>
                  </SelectContent>
                </Select>
              </Field>
              <Field class="min-w-0">
                <FieldLabel for="completion-path">Result JSON pointer</FieldLabel>
                <Input id="completion-path" v-model="form.completionPath" placeholder="/status" />
                <FieldDescription>
                  Example: /status checks a top-level status value.
                </FieldDescription>
              </Field>
              <Field class="min-w-0">
                <FieldLabel>Expected result type</FieldLabel>
                <Select
                  :value="form.completionType"
                  @value-change="(value) => { if (value) form.completionType = String(value); }"
                >
                  <SelectTrigger
                    aria-label="Expected completion result type"
                    class="min-h-11 min-w-0"
                  >
                    <SelectValue />
                    <AppIcon name="chevron-down" :size="14" />
                  </SelectTrigger>
                  <SelectContent>
                    <SelectItem
                      v-for="predicate in [
                        'non_empty_string',
                        'string',
                        'boolean',
                        'object',
                        'non_empty_object',
                        'array',
                        'non_empty_array',
                        'number',
                        'integer',
                        'null',
                      ]"
                      :key="predicate"
                      :value="predicate"
                    >
                      {{ predicate.replaceAll("_", " ") }}
                      <SelectItemIndicator><AppIcon name="check" :size="14" /></SelectItemIndicator>
                    </SelectItem>
                  </SelectContent>
                </Select>
              </Field>
            </div>
          </fieldset>

          <fieldset class="min-w-0 max-w-full space-y-3 rounded-md border border-[#E4E4E7] p-3 sm:p-4">
            <legend class="px-1 text-sm font-semibold text-[#09090B]">Human baseline</legend>
            <p class="text-xs leading-5 text-[#52525B]">
              Enter the time this activity takes without the gateway. This baseline appears in
              Business Value; estimates remain marked as unvalidated.
            </p>
            <div class="grid grid-cols-1 gap-3 sm:grid-cols-2">
              <Field class="min-w-0">
                <FieldLabel for="baseline-minutes">Manual workflow time (minutes)</FieldLabel>
                <Input
                  id="baseline-minutes"
                  v-model="form.minutes"
                  type="number"
                  min="0.01"
                  step="0.01"
                  required
                  placeholder="For example, 8"
                />
              </Field>
              <Field class="min-w-0">
                <FieldLabel>Evidence source</FieldLabel>
                <Select
                  :value="form.source"
                  @value-change="(value) => { if (value) form.source = String(value); }"
                >
                  <SelectTrigger aria-label="Baseline evidence source" class="min-h-11 min-w-0">
                    <SelectValue />
                    <AppIcon name="chevron-down" :size="14" />
                  </SelectTrigger>
                  <SelectContent>
                    <SelectItem value="administrator_estimate">Administrator estimate</SelectItem>
                    <SelectItem value="sampled_manual_measurement">
                      Sampled manual measurement
                    </SelectItem>
                    <SelectItem value="validated_external_measurement">
                      Validated external measurement
                    </SelectItem>
                  </SelectContent>
                </Select>
                <FieldDescription>
                  {{
                    form.source === "administrator_estimate"
                      ? "Reported as an unvalidated estimate."
                      : form.source === "sampled_manual_measurement"
                        ? "Reported as a sampled measurement."
                        : "Reported as a validated external measurement."
                  }}
                </FieldDescription>
              </Field>
              <Field class="min-w-0">
                <FieldLabel for="baseline-effective">Effective date</FieldLabel>
                <Input id="baseline-effective" v-model="form.effectiveFrom" type="date" required />
              </Field>
              <Field class="min-w-0">
                <FieldLabel for="baseline-hourly">
                  Loaded labor rate ({{ activeCurrency }} / hr, optional)
                </FieldLabel>
                <Input
                  id="baseline-hourly"
                  v-model="form.loadedHourlyCost"
                  type="number"
                  min="0"
                  step="0.5"
                  placeholder="e.g. 95"
                />
              </Field>
              <Field class="min-w-0">
                <FieldLabel for="baseline-review">Expected review minutes (optional)</FieldLabel>
                <Input
                  id="baseline-review"
                  v-model="form.reviewMinutes"
                  type="number"
                  min="0"
                  max="1440"
                  step="0.1"
                  placeholder="e.g. 1.0"
                />
              </Field>
              <Field class="min-w-0">
                <FieldLabel for="baseline-correction">
                  Expected correction minutes (optional)
                </FieldLabel>
                <Input
                  id="baseline-correction"
                  v-model="form.correctionMinutes"
                  type="number"
                  min="0"
                  max="1440"
                  step="0.1"
                  placeholder="e.g. 0.5"
                />
              </Field>
              <div class="space-y-2 rounded border border-[#E4E4E7] bg-[#FAFAFA] p-2.5 sm:col-span-2">
                <div class="flex flex-wrap items-center justify-between gap-2">
                  <span class="text-xs font-medium text-[#09090B]">
                    Regional rate overrides ({{ activeCurrency }} / hr, optional)
                  </span>
                  <Button
                    type="button"
                    class="min-h-8 text-xs"
                    @click="form.regionalRates.push({ region: '', rate: '' })"
                  >
                    + Add region rate
                  </Button>
                </div>
                <p class="text-[11px] text-[#71717A]">
                  Match caller country/region JWT claims (e.g. US, EU, LATAM, BR) while reporting in {{ activeCurrency }}.
                </p>
                <div
                  v-for="(row, rIdx) in form.regionalRates"
                  :key="rIdx"
                  class="flex flex-wrap items-center gap-2"
                >
                  <Input
                    v-model="row.region"
                    placeholder="Region (e.g. BR, US)"
                    aria-label="Region code"
                    class="w-36 font-mono-data uppercase"
                  />
                  <Input
                    v-model="row.rate"
                    type="number"
                    min="0"
                    step="0.5"
                    :placeholder="`Rate (${activeCurrency}/hr)`"
                    aria-label="Regional hourly rate"
                    class="w-36"
                  />
                  <Button
                    type="button"
                    class="min-h-9 text-xs"
                    @click="form.regionalRates.splice(rIdx, 1)"
                  >
                    Remove
                  </Button>
                </div>
              </div>
              <Field class="min-w-0 sm:col-span-2">
                <FieldLabel for="baseline-note">Evidence note (optional)</FieldLabel>
                <Textarea
                  id="baseline-note"
                  v-model="form.evidenceNote"
                  maxlength="1000"
                  placeholder="Where the measurement came from or what it includes"
                />
              </Field>
            </div>
          </fieldset>

          <div
            v-if="error"
            role="alert"
            class="space-y-1 rounded-md border border-[#FECACA] bg-[#FEF2F2] p-3 text-sm text-[#991B1B]"
          >
            <p class="font-semibold">Could not publish this workflow definition</p>
            <p>{{ error }}</p>
            <p class="text-xs">
              Your entries are still in this form. Correct the fields above and try again.
            </p>
          </div>
        </div>

        <div
          class="shrink-0 flex flex-col-reverse gap-2 border-t border-[#E4E4E7] bg-white px-5 py-3.5 sm:flex-row sm:items-center sm:justify-between sm:px-6"
        >
          <p class="text-xs leading-5 text-[#52525B]">
            The gateway validates, saves a draft, then publishes. A failed step leaves this form
            open.
          </p>
          <div class="flex justify-end gap-2">
            <Button type="button" class="min-h-11" :disabled="saving" @click="close">Cancel</Button>
            <Button
              type="submit"
              data-variant="primary"
              class="min-h-11"
              :disabled="saving"
              pending-announcement="Validating and publishing workflow"
            >
              {{
                saving
                  ? submissionStep === "validate"
                    ? "Validating…"
                    : submissionStep === "save-draft"
                      ? "Saving draft…"
                      : "Publishing…"
                  : "Validate and publish"
              }}
            </Button>
          </div>
        </div>
      </form>
    </DrawerContent>
  </Drawer>
</template>
