<script setup lang="ts">
import { Button } from "@ui-sdk/vue/button";
import {
  Meter,
  MeterIndicator,
  MeterLabel,
  MeterTrack,
  MeterValue,
} from "@ui-sdk/vue/meter";
import { computed, onBeforeUnmount, onMounted, ref, watch } from "vue";
import { extractErrorMessage } from "~/composables/useAdminApi";

interface ProcessStats {
  cpu_percent: number;
  rss_mb: number;
  load_avg_1m: number;
  cpu_count: number;
  uptime_seconds: number;
  active_executions: number;
  total_executions: number;
}

interface SandboxWorkerPool {
  min_processes?: number;
  max_processes?: number;
  max_concurrent_executions?: number;
}

interface SandboxWorkerEntry {
  worker_id?: string;
  mode: string;
  status: string;
  health_url?: string;
  error?: string;
  pool?: SandboxWorkerPool;
  stats?: ProcessStats;
}

interface MontyTelemetry {
  ephemeral?: boolean;
  codemode_runs?: number;
  grounding_blocked_runs?: number;
  spawn_p50_ms?: number;
  spawn_p95_ms?: number;
  exec_p50_ms?: number;
  exec_p95_ms?: number;
}

interface RuntimeTelemetryResponse {
  gateway: ProcessStats & {
    profile: string;
    app_env: string;
    auth_mode: string;
    catalog_version: string;
    upstreams_count: number;
    cache_backend: string;
    store_backend: string;
  };
  sandbox_worker: SandboxWorkerEntry & {
    worker_count?: number;
    healthy_count?: number;
  };
  sandbox_workers?: SandboxWorkerEntry[];
  monty_telemetry?: MontyTelemetry;
}

interface UpstreamBreakdownRow {
  server: string;
  calls: number;
  errors?: number;
  raw_bytes: number;
  abac_rows_filtered: number;
  redactions_count: number;
  classic_calls: number;
  codemode_calls: number;
  avg_duration_ms: number;
}

interface AuditValueResponse {
  window_runs: number;
  efficiency: {
    total_subcalls: number;
    total_raw_upstream_bytes: number;
    total_returned_bytes: number;
    payload_reduction_pct: number;
    payload_tokens_saved: number;
    schema_tokens_saved: number;
    total_tokens_saved: number;
    estimated_llm_cost_saved_usd: number;
    llm_turns_avoided: number;
    context_overflows_prevented: number;
    obo_cache_hit_rate_pct: number;
  };
  security_and_governance: {
    abac_rows_filtered: number;
    pii_secrets_redacted: number;
    grounded_runs_verified: number;
    grounding_violations_blocked: number;
  };
  by_upstream?: Record<string, Omit<UpstreamBreakdownRow, "server">>;
  upstream_breakdown?: UpstreamBreakdownRow[];
  recommendations?: { server: string; reason: string }[];
}

interface AnalyticsSummary {
  summary: {
    total_runs: number;
    classic_runs: number;
    codemode_runs: number;
    codemode_share_pct: number;
  };
}

interface RecoverySummary {
  open: number;
  needs_attention: number;
  completed_24h: number;
  failed_24h: number;
  failure_rate_24h_pct: number | null;
  upstream_health: { status: string }[];
}

interface BusinessValueReport {
  completed_count: number;
  summary?: {
    estimated_manual_baseline_minutes?: number | null;
  };
  by_integration_workflow_day?: {
    estimated_manual_baseline_minutes?: number | null;
  }[];
  totals_by_currency?: Record<
    string,
    {
      completed_count?: number;
      estimated_manual_baseline_minutes?: number;
    }
  >;
}

interface WorkflowsCatalogResponse {
  upstreams?: string[];
  workflows?: {
    upstream_name: string;
    workflow?: {
      id?: string;
      name?: string;
      enabled?: boolean;
    };
  }[];
  candidates?: {
    candidate_id: string;
    status: string;
  }[];
  episodes?: unknown[];
}

interface GoldenRunResult {
  evidence_rows?: number;
  traffic_requests?: number;
  traffic_errors?: number;
  recovery_operations_created?: number;
  business_value_workflow?: {
    status?: string;
    completion_count?: number;
    manual_baseline_minutes?: number;
  };
}

const { apiFetch, setRuntimeMeta } = useAdminApi();
const { status: demoStatus, busy: demoBusy, runBurst } = useDemoTraffic();
const {
  connected: wsConnected,
  connectionState: wsConnectionState,
  events: liveEvents,
  latestRuntime,
  alerts: liveAlerts,
  totalRunsTick,
} = useLiveTelemetry();

const loading = ref(true);
const summaryRefreshing = ref(false);
const errorMessage = ref("");
const summaryUpdatedAt = ref("");
const runtimeData = ref<RuntimeTelemetryResponse | null>(null);
const runtimeUpdatedAt = ref("");
const runtimeError = ref("");
const runtimeLoading = ref(false);
const valueData = ref<AuditValueResponse | null>(null);
const analyticsData = ref<AnalyticsSummary | null>(null);
const recoveryData = ref<RecoverySummary | null>(null);
const businessValueData = ref<BusinessValueReport | null>(null);
const workflowsCatalogData = ref<WorkflowsCatalogResponse | null>(null);

const goldenBusy = ref(false);
const goldenNotice = ref("");
const goldenError = ref("");

let refreshDebounce: ReturnType<typeof setTimeout> | null = null;

async function triggerOverviewBurst() {
  goldenNotice.value = "";
  goldenError.value = "";
  await runBurst(8);
  await fetchOverview();
}

async function triggerGoldenDemo() {
  if (goldenBusy.value || demoBusy.value) return;
  goldenBusy.value = true;
  goldenNotice.value = "";
  goldenError.value = "";
  try {
    const result = await apiFetch<GoldenRunResult>("/admin/demo/golden-run", {
      method: "POST",
      timeout: 60000,
    });
    const requests = Number(result?.traffic_requests ?? 0);
    const evidenceRows = Number(result?.evidence_rows ?? 0);
    goldenNotice.value = requests || evidenceRows
      ? `Golden demo completed: ${formatInt(requests)} MCP calls and ${formatInt(evidenceRows)} simulated report rows recorded.`
      : "Golden demo completed.";
    await fetchOverview();
  } catch (err) {
    goldenError.value = extractErrorMessage(err);
  } finally {
    goldenBusy.value = false;
  }
}

