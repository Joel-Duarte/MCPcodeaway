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
  by_upstream: Record<
    string,
    {
      calls: number;
      errors: number;
      raw_bytes: number;
      abac_rows_filtered: number;
      redactions_count: number;
      classic_calls: number;
      codemode_calls: number;
      avg_duration_ms: number;
    }
  >;
  recommendations: { server: string; reason: string }[];
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
const errorMessage = ref("");
const runtimeData = ref<RuntimeTelemetryResponse | null>(null);
const valueData = ref<AuditValueResponse | null>(null);
const analyticsData = ref<AnalyticsSummary | null>(null);
const recoveryData = ref<RecoverySummary | null>(null);

let refreshDebounce: ReturnType<typeof setTimeout> | null = null;

async function fetchOverview() {
  errorMessage.value = "";
  try {
    const [val, ana] = await Promise.all([
      apiFetch<AuditValueResponse>("/audit/value"),
      apiFetch<AnalyticsSummary>("/audit/analytics"),
    ]);
    valueData.value = val;
    analyticsData.value = ana;
    try {
      recoveryData.value = await apiFetch<RecoverySummary>("/admin/operations/metrics");
    } catch {
      recoveryData.value = null;
    }
    try {
      const rt = await apiFetch<RuntimeTelemetryResponse>("/admin/telemetry/runtime", {}, true);
      runtimeData.value = rt;
      setRuntimeMeta({
        profile: rt.gateway.profile,
        app_env: rt.gateway.app_env,
        catalog_version: rt.gateway.catalog_version,
      });
    } catch {
    }
  } catch (err) {
    errorMessage.value = extractErrorMessage(err);
  } finally {
    loading.value = false;
  }
}

watch(latestRuntime, (rt) => {
  if (rt && typeof rt === "object" && "gateway" in rt) {
    runtimeData.value = rt as unknown as RuntimeTelemetryResponse;
  }
});

