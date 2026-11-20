<script setup lang="ts">
import { computed, onMounted, onUnmounted, ref, watch } from "vue";
import { extractErrorMessage } from "~/composables/useAdminApi";

interface ModelPerfRow {
  model: string;
  server: string;
  tool: string;
  mode: string;
  runs: number;
  success_rate_pct: number;
  avg_duration_ms: number;
  avg_tokens: number;
  recommended_mode?: string | null;
}

interface ArmSummary {
  runs: number;
  successPct: number;
  avgLatencyMs: number;
  avgTokens: number;
}

interface ModelToolPair {
  key: string;
  model: string;
  server: string;
  tool: string;
  code: ArmSummary | null;
  classic: ArmSummary | null;
  hybridRuns: number;
  otherRuns: number;
  recommendation: string;
}

const { apiFetch } = useAdminApi();
const rows = ref<ModelPerfRow[]>([]);
const loading = ref(true);
const errorMessage = ref("");
const searchQuery = ref("");
const selectedRecommendation = ref("all");
const selectedMode = ref("all");
const showRawObservations = ref(false);
const { selectedRange: selectedWindow } = useReportRange();

const calibrationPolicy = ref<{
  turn_penalty_pts: number;
  tokens_per_penalty_pt: number;
  calibration_runs_per_arm: number;
} | null>(null);

let pollTimer: ReturnType<typeof setInterval> | null = null;
let requestSequence = 0;

async function loadData() {
  const requestId = ++requestSequence;
  errorMessage.value = "";
  try {
    const params = new URLSearchParams({ window: selectedWindow.value, limit: "5000" });
    const res = await apiFetch<{ rows: ModelPerfRow[] }>(
      `/admin/ab/model-performance?${params.toString()}`
    );
    if (requestId !== requestSequence) return;
    rows.value = Array.isArray(res?.rows) ? res.rows : [];
  } catch (err) {
    if (requestId === requestSequence) errorMessage.value = extractErrorMessage(err);
  } finally {
    if (requestId === requestSequence) loading.value = false;
  }
}

async function loadCalibrationPolicy() {
  try {
    calibrationPolicy.value = await apiFetch<{
      turn_penalty_pts: number;
      tokens_per_penalty_pt: number;
      calibration_runs_per_arm: number;
    }>("/admin/ab/settings");
  } catch {
    calibrationPolicy.value = null;
  }
}

function modeFamily(mode: string): "code" | "classic" | "hybrid" | "other" {
  const normalized = (mode || "").toLowerCase();
  if (normalized === "codemode" || normalized === "codemode_upfront") return "code";
  if (normalized === "traditional" || normalized === "traditional_discover") return "classic";
  if (normalized === "hybrid") return "hybrid";
  return "other";
}

function modeLabel(mode: string): string {
  if (mode === "codemode" || mode === "codemode_upfront") return "Code Mode MCP";
  if (mode === "traditional" || mode === "traditional_discover") return "Classic MCP";
  if (mode === "hybrid") return "Hybrid";
  return mode || "Unknown";
}

function recommendationLabel(mode: string): string {
  if (mode === "codemode") return "Code Mode";
  if (mode === "traditional") return "Classic MCP";
  if (mode === "varies") return "Varies by observation";
  if (!mode || mode === "—" || mode === "none") return "No recommendation yet";
  return modeLabel(mode);
}

function summarizeArm(items: ModelPerfRow[]): ArmSummary | null {
  const runs = items.reduce((sum, item) => sum + Math.max(0, item.runs || 0), 0);
  if (!runs) return null;
  const weighted = (field: "success_rate_pct" | "avg_duration_ms" | "avg_tokens") =>
    items.reduce((sum, item) => sum + Math.max(0, item.runs || 0) * Number(item[field] || 0), 0) / runs;
  return {
    runs,
    successPct: weighted("success_rate_pct"),
    avgLatencyMs: weighted("avg_duration_ms"),
    avgTokens: weighted("avg_tokens"),
  };
}

