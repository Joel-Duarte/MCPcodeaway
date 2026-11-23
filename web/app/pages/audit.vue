<script setup lang="ts">
import { extractErrorMessage } from "~/composables/useAdminApi";

interface AuditSubcall {
  server: string;
  tool: string;
  status: string;
  duration_ms: number;
  raw_bytes: number;
  abac_rows_filtered: number;
  redactions_count: number;
  error?: string | null;
}

interface AuditRun {
  run_id: string;
  created_at?: string;
  mode: string;
  ok: boolean;
  duration_ms: number;
  tenant_id: string;
  user_sub: string;
  user_email?: string | null;
  user_groups?: string[];
  subcall_count: number;
  raw_upstream_bytes: number;
  returned_bytes: number;
  estimated_tokens_saved: number;
  schema_tokens_saved: number;
  abac_rows_filtered: number;
  redactions_count: number;
  grounding_blocked?: boolean;
  tokens_used_est?: number;
  tokens_saved_by?: string;
  monty_spawn_ms?: number;
  monty_exec_ms?: number;
  trace_spans?: {
    stage: string;
    label: string;
    start_ms: number;
    duration_ms: number;
    status: string;
  }[];
  code?: string | null;
  stdout?: string | null;
  error?: string | null;
  mode_decision_reason?: string | null;
  mode_decision_score?: number | null;
  experiment_id?: string | null;
  ab_variant?: string | null;
  provenance?: {
    grounded?: boolean;
    provenance_hmac?: string;
    hmac_sha256?: string;
    unverified_identifiers?: string[];
  } | null;
  subcalls?: AuditSubcall[];
}

const { apiFetch } = useAdminApi();
const { events: wsEvents } = useLiveTelemetry();

const loading = ref(true);
const errorMessage = ref("");
const statusMessage = ref("");
const limitFilter = ref("25");
const offset = ref(0);
const totalCount = ref(0);
const statusFilter = ref<"all" | "errors" | "grounding_blocked" | "ok">("all");
const modeFilter = ref("all");
const searchQuery = ref("");
const liveMode = ref(true);
const tenantFilter = ref("");
const runs = ref<AuditRun[]>([]);
let searchDebounce: ReturnType<typeof setTimeout> | null = null;

const drawerOpen = ref(false);
const selectedRun = ref<AuditRun | null>(null);

const pruneDialogOpen = ref(false);
const retentionDays = ref("30");

const limitOptions = [
  { value: "25", label: "25 per page" },
  { value: "50", label: "50 per page" },
  { value: "100", label: "100 per page" },
];

const statusFilterLabels = {
  all: "All runs",
  errors: "Errors only",
  grounding_blocked: "Grounding blocked",
  ok: "Succeeded",
} as const;

const modeFilterOptions = [
  { value: "all", label: "All Modes" },
  { value: "codemode", label: "Code Mode (Deferred)" },
  { value: "codemode_upfront", label: "Code Mode (Upfront)" },
  { value: "traditional", label: "Classic (Upfront)" },
  { value: "traditional_discover", label: "Classic (Discover)" },
  { value: "hybrid", label: "Hybrid" },
];

async function fetchRuns(resetOffset = false) {
  if (resetOffset) offset.value = 0;
  errorMessage.value = "";
  const params = new URLSearchParams();
  params.set("limit", limitFilter.value || "25");
  params.set("offset", String(offset.value));
  params.set("status", statusFilter.value);
  if (modeFilter.value !== "all") {
    params.set("mode", modeFilter.value);
  }
  if (searchQuery.value.trim()) {
    params.set("q", searchQuery.value.trim());
  }
  if (tenantFilter.value.trim()) {
    params.set("tenant_id", tenantFilter.value.trim());
  }
  try {
    const res = await apiFetch<{ count: number; total_count?: number; runs: AuditRun[] }>(
      `/audit/runs?${params.toString()}`
    );
    runs.value = res.runs;
    totalCount.value = res.total_count ?? res.count;
  } catch (err) {
    errorMessage.value = extractErrorMessage(err);
  } finally {
    loading.value = false;
  }
}

