<script setup lang="ts">
import { extractErrorMessage } from "~/composables/useAdminApi";

interface TraceSpan {
  stage: string;
  label: string;
  start_ms: number;
  duration_ms: number;
  status: string;
}

interface TraceRun {
  run_id: string;
  created_at?: string;
  mode: string;
  ok: boolean;
  grounding_blocked?: boolean;
  duration_ms: number;
  user_sub: string;
  user_email?: string | null;
  user_groups?: string[];
  subcall_count: number;
  tokens_used_est?: number;
  tokens_saved_by?: string;
  estimated_tokens_saved: number;
  schema_tokens_saved: number;
  monty_spawn_ms?: number;
  monty_exec_ms?: number;
  trace_spans?: TraceSpan[];
}

interface RuntimeTelemetry {
  gateway?: {
    active_executions?: number;
    total_executions?: number;
    rss_mb?: number;
    cpu_percent?: number;
  };
  sandbox_worker?: {
    mode?: string;
    status?: string;
  };
  monty_telemetry?: {
    ephemeral?: boolean;
    codemode_runs?: number;
    grounding_blocked_runs?: number;
    spawn_p50_ms?: number;
    spawn_p95_ms?: number;
    exec_p50_ms?: number;
    exec_p95_ms?: number;
  };
}

const { apiFetch } = useAdminApi();
const { totalRunsTick } = useLiveTelemetry();

const loading = ref(true);
const errorMessage = ref("");
const runs = ref<TraceRun[]>([]);
const runtime = ref<RuntimeTelemetry | null>(null);
const expandedId = ref<string | null>(null);
const liveUpdates = ref(true);