async function fetchOverview() {
  if (summaryRefreshing.value) return;
  summaryRefreshing.value = true;
  errorMessage.value = "";
  try {
    const [valRes, anaRes, recRes, bvRes, wfRes] = await Promise.allSettled([
      apiFetch<AuditValueResponse>("/audit/value"),
      apiFetch<AnalyticsSummary>("/audit/analytics"),
      apiFetch<RecoverySummary>("/admin/operations/metrics", {}, true),
      apiFetch<BusinessValueReport>("/admin/business-value/report", {}, true),
      apiFetch<WorkflowsCatalogResponse>("/admin/workflows/catalog", {}, true),
    ]);

    valueData.value = valRes.status === "fulfilled" ? valRes.value : null;
    analyticsData.value = anaRes.status === "fulfilled" ? anaRes.value : null;
    recoveryData.value = recRes.status === "fulfilled" ? recRes.value : null;
    businessValueData.value = bvRes.status === "fulfilled" ? bvRes.value : null;
    workflowsCatalogData.value = wfRes.status === "fulfilled" ? wfRes.value : null;

    if (valRes.status === "fulfilled" || anaRes.status === "fulfilled") {
      summaryUpdatedAt.value = new Date().toISOString();
    }
    if (valRes.status === "rejected") {
      errorMessage.value = extractErrorMessage(valRes.reason);
    } else if (anaRes.status === "rejected") {
      errorMessage.value = extractErrorMessage(anaRes.reason);
    }
  } catch (err) {
    errorMessage.value = extractErrorMessage(err);
  } finally {
    loading.value = false;
  }
  await fetchRuntimeTelemetry();
  summaryRefreshing.value = false;
}

async function fetchRuntimeTelemetry() {
  if (runtimeLoading.value) return;
  runtimeLoading.value = true;
  runtimeError.value = "";
  try {
    const rt = await apiFetch<RuntimeTelemetryResponse>("/admin/telemetry/runtime", {}, true);
    runtimeData.value = rt;
    runtimeUpdatedAt.value = new Date().toISOString();
    setRuntimeMeta({
      profile: rt.gateway.profile,
      app_env: rt.gateway.app_env,
      catalog_version: rt.gateway.catalog_version,
    });
  } catch (err) {
    runtimeError.value = extractErrorMessage(err);
  } finally {
    runtimeLoading.value = false;
  }
}

watch(latestRuntime, (rt) => {
  if (rt && typeof rt === "object" && "gateway" in rt) {
    runtimeData.value = rt as unknown as RuntimeTelemetryResponse;
    runtimeUpdatedAt.value = new Date().toISOString();
    runtimeError.value = "";
  }
});

watch(totalRunsTick, () => {
  if (refreshDebounce) return;
  refreshDebounce = setTimeout(() => {
    refreshDebounce = null;
    void fetchOverview();
  }, 5000);
});

onMounted(() => {
  void fetchOverview();
});

onBeforeUnmount(() => {
  if (refreshDebounce) {
    clearTimeout(refreshDebounce);
    refreshDebounce = null;
  }
});

const normalizedSandboxWorkers = computed<SandboxWorkerEntry[]>(() => {
  const rt = runtimeData.value;
  if (!rt) return [];
  if (Array.isArray(rt.sandbox_workers) && rt.sandbox_workers.length > 0) {
    return rt.sandbox_workers;
  }
  if (rt.sandbox_worker) {
    return [
      {
        worker_id: "primary",
        ...rt.sandbox_worker,
      },
    ];
  }
  return [];
});

const totalWorkerCount = computed(() => {
  const rt = runtimeData.value;
  if (!rt) return 0;
  if (Array.isArray(rt.sandbox_workers) && rt.sandbox_workers.length > 0) {
    return rt.sandbox_workers.length;
  }
  return Math.max(normalizedSandboxWorkers.value.length, rt.sandbox_worker?.worker_count ?? 0);
});

const healthyWorkerCount = computed(() => {
  const rt = runtimeData.value;
  if (!rt) return 0;
  if (Array.isArray(rt.sandbox_workers) && rt.sandbox_workers.length > 0) {
    return rt.sandbox_workers.filter((w) => w.status === "ok" || w.status === "in_process").length;
  }
  if (typeof rt.sandbox_worker?.healthy_count === "number") {
    return rt.sandbox_worker.healthy_count;
  }
  return normalizedSandboxWorkers.value.filter(
    (w) => w.status === "ok" || w.status === "in_process",
  ).length;
});

const upstreamRows = computed<UpstreamBreakdownRow[]>(() => {
  if (Array.isArray(valueData.value?.upstream_breakdown)) {
    return valueData.value.upstream_breakdown;
  }
  const entries = Object.entries(valueData.value?.by_upstream || {});
  return entries.map(([server, info]) => ({ server, ...info }));
});

const totalWorkflowsCount = computed(() => workflowsCatalogData.value?.workflows?.length ?? 0);
const activeWorkflowsCount = computed(
  () =>
    (workflowsCatalogData.value?.workflows ?? []).filter(
      (entry) => entry.workflow?.enabled !== false,
    ).length,
);
const pendingCandidatesCount = computed(
  () =>
    (workflowsCatalogData.value?.candidates ?? []).filter(
      (cand) => cand.status === "pending_review",
    ).length,
);

const estimatedBaselineMinutes = computed<number | null>(() => {
  const bv = businessValueData.value;
  if (!bv) return null;
  const rows = bv.by_integration_workflow_day;
  if (Array.isArray(rows) && rows.length > 0) {
    if (rows.some((row) => row.estimated_manual_baseline_minutes == null)) return null;
    return rows.reduce((sum, row) => sum + (row.estimated_manual_baseline_minutes ?? 0), 0);
  }
  if (typeof bv.summary?.estimated_manual_baseline_minutes === "number") {
    return bv.summary.estimated_manual_baseline_minutes;
  }
  if (bv.totals_by_currency && Object.keys(bv.totals_by_currency).length > 0) {
    return Object.values(bv.totals_by_currency).reduce(
      (sum, item) => sum + (item.estimated_manual_baseline_minutes ?? 0),
      0,
    );
  }
  return 0;
});

const pairedModeRunCount = computed(() =>
  (analyticsData.value?.summary.codemode_runs || 0) + (analyticsData.value?.summary.classic_runs || 0),
);
const otherModeRunCount = computed(() => Math.max(
  0,
  (analyticsData.value?.summary.total_runs || 0) - pairedModeRunCount.value,
));
const codemodeSharePct = computed(() => pairedModeRunCount.value
  ? Math.round(((analyticsData.value?.summary.codemode_runs || 0) / pairedModeRunCount.value) * 1000) / 10
  : 0);
const classicSharePct = computed(() => pairedModeRunCount.value
  ? Math.round(((analyticsData.value?.summary.classic_runs || 0) / pairedModeRunCount.value) * 1000) / 10
  : 0);

const executionMixStyle = computed(() => ({
  background: `conic-gradient(#0F766E 0 ${codemodeSharePct.value}%, #2563EB ${codemodeSharePct.value}% 100%)`,
}));

function formatInt(n: number | undefined): string {
  return Number(n || 0).toLocaleString("en-US");
}

function formatUsd(n: number | undefined): string {
  return `$${Number(n || 0).toFixed(4)}`;
}

function formatUtcTime(value: string): string {
  const date = new Date(value);
  return Number.isNaN(date.getTime()) ? "time unavailable" : `${date.toISOString().slice(0, 19).replace("T", " ")} UTC`;
}

function alertRunsPath(alert: { id: string; filter_status?: string }): string | null {
  const status = alert.filter_status;
  if (status === "errors" || status === "grounding_blocked" || status === "ok") {
    return `/audit?status=${status}`;
  }
  return alert.id === "high_error_rate" ? "/audit?status=errors" : null;
}