watch(totalRunsTick, () => {
  if (refreshDebounce) clearTimeout(refreshDebounce);
  refreshDebounce = setTimeout(() => {
    void fetchOverview();
  }, 600);
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

const upstreamRows = computed(() => {
  const entries = Object.entries(valueData.value?.by_upstream || {});
  return entries.map(([server, info]) => ({ server, ...info }));
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
        <Button v-if="!wsConnected" data-variant="primary" pending-announcement="Refreshing metrics" @click="fetchOverview">
          <AppIcon name="refresh" :size="15" />
          <span>Refresh</span>
        </Button>
      </div>
    </div>

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
        v-if="demoStatus.enabled && (valueData?.window_runs ?? 0) === 0"
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
          v-if="liveAlerts.length === 0"
          class="flex items-center justify-between gap-2 rounded-lg border border-[#A7F3D0] bg-[#ECFDF5] px-4 py-2.5 text-xs text-[#059669]"
        >
          <span class="font-medium">System Health Nominal — 0 active SLA, drift, or security alerts.</span>
          <NuxtLink to="/settings" class="font-medium text-[#0F766E] hover:underline">
            Configure Alert Rules →
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
          </div>
          <div class="flex shrink-0 items-center gap-3">
            <NuxtLink
              v-if="al.filter_status"
              :to="`/audit?status=${encodeURIComponent(al.filter_status)}`"
              class="font-semibold underline"
            >
              Inspect Offending Runs →
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
            {{ formatInt(valueData?.efficiency.total_tokens_saved) }}
          </div>
          <div class="mt-1 text-xs text-[#71717A] tabular-nums">
            Payload: {{ formatInt(valueData?.efficiency.payload_tokens_saved) }} · Schema: {{ formatInt(valueData?.efficiency.schema_tokens_saved) }}
          </div>
        </div>

        <div class="rounded-lg border border-[#E4E4E7] bg-white p-4">
          <div class="text-xs font-medium text-[#52525B]">Gateway-estimated LLM cost reduction</div>
          <div class="mt-1.5 text-2xl font-semibold text-[#0F766E] tabular-nums">
            {{ formatUsd(valueData?.efficiency.estimated_llm_cost_saved_usd) }}
          </div>
          <div class="mt-1 text-xs text-[#71717A] tabular-nums">
            {{ formatInt(valueData?.efficiency.llm_turns_avoided) }} turns avoided · {{ formatInt(valueData?.efficiency.context_overflows_prevented) }} overflows prevented
          </div>
        </div>

        <div class="rounded-lg border border-[#E4E4E7] bg-white p-4">
          <div class="text-xs font-medium text-[#52525B]">Payload Reduction</div>
          <div class="mt-1.5 text-2xl font-semibold text-[#09090B] tabular-nums">
            {{ (valueData?.efficiency.payload_reduction_pct ?? 0).toFixed(1) }}%
          </div>
          <div class="mt-1 text-xs text-[#71717A] tabular-nums">
            Across {{ formatInt(valueData?.window_runs) }} runs · OBO cache {{ (valueData?.efficiency.obo_cache_hit_rate_pct ?? 0).toFixed(0) }}%
          </div>
        </div>

        <div class="rounded-lg border border-[#E4E4E7] bg-white p-4">
          <div class="text-xs font-medium text-[#52525B]">Security &amp; Grounding</div>
          <div class="mt-1.5 text-2xl font-semibold text-[#09090B] tabular-nums">
            {{ formatInt(valueData?.security_and_governance.grounded_runs_verified) }} verified
          </div>
          <div class="mt-1 text-xs text-[#71717A] tabular-nums">
            ABAC filtered: {{ formatInt(valueData?.security_and_governance.abac_rows_filtered) }} · PII redacted: {{ formatInt(valueData?.security_and_governance.pii_secrets_redacted) }}
          </div>
        </div>
      </section>
      <p class="-mt-3 text-[11px] leading-5 text-[#71717A]">Token reduction and USD reduction are gateway-reported estimates. USD uses configured model rates and is not reconciled against provider invoices.</p>

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
                <th class="px-3 py-2 whitespace-nowrap">Target / Tool</th>
                <th class="px-3 py-2 whitespace-nowrap">Mode</th>
                <th class="px-3 py-2 text-right whitespace-nowrap">Reported reduction</th>
                <th class="px-3 py-2 text-right whitespace-nowrap">Latency</th>
                <th class="px-3 py-2 whitespace-nowrap">Status</th>
              </tr>
            </thead>
            <tbody class="divide-y divide-[#E4E4E7]">
              <tr v-for="ev in liveEvents" :key="ev.run_id">
                <td class="px-3 py-1.5 font-medium text-[#09090B] whitespace-nowrap">{{ ev.company_group }}</td>
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
                    {{ ev.grounding_blocked ? "GROUNDING BLOCKED" : ev.ok ? "OK" : "DENIED/ERR" }}
                  </span>
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
        </div>

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
                :value="Math.min(100, runtimeData.gateway.cpu_percent)"
                :min="0"
                :max="100"
                :format="(v: number) => `${v.toFixed(1)}%`"
              >
                <MeterLabel>Process CPU</MeterLabel>
                <MeterValue class="tabular-nums text-xs font-medium" />
                <MeterTrack>
                  <MeterIndicator />
                </MeterTrack>
              </Meter>

              <Meter
                :value="Math.min(512, runtimeData.gateway.rss_mb)"
                :min="0"
                :max="512"
                :format="(v: number) => `${v.toFixed(1)} MiB`"
              >
                <MeterLabel>Memory RSS</MeterLabel>
                <MeterValue class="tabular-nums text-xs font-medium" />
                <MeterTrack>
                  <MeterIndicator />
                </MeterTrack>
              </Meter>
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
                {{ runtimeData.sandbox_worker.status }}
              </span>
            </div>

            <div
              v-if="runtimeData.sandbox_worker.status === 'unreachable'"
              class="rounded-md border border-[#FECACA] bg-[#FEF2F2] p-3 text-xs text-[#DC2626]"
            >
              Worker health check failed: {{ runtimeData.sandbox_worker.error }}
            </div>

            <template v-else-if="workerStats">
              <div class="space-y-3">
                <Meter
                  :value="Math.min(100, workerStats.cpu_percent)"
                  :min="0"
                  :max="100"
                  :format="(v: number) => `${v.toFixed(1)}%`"
                >
                  <MeterLabel>Worker CPU</MeterLabel>
                  <MeterValue class="tabular-nums text-xs font-medium" />
                  <MeterTrack>
                    <MeterIndicator />
                  </MeterTrack>
                </Meter>

                <Meter
                  :value="Math.min(512, workerStats.rss_mb)"
                  :min="0"
                  :max="512"
                  :format="(v: number) => `${v.toFixed(1)} MiB`"
                >
                  <MeterLabel>Worker Memory RSS</MeterLabel>
                  <MeterValue class="tabular-nums text-xs font-medium" />
                  <MeterTrack>
                    <MeterIndicator />
                  </MeterTrack>
                </Meter>
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

      <div class="grid grid-cols-1 gap-4 lg:grid-cols-2">
        <section class="rounded-lg border border-[#E4E4E7] bg-white p-4">
          <div class="flex flex-wrap items-start justify-between gap-2">
            <div>
              <h2 class="text-[15px] font-semibold text-[#09090B]">Execution mode distribution</h2>
              <p class="mt-0.5 text-xs text-[#71717A]">Code Mode vs Classic share of observed requests, not a recommendation score.</p>
            </div>
            <span class="text-xs tabular-nums text-[#71717A]">{{ formatInt(pairedModeRunCount) }} requests</span>
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
          <p v-else class="mt-3 text-xs text-[#71717A]">No Code Mode or Classic MCP executions recorded yet. Send requests to <code class="font-mono-data">/mcp</code> or start Demo Control traffic.</p>
          <p v-if="otherModeRunCount > 0" class="mt-3 text-[11px] text-[#71717A]">{{ formatInt(otherModeRunCount) }} other or Hybrid requests excluded.</p>
        </section>

        <section class="rounded-lg border border-[#E4E4E7] bg-white p-4">
          <h2 class="text-[15px] font-semibold text-[#09090B]">Security &amp; efficiency posture</h2>
          <p class="mt-0.5 text-xs text-[#71717A]">Governance actions and savings in the last {{ formatInt(valueData?.window_runs) }} runs.</p>
          <dl class="mt-4 grid grid-cols-2 gap-x-4 gap-y-3 text-xs">
            <div><dt class="text-[#71717A]">ABAC rows filtered</dt><dd class="mt-0.5 text-lg font-semibold tabular-nums">{{ formatInt(valueData?.security_and_governance?.abac_rows_filtered) }}</dd></div>
            <div><dt class="text-[#71717A]">PII / secrets redacted</dt><dd class="mt-0.5 text-lg font-semibold tabular-nums">{{ formatInt(valueData?.security_and_governance?.pii_secrets_redacted) }}</dd></div>
            <div><dt class="text-[#71717A]">Grounded runs verified</dt><dd class="mt-0.5 text-lg font-semibold tabular-nums text-[#059669]">{{ formatInt(valueData?.security_and_governance?.grounded_runs_verified) }}</dd></div>
            <div><dt class="text-[#71717A]">Grounding violations blocked</dt><dd class="mt-0.5 text-lg font-semibold tabular-nums text-[#DC2626]">{{ formatInt(valueData?.security_and_governance?.grounding_violations_blocked) }}</dd></div>
            <div><dt class="text-[#71717A]">Payload reduction</dt><dd class="mt-0.5 text-lg font-semibold tabular-nums">{{ (valueData?.efficiency?.payload_reduction_pct ?? 0).toFixed(1) }}%</dd></div>
            <div><dt class="text-[#71717A]">Tokens saved (reported)</dt><dd class="mt-0.5 text-lg font-semibold tabular-nums">{{ formatInt(valueData?.efficiency?.total_tokens_saved) }}</dd></div>
          </dl>
        </section>
      </div>

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
                <th class="px-4 py-2.5 whitespace-nowrap text-right">Subcalls</th>
                <th class="px-4 py-2.5 whitespace-nowrap text-right">Code Mode</th>
                <th class="px-4 py-2.5 whitespace-nowrap text-right">Classic</th>
                <th class="px-4 py-2.5 whitespace-nowrap text-right">Raw Bytes</th>
                <th class="px-4 py-2.5 whitespace-nowrap text-right">Avg Latency</th>
                <th class="px-4 py-2.5 whitespace-nowrap text-right">ABAC Filtered</th>
                <th class="px-4 py-2.5 whitespace-nowrap text-right">PII Redacted</th>
              </tr>
            </thead>
            <tbody class="divide-y divide-[#E4E4E7]">
              <tr v-for="row in upstreamRows" :key="row.server" class="hover:bg-[#FAFAFA]">
                <td class="px-4 py-2.5 font-mono-data font-medium text-[#09090B] whitespace-nowrap">
                  {{ row.server }}
                </td>
                <td class="px-4 py-2.5 text-right tabular-nums whitespace-nowrap">{{ formatInt(row.calls) }}</td>
                <td class="px-4 py-2.5 text-right tabular-nums text-[#0F766E] whitespace-nowrap">{{ formatInt(row.codemode_calls) }}</td>
                <td class="px-4 py-2.5 text-right tabular-nums text-[#2563EB] whitespace-nowrap">{{ formatInt(row.classic_calls) }}</td>
                <td class="px-4 py-2.5 text-right tabular-nums whitespace-nowrap">{{ formatInt(row.raw_bytes) }}</td>
                <td class="px-4 py-2.5 text-right tabular-nums whitespace-nowrap">{{ row.avg_duration_ms.toFixed(1) }} ms</td>
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
