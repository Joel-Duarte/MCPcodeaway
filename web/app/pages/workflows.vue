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
import { Switch, SwitchThumb } from "@ui-sdk/vue/switch";
import { Tabs, TabsList, TabsPanel, TabsTab } from "@ui-sdk/vue/tabs";
import { Textarea } from "@ui-sdk/vue/textarea";
import { computed, onMounted, ref } from "vue";
import BusinessValueReportSection, {
  type BusinessValueReport,
} from "~/components/BusinessValueReportSection.vue";
import PromptBundleDrawer from "~/components/PromptBundleDrawer.vue";
import WorkflowEditorDrawer from "~/components/WorkflowEditorDrawer.vue";
import { extractErrorMessage } from "~/composables/useAdminApi";

interface WorkflowCatalogEntry {
  upstream_name: string;
  definition_revision: number | null;
  workflow: Record<string, any>;
}

interface DiscoveredCandidate {
  candidate_id: string;
  upstream_name: string;
  workflow_slug: string;
  fingerprint?: string;
  origin: string;
  status: string;
  observed_count: number;
  verified_count: number;
  sample_intents?: string[];
  draft_definition?: Record<string, any>;
  updated_at?: string;
}

interface DetectedEpisode {
  episode_id: string;
  upstream_name: string;
  workflow_id?: string | null;
  matched_workflow_id?: string | null;
  status: string;
  outcome_status?: string | null;
  completion_tier?: string | null;
  confidence?: number | null;
  step_count?: number;
  manual_baseline_minutes?: number | null;
  updated_at?: string;
}

const { apiFetch } = useAdminApi();

const loading = ref(true);
const loadError = ref("");
const statusMessage = ref("");
const actionError = ref("");

const upstreams = ref<string[]>([]);
const workflows = ref<WorkflowCatalogEntry[]>([]);
const candidates = ref<DiscoveredCandidate[]>([]);
const episodes = ref<DetectedEpisode[]>([]);

const bvReport = ref<BusinessValueReport | null>(null);
const bvLoading = ref(false);
const bvError = ref("");
const bvFetchedAt = ref<Date | null>(null);

const routeObj = typeof useRoute === "function" ? useRoute() : null;
const routeUpstream = routeObj?.query?.upstream;
const routeTab = routeObj?.query?.tab;
const initialUpstreamFilter =
  typeof routeUpstream === "string" && routeUpstream.trim() ? routeUpstream.trim() : "__all__";
const activeTab = ref<"workflows" | "ledger">(
  routeTab === "ledger" ? "ledger" : "workflows",
);

const upstreamFilter = ref(initialUpstreamFilter);
const searchQuery = ref("");
const discoverBusy = ref(false);
const dismissSchemaBusy = ref(false);
const togglingKey = ref("");

const editDrawerOpen = ref(false);
const editTarget = ref<WorkflowCatalogEntry | null>(null);
const editSaving = ref(false);
const editDrawerError = ref("");
const editForm = ref({
  name: "",
  description: "",
  manual_baseline_minutes: 10,
  baseline_source: "administrator_estimate",
  confidence: "unvalidated",
  evidence_note: "",
  intent_examples_text: "",
  enabled: true,
});

const promoteDrawerOpen = ref(false);
const promoteTarget = ref<DiscoveredCandidate | null>(null);
const promoteSaving = ref(false);
const promoteDrawerError = ref("");
const promoteForm = ref({
  manual_baseline_minutes: 10,
  baseline_source: "administrator_estimate",
  effective_from: new Date().toISOString().slice(0, 10),
  evidence_note: "Confirmed from discovered workflow candidate.",
});

const createWorkflowDrawerOpen = ref(false);
const promptDrawerOpen = ref(false);

const deleteDialogOpen = ref(false);
const deleteTarget = ref<WorkflowCatalogEntry | null>(null);
const deleteBusy = ref(false);

const upstreamSelectItems = computed(() => [
  { value: "__all__", label: "All upstreams" },
  ...upstreams.value.map((name) => ({ value: name, label: name })),
]);

const baselineSourceOptions = [
  { value: "administrator_estimate", label: "Administrator estimate" },
  { value: "sampled_manual_measurement", label: "Sampled manual measurement" },
  { value: "validated_external_measurement", label: "Validated external measurement" },
];

const confidenceOptions = [
  { value: "unvalidated", label: "Unvalidated" },
  { value: "sampled", label: "Sampled" },
  { value: "validated", label: "Validated" },
];

const filteredWorkflows = computed(() => {
  const q = searchQuery.value.trim().toLowerCase();
  return workflows.value.filter((entry) => {
    if (upstreamFilter.value !== "__all__" && entry.upstream_name !== upstreamFilter.value) {
      return false;
    }
    if (!q) return true;
    const wf = entry.workflow ?? {};
    return (
      String(wf.name ?? "").toLowerCase().includes(q) ||
      String(wf.id ?? "").toLowerCase().includes(q) ||
      entry.upstream_name.toLowerCase().includes(q)
    );
  });
});

const pendingCandidates = computed(() => {
  const q = searchQuery.value.trim().toLowerCase();
  return candidates.value.filter((cand) => {
    if (cand.status !== "pending_review") return false;
    if (upstreamFilter.value !== "__all__" && cand.upstream_name !== upstreamFilter.value) {
      return false;
    }
    if (!q) return true;
    return (
      String(cand.draft_definition?.name ?? cand.workflow_slug).toLowerCase().includes(q) ||
      cand.upstream_name.toLowerCase().includes(q)
    );
  });
});

const filteredEpisodes = computed(() => {
  if (upstreamFilter.value === "__all__") return episodes.value;
  return episodes.value.filter((ep) => ep.upstream_name === upstreamFilter.value);
});

const activeCount = computed(
  () => filteredWorkflows.value.filter((entry) => entry.workflow?.enabled !== false).length,
);
const inactiveCount = computed(
  () => filteredWorkflows.value.filter((entry) => entry.workflow?.enabled === false).length,
);
const verifiedOutcomeCount = computed(
  () =>
    filteredEpisodes.value.filter(
      (ep) => ep.status === "completed_verified" || ep.outcome_status === "verified",
    ).length,
);

function workflowConfidence(entry: WorkflowCatalogEntry): "validated" | "sampled" | "unvalidated" {
  const conf = String(entry.workflow?.business_value?.confidence ?? "unvalidated").toLowerCase();
  if (conf === "validated" || conf === "sampled") return conf;
  return "unvalidated";
}

function confidenceBadgeLabel(conf: string): string {
  if (conf === "validated") return "Validated baseline";
  if (conf === "sampled") return "Sampled baseline";
  return "Unvalidated estimate";
}