watch(wsEvents, (list) => {
  if (!liveMode.value || offset.value > 0 || list.length === 0) return;
  const latest = list[0];
  if (!latest) return;
  if (statusFilter.value === "errors" && latest.ok) return;
  if (statusFilter.value === "ok" && !latest.ok) return;
  if (statusFilter.value === "grounding_blocked" && !latest.grounding_blocked) return;
  if (modeFilter.value !== "all" && latest.mode !== modeFilter.value) return;
  if (runs.value.some((r) => r.run_id === latest.run_id)) return;
  const row: AuditRun = {
    run_id: latest.run_id,
    created_at: latest.created_at,
    mode: latest.mode,
    ok: latest.ok,
    duration_ms: latest.duration_ms,
    tenant_id: latest.tenant_id || "",
    user_sub: latest.user_sub,
    user_email: latest.user_email ?? null,
    user_groups: latest.user_groups || [],
    subcall_count: latest.subcall_count ?? 0,
    raw_upstream_bytes: latest.raw_upstream_bytes ?? 0,
    returned_bytes: latest.returned_bytes ?? 0,
    estimated_tokens_saved: latest.estimated_tokens_saved ?? latest.tokens_saved,
    schema_tokens_saved: latest.schema_tokens_saved ?? 0,
    abac_rows_filtered: latest.abac_rows_filtered ?? 0,
    redactions_count: latest.redactions_count ?? 0,
    grounding_blocked: latest.grounding_blocked,
    mode_decision_reason: latest.mode_decision_reason,
  };
  const pageCap = Number.parseInt(limitFilter.value || "25", 10) || 25;
  runs.value = [row, ...runs.value].slice(0, pageCap);
  totalCount.value += 1;
});

watch([searchQuery, tenantFilter], () => {
  if (searchDebounce) clearTimeout(searchDebounce);
  searchDebounce = setTimeout(() => {
    void fetchRuns(true);
  }, 300);
});

function setStatusFilter(val: "all" | "errors" | "grounding_blocked" | "ok") {
  statusFilter.value = val;
  void fetchRuns(true);
}

const filterSummary = computed(() => {
  const scope = tenantFilter.value.trim()
    ? `Tenant ${tenantFilter.value.trim()}`
    : "Default tenant scope";
  const mode = modeFilterOptions.find((option) => option.value === modeFilter.value)?.label || "All modes";
  const search = searchQuery.value.trim();
  return [
    `Tenant: ${scope}`,
    `Status: ${statusFilterLabels[statusFilter.value]}`,
    `Mode: ${mode}`,
    search ? `Search: “${search}”` : "No search text",
  ].join(" · ");
});

const hasActiveFilters = computed(() =>
  statusFilter.value !== "all"
  || modeFilter.value !== "all"
  || Boolean(searchQuery.value.trim())
  || Boolean(tenantFilter.value.trim())
);

function clearFilters() {
  if (searchDebounce) clearTimeout(searchDebounce);
  const hasTextFilters = Boolean(searchQuery.value.trim() || tenantFilter.value.trim());
  searchQuery.value = "";
  tenantFilter.value = "";
  statusFilter.value = "all";
  modeFilter.value = "all";
  if (!hasTextFilters) void fetchRuns(true);
}

function prevPage() {
  const step = Number.parseInt(limitFilter.value || "25", 10) || 25;
  offset.value = Math.max(0, offset.value - step);
  void fetchRuns();
}

function nextPage() {
  const step = Number.parseInt(limitFilter.value || "25", 10) || 25;
  if (offset.value + step < totalCount.value) {
    offset.value += step;
    void fetchRuns();
  }
}

async function inspectRun(runId: string) {
  errorMessage.value = "";
  try {
    selectedRun.value = await apiFetch<AuditRun>(`/audit/runs/${encodeURIComponent(runId)}`);
    drawerOpen.value = true;
  } catch (err) {
    errorMessage.value = extractErrorMessage(err);
  }
}

async function confirmPrune() {
  errorMessage.value = "";
  statusMessage.value = "";
  const days = parsedRetentionDays.value;
  if (days === null) {
    errorMessage.value = "Enter a whole number from 0 to 3,650 days.";
    return;
  }
  try {
    const res = await apiFetch<{ deleted_runs: number; retention_days: number }>(
      `/admin/audit/prune?retention_days=${days}`,
      { method: "POST" }
    );
    statusMessage.value = `Pruned ${res.deleted_runs} audit runs older than ${res.retention_days} days.`;
    pruneDialogOpen.value = false;
    await fetchRuns();
  } catch (err) {
    errorMessage.value = extractErrorMessage(err);
  }
}

const parsedRetentionDays = computed(() => {
  const raw = retentionDays.value.trim();
  if (!/^\d+$/.test(raw)) return null;
  const days = Number.parseInt(raw, 10);
  return days <= 3650 ? days : null;
});

const pruneCutoffPreview = computed(() => {
  const days = parsedRetentionDays.value;
  if (days === null) return "Enter a whole number from 0 to 3,650 days to see the retention cutoff.";
  if (days === 0) return "All audit runs currently stored in this gateway will be deleted.";
  const cutoff = new Date(Date.now() - days * 24 * 60 * 60 * 1000);
  const date = new Intl.DateTimeFormat("en-GB", {
    dateStyle: "medium",
    timeStyle: "short",
    timeZone: "UTC",
  }).format(cutoff);
  return `Runs older than about ${date} UTC will be deleted from this gateway's audit store.`;
});

function runOutcomeLabel(run: AuditRun): string {
  return run.ok ? "Succeeded" : "Failed";
}