function cpuCapacity(stats: ProcessStats): number {
  return Math.max(1, stats.cpu_count) * 100;
}

function formatCpu(n: number): string {
  return `${Number(n || 0).toFixed(1)}% of one vCPU`;
}

function workerStatusLabel(status: string): string {
  if (status === "in_process") return "Shared process";
  if (status === "ok") return "Healthy";
  if (status === "unreachable") return "Unreachable";
  if (status === "degraded") return "Degraded";
  return status;
}

function workerStatusBadgeClass(status: string): string {
  if (status === "unreachable") {
    return "border-[#FECACA] bg-[#FEF2F2] text-[#DC2626]";
  }
  if (status === "degraded") {
    return "border-[#FDE68A] bg-[#FFFBEB] text-[#D97706]";
  }
  return "border-[#A7F3D0] bg-[#ECFDF5] text-[#059669]";
}

function isSharedWorker(worker: SandboxWorkerEntry): boolean {
  return worker.status === "in_process" || worker.mode === "in_process";
}

function hasPoolConfig(worker: SandboxWorkerEntry): boolean {
  return Boolean(
    worker.pool &&
      (worker.pool.min_processes != null ||
        worker.pool.max_processes != null ||
        worker.pool.max_concurrent_executions != null),
  );
}
</script>

