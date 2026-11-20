<script setup lang="ts">
import { extractErrorMessage } from "~/composables/useAdminApi";

interface PerfRow {
  model: string;
  server: string;
  tool: string;
  mode: string;
  runs: number;
  success_rate_pct: number;
  avg_duration_ms: number;
  avg_tokens: number;
}
interface Arm { runs: number; successPct: number; latencyMs: number; tokens: number }
interface Group { key: string; label: string; model: string; code: Arm | null; classic: Arm | null; savedTokens: number; savedUsd: number }

const { apiFetch } = useAdminApi();
const { totalRunsTick } = useLiveTelemetry();
const { selectedRange } = useReportRange();

const loading = ref(true);
const errorMessage = ref("");
const rows = ref<PerfRow[]>([]);
const prices = ref<Record<string, { input_per_1m_usd: number; output_per_1m_usd: number }>>({});
let seq = 0;

async function load() {
  const id = ++seq;
  errorMessage.value = "";
  try {
    const params = new URLSearchParams({ window: selectedRange.value, limit: "5000" });
    const [perf, pricing] = await Promise.all([
      apiFetch<{ rows: PerfRow[] }>(`/admin/ab/model-performance?${params}`),
      apiFetch<{ models: Record<string, { input_per_1m_usd: number; output_per_1m_usd: number }> }>("/admin/pricing/models").catch(() => ({ models: {} })),
    ]);
    if (id !== seq) return;
    rows.value = Array.isArray(perf?.rows) ? perf.rows : [];
    prices.value = pricing?.models || {};
  } catch (err) {
    if (id === seq) errorMessage.value = extractErrorMessage(err);
  } finally {
    if (id === seq) loading.value = false;
  }
}

onMounted(load);
watch(selectedRange, load);
let tick: ReturnType<typeof setTimeout> | null = null;
watch(totalRunsTick, () => {
  if (tick) clearTimeout(tick);
  tick = setTimeout(load, 1500);
});
onBeforeUnmount(() => { if (tick) clearTimeout(tick); });

const family = (m: string) => (m.startsWith("codemode") ? "code" : m.startsWith("traditional") ? "classic" : "other");

function arm(items: PerfRow[]): Arm | null {
  const runs = items.reduce((s, r) => s + r.runs, 0);
  if (!runs) return null;
  const w = (f: "success_rate_pct" | "avg_duration_ms" | "avg_tokens") => items.reduce((s, r) => s + r.runs * (r[f] || 0), 0) / runs;
  return { runs, successPct: w("success_rate_pct"), latencyMs: w("avg_duration_ms"), tokens: w("avg_tokens") };
}

function build(keyFn: (r: PerfRow) => string, labelFn: (r: PerfRow) => string): Group[] {
  const map = new Map<string, PerfRow[]>();
  for (const r of rows.value) map.set(keyFn(r), [...(map.get(keyFn(r)) || []), r]);
  return [...map.entries()].map(([key, items]) => {
    const code = arm(items.filter((r) => family(r.mode) === "code"));
    const classic = arm(items.filter((r) => family(r.mode) === "classic"));
    const savedTokens = code && classic ? Math.max(0, (classic.tokens - code.tokens) * code.runs) : 0;
    const price = prices.value[items[0].model]?.input_per_1m_usd ?? 0;
    return { key, label: labelFn(items[0]), model: items[0].model, code, classic, savedTokens, savedUsd: (savedTokens / 1_000_000) * price };
  }).sort((a, b) => b.savedTokens - a.savedTokens);
}

const overall = computed(() => ({
  code: arm(rows.value.filter((r) => family(r.mode) === "code")),
  classic: arm(rows.value.filter((r) => family(r.mode) === "classic")),
}));
const byModel = computed(() => build((r) => r.model, (r) => r.model));
const byTool = computed(() => build((r) => `${r.server}/${r.tool}`, (r) => `${r.server} · ${r.tool}`).slice(0, 25));
const totalSavedUsd = computed(() => byModel.value.reduce((s, g) => s + g.savedUsd, 0));
const totalSavedTokens = computed(() => byModel.value.reduce((s, g) => s + g.savedTokens, 0));

const fmt = (n: number | undefined, d = 0) => Number(n || 0).toLocaleString("en-US", { maximumFractionDigits: d });
const usd = (n: number) => `$${n.toFixed(4)}`;
</script>