function runOutcomeClass(run: AuditRun): string {
  return run.ok
    ? "border-[#A7F3D0] bg-[#ECFDF5] text-[#059669]"
    : "border-[#FECACA] bg-[#FEF2F2] text-[#DC2626]";
}

function groundingStatusLabel(run: AuditRun): string {
  if (run.grounding_blocked) return "Blocked";
  if (run.provenance?.grounded === true) return "Verified";
  if (run.provenance?.grounded === false) return "Not verified";
  return "Not supplied";
}

function groundingStatusClass(run: AuditRun): string {
  if (run.provenance?.grounded === false) return "text-[#DC2626]";
  if (run.grounding_blocked) return "text-[#92400E]";
  if (run.provenance?.grounded === true) return "text-[#059669]";
  return "text-[#71717A]";
}

function modeLabel(mode?: string | null): string {
  if (mode === "codemode") return "Code Mode (deferred)";
  if (mode === "codemode_upfront") return "Code Mode (upfront)";
  if (mode === "traditional_discover") return "Classic (discover tools)";
  if (mode === "traditional" || mode === "classic") return "Classic (upfront)";
  if (mode === "hybrid") return "Hybrid";
  return "the recorded mode";
}

function decisionExplanation(run: AuditRun): string {
  const reason = run.mode_decision_reason?.trim() || "";
  if (reason.startsWith("explicit:")) {
    return `The request explicitly selected ${modeLabel(reason.slice("explicit:".length))}.`;
  }
  const autoMatch = reason.match(/^auto:(.+?)->([^;]+)$/);
  if (autoMatch) return `Auto selected ${modeLabel(autoMatch[2])} for this request.`;
  if (reason.startsWith("auto->")) {
    return `Auto selected ${modeLabel(reason.slice("auto->".length).split(";")[0])} for this request.`;
  }
  if (reason.startsWith("adaptive_recommendation:")) {
    const mode = reason.split(":").at(-1)?.split(";")[0];
    return `Adaptive routing selected ${modeLabel(mode)} based on paired run evidence.`;
  }
  if (reason.startsWith("adaptive_")) {
    return "Adaptive routing details are available in Raw Audit JSON.";
  }
  if (!reason) return `Applied ${modeLabel(run.mode)}; no selection reason was recorded.`;
  return `Routing details are available in Raw Audit JSON. Applied ${modeLabel(run.mode)}.`;
}

function subcallStatusLabel(status?: string | null): string {
  if (!status) return "Not reported";
  const normalized = status.toLowerCase();
  if (normalized === "ok" || normalized === "succeeded" || normalized === "success") return "Succeeded";
  if (normalized === "error" || normalized === "failed" || normalized === "failure") return "Failed";
  if (normalized === "blocked") return "Blocked";
  return status;
}

function subcallStatusClass(status?: string | null): string {
  const normalized = status?.toLowerCase();
  if (normalized === "ok" || normalized === "succeeded" || normalized === "success") return "text-[#059669]";
  if (normalized === "error" || normalized === "failed" || normalized === "failure") return "text-[#DC2626]";
  if (normalized === "blocked") return "text-[#92400E]";
  return "text-[#71717A]";
}

function reductionCredit(run: AuditRun): number {
  if (!run.ok || run.grounding_blocked || run.provenance?.grounded !== true) return 0;
  return Math.max(0, (run.estimated_tokens_saved || 0) + (run.schema_tokens_saved || 0));
}

function reductionCreditNote(run: AuditRun): string {
  if (!run.ok) return "No reduction credit: this execution did not complete successfully.";
  if (run.grounding_blocked) return "No reduction credit: grounding verification blocked this execution.";
  if (!run.provenance) return "No reduction credit: this record does not include an affirmative provenance-verification result.";
  if (run.provenance.grounded !== true) return "No reduction credit: provenance verification did not pass.";
  return "Gateway-reported estimated token reduction (estimated_tokens_saved + schema_tokens_saved). Credited only for successful executions with affirmative provenance verification; this is not an independently verified billed-token delta or cash saving.";
}

const timelineTicks = [0, 25, 50, 75, 100];
function timelineTickLabel(run: AuditRun, pct: number): string {
  return `${((run.duration_ms * pct) / 100).toFixed(1)} ms`;
}

const route = useRoute();

const bottleneck = computed(() => {
  const run = selectedRun.value;
  const spans = run?.trace_spans ?? [];
  const [first] = spans;
  if (!run || !first) return null;
  const slowest = spans.reduce((a, b) => (b.duration_ms > a.duration_ms ? b : a), first);
  const pct = run.duration_ms > 0 ? Math.round((slowest.duration_ms / run.duration_ms) * 100) : 0;
  return { span: slowest, pct };
});