function confidenceBadgeClass(conf: string): string {
  if (conf === "validated") return "border-[#A7F3D0] bg-[#ECFDF5] text-[#059669]";
  if (conf === "sampled") return "border-[#FDE68A] bg-[#FFFBEB] text-[#B45309]";
  return "border-[#E4E4E7] bg-[#F4F4F5] text-[#52525B]";
}

const validationMix = computed(() => {
  let validated = 0;
  let sampled = 0;
  let unvalidated = 0;
  for (const entry of filteredWorkflows.value) {
    const c = workflowConfidence(entry);
    if (c === "validated") validated += 1;
    else if (c === "sampled") sampled += 1;
    else unvalidated += 1;
  }
  return { validated, sampled, unvalidated };
});

const filteredReportRows = computed(() => {
  const rows = bvReport.value?.by_integration_workflow_day ?? [];
  if (upstreamFilter.value === "__all__") return rows;
  return rows.filter((row) => row.upstream_name === upstreamFilter.value);
});

const roiSummary = computed(() => {
  let completedOutcomes = 0;
  let totalMinutesSaved = 0;
  let hasSavings = false;
  let validatedMinutes = 0;
  let grossMinutes = 0;
  const currencyTotals: Record<string, number> = {};
  for (const row of filteredReportRows.value) {
    completedOutcomes += Number(row.completed_count ?? 0);
    grossMinutes += Number(row.estimated_manual_baseline_minutes ?? 0);
    if (row.baseline_confidence === "validated") {
      validatedMinutes += Number(
        row.estimated_minutes_saved ?? row.validated_manual_baseline_minutes ?? 0,
      );
    }
    if (typeof row.estimated_minutes_saved === "number") {
      totalMinutesSaved += row.estimated_minutes_saved;
      hasSavings = true;
    }
    if (row.estimated_labor_value && typeof row.estimated_labor_value.amount === "number") {
      const cur = row.estimated_labor_value.currency || "USD";
      currencyTotals[cur] = (currencyTotals[cur] ?? 0) + row.estimated_labor_value.amount;
    }
  }
  const currencies = Object.entries(currencyTotals);
  const primaryValue = currencies.length
    ? currencies
        .map(
          ([cur, amt]) =>
            `${cur} ${amt.toLocaleString("en-US", { minimumFractionDigits: 2, maximumFractionDigits: 2 })}`,
        )
        .join(" + ")
    : null;
  const effectiveSaved = hasSavings ? totalMinutesSaved : grossMinutes;
  return {
    completedOutcomes:
      upstreamFilter.value === "__all__" && bvReport.value
        ? Math.max(bvReport.value.completed_count ?? 0, completedOutcomes)
        : completedOutcomes,
    effectiveSavedMinutes: effectiveSaved,
    hasNetSavings: hasSavings,
    validatedMinutes,
    grossMinutes,
    primaryValue,
  };
});

function formatDurationMinutes(minutes: number): string {
  const rounded = Math.round(minutes * 10) / 10;
  if (rounded >= 60) {
    const hrs = Math.floor(rounded / 60);
    const rem = Math.round(rounded % 60);
    return rem ? `${hrs}h ${rem}m` : `${hrs}h`;
  }
  return `${rounded.toLocaleString("en-US", { maximumFractionDigits: 1 })} min`;
}

function workflowEvidenceSummary(entry: WorkflowCatalogEntry) {
  const qualifiedId = `${entry.upstream_name}/${entry.workflow?.id}`;
  const matchedEpisodes = episodes.value.filter(
    (ep) =>
      ep.matched_workflow_id === qualifiedId ||
      (ep.upstream_name === entry.upstream_name && ep.workflow_id === entry.workflow?.id),
  );
  const verifiedRuns = matchedEpisodes.filter(
    (ep) => ep.status === "completed_verified" || ep.outcome_status === "verified",
  ).length;
  const matchedRows = (bvReport.value?.by_integration_workflow_day ?? []).filter(
    (row) => row.selected_id === qualifiedId,
  );
  const completions = matchedRows.reduce((s, r) => s + Number(r.completed_count ?? 0), 0);
  const savedMinutes = matchedRows.reduce(
    (s, r) =>
      s + Number(r.estimated_minutes_saved ?? r.estimated_manual_baseline_minutes ?? 0),
    0,
  );
  let valueText: string | null = null;
  const firstVal = matchedRows.find((r) => r.estimated_labor_value?.amount != null);
  if (firstVal?.estimated_labor_value) {
    const cur = firstVal.estimated_labor_value.currency || "USD";
    const totalAmt = matchedRows.reduce(
      (s, r) => s + Number(r.estimated_labor_value?.amount ?? 0),
      0,
    );
    valueText = `${cur} ${totalAmt.toLocaleString("en-US", {
      minimumFractionDigits: 2,
      maximumFractionDigits: 2,
    })}`;
  }
  return {
    verifiedRuns,
    completions,
    savedMinutes,
    valueText,
    hasEvidence: verifiedRuns > 0 || completions > 0,
  };
}

async function loadCatalog() {
  loading.value = true;
  bvLoading.value = true;
  loadError.value = "";
  bvError.value = "";
  const [catResult, bvResult] = await Promise.allSettled([
    apiFetch<{
      upstreams?: string[];
      workflows?: WorkflowCatalogEntry[];
      candidates?: DiscoveredCandidate[];
      episodes?: DetectedEpisode[];
    }>("/admin/workflows/catalog"),
    apiFetch<BusinessValueReport>("/admin/business-value/report"),
  ]);
  if (catResult.status === "fulfilled") {
    upstreams.value = catResult.value.upstreams ?? [];
    workflows.value = catResult.value.workflows ?? [];
    candidates.value = catResult.value.candidates ?? [];
    episodes.value = catResult.value.episodes ?? [];
  } else {
    loadError.value = extractErrorMessage(catResult.reason);
  }
  if (bvResult.status === "fulfilled") {
    bvReport.value = bvResult.value;
    bvFetchedAt.value = new Date();
  } else {
    bvReport.value = null;
    bvError.value = "";
  }
  loading.value = false;
  bvLoading.value = false;
}

onMounted(() => {
  void loadCatalog();
});