<template>
  <div class="space-y-6">
    <div class="flex flex-col gap-3 sm:flex-row sm:items-center sm:justify-between">
      <div>
        <h1 class="text-[20px] font-semibold tracking-[-0.015em] text-[#09090B]">
          Overview &amp; Live Runtime
        </h1>
        <p class="mt-0.5 text-[13px] text-[#52525B]">
          Gateway and Monty worker CPU/RAM telemetry, value attribution, and governance enforcement.
        </p>
      </div>
      <div class="flex flex-wrap items-center gap-3">
        <span
          role="status"
          aria-live="polite"
          class="inline-flex items-center gap-1.5 rounded-full border px-2.5 py-0.5 text-xs font-medium"
          :class="
            wsConnected
              ? 'border-[#99F6E4] bg-[#F0FDFA] text-[#0F766E]'
              : 'border-[#E4E4E7] bg-white text-[#52525B]'
          "
        >
          <span
            class="inline-block h-1.5 w-1.5 rounded-full"
            :class="wsConnected ? 'bg-[#0F766E] animate-pulse' : 'bg-[#D97706]'"
          />
          <span>
            {{
              wsConnectionState === "live"
                ? "Live"
                : wsConnectionState === "reconnecting"
                  ? "Reconnecting..."
                  : wsConnectionState === "disconnected"
                    ? "Offline"
                    : "Connecting..."
            }}
          </span>
        </span>
        <Button :disabled="summaryRefreshing" class="min-h-11" data-variant="primary" pending-announcement="Refreshing metrics" @click="fetchOverview">
          <AppIcon name="refresh" :size="15" />
          <span>{{ summaryRefreshing ? "Refreshing…" : "Refresh summary" }}</span>
        </Button>
      </div>
    </div>

    <p class="-mt-4 text-[11px] leading-5 text-[#71717A]" aria-live="polite">
      Summary data refreshed: {{ summaryUpdatedAt ? formatUtcTime(summaryUpdatedAt) : "not yet refreshed" }}
      <span v-if="wsConnectionState === 'live'"> · Activity stream connected</span>
      <span v-else> · Activity stream {{ wsConnectionState }}</span>
    </p>

    <div
      v-if="errorMessage"
      role="alert"
      class="flex items-start gap-2 rounded-lg border border-[#FECACA] bg-[#FEF2F2] px-4 py-3 text-[13px] text-[#DC2626]"
    >
      <AppIcon name="alert" :size="16" class="mt-0.5" />
      <span>{{ errorMessage }}</span>
    </div>

    <div
      v-if="goldenNotice"
      role="status"
      class="rounded-lg border border-[#99F6E4] bg-[#F0FDFA] px-4 py-3 text-[13px] text-[#0F766E]"
    >
      {{ goldenNotice }}
    </div>

    <div
      v-if="goldenError"
      role="alert"
      class="flex items-start gap-2 rounded-lg border border-[#FECACA] bg-[#FEF2F2] px-4 py-3 text-[13px] text-[#DC2626]"
    >
      <AppIcon name="alert" :size="16" class="mt-0.5" />
      <span>Golden demo failed: {{ goldenError }}</span>
    </div>

    <div v-if="loading" class="grid grid-cols-1 gap-4 sm:grid-cols-2 lg:grid-cols-4">
      <div
        v-for="i in 4"
        :key="i"
        class="h-24 animate-pulse rounded-lg border border-[#E4E4E7] bg-white"
      />
    </div>

    <template v-else>
      <section
        v-if="demoStatus.enabled && valueData && analyticsData && valueData.window_runs === 0 && analyticsData.summary.total_runs === 0"
        class="flex flex-col gap-3 rounded-lg border border-[#99F6E4] bg-[#F0FDFA] p-4 sm:flex-row sm:items-center sm:justify-between"
      >
        <div class="text-[13px] text-[#09090B]">
          <span class="font-semibold text-[#0F766E]">No /mcp runs recorded yet.</span>
          <span class="ml-1 text-[#52525B]">
            Generate real Classic, Code Mode, Auto, and custom-upstream traffic to inspect live telemetry and ROI.
          </span>
        </div>
        <div class="flex flex-wrap items-center gap-2">
          <Button data-variant="primary" class="min-h-11" :disabled="demoBusy || goldenBusy" @click="triggerOverviewBurst">
            Run Demo Burst (+8)
          </Button>
          <Button
            class="min-h-11 border border-[#D4D4D8] bg-white text-[#09090B] hover:bg-[#F4F4F5]"
            :disabled="demoBusy || goldenBusy"
            @click="triggerGoldenDemo"
          >
            {{ goldenBusy ? "Running Golden Demo…" : "Run Golden Demo" }}
          </Button>
        </div>
      </section>

      <section aria-label="Active monitoring alerts" class="space-y-2">
        <div
          v-if="liveAlerts.length === 0 && wsConnected"
          class="flex items-center justify-between gap-2 rounded-lg border border-[#A7F3D0] bg-[#ECFDF5] px-4 py-2.5 text-xs text-[#059669]"
        >
          <span class="font-medium">System Health Nominal — 0 active SLA, drift, or security alerts.</span>
          <NuxtLink to="/settings" class="font-medium text-[#0F766E] hover:underline">
            Configure alert rules
          </NuxtLink>
        </div>
        <div
          v-else-if="liveAlerts.length === 0"
          role="status"
          class="flex flex-wrap items-center justify-between gap-2 rounded-lg border border-[#FDE68A] bg-[#FFFBEB] px-4 py-2.5 text-xs text-[#92400E]"
        >
          <span class="font-medium">Alert status is unavailable while the live connection is {{ wsConnectionState }}.</span>
          <NuxtLink to="/settings" class="font-medium text-[#0F766E] hover:underline">
            Alert rules
          </NuxtLink>
        </div>
        <div
          v-for="al in liveAlerts"
          :key="al.id"
          role="alert"
          class="flex flex-col gap-2 rounded-lg border px-4 py-3 text-xs sm:flex-row sm:items-center sm:justify-between"
          :class="
            al.severity === 'critical'
              ? 'border-[#FECACA] bg-[#FEF2F2] text-[#DC2626]'
              : 'border-[#FDE68A] bg-[#FFFBEB] text-[#92400E]'
          "
        >
          <div class="space-y-0.5">
            <div>
              <strong class="font-semibold">{{ al.title }}</strong>
              <span class="ml-2 text-[#52525B]">{{ al.detail }}</span>
            </div>
            <div v-if="al.affected_upstreams?.length || al.affected_users?.length" class="text-[11px] text-[#52525B]">
              <span v-if="al.affected_upstreams?.length">Services: <code class="font-mono-data">{{ al.affected_upstreams.join(", ") }}</code></span>
              <span v-if="al.affected_users?.length" class="ml-2">Callers: <code class="font-mono-data">{{ al.affected_users.join(", ") }}</code></span>
            </div>
            <div v-if="al.remediation" class="text-[11px] text-[#09090B]">
              <strong>Action:</strong> {{ al.remediation }}
            </div>
            <div class="text-[11px] text-[#52525B]">Evaluated {{ formatUtcTime(al.observed_at) }}</div>
          </div>
          <div class="flex shrink-0 items-center gap-3">
            <NuxtLink
              v-if="alertRunsPath(al)"
              :to="alertRunsPath(al)!"
              class="font-semibold underline"
            >
              View affected runs
            </NuxtLink>
            <NuxtLink to="/settings" class="font-medium underline">Rules</NuxtLink>
          </div>
        </div>
      </section>

      <section v-if="recoveryData" aria-label="Operations recovery summary" class="flex flex-col gap-3 rounded-lg border border-[#E4E4E7] bg-white px-4 py-3 sm:flex-row sm:items-center sm:justify-between">
        <div class="min-w-0">
          <h2 class="text-[15px] font-semibold text-[#09090B]">Operations recovery</h2>
          <p class="mt-1 text-xs text-[#52525B] tabular-nums">{{ recoveryData.open }} open · {{ recoveryData.needs_attention }} needs attention · {{ recoveryData.completed_24h }} completed / {{ recoveryData.failed_24h }} failed in 24h<span v-if="recoveryData.failure_rate_24h_pct != null"> · {{ recoveryData.failure_rate_24h_pct }}% terminal failure rate</span><span v-if="recoveryData.upstream_health.some((row) => row.status === 'unhealthy' || row.status === 'degraded')"> · {{ recoveryData.upstream_health.filter((row) => row.status === 'unhealthy' || row.status === 'degraded').length }} upstream checks impaired</span></p>
        </div>
        <NuxtLink to="/operations" class="inline-flex min-h-11 shrink-0 items-center font-medium text-[#0F766E] underline underline-offset-2 focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-[#0F766E]">Review recovery activity</NuxtLink>
      </section>

      <section aria-label="Gateway value summary" class="grid grid-cols-1 gap-4 sm:grid-cols-2 lg:grid-cols-4">
        <div class="rounded-lg border border-[#E4E4E7] bg-white p-4">
          <div class="text-xs font-medium text-[#52525B]">Gateway-reported token reduction</div>
          <div class="mt-1.5 text-2xl font-semibold text-[#09090B] tabular-nums">
            {{ valueData ? formatInt(valueData.efficiency.total_tokens_saved) : "Unavailable" }}
          </div>
          <div class="mt-1 text-xs text-[#71717A] tabular-nums">
            <template v-if="valueData">Payload: {{ formatInt(valueData.efficiency.payload_tokens_saved) }} · Schema: {{ formatInt(valueData.efficiency.schema_tokens_saved) }}</template>
            <span v-else>Gateway report unavailable</span>
          </div>
        </div>

        <div class="rounded-lg border border-[#E4E4E7] bg-white p-4">
          <div class="text-xs font-medium text-[#52525B]">Gateway-estimated LLM cost reduction</div>
          <div class="mt-1.5 text-2xl font-semibold text-[#0F766E] tabular-nums">
            {{ valueData ? formatUsd(valueData.efficiency.estimated_llm_cost_saved_usd) : "Unavailable" }}
          </div>
          <div class="mt-1 text-xs text-[#71717A] tabular-nums">
            <template v-if="valueData">{{ formatInt(valueData.efficiency.llm_turns_avoided) }} turns avoided · {{ formatInt(valueData.efficiency.context_overflows_prevented) }} overflows prevented</template>
            <span v-else>Gateway report unavailable</span>
          </div>
        </div>

        <div class="rounded-lg border border-[#E4E4E7] bg-white p-4">
          <div class="text-xs font-medium text-[#52525B]">Payload Reduction</div>
          <div class="mt-1.5 text-2xl font-semibold text-[#09090B] tabular-nums">
            {{ valueData ? `${valueData.efficiency.payload_reduction_pct.toFixed(1)}%` : "Unavailable" }}
          </div>
          <div class="mt-1 text-xs text-[#71717A] tabular-nums">
            <template v-if="valueData">Across {{ formatInt(valueData.window_runs) }} runs · OBO cache {{ valueData.efficiency.obo_cache_hit_rate_pct.toFixed(0) }}%</template>
            <span v-else>Gateway report unavailable</span>
          </div>
        </div>

        <div class="rounded-lg border border-[#E4E4E7] bg-white p-4">
          <div class="text-xs font-medium text-[#52525B]">Security &amp; Grounding</div>
          <div class="mt-1.5 text-2xl font-semibold text-[#09090B] tabular-nums">
            {{ valueData ? `${formatInt(valueData.security_and_governance.grounded_runs_verified)} verified` : "Unavailable" }}
          </div>
          <div class="mt-1 text-xs text-[#71717A] tabular-nums">
            <template v-if="valueData">{{ formatInt(valueData.security_and_governance.grounding_violations_blocked) }} grounding blocks · {{ formatInt(valueData.security_and_governance.abac_rows_filtered) }} ABAC rows filtered</template>
            <span v-else>Gateway report unavailable</span>
          </div>
        </div>
      </section>
      <div class="-mt-3 flex flex-wrap items-start justify-between gap-2 text-[11px] leading-5 text-[#71717A]">
        <p v-if="valueData">Totals cover the latest {{ formatInt(valueData.window_runs) }} runs. Token and USD reductions are gateway estimates; USD uses configured model rates and is not reconciled against provider invoices.</p>
        <p v-else>Summary metrics are unavailable until the gateway report loads. Token and USD reductions are gateway estimates.</p>
        <div class="flex shrink-0 flex-wrap gap-x-3">
          <NuxtLink to="/business-value" class="min-h-11 py-2 font-medium text-[#0F766E] underline underline-offset-2">Review business outcomes</NuxtLink>
          <NuxtLink to="/audit" class="min-h-11 py-2 font-medium text-[#0F766E] underline underline-offset-2">Review audit activity</NuxtLink>
        </div>
      </div>

      <section
        aria-label="Live gateway activity stream"
        class="rounded-lg border border-[#E4E4E7] bg-white p-4"
      >
        <div class="flex flex-wrap items-center justify-between gap-2">
          <div>
            <h2 class="text-[15px] font-semibold text-[#09090B]">
              Live Gateway Activity Stream
            </h2>
            <p class="text-xs text-[#71717A]">
              Real-time WebSocket execution feed (summary metadata only; payloads redacted by policy).
            </p>
          </div>
          <span class="rounded border border-[#E4E4E7] bg-[#F4F4F5] px-2 py-0.5 text-xs font-medium text-[#52525B] tabular-nums">
            {{ liveEvents.length }} live events
          </span>
        </div>

        <div v-if="liveEvents.length === 0" class="py-5 text-center text-xs text-[#71717A]">
          Waiting for live /mcp executions over WebSocket…
        </div>
        <div v-else class="mt-3 max-h-56 overflow-x-auto overflow-y-auto rounded border border-[#E4E4E7] table-scroll">
          <table class="w-full border-collapse text-left text-xs">
            <thead>
              <tr class="border-b border-[#E4E4E7] bg-[#F4F4F5] text-[#52525B]">
                <th class="px-3 py-2 whitespace-nowrap">Group</th>
                <th class="px-3 py-2 whitespace-nowrap">Recorded (UTC)</th>
                <th class="px-3 py-2 whitespace-nowrap">Target / Tool</th>
                <th class="px-3 py-2 whitespace-nowrap">Mode</th>
                <th class="px-3 py-2 text-right whitespace-nowrap">Reported reduction</th>
                <th class="px-3 py-2 text-right whitespace-nowrap">Latency</th>
                <th class="px-3 py-2 whitespace-nowrap">Status</th>
                <th class="px-3 py-2 text-right whitespace-nowrap">Run</th>
              </tr>
            </thead>
            <tbody class="divide-y divide-[#E4E4E7]">
              <tr v-for="ev in liveEvents" :key="ev.run_id">
                <td class="px-3 py-1.5 font-medium text-[#09090B] whitespace-nowrap">{{ ev.company_group }}</td>
                <td class="px-3 py-1.5 text-[#71717A] tabular-nums whitespace-nowrap">{{ formatUtcTime(ev.created_at) }}</td>
                <td class="px-3 py-1.5 font-mono-data text-[#52525B] whitespace-nowrap">{{ ev.tool_name }}</td>
                <td class="px-3 py-1.5 whitespace-nowrap">
                  <span
                    class="rounded px-1.5 py-0.5 text-[11px] font-medium"
                    :class="ev.mode === 'codemode' ? 'bg-[#F0FDFA] text-[#0F766E]' : 'bg-[#EFF6FF] text-[#2563EB]'"
                  >
                    {{ ev.mode }}
                  </span>
                </td>
                <td class="px-3 py-1.5 text-right text-[#0F766E] tabular-nums whitespace-nowrap">{{ formatInt(ev.tokens_saved) }}</td>
                <td class="px-3 py-1.5 text-right text-[#52525B] tabular-nums whitespace-nowrap">{{ Number(ev.duration_ms || 0).toFixed(1) }} ms</td>
                <td class="px-3 py-1.5 whitespace-nowrap">
                  <span
                    class="rounded px-1.5 py-0.5 text-[11px] font-medium"
                    :class="ev.grounding_blocked ? 'bg-[#FEF2F2] text-[#DC2626]' : ev.ok ? 'bg-[#ECFDF5] text-[#059669]' : 'bg-[#FFFBEB] text-[#D97706]'"
                  >
                    {{ ev.grounding_blocked ? "GROUNDING BLOCKED" : ev.ok ? "OK" : "UNSUCCESSFUL (DENIED OR FAILED)" }}
                  </span>
                </td>
                <td class="px-3 py-1.5 text-right whitespace-nowrap">
                  <NuxtLink v-if="ev.run_id" :to="`/audit?run_id=${encodeURIComponent(ev.run_id)}`" class="inline-flex min-h-11 items-center py-2 font-medium text-[#0F766E] underline underline-offset-2 focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-[#0F766E]">
                    Inspect run
                  </NuxtLink>
                  <span v-else class="text-[#71717A]">Unavailable</span>
                </td>
              </tr>
            </tbody>
          </table>
        </div>
      </section>

      <section class="rounded-lg border border-[#E4E4E7] bg-white">
        <div class="flex flex-wrap items-center justify-between gap-2 border-b border-[#E4E4E7] px-4 py-3">
          <div class="flex flex-wrap items-center gap-2">
            <AppIcon name="cpu" :size="16" class="text-[#0F766E]" />
            <h2 class="text-[15px] font-semibold text-[#09090B]">
              Compute &amp; Process Telemetry
            </h2>
            <span
              v-if="runtimeData && (totalWorkerCount > 1 || (runtimeData.sandbox_workers?.length ?? 0) > 0)"
              class="rounded-full border px-2.5 py-0.5 text-xs font-medium tabular-nums"
              :class="
                healthyWorkerCount === totalWorkerCount
                  ? 'border-[#A7F3D0] bg-[#ECFDF5] text-[#059669]'
                  : 'border-[#FDE68A] bg-[#FFFBEB] text-[#D97706]'
              "
            >
              {{ healthyWorkerCount }} / {{ totalWorkerCount }} {{ totalWorkerCount === 1 ? "worker" : "workers" }} healthy
            </span>
          </div>
          <div v-if="runtimeData" class="flex flex-wrap items-center gap-2 text-xs text-[#52525B] tabular-nums">
            <span>Cache: <strong class="text-[#09090B]">{{ runtimeData.gateway.cache_backend }}</strong></span>
            <span>·</span>
            <span>Store: <strong class="text-[#09090B]">{{ runtimeData.gateway.store_backend }}</strong></span>
            <span>·</span>
            <span>Upstreams: <strong class="text-[#09090B]">{{ runtimeData.gateway.upstreams_count }}</strong></span>
          </div>
          <div class="flex w-full flex-wrap items-center justify-between gap-2 text-[11px] text-[#71717A]">
            <span aria-live="polite">Runtime telemetry {{ runtimeUpdatedAt ? `updated ${formatUtcTime(runtimeUpdatedAt)}` : "not yet available" }}</span>
            <Button :disabled="runtimeLoading" class="min-h-11 px-2 text-xs text-[#0F766E] underline underline-offset-2" @click="fetchRuntimeTelemetry">
              {{ runtimeLoading ? "Refreshing…" : "Refresh telemetry" }}
            </Button>
          </div>
        </div>

        <div v-if="runtimeError" role="alert" class="mx-4 mt-3 flex flex-wrap items-center justify-between gap-2 rounded-md border border-[#FECACA] bg-[#FEF2F2] px-3 py-2 text-xs text-[#B91C1C]">
          <span>Runtime telemetry could not be refreshed: {{ runtimeError }}</span>
          <Button :disabled="runtimeLoading" class="min-h-11 border border-[#D4D4D8] bg-white px-3 text-xs text-[#09090B] hover:bg-[#F4F4F5]" @click="fetchRuntimeTelemetry">Retry</Button>
        </div>
        <div v-if="!runtimeData && !runtimeError" class="m-4 h-24 animate-pulse rounded-lg border border-[#E4E4E7] bg-white" aria-label="Loading runtime telemetry" />

        <template v-if="runtimeData">
          <div class="grid grid-cols-1 divide-y divide-[#E4E4E7] lg:grid-cols-12 lg:divide-x lg:divide-y-0">
            <div class="space-y-4 p-4 lg:col-span-4">
              <div class="flex items-center justify-between gap-2">
                <span class="text-sm font-semibold text-[#09090B]">Gateway Replica</span>
                <span class="rounded border border-[#A7F3D0] bg-[#ECFDF5] px-2 py-0.5 text-xs font-medium text-[#059669]">
                  active · {{ runtimeData.gateway.cpu_count }} vCPU
                </span>
              </div>

              <div class="space-y-3">
                <Meter
                  :value="Math.min(cpuCapacity(runtimeData.gateway), runtimeData.gateway.cpu_percent)"
                  :min="0"
                  :max="cpuCapacity(runtimeData.gateway)"
                  :format="formatCpu"
                >
                  <MeterLabel>Process CPU (one vCPU = 100%)</MeterLabel>
                  <MeterValue class="tabular-nums text-xs font-medium" />
                  <MeterTrack>
                    <MeterIndicator />
                  </MeterTrack>
                </Meter>

                <p class="text-xs text-[#52525B]">Memory RSS <strong class="font-semibold tabular-nums text-[#09090B]">{{ runtimeData.gateway.rss_mb.toFixed(1) }} MiB</strong></p>
              </div>

              <dl class="grid grid-cols-3 gap-2 border-t border-[#E4E4E7] pt-3 text-xs">
                <div>
                  <dt class="text-[#71717A]">1m Load</dt>
                  <dd class="mt-0.5 font-semibold text-[#09090B] tabular-nums">
                    {{ runtimeData.gateway.load_avg_1m.toFixed(2) }}
                  </dd>
                </div>
                <div>
                  <dt class="text-[#71717A]">Executions</dt>
                  <dd class="mt-0.5 font-semibold text-[#09090B] tabular-nums">
                    {{ runtimeData.gateway.active_executions }} active / {{ runtimeData.gateway.total_executions }}
                  </dd>
                </div>
                <div>
                  <dt class="text-[#71717A]">Uptime</dt>
                  <dd class="mt-0.5 font-semibold text-[#09090B] tabular-nums">
                    {{ Math.round(runtimeData.gateway.uptime_seconds) }}s
                  </dd>
                </div>
              </dl>
            </div>

            <div class="lg:col-span-8">
              <div
                class="grid grid-cols-1 gap-px bg-[#E4E4E7]"
                :class="normalizedSandboxWorkers.length > 1 ? 'md:grid-cols-2' : ''"
              >
                <div
                  v-for="(worker, idx) in normalizedSandboxWorkers"
                  :key="worker.worker_id || idx"
                  class="space-y-4 bg-white p-4"
                >
                  <div class="flex flex-wrap items-start justify-between gap-2">
                    <div class="min-w-0">
                      <div class="text-sm font-semibold text-[#09090B]">
                        {{ worker.worker_id || `Worker #${idx + 1}` }}
                        <span class="font-normal text-[#52525B]">({{ worker.mode }})</span>
                      </div>
                      <div
                        v-if="worker.health_url"
                        class="mt-0.5 truncate font-mono-data text-[11px] text-[#71717A]"
                        :title="worker.health_url"
                      >
                        {{ worker.health_url }}
                      </div>
                      <div v-if="hasPoolConfig(worker)" class="mt-1 text-xs text-[#52525B] tabular-nums">
                        Pool: <strong class="font-medium text-[#09090B]">{{ worker.pool?.min_processes ?? 0 }}–{{ worker.pool?.max_processes ?? 0 }} processes</strong><span v-if="worker.pool?.max_concurrent_executions != null"> · {{ worker.pool.max_concurrent_executions }} max concurrent</span>
                      </div>
                    </div>
                    <span
                      class="shrink-0 rounded border px-2 py-0.5 text-xs font-medium"
                      :class="workerStatusBadgeClass(worker.status)"
                    >
                      {{ workerStatusLabel(worker.status) }}
                    </span>
                  </div>

                  <p v-if="isSharedWorker(worker)" class="rounded-md border border-[#E4E4E7] bg-[#FAFAFA] p-3 text-xs leading-5 text-[#52525B]">
                    This worker shares the gateway process. Its CPU, memory, uptime, and execution counts describe the same process capacity shown on the left.
                  </p>

                  <div
                    v-else-if="worker.status === 'unreachable'"
                    class="rounded-md border border-[#FECACA] bg-[#FEF2F2] p-3 text-xs text-[#DC2626]"
                  >
                    Worker health check failed: {{ worker.error || "unreachable" }}
                  </div>

                  <template v-else-if="worker.stats">
                    <div
                      v-if="worker.error"
                      class="rounded-md border border-[#FDE68A] bg-[#FFFBEB] p-2.5 text-xs text-[#92400E]"
                    >
                      {{ worker.error }}
                    </div>

                    <div class="space-y-3">
                      <Meter
                        :value="Math.min(cpuCapacity(worker.stats), worker.stats.cpu_percent)"
                        :min="0"
                        :max="cpuCapacity(worker.stats)"
                        :format="formatCpu"
                      >
                        <MeterLabel>Worker CPU (one vCPU = 100%)</MeterLabel>
                        <MeterValue class="tabular-nums text-xs font-medium" />
                        <MeterTrack>
                          <MeterIndicator />
                        </MeterTrack>
                      </Meter>

                      <p class="text-xs text-[#52525B]">Memory RSS <strong class="font-semibold tabular-nums text-[#09090B]">{{ worker.stats.rss_mb.toFixed(1) }} MiB</strong></p>
                    </div>

                    <dl class="grid grid-cols-3 gap-2 border-t border-[#E4E4E7] pt-3 text-xs">
                      <div>
                        <dt class="text-[#71717A]">1m Load</dt>
                        <dd class="mt-0.5 font-semibold text-[#09090B] tabular-nums">
                          {{ worker.stats.load_avg_1m.toFixed(2) }}
                        </dd>
                      </div>
                      <div>
                        <dt class="text-[#71717A]">Executions</dt>
                        <dd class="mt-0.5 font-semibold text-[#09090B] tabular-nums">
                          {{ worker.stats.active_executions }} active / {{ worker.stats.total_executions }}
                        </dd>
                      </div>
                      <div>
                        <dt class="text-[#71717A]">Uptime</dt>
                        <dd class="mt-0.5 font-semibold text-[#09090B] tabular-nums">
                          {{ Math.round(worker.stats.uptime_seconds) }}s
                        </dd>
                      </div>
                    </dl>
                  </template>
                </div>
              </div>
            </div>
          </div>

          <div class="border-t border-[#E4E4E7] bg-[#FAFAFA] px-4 py-3">
            <div class="flex flex-wrap items-center justify-between gap-2">
              <span class="text-xs font-semibold text-[#09090B]">Monty Sandbox Execution Telemetry</span>
              <span class="text-[11px] text-[#71717A]">
                {{ runtimeData.monty_telemetry?.ephemeral === false ? "Persistent sandbox" : "Ephemeral checkout per run" }}
              </span>
            </div>
            <dl class="mt-2 grid grid-cols-2 gap-3 text-xs sm:grid-cols-4">
              <div>
                <dt class="text-[#71717A]">Code Mode Runs</dt>
                <dd class="mt-0.5 font-semibold text-[#09090B] tabular-nums">
                  {{ formatInt(runtimeData.monty_telemetry?.codemode_runs) }}
                </dd>
              </div>
              <div>
                <dt class="text-[#71717A]">Spawn p50 / p95</dt>
                <dd class="mt-0.5 font-semibold text-[#0F766E] tabular-nums">
                  {{ Number(runtimeData.monty_telemetry?.spawn_p50_ms ?? 0).toFixed(2) }} ms / {{ Number(runtimeData.monty_telemetry?.spawn_p95_ms ?? 0).toFixed(2) }} ms
                </dd>
              </div>
              <div>
                <dt class="text-[#71717A]">Exec p50 / p95</dt>
                <dd class="mt-0.5 font-semibold text-[#0F766E] tabular-nums">
                  {{ Number(runtimeData.monty_telemetry?.exec_p50_ms ?? 0).toFixed(2) }} ms / {{ Number(runtimeData.monty_telemetry?.exec_p95_ms ?? 0).toFixed(2) }} ms
                </dd>
              </div>
              <div>
                <dt class="text-[#71717A]">Grounding Blocked</dt>
                <dd class="mt-0.5 font-semibold text-[#DC2626] tabular-nums">
                  {{ formatInt(runtimeData.monty_telemetry?.grounding_blocked_runs) }}
                </dd>
              </div>
            </dl>
          </div>
        </template>
      </section>

      <div class="grid grid-cols-1 gap-4 lg:grid-cols-2">
        <section class="rounded-lg border border-[#E4E4E7] bg-white p-4">
          <div class="flex flex-wrap items-start justify-between gap-2">
            <div>
              <h2 class="text-[15px] font-semibold text-[#09090B]">Execution mode distribution</h2>
              <p class="mt-0.5 text-xs text-[#71717A]">Code Mode vs Classic share of observed requests, not a recommendation score.</p>
            </div>
            <span class="text-xs tabular-nums text-[#71717A]">{{ analyticsData ? `${formatInt(pairedModeRunCount)} requests` : "Usage summary unavailable" }}</span>
          </div>
          <div v-if="pairedModeRunCount" class="mt-4 flex items-center gap-5">
            <div class="relative h-28 w-28 shrink-0 rounded-full" :style="executionMixStyle" role="img" :aria-label="`Code Mode ${codemodeSharePct}% and Classic ${classicSharePct}%`">
              <div class="absolute inset-[14px] flex flex-col items-center justify-center rounded-full bg-white">
                <strong class="text-lg font-semibold tabular-nums text-[#09090B]">{{ codemodeSharePct.toFixed(1) }}%</strong>
                <span class="text-[10px] text-[#71717A]">Code Mode</span>
              </div>
            </div>
            <div class="space-y-3">
              <div class="flex items-start gap-2"><span class="mt-1.5 h-2.5 w-2.5 shrink-0 rounded-full bg-[#0F766E]" /><div><div class="text-xs font-medium text-[#52525B]">Code Mode MCP</div><div class="text-sm font-semibold tabular-nums">{{ formatInt(analyticsData?.summary.codemode_runs) }} <span class="text-xs font-normal text-[#71717A]">requests</span></div></div></div>
              <div class="flex items-start gap-2"><span class="mt-1.5 h-2.5 w-2.5 shrink-0 rounded-full bg-[#2563EB]" /><div><div class="text-xs font-medium text-[#52525B]">Classic MCP</div><div class="text-sm font-semibold tabular-nums">{{ formatInt(analyticsData?.summary.classic_runs) }} <span class="text-xs font-normal text-[#71717A]">requests</span></div></div></div>
            </div>
          </div>
          <p v-else-if="analyticsData" class="mt-3 text-xs text-[#71717A]">No Code Mode or Classic MCP executions recorded yet. Send requests to <code class="font-mono-data">/mcp</code> or start Demo Control traffic.</p>
          <p v-else class="mt-3 text-xs text-[#71717A]">Usage data could not be loaded. Refresh the summary to check again.</p>
          <p v-if="otherModeRunCount > 0" class="mt-3 text-[11px] text-[#71717A]">{{ formatInt(otherModeRunCount) }} other or Hybrid requests excluded.</p>
          <NuxtLink to="/analytics" class="mt-3 inline-flex min-h-11 items-center text-xs font-medium text-[#0F766E] underline underline-offset-2">Review traffic by group in Gateway Usage</NuxtLink>
        </section>

        <section class="rounded-lg border border-[#E4E4E7] bg-white p-4">
          <h2 class="text-[15px] font-semibold text-[#09090B]">Security &amp; efficiency posture</h2>
          <p class="mt-0.5 text-xs text-[#71717A]">Governance actions and savings in the last {{ formatInt(valueData?.window_runs) }} runs.</p>
          <dl class="mt-4 grid grid-cols-2 gap-x-4 gap-y-3 text-xs">
            <div>
              <dt class="text-[#71717A]">ABAC rows filtered</dt>
              <dd class="mt-0.5 text-lg font-semibold tabular-nums text-[#09090B]">{{ formatInt(valueData?.security_and_governance?.abac_rows_filtered) }}</dd>
            </div>
            <div>
              <dt class="text-[#71717A]">PII / secrets redacted</dt>
              <dd class="mt-0.5 text-lg font-semibold tabular-nums text-[#09090B]">{{ formatInt(valueData?.security_and_governance?.pii_secrets_redacted) }}</dd>
            </div>
            <div>
              <dt class="text-[#71717A]">Grounded runs verified</dt>
              <dd class="mt-0.5 text-lg font-semibold tabular-nums text-[#059669]">{{ formatInt(valueData?.security_and_governance?.grounded_runs_verified) }}</dd>
            </div>
            <div>
              <dt class="text-[#71717A]">Grounding violations blocked</dt>
              <dd class="mt-0.5 text-lg font-semibold tabular-nums text-[#DC2626]">{{ formatInt(valueData?.security_and_governance?.grounding_violations_blocked) }}</dd>
            </div>
            <div>
              <dt class="text-[#71717A]">Payload reduction</dt>
              <dd class="mt-0.5 text-lg font-semibold tabular-nums text-[#09090B]">{{ (valueData?.efficiency?.payload_reduction_pct ?? 0).toFixed(1) }}%</dd>
            </div>
            <div>
              <dt class="text-[#71717A]">Total tokens saved</dt>
              <dd class="mt-0.5 text-lg font-semibold tabular-nums text-[#0F766E]">{{ formatInt(valueData?.efficiency?.total_tokens_saved) }}</dd>
            </div>
          </dl>
        </section>
      </div>

      <section aria-label="Business value and workflows snapshot" class="rounded-lg border border-[#E4E4E7] bg-white p-4">
        <div class="flex flex-wrap items-center justify-between gap-2">
          <div>
            <h2 class="text-[15px] font-semibold text-[#09090B]">Business Value &amp; Workflows</h2>
            <p class="mt-0.5 text-xs text-[#71717A]">
              Published workflow definitions, discovered candidates pending review, and completed human-time baselines.
            </p>
          </div>
          <div class="flex flex-wrap items-center gap-3 text-xs">
            <NuxtLink to="/workflows" class="inline-flex min-h-11 items-center font-medium text-[#0F766E] underline underline-offset-2 focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-[#0F766E]">
              Manage workflows
            </NuxtLink>
            <NuxtLink to="/business-value" class="inline-flex min-h-11 items-center font-medium text-[#0F766E] underline underline-offset-2 focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-[#0F766E]">
              Business Value report
            </NuxtLink>
          </div>
        </div>

        <dl class="mt-3 grid grid-cols-1 divide-y divide-[#E4E4E7] border-t border-[#E4E4E7] pt-3 sm:grid-cols-2 sm:divide-y-0 lg:grid-cols-4 lg:divide-x">
          <div class="py-2 sm:pr-4 lg:py-0 lg:pl-4 lg:first:pl-0">
            <dt class="text-xs font-medium text-[#52525B]">Configured Workflows</dt>
            <dd class="mt-1 text-lg font-semibold text-[#09090B] tabular-nums">
              <template v-if="workflowsCatalogData">{{ activeWorkflowsCount }} active / {{ totalWorkflowsCount }} total</template>
              <span v-else class="text-sm font-normal text-[#71717A]">Unavailable</span>
            </dd>
          </div>
          <div class="py-2 sm:pr-4 lg:py-0 lg:pl-4">
            <dt class="text-xs font-medium text-[#52525B]">Pending Candidates</dt>
            <dd class="mt-1 flex items-baseline gap-2 text-lg font-semibold text-[#09090B] tabular-nums">
              <template v-if="workflowsCatalogData">
                <span>{{ formatInt(pendingCandidatesCount) }}</span>
                <NuxtLink to="/workflows" class="text-xs font-medium text-[#0F766E] underline underline-offset-2">
                  Review
                </NuxtLink>
              </template>
              <span v-else class="text-sm font-normal text-[#71717A]">Unavailable</span>
            </dd>
          </div>
          <div class="py-2 sm:pr-4 lg:py-0 lg:pl-4">
            <dt class="text-xs font-medium text-[#52525B]">Completed Outcomes</dt>
            <dd class="mt-1 text-lg font-semibold text-[#09090B] tabular-nums">
              <template v-if="businessValueData">{{ formatInt(businessValueData.completed_count) }}</template>
              <span v-else class="text-sm font-normal text-[#71717A]">Unavailable</span>
            </dd>
          </div>
          <div class="py-2 lg:py-0 lg:pl-4">
            <dt class="text-xs font-medium text-[#52525B]">Estimated Baseline Minutes</dt>
            <dd class="mt-1 text-lg font-semibold text-[#0F766E] tabular-nums">
              <template v-if="businessValueData">
                {{ estimatedBaselineMinutes !== null ? `${estimatedBaselineMinutes.toLocaleString("en-US", { maximumFractionDigits: 1 })} min` : "Unavailable" }}
              </template>
              <span v-else class="text-sm font-normal text-[#71717A]">Unavailable</span>
            </dd>
          </div>
        </dl>
      </section>

      <section v-if="valueData?.recommendations?.length" class="rounded-lg border border-[#FDE68A] bg-[#FFFBEB] p-4">
        <h2 class="text-[15px] font-semibold text-[#92400E]">
          Optimization Recommendations
        </h2>
        <ul class="mt-2 space-y-1.5 text-[13px] text-[#78350F]">
          <li v-for="rec in valueData.recommendations" :key="rec.server">
            <strong class="font-mono-data">{{ rec.server }}</strong>: {{ rec.reason }}
          </li>
        </ul>
      </section>

      <section class="rounded-lg border border-[#E4E4E7] bg-white">
        <div class="border-b border-[#E4E4E7] px-4 py-3">
          <h2 class="text-[15px] font-semibold text-[#09090B]">
            Upstream Execution Breakdown
          </h2>
        </div>

        <div v-if="upstreamRows.length === 0" class="p-6 text-center text-[13px] text-[#71717A]">
          No upstream tool subcalls recorded yet in this window.
        </div>

        <div v-else class="overflow-x-auto table-scroll">
          <table class="w-full border-collapse text-left text-[13px]">
            <thead>
              <tr class="border-b border-[#E4E4E7] bg-[#F4F4F5] text-xs font-medium text-[#52525B]">
                <th class="px-4 py-2.5 whitespace-nowrap">Upstream</th>
                <th class="px-4 py-2.5 text-right whitespace-nowrap">Subcalls</th>
                <th class="px-4 py-2.5 text-right whitespace-nowrap">Code Mode</th>
                <th class="px-4 py-2.5 text-right whitespace-nowrap">Classic</th>
                <th class="px-4 py-2.5 text-right whitespace-nowrap">Raw Bytes</th>
                <th class="px-4 py-2.5 text-right whitespace-nowrap">Avg Latency</th>
                <th class="px-4 py-2.5 text-right whitespace-nowrap">ABAC Filtered</th>
                <th class="px-4 py-2.5 text-right whitespace-nowrap">PII Redacted</th>
              </tr>
            </thead>
            <tbody class="divide-y divide-[#E4E4E7]">
              <tr v-for="row in upstreamRows" :key="row.server" class="hover:bg-[#FAFAFA]">
                <td class="px-4 py-2.5 font-mono-data font-medium text-[#09090B] whitespace-nowrap">
                  {{ row.server }}
                </td>
                <td class="px-4 py-2.5 text-right tabular-nums whitespace-nowrap">{{ formatInt(row.calls) }}</td>
                <td class="px-4 py-2.5 text-right text-[#0F766E] tabular-nums whitespace-nowrap">{{ formatInt(row.codemode_calls) }}</td>
                <td class="px-4 py-2.5 text-right text-[#2563EB] tabular-nums whitespace-nowrap">{{ formatInt(row.classic_calls) }}</td>
                <td class="px-4 py-2.5 text-right tabular-nums whitespace-nowrap">{{ formatInt(row.raw_bytes) }}</td>
                <td class="px-4 py-2.5 text-right tabular-nums whitespace-nowrap">{{ Number(row.avg_duration_ms || 0).toFixed(1) }} ms</td>
                <td class="px-4 py-2.5 text-right tabular-nums whitespace-nowrap">{{ formatInt(row.abac_rows_filtered) }}</td>
                <td class="px-4 py-2.5 text-right tabular-nums whitespace-nowrap">{{ formatInt(row.redactions_count) }}</td>
              </tr>
            </tbody>
          </table>
        </div>
      </section>
    </template>
  </div>
</template>
