<script setup lang="ts">
import { onBeforeUnmount } from "vue";
import { extractErrorMessage } from "~/composables/useAdminApi";

interface TraceRun {
  run_id: string;
  mode: string;
  applied_mode?: string;
  recommended_mode?: string | null;
  mode_decision_reason?: string | null;
  mode_decision_score?: number | null;
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
let groupFilterTimer: ReturnType<typeof setTimeout> | null = null;

async function fetchTraces() {
  errorMessage.value = "";
  try {
    const gq = groupFilter.value.trim()
      ? `?group=${encodeURIComponent(groupFilter.value.trim())}`
      : "";
    const [r, a] = await Promise.all([
      apiFetch<{ runs: TraceRun[] }>("/audit/runs?limit=50"),
      apiFetch<{ timeline_by_group?: TimelineGroup[] }>(`/audit/analytics?limit=5000${gq ? `&${gq.slice(1)}` : ""}`),
    ]);
    const allRuns = r.runs || [];
    const gf = groupFilter.value.trim().toLowerCase();
    runs.value = gf
      ? allRuns.filter((x) => (x.user_groups || []).some((g) => g.toLowerCase().includes(gf)))
      : allRuns;
    timelineGroups.value = a.timeline_by_group || [];
    try {
      runtime.value = await apiFetch<RuntimeTelemetry>("/admin/telemetry/runtime", {}, true);
    } catch {
      runtime.value = null;
    }
  } catch (err) {
    errorMessage.value = extractErrorMessage(err);
  } finally {
    loading.value = false;
  }
}

onMounted(() => {
  void fetchTraces();
});

watch(groupFilter, () => {
  if (groupFilterTimer) clearTimeout(groupFilterTimer);
  groupFilterTimer = setTimeout(() => void fetchTraces(), 300);
});

onBeforeUnmount(() => {
  if (groupFilterTimer) clearTimeout(groupFilterTimer);
});

watch(totalRunsTick, () => {
  if (liveUpdates.value) {
    void fetchTraces();
  }
});

const totalP50 = computed(() => {
  const vals = runs.value.map((r) => r.duration_ms).sort((a, b) => a - b);
  return vals[Math.min(vals.length - 1, Math.max(0, Math.floor(0.5 * vals.length)))] ?? 0;
});

const totalP95 = computed(() => {
  const vals = runs.value.map((r) => r.duration_ms).sort((a, b) => a - b);
  return vals[Math.min(vals.length - 1, Math.max(0, Math.floor(0.95 * vals.length)))] ?? 0;
});

const slowestRuns = computed(() =>
  [...runs.value].sort((a, b) => b.duration_ms - a.duration_ms).slice(0, 12)
);

const modeLabel = (mode?: string | null) => {
  if (mode === "codemode" || mode?.startsWith("codemode")) return "Code Mode";
  if (mode === "traditional" || mode === "classic") return "Classic";
  return mode || "Unknown";
};
const adaptiveUnavailableLabel = (marker: string) => {
  const status = marker.split(":")[1];
  const messages: Record<string, string> = {
    unknown_model: "No model name was available for evidence matching.",
    unknown_workload: "The request did not match a supported workload profile.",
    no_accessible_tools: "No accessible tools were available for adaptive routing.",
    mutating_tools: "Adaptive recommendations apply only to read-only tools.",
    insufficient_evidence: "There are not enough qualified matched runs for a recommendation.",
    quality_gate: "The evidence did not meet the recommendation quality or regression gates.",
    conflicting_evidence: "Qualified fallback cohorts disagree, so normal Auto routing applies.",
  };
  return messages[status] || marker.split(";").slice(1).join(";").trim() || "No eligible adaptive recommendation was available.";
};
const decisionExplanation = (run: TraceRun) => {
  const reason = run.mode_decision_reason || "";
  const [marker, ...details] = reason.split(";").map((part) => part.trim());
  const detail = details.join("; ");
  if (marker.startsWith("adaptive_unavailable:")) {
    return `Adaptive routing was skipped. ${adaptiveUnavailableLabel(marker)}`;
  }
  if (marker.startsWith("adaptive_recommendation:") || marker.startsWith("adaptive_")) {
    const parts = marker.split(":");
    const tier = marker.startsWith("adaptive_recommendation:") ? parts[1] : parts[0].slice("adaptive_".length);
    const workload = marker.startsWith("adaptive_recommendation:") ? parts[2] : parts[1];
    const mode = parts.at(-1);
    const tierLabel = tier === "workload_family" ? "workload-family" : tier === "model_wide" ? "model-wide" : "exact-workload";
    return `Adaptive evidence selected ${modeLabel(mode)} from ${tierLabel} evidence for ${workloadLabel(workload)}. ${detail}`;
  }
  if (marker.startsWith("auto->")) {
    const unavailable = details.find((part) => part.startsWith("adaptive_unavailable:"));
    const autoDetails = details.filter((part) => !part.startsWith("adaptive_unavailable:"));
    return `Normal Auto selected ${modeLabel(run.applied_mode || run.mode)}. ${autoDetails.join("; ") || marker}${unavailable ? ` Adaptive routing was skipped: ${adaptiveUnavailableLabel(unavailable)}` : ""}`;
  }
  return reason;
};

const timelineGroupsChart = computed(() => {
  const sorted = [...timelineGroups.value].sort((a, b) => b.runs - a.runs);
  const top = sorted.slice(0, 5);
  const rest = sorted.slice(5);
  if (!rest.length) return top;
  const runs = rest.reduce((sum, item) => sum + item.runs, 0);
  const weighted = (key: keyof TimelineGroup) => runs
    ? rest.reduce((sum, item) => sum + Number(item[key] || 0) * item.runs, 0) / runs
    : 0;
  return [...top, {
    group: `Other ${rest.length} groups`,
    runs,
    avg_total_ms: weighted("avg_total_ms"),
    avg_gateway_ms: weighted("avg_gateway_ms"),
    avg_monty_spawn_ms: weighted("avg_monty_spawn_ms"),
    avg_monty_exec_ms: weighted("avg_monty_exec_ms"),
    avg_upstream_ms: weighted("avg_upstream_ms"),
    avg_security_ms: weighted("avg_security_ms"),
  }];
});

function stageMs(g: TimelineGroup) {
  return {
    gateway: g.avg_gateway_ms,
    monty: g.avg_monty_spawn_ms + g.avg_monty_exec_ms,
    upstream: g.avg_upstream_ms,
    security: g.avg_security_ms,
  };
}

const sharedTimelineScaleMs = computed(() => {
  const largestObserved = timelineGroupsChart.value.reduce((largest, group) => {
    const stages = stageMs(group);
    const accounted = stages.gateway + stages.monty + stages.upstream + stages.security;
    return Math.max(largest, group.avg_total_ms, accounted);
  }, 0);
  return Math.max(largestObserved, 0.001);
});

function stagePct(_group: TimelineGroup, stage: number): string {
  return `${Math.min(100, Math.max(0, (stage / sharedTimelineScaleMs.value) * 100)).toFixed(1)}%`;
}

function otherStageMs(g: TimelineGroup): number {
  const stages = stageMs(g);
  return Math.max(0, g.avg_total_ms - stages.gateway - stages.monty - stages.upstream - stages.security);
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
          class="w-full sm:w-48 rounded-md border border-[#E4E4E7] bg-white px-3 py-1.5 text-xs text-[#09090B]"
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
      <section class="rounded-lg border border-[#E4E4E7] bg-white">
        <div class="flex flex-wrap items-center justify-between gap-2 border-b border-[#E4E4E7] px-4 py-3">
          <div><h2 class="text-[15px] font-semibold text-[#09090B]">Where Time Is Spent by Group</h2><p class="mt-0.5 text-[11px] text-[#71717A]">Top five groups by run volume plus all remaining groups combined. Bars share a common 0–{{ sharedTimelineScaleMs.toFixed(1) }} ms scale; labels show average milliseconds.</p></div>
          <div class="flex flex-wrap items-center gap-3 text-[11px] text-[#52525B]">
            <span class="inline-flex items-center gap-1"><span class="h-2.5 w-2.5 rounded-sm bg-[#71717A]" /> Gateway</span>
            <span class="inline-flex items-center gap-1"><span class="h-2.5 w-2.5 rounded-sm bg-[#0F766E]" /> Monty</span>
            <span class="inline-flex items-center gap-1"><span class="h-2.5 w-2.5 rounded-sm bg-[#2563EB]" /> Upstream</span>
            <span class="inline-flex items-center gap-1"><span class="h-2.5 w-2.5 rounded-sm bg-[#D97706]" /> Security</span>
            <span class="inline-flex items-center gap-1"><span class="h-2.5 w-2.5 rounded-sm bg-[#D4D4D8]" /> Other / overhead</span>
          </div>
        </div>
        <div v-if="timelineGroups.length === 0" class="p-6 text-center text-xs text-[#71717A]">
          No group timeline data recorded yet.
        </div>
        <div v-else>
          <div class="grid grid-cols-[minmax(0,1fr)_auto] items-center gap-3 px-4 pt-3 text-[10px] text-[#71717A] tabular-nums">
            <div class="flex justify-between"><span>0 ms</span><span>{{ (sharedTimelineScaleMs / 2).toFixed(1) }} ms</span><span>{{ sharedTimelineScaleMs.toFixed(1) }} ms</span></div>
            <span>Average total</span>
          </div>
          <div class="divide-y divide-[#E4E4E7]">
          <div v-for="g in timelineGroupsChart" :key="g.group" class="px-4 py-3 space-y-1.5">
            <div class="flex items-center justify-between text-xs">
              <span class="font-semibold text-[#09090B]">{{ g.group }} <span class="font-normal text-[#71717A]">({{ g.runs }} runs)</span></span>
              <span class="font-mono-data text-[#52525B] tabular-nums">
                avg {{ g.avg_total_ms.toFixed(1) }} ms
              </span>
            </div>
            <div class="flex h-3 w-full overflow-hidden rounded bg-[#F4F4F5]" :aria-label="`Stage durations on a shared zero to ${sharedTimelineScaleMs.toFixed(1)} ms scale; average total ${g.avg_total_ms.toFixed(1)} ms`">
              <div class="bg-[#71717A]" :style="{ width: stagePct(g, g.avg_gateway_ms) }" :title="`Gateway: ${g.avg_gateway_ms.toFixed(2)} ms`" />
              <div class="bg-[#0F766E]" :style="{ width: stagePct(g, g.avg_monty_spawn_ms + g.avg_monty_exec_ms) }" :title="`Monty: ${(g.avg_monty_spawn_ms + g.avg_monty_exec_ms).toFixed(2)} ms`" />
              <div class="bg-[#2563EB]" :style="{ width: stagePct(g, g.avg_upstream_ms) }" :title="`Upstream: ${g.avg_upstream_ms.toFixed(2)} ms`" />
              <div class="bg-[#D97706]" :style="{ width: stagePct(g, g.avg_security_ms) }" :title="`Security: ${g.avg_security_ms.toFixed(2)} ms`" />
              <div class="bg-[#D4D4D8]" :style="{ width: stagePct(g, otherStageMs(g)) }" :title="`Other / unaccounted: ${otherStageMs(g).toFixed(2)} ms`" />
            </div>
            <div class="flex flex-wrap gap-x-3 gap-y-1 text-[10px] text-[#71717A] tabular-nums">
              <span>Gateway {{ g.avg_gateway_ms.toFixed(2) }} ms</span>
              <span>Monty {{ (g.avg_monty_spawn_ms + g.avg_monty_exec_ms).toFixed(2) }} ms</span>
              <span>Upstream {{ g.avg_upstream_ms.toFixed(2) }} ms</span>
              <span>Security {{ g.avg_security_ms.toFixed(2) }} ms</span>
              <span v-if="otherStageMs(g) > 0">Other {{ otherStageMs(g).toFixed(2) }} ms</span>
            </div>
          </div>
          </div>
        </div>
      </section>

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
                <th class="px-4 py-2">Applied / recommended</th>
                <th class="px-4 py-2 text-right">Subcalls</th>
                <th class="px-4 py-2 text-right">Duration</th>
                <th class="px-4 py-2 text-right">Inspect</th>
              </tr>
            </thead>
            <tbody class="divide-y divide-[#E4E4E7]">
              <tr v-for="r in slowestRuns" :key="r.run_id" class="hover:bg-[#FAFAFA]">
                <td class="px-4 py-2 font-mono-data text-xs text-[#09090B]">{{ r.run_id.slice(0, 12) }}</td>
                <td class="px-4 py-2 text-xs text-[#52525B]">{{ (r.user_groups || []).join(", ") || "open" }} · {{ r.user_email || r.user_sub }}</td>
                <td class="px-4 py-2 font-mono-data text-xs">{{ r.applied_mode || r.mode }}</td>
                <td class="px-4 py-2 text-xs">
                  <span>Applied: {{ modeLabel(r.applied_mode || r.mode) }}</span>
                  <span v-if="r.recommended_mode" class="block text-[#52525B]">Adaptive recommendation: {{ modeLabel(r.recommended_mode) }}<span v-if="r.mode_decision_reason?.includes('adaptive_recommendation:')"> · observed only</span></span>
                  <span v-if="r.mode_decision_score != null" class="block tabular-nums text-[#52525B]">Auto suitability score: {{ r.mode_decision_score.toFixed(1) }} / 100</span>
                  <span v-if="r.mode_decision_reason" class="block max-w-[24rem] break-words text-[10px] leading-4 text-[#71717A]">{{ decisionExplanation(r) }}</span>
                  <span v-if="!r.recommended_mode && r.mode_decision_score == null" class="block text-[#71717A]">No adaptive recommendation recorded</span>
                </td>
                <td class="px-4 py-2 text-right tabular-nums">{{ r.subcall_count }}</td>
                <td class="px-4 py-2 text-right font-semibold tabular-nums">{{ r.duration_ms.toFixed(1) }} ms</td>
                <td class="px-4 py-2 text-right">
                  <NuxtLink :to="`/audit?run_id=${encodeURIComponent(r.run_id)}`" class="text-xs font-medium text-[#0F766E] hover:underline">
                    Inspect trace
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
