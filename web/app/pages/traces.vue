<script setup lang="ts">
import { extractErrorMessage } from "~/composables/useAdminApi";

interface TraceRun {
  run_id: string;
  mode: string;
  ok: boolean;
  grounding_blocked?: boolean;
  duration_ms: number;
  user_sub: string;
  user_email?: string | null;
  user_groups?: string[];
  subcall_count: number;
  monty_spawn_ms?: number;
  monty_exec_ms?: number;
}

interface TimelineGroup {
  group: string;
  runs: number;
  avg_total_ms: number;
  avg_gateway_ms: number;
  avg_monty_spawn_ms: number;
  avg_monty_exec_ms: number;
  avg_upstream_ms: number;
  avg_security_ms: number;
}

interface RuntimeTelemetry {
  gateway?: {
    active_executions?: number;
    total_executions?: number;
  };
  monty_telemetry?: {
    spawn_p50_ms?: number;
    spawn_p95_ms?: number;
    exec_p50_ms?: number;
    exec_p95_ms?: number;
    grounding_blocked_runs?: number;
  };
}

const { apiFetch } = useAdminApi();
const { totalRunsTick } = useLiveTelemetry();

const loading = ref(true);
const errorMessage = ref("");
const groupFilter = ref("");
const runs = ref<TraceRun[]>([]);
const timelineGroups = ref<TimelineGroup[]>([]);
const runtime = ref<RuntimeTelemetry | null>(null);
const liveUpdates = ref(true);

async function fetchTraces() {
  errorMessage.value = "";
  try {
    const gq = groupFilter.value.trim()
      ? `?group=${encodeURIComponent(groupFilter.value.trim())}`
      : "";
    const [r, a, rt] = await Promise.all([
      apiFetch<{ runs: TraceRun[] }>("/audit/runs?limit=50"),
      apiFetch<{ timeline_by_group?: TimelineGroup[] }>(`/audit/analytics${gq}`),
      apiFetch<RuntimeTelemetry>("/admin/telemetry/runtime"),
    ]);
    const allRuns = r.runs || [];
    const gf = groupFilter.value.trim().toLowerCase();
    runs.value = gf
      ? allRuns.filter((x) => (x.user_groups || []).some((g) => g.toLowerCase().includes(gf)))
      : allRuns;
    timelineGroups.value = a.timeline_by_group || [];
    runtime.value = rt;
  } catch (err) {
    errorMessage.value = extractErrorMessage(err);
  } finally {
    loading.value = false;
  }
}

onMounted(() => {
  void fetchTraces();
});

watch(totalRunsTick, () => {
  if (liveUpdates.value) {
    void fetchTraces();
  }
});

const totalP50 = computed(() => {
  const vals = runs.value.map((r) => r.duration_ms).sort((a, b) => a - b);
  if (!vals.length) return 0;
  return vals[Math.min(vals.length - 1, Math.max(0, Math.floor(0.5 * vals.length)))];
});

const totalP95 = computed(() => {
  const vals = runs.value.map((r) => r.duration_ms).sort((a, b) => a - b);
  if (!vals.length) return 0;
  return vals[Math.min(vals.length - 1, Math.max(0, Math.floor(0.95 * vals.length)))];
});

const slowestRuns = computed(() =>
  [...runs.value].sort((a, b) => b.duration_ms - a.duration_ms).slice(0, 12)
);

function pct(part: number, total: number): string {
  if (total <= 0) return "0%";
  return `${Math.max(2, Math.round((part / total) * 100))}%`;
}
</script>

