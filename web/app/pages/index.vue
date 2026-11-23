<script setup lang="ts">
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
  sandbox_worker: {
    mode: string;
    status: string;
    health_url?: string;
    error?: string;
    stats?: ProcessStats;
  };
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

async function triggerOverviewBurst() {
  await runBurst(8);
  await fetchOverview();
}

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

let refreshDebounce: ReturnType<typeof setTimeout> | null = null;

async function fetchOverview() {
  if (summaryRefreshing.value) return;
  summaryRefreshing.value = true;
  errorMessage.value = "";
  try {
    const [val, ana] = await Promise.all([
      apiFetch<AuditValueResponse>("/audit/value"),
      apiFetch<AnalyticsSummary>("/audit/analytics"),
    ]);
    valueData.value = val;
    analyticsData.value = ana;
    summaryUpdatedAt.value = new Date().toISOString();
    try {
      recoveryData.value = await apiFetch<RecoverySummary>("/admin/operations/metrics");
    } catch {
      recoveryData.value = null;
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

const workerStats = computed<ProcessStats | null>(() => {
  return runtimeData.value?.sandbox_worker?.stats || null;
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
  return status;
}

function isSharedWorker(worker: RuntimeTelemetryResponse["sandbox_worker"]): boolean {
  return worker.status === "in_process" || worker.mode === "in_process";
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
        <Button data-variant="primary" :disabled="demoBusy" @click="triggerOverviewBurst">
          Run Demo Burst (+8)
        </Button>
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
          <div class="flex items-center gap-2">
            <AppIcon name="cpu" :size="16" class="text-[#0F766E]" />
            <h2 class="text-[15px] font-semibold text-[#09090B]">
              Compute &amp; Process Telemetry
            </h2>
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

        <div v-if="runtimeData" class="grid grid-cols-1 divide-y divide-[#E4E4E7] md:grid-cols-2 md:divide-x md:divide-y-0">
          <div class="space-y-4 p-4">
            <div class="flex items-center justify-between">
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

          <div class="space-y-4 p-4">
            <div class="flex items-center justify-between gap-2">
                <span class="text-sm font-semibold text-[#09090B]">
                Monty Sandbox Worker ({{ runtimeData.sandbox_worker.mode }})
              </span>
              <span
                class="rounded border px-2 py-0.5 text-xs font-medium"
                :class="
                  runtimeData.sandbox_worker.status === 'unreachable'
                    ? 'border-[#FECACA] bg-[#FEF2F2] text-[#DC2626]'
                    : 'border-[#A7F3D0] bg-[#ECFDF5] text-[#059669]'
                "
              >
                {{ workerStatusLabel(runtimeData.sandbox_worker.status) }}
              </span>
            </div>

            <p v-if="isSharedWorker(runtimeData.sandbox_worker)" class="rounded-md border border-[#E4E4E7] bg-[#FAFAFA] p-3 text-xs leading-5 text-[#52525B]">
              This worker shares the gateway process. Its CPU, memory, uptime, and execution counts describe the same process capacity shown on the left.
            </p>
            <div
              v-if="runtimeData.sandbox_worker.status === 'unreachable'"
              class="rounded-md border border-[#FECACA] bg-[#FEF2F2] p-3 text-xs text-[#DC2626]"
            >
              Worker health check failed: {{ runtimeData.sandbox_worker.error }}
            </div>

            <template v-else-if="workerStats && !isSharedWorker(runtimeData.sandbox_worker)">
              <div class="space-y-3">
                <Meter
                  :value="Math.min(cpuCapacity(workerStats), workerStats.cpu_percent)"
                  :min="0"
                  :max="cpuCapacity(workerStats)"
                  :format="formatCpu"
                >
                  <MeterLabel>Worker CPU (one vCPU = 100%)</MeterLabel>
                  <MeterValue class="tabular-nums text-xs font-medium" />
                  <MeterTrack>
                    <MeterIndicator />
                  </MeterTrack>
                </Meter>

                <p class="text-xs text-[#52525B]">Memory RSS <strong class="font-semibold tabular-nums text-[#09090B]">{{ workerStats.rss_mb.toFixed(1) }} MiB</strong></p>
              </div>

              <dl class="grid grid-cols-3 gap-2 border-t border-[#E4E4E7] pt-3 text-xs">
                <div>
                  <dt class="text-[#71717A]">1m Load</dt>
                  <dd class="mt-0.5 font-semibold text-[#09090B] tabular-nums">
                    {{ workerStats.load_avg_1m.toFixed(2) }}
                  </dd>
                </div>
                <div>
                  <dt class="text-[#71717A]">Executions</dt>
                  <dd class="mt-0.5 font-semibold text-[#09090B] tabular-nums">
                    {{ workerStats.active_executions }} active / {{ workerStats.total_executions }}
                  </dd>
                </div>
                <div>
                  <dt class="text-[#71717A]">Uptime</dt>
                  <dd class="mt-0.5 font-semibold text-[#09090B] tabular-nums">
                    {{ Math.round(workerStats.uptime_seconds) }}s
                  </dd>
                </div>
              </dl>
            </template>
          </div>
        </div>
      </section>

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
    </template>
  </div>
</template>
