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

const { apiFetch, setRuntimeMeta } = useAdminApi();

const loading = ref(true);
const errorMessage = ref("");
const autoRefresh = ref(true);
const runtimeData = ref<RuntimeTelemetryResponse | null>(null);
const valueData = ref<AuditValueResponse | null>(null);
const analyticsData = ref<AnalyticsSummary | null>(null);

let pollTimer: ReturnType<typeof setInterval> | null = null;

async function fetchOverview() {
  errorMessage.value = "";
  try {
    const [rt, val, ana] = await Promise.all([
      apiFetch<RuntimeTelemetryResponse>("/admin/telemetry/runtime"),
      apiFetch<AuditValueResponse>("/audit/value"),
      apiFetch<AnalyticsSummary>("/audit/analytics"),
    ]);
    runtimeData.value = rt;
    valueData.value = val;
    analyticsData.value = ana;
    setRuntimeMeta({
      profile: rt.gateway.profile,
      app_env: rt.gateway.app_env,
      catalog_version: rt.gateway.catalog_version,
    });
  } catch (err) {
    errorMessage.value = extractErrorMessage(err);
  } finally {
    loading.value = false;
  }
}

watch(
  autoRefresh,
  (enabled) => {
    if (pollTimer) {
      clearInterval(pollTimer);
      pollTimer = null;
    }
    if (enabled && import.meta.client) {
      pollTimer = setInterval(() => {
        void fetchOverview();
      }, 15000);
    }
  },
  { immediate: true }
);

onMounted(() => {
  void fetchOverview();
});

onBeforeUnmount(() => {
  if (pollTimer) {
    clearInterval(pollTimer);
    pollTimer = null;
  }
});

const upstreamRows = computed(() => {
  const entries = Object.entries(valueData.value?.by_upstream || {});
  return entries.map(([server, info]) => ({ server, ...info }));
});

const workerStats = computed<ProcessStats | null>(() => {
  return runtimeData.value?.sandbox_worker?.stats || null;
});

const classicSharePct = computed(() => {
  const total = analyticsData.value?.summary.total_runs || 0;
  if (!total) return 0;
  const cm = analyticsData.value?.summary.codemode_share_pct || 0;
  return Math.max(0, Math.round((100 - cm) * 10) / 10);
});

const codemodeSharePct = computed(() => {
  const total = analyticsData.value?.summary.total_runs || 0;
  if (!total) return 0;
  return analyticsData.value?.summary.codemode_share_pct || 0;
});

function formatInt(n: number | undefined): string {
  return Number(n || 0).toLocaleString("en-US");
}

function formatUsd(n: number | undefined): string {
  return `$${Number(n || 0).toFixed(4)}`;
}
</script>

<template>
  <div class="space-y-6">
    <!-- Page header -->
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
        <label class="flex items-center gap-2 text-xs font-medium text-[#52525B] cursor-pointer">
          <Switch v-model:checked="autoRefresh">
            <SwitchThumb />
          </Switch>
          <span>15s Auto-refresh</span>
        </label>
        <Button data-variant="primary" pending-announcement="Refreshing metrics" @click="fetchOverview">
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

    <!-- Loading Skeleton -->
    <div v-if="loading" class="grid grid-cols-1 gap-4 sm:grid-cols-2 lg:grid-cols-4">
      <div
        v-for="i in 4"
        :key="i"
        class="h-24 animate-pulse rounded-lg border border-[#E4E4E7] bg-white"
      />
    </div>

    <template v-else>
      <!-- 4-column KPI strip -->
      <section aria-label="Gateway value summary" class="grid grid-cols-1 gap-4 sm:grid-cols-2 lg:grid-cols-4">
        <div class="rounded-lg border border-[#E4E4E7] bg-white p-4">
          <div class="text-xs font-medium text-[#52525B]">Total Tokens Saved</div>
          <div class="mt-1.5 text-2xl font-semibold text-[#09090B] tabular-nums">
            {{ formatInt(valueData?.efficiency.total_tokens_saved) }}
          </div>
          <div class="mt-1 text-xs text-[#71717A] tabular-nums">
            Payload: {{ formatInt(valueData?.efficiency.payload_tokens_saved) }} · Schema: {{ formatInt(valueData?.efficiency.schema_tokens_saved) }}
          </div>
        </div>

        <div class="rounded-lg border border-[#E4E4E7] bg-white p-4">
          <div class="text-xs font-medium text-[#52525B]">Est. LLM Cost Saved</div>
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

      <!-- Live Compute & Runtime Telemetry (single flat surface split by hairline border) -->
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
          <!-- Gateway Process -->
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

          <!-- Sandbox Worker -->
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

      <!-- Execution Mode Split Bar (strictly bound to API data) -->
      <section class="rounded-lg border border-[#E4E4E7] bg-white p-4">
        <div class="flex flex-wrap items-center justify-between gap-2">
          <h2 class="text-[15px] font-semibold text-[#09090B]">
            Code Mode vs. Classic MCP Execution Mix
          </h2>
          <div class="flex items-center gap-4 text-xs tabular-nums">
            <span class="flex items-center gap-1.5 text-[#0F766E] font-medium">
              <span class="inline-block h-2.5 w-2.5 rounded-sm bg-[#0F766E]" />
              Code Mode: {{ codemodeSharePct }}% ({{ formatInt(analyticsData?.summary.codemode_runs) }})
            </span>
            <span class="flex items-center gap-1.5 text-[#2563EB] font-medium">
              <span class="inline-block h-2.5 w-2.5 rounded-sm bg-[#2563EB]" />
              Classic: {{ classicSharePct }}% ({{ formatInt(analyticsData?.summary.classic_runs) }})
            </span>
          </div>
        </div>

        <div class="mt-3 flex h-3 w-full overflow-hidden rounded bg-[#F4F4F5]">
          <div
            v-if="codemodeSharePct > 0"
            class="h-full bg-[#0F766E] transition-all"
            :style="{ width: `${codemodeSharePct}%` }"
          />
          <div
            v-if="classicSharePct > 0"
            class="h-full bg-[#2563EB] transition-all"
            :style="{ width: `${classicSharePct}%` }"
          />
        </div>
        <p v-if="!analyticsData?.summary.total_runs" class="mt-2 text-xs text-[#71717A]">
          No MCP executions recorded yet. Send requests to <code class="font-mono-data">/mcp</code> or run the Onboarding A/B Lab.
        </p>
      </section>

      <!-- Recommendations -->
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

      <!-- Per-Upstream Telemetry Table -->
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