async function runDiscoverForSelection() {
  actionError.value = "";
  statusMessage.value = "";
  const targets =
    upstreamFilter.value !== "__all__" ? [upstreamFilter.value] : [...upstreams.value];
  if (!targets.length) {
    actionError.value = "Onboard at least one upstream before running workflow discovery.";
    return;
  }
  discoverBusy.value = true;
  let totalDiscovered = 0;
  try {
    for (const target of targets) {
      const res = await apiFetch<{ discovered_count?: number }>(
        `/admin/integration-definitions/${encodeURIComponent(target)}/discover`,
        { method: "POST", body: {} },
      );
      totalDiscovered += Number(res.discovered_count ?? 0);
    }
    statusMessage.value = `Discovery checked ${targets.length} upstream(s) and surfaced ${totalDiscovered} candidate(s).`;
    await loadCatalog();
  } catch (err) {
    actionError.value = extractErrorMessage(err);
  } finally {
    discoverBusy.value = false;
  }
}

async function toggleWorkflowEnabled(entry: WorkflowCatalogEntry, enabled: boolean) {
  const wf = entry.workflow;
  const key = `${entry.upstream_name}:${wf.id}`;
  togglingKey.value = key;
  actionError.value = "";
  try {
    await apiFetch(
      `/admin/integration-definitions/${encodeURIComponent(entry.upstream_name)}/workflows/${encodeURIComponent(String(wf.id))}`,
      {
        method: "PATCH",
        body: { enabled },
      },
    );
    wf.enabled = enabled;
    statusMessage.value = `${enabled ? "Activated" : "Paused"} workflow '${wf.name}'.`;
  } catch (err) {
    actionError.value = extractErrorMessage(err);
  } finally {
    togglingKey.value = "";
  }
}

function openEditDrawer(entry: WorkflowCatalogEntry) {
  const wf = entry.workflow;
  const bv = wf.business_value ?? {};
  editDrawerError.value = "";
  editTarget.value = entry;
  editForm.value = {
    name: String(wf.name ?? ""),
    description: String(wf.description ?? ""),
    manual_baseline_minutes: Number(bv.manual_baseline_minutes ?? 10),
    baseline_source: String(bv.baseline_source ?? "administrator_estimate"),
    confidence: String(bv.confidence ?? "unvalidated"),
    evidence_note: String(bv.evidence_note ?? ""),
    intent_examples_text: Array.isArray(wf.intent_examples)
      ? wf.intent_examples.join("\n")
      : "",
    enabled: wf.enabled !== false,
  };
  editDrawerOpen.value = true;
}

function onEditConfidenceChange(val: string) {
  editForm.value.confidence = val;
  if (val === "validated" && editForm.value.baseline_source === "administrator_estimate") {
    editForm.value.baseline_source = "validated_external_measurement";
  } else if (val === "sampled" && editForm.value.baseline_source === "administrator_estimate") {
    editForm.value.baseline_source = "sampled_manual_measurement";
  } else if (val === "unvalidated") {
    editForm.value.baseline_source = "administrator_estimate";
  }
}

function onEditBaselineSourceChange(val: string) {
  editForm.value.baseline_source = val;
  if (val === "administrator_estimate") {
    editForm.value.confidence = "unvalidated";
  } else if (val === "sampled_manual_measurement" && editForm.value.confidence === "unvalidated") {
    editForm.value.confidence = "sampled";
  } else if (val === "validated_external_measurement") {
    editForm.value.confidence = "validated";
  }
}

async function saveWorkflowEdit() {
  if (!editTarget.value) return;
  const target = editTarget.value;
  editSaving.value = true;
  editDrawerError.value = "";
  actionError.value = "";
  try {
    const minutes = Number(editForm.value.manual_baseline_minutes);
    if (!Number.isFinite(minutes) || minutes <= 0) {
      throw new Error("Manual baseline minutes must be greater than zero.");
    }
    const intentExamples = editForm.value.intent_examples_text
      .split("\n")
      .map((line) => line.trim())
      .filter(Boolean);
    await apiFetch(
      `/admin/integration-definitions/${encodeURIComponent(target.upstream_name)}/workflows/${encodeURIComponent(String(target.workflow.id))}`,
      {
        method: "PATCH",
        body: {
          name: editForm.value.name.trim(),
          description: editForm.value.description.trim(),
          manual_baseline_minutes: minutes,
          baseline_source: editForm.value.baseline_source,
          confidence: editForm.value.confidence,
          evidence_note: editForm.value.evidence_note.trim() || null,
          intent_examples: intentExamples,
          enabled: editForm.value.enabled,
        },
      },
    );
    editDrawerOpen.value = false;
    statusMessage.value = `Updated workflow '${editForm.value.name}'.`;
    await loadCatalog();
  } catch (err) {
    editDrawerError.value = extractErrorMessage(err);
  } finally {
    editSaving.value = false;
  }
}

function confirmDeleteWorkflow(entry: WorkflowCatalogEntry) {
  deleteTarget.value = entry;
  deleteDialogOpen.value = true;
}

async function performDeleteWorkflow() {
  if (!deleteTarget.value) return;
  const target = deleteTarget.value;
  deleteBusy.value = true;
  actionError.value = "";
  try {
    await apiFetch(
      `/admin/integration-definitions/${encodeURIComponent(target.upstream_name)}/workflows/${encodeURIComponent(String(target.workflow.id))}`,
      { method: "DELETE" },
    );
    deleteDialogOpen.value = false;
    statusMessage.value = `Deleted workflow '${target.workflow.name}' from '${target.upstream_name}'.`;
    await loadCatalog();
  } catch (err) {
    actionError.value = extractErrorMessage(err);
  } finally {
    deleteBusy.value = false;
  }
}

function openPromoteDrawer(cand: DiscoveredCandidate) {
  promoteDrawerError.value = "";
  promoteTarget.value = cand;
  promoteForm.value = {
    manual_baseline_minutes: 10,
    baseline_source: "administrator_estimate",
    effective_from: new Date().toISOString().slice(0, 10),
    evidence_note: "Activated from auto-discovered workflow candidate.",
  };
  promoteDrawerOpen.value = true;
}

async function confirmPromoteCandidate() {
  if (!promoteTarget.value) return;
  promoteSaving.value = true;
  promoteDrawerError.value = "";
  actionError.value = "";
  try {
    const minutes = Number(promoteForm.value.manual_baseline_minutes);
    if (!Number.isFinite(minutes) || minutes <= 0) {
      throw new Error("Manual baseline minutes must be greater than zero.");
    }
    const confidence =
      promoteForm.value.baseline_source === "validated_external_measurement"
        ? "validated"
        : promoteForm.value.baseline_source === "sampled_manual_measurement"
          ? "sampled"
          : "unvalidated";
    await apiFetch(
      `/admin/workflows/discovered/${encodeURIComponent(promoteTarget.value.candidate_id)}/promote`,
      {
        method: "POST",
        body: {
          manual_baseline_minutes: minutes,
          baseline_source: promoteForm.value.baseline_source,
          confidence,
          effective_from: promoteForm.value.effective_from,
          evidence_note: promoteForm.value.evidence_note.trim() || undefined,
          publish: true,
        },
      },
    );
    promoteDrawerOpen.value = false;
    statusMessage.value = `Activated workflow '${promoteTarget.value.draft_definition?.name || promoteTarget.value.workflow_slug}'.`;
    await loadCatalog();
  } catch (err) {
    promoteDrawerError.value = extractErrorMessage(err);
  } finally {
    promoteSaving.value = false;
  }
}