const allPairs = computed<ModelToolPair[]>(() => {
  const grouped = new Map<string, ModelPerfRow[]>();
  for (const row of rows.value) {
    const server = row.server || "gateway";
    const key = `${row.model}\u0000${server}\u0000${row.tool}`;
    const current = grouped.get(key) || [];
    current.push(row);
    grouped.set(key, current);
  }

  return [...grouped.entries()].map(([key, items]) => {
    const recommendations = new Set(
      items
        .map((item) => {
          const mode = String(item.recommended_mode || "").trim();
          const family = modeFamily(mode);
          return family === "code" ? "codemode" : family === "classic" ? "traditional" : family === "hybrid" ? "hybrid" : mode;
        })
        .filter((mode) => mode && mode !== "—" && mode !== "none")
    );
    const recommendation = recommendations.size === 1
      ? [...recommendations][0]
      : recommendations.size > 1 ? "varies" : "none";
    const codeItems = items.filter((item) => modeFamily(item.mode) === "code");
    const classicItems = items.filter((item) => modeFamily(item.mode) === "classic");
    return {
      key,
      model: items[0].model,
      server: items[0].server || "gateway",
      tool: items[0].tool,
      code: summarizeArm(codeItems),
      classic: summarizeArm(classicItems),
      hybridRuns: items.filter((item) => modeFamily(item.mode) === "hybrid").reduce((sum, item) => sum + item.runs, 0),
      otherRuns: items.filter((item) => modeFamily(item.mode) === "other").reduce((sum, item) => sum + item.runs, 0),
      recommendation,
    };
  }).sort((a, b) => a.model.localeCompare(b.model) || a.server.localeCompare(b.server) || a.tool.localeCompare(b.tool));
});

const filteredPairs = computed(() => {
  const query = searchQuery.value.trim().toLowerCase();
  return allPairs.value.filter((pair) => {
    if (selectedRecommendation.value !== "all") {
      if (selectedRecommendation.value === "unresolved") {
        if (pair.recommendation !== "none" && pair.recommendation !== "varies") return false;
      } else if (pair.recommendation !== selectedRecommendation.value) {
        return false;
      }
    }
    if (!query) return true;
    return `${pair.model} ${pair.server} ${pair.tool}`.toLowerCase().includes(query);
  });
});

const filteredObservations = computed(() => {
  const pairKeys = new Set(filteredPairs.value.map((pair) => pair.key));
  return rows.value.filter((row) => {
    const key = `${row.model}\u0000${row.server || "gateway"}\u0000${row.tool}`;
    return pairKeys.has(key) && (selectedMode.value === "all" || modeFamily(row.mode) === selectedMode.value);
  });
});

const totalPairRuns = computed(() => filteredPairs.value.reduce((sum, pair) =>
  sum + (pair.code?.runs || 0) + (pair.classic?.runs || 0) + pair.hybridRuns + pair.otherRuns, 0));
const modelsTracked = computed(() => new Set(filteredPairs.value.map((pair) => pair.model)).size);
const serversTracked = computed(() => new Set(filteredPairs.value.map((pair) => pair.server)).size);
const decidedPairs = computed(() => filteredPairs.value.filter((pair) =>
  modeFamily(pair.recommendation) === "code" || modeFamily(pair.recommendation) === "classic"));
const codeModeRecommendedPairs = computed(() => decidedPairs.value.filter((pair) => modeFamily(pair.recommendation) === "code").length);
const classicRecommendedPairs = computed(() => decidedPairs.value.filter((pair) => modeFamily(pair.recommendation) === "classic").length);
const unresolvedPairs = computed(() => filteredPairs.value.length - decidedPairs.value.length);
const codeModeRecommendationPct = computed(() => decidedPairs.value.length
  ? Math.round((codeModeRecommendedPairs.value / decidedPairs.value.length) * 100)
  : 0);
const weightedExecutionSuccessPct = computed(() => {
  let weightedSuccesses = 0;
  let runCount = 0;
  for (const pair of filteredPairs.value) {
    for (const arm of [pair.code, pair.classic]) {
      if (!arm) continue;
      weightedSuccesses += arm.runs * arm.successPct;
      runCount += arm.runs;
    }
  }
  return runCount ? weightedSuccesses / runCount : 0;
});
const classicPenaltyTokenEquivalent = computed(() => {
  if (!calibrationPolicy.value) return null;
  return calibrationPolicy.value.turn_penalty_pts * calibrationPolicy.value.tokens_per_penalty_pt;
});

function formatMetric(value: number | undefined, digits = 1): string {
  return typeof value === "number" && Number.isFinite(value)
    ? value.toLocaleString("en-US", { maximumFractionDigits: digits })
    : "—";
}

