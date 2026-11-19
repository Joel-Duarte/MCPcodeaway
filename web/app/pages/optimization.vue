<script setup lang="ts">
import { extractErrorMessage } from "~/composables/useAdminApi";

interface AnalyticsResponse {
  window_runs: number;
  pricing: {
    model: string;
    input_per_1m_usd: number;
    output_per_1m_usd: number;
    available_models: string[];
  };
  summary: {
    total_runs: number;
    classic_runs: number;
    codemode_runs: number;
    codemode_share_pct: number;
    total_tokens_saved: number;
    estimated_cost_usd: number;
    estimated_cost_saved_usd: number;
  };
  mode_decisions: {
    mode: string;
    reason: string;
    runs: number;
    avg_score: number | null;
    avg_tokens_saved: number;
  }[];
}

interface ExperimentsResponse {
  results: Record<
    string,
    {
      arms: Record<
        string,
        { runs: number; error_rate_pct: number; avg_duration_ms: number; avg_tokens_saved: number; total_tokens_saved: number }
      >;
      recommended_winner: string;
    }
  >;
}

const { apiFetch } = useAdminApi();
const { subscribeTelemetry } = useLiveTelemetry();

const loading = ref(true);
const errorMessage = ref("");
const selectedModel = ref("qwen3.5-9b");
const analytics = ref<AnalyticsResponse | null>(null);
const experiments = ref<ExperimentsResponse | null>(null);

async function fetchOptimization() {
  errorMessage.value = "";
  try {
    const q = new URLSearchParams({ model: selectedModel.value });
    const [a, e] = await Promise.all([
      apiFetch<AnalyticsResponse>(`/admin/analytics/groups?${q.toString()}`),
      apiFetch<ExperimentsResponse>("/admin/ab/experiments"),
    ]);
    analytics.value = a;
    experiments.value = e;
  } catch (err) {
    errorMessage.value = extractErrorMessage(err);
  } finally {
    loading.value = false;
  }
}

let unsub: (() => void) | null = null;
onMounted(() => {
  void fetchOptimization();
  unsub = subscribeTelemetry((ev) => {
    if (ev.type === "run_recorded") {
      void fetchOptimization();
    }
  });
});

onBeforeUnmount(() => {
  unsub?.();
});

const modelOptions = computed(() => {
  const models = analytics.value?.pricing?.available_models || ["qwen3.5-9b"];
  return models.map((m) => ({ value: m, label: m }));
});

const experimentRows = computed(() => {
  const results = experiments.value?.results || {};
  return Object.entries(results).map(([id, info]) => ({ id, ...info }));
});

function formatInt(n: number | undefined): string {
  return Number(n || 0).toLocaleString("en-US");
}
</script>

