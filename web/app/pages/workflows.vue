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
import { Textarea } from "@ui-sdk/vue/textarea";
import { computed, onMounted, ref } from "vue";
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
  status: string;
  outcome_status?: string | null;
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

const routeUpstream =
  typeof useRoute === "function" ? useRoute()?.query?.upstream : undefined;
const initialUpstreamFilter =
  typeof routeUpstream === "string" && routeUpstream.trim() ? routeUpstream.trim() : "__all__";

const upstreamFilter = ref(initialUpstreamFilter);
const searchQuery = ref("");
const discoverBusy = ref(false);
const dismissSchemaBusy = ref(false);
const togglingKey = ref("");

const editDrawerOpen = ref(false);
const editTarget = ref<WorkflowCatalogEntry | null>(null);
const editSaving = ref(false);
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
const promoteForm = ref({
  manual_baseline_minutes: 10,
  baseline_source: "administrator_estimate",
  effective_from: new Date().toISOString().slice(0, 10),
  evidence_note: "Confirmed from discovered workflow candidate.",
});

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
  () => filteredEpisodes.value.filter((ep) => ep.outcome_status === "verified").length,
);

async function loadCatalog() {
  loading.value = true;
  loadError.value = "";
  try {
    const res = await apiFetch<{
      upstreams?: string[];
      workflows?: WorkflowCatalogEntry[];
      candidates?: DiscoveredCandidate[];
      episodes?: DetectedEpisode[];
    }>("/admin/workflows/catalog");
    upstreams.value = res.upstreams ?? [];
    workflows.value = res.workflows ?? [];
    candidates.value = res.candidates ?? [];
    episodes.value = res.episodes ?? [];
  } catch (err) {
    loadError.value = extractErrorMessage(err);
  } finally {
    loading.value = false;
  }
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

async function saveWorkflowEdit() {
  if (!editTarget.value) return;
  const target = editTarget.value;
  editSaving.value = true;
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
    actionError.value = extractErrorMessage(err);
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
  actionError.value = "";
  try {
    const minutes = Number(promoteForm.value.manual_baseline_minutes);
    if (!Number.isFinite(minutes) || minutes <= 0) {
      throw new Error("Manual baseline minutes must be greater than zero.");
    }
    await apiFetch(
      `/admin/workflows/discovered/${encodeURIComponent(promoteTarget.value.candidate_id)}/promote`,
      {
        method: "POST",
        body: {
          manual_baseline_minutes: minutes,
          baseline_source: promoteForm.value.baseline_source,
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
    actionError.value = extractErrorMessage(err);
  } finally {
    promoteSaving.value = false;
  }
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
    <div class="flex flex-col gap-3 sm:flex-row sm:items-center sm:justify-between">
      <div>
        <h1 class="text-[20px] font-semibold tracking-[-0.015em] text-[#09090B]">Workflows</h1>
        <p class="mt-0.5 text-[13px] text-[#52525B]">
          Activate or pause workflows, tune manual human time baselines and trigger intents, and review auto-discovered processes.
        </p>
      </div>
      <div class="flex flex-wrap items-center gap-2">
        <Button class="min-h-11 sm:min-h-9" :disabled="discoverBusy" @click="runDiscoverForSelection">
          {{ discoverBusy ? "Discovering…" : "Discover from Schema & Traffic" }}
        </Button>
        <Button class="min-h-11 sm:min-h-9" @click="loadCatalog">
          <AppIcon name="refresh" :size="14" />
          <span>Refresh</span>
        </Button>
        <NuxtLink
          to="/onboarding"
          class="inline-flex min-h-11 items-center rounded-md border border-[#E4E4E7] bg-white px-3 text-[13px] font-medium text-[#09090B] hover:bg-[#F4F4F5] sm:min-h-9"
        >
          New Onboarding Bundle
        </NuxtLink>
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
      <Button class="min-h-11" @click="loadCatalog">Retry</Button>
    </div>

    <section class="grid grid-cols-2 gap-3 rounded-lg border border-[#E4E4E7] bg-white p-4 sm:grid-cols-4">
      <div>
        <div class="text-[12px] font-medium text-[#52525B]">Active workflows</div>
        <div class="mt-1 text-[20px] font-semibold tabular-nums text-[#0F766E]">{{ activeCount }}</div>
      </div>
      <div>
        <div class="text-[12px] font-medium text-[#52525B]">Paused workflows</div>
        <div class="mt-1 text-[20px] font-semibold tabular-nums text-[#09090B]">{{ inactiveCount }}</div>
      </div>
      <div>
        <div class="text-[12px] font-medium text-[#52525B]">Candidates to review</div>
        <div class="mt-1 text-[20px] font-semibold tabular-nums text-[#09090B]">{{ pendingCandidates.length }}</div>
      </div>
      <div>
        <div class="text-[12px] font-medium text-[#52525B]">Verified outcomes</div>
        <div class="mt-1 text-[20px] font-semibold tabular-nums text-[#09090B]">{{ verifiedOutcomeCount }}</div>
      </div>
    </section>

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
            Toggle workflows active or paused without deleting definitions, or tune manual time baselines and intent triggers.
          </p>
        </div>
        <span class="text-[12px] tabular-nums text-[#71717A]">
          {{ filteredWorkflows.length }} total
        </span>
      </div>

      <div v-if="loading" class="py-6 text-[13px] text-[#52525B]" role="status">
        Loading workflow catalog…
      </div>
      <div v-else-if="!filteredWorkflows.length" class="py-6 text-[13px] text-[#52525B]">
        No workflows configured yet. Paste an agent-generated bundle on
        <NuxtLink to="/onboarding" class="font-medium text-[#0F766E] underline">Onboarding</NuxtLink>
        or run discovery above.
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
                ({{ entry.workflow.business_value?.confidence || "unvalidated" }})
              </span>
              <span class="font-mono-data">
                Terminal: {{ entry.workflow.completion?.tool }} · {{ entry.workflow.completion?.success_when?.path }}
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
            <label class="inline-flex min-h-11 items-center gap-2 text-[12px] font-medium text-[#09090B] sm:min-h-9">
              <Switch
                :checked="entry.workflow.enabled !== false"
                :disabled="togglingKey === `${entry.upstream_name}:${entry.workflow.id}`"
                :aria-label="`Toggle ${entry.workflow.name} active state`"
                @checked-change="(checked) => toggleWorkflowEnabled(entry, checked === true)"
              >
                <SwitchThumb />
              </Switch>
              <span>{{ entry.workflow.enabled !== false ? "On" : "Off" }}</span>
            </label>
            <Button class="min-h-11 sm:min-h-9" @click="openEditDrawer(entry)">
              Edit Timing &amp; Triggers
            </Button>
            <Button class="min-h-11 sm:min-h-9" @click="confirmDeleteWorkflow(entry)">
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
            Live workflow episodes classified across MCP requests and tool calls.
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
              <th class="py-2 pr-3 font-medium">Steps</th>
              <th class="py-2 pr-3 font-medium">Baseline</th>
              <th class="py-2 font-medium">Updated</th>
            </tr>
          </thead>
          <tbody class="divide-y divide-[#E4E4E7]">
            <tr v-for="ep in filteredEpisodes.slice(0, 25)" :key="ep.episode_id">
              <td class="py-2 pr-3 font-mono-data">{{ ep.upstream_name }}</td>
              <td class="py-2 pr-3 font-mono-data">{{ ep.workflow_id || ep.episode_id }}</td>
              <td class="py-2 pr-3">{{ ep.outcome_status || ep.status }}</td>
              <td class="py-2 pr-3 tabular-nums">{{ ep.step_count ?? 1 }}</td>
              <td class="py-2 pr-3 tabular-nums">
                {{ ep.manual_baseline_minutes ? `${ep.manual_baseline_minutes} min` : "—" }}
              </td>
              <td class="py-2 font-mono-data text-[#71717A]">{{ ep.updated_at || "—" }}</td>
            </tr>
          </tbody>
        </table>
      </div>
    </section>

    <Drawer v-model:open="editDrawerOpen" side="right" prevent-edge-swipe>
      <DrawerBackdrop />
      <DrawerContent class="flex h-dvh w-full max-w-[95vw] flex-col gap-0 border-l border-[#E4E4E7] bg-white p-0 sm:w-[36rem]">
        <div class="min-h-0 flex-1 overflow-y-auto overflow-x-hidden p-5 sm:p-7">
          <DrawerClose aria-label="Close workflow editor" class="absolute right-4 top-4">
            <AppIcon name="x" :size="16" />
          </DrawerClose>
          <DrawerTitle>Edit Workflow Timing &amp; Triggers</DrawerTitle>
          <DrawerDescription>
            Adjust manual time assumptions, confidence, active status, or trigger phrases for {{ editTarget?.workflow?.id }}.
          </DrawerDescription>
          <form class="mt-5 min-w-0 space-y-4" @submit.prevent="saveWorkflowEdit">
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
                @value-change="(val) => { if (val) editForm.confidence = String(val); }"
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
              @value-change="(val) => { if (val) editForm.baseline_source = String(val); }"
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
        <div class="min-h-0 flex-1 overflow-y-auto overflow-x-hidden p-5 sm:p-7">
          <DrawerClose aria-label="Close promotion drawer" class="absolute right-4 top-4">
            <AppIcon name="x" :size="16" />
          </DrawerClose>
          <DrawerTitle>Set Human Baseline &amp; Activate</DrawerTitle>
          <DrawerDescription>
            Confirm how many minutes a person normally takes to finish '{{ promoteTarget?.draft_definition?.name || promoteTarget?.workflow_slug }}'.
          </DrawerDescription>
          <form class="mt-5 min-w-0 space-y-4" @submit.prevent="confirmPromoteCandidate">
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