onMounted(() => {
  const qStatus = String(route.query.status || "");
  if (qStatus === "errors" || qStatus === "grounding_blocked" || qStatus === "ok") {
    statusFilter.value = qStatus;
  }
  void fetchRuns();
  const qRunId = String(route.query.run_id || "");
  if (qRunId) {
    void inspectRun(qRunId);
  }
});

function spanBarStyle(span: { start_ms: number; duration_ms: number }, totalMs: number) {
  const denom = Math.max(totalMs, 0.001);
  const left = Math.min(100, Math.max(0, (span.start_ms / denom) * 100));
  const width = Math.max(0, Math.min(100 - left, (span.duration_ms / denom) * 100));
  return { left: `${left.toFixed(2)}%`, width: `${width.toFixed(2)}%` };
}

function spanBarColor(stage: string, status: string) {
  if (status === "blocked") return "bg-[#D97706]";
  if (status === "error") return "bg-[#DC2626]";
  if (stage === "monty_spawn" || stage === "monty_exec") return "bg-[#0F766E]";
  if (stage === "upstream_subcall") return "bg-[#2563EB]";
  return "bg-[#71717A]";
}
</script>

<template>
  <div class="space-y-6">
    <div class="flex flex-col gap-3 sm:flex-row sm:items-center sm:justify-between">
      <div>
        <h1 class="text-[20px] font-semibold tracking-[-0.015em] text-[#09090B]">
          Audit Runs, Provenance &amp; Retention
        </h1>
        <p class="mt-0.5 text-[13px] text-[#52525B]">
          Inspect Monty scripts, subcall provenance signatures, ABAC filtering, and database retention pruning.
        </p>
      </div>
      <div class="flex flex-wrap items-center gap-2">
        <label class="flex items-center gap-2 rounded-md border border-[#E4E4E7] bg-white px-2.5 py-1.5 text-xs font-medium text-[#09090B] cursor-pointer">
          <Switch v-model:checked="liveMode">
            <SwitchThumb />
          </Switch>
          <span>Real-Time</span>
        </label>
        <Button pending-announcement="Refreshing runs" @click="fetchRuns(false)">
          <AppIcon name="refresh" :size="15" />
          <span>Refresh</span>
        </Button>
        <Button data-variant="danger" @click="pruneDialogOpen = true">
          <AppIcon name="trash" :size="15" />
          <span>Prune Old Runs</span>
        </Button>
      </div>
    </div>

    <section class="rounded-lg border border-[#E4E4E7] bg-white p-4 space-y-3">
      <div class="flex flex-wrap items-center gap-2">
        <span class="text-xs font-medium text-[#52525B]">Status:</span>
        <Button
          v-for="s in [
            { value: 'all', label: 'All Runs' },
            { value: 'errors', label: 'Errors Only' },
            { value: 'grounding_blocked', label: 'Grounding Blocked' },
            { value: 'ok', label: 'Succeeded' },
          ] as const"
          :key="s.value"
          :data-variant="statusFilter === s.value ? 'primary' : undefined"
          class="px-2.5 py-1 text-xs"
          @click="setStatusFilter(s.value)"
        >
          {{ s.label }}
        </Button>
      </div>

      <div class="grid grid-cols-1 gap-3 sm:grid-cols-2 lg:grid-cols-4">
        <Field>
          <FieldLabel>Search Run / User / Error</FieldLabel>
          <Input v-model="searchQuery" placeholder="run_id, user, or error text..." />
        </Field>

        <Field>
          <FieldLabel>Execution Mode</FieldLabel>
          <Select
            :items="modeFilterOptions"
            :value="modeFilter"
            @value-change="(v) => { if (v) { modeFilter = String(v); void fetchRuns(true); } }"
          >
            <SelectTrigger>
              <SelectValue />
              <AppIcon name="chevron-down" :size="14" />
            </SelectTrigger>
            <SelectContent>
              <SelectItem v-for="opt in modeFilterOptions" :key="opt.value" :value="opt.value">
                {{ opt.label }}
                <SelectItemIndicator><AppIcon name="check" :size="14" /></SelectItemIndicator>
              </SelectItem>
            </SelectContent>
          </Select>
        </Field>

        <Field>
          <FieldLabel>Page Size</FieldLabel>
          <Select
            :items="limitOptions"
            :value="limitFilter"
            @value-change="(v) => { if (v) { limitFilter = String(v); void fetchRuns(true); } }"
          >
            <SelectTrigger>
              <SelectValue />
              <AppIcon name="chevron-down" :size="14" />
            </SelectTrigger>
            <SelectContent>
              <SelectItem v-for="opt in limitOptions" :key="opt.value" :value="opt.value">
                {{ opt.label }}
                <SelectItemIndicator><AppIcon name="check" :size="14" /></SelectItemIndicator>
              </SelectItem>
            </SelectContent>
          </Select>
        </Field>

        <Field>
          <FieldLabel>Tenant filter</FieldLabel>
          <Input v-model="tenantFilter" placeholder="Leave blank for default scope" />
          <FieldDescription>Blank uses your default tenant. Enter a tenant ID to narrow this run list; it does not limit retention pruning.</FieldDescription>
        </Field>
      </div>
      <div class="border-t border-[#E4E4E7] pt-3 text-xs leading-5 text-[#52525B]">
        <span class="font-medium text-[#09090B]">Run filters:</span>
        <strong class="text-[#DC2626]">Errors</strong> are requests that failed, including upstream, access-policy, or code errors.
        <strong class="text-[#92400E]">Grounding blocked</strong> means Code Mode stopped output that could not be checked against upstream results.
      </div>
    </section>

    <div
      v-if="statusMessage"
      role="status"
      class="rounded-lg border border-[#99F6E4] bg-[#F0FDFA] px-4 py-2.5 text-[13px] text-[#0F766E]"
    >
      {{ statusMessage }}
    </div>

    <div
      v-if="errorMessage"
      role="alert"
      class="flex items-start gap-2 rounded-lg border border-[#FECACA] bg-[#FEF2F2] px-4 py-3 text-[13px] text-[#DC2626]"
    >
      <AppIcon name="alert" :size="16" class="mt-0.5" />
      <span>{{ errorMessage }}</span>
    </div>

    <div v-if="loading" class="h-48 animate-pulse rounded-lg border border-[#E4E4E7] bg-white" />

    <section v-else class="rounded-lg border border-[#E4E4E7] bg-white">
      <div class="border-b border-[#E4E4E7] px-4 py-3">
        <div class="flex flex-wrap items-start justify-between gap-2">
          <div>
            <h2 class="text-[15px] font-semibold text-[#09090B]">Audit runs ({{ runs.length }})</h2>
            <p class="mt-1 break-words text-xs leading-5 text-[#52525B]" aria-live="polite">
              Scope and filters: {{ filterSummary }}
            </p>
          </div>
          <Button v-if="hasActiveFilters" class="min-h-11 px-3 text-xs" @click="clearFilters">Clear filters</Button>
        </div>
      </div>

      <div v-if="runs.length === 0" class="p-8 text-center text-[13px] text-[#71717A]">
        <template v-if="hasActiveFilters">
          No runs match these filters. Clear filters or change your search.
        </template>
        <template v-else>
          No audit runs are recorded in this tenant scope yet.
        </template>
      </div>

      <div v-else class="overflow-x-auto table-scroll">
        <table class="w-full border-collapse text-left text-[13px]">
          <thead>
            <tr class="border-b border-[#E4E4E7] bg-[#F4F4F5] text-xs font-medium text-[#52525B]">
              <th class="px-4 py-2.5 whitespace-nowrap">Run ID</th>
              <th class="px-4 py-2.5 whitespace-nowrap">Mode</th>
              <th class="px-4 py-2.5 whitespace-nowrap">Caller / Groups</th>
              <th class="px-4 py-2.5 whitespace-nowrap">Run result</th>
              <th class="px-4 py-2.5 whitespace-nowrap text-right">Subcalls</th>
              <th class="px-4 py-2.5 whitespace-nowrap text-right">Estimated token reduction</th>
              <th class="px-4 py-2.5 whitespace-nowrap text-right">Duration</th>
              <th class="px-4 py-2.5 whitespace-nowrap text-right">Details</th>
            </tr>
          </thead>
          <tbody class="divide-y divide-[#E4E4E7]">
            <tr v-for="r in runs" :key="r.run_id" class="hover:bg-[#FAFAFA]">
              <td class="px-4 py-2.5 font-mono-data text-xs text-[#09090B] whitespace-nowrap">
                {{ r.run_id.slice(0, 12) }}
              </td>
              <td class="px-4 py-2.5 font-mono-data text-xs whitespace-nowrap" :class="r.mode.startsWith('codemode') ? 'text-[#0F766E]' : 'text-[#2563EB]'">
                {{ r.mode }}
              </td>
              <td class="px-4 py-2.5 text-xs whitespace-nowrap">
                <div class="text-[#09090B]">{{ r.user_email || r.user_sub }}</div>
                <div class="text-[11px] text-[#71717A]">{{ (r.user_groups || []).join(", ") || "—" }}</div>
              </td>
              <td class="px-4 py-2.5 whitespace-nowrap">
                <span
                  class="rounded border px-2 py-0.5 text-xs font-medium"
                  :class="runOutcomeClass(r)"
                >
                  {{ runOutcomeLabel(r) }}
                </span>
                <span v-if="r.grounding_blocked" class="mt-1 block text-[11px] text-[#92400E]">Grounding check blocked</span>
              </td>
              <td class="px-4 py-2.5 text-right tabular-nums whitespace-nowrap">{{ r.subcall_count }}</td>
              <td class="px-4 py-2.5 text-right tabular-nums whitespace-nowrap" :class="reductionCredit(r) > 0 ? 'text-[#0F766E]' : 'text-[#71717A]'" :title="reductionCreditNote(r)">
                {{ reductionCredit(r).toLocaleString("en-US") }}
              </td>
              <td class="px-4 py-2.5 text-right tabular-nums whitespace-nowrap">{{ r.duration_ms.toFixed(1) }} ms</td>
              <td class="px-4 py-2.5 text-right whitespace-nowrap">
                <Button @click="inspectRun(r.run_id)">Inspect</Button>
              </td>
            </tr>
          </tbody>
        </table>
      </div>
      <div class="flex flex-wrap items-center justify-between gap-2 border-t border-[#E4E4E7] bg-[#FAFAFA] px-4 py-2.5 text-xs text-[#52525B]">
        <span class="tabular-nums">
          Showing {{ runs.length ? offset + 1 : 0 }}–{{ offset + runs.length }} of {{ totalCount }} total runs
        </span>
        <div class="flex items-center gap-2">
          <Button class="px-2.5 py-1 text-xs" :disabled="offset === 0" @click="prevPage">
            Previous
          </Button>
          <Button
            class="px-2.5 py-1 text-xs"
            :disabled="offset + runs.length >= totalCount"
            @click="nextPage"
          >
            Next
          </Button>
        </div>
      </div>
    </section>

    <Drawer v-model:open="drawerOpen" side="right" prevent-edge-swipe>
      <DrawerBackdrop />
      <DrawerContent class="flex h-dvh w-full max-w-[95vw] flex-col gap-0 border-l border-[#E4E4E7] bg-white p-0 sm:w-[36rem]">
        <div class="flex shrink-0 items-center justify-between border-b border-[#E4E4E7] bg-[#FAFAFA] px-5 py-4">
          <div class="min-w-0">
            <div class="flex items-center gap-2">
              <DrawerTitle class="font-mono-data text-sm font-semibold text-[#09090B]">
                {{ selectedRun?.run_id.slice(0, 16) }}
              </DrawerTitle>
              <span
                v-if="selectedRun"
                class="rounded border px-2 py-0.5 text-[11px] font-medium"
                :class="runOutcomeClass(selectedRun)"
              >
                {{ runOutcomeLabel(selectedRun) }}
              </span>
            </div>
            <DrawerDescription class="mt-0.5 truncate text-xs text-[#52525B]">
              {{ selectedRun?.mode }} · {{ selectedRun?.user_email || selectedRun?.user_sub }}
            </DrawerDescription>
          </div>
          <DrawerClose aria-label="Close inspector" class="min-h-[44px] min-w-[44px] shrink-0 rounded-md px-2 focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-[#0F766E]">
            <AppIcon name="x" :size="16" />
          </DrawerClose>
        </div>

        <div class="min-h-0 flex-1 overflow-y-auto overflow-x-hidden p-5">
          <div v-if="selectedRun" class="space-y-6 text-xs">
          <dl class="grid grid-cols-2 gap-x-4 gap-y-3 sm:grid-cols-3">
            <div class="border-b border-[#E4E4E7] pb-2 sm:border-b-0 sm:border-r sm:pr-3">
              <dt class="text-[11px] text-[#71717A]">Run result</dt>
              <dd class="mt-1 text-sm font-semibold" :class="selectedRun.ok ? 'text-[#059669]' : 'text-[#DC2626]'">{{ runOutcomeLabel(selectedRun) }}</dd>
            </div>
            <div class="border-b border-[#E4E4E7] pb-2 sm:border-b-0 sm:border-r sm:pr-3">
              <dt class="text-[11px] text-[#71717A]">Grounding check</dt>
              <dd class="mt-1 text-sm font-semibold" :class="groundingStatusClass(selectedRun)">{{ groundingStatusLabel(selectedRun) }}</dd>
            </div>
            <div class="border-b border-[#E4E4E7] pb-2 sm:border-b-0 sm:border-r sm:pr-3">
              <dt class="text-[11px] text-[#71717A]">Duration</dt>
              <dd class="mt-1 text-sm font-semibold tabular-nums text-[#09090B]">{{ selectedRun.duration_ms.toFixed(1) }} ms</dd>
            </div>
            <div class="border-b border-[#E4E4E7] pb-2 sm:border-b-0 sm:border-r sm:pr-3">
              <dt class="text-[11px] text-[#71717A]">Estimated tokens used</dt>
              <dd class="mt-1 text-sm font-semibold tabular-nums text-[#09090B]">{{ (selectedRun.tokens_used_est ?? 0).toLocaleString("en-US") }}</dd>
            </div>
            <div class="border-b border-[#E4E4E7] pb-2 sm:border-b-0 sm:border-r sm:pr-3">
              <dt class="text-[11px] text-[#71717A]">Estimated token reduction</dt>
              <dd class="mt-1 text-sm font-semibold tabular-nums" :class="reductionCredit(selectedRun) > 0 ? 'text-[#0F766E]' : 'text-[#71717A]'">{{ reductionCredit(selectedRun).toLocaleString("en-US") }}</dd>
            </div>
            <div>
              <dt class="text-[11px] text-[#71717A]">Subcalls</dt>
              <dd class="mt-1 text-sm font-semibold tabular-nums text-[#09090B]">{{ selectedRun.subcall_count }}</dd>
            </div>
          </dl>

          <section class="space-y-1.5 border-t border-[#E4E4E7] pt-4">
            <h3 class="font-semibold text-[#09090B]">Token reduction attribution</h3>
            <p class="text-[11px] leading-5" :class="reductionCredit(selectedRun) > 0 ? 'text-[#0F766E]' : 'text-[#92400E]'">{{ reductionCreditNote(selectedRun) }}</p>
            <p v-if="reductionCredit(selectedRun) > 0 && selectedRun.tokens_saved_by" class="text-[11px] leading-5 text-[#52525B]">{{ selectedRun.tokens_saved_by }}</p>
            <p v-if="selectedRun.monty_spawn_ms || selectedRun.monty_exec_ms" class="text-[11px] text-[#52525B] tabular-nums">Monty spawn {{ (selectedRun.monty_spawn_ms ?? 0).toFixed(2) }} ms · execution {{ (selectedRun.monty_exec_ms ?? 0).toFixed(2) }} ms</p>
          </section>

          <section v-if="selectedRun.trace_spans?.length" class="space-y-3 border-t border-[#E4E4E7] pt-4">
            <div class="flex items-center justify-between gap-3">
              <h3 class="font-semibold text-[#09090B]">Request latency waterfall</h3>
              <div class="text-[11px] text-[#71717A] tabular-nums">Total {{ selectedRun.duration_ms.toFixed(1) }} ms</div>
            </div>
            <p v-if="bottleneck" class="text-[11px] leading-5 text-[#52525B]">
              Longest recorded stage: <strong class="text-[#09090B]">{{ bottleneck.span.label }}</strong>
              · {{ bottleneck.span.duration_ms.toFixed(2) }} ms ({{ bottleneck.pct }}% of request duration).
            </p>
            <div class="grid grid-cols-[7.5rem_1fr_4rem] sm:grid-cols-[11rem_1fr_4.5rem] items-center gap-2 text-[9px] text-[#71717A] tabular-nums">
              <span>Elapsed time</span>
              <div class="grid grid-cols-5">
                <span v-for="tick in timelineTicks" :key="tick" :class="tick === 0 ? 'text-left' : tick === 100 ? 'text-right' : 'text-center'">{{ timelineTickLabel(selectedRun, tick) }}</span>
              </div>
              <span class="text-right">Duration</span>
            </div>
            <div class="space-y-2">
              <div
                v-for="(sp, idx) in selectedRun.trace_spans"
                :key="idx"
                class="grid grid-cols-[7.5rem_1fr_4rem] sm:grid-cols-[11rem_1fr_4.5rem] items-center gap-2 py-1 text-[11px]"
                :title="`${sp.label} (${sp.stage}) · start +${sp.start_ms.toFixed(2)}ms · duration ${sp.duration_ms.toFixed(2)}ms · status: ${sp.status}`"
              >
                <div class="truncate font-medium text-[#09090B]">{{ sp.label }}</div>
                <div class="relative h-3 rounded bg-[#F4F4F5] overflow-hidden">
                  <div
                    class="absolute top-0 bottom-0 rounded"
                    :class="spanBarColor(sp.stage, sp.status)"
                    :style="spanBarStyle(sp, selectedRun.duration_ms)"
                  />
                </div>
                <div class="text-right font-mono-data text-[#52525B] tabular-nums">
                  {{ sp.duration_ms.toFixed(2) }} ms
                </div>
              </div>
            </div>
          </section>

          <section class="grid grid-cols-2 gap-x-5 gap-y-4 border-t border-[#E4E4E7] pt-4">
            <div>
              <div class="text-[#71717A]">How mode was selected</div>
              <div class="mt-0.5 leading-5 text-[#09090B]">
                {{ decisionExplanation(selectedRun) }}
              </div>
            </div>
            <div>
              <div class="text-[#71717A]">Groups</div>
              <div class="mt-0.5 font-mono-data text-[#09090B]">
                {{ (selectedRun.user_groups || []).join(", ") || "—" }}
              </div>
            </div>
            <div>
              <div class="text-[#71717A]">Security Filters</div>
              <div class="mt-0.5 text-[#09090B] tabular-nums">
                ABAC: {{ selectedRun.abac_rows_filtered }} · PII: {{ selectedRun.redactions_count }}
              </div>
            </div>
          </section>

          <section v-if="selectedRun.error" class="space-y-1.5 border-t border-[#E4E4E7] pt-4">
            <div class="font-semibold" :class="selectedRun.grounding_blocked ? 'text-[#92400E]' : 'text-[#DC2626]'">
              {{ selectedRun.grounding_blocked ? "Grounding Violation Blocked" : "Execution Error" }}
            </div>
            <pre
              class="overflow-x-auto rounded border p-3 font-mono-data text-xs whitespace-pre-wrap"
              :class="
                selectedRun.grounding_blocked
                  ? 'border-[#FDE68A] bg-[#FFFBEB] text-[#92400E]'
                  : 'border-[#FECACA] bg-[#FEF2F2] text-[#DC2626]'
              "
            >{{ selectedRun.error }}</pre>
          </section>

          <section v-if="selectedRun.code" class="space-y-1.5 border-t border-[#E4E4E7] pt-4">
            <div class="font-semibold text-[#09090B]">Executed Monty Python</div>
            <pre class="overflow-x-auto rounded border border-[#E4E4E7] bg-[#FAFAFA] p-3 font-mono-data text-xs text-[#09090B]">{{ selectedRun.code }}</pre>
          </section>

          <section v-if="selectedRun.subcalls?.length" class="min-w-0 space-y-1.5 border-t border-[#E4E4E7] pt-4">
            <div class="font-semibold text-[#09090B]">Upstream Subcalls ({{ selectedRun.subcalls.length }})</div>
            <div class="divide-y divide-[#E4E4E7] border-y border-[#E4E4E7]">
              <div v-for="sc in selectedRun.subcalls" :key="`${sc.server}.${sc.tool}.${sc.raw_bytes}.${sc.duration_ms}`" class="grid min-w-0 grid-cols-2 gap-x-3 gap-y-1.5 py-2 sm:grid-cols-[minmax(0,1fr)_auto_auto_auto] sm:items-center">
                <div class="col-span-2 min-w-0 break-words font-mono-data text-[11px] text-[#09090B] sm:col-span-1">{{ sc.server }}.{{ sc.tool }}</div>
                <div class="text-[11px]" :class="subcallStatusClass(sc.status)">
                  <span class="text-[#71717A]">Result:</span> {{ subcallStatusLabel(sc.status) }}
                </div>
                <div class="text-right text-[11px] tabular-nums text-[#52525B]">{{ sc.raw_bytes.toLocaleString("en-US") }} bytes</div>
                <div class="text-right text-[11px] tabular-nums text-[#52525B]">{{ sc.duration_ms.toFixed(1) }} ms</div>
                <p v-if="sc.error" class="col-span-full break-words text-[11px] leading-5 text-[#DC2626]">{{ sc.error }}</p>
              </div>
            </div>
          </section>

          <details class="border-t border-[#E4E4E7] pt-4">
            <summary class="cursor-pointer font-medium text-[#09090B]">Raw Audit JSON</summary>
            <pre class="mt-2 max-h-64 overflow-auto rounded border border-[#E4E4E7] bg-[#FAFAFA] p-3 font-mono-data text-[11px] text-[#09090B]">{{ JSON.stringify(selectedRun, null, 2) }}</pre>
          </details>
          </div>
        </div>
      </DrawerContent>
    </Drawer>

    <Dialog v-model:open="pruneDialogOpen">
      <DialogBackdrop />
      <DialogContent>
        <DialogTitle>Prune aged audit runs?</DialogTitle>
        <DialogDescription>
          Permanently deletes execution telemetry and subcall records older than the specified retention window.
        </DialogDescription>
        <Field>
          <FieldLabel>Retention Window (days)</FieldLabel>
          <Input v-model="retentionDays" type="number" min="0" max="3650" step="1" inputmode="numeric" />
          <FieldDescription>Use 0 to delete all recorded runs immediately. Enter a whole number up to 3,650.</FieldDescription>
        </Field>
        <div class="rounded-md border border-[#E4E4E7] bg-[#FAFAFA] p-3 text-xs leading-5 text-[#52525B]" aria-live="polite">
          <p class="font-medium text-[#09090B]">Prune preview</p>
          <p>{{ pruneCutoffPreview }}</p>
          <p v-if="parsedRetentionDays !== null" class="mt-1">This action applies to all tenants in the gateway audit store; current list filters do not limit it. The exact deleted count is returned after pruning and is not available as a preview.</p>
        </div>
        <div class="flex justify-end gap-2 pt-2">
          <DialogClose>Cancel</DialogClose>
          <Button
            data-variant="danger"
            :disabled="parsedRetentionDays === null"
            data-agent-confirm="Permanently delete aged audit runs from storage?"
            pending-announcement="Pruning runs"
            @click="confirmPrune"
          >
            Confirm Prune
          </Button>
        </div>
      </DialogContent>
    </Dialog>
  </div>
</template>