<template>
  <div class="space-y-6">
    <div>
      <h1 class="text-[20px] font-semibold tracking-[-0.015em] text-[#09090B]">Optimization &amp; ROI</h1>
      <p class="mt-0.5 text-[13px] text-[#52525B]">Code Mode vs Classic MCP measured head to head, and the token and cost difference per model and per tool. Savings are estimates from observed averages and configured input prices.</p>
    </div>

    <div v-if="errorMessage" role="alert" class="flex items-start gap-2 rounded-lg border border-[#FECACA] bg-[#FEF2F2] px-4 py-3 text-[13px] text-[#DC2626]">
      <AppIcon name="alert" :size="16" class="mt-0.5" />
      <span>{{ errorMessage }}</span>
    </div>
    <div v-if="loading" class="h-48 animate-pulse rounded-lg border border-[#E4E4E7] bg-white" />

    <template v-else>
      <section class="grid grid-cols-1 gap-4 lg:grid-cols-2">
        <div v-for="[name, a, color] in [['Code Mode MCP', overall.code, '#0F766E'], ['Classic MCP', overall.classic, '#2563EB']] as const" :key="name" class="rounded-lg border border-[#E4E4E7] bg-white p-4">
          <div class="flex items-center gap-2 text-sm font-semibold"><span class="h-2.5 w-2.5 rounded-full" :style="{ background: color }" />{{ name }}</div>
          <dl v-if="a" class="mt-3 grid grid-cols-2 gap-3 text-xs sm:grid-cols-4">
            <div><dt class="text-[#71717A]">Requests</dt><dd class="text-lg font-semibold tabular-nums">{{ fmt(a.runs) }}</dd></div>
            <div><dt class="text-[#71717A]">Success</dt><dd class="text-lg font-semibold tabular-nums">{{ fmt(a.successPct, 1) }}%</dd></div>
            <div><dt class="text-[#71717A]">Avg latency</dt><dd class="text-lg font-semibold tabular-nums">{{ fmt(a.latencyMs, 1) }} ms</dd></div>
            <div><dt class="text-[#71717A]">Avg tokens</dt><dd class="text-lg font-semibold tabular-nums">{{ fmt(a.tokens) }}</dd></div>
          </dl>
          <p v-else class="mt-3 text-xs text-[#71717A]">No observations in this range.</p>
        </div>
      </section>

      <section class="grid grid-cols-1 gap-4 sm:grid-cols-2">
        <div class="rounded-lg border border-[#E4E4E7] bg-white p-4"><div class="text-xs text-[#52525B]">Estimated token reduction (Code vs Classic)</div><div class="mt-1 text-2xl font-semibold tabular-nums text-[#0F766E]">{{ fmt(totalSavedTokens) }}</div></div>
        <div class="rounded-lg border border-[#E4E4E7] bg-white p-4"><div class="text-xs text-[#52525B]">Estimated input-cost reduction</div><div class="mt-1 text-2xl font-semibold tabular-nums text-[#0F766E]">{{ usd(totalSavedUsd) }}</div></div>
      </section>

      <section v-for="[title, groups, hint] in [['ROI per model', byModel, 'Models with no price configured show $0.'], ['ROI per tool (top 25)', byTool, 'Priced using each tool group’s model.']] as const" :key="title" class="rounded-lg border border-[#E4E4E7] bg-white">
        <div class="border-b border-[#E4E4E7] px-4 py-3"><h2 class="text-[15px] font-semibold">{{ title }}</h2><p class="mt-0.5 text-xs text-[#71717A]">{{ hint }}</p></div>
        <div v-if="!groups.length" class="px-4 py-6 text-center text-xs text-[#71717A]">No data yet. Start traffic from Demo Control.</div>
        <div v-else class="overflow-x-auto">
          <table class="w-full border-collapse text-left text-xs tabular-nums">
            <thead class="border-b border-[#E4E4E7] bg-[#F4F4F5] text-[#52525B]"><tr>
              <th class="px-4 py-2 font-medium">Name</th>
              <th class="px-4 py-2 text-right font-medium">Code runs / success / ms / tokens</th>
              <th class="px-4 py-2 text-right font-medium">Classic runs / success / ms / tokens</th>
              <th class="px-4 py-2 text-right font-medium">Tokens saved</th>
              <th class="px-4 py-2 text-right font-medium">Cost saved</th>
            </tr></thead>
            <tbody class="divide-y divide-[#E4E4E7]">
              <tr v-for="g in groups" :key="g.key">
                <td class="px-4 py-2 font-mono-data font-medium">{{ g.label }}</td>
                <td class="px-4 py-2 text-right text-[#0F766E]">{{ g.code ? `${fmt(g.code.runs)} / ${fmt(g.code.successPct)}% / ${fmt(g.code.latencyMs, 1)} / ${fmt(g.code.tokens)}` : "—" }}</td>
                <td class="px-4 py-2 text-right text-[#2563EB]">{{ g.classic ? `${fmt(g.classic.runs)} / ${fmt(g.classic.successPct)}% / ${fmt(g.classic.latencyMs, 1)} / ${fmt(g.classic.tokens)}` : "—" }}</td>
                <td class="px-4 py-2 text-right">{{ fmt(g.savedTokens) }}</td>
                <td class="px-4 py-2 text-right">{{ usd(g.savedUsd) }}</td>
              </tr>
            </tbody>
          </table>
        </div>
      </section>
    </template>
  </div>
</template>