watch(selectedWindow, () => { void loadData(); });

onMounted(() => {
  void loadData();
  void loadCalibrationPolicy();
  pollTimer = setInterval(loadData, 15000);
});

onUnmounted(() => {
  if (pollTimer) clearInterval(pollTimer);
});
</script>

<template>
  <div class="space-y-6">
    <div class="flex flex-wrap items-start justify-between gap-3">
      <div>
        <div class="flex items-center gap-2">
          <h1 class="text-[20px] font-semibold tracking-[-0.015em] text-[#09090B]">Model Performance</h1>
          <span class="inline-flex items-center gap-1 rounded-full border border-[#99F6E4] bg-[#F0FDFA] px-2 py-0.5 text-[11px] font-medium text-[#0F766E]">
            <span class="h-1.5 w-1.5 rounded-full bg-[#0F766E]" />
            Refreshes every 15s
          </span>
        </div>
        <p class="mt-1 max-w-3xl text-[13px] leading-5 text-[#52525B]">
          Compare the observed Code Mode and Classic MCP arms for each model, upstream server and tool. The gateway's recommendation is scoped to that model/tool pair—not a global mode setting.
        </p>
      </div>
    </div>

    <div class="grid grid-cols-2 gap-3 sm:grid-cols-3 xl:grid-cols-5">
      <div class="rounded-lg border border-[#E4E4E7] bg-white p-4">
        <div class="text-xs text-[#71717A]">Observed runs</div>
        <div class="mt-1 font-mono-data text-2xl font-semibold tabular-nums text-[#09090B]">{{ totalPairRuns.toLocaleString("en-US") }}</div>
      </div>
      <div class="rounded-lg border border-[#E4E4E7] bg-white p-4">
        <div class="text-xs text-[#71717A]">Models tracked</div>
        <div class="mt-1 font-mono-data text-2xl font-semibold tabular-nums text-[#09090B]">{{ modelsTracked }}</div>
      </div>
      <div class="rounded-lg border border-[#E4E4E7] bg-white p-4">
        <div class="text-xs text-[#71717A]">Upstream servers</div>
        <div class="mt-1 font-mono-data text-2xl font-semibold tabular-nums text-[#09090B]">{{ serversTracked }}</div>
      </div>
      <div class="rounded-lg border border-[#E4E4E7] bg-white p-4">
        <div class="text-xs text-[#71717A]">Code Mode recommended</div>
        <div class="mt-1 font-mono-data text-2xl font-semibold tabular-nums text-[#0F766E]">{{ codeModeRecommendationPct }}%</div>
        <div class="mt-1 text-[11px] text-[#71717A] tabular-nums">{{ codeModeRecommendedPairs }} Code · {{ classicRecommendedPairs }} Classic · {{ unresolvedPairs }} unresolved</div>
      </div>
      <div class="rounded-lg border border-[#E4E4E7] bg-white p-4">
        <div class="text-xs text-[#71717A]">Observed success · Code + Classic</div>
        <div class="mt-1 font-mono-data text-2xl font-semibold tabular-nums text-[#09090B]">{{ formatMetric(weightedExecutionSuccessPct, 1) }}%</div>
        <div class="mt-1 text-[11px] text-[#71717A]">Run-weighted · paired arms only</div>
      </div>
    </div>

    <section class="rounded-lg border border-[#E4E4E7] bg-white p-4">
      <div class="mb-3 flex flex-wrap items-start justify-between gap-2">
        <div>
          <h2 class="text-[15px] font-semibold text-[#09090B]">Paired model / tool comparison</h2>
          <p class="mt-1 text-xs leading-5 text-[#71717A]">Each row is one model + server + tool combination. Arm metrics are weighted by observed runs.</p>
        </div>
        <span class="text-xs tabular-nums text-[#71717A]">{{ filteredPairs.length }} pairs</span>
      </div>
      <div class="mb-4 grid grid-cols-1 gap-3 sm:grid-cols-[minmax(14rem,1fr)_minmax(12rem,0.5fr)]">
        <Input v-model="searchQuery" placeholder="Search model, upstream or tool…" aria-label="Search model, upstream or tool" />
        <select v-model="selectedRecommendation" aria-label="Filter by recommendation" class="min-h-10 rounded-md border border-[#D4D4D8] bg-white px-3 text-xs text-[#09090B]">
          <option value="all">All recommendations</option>
          <option value="codemode">Recommended: Code Mode</option>
          <option value="traditional">Recommended: Classic MCP</option>
          <option value="unresolved">No recommendation / varying</option>
        </select>
      </div>

      <div v-if="errorMessage" role="alert" class="mb-3 rounded-md border border-[#FECACA] bg-[#FEF2F2] px-3 py-2 text-xs text-[#B91C1C]">
        {{ errorMessage }} <button type="button" class="ml-2 underline" @click="loadData">Retry</button>
      </div>
      <div v-if="loading" class="py-8 text-center text-xs text-[#71717A]">Loading paired model performance…</div>
      <div v-else-if="filteredPairs.length === 0" class="py-8 text-center text-xs text-[#71717A]">No matching model/tool comparisons. Trigger server demo traffic or adjust the filters.</div>
      <div v-else class="table-scroll overflow-x-auto rounded-md border border-[#E4E4E7]">
        <table class="w-full min-w-[1120px] text-left text-xs tabular-nums">
          <thead class="bg-[#F4F4F5] text-[#52525B]">
            <tr>
              <th class="px-3 py-2.5 font-medium">Model / server / tool</th>
              <th class="px-3 py-2.5 font-medium">Code Mode MCP</th>
              <th class="px-3 py-2.5 font-medium">Classic MCP</th>
              <th class="px-3 py-2.5 font-medium">Gateway recommendation</th>
            </tr>
          </thead>
          <tbody class="divide-y divide-[#E4E4E7]">
            <tr v-for="pair in filteredPairs" :key="pair.key" class="align-top hover:bg-[#FAFAFA]">
              <td class="max-w-[20rem] px-3 py-3">
                <div class="break-all font-medium text-[#09090B]">{{ pair.model }}</div>
                <div class="mt-1 break-all font-mono-data text-[11px] text-[#71717A]">{{ pair.server }} / {{ pair.tool }}</div>
                <div v-if="pair.hybridRuns || pair.otherRuns" class="mt-1 text-[11px] text-[#71717A]">Other observations: {{ pair.hybridRuns + pair.otherRuns }} runs</div>
              </td>
              <td class="px-3 py-3">
                <template v-if="pair.code">
                  <div class="font-semibold text-[#0F766E]">{{ pair.code.runs.toLocaleString("en-US") }} runs</div>
                  <div class="mt-1">Success {{ formatMetric(pair.code.successPct, 1) }}%</div>
                  <div>Avg. latency {{ formatMetric(pair.code.avgLatencyMs, 2) }} ms</div>
                  <div>Avg. measured tokens {{ formatMetric(pair.code.avgTokens, 0) }}</div>
                </template>
                <span v-else class="text-[#A1A1AA]">No Code Mode observations</span>
              </td>
              <td class="px-3 py-3">
                <template v-if="pair.classic">
                  <div class="font-semibold text-[#1D4ED8]">{{ pair.classic.runs.toLocaleString("en-US") }} runs</div>
                  <div class="mt-1">Success {{ formatMetric(pair.classic.successPct, 1) }}%</div>
                  <div>Avg. latency {{ formatMetric(pair.classic.avgLatencyMs, 2) }} ms</div>
                  <div>Avg. measured tokens {{ formatMetric(pair.classic.avgTokens, 0) }}</div>
                </template>
                <span v-else class="text-[#A1A1AA]">No Classic MCP observations</span>
              </td>
              <td class="px-3 py-3">
                <span class="inline-flex rounded border px-2 py-1 text-[11px] font-medium"
                  :class="pair.recommendation === 'codemode' ? 'border-[#99F6E4] bg-[#F0FDFA] text-[#0F766E]' : pair.recommendation === 'traditional' ? 'border-[#BFDBFE] bg-[#EFF6FF] text-[#1D4ED8]' : 'border-[#E4E4E7] bg-[#F4F4F5] text-[#52525B]'">
                  {{ recommendationLabel(pair.recommendation) }}
                </span>
                <div class="mt-1.5 max-w-[14rem] text-[11px] leading-4 text-[#71717A]">This is the gateway's current calibration recommendation for this model/tool pair.</div>
              </td>
            </tr>
          </tbody>
        </table>
      </div>
    </section>

    <section class="rounded-lg border border-[#E4E4E7] bg-white p-4">
      <h2 class="text-sm font-semibold text-[#09090B]">Calibration accounting</h2>
      <p v-if="calibrationPolicy" class="mt-1 text-xs leading-5 text-[#52525B]">
        Configured target: {{ calibrationPolicy.calibration_runs_per_arm }} runs per arm. Classic MCP adds {{ classicPenaltyTokenEquivalent?.toLocaleString("en-US") }} modeled token-equivalents per turn after the first ({{ calibrationPolicy.turn_penalty_pts }} points × {{ calibrationPolicy.tokens_per_penalty_pt }} token-equivalents/point). Code Mode has no turn penalty in this policy.
      </p>
      <p v-else class="mt-1 text-xs leading-5 text-[#71717A]">Calibration policy could not be loaded. Open Settings → Routing &amp; calibration to verify the active values.</p>
      <p class="mt-2 text-xs leading-5 text-[#71717A]">
        The performance endpoint reports average measured tokens and does not return average turn counts. Therefore this table does not invent an adjusted token-equivalent average: measured tokens remain separate from the modeled Classic turn overhead. Token-equivalents are not provider-billed tokens or a cash-savings figure.
      </p>
    </section>

    <section class="rounded-lg border border-[#E4E4E7] bg-white p-4">
      <div class="flex flex-wrap items-center justify-between gap-3">
        <div>
          <h2 class="text-sm font-semibold text-[#09090B]">Raw mode observations</h2>
          <p class="mt-1 text-xs text-[#71717A]">Arm-level rows used to build the paired comparison.</p>
        </div>
        <button type="button" class="min-h-10 rounded-md border border-[#D4D4D8] px-3 text-xs font-medium text-[#52525B] hover:bg-[#F4F4F5]" :aria-expanded="showRawObservations" @click="showRawObservations = !showRawObservations">
          {{ showRawObservations ? "Hide raw rows" : "Show raw rows" }}
        </button>
      </div>
      <div v-if="showRawObservations" class="mt-4 space-y-3">
        <select v-model="selectedMode" aria-label="Filter raw observations by mode" class="min-h-10 w-full max-w-xs rounded-md border border-[#D4D4D8] bg-white px-3 text-xs text-[#09090B]">
          <option value="all">All modes</option>
          <option value="code">Code Mode MCP</option>
          <option value="classic">Classic MCP</option>
          <option value="hybrid">Hybrid</option>
          <option value="other">Other</option>
        </select>
        <div class="table-scroll overflow-x-auto rounded-md border border-[#E4E4E7]">
          <table class="w-full min-w-[900px] text-left text-xs tabular-nums">
            <thead class="bg-[#F4F4F5] text-[#52525B]"><tr>
              <th class="px-3 py-2 font-medium">Model</th><th class="px-3 py-2 font-medium">Server</th><th class="px-3 py-2 font-medium">Tool</th><th class="px-3 py-2 font-medium">Arm</th><th class="px-3 py-2 text-right font-medium">Runs</th><th class="px-3 py-2 text-right font-medium">Success</th><th class="px-3 py-2 text-right font-medium">Avg. latency</th><th class="px-3 py-2 text-right font-medium">Avg. measured tokens</th>
            </tr></thead>
            <tbody class="divide-y divide-[#E4E4E7]">
              <tr v-for="(row, index) in filteredObservations" :key="`${row.model}-${row.server}-${row.tool}-${row.mode}-${index}`">
                <td class="px-3 py-2 font-mono-data">{{ row.model }}</td><td class="px-3 py-2">{{ row.server || "gateway" }}</td><td class="px-3 py-2">{{ row.tool }}</td><td class="px-3 py-2">{{ modeLabel(row.mode) }}</td><td class="px-3 py-2 text-right">{{ row.runs }}</td><td class="px-3 py-2 text-right">{{ formatMetric(row.success_rate_pct, 1) }}%</td><td class="px-3 py-2 text-right">{{ formatMetric(row.avg_duration_ms, 2) }} ms</td><td class="px-3 py-2 text-right">{{ formatMetric(row.avg_tokens, 0) }}</td>
              </tr>
            </tbody>
          </table>
        </div>
      </div>
    </section>
  </div>
</template>
