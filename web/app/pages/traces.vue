<script setup lang="ts">
import { onBeforeUnmount } from "vue";
import { extractErrorMessage } from "~/composables/useAdminApi";
import type { ReportRange } from "~/composables/useReportRange";

interface TraceRun {
  run_id: string;
  created_at?: string;
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
const route = useRoute();
const router = useRouter();
const { selectedRange } = useReportRange();
const reportRanges: ReportRange[] = ["1h", "24h", "7d", "30d", "all"];
const rangeLabels: Record<ReportRange, string> = {
  "1h": "Last hour",
  "24h": "Last 24 hours",
  "7d": "Last 7 days",
  "30d": "Last 30 days",
  all: "All available history",
};
const initialGroup = typeof route.query.group === "string" ? route.query.group : "";
const initialRange = typeof route.query.window === "string" && reportRanges.includes(route.query.window as ReportRange)
  ? route.query.window as ReportRange
  : null;
if (initialRange) selectedRange.value = initialRange;

const loading = ref(true);
const errorMessage = ref("");
const runtimeUnavailable = ref(false);
const groupFilter = ref(initialGroup);
const runs = ref<TraceRun[]>([]);
const timelineGroups = ref<TimelineGroup[]>([]);
const runtime = ref<RuntimeTelemetry | null>(null);
const liveUpdates = ref(true);
const lastUpdatedAt = ref("");
const traceSampleLimit = 200;
let groupFilterTimer: ReturnType<typeof setTimeout> | null = null;
let querySyncTimer: ReturnType<typeof setTimeout> | null = null;
let traceRequestSequence = 0;

async function fetchTraces() {
  const requestSequence = ++traceRequestSequence;
  errorMessage.value = "";
  try {
    const group = groupFilter.value.trim();
    const analyticsParams = new URLSearchParams({ limit: "5000" });
    if (group) analyticsParams.set("group", group);
    if (selectedRange.value !== "all") analyticsParams.set("window", selectedRange.value);
    const [r, a] = await Promise.all([
      apiFetch<{ runs: TraceRun[] }>(`/audit/runs?limit=${traceSampleLimit}`),
      apiFetch<{ timeline_by_group?: TimelineGroup[] }>(`/audit/analytics?${analyticsParams.toString()}`),
    ]);
    if (requestSequence !== traceRequestSequence) return;
    runs.value = r.runs || [];
    timelineGroups.value = a.timeline_by_group || [];
    runtimeUnavailable.value = false;
    try {
      const runtimeResult = await apiFetch<RuntimeTelemetry>("/admin/telemetry/runtime", {}, true);
      if (requestSequence === traceRequestSequence) runtime.value = runtimeResult;
    } catch {
      if (requestSequence === traceRequestSequence) {
        runtime.value = null;
        runtimeUnavailable.value = true;
      }
    }
    if (requestSequence === traceRequestSequence) lastUpdatedAt.value = new Date().toLocaleTimeString();
  } catch (err) {
    if (requestSequence === traceRequestSequence) errorMessage.value = extractErrorMessage(err);
  } finally {
    if (requestSequence === traceRequestSequence) loading.value = false;
  }
}

const rangeHours: Record<ReportRange, number | null> = {
  "1h": 1,
  "24h": 24,
  "7d": 168,
  "30d": 720,
  all: null,
};
const matchingRuns = computed(() => {
  const group = groupFilter.value.trim().toLowerCase();
  const hours = rangeHours[selectedRange.value];
  const cutoff = hours === null ? null : Date.now() - hours * 60 * 60 * 1000;
  return runs.value.filter((run) => {
    if (cutoff !== null) {
      const createdAt = Date.parse(run.created_at || "");
      if (!Number.isFinite(createdAt) || createdAt < cutoff) return false;
    }
    return !group || (run.user_groups || []).some((item) => item.toLowerCase().includes(group));
  });
});
const unstampedRunCount = computed(() => runs.value.filter((run) => !Number.isFinite(Date.parse(run.created_at || ""))).length);
const rangeLabel = computed(() => rangeLabels[selectedRange.value]);
const slowestRuns = computed(() =>
  [...matchingRuns.value].sort((a, b) => b.duration_ms - a.duration_ms).slice(0, 12)
);
const timelineScopeDescription = computed(() =>
  `Up to 5,000 stored traces · ${rangeLabel.value.toLowerCase()}${groupFilter.value.trim() ? ` · group matches “${groupFilter.value.trim()}”` : ""}`
);

function syncRouteQuery() {
  const query = { ...route.query };
  const group = groupFilter.value.trim();
  if (group) query.group = group;
  else delete query.group;
  query.window = selectedRange.value;
  if (query.group === route.query.group && query.window === route.query.window) return;
  if (querySyncTimer) clearTimeout(querySyncTimer);
  querySyncTimer = setTimeout(() => void router.replace({ query }), 150);
}

onMounted(() => {
  void fetchTraces();
  syncRouteQuery();
});

watch([groupFilter, selectedRange], () => {
  if (groupFilterTimer) clearTimeout(groupFilterTimer);
  groupFilterTimer = setTimeout(() => void fetchTraces(), 300);
  syncRouteQuery();
});

watch(() => [route.query.group, route.query.window] as const, ([group, window]) => {
  const nextGroup = typeof group === "string" ? group : "";
  const nextRange = typeof window === "string" && reportRanges.includes(window as ReportRange)
    ? window as ReportRange
    : selectedRange.value;
  if (groupFilter.value !== nextGroup) groupFilter.value = nextGroup;
  if (selectedRange.value !== nextRange) selectedRange.value = nextRange;
});

onBeforeUnmount(() => {
  if (groupFilterTimer) clearTimeout(groupFilterTimer);
  if (querySyncTimer) clearTimeout(querySyncTimer);
});

watch(totalRunsTick, () => {
  if (liveUpdates.value) {
    void fetchTraces();
  }
});

const totalP50 = computed(() => {
  const vals = matchingRuns.value.map((r) => r.duration_ms).sort((a, b) => a - b);
  return vals[Math.min(vals.length - 1, Math.max(0, Math.floor(0.5 * vals.length)))] ?? 0;
});

const totalP95 = computed(() => {
  const vals = matchingRuns.value.map((r) => r.duration_ms).sort((a, b) => a - b);
  return vals[Math.min(vals.length - 1, Math.max(0, Math.floor(0.95 * vals.length)))] ?? 0;
});

function clearGroupFilter() {
  groupFilter.value = "";
}

function runStatusLabel(run: TraceRun): string {
  if (run.grounding_blocked) return "Grounding blocked";
  return run.ok ? "Succeeded" : "Error";
}

function runStatusClass(run: TraceRun): string {
  if (run.grounding_blocked) return "border-[#FDE68A] bg-[#FFFBEB] text-[#92400E]";
  return run.ok
    ? "border-[#A7F3D0] bg-[#ECFDF5] text-[#059669]"
    : "border-[#FECACA] bg-[#FEF2F2] text-[#DC2626]";
}

const modeLabel = (mode?: string | null) => {
  if (mode === "codemode" || mode?.startsWith("codemode")) return "Code Mode";
  if (mode === "traditional_discover") return "Classic · discover tools";
  if (mode === "traditional" || mode === "classic") return "Classic";
  if (mode === "hybrid") return "Hybrid";
  return mode || "Unknown mode";
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
  if (marker.startsWith("explicit:")) {
    return `The request explicitly selected ${modeLabel(marker.slice("explicit:".length))}.`;
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
      <div class="flex flex-wrap items-end gap-2 sm:justify-end">
        <Field class="w-full sm:w-auto">
          <FieldLabel for="trace-window">Time range</FieldLabel>
          <select
            id="trace-window"
            v-model="selectedRange"
            class="min-h-11 w-full rounded-md border border-[#D4D4D8] bg-white px-3 text-xs text-[#09090B] focus-visible:outline-2 focus-visible:outline-[#0F766E] sm:w-44"
          >
            <option v-for="value in reportRanges" :key="value" :value="value">{{ rangeLabels[value] }}</option>
          </select>
        </Field>
        <Field class="w-full sm:w-auto">
          <FieldLabel for="trace-group">Caller group</FieldLabel>
          <input
            id="trace-group"
            v-model="groupFilter"
            type="search"
            placeholder="e.g. finance"
            aria-label="Filter traces by caller group"
            class="min-h-11 w-full rounded-md border border-[#E4E4E7] bg-white px-3 text-xs text-[#09090B] focus-visible:outline-2 focus-visible:outline-[#0F766E] sm:w-52"
          />
        </Field>
        <Button v-if="groupFilter" class="min-h-11 px-3 text-xs" @click="clearGroupFilter">Clear group</Button>
        <div class="flex items-center gap-2">
          <label class="flex min-h-11 items-center gap-2 rounded-md border border-[#E4E4E7] bg-white px-2.5 text-xs font-medium text-[#09090B] cursor-pointer">
            <Switch v-model:checked="liveUpdates" aria-label="Real-time trace updates">
            <SwitchThumb />
            </Switch>
            <span>Real-time updates</span>
          </label>
          <Button class="min-h-11 px-3 text-xs" pending-announcement="Refreshing traces" @click="fetchTraces">Refresh</Button>
        </div>
      </div>
    </div>

    <p class="text-xs leading-5 text-[#52525B]" aria-live="polite">
      {{ liveUpdates ? "Live updates on" : "Live updates paused" }}<span v-if="lastUpdatedAt"> · Last updated {{ lastUpdatedAt }}</span>.
      Request latency and the slowest list use the latest {{ traceSampleLimit }} stored traces; the group timeline uses up to 5,000 traces. Each follows the selected time range and group filter.
      <span v-if="selectedRange !== 'all' && unstampedRunCount"> {{ unstampedRunCount }} latest records without timestamps are excluded from the request sample.</span>
    </p>

    <div
      v-if="errorMessage"
      role="alert"
      class="flex items-start gap-2 rounded-lg border border-[#FECACA] bg-[#FEF2F2] px-4 py-3 text-[13px] text-[#DC2626]"
    >
      <AppIcon name="alert" :size="16" class="mt-0.5" />
      <span>{{ errorMessage }}</span>
      <Button class="ml-auto min-h-11 px-3 text-xs" @click="fetchTraces">Retry</Button>
    </div>

    <section class="grid grid-cols-1 gap-3 sm:grid-cols-2 lg:grid-cols-4" aria-label="Latency and live runtime metrics">
      <div class="rounded-lg border border-[#E4E4E7] bg-white p-4">
        <div class="text-xs text-[#52525B]">Request latency · p50 / p95</div>
        <div class="mt-1 text-lg font-semibold text-[#09090B] tabular-nums">
          <template v-if="matchingRuns.length">{{ totalP50.toFixed(1) }} ms / {{ totalP95.toFixed(1) }} ms</template>
          <span v-else>—</span>
        </div>
        <div class="mt-1 text-xs text-[#71717A] tabular-nums">Across {{ matchingRuns.length }} matching runs · {{ rangeLabel }}</div>
      </div>
      <template v-if="runtime">
        <div class="rounded-lg border border-[#E4E4E7] bg-white p-4">
          <div class="text-xs text-[#52525B]">Monty spawn · runtime p50 / p95</div>
          <div class="mt-1 text-lg font-semibold text-[#0F766E] tabular-nums">
            {{ (runtime.monty_telemetry?.spawn_p50_ms ?? 0).toFixed(2) }} ms / {{ (runtime.monty_telemetry?.spawn_p95_ms ?? 0).toFixed(2) }} ms
          </div>
          <div class="mt-1 text-xs text-[#71717A]">Live runtime aggregate · ephemeral sandbox checkout</div>
        </div>
        <div class="rounded-lg border border-[#E4E4E7] bg-white p-4">
          <div class="text-xs text-[#52525B]">Monty execution · runtime p50 / p95</div>
          <div class="mt-1 text-lg font-semibold text-[#0F766E] tabular-nums">
            {{ (runtime.monty_telemetry?.exec_p50_ms ?? 0).toFixed(2) }} ms / {{ (runtime.monty_telemetry?.exec_p95_ms ?? 0).toFixed(2) }} ms
          </div>
          <div class="mt-1 text-xs text-[#71717A]">Live runtime aggregate · Python sandbox execution</div>
        </div>
        <div class="rounded-lg border border-[#E4E4E7] bg-white p-4">
          <div class="text-xs text-[#52525B]">Sandbox activity · live runtime</div>
          <div class="mt-1 text-lg font-semibold text-[#09090B] tabular-nums">
            {{ runtime.gateway?.active_executions ?? 0 }} active / {{ runtime.gateway?.total_executions ?? 0 }} total
          </div>
          <div class="mt-1 text-xs text-[#71717A] tabular-nums">
            Grounding blocks: {{ runtime.monty_telemetry?.grounding_blocked_runs ?? 0 }}
          </div>
        </div>
      </template>
      <div v-else-if="runtimeUnavailable" class="rounded-lg border border-[#E4E4E7] bg-white p-4 text-xs leading-5 text-[#52525B]">
        Live runtime metrics are unavailable. Request history and latency above remain available; retry to refresh runtime metrics.
        <Button class="mt-2 min-h-11 px-3 text-xs" @click="fetchTraces">Retry runtime data</Button>
      </div>
    </section>

    <div v-if="loading" class="h-48 animate-pulse rounded-lg border border-[#E4E4E7] bg-white" />

    <template v-else>
      <section class="rounded-lg border border-[#E4E4E7] bg-white">
        <div class="flex flex-wrap items-center justify-between gap-2 border-b border-[#E4E4E7] px-4 py-3">
          <div><h2 class="text-[15px] font-semibold text-[#09090B]">Where Time Is Spent by Group</h2><p class="mt-0.5 text-[11px] leading-5 text-[#71717A]">{{ timelineScopeDescription }}. Top five groups by run volume plus remaining groups combined. Bars share a common 0–{{ sharedTimelineScaleMs.toFixed(1) }} ms scale; labels show average milliseconds. A request can count in multiple groups when its caller belongs to multiple groups.</p></div>
          <div class="flex flex-wrap items-center gap-3 text-[11px] text-[#52525B]">
            <span class="inline-flex items-center gap-1"><span class="h-2.5 w-2.5 rounded-sm bg-[#71717A]" /> Gateway</span>
            <span class="inline-flex items-center gap-1"><span class="h-2.5 w-2.5 rounded-sm bg-[#0F766E]" /> Monty</span>
            <span class="inline-flex items-center gap-1"><span class="h-2.5 w-2.5 rounded-sm bg-[#2563EB]" /> Upstream</span>
            <span class="inline-flex items-center gap-1"><span class="h-2.5 w-2.5 rounded-sm bg-[#D97706]" /> Security</span>
            <span class="inline-flex items-center gap-1"><span class="h-2.5 w-2.5 rounded-sm bg-[#D4D4D8]" /> Other / overhead</span>
          </div>
        </div>
        <div v-if="timelineGroups.length === 0" class="p-6 text-center text-xs text-[#71717A]">
          <p>No group timeline data matches {{ groupFilter ? `“${groupFilter}” in ${rangeLabel.toLowerCase()}` : rangeLabel.toLowerCase() }}.</p>
          <Button v-if="groupFilter" class="mt-2 min-h-11 px-3 text-xs" @click="clearGroupFilter">Clear group filter</Button>
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
          <p class="mt-0.5 text-xs leading-5 text-[#52525B]">Top {{ slowestRuns.length }} by duration from {{ matchingRuns.length }} matching traces in the latest {{ runs.length }} stored records.</p>
        </div>
        <div v-if="slowestRuns.length === 0" class="p-6 text-center text-xs text-[#71717A]">
          <p>No recent traces match {{ groupFilter ? `group “${groupFilter}” and ` : "" }}{{ rangeLabel.toLowerCase() }}.</p>
          <p v-if="selectedRange !== 'all' && unstampedRunCount" class="mt-1">{{ unstampedRunCount }} of the latest records have no timestamp and are excluded from this time range.</p>
          <Button v-if="groupFilter" class="mt-2 min-h-11 px-3 text-xs" @click="clearGroupFilter">Clear group filter</Button>
        </div>
        <ul v-else class="divide-y divide-[#E4E4E7] lg:hidden" aria-label="Slowest recent traces">
          <li v-for="r in slowestRuns" :key="r.run_id" class="space-y-2 p-3">
            <div class="flex items-start justify-between gap-3">
              <div class="min-w-0">
                <div class="font-mono-data text-xs font-medium text-[#09090B]">{{ r.run_id.slice(0, 12) }}</div>
                <div class="mt-1 break-words text-xs text-[#52525B]">{{ (r.user_groups || []).join(", ") || "Unassigned" }} · {{ r.user_email || r.user_sub }}</div>
              </div>
              <div class="shrink-0 text-right">
                <div class="font-semibold tabular-nums text-[#09090B]">{{ r.duration_ms.toFixed(1) }} ms</div>
                <div class="text-[11px] text-[#71717A]">{{ r.subcall_count }} subcalls</div>
              </div>
            </div>
            <div class="flex flex-wrap items-center justify-between gap-2 text-xs">
              <span class="text-[#52525B]">{{ modeLabel(r.applied_mode || r.mode) }} · <span :class="runStatusClass(r)">{{ runStatusLabel(r) }}</span></span>
              <NuxtLink :to="`/audit?run_id=${encodeURIComponent(r.run_id)}`" class="inline-flex min-h-11 items-center justify-center rounded-md border border-[#D4D4D8] px-3 font-medium text-[#0F766E] hover:bg-[#F4F4F5] focus-visible:outline-2 focus-visible:outline-[#0F766E]">
                Inspect trace
              </NuxtLink>
            </div>
          </li>
        </ul>
        <div v-if="slowestRuns.length" class="hidden overflow-x-auto table-scroll lg:block">
          <table class="w-full border-collapse text-left text-[13px]">
            <thead>
              <tr class="border-b border-[#E4E4E7] bg-[#F4F4F5] text-xs font-medium text-[#52525B]">
                <th class="px-4 py-2">Run</th>
                <th class="px-4 py-2">Group / Caller</th>
                <th class="px-4 py-2">Mode</th>
                <th class="px-4 py-2">Applied / recommended</th>
                <th class="px-4 py-2">Status</th>
                <th class="px-4 py-2 text-right">Subcalls</th>
                <th class="px-4 py-2 text-right">Duration</th>
                <th class="px-4 py-2 text-right">Inspect</th>
              </tr>
            </thead>
            <tbody class="divide-y divide-[#E4E4E7]">
              <tr v-for="r in slowestRuns" :key="r.run_id" class="hover:bg-[#FAFAFA]">
                <td class="px-4 py-2 font-mono-data text-xs text-[#09090B] whitespace-nowrap">{{ r.run_id.slice(0, 12) }}</td>
                <td class="px-4 py-2 text-xs text-[#52525B]">{{ (r.user_groups || []).join(", ") || "Unassigned" }} · {{ r.user_email || r.user_sub }}</td>
                <td class="whitespace-nowrap px-4 py-2 text-xs">{{ modeLabel(r.applied_mode || r.mode) }}</td>
                <td class="px-4 py-2 text-xs">
                  <span>Applied: {{ modeLabel(r.applied_mode || r.mode) }}</span>
                  <span v-if="r.recommended_mode" class="block text-[#52525B]">Adaptive recommendation: {{ modeLabel(r.recommended_mode) }}<span v-if="r.mode_decision_reason?.includes('adaptive_recommendation:')"> · observed only</span></span>
                  <span v-if="r.mode_decision_score != null" class="block tabular-nums text-[#52525B]">Auto suitability score: {{ r.mode_decision_score.toFixed(1) }} / 100</span>
                  <span v-if="r.mode_decision_reason" class="block max-w-[24rem] break-words text-[10px] leading-4 text-[#71717A]">{{ decisionExplanation(r) }}</span>
                  <span v-if="!r.recommended_mode && r.mode_decision_score == null" class="block text-[#71717A]">No adaptive recommendation recorded</span>
                </td>
                <td class="px-4 py-2 text-xs">
                  <span class="rounded border px-2 py-0.5 font-medium" :class="runStatusClass(r)">{{ runStatusLabel(r) }}</span>
                </td>
                <td class="px-4 py-2 text-right tabular-nums">{{ r.subcall_count }}</td>
                <td class="px-4 py-2 text-right font-semibold tabular-nums">{{ r.duration_ms.toFixed(1) }} ms</td>
                <td class="px-4 py-2 text-right">
                  <NuxtLink :to="`/audit?run_id=${encodeURIComponent(r.run_id)}`" class="inline-flex min-h-11 items-center justify-center whitespace-nowrap rounded-md px-3 text-xs font-medium text-[#0F766E] hover:bg-[#F0FDFA] hover:underline focus-visible:outline-2 focus-visible:outline-[#0F766E]">
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
