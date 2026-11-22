<script setup lang="ts">
import { Tabs, TabsList, TabsPanel, TabsTab } from "@ui-sdk/vue/tabs";
import {
  Select, SelectContent, SelectItem, SelectLabel, SelectTrigger, SelectValue,
} from "@ui-sdk/vue/select";
import { Field, FieldLabel } from "@ui-sdk/vue/field";
import { Input } from "@ui-sdk/vue/input";
import { Button } from "@ui-sdk/vue/button";
import { computed, onMounted, ref, watch } from "vue";
import { extractErrorMessage } from "~/composables/useAdminApi";

interface Arm { correct_pct: number; grounded_pct: number; verified_pct: number; mean_cost_usd: number | null; mean_provider_tokens: number | null; p95_wall_ms: number | null; mean_upstream_calls: number | null; mean_recovery: number | null }
interface LiveRouting { recommended_mode: string | null; evidence_tier: string | null; evidence_model?: string; evidence_workload?: string; pairs: number; qualification?: string; latest_evidence_at?: string | null; reason: string; status: string; traditional?: Arm | null; codemode?: Arm | null }
interface Row { model: string; workload: string; pairs: number; qualification?: string; latest_evidence_at?: string | null; evidence_tier?: string; traditional: Arm | null; codemode: Arm | null; recommended_mode: string | null; simulated_recommendation?: string | null; simulated_recommendation_reason?: string; reason: string; live_routing?: LiveRouting | null }
const { apiFetch } = useAdminApi();
const { selectedRange } = useReportRange();
const route = useRoute();
const rows = ref<Row[]>([]);
const adaptiveMode = ref("observe");
const loading = ref(true);
const error = ref("");
const query = ref("");
const modelFilter = ref("all");
const recommendationFilter = ref("all");
const modeFilter = ref("all");
const expanded = ref<string[]>([]);
const models = computed(() => [...new Set(rows.value.map((r) => r.model))].sort());
const modelItems = computed(() => [{ value: "all", label: "All models" }, ...models.value.map((model) => ({ value: model, label: model }))]);
const recommendationItems = [
  { value: "all", label: "All recommendations" }, { value: "codemode", label: "Code Mode" },
  { value: "traditional", label: "Classic" }, { value: "none", label: "No recommendation" },
];
const evidenceItems = [
  { value: "all", label: "Both strategies" }, { value: "traditional", label: "Classic data present" },
  { value: "codemode", label: "Code Mode data present" },
];
const filtered = computed(() => rows.value.filter((r) => {
  const text = `${r.model} ${workloadLabel(r.workload)}`.toLowerCase();
  const recommendation = r.evidence_tier === "demo_simulated" ? r.simulated_recommendation : r.recommended_mode;
  return (!query.value || text.includes(query.value.toLowerCase())) &&
    (modelFilter.value === "all" || modelFilter.value === r.model) &&
    (recommendationFilter.value === "all" || (recommendation || "none") === recommendationFilter.value) &&
    (modeFilter.value === "all" || (modeFilter.value === "traditional" ? !!r.traditional : !!r.codemode));
}));
const fmt = (n: number | null | undefined, d = 1) => n == null ? "Unknown" : Number(n).toLocaleString("en-US", { maximumFractionDigits: d });
const money = (n: number | null | undefined) => n == null ? "Unknown" : `$${n.toFixed(6)}`;
const cost = (a: Arm | null) => a?.mean_cost_usd != null ? money(a.mean_cost_usd) : a?.mean_provider_tokens != null ? `${fmt(a.mean_provider_tokens, 0)} tokens` : "Unknown";
const signed = (n: number, digits = 1) => `${n > 0 ? "+" : ""}${fmt(n, digits)}`;
const signedMoney = (n: number) => `${n < 0 ? "-" : "+"}$${Math.abs(n).toFixed(6)}`;
const workloadLabel = (value: string) => ({
  single_read: "Single read", small_fanout: "Small fan out", large_fanout: "Large fan out",
  data_reconciliation: "Data reconciliation", large_data_reconciliation: "Large data reconciliation",
  batch_compute: "Batch computation", sequential_workflow: "Sequential workflow",
  mutating_workflow: "Mutating workflow", large_payload_processing: "Large payload processing",
}[value] || value);
function toggle(key: string) { expanded.value = expanded.value.includes(key) ? expanded.value.filter((x) => x !== key) : [...expanded.value, key]; }
const recommendationLabel = (row: Row) => {
  const recommendation = row.evidence_tier === "demo_simulated" ? row.simulated_recommendation : row.recommended_mode;
  const mode = recommendation === "codemode" ? "Code Mode" : recommendation === "traditional" ? "Classic" : "No recommendation";
  return recommendation
    ? `${row.evidence_tier === "demo_simulated" ? "Illustrative" : "Recommended"}: ${mode}`
    : mode;
};
const liveRoutingLabel = (row: Row) => {
  if (row.evidence_tier === "demo_simulated") return "Simulated evidence · never used for live routing";
  const live = row.live_routing;
  if (!live?.recommended_mode) return `Auto heuristic remains in control · ${live?.reason || "No eligible recommendation"}`;
  const action = adaptiveMode.value === "active" ? "Applied to eligible Auto requests" : "Observed only";
  const mode = live.recommended_mode === "codemode" ? "Code Mode" : "Classic";
  const tier = live.evidence_tier === "workload_family" ? "workload family" : live.evidence_tier === "model_wide" ? "model-wide" : "exact workload";
  return `${action}: ${mode} · ${tier} · ${live.pairs} matched pairs`;
};
let requestSequence = 0;
async function load() {
  const requestId = ++requestSequence;
  loading.value = true; error.value = "";
  try {
    const window = selectedRange.value;
    const result = await apiFetch<{ rows: Row[]; adaptive_mode?: string; routing_window_days?: number }>(`/admin/adaptive-execution/report?window=${encodeURIComponent(window)}`);
    if (requestId !== requestSequence) return;
    rows.value = result.rows || [];
    adaptiveMode.value = result.adaptive_mode || "observe";
  } catch (err) {
    if (requestId === requestSequence) error.value = extractErrorMessage(err);
  } finally {
    if (requestId === requestSequence) loading.value = false;
  }
}
onMounted(load);
watch(selectedRange, load);
const viewTab = (value: unknown) => value === "discovery" || value === "efficiency" ? value : "routing";
const selectedTab = ref(viewTab(route.query.tab));
watch(() => route.query.tab, (value) => { selectedTab.value = viewTab(value); });
watch(selectedTab, (value) => {
  const tab = viewTab(value);
  if (route.query.tab !== tab) void navigateTo({ query: { ...route.query, tab } }, { replace: true });
});
</script>