<template>
  <div class="space-y-6">
    <div class="flex flex-col gap-3 sm:flex-row sm:items-center sm:justify-between">
      <div>
        <h1 class="text-[20px] font-semibold tracking-[-0.015em] text-[#09090B]">
          Traces &amp; Latency Timeline
        </h1>
        <p class="mt-0.5 text-[13px] text-[#52525B]">
          Where time is spent across incoming requests by group, with p50/p95 latency and Monty sandbox metrics.
        </p>
      </div>
      <div class="flex flex-wrap items-center gap-2">
        <input
          v-model="groupFilter"
          type="search"
          placeholder="Filter group (e.g. finance)..."
          aria-label="Filter traces by group"
          class="w-48 rounded-md border border-[#E4E4E7] bg-white px-3 py-1.5 text-xs text-[#09090B]"
          @input="fetchTraces()"
        />
        <label class="flex items-center gap-2 rounded-md border border-[#E4E4E7] bg-white px-2.5 py-1.5 text-xs font-medium text-[#09090B] cursor-pointer">
          <Switch v-model:checked="liveUpdates">
            <SwitchThumb />
          </Switch>
          <span>Real-Time</span>
        </label>
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

    <!-- p50 / p95 & Monty Sandbox Metrics -->
    <section v-if="runtime" class="grid grid-cols-1 gap-3 sm:grid-cols-2 lg:grid-cols-4">
      <div class="rounded-lg border border-[#E4E4E7] bg-white p-4">
        <div class="text-xs text-[#52525B]">Request Latency (p50 / p95)</div>
        <div class="mt-1 text-lg font-semibold text-[#09090B] tabular-nums">
          {{ totalP50.toFixed(1) }} ms / {{ totalP95.toFixed(1) }} ms
        </div>
        <div class="mt-1 text-xs text-[#71717A] tabular-nums">Across {{ runs.length }} recent runs</div>
      </div>
      <div class="rounded-lg border border-[#E4E4E7] bg-white p-4">
        <div class="text-xs text-[#52525B]">Monty Spawn (p50 / p95)</div>
        <div class="mt-1 text-lg font-semibold text-[#0F766E] tabular-nums">
          {{ (runtime.monty_telemetry?.spawn_p50_ms ?? 0).toFixed(2) }} ms / {{ (runtime.monty_telemetry?.spawn_p95_ms ?? 0).toFixed(2) }} ms
        </div>
        <div class="mt-1 text-xs text-[#71717A]">Ephemeral sandbox checkout</div>
      </div>
      <div class="rounded-lg border border-[#E4E4E7] bg-white p-4">
        <div class="text-xs text-[#52525B]">Monty Exec (p50 / p95)</div>
        <div class="mt-1 text-lg font-semibold text-[#0F766E] tabular-nums">
          {{ (runtime.monty_telemetry?.exec_p50_ms ?? 0).toFixed(2) }} ms / {{ (runtime.monty_telemetry?.exec_p95_ms ?? 0).toFixed(2) }} ms
        </div>
        <div class="mt-1 text-xs text-[#71717A]">Python sandbox execution</div>
      </div>
      <div class="rounded-lg border border-[#E4E4E7] bg-white p-4">
        <div class="text-xs text-[#52525B]">Sandbox Activity</div>
        <div class="mt-1 text-lg font-semibold text-[#09090B] tabular-nums">
          {{ runtime.gateway?.active_executions ?? 0 }} active / {{ runtime.gateway?.total_executions ?? 0 }} total
        </div>
        <div class="mt-1 text-xs text-[#71717A] tabular-nums">
          Grounding blocks: {{ runtime.monty_telemetry?.grounding_blocked_runs ?? 0 }}
        </div>
      </div>
    </section>

    <div v-if="loading" class="h-48 animate-pulse rounded-lg border border-[#E4E4E7] bg-white" />

    <template v-else>
      <!-- Aggregated Group Timeline Bars -->
      <section class="rounded-lg border border-[#E4E4E7] bg-white">
        <div class="flex flex-wrap items-center justify-between gap-2 border-b border-[#E4E4E7] px-4 py-3">
          <h2 class="text-[15px] font-semibold text-[#09090B]">Where Time Is Spent by Group</h2>
          <div class="flex flex-wrap items-center gap-3 text-[11px] text-[#52525B]">
            <span class="inline-flex items-center gap-1"><span class="h-2.5 w-2.5 rounded-sm bg-[#71717A]" /> Gateway</span>
            <span class="inline-flex items-center gap-1"><span class="h-2.5 w-2.5 rounded-sm bg-[#0F766E]" /> Monty</span>
            <span class="inline-flex items-center gap-1"><span class="h-2.5 w-2.5 rounded-sm bg-[#2563EB]" /> Upstream</span>
            <span class="inline-flex items-center gap-1"><span class="h-2.5 w-2.5 rounded-sm bg-[#D97706]" /> Security</span>
          </div>
        </div>
        <div v-if="timelineGroups.length === 0" class="p-6 text-center text-xs text-[#71717A]">
          No group timeline data recorded yet.
        </div>
        <div v-else class="divide-y divide-[#E4E4E7]">
          <div v-for="g in timelineGroups" :key="g.group" class="px-4 py-3 space-y-1.5">
            <div class="flex items-center justify-between text-xs">
              <span class="font-semibold text-[#09090B]">{{ g.group }} <span class="font-normal text-[#71717A]">({{ g.runs }} runs)</span></span>
              <span class="font-mono-data text-[#52525B] tabular-nums">
                avg {{ g.avg_total_ms.toFixed(1) }} ms
              </span>
            </div>
            <div class="flex h-3 w-full overflow-hidden rounded bg-[#F4F4F5]">
              <div class="bg-[#71717A]" :style="{ width: pct(g.avg_gateway_ms, g.avg_total_ms) }" :title="`Gateway: ${g.avg_gateway_ms} ms`" />
              <div class="bg-[#0F766E]" :style="{ width: pct(g.avg_monty_spawn_ms + g.avg_monty_exec_ms, g.avg_total_ms) }" :title="`Monty: ${(g.avg_monty_spawn_ms + g.avg_monty_exec_ms).toFixed(2)} ms`" />
              <div class="bg-[#2563EB]" :style="{ width: pct(g.avg_upstream_ms, g.avg_total_ms) }" :title="`Upstream: ${g.avg_upstream_ms} ms`" />
              <div class="bg-[#D97706]" :style="{ width: pct(g.avg_security_ms, g.avg_total_ms) }" :title="`Security: ${g.avg_security_ms} ms`" />
            </div>
          </div>
        </div>
      </section>

      <!-- Compact Slowest / Recent Runs linking to /audit?run_id=... -->
      <section class="rounded-lg border border-[#E4E4E7] bg-white">
        <div class="border-b border-[#E4E4E7] px-4 py-3">
          <h2 class="text-[15px] font-semibold text-[#09090B]">Slowest Recent Traces</h2>
        </div>
        <div class="overflow-x-auto table-scroll">
          <table class="w-full border-collapse text-left text-[13px]">
            <thead>
              <tr class="border-b border-[#E4E4E7] bg-[#F4F4F5] text-xs font-medium text-[#52525B]">
                <th class="px-4 py-2">Run</th>
                <th class="px-4 py-2">Group / Caller</th>
                <th class="px-4 py-2">Mode</th>
                <th class="px-4 py-2 text-right">Subcalls</th>
                <th class="px-4 py-2 text-right">Duration</th>
                <th class="px-4 py-2 text-right">Inspect</th>
              </tr>
            </thead>
            <tbody class="divide-y divide-[#E4E4E7]">
              <tr v-for="r in slowestRuns" :key="r.run_id" class="hover:bg-[#FAFAFA]">
                <td class="px-4 py-2 font-mono-data text-xs text-[#09090B]">{{ r.run_id.slice(0, 12) }}</td>
                <td class="px-4 py-2 text-xs text-[#52525B]">{{ (r.user_groups || []).join(", ") || "open" }} · {{ r.user_email || r.user_sub }}</td>
                <td class="px-4 py-2 font-mono-data text-xs">{{ r.mode }}</td>
                <td class="px-4 py-2 text-right tabular-nums">{{ r.subcall_count }}</td>
                <td class="px-4 py-2 text-right font-semibold tabular-nums">{{ r.duration_ms.toFixed(1) }} ms</td>
                <td class="px-4 py-2 text-right">
                  <NuxtLink :to="`/audit?run_id=${encodeURIComponent(r.run_id)}`" class="text-xs font-medium text-[#0F766E] hover:underline">
                    Inspect in Audit →
                  </NuxtLink>
                </td>
              </tr>
            </tbody>
          </table>
        </div>
      </section>
    </template>
  </div>
</template>
