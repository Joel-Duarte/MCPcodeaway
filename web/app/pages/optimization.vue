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

const { apiFetch } = useAdminApi();
const { totalRunsTick } = useLiveTelemetry();

const loading = ref(true);
const errorMessage = ref("");
const statusMessage = ref("");
const selectedModel = ref("qwen3.5-9b");
const analytics = ref<AnalyticsResponse | null>(null);
const upstreamsList = ref<{ name: string; preferred_mode: string; enabled: boolean }[]>([]);
const optimizingName = ref<string | null>(null);

async function fetchOptimization() {
  errorMessage.value = "";
  try {
    const q = new URLSearchParams({ model: selectedModel.value });
    const [a, u] = await Promise.all([
      apiFetch<AnalyticsResponse>(`/audit/analytics?${q.toString()}`),
      apiFetch<{ upstreams: { name: string; preferred_mode: string; enabled: boolean }[] }>(
        "/admin/upstreams?page_size=200"
      ),
    ]);
    analytics.value = a;
    upstreamsList.value = u.upstreams || [];
  } catch (err) {
    errorMessage.value = extractErrorMessage(err);
  } finally {
    loading.value = false;
  }
}

async function optimizeUpstream(name: string, targetMode: "codemode" | "auto") {
  optimizingName.value = name;
  errorMessage.value = "";
  statusMessage.value = "";
  try {
    await apiFetch(
      `/admin/upstreams/${encodeURIComponent(name)}/optimize-mode?target_mode=${targetMode}`,
      { method: "POST" }
    );
    statusMessage.value = `Switched '${name}' to '${targetMode}' mode.`;
    await fetchOptimization();
  } catch (err) {
    errorMessage.value = extractErrorMessage(err);
  } finally {
    optimizingName.value = null;
  }
}

onMounted(() => {
  void fetchOptimization();
});

watch(totalRunsTick, () => {
  void fetchOptimization();
});

const modelOptions = computed(() => {
  const models = analytics.value?.pricing?.available_models || ["qwen3.5-9b"];
  return models.map((m) => ({ value: m, label: m }));
});

const unoptimizedUpstreams = computed(() =>
  upstreamsList.value.filter(
    (u) => u.preferred_mode !== "codemode" && u.preferred_mode !== "auto"
  )
);

function formatInt(n: number | undefined): string {
  return Number(n || 0).toLocaleString("en-US");
}
</script>

<template>
  <div class="space-y-6">
    <div class="flex flex-col gap-3 sm:flex-row sm:items-center sm:justify-between">
      <div>
        <h1 class="text-[20px] font-semibold tracking-[-0.015em] text-[#09090B]">
          Optimization &amp; Cost Savings
        </h1>
        <p class="mt-0.5 text-[13px] text-[#52525B]">
          Track Code Mode vs. Classic MCP token savings, routing attribution, and one-click upstream mode upgrades.
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
      v-if="statusMessage"
      role="status"
      class="rounded-lg border border-[#99F6E4] bg-[#F0FDFA] px-4 py-2.5 text-[13px] text-[#0F766E]"
    >
      {{ statusMessage }}
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
      <!-- ROI KPI Strip -->
      <section class="grid grid-cols-1 gap-3 sm:grid-cols-2 lg:grid-cols-4">
        <div class="rounded-lg border border-[#E4E4E7] bg-white p-4">
          <div class="text-xs text-[#52525B]">Estimated Cost Saved</div>
          <div class="mt-1 text-2xl font-semibold text-[#0F766E] tabular-nums">
            \${{ analytics.summary.estimated_cost_saved_usd.toFixed(4) }}
          </div>
          <div class="mt-1 text-xs text-[#71717A] font-mono-data">
            {{ analytics.pricing.model }} (\${{ analytics.pricing.input_per_1m_usd }}/\${{ analytics.pricing.output_per_1m_usd }} per 1M)
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
          <div class="text-xs text-[#52525B]">Code Mode Share</div>
          <div class="mt-1 text-2xl font-semibold text-[#0F766E] tabular-nums">
            {{ analytics.summary.codemode_share_pct }}%
          </div>
          <div class="mt-1 text-xs text-[#71717A] tabular-nums">
            {{ analytics.summary.codemode_runs }} CM / {{ analytics.summary.classic_runs }} CL
          </div>
        </div>
        <div class="rounded-lg border border-[#E4E4E7] bg-white p-4">
          <div class="text-xs text-[#52525B]">Pending Recommendations</div>
          <div class="mt-1 text-2xl font-semibold text-[#2563EB] tabular-nums">
            {{ unoptimizedUpstreams.length }}
          </div>
          <div class="mt-1 text-xs text-[#71717A]">Upstreams eligible for upgrade</div>
        </div>
      </section>

      <!-- 1-Click Upstream Mode Optimizer (auto-hides once optimized) -->
      <section class="rounded-lg border border-[#E4E4E7] bg-white">
        <div class="flex items-center justify-between border-b border-[#E4E4E7] px-4 py-3">
          <div>
            <h2 class="text-[15px] font-semibold text-[#09090B]">Upstreams Needing Optimization</h2>
            <p class="mt-0.5 text-xs text-[#52525B]">
              Upstreams currently pinned to <code class="font-mono-data">traditional</code> or <code class="font-mono-data">hybrid</code>. Once switched to <code class="font-mono-data">codemode</code> or <code class="font-mono-data">auto</code>, they leave this list.
            </p>
          </div>
          <NuxtLink to="/onboarding" class="text-xs font-medium text-[#0F766E] hover:underline">
            Run A/B Experiment →
          </NuxtLink>
        </div>

        <div v-if="unoptimizedUpstreams.length === 0" class="p-6 text-center text-[13px] text-[#0F766E]">
          All registered upstreams are already optimized (<code class="font-mono-data">codemode</code> or <code class="font-mono-data">auto</code>).
        </div>

        <div v-else class="overflow-x-auto table-scroll">
          <table class="w-full border-collapse text-left text-[13px]">
            <thead>
              <tr class="border-b border-[#E4E4E7] bg-[#F4F4F5] text-xs font-medium text-[#52525B]">
                <th class="px-4 py-2.5">Upstream</th>
                <th class="px-4 py-2.5">Current Mode</th>
                <th class="px-4 py-2.5 text-right">Optimize</th>
              </tr>
            </thead>
            <tbody class="divide-y divide-[#E4E4E7]">
              <tr v-for="u in unoptimizedUpstreams" :key="u.name" class="hover:bg-[#FAFAFA]">
                <td class="px-4 py-2.5 font-mono-data text-xs font-medium text-[#09090B]">{{ u.name }}</td>
                <td class="px-4 py-2.5 font-mono-data text-xs text-[#2563EB]">{{ u.preferred_mode }}</td>
                <td class="px-4 py-2.5 text-right space-x-2">
                  <Button
                    data-variant="primary"
                    :disabled="optimizingName === u.name"
                    @click="optimizeUpstream(u.name, 'codemode')"
                  >
                    {{ optimizingName === u.name ? "Applying..." : "Switch to Code Mode" }}
                  </Button>
                  <Button
                    :disabled="optimizingName === u.name"
                    @click="optimizeUpstream(u.name, 'auto')"
                  >
                    Set Auto
                  </Button>
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