<template>
  <div class="space-y-5">
    <header>
      <h1 class="text-[20px] font-semibold tracking-[-0.015em] text-[#09090B]">Model Performance</h1>
      <p v-if="selectedTab !== 'discovery'" class="mt-1 text-[13px] text-[#52525B]">Compare Classic and Code Mode using paired workflow evidence. Live adaptive routing always uses qualified evidence from the last 30 days; cohort metrics below use the selected report window.</p>
    </header>
    <template v-if="selectedTab !== 'discovery'">
    <div v-if="error" role="alert" class="rounded-lg border border-[#FECACA] bg-[#FEF2F2] p-3 text-sm text-[#B91C1C]">{{ error }} <Button class="ml-2 min-h-11 rounded border border-[#D4D4D8] px-3 text-sm" @click="load">Retry report</Button></div>
    <div v-if="loading" aria-label="Loading workflow evidence" class="h-32 animate-pulse rounded-lg border border-[#E4E4E7] bg-white" />
    <section v-if="!loading" class="rounded-lg border border-[#E4E4E7] bg-white p-3" aria-label="Adaptive routing status">
      <p class="text-sm font-semibold text-[#09090B]">Adaptive routing: {{ adaptiveMode === "active" ? "Active" : "Observe" }}</p>
      <p class="mt-1 text-xs leading-5 text-[#52525B]">{{ adaptiveMode === "active" ? "Qualified recommendations are applied" : "Recommendations are recorded but not applied" }} only for Auto requests with a known model, a recognized workload, and read-only tools. Otherwise normal Auto routing decides. Live decisions use real paired evidence from the last 30 days; simulated demo evidence is excluded.</p>
      <NuxtLink to="/settings?tab=routing" class="mt-2 inline-block min-h-11 py-3 text-xs font-medium text-[#0F766E] underline underline-offset-2">Manage adaptive routing</NuxtLink>
    </section>
    <div v-if="!loading" class="grid grid-cols-1 gap-3 rounded-lg border border-[#E4E4E7] bg-white p-3 sm:grid-cols-2 lg:grid-cols-4">
          <Field><FieldLabel class="text-xs text-[#52525B]">Search model or workload</FieldLabel><Input v-model="query" type="search" class="mt-1 min-h-11 w-full rounded-md border border-[#D4D4D8] px-3 text-sm text-[#09090B] focus-visible:outline-2 focus-visible:outline-[#0F766E]" /></Field>
          <div class="min-w-0"><Select v-model:value="modelFilter" :items="modelItems">
            <SelectLabel class="block text-xs text-[#52525B]">Model</SelectLabel>
            <SelectTrigger class="mt-1 flex min-h-11 w-full items-center justify-between rounded-md border border-[#D4D4D8] bg-white px-3 text-sm focus-visible:outline-2 focus-visible:outline-[#0F766E]"><SelectValue /></SelectTrigger>
            <SelectContent class="z-50 max-h-64 overflow-auto rounded-md border border-[#E4E4E7] bg-white p-1 shadow-[0_4px_12px_rgb(9_9_11_/_0.08)]">
              <SelectItem v-for="item in modelItems" :key="item.value" :value="item.value" class="min-h-11 cursor-pointer rounded px-3 py-2 text-sm data-[highlighted]:bg-[#F4F4F5] data-[selected]:font-medium">{{ item.label }}</SelectItem>
            </SelectContent>
          </Select></div>
          <div class="min-w-0"><Select v-model:value="recommendationFilter" :items="recommendationItems">
            <SelectLabel class="block text-xs text-[#52525B]">Selected-window analysis</SelectLabel>
            <SelectTrigger class="mt-1 flex min-h-11 w-full items-center justify-between rounded-md border border-[#D4D4D8] bg-white px-3 text-sm focus-visible:outline-2 focus-visible:outline-[#0F766E]"><SelectValue /></SelectTrigger>
            <SelectContent class="z-50 max-h-64 overflow-auto rounded-md border border-[#E4E4E7] bg-white p-1 shadow-[0_4px_12px_rgb(9_9_11_/_0.08)]">
              <SelectItem v-for="item in recommendationItems" :key="item.value" :value="item.value" class="min-h-11 cursor-pointer rounded px-3 py-2 text-sm data-[highlighted]:bg-[#F4F4F5] data-[selected]:font-medium">{{ item.label }}</SelectItem>
            </SelectContent>
          </Select></div>
          <div class="min-w-0"><Select v-model:value="modeFilter" :items="evidenceItems">
            <SelectLabel class="block text-xs text-[#52525B]">Evidence arm</SelectLabel>
            <SelectTrigger class="mt-1 flex min-h-11 w-full items-center justify-between rounded-md border border-[#D4D4D8] bg-white px-3 text-sm focus-visible:outline-2 focus-visible:outline-[#0F766E]"><SelectValue /></SelectTrigger>
            <SelectContent class="z-50 max-h-64 overflow-auto rounded-md border border-[#E4E4E7] bg-white p-1 shadow-[0_4px_12px_rgb(9_9_11_/_0.08)]">
              <SelectItem v-for="item in evidenceItems" :key="item.value" :value="item.value" class="min-h-11 cursor-pointer rounded px-3 py-2 text-sm data-[highlighted]:bg-[#F4F4F5] data-[selected]:font-medium">{{ item.label }}</SelectItem>
            </SelectContent>
          </Select></div>
        </div>
    </template>
    <p v-if="!loading && rows.some((row) => row.evidence_tier === 'demo_simulated')" class="rounded-lg border border-[#FDE68A] bg-[#FFFBEB] px-3 py-2 text-xs leading-5 text-[#92400E]">Demo rows use deterministic per-model and per-workload token, latency, turn, and outcome variation with configured model prices. “Illustrative” recommendations exercise report logic only; simulated rows never change live routing. Use real paired evidence to validate production recommendations.</p>
    <Tabs v-model:value="selectedTab" class="space-y-4">
      <TabsList aria-label="Model performance views" class="flex min-h-11 gap-1 border-b border-[#E4E4E7]">
        <TabsTab value="routing" class="min-h-11 border-b-2 px-4 text-sm data-[selected]:border-[#0F766E] data-[selected]:font-semibold data-[selected]:text-[#0F766E]">Adaptive routing</TabsTab>
        <TabsTab value="efficiency" class="min-h-11 border-b-2 px-4 text-sm data-[selected]:border-[#0F766E] data-[selected]:font-semibold data-[selected]:text-[#0F766E]">Efficiency</TabsTab>
        <TabsTab value="discovery" class="min-h-11 border-b-2 px-3 text-sm data-[selected]:border-[#0F766E] data-[selected]:font-semibold data-[selected]:text-[#0F766E]">Tool discovery</TabsTab>
      </TabsList>
      <TabsPanel value="routing" class="space-y-4">
        <div v-if="!filtered.length" class="rounded-lg border border-[#E4E4E7] bg-white p-8 text-center text-sm text-[#52525B]">No matched workflow evidence in this range. <NuxtLink to="/demo" class="font-medium text-[#0F766E] underline underline-offset-2">Generate simulated demo evidence</NuxtLink> or import paired benchmark evidence to populate the report.</div>
        <template v-else>
        <div class="divide-y divide-[#E4E4E7] rounded-lg border border-[#E4E4E7] bg-white md:hidden">
          <article v-for="row in filtered" :key="`mobile:${row.model}:${row.workload}`" class="min-w-0 px-4 py-3">
            <Button class="flex min-h-11 w-full max-w-full flex-col items-start justify-center text-left focus-visible:outline-2 focus-visible:outline-[#0F766E]" :aria-label="`${row.model} ${workloadLabel(row.workload)} details`" :aria-expanded="expanded.includes(`${row.model}:${row.workload}`)" @click="toggle(`${row.model}:${row.workload}`)">
              <span class="w-full max-w-full whitespace-normal break-words font-medium text-[#09090B]">{{ row.model }}<span v-if="row.evidence_tier === 'demo_simulated'" class="ml-2 rounded border border-[#FDE68A] px-1.5 py-0.5 text-[10px] text-[#92400E]">simulated</span></span>
              <span class="w-full max-w-full whitespace-normal break-words text-sm text-[#52525B]">{{ workloadLabel(row.workload) }} · {{ row.pairs }} matched pairs</span>
              <span class="mt-1 w-full max-w-full whitespace-normal break-words text-xs text-[#52525B]">{{ liveRoutingLabel(row) }}</span>
              <span class="w-full max-w-full whitespace-normal break-words text-xs text-[#71717A]">Selected window: {{ recommendationLabel(row) }} · verified {{ fmt(row.traditional?.verified_pct) }}% / {{ fmt(row.codemode?.verified_pct) }}%</span>
            </Button>
            <dl v-if="expanded.includes(`${row.model}:${row.workload}`)" class="mt-3 grid grid-cols-2 gap-x-3 gap-y-2 border-t border-[#E4E4E7] pt-3 text-xs">
              <div><dt class="text-[#71717A]">Cost / completion · Classic / Code</dt><dd class="tabular-nums">{{ cost(row.traditional) }} / {{ cost(row.codemode) }}</dd></div>
              <div><dt class="text-[#71717A]">p95 wall time · Classic / Code</dt><dd class="tabular-nums">{{ fmt(row.traditional?.p95_wall_ms) }} / {{ fmt(row.codemode?.p95_wall_ms) }} ms</dd></div>
              <div><dt class="text-[#71717A]">Upstream calls · Classic / Code</dt><dd class="tabular-nums">{{ fmt(row.traditional?.mean_upstream_calls) }} / {{ fmt(row.codemode?.mean_upstream_calls) }}</dd></div>
              <div><dt class="text-[#71717A]">Recovery events per workflow · Classic / Code</dt><dd class="tabular-nums">{{ fmt(row.traditional?.mean_recovery) }} / {{ fmt(row.codemode?.mean_recovery) }}</dd></div>
              <div class="col-span-2"><dt class="text-[#71717A]">Live routing explanation</dt><dd>{{ liveRoutingLabel(row) }}. {{ row.live_routing?.reason || row.reason }}<span v-if="row.live_routing?.recommended_mode"> Evidence: {{ row.live_routing.evidence_model }} · {{ workloadLabel(row.live_routing.evidence_workload || row.workload) }} · {{ row.live_routing.pairs }} matched pairs · latest {{ row.live_routing.latest_evidence_at || "unknown" }}.</span></dd></div>
              <div class="col-span-2"><dt class="text-[#71717A]">Selected-window analysis</dt><dd>{{ row.evidence_tier === 'demo_simulated' ? (row.simulated_recommendation_reason || row.reason) : row.reason }} · {{ row.evidence_tier || "exact" }} evidence · {{ row.qualification || "sparse" }} · latest {{ row.latest_evidence_at || "unknown" }}</dd></div>
            </dl>
          </article>
        </div>
        <div class="hidden overflow-x-auto rounded-lg border border-[#E4E4E7] bg-white md:block">
          <table class="w-full min-w-[1100px] border-collapse text-left text-xs">
            <thead class="bg-[#F4F4F5] text-[#52525B]"><tr><th class="px-3 py-3">Model / workload</th><th class="px-3 py-3">Adaptive result for Auto</th><th class="px-3 py-3 text-right">Pairs in selected window</th><th class="px-3 py-3 text-right">Verified rate · Classic / Code</th><th class="px-3 py-3 text-right">Cost / completion · Classic / Code</th><th class="px-3 py-3 text-right">p95 wall time · Classic / Code</th></tr></thead>
            <tbody><template v-for="row in filtered" :key="`${row.model}:${row.workload}`"><tr class="border-t border-[#E4E4E7] hover:bg-[#FAFAFA]"><td class="px-3 py-3"><Button class="min-h-11 text-left font-medium text-[#09090B] underline decoration-[#A1A1AA] underline-offset-2 focus-visible:outline-2 focus-visible:outline-[#0F766E]" :aria-label="`${row.model} ${workloadLabel(row.workload)} details`" :aria-expanded="expanded.includes(`${row.model}:${row.workload}`)" @click="toggle(`${row.model}:${row.workload}`)">{{ row.model }}<span v-if="row.evidence_tier === 'demo_simulated'" class="ml-2 rounded border border-[#FDE68A] px-1.5 py-0.5 text-[10px] text-[#92400E]">simulated</span><span class="block font-normal text-[#71717A]">{{ workloadLabel(row.workload) }}</span></Button></td><td class="px-3 py-3">{{ liveRoutingLabel(row) }}</td><td class="px-3 py-3 text-right tabular-nums">{{ row.pairs }}</td><td class="px-3 py-3 text-right tabular-nums">Classic {{ fmt(row.traditional?.verified_pct) }}% · Code {{ fmt(row.codemode?.verified_pct) }}%</td><td class="px-3 py-3 text-right tabular-nums">{{ cost(row.traditional) }} · {{ cost(row.codemode) }}</td><td class="px-3 py-3 text-right tabular-nums">{{ fmt(row.traditional?.p95_wall_ms) }} · {{ fmt(row.codemode?.p95_wall_ms) }} ms</td></tr>
              <tr v-if="expanded.includes(`${row.model}:${row.workload}`)" class="border-t border-[#E4E4E7] bg-[#FAFAFA]"><td colspan="6" class="px-4 py-4"><p class="font-medium text-[#09090B]">Live routing: {{ liveRoutingLabel(row) }}</p><p class="mt-1 text-[#52525B]">{{ row.live_routing?.reason || (row.evidence_tier === 'demo_simulated' ? 'Simulated data is excluded from live routing.' : row.reason) }}</p><p v-if="row.live_routing?.recommended_mode" class="mt-1 text-[#71717A]">Evidence tier: {{ row.live_routing.evidence_tier }} · {{ row.live_routing.pairs }} matched pairs · {{ row.live_routing.qualification }} · latest {{ row.live_routing.latest_evidence_at || "unknown" }}</p><p class="mt-3 font-medium text-[#09090B]">Selected-window analysis: {{ recommendationLabel(row) }}</p><p class="mt-1 text-[#52525B]">{{ row.evidence_tier === 'demo_simulated' ? (row.simulated_recommendation_reason || row.reason) : row.reason }}<span v-if="row.evidence_tier === 'demo_simulated'"> Illustrative only; this result cannot affect live routing.</span></p><p class="mt-1 text-[#71717A]">{{ row.evidence_tier || "exact" }} evidence · {{ row.qualification || "sparse" }} · latest {{ row.latest_evidence_at || "unknown" }}</p><p class="mt-2 text-[#52525B]">Calls per completion: {{ fmt(row.traditional?.mean_upstream_calls) }} / {{ fmt(row.codemode?.mean_upstream_calls) }} · Recovery events per workflow: {{ fmt(row.traditional?.mean_recovery) }} / {{ fmt(row.codemode?.mean_recovery) }}</p><p class="mt-1 text-[#71717A]">Correct and grounded: {{ fmt(row.traditional?.correct_pct) }}% / {{ fmt(row.traditional?.grounded_pct) }}% · {{ fmt(row.codemode?.correct_pct) }}% / {{ fmt(row.codemode?.grounded_pct) }}%</p></td></tr>
            </template></tbody>
          </table>
        </div>
        </template>
      </TabsPanel>
      <TabsPanel value="efficiency" class="rounded-lg border border-[#E4E4E7] bg-white p-4">
        <h2 class="text-[15px] font-semibold text-[#09090B]">Matched cohort differences</h2>
        <p class="mt-1 text-sm text-[#52525B]">These differences use the selected report window, not the live routing window. Values are Code Mode minus Classic; positive cost, latency, calls, or recovery indicate a regression. Quality remains the routing priority.</p>
        <div class="mt-4 divide-y divide-[#E4E4E7]"><div v-for="row in filtered" :key="`${row.model}:${row.workload}:delta`" class="py-3 text-xs"><h3 class="font-medium text-[#09090B]">{{ row.model }} · {{ workloadLabel(row.workload) }} ({{ row.pairs }} pairs)</h3><dl class="mt-2 grid grid-cols-1 gap-2 tabular-nums sm:grid-cols-2 lg:grid-cols-5"><div><dt class="text-[#71717A]">Cost per completion (Code − Classic)</dt><dd>{{ row.traditional?.mean_cost_usd != null && row.codemode?.mean_cost_usd != null ? signedMoney(row.codemode.mean_cost_usd - row.traditional.mean_cost_usd) : row.traditional?.mean_provider_tokens != null && row.codemode?.mean_provider_tokens != null ? `${signed(row.codemode.mean_provider_tokens - row.traditional.mean_provider_tokens, 0)} tokens` : "Unknown" }}</dd></div><div><dt class="text-[#71717A]">Provider tokens / completion</dt><dd>{{ row.traditional?.mean_provider_tokens == null || row.codemode?.mean_provider_tokens == null ? "Unknown" : `${signed(row.codemode.mean_provider_tokens - row.traditional.mean_provider_tokens, 0)} tokens` }}</dd></div><div><dt class="text-[#71717A]">p95 wall time</dt><dd>{{ row.traditional?.p95_wall_ms == null || row.codemode?.p95_wall_ms == null ? "Unknown" : `${signed(row.codemode.p95_wall_ms - row.traditional.p95_wall_ms)} ms` }}</dd></div><div><dt class="text-[#71717A]">Upstream calls per completion</dt><dd>{{ row.traditional?.mean_upstream_calls == null || row.codemode?.mean_upstream_calls == null ? "Unknown" : signed(row.codemode.mean_upstream_calls - row.traditional.mean_upstream_calls) }}</dd></div><div><dt class="text-[#71717A]">Recovery events per workflow (Code − Classic)</dt><dd>{{ row.traditional?.mean_recovery == null || row.codemode?.mean_recovery == null ? "Unknown" : signed(row.codemode.mean_recovery - row.traditional.mean_recovery) }}</dd></div></dl></div></div>
      </TabsPanel>
      <TabsPanel value="discovery"><DiscoveryReport v-if="selectedTab === 'discovery'" /></TabsPanel>
    </Tabs>
  </div>
</template>