<template>
  <div class="space-y-6">
    <div class="flex flex-col gap-3 sm:flex-row sm:items-center sm:justify-between">
      <div>
        <h1 class="text-[20px] font-semibold tracking-[-0.015em] text-[#09090B]">
          Optimization, Cost Savings &amp; A/B Splits
        </h1>
        <p class="mt-0.5 text-[13px] text-[#52525B]">
          Evaluate Code Mode vs. Classic MCP token savings, algorithm routing decisions, and live A/B experiment winners.
        </p>
      </div>
      <div class="flex flex-wrap items-center gap-2">
        <select
          v-model="selectedModel"
          aria-label="Select model pricing"
          class="rounded-md border border-[#D4D4D8] bg-white px-3 py-1.5 text-xs font-medium text-[#09090B]"
          @change="fetchOptimization"
        >
          <option v-for="m in modelOptions" :key="m.value" :value="m.value">{{ m.label }}</option>
        </select>
        <NuxtLink
          to="/settings"
          class="no-underline inline-flex items-center gap-1.5 rounded-md border border-[#D4D4D8] bg-white px-3 py-1.5 text-xs font-medium text-[#09090B] hover:bg-[#F4F4F5]"
        >
          <span>Edit Model Prices</span>
        </NuxtLink>
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

    <div v-if="loading" class="h-48 animate-pulse rounded-lg border border-[#E4E4E7] bg-white" />

    <template v-else-if="analytics">
      <!-- Empirical Proof Banner -->
      <section class="rounded-lg border border-[#99F6E4] bg-[#F0FDFA] px-4 py-3 text-xs text-[#0F766E]">
        <span class="font-semibold">Empirical 9B Baseline:</span>
        Code Mode reduces prompt bloat via deferred schema search and Monty Python batching, delivering
        <strong class="tabular-nums">{{ formatInt(analytics.summary.total_tokens_saved) }}</strong> tokens saved
        (<strong class="tabular-nums">\${{ analytics.summary.estimated_cost_saved_usd.toFixed(4) }}</strong> on
        <code class="font-mono-data">{{ analytics.pricing.model }}</code>).
      </section>

      <!-- ROI KPI Strip -->
      <section class="grid grid-cols-1 gap-3 sm:grid-cols-2 lg:grid-cols-4">
        <div class="rounded-lg border border-[#E4E4E7] bg-white p-4">
          <div class="text-xs text-[#52525B]">Code Mode Share</div>
          <div class="mt-1 text-2xl font-semibold text-[#0F766E] tabular-nums">
            {{ analytics.summary.codemode_share_pct }}%
          </div>
          <div class="mt-1 text-xs text-[#71717A] tabular-nums">
            {{ analytics.summary.codemode_runs }} CM / {{ analytics.summary.classic_runs }} CL
          </div>
        </div>
        <div class="rounded-lg border border-[#E4E4E7] bg-white p-4">
          <div class="text-xs text-[#52525B]">Tokens Saved</div>
          <div class="mt-1 text-2xl font-semibold text-[#09090B] tabular-nums">
            {{ formatInt(analytics.summary.total_tokens_saved) }}
          </div>
          <div class="mt-1 text-xs text-[#71717A]">Across {{ analytics.summary.total_runs }} runs</div>
        </div>
        <div class="rounded-lg border border-[#E4E4E7] bg-white p-4">
          <div class="text-xs text-[#52525B]">Estimated Cost Saved</div>
          <div class="mt-1 text-2xl font-semibold text-[#0F766E] tabular-nums">
            \${{ analytics.summary.estimated_cost_saved_usd.toFixed(4) }}
          </div>
          <div class="mt-1 text-xs text-[#71717A]">
            \${{ analytics.pricing.input_per_1m_usd }} in / \${{ analytics.pricing.output_per_1m_usd }} out per 1M
          </div>
        </div>
        <div class="rounded-lg border border-[#E4E4E7] bg-white p-4">
          <div class="text-xs text-[#52525B]">Active A/B Experiments</div>
          <div class="mt-1 text-2xl font-semibold text-[#2563EB] tabular-nums">
            {{ experimentRows.length }}
          </div>
          <div class="mt-1 text-xs text-[#71717A]">Live traffic split evaluations</div>
        </div>
      </section>

      <!-- Live A/B Experiments -->
      <section class="rounded-lg border border-[#E4E4E7] bg-white">
        <div class="flex items-center justify-between border-b border-[#E4E4E7] px-4 py-3">
          <h2 class="text-[15px] font-semibold text-[#09090B]">Live A/B Experiment Winners</h2>
          <NuxtLink to="/onboarding" class="text-xs font-medium text-[#0F766E] hover:underline">
            Configure New Split
          </NuxtLink>
        </div>

        <div v-if="experimentRows.length === 0" class="p-6 text-center text-[13px] text-[#71717A]">
          No A/B experiments have accumulated runs yet. Start one in Onboarding Step 3.
        </div>

        <div v-else class="overflow-x-auto table-scroll">
          <table class="w-full border-collapse text-left text-[13px]">
            <thead>
              <tr class="border-b border-[#E4E4E7] bg-[#F4F4F5] text-xs font-medium text-[#52525B]">
                <th class="px-4 py-2.5">Experiment ID</th>
                <th class="px-4 py-2.5">Code Mode Arm</th>
                <th class="px-4 py-2.5">Classic Arm</th>
                <th class="px-4 py-2.5">Recommended Winner</th>
              </tr>
            </thead>
            <tbody class="divide-y divide-[#E4E4E7]">
              <tr v-for="exp in experimentRows" :key="exp.id" class="hover:bg-[#FAFAFA]">
                <td class="px-4 py-2.5 font-mono-data font-medium text-[#09090B]">{{ exp.id }}</td>
                <td class="px-4 py-2.5 text-xs tabular-nums">
                  <template v-if="exp.arms.codemode">
                    {{ exp.arms.codemode.runs }} runs · {{ exp.arms.codemode.avg_duration_ms.toFixed(1) }} ms · {{ exp.arms.codemode.total_tokens_saved }} tok saved
                  </template>
                  <span v-else class="text-[#71717A]">—</span>
                </td>
                <td class="px-4 py-2.5 text-xs tabular-nums">
                  <template v-if="exp.arms.classic">
                    {{ exp.arms.classic.runs }} runs · {{ exp.arms.classic.avg_duration_ms.toFixed(1) }} ms · err {{ exp.arms.classic.error_rate_pct }}%
                  </template>
                  <span v-else class="text-[#71717A]">—</span>
                </td>
                <td class="px-4 py-2.5">
                  <span
                    class="rounded border px-2 py-0.5 text-xs font-medium"
                    :class="
                      exp.recommended_winner === 'codemode'
                        ? 'border-[#99F6E4] bg-[#F0FDFA] text-[#0F766E]'
                        : 'border-[#BFDBFE] bg-[#EFF6FF] text-[#2563EB]'
                    "
                  >
                    {{ exp.recommended_winner }}
                  </span>
                </td>
              </tr>
            </tbody>
          </table>
        </div>
      </section>

      <!-- Mode Decision Attribution -->
      <section class="rounded-lg border border-[#E4E4E7] bg-white">
        <div class="border-b border-[#E4E4E7] px-4 py-3">
          <h2 class="text-[15px] font-semibold text-[#09090B]">Mode Routing &amp; Algorithm Decisions</h2>
        </div>
        <div class="overflow-x-auto table-scroll">
          <table class="w-full border-collapse text-left text-[13px]">
            <thead>
              <tr class="border-b border-[#E4E4E7] bg-[#F4F4F5] text-xs font-medium text-[#52525B]">
                <th class="px-4 py-2.5">Mode</th>
                <th class="px-4 py-2.5">Decision Reason</th>
                <th class="px-4 py-2.5 text-right">Runs</th>
                <th class="px-4 py-2.5 text-right">Avg Tokens Saved</th>
              </tr>
            </thead>
            <tbody class="divide-y divide-[#E4E4E7]">
              <tr v-for="(d, idx) in analytics.mode_decisions" :key="idx" class="hover:bg-[#FAFAFA]">
                <td class="px-4 py-2.5 font-mono-data text-xs">{{ d.mode }}</td>
                <td class="px-4 py-2.5 text-xs text-[#52525B]">{{ d.reason }}</td>
                <td class="px-4 py-2.5 text-right tabular-nums">{{ d.runs }}</td>
                <td class="px-4 py-2.5 text-right tabular-nums text-[#0F766E]">{{ d.avg_tokens_saved }}</td>
              </tr>
            </tbody>
          </table>
        </div>
      </section>
    </template>
  </div>
</template>