async function handleCreatedWorkflowSaved(payload: { statusMessage: string }) {
  statusMessage.value = payload.statusMessage;
  await loadCatalog();
}

async function handleBundleApplied(upstreamName: string) {
  statusMessage.value = `Applied and published workflow bundle${upstreamName ? ` for '${upstreamName}'` : ""}.`;
  await loadCatalog();
}

async function dismissCandidate(cand: DiscoveredCandidate) {
  actionError.value = "";
  try {
    await apiFetch(
      `/admin/workflows/discovered/${encodeURIComponent(cand.candidate_id)}/dismiss`,
      { method: "POST" },
    );
    candidates.value = candidates.value.filter((item) => item.candidate_id !== cand.candidate_id);
    statusMessage.value = `Dismissed candidate '${cand.draft_definition?.name || cand.workflow_slug}'.`;
  } catch (err) {
    actionError.value = extractErrorMessage(err);
  }
}

async function dismissAllSchemaSuggestions() {
  actionError.value = "";
  dismissSchemaBusy.value = true;
  try {
    const body =
      upstreamFilter.value !== "__all__" ? { upstream_name: upstreamFilter.value } : {};
    const res = await apiFetch<{ dismissed_count?: number }>(
      "/admin/workflows/discovered/dismiss-schema",
      { method: "POST", body },
    );
    statusMessage.value = `Dismissed ${res.dismissed_count ?? 0} unverified schema suggestion(s).`;
    await loadCatalog();
  } catch (err) {
    actionError.value = extractErrorMessage(err);
  } finally {
    dismissSchemaBusy.value = false;
  }
}
</script>