async function fetchTraces() {
  errorMessage.value = "";
  try {
    const [r, rt] = await Promise.all([
      apiFetch<{ runs: TraceRun[] }>("/audit/runs?limit=25"),
      apiFetch<RuntimeTelemetry>("/admin/telemetry/runtime"),
    ]);
    runs.value = r.runs || [];
    runtime.value = rt;
    if (!expandedId.value && runs.value.length > 0) {
      expandedId.value = runs.value[0].run_id;
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

watch(totalRunsTick, () => {
  if (liveUpdates.value) {
    void fetchTraces();
  }
});

function synthesizedSpans(run: TraceRun): TraceSpan[] {
  if (run.trace_spans?.length) return run.trace_spans;
  const total = Math.max(0.5, run.duration_ms || 1);
  return [
    { stage: "gateway_auth", label: "Gateway Auth & RBAC", start_ms: 0, duration_ms: Number((total * 0.2).toFixed(2)), status: "ok" },
    { stage: "mode_route", label: `Mode Routing (${run.mode})`, start_ms: Number((total * 0.2).toFixed(2)), duration_ms: Number((total * 0.6).toFixed(2)), status: run.ok ? "ok" : "error" },
    { stage: "security_guard", label: "ABAC / PII / Grounding", start_ms: Number((total * 0.8).toFixed(2)), duration_ms: Number((total * 0.2).toFixed(2)), status: run.grounding_blocked ? "blocked" : "ok" },
  ];
}

function spanBarStyle(span: TraceSpan, totalMs: number) {
  const denom = Math.max(totalMs, 0.5);
  const left = Math.min(96, Math.max(0, (span.start_ms / denom) * 100));
  const width = Math.max(2, Math.min(100 - left, (span.duration_ms / denom) * 100));
  return { left: `${left.toFixed(1)}%`, width: `${width.toFixed(1)}%` };
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
          Request Traces &amp; Monty Sandbox Timeline
        </h1>
        <p class="mt-0.5 text-[13px] text-[#52525B]">
          Linear-style waterfall of gateway auth, routing, ephemeral Monty sandbox spawn/execution, upstream subcalls, and grounding checks.
        </p>
      </div>
      <div class="flex items-center gap-2">
        <label class="flex items-center gap-2 rounded-md border border-[#E4E4E7] bg-white px-2.5 py-1.5 text-xs font-medium text-[#09090B] cursor-pointer">
          <Switch v-model:checked="liveUpdates">
            <SwitchThumb />
          </Switch>
          <span>Real-Time</span>
        </label>
        <Button @click="fetchTraces">Refresh</Button>
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

    <!-- Monty Sandbox Telemetry Strip -->
    <section v-if="runtime" class="grid grid-cols-1 gap-3 sm:grid-cols-2 lg:grid-cols-4">
      <div class="rounded-lg border border-[#E4E4E7] bg-white p-4">
        <div class="text-xs text-[#52525B]">Monty Sandbox Mode</div>
        <div class="mt-1 text-lg font-semibold text-[#0F766E]">
          Ephemeral ({{ runtime.sandbox_worker?.status || "in_process" }})
        </div>
        <div class="mt-1 text-xs text-[#71717A] tabular-nums">
          {{ runtime.gateway?.active_executions ?? 0 }} active · {{ runtime.gateway?.total_executions ?? 0 }} total
        </div>
      </div>
      <div class="rounded-lg border border-[#E4E4E7] bg-white p-4">
        <div class="text-xs text-[#52525B]">Monty Spawn Latency</div>
        <div class="mt-1 text-lg font-semibold text-[#09090B] tabular-nums">
          p50 {{ (runtime.monty_telemetry?.spawn_p50_ms ?? 0).toFixed(2) }} ms
        </div>
        <div class="mt-1 text-xs text-[#71717A] tabular-nums">
          p95 {{ (runtime.monty_telemetry?.spawn_p95_ms ?? 0).toFixed(2) }} ms
        </div>
      </div>
      <div class="rounded-lg border border-[#E4E4E7] bg-white p-4">
        <div class="text-xs text-[#52525B]">Monty Exec Latency</div>
        <div class="mt-1 text-lg font-semibold text-[#09090B] tabular-nums">
          p50 {{ (runtime.monty_telemetry?.exec_p50_ms ?? 0).toFixed(2) }} ms
        </div>
        <div class="mt-1 text-xs text-[#71717A] tabular-nums">
          p95 {{ (runtime.monty_telemetry?.exec_p95_ms ?? 0).toFixed(2) }} ms
        </div>
      </div>
      <div class="rounded-lg border border-[#E4E4E7] bg-white p-4">
        <div class="text-xs text-[#52525B]">Gateway Footprint</div>
        <div class="mt-1 text-lg font-semibold text-[#09090B] tabular-nums">
          {{ (runtime.gateway?.rss_mb ?? 0).toFixed(1) }} MB RSS
        </div>
        <div class="mt-1 text-xs text-[#71717A] tabular-nums">
          CPU {{ (runtime.gateway?.cpu_percent ?? 0).toFixed(1) }}% · Grounding blocks {{ runtime.monty_telemetry?.grounding_blocked_runs ?? 0 }}
        </div>
      </div>
    </section>

    <div v-if="loading" class="h-48 animate-pulse rounded-lg border border-[#E4E4E7] bg-white" />

    <!-- Linear-style Waterfall List -->
    <section v-else class="space-y-3">
      <div v-if="runs.length === 0" class="rounded-lg border border-[#E4E4E7] bg-white p-8 text-center text-xs text-[#71717A]">
        No traces recorded yet. Click Burst +8 in the header to generate live traces.
      </div>

      <div
        v-for="r in runs"
        :key="r.run_id"
        class="rounded-lg border border-[#E4E4E7] bg-white overflow-hidden"
      >
        <button
          type="button"
          class="flex w-full flex-wrap items-center justify-between gap-2 px-4 py-3 text-left hover:bg-[#FAFAFA]"
          @click="expandedId = expandedId === r.run_id ? null : r.run_id"
        >
          <div class="flex items-center gap-3 min-w-0">
            <span class="font-mono-data text-xs font-semibold text-[#09090B]">{{ r.run_id.slice(0, 14) }}</span>
            <span
              class="rounded border px-2 py-0.5 text-[11px] font-medium"
              :class="
                r.grounding_blocked
                  ? 'border-[#FDE68A] bg-[#FFFBEB] text-[#92400E]'
                  : r.ok
                    ? 'border-[#A7F3D0] bg-[#ECFDF5] text-[#059669]'
                    : 'border-[#FECACA] bg-[#FEF2F2] text-[#DC2626]'
              "
            >
              {{ r.grounding_blocked ? "grounding blocked" : r.ok ? "ok" : "error" }}
            </span>
            <span class="font-mono-data text-xs" :class="r.mode.startsWith('codemode') ? 'text-[#0F766E]' : 'text-[#2563EB]'">
              {{ r.mode }}
            </span>
            <span class="truncate text-xs text-[#52525B]">
              {{ r.user_email || r.user_sub }} ({{ (r.user_groups || []).join(", ") || "open" }})
            </span>
          </div>
          <div class="flex items-center gap-4 text-xs tabular-nums text-[#52525B]">
            <span>{{ r.subcall_count }} subcalls</span>
            <span class="text-[#0F766E]">
              +{{ (r.estimated_tokens_saved + r.schema_tokens_saved).toLocaleString("en-US") }} tok saved
            </span>
            <span class="font-semibold text-[#09090B]">{{ r.duration_ms.toFixed(1) }} ms</span>
          </div>
        </button>

        <div v-if="expandedId === r.run_id" class="border-t border-[#E4E4E7] bg-[#FAFAFA] p-4 space-y-3">
          <div class="flex flex-wrap items-center justify-between gap-2 text-[11px] text-[#52525B]">
            <div>
              <strong>Tokens Used:</strong> {{ (r.tokens_used_est ?? 0).toLocaleString("en-US") }}
              <span class="mx-2">·</span>
              <strong>Tokens Saved:</strong>
              <span class="text-[#0F766E] font-medium">
                {{ (r.estimated_tokens_saved + r.schema_tokens_saved).toLocaleString("en-US") }}
              </span>
              <span v-if="r.tokens_saved_by" class="ml-1">({{ r.tokens_saved_by }})</span>
            </div>
            <div v-if="r.monty_spawn_ms || r.monty_exec_ms" class="font-mono-data tabular-nums">
              Monty spawn {{ (r.monty_spawn_ms ?? 0).toFixed(2) }}ms · exec {{ (r.monty_exec_ms ?? 0).toFixed(2) }}ms
            </div>
          </div>

          <div class="space-y-1.5 rounded-md border border-[#E4E4E7] bg-white p-3">
            <div
              v-for="(sp, idx) in synthesizedSpans(r)"
              :key="idx"
              class="grid grid-cols-[12rem_1fr_4.5rem] items-center gap-2 text-[11px]"
            >
              <div class="truncate font-medium text-[#09090B]" :title="sp.label">{{ sp.label }}</div>
              <div class="relative h-3 rounded bg-[#F4F4F5] overflow-hidden">
                <div
                  class="absolute top-0 bottom-0 rounded"
                  :class="spanBarColor(sp.stage, sp.status)"
                  :style="spanBarStyle(sp, r.duration_ms)"
                />
              </div>
              <div class="text-right font-mono-data text-[#52525B] tabular-nums">
                {{ sp.duration_ms.toFixed(2) }} ms
              </div>
            </div>
          </div>
        </div>
      </div>
    </section>
  </div>
</template>