<template>
  <div class="space-y-6">
    <div class="flex flex-col gap-3 lg:flex-row lg:items-center lg:justify-between">
      <div class="min-w-0">
        <h1 class="text-[20px] font-semibold tracking-[-0.015em] text-[#09090B]">
          Workflows &amp; ROI
        </h1>
        <p class="mt-0.5 text-[13px] text-[#52525B]">
          Track time saved and labor value across automated processes, inspect completion evidence, toggle active workflows, and audit the outcome ledger.
        </p>
      </div>
      <div class="flex flex-wrap items-center gap-2 shrink-0">
        <Button class="min-h-11 whitespace-nowrap sm:min-h-9" :disabled="discoverBusy" @click="runDiscoverForSelection">
          {{ discoverBusy ? "Discovering…" : "Discover from Schema & Traffic" }}
        </Button>
        <Button class="min-h-11 whitespace-nowrap sm:min-h-9" @click="loadCatalog">
          <AppIcon name="refresh" :size="14" />
          <span>Refresh</span>
        </Button>
        <Button class="min-h-11 whitespace-nowrap sm:min-h-9" @click="promptDrawerOpen = true">
          Generate Workflows via Prompt
        </Button>
        <Button
          data-variant="primary"
          class="min-h-11 whitespace-nowrap sm:min-h-9"
          :disabled="!upstreams.length"
          @click="createWorkflowDrawerOpen = true"
        >
          Create Workflow
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
      <span>Failed to load workflows: {{ loadError }}</span>
      <Button class="min-h-11 sm:min-h-9" @click="loadCatalog">Retry</Button>
    </div>

    <section class="grid grid-cols-1 gap-3 rounded-lg border border-[#E4E4E7] bg-white p-4 sm:grid-cols-2 xl:grid-cols-4">
      <div>
        <div class="text-[12px] font-medium text-[#52525B]">Workflow time saved</div>
        <div class="mt-1 text-[20px] font-semibold tabular-nums text-[#0F766E]">
          {{
            roiSummary.effectiveSavedMinutes > 0
              ? formatDurationMinutes(roiSummary.effectiveSavedMinutes)
              : "0 min"
          }}
        </div>
        <p class="mt-0.5 text-[11px] tabular-nums text-[#71717A]">
          {{
            roiSummary.validatedMinutes > 0
              ? `${formatDurationMinutes(roiSummary.validatedMinutes)} validated savings`
              : `${formatDurationMinutes(roiSummary.grossMinutes)} gross baseline`
          }}
        </p>
      </div>
      <div>
        <div class="text-[12px] font-medium text-[#52525B]">Estimated value &amp; outcomes</div>
        <div class="mt-1 text-[20px] font-semibold tabular-nums text-[#09090B]">
          {{ roiSummary.primaryValue || `${roiSummary.completedOutcomes} completed` }}
        </div>
        <p class="mt-0.5 text-[11px] tabular-nums text-[#71717A]">
          {{ roiSummary.completedOutcomes }} attributed · {{ verifiedOutcomeCount }} verified episodes
        </p>
      </div>
      <div>
        <div class="text-[12px] font-medium text-[#52525B]">Active workflows</div>
        <div class="mt-1 text-[20px] font-semibold tabular-nums text-[#09090B]">
          {{ activeCount }}
          <span class="text-[13px] font-normal text-[#71717A]">/ {{ activeCount + inactiveCount }}</span>
        </div>
        <p class="mt-0.5 text-[11px] tabular-nums text-[#71717A]">
          {{ validationMix.validated }} validated · {{ validationMix.sampled }} sampled · {{ validationMix.unvalidated }} est.
        </p>
      </div>
      <div>
        <div class="text-[12px] font-medium text-[#52525B]">Candidates to review</div>
        <div class="mt-1 text-[20px] font-semibold tabular-nums text-[#09090B]">{{ pendingCandidates.length }}</div>
        <p class="mt-0.5 text-[11px] text-[#71717A]">
          {{ inactiveCount }} paused · auto-discovered awaiting baseline
        </p>
      </div>
    </section>

    <Tabs
      :value="activeTab"
      @value-change="(value) => { if (value === 'workflows' || value === 'ledger') activeTab = value; }"
    >
      <TabsList aria-label="Workflows and ROI views">
        <TabsTab value="workflows">
          Configured Workflows &amp; Evidence ({{ filteredWorkflows.length }})
        </TabsTab>
        <TabsTab value="ledger">
          Outcome &amp; ROI Ledger ({{ filteredReportRows.length }})
        </TabsTab>
      </TabsList>

      <TabsPanel value="workflows" class="mt-4 space-y-6">
        <section class="flex flex-col gap-3 rounded-lg border border-[#E4E4E7] bg-white p-4 sm:flex-row sm:items-end">
          <Field class="sm:w-64">
            <FieldLabel>Filter by upstream</FieldLabel>
            <Select
              :items="upstreamSelectItems"
              :value="upstreamFilter"
              @value-change="(value) => { if (value) upstreamFilter = String(value); }"
            >
              <SelectTrigger aria-label="Filter workflows by upstream" class="min-h-11 sm:min-h-9">
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
            <FieldLabel for="workflow-search">Search workflows</FieldLabel>
            <Input
              id="workflow-search"
              v-model="searchQuery"
              placeholder="Search by workflow name, slug, or upstream..."
            />
          </Field>
        </section>

        <section class="rounded-lg border border-[#E4E4E7] bg-white p-5">
          <div class="flex flex-wrap items-baseline justify-between gap-2 pb-3 border-b border-[#E4E4E7]">
            <div>
              <h2 class="text-[16px] font-semibold text-[#09090B]">Configured Workflows</h2>
              <p class="text-[12px] text-[#52525B]">
                Toggle workflows active or paused cleanly, check verified completion evidence and baseline status badges, or tune manual time baselines and triggers.
              </p>
            </div>
            <span class="text-[12px] tabular-nums text-[#71717A]">
              {{ filteredWorkflows.length }} total
            </span>
          </div>

          <div v-if="loading" class="py-6 text-[13px] text-[#52525B]" role="status">
            Loading workflow catalog…
          </div>
          <div v-else-if="!filteredWorkflows.length" class="py-6 text-[13px] text-[#52525B] space-y-3">
            <p>
              No workflows configured yet. Create one directly here, generate an agent bundle via
              prompt, or run discovery above.
            </p>
            <div class="flex flex-wrap items-center gap-2">
              <Button
                data-variant="primary"
                class="min-h-9 text-xs"
                :disabled="!upstreams.length"
                @click="createWorkflowDrawerOpen = true"
              >
                Create Workflow
              </Button>
              <Button class="min-h-9 text-xs" @click="promptDrawerOpen = true">
                Generate Workflows via Prompt
              </Button>
            </div>
          </div>
          <div v-else class="divide-y divide-[#E4E4E7]">
            <div
              v-for="entry in filteredWorkflows"
              :key="`${entry.upstream_name}:${entry.workflow.id}`"
              class="flex flex-col gap-3 py-4 lg:flex-row lg:items-center lg:justify-between"
            >
              <div class="min-w-0 flex-1 space-y-1.5">
                <div class="flex flex-wrap items-center gap-2">
                  <span class="text-[14px] font-semibold text-[#09090B]">
                    {{ entry.workflow.name }}
                  </span>
                  <span class="rounded border border-[#E4E4E7] bg-[#F4F4F5] px-1.5 py-0.5 font-mono-data text-[11px] text-[#52525B]">
                    {{ entry.upstream_name }}
                  </span>
                  <span
                    class="rounded px-1.5 py-0.5 text-[11px] font-medium"
                    :class="
                      entry.workflow.enabled !== false
                        ? 'border border-[#99F6E4] bg-[#F0FDFA] text-[#0F766E]'
                        : 'border border-[#E4E4E7] bg-[#F4F4F5] text-[#52525B]'
                    "
                  >
                    {{ entry.workflow.enabled !== false ? "Active" : "Paused" }}
                  </span>
                  <span
                    class="rounded border px-1.5 py-0.5 text-[11px] font-medium"
                    :class="confidenceBadgeClass(workflowConfidence(entry))"
                  >
                    {{ confidenceBadgeLabel(workflowConfidence(entry)) }}
                  </span>
                </div>
                <p class="text-[13px] text-[#52525B]">{{ entry.workflow.description }}</p>
                <div v-if="Array.isArray(entry.workflow.participating_tools) && entry.workflow.participating_tools.length" class="flex flex-wrap items-center gap-1 pt-0.5 text-[11px] font-mono-data text-[#09090B]">
                  <template v-for="(toolName, tIdx) in entry.workflow.participating_tools" :key="`${entry.workflow.id}:${toolName}`">
                    <span class="rounded border border-[#E4E4E7] bg-[#FAFAFA] px-1.5 py-0.5">
                      {{ String(toolName).replace(`${entry.upstream_name}__`, "") }}
                    </span>
                    <span v-if="Number(tIdx) < entry.workflow.participating_tools.length - 1" class="text-[#71717A]">→</span>
                  </template>
                </div>
                <div class="flex flex-wrap gap-x-5 gap-y-1 text-[12px] text-[#52525B]">
                  <span>
                    Baseline:
                    <strong class="font-mono-data tabular-nums text-[#09090B]">
                      {{ entry.workflow.business_value?.manual_baseline_minutes }} min
                    </strong>
                    <span v-if="entry.workflow.business_value?.loaded_labor_cost_per_hour">
                      · {{ entry.workflow.business_value?.currency || "USD" }} {{ entry.workflow.business_value?.loaded_labor_cost_per_hour }}/hr
                    </span>
                  </span>
                  <span class="font-mono-data">
                    Terminal: {{ entry.workflow.completion?.tool }} · {{ entry.workflow.completion?.success_when?.path }}
                  </span>
                </div>
                <div class="flex flex-wrap items-center gap-2 text-[11px]">
                  <span
                    v-if="workflowEvidenceSummary(entry).hasEvidence"
                    class="inline-flex items-center gap-1.5 rounded border border-[#E4E4E7] bg-[#FAFAFA] px-2 py-0.5 tabular-nums text-[#09090B]"
                  >
                    <span class="font-medium text-[#0F766E]">
                      {{ workflowEvidenceSummary(entry).completions }} completed outcome{{
                        workflowEvidenceSummary(entry).completions === 1 ? "" : "s"
                      }}
                    </span>
                    <span v-if="workflowEvidenceSummary(entry).verifiedRuns > 0" class="text-[#52525B]">
                      · {{ workflowEvidenceSummary(entry).verifiedRuns }} verified run{{
                        workflowEvidenceSummary(entry).verifiedRuns === 1 ? "" : "s"
                      }}
                    </span>
                    <span v-if="workflowEvidenceSummary(entry).savedMinutes > 0" class="text-[#52525B]">
                      · {{ formatDurationMinutes(workflowEvidenceSummary(entry).savedMinutes) }} saved
                    </span>
                    <span v-if="workflowEvidenceSummary(entry).valueText" class="font-medium text-[#059669]">
                      · {{ workflowEvidenceSummary(entry).valueText }}
                    </span>
                  </span>
                  <span
                    v-else
                    class="inline-flex items-center rounded border border-[#E4E4E7] bg-[#FAFAFA] px-2 py-0.5 text-[#71717A]"
                  >
                    No completed run evidence yet
                  </span>
                  <span
                    v-if="entry.workflow.business_value?.evidence_note"
                    class="text-[#52525B]"
                  >
                    {{ entry.workflow.business_value.evidence_note }}
                  </span>
                </div>
                <div
                  v-if="Array.isArray(entry.workflow.intent_examples) && entry.workflow.intent_examples.length"
                  class="flex flex-wrap items-center gap-1.5 pt-0.5"
                >
                  <span class="text-[11px] font-medium text-[#71717A]">Triggers:</span>
                  <span
                    v-for="(intent, idx) in entry.workflow.intent_examples.slice(0, 5)"
                    :key="idx"
                    class="rounded bg-[#F4F4F5] px-2 py-0.5 text-[11px] text-[#09090B]"
                  >
                    {{ intent }}
                  </span>
                </div>
              </div>

              <div class="flex flex-wrap items-center gap-3 shrink-0">
                <div class="inline-flex min-h-11 items-center gap-2 text-[12px] font-medium text-[#09090B] sm:min-h-9">
                  <Switch
                    :checked="entry.workflow.enabled !== false"
                    :disabled="togglingKey === `${entry.upstream_name}:${entry.workflow.id}`"
                    :aria-label="`Toggle ${entry.workflow.name} active state`"
                    @checked-change="(checked) => toggleWorkflowEnabled(entry, checked === true)"
                  >
                    <SwitchThumb />
                  </Switch>
                  <span class="min-w-[1.5rem] tabular-nums">
                    {{ entry.workflow.enabled !== false ? "On" : "Off" }}
                  </span>
                </div>
                <Button class="min-h-11 whitespace-nowrap sm:min-h-9" @click="openEditDrawer(entry)">
                  Edit Timing &amp; Triggers
                </Button>
                <Button class="min-h-11 whitespace-nowrap sm:min-h-9" @click="confirmDeleteWorkflow(entry)">
                  <AppIcon name="trash" :size="14" />
                  <span>Delete</span>
                </Button>
              </div>
            </div>
          </div>
        </section>

        <section class="rounded-lg border border-[#E4E4E7] bg-white p-5">
          <div class="flex flex-wrap items-center justify-between gap-2 pb-3 border-b border-[#E4E4E7]">
            <div>
              <h2 class="text-[16px] font-semibold text-[#09090B]">Unconfigured Discovered Workflows</h2>
              <p class="text-[12px] text-[#52525B]">
                Ranked by verified live traffic first, then schema dataflow patterns. Confirm a human baseline to activate, or dismiss false positives.
              </p>
            </div>
            <div class="flex flex-wrap items-center gap-2">
              <Button
                v-if="pendingCandidates.some((c) => c.origin === 'auto_schema' && c.verified_count === 0)"
                class="min-h-9 text-xs"
                :disabled="dismissSchemaBusy"
                @click="dismissAllSchemaSuggestions"
              >
                {{ dismissSchemaBusy ? "Dismissing…" : "Dismiss Schema Suggestions" }}
              </Button>
              <span class="text-[12px] tabular-nums text-[#71717A]">
                {{ pendingCandidates.length }} pending
              </span>
            </div>
          </div>

          <div v-if="!pendingCandidates.length" class="py-6 text-[13px] text-[#52525B]">
            No unreviewed workflow candidates right now.
          </div>
          <div v-else class="divide-y divide-[#E4E4E7]">
            <div
              v-for="cand in pendingCandidates"
              :key="cand.candidate_id"
              class="flex flex-col gap-3 py-4 lg:flex-row lg:items-center lg:justify-between"
            >
              <div class="min-w-0 flex-1 space-y-1">
                <div class="flex flex-wrap items-center gap-2">
                  <span class="text-[14px] font-semibold text-[#09090B]">
                    {{ cand.draft_definition?.name || cand.workflow_slug }}
                  </span>
                  <span class="rounded border border-[#E4E4E7] bg-[#F4F4F5] px-1.5 py-0.5 font-mono-data text-[11px] text-[#52525B]">
                    {{ cand.upstream_name }}
                  </span>
                  <span
                    class="rounded px-1.5 py-0.5 text-[11px] font-medium tabular-nums"
                    :class="
                      cand.verified_count > 0
                        ? 'border border-[#99F6E4] bg-[#F0FDFA] text-[#0F766E]'
                        : 'border border-[#FDE68A] bg-[#FFFBEB] text-[#B45309]'
                    "
                  >
                    {{
                      cand.verified_count > 0
                        ? `Verified by traffic (${cand.verified_count}×)`
                        : `${cand.origin} · observed ${cand.observed_count}×`
                    }}
                  </span>
                </div>
                <p class="text-[12px] text-[#52525B]">
                  {{ cand.draft_definition?.description || "Candidate mined from tool flow." }}
                </p>
                <div class="text-[12px] font-mono-data text-[#52525B] break-words">
                  {{ (cand.draft_definition?.participating_tools || []).join(" → ") }}
                </div>
                <div v-if="cand.sample_intents && cand.sample_intents.length" class="text-[11px] text-[#71717A]">
                  Sample intents: {{ cand.sample_intents.slice(0, 3).join(" · ") }}
                </div>
              </div>
              <div class="flex flex-wrap items-center gap-2 shrink-0">
                <Button data-variant="primary" class="min-h-11 sm:min-h-9" @click="openPromoteDrawer(cand)">
                  Set Baseline &amp; Activate
                </Button>
                <Button class="min-h-11 sm:min-h-9" @click="dismissCandidate(cand)">
                  Dismiss
                </Button>
              </div>
            </div>
          </div>
        </section>

        <section class="rounded-lg border border-[#E4E4E7] bg-white p-5">
          <div class="flex flex-wrap items-baseline justify-between gap-2 pb-3 border-b border-[#E4E4E7]">
            <div>
              <h2 class="text-[16px] font-semibold text-[#09090B]">Recent Detected Episodes</h2>
              <p class="text-[12px] text-[#52525B]">
                Live workflow episodes and verification evidence classified across MCP requests and tool calls.
              </p>
            </div>
            <span class="text-[12px] tabular-nums text-[#71717A]">
              {{ filteredEpisodes.length }} recent
            </span>
          </div>
          <div v-if="!filteredEpisodes.length" class="py-5 text-[13px] text-[#52525B]">
            No workflow episodes detected yet. Send requests through <code class="font-mono-data">/mcp</code> to observe completion.
          </div>
          <div v-else class="overflow-x-auto pt-2">
            <table class="w-full border-collapse text-left text-[12px]">
              <thead>
                <tr class="border-b border-[#E4E4E7] text-[#52525B]">
                  <th class="py-2 pr-3 font-medium">Upstream</th>
                  <th class="py-2 pr-3 font-medium">Workflow</th>
                  <th class="py-2 pr-3 font-medium">Outcome</th>
                  <th class="py-2 pr-3 font-medium">Tier</th>
                  <th class="py-2 pr-3 font-medium">Baseline</th>
                  <th class="py-2 font-medium">Updated</th>
                </tr>
              </thead>
              <tbody class="divide-y divide-[#E4E4E7]">
                <tr v-for="ep in filteredEpisodes.slice(0, 25)" :key="ep.episode_id">
                  <td class="py-2 pr-3 font-mono-data">{{ ep.upstream_name }}</td>
                  <td class="py-2 pr-3 font-mono-data">
                    {{ ep.matched_workflow_id || ep.workflow_id || ep.episode_id }}
                  </td>
                  <td class="py-2 pr-3">
                    <span
                      class="rounded px-1.5 py-0.5 text-[11px] font-medium"
                      :class="
                        ep.status === 'completed_verified' || ep.outcome_status === 'verified'
                          ? 'border border-[#A7F3D0] bg-[#ECFDF5] text-[#059669]'
                          : 'border border-[#E4E4E7] bg-[#F4F4F5] text-[#52525B]'
                      "
                    >
                      {{ (ep.outcome_status || ep.status).replaceAll("_", " ") }}
                    </span>
                  </td>
                  <td class="py-2 pr-3 text-[#52525B]">
                    {{ (ep.completion_tier || "observed").replaceAll("_", " ") }}
                  </td>
                  <td class="py-2 pr-3 tabular-nums">
                    {{ ep.manual_baseline_minutes ? `${ep.manual_baseline_minutes} min` : "—" }}
                  </td>
                  <td class="py-2 font-mono-data text-[#71717A]">{{ ep.updated_at || "—" }}</td>
                </tr>
              </tbody>
            </table>
          </div>
        </section>
      </TabsPanel>

      <TabsPanel value="ledger" class="mt-4">
        <BusinessValueReportSection
          :external-report="bvReport"
          :external-loading="bvLoading"
          :external-error="bvError"
          :external-fetched-at="bvFetchedAt"
          embedded
          @refresh="loadCatalog"
          @open-review="activeTab = 'workflows'"
        />
      </TabsPanel>
    </Tabs>

    <Drawer v-model:open="editDrawerOpen" side="right" prevent-edge-swipe>
      <DrawerBackdrop />
      <DrawerContent class="flex h-dvh w-full max-w-[95vw] flex-col gap-0 border-l border-[#E4E4E7] bg-white p-0 sm:w-[36rem]">
        <div class="shrink-0 flex items-start justify-between gap-3 border-b border-[#E4E4E7] px-5 py-4">
          <div class="min-w-0 space-y-1">
            <DrawerTitle>Edit Workflow Timing &amp; Triggers</DrawerTitle>
            <DrawerDescription>
              Adjust manual time assumptions, confidence, active status, or trigger phrases for {{ editTarget?.workflow?.id }}.
            </DrawerDescription>
          </div>
          <DrawerClose
            aria-label="Close workflow editor"
            class="inline-flex min-h-9 min-w-9 shrink-0 items-center justify-center rounded-md border border-[#D4D4D8] bg-white text-[#09090B] hover:bg-[#F4F4F5] focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-[#0F766E]"
          >
            <AppIcon name="x" :size="16" />
          </DrawerClose>
        </div>
        <div class="min-h-0 flex-1 overflow-y-auto overflow-x-hidden p-5 sm:p-6">
          <div
            v-if="editDrawerError"
            role="alert"
            class="mb-4 flex items-start gap-2 rounded-md border border-[#FECACA] bg-[#FEF2F2] px-3 py-2.5 text-[12px] text-[#DC2626]"
          >
            <AppIcon name="alert" :size="15" class="mt-0.5 shrink-0" />
            <span>{{ editDrawerError }}</span>
          </div>
          <form class="min-w-0 space-y-4" @submit.prevent="saveWorkflowEdit">
          <Field>
            <FieldLabel for="edit-wf-name">Workflow name</FieldLabel>
            <Input id="edit-wf-name" v-model="editForm.name" required />
          </Field>
          <Field>
            <FieldLabel for="edit-wf-desc">Business description</FieldLabel>
            <Textarea id="edit-wf-desc" v-model="editForm.description" rows="2" required />
          </Field>
          <div class="grid grid-cols-1 gap-3 sm:grid-cols-2">
            <Field>
              <FieldLabel for="edit-wf-minutes">Manual baseline (minutes)</FieldLabel>
              <Input
                id="edit-wf-minutes"
                :model-value="String(editForm.manual_baseline_minutes)"
                type="number"
                min="0.1"
                step="0.1"
                required
                @update:model-value="editForm.manual_baseline_minutes = Number($event)"
              />
            </Field>
            <Field>
              <FieldLabel>Confidence</FieldLabel>
              <Select
                :items="confidenceOptions"
                :value="editForm.confidence"
                @value-change="(val) => { if (val) onEditConfidenceChange(String(val)); }"
              >
                <SelectTrigger aria-label="Workflow confidence" class="min-h-10">
                  <SelectValue />
                  <AppIcon name="chevron-down" :size="14" />
                </SelectTrigger>
                <SelectContent>
                  <SelectItem v-for="opt in confidenceOptions" :key="opt.value" :value="opt.value">
                    {{ opt.label }}
                    <SelectItemIndicator><AppIcon name="check" :size="14" /></SelectItemIndicator>
                  </SelectItem>
                </SelectContent>
              </Select>
            </Field>
          </div>
          <Field>
            <FieldLabel>Baseline source</FieldLabel>
            <Select
              :items="baselineSourceOptions"
              :value="editForm.baseline_source"
              @value-change="(val) => { if (val) onEditBaselineSourceChange(String(val)); }"
            >
              <SelectTrigger aria-label="Baseline source" class="min-h-10">
                <SelectValue />
                <AppIcon name="chevron-down" :size="14" />
              </SelectTrigger>
              <SelectContent>
                <SelectItem v-for="opt in baselineSourceOptions" :key="opt.value" :value="opt.value">
                  {{ opt.label }}
                  <SelectItemIndicator><AppIcon name="check" :size="14" /></SelectItemIndicator>
                </SelectItem>
              </SelectContent>
            </Select>
            <FieldDescription>
              Administrator estimates are unvalidated; external/sampled measurements allow Validated confidence.
            </FieldDescription>
          </Field>
          <Field>
            <FieldLabel for="edit-wf-intents">Trigger phrases / intents (one per line)</FieldLabel>
            <Textarea id="edit-wf-intents" v-model="editForm.intent_examples_text" rows="4" />
            <FieldDescription>
              Used as classifier and semantic intent triggers when evaluating incoming agent traffic.
            </FieldDescription>
          </Field>
          <Field>
            <FieldLabel for="edit-wf-note">Evidence note</FieldLabel>
            <Input id="edit-wf-note" v-model="editForm.evidence_note" />
          </Field>
          <label class="flex items-center gap-2 text-[13px] text-[#09090B]">
            <Switch
              :checked="editForm.enabled"
              aria-label="Workflow active"
              @checked-change="(val) => { editForm.enabled = val === true; }"
            >
              <SwitchThumb />
            </Switch>
            <span>Workflow active (enabled for episode attribution &amp; ROI)</span>
          </label>
          <div class="flex justify-end gap-2 border-t border-[#E4E4E7] pt-4">
            <Button type="button" @click="editDrawerOpen = false">Cancel</Button>
            <Button type="submit" data-variant="primary" :disabled="editSaving">
              {{ editSaving ? "Saving…" : "Save Changes" }}
            </Button>
          </div>
          </form>
        </div>
      </DrawerContent>
    </Drawer>

    <Drawer v-model:open="promoteDrawerOpen" side="right" prevent-edge-swipe>
      <DrawerBackdrop />
      <DrawerContent class="flex h-dvh w-full max-w-[95vw] flex-col gap-0 border-l border-[#E4E4E7] bg-white p-0 sm:w-[32rem]">
        <div class="shrink-0 flex items-start justify-between gap-3 border-b border-[#E4E4E7] px-5 py-4">
          <div class="min-w-0 space-y-1">
            <DrawerTitle>Set Human Baseline &amp; Activate</DrawerTitle>
            <DrawerDescription>
              Confirm how many minutes a person normally takes to finish '{{ promoteTarget?.draft_definition?.name || promoteTarget?.workflow_slug }}'.
            </DrawerDescription>
          </div>
          <DrawerClose
            aria-label="Close promotion drawer"
            class="inline-flex min-h-9 min-w-9 shrink-0 items-center justify-center rounded-md border border-[#D4D4D8] bg-white text-[#09090B] hover:bg-[#F4F4F5] focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-[#0F766E]"
          >
            <AppIcon name="x" :size="16" />
          </DrawerClose>
        </div>
        <div class="min-h-0 flex-1 overflow-y-auto overflow-x-hidden p-5 sm:p-6">
          <div
            v-if="promoteDrawerError"
            role="alert"
            class="mb-4 flex items-start gap-2 rounded-md border border-[#FECACA] bg-[#FEF2F2] px-3 py-2.5 text-[12px] text-[#DC2626]"
          >
            <AppIcon name="alert" :size="15" class="mt-0.5 shrink-0" />
            <span>{{ promoteDrawerError }}</span>
          </div>
          <form class="min-w-0 space-y-4" @submit.prevent="confirmPromoteCandidate">
          <Field>
            <FieldLabel for="promote-minutes">Manual workflow time (minutes)</FieldLabel>
            <Input
              id="promote-minutes"
              :model-value="String(promoteForm.manual_baseline_minutes)"
              type="number"
              min="0.1"
              step="0.1"
              required
              @update:model-value="promoteForm.manual_baseline_minutes = Number($event)"
            />
          </Field>
          <Field>
            <FieldLabel>Baseline source</FieldLabel>
            <Select
              :items="baselineSourceOptions"
              :value="promoteForm.baseline_source"
              @value-change="(val) => { if (val) promoteForm.baseline_source = String(val); }"
            >
              <SelectTrigger aria-label="Candidate baseline source" class="min-h-10">
                <SelectValue />
                <AppIcon name="chevron-down" :size="14" />
              </SelectTrigger>
              <SelectContent>
                <SelectItem v-for="opt in baselineSourceOptions" :key="opt.value" :value="opt.value">
                  {{ opt.label }}
                  <SelectItemIndicator><AppIcon name="check" :size="14" /></SelectItemIndicator>
                </SelectItem>
              </SelectContent>
            </Select>
          </Field>
          <Field>
            <FieldLabel for="promote-note">Evidence note</FieldLabel>
            <Input id="promote-note" v-model="promoteForm.evidence_note" />
          </Field>
          <div class="flex justify-end gap-2 border-t border-[#E4E4E7] pt-4">
            <Button type="button" @click="promoteDrawerOpen = false">Cancel</Button>
            <Button type="submit" data-variant="primary" :disabled="promoteSaving">
              {{ promoteSaving ? "Activating…" : "Confirm & Activate" }}
            </Button>
          </div>
          </form>
        </div>
      </DrawerContent>
    </Drawer>

    <WorkflowEditorDrawer
      v-model:open="createWorkflowDrawerOpen"
      :upstreams="upstreams"
      :upstream="upstreamFilter !== '__all__' ? upstreamFilter : upstreams[0] || ''"
      @saved="handleCreatedWorkflowSaved"
    />

    <PromptBundleDrawer
      v-model:open="promptDrawerOpen"
      focus="workflows"
      :upstreams="upstreams"
      :upstream="upstreamFilter !== '__all__' ? upstreamFilter : upstreams[0] || ''"
      @applied="handleBundleApplied"
    />

    <Dialog v-model:open="deleteDialogOpen">
      <DialogBackdrop />
      <DialogContent class="sm:max-w-md w-[calc(100%-2rem)]">
        <DialogTitle>Delete Workflow?</DialogTitle>
        <DialogDescription>
          Remove '{{ deleteTarget?.workflow?.name }}' from '{{ deleteTarget?.upstream_name }}' and publish a new definition revision. To temporarily stop attribution instead, pause its toggle.
        </DialogDescription>
        <div class="mt-4 flex justify-end gap-2">
          <DialogClose>Cancel</DialogClose>
          <Button data-variant="primary" :disabled="deleteBusy" @click="performDeleteWorkflow">
            {{ deleteBusy ? "Deleting…" : "Delete Workflow" }}
          </Button>
        </div>
      </DialogContent>
    </Dialog>
  </div>
</template>
