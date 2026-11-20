<script setup lang="ts">
import { computed, onMounted, onUnmounted, ref } from "vue";

interface ModelPerfRow {
  model: string;
  server: string;
  tool: string;
  mode: string;
  runs: number;
  success_rate_pct: number;
  avg_duration_ms: number;
  avg_tokens: number;
  recommended_mode: string;
}

const { apiFetch } = useAdminApi();
const rows = ref<ModelPerfRow[]>([]);
const loading = ref(true);

const searchQuery = ref("");
const selectedServer = ref("all");
const selectedModel = ref("all");
const selectedMode = ref("all");
const selectedWinner = ref("all");
const selectedWindow = ref("24h");

let pollTimer: ReturnType<typeof setInterval> | null = null;

async function loadData() {
  try {
    const res = await apiFetch<{ rows: ModelPerfRow[] }>(
      `/admin/ab/model-performance?window=${encodeURIComponent(selectedWindow.value)}`
    );
    if (res) {
      rows.value = res.rows || [];
    }
  } finally {
    loading.value = false;
  }
}

const servers = computed(() =>
  Array.from(new Set(rows.value.map((r) => r.server || "gateway"))).sort()
);
const models = computed(() =>
  Array.from(new Set(rows.value.map((r) => r.model))).sort()
);

const filteredRows = computed(() => {
  const q = searchQuery.value.trim().toLowerCase();
  return rows.value.filter((r) => {
    if (selectedServer.value !== "all" && (r.server || "gateway") !== selectedServer.value) {
      return false;
    }
    if (selectedModel.value !== "all" && r.model !== selectedModel.value) {
      return false;
    }
    if (selectedMode.value !== "all" && r.mode !== selectedMode.value) {
      return false;
    }
    if (selectedWinner.value !== "all" && r.recommended_mode !== selectedWinner.value) {
      return false;
    }
    if (q) {
      const hay = `${r.model} ${r.server} ${r.tool} ${r.mode} ${r.recommended_mode}`.toLowerCase();
      if (!hay.includes(q)) return false;
    }
    return true;
  });
});

const totalRuns = computed(() => filteredRows.value.reduce((acc, r) => acc + r.runs, 0));
const codemodeWinPct = computed(() => {
  if (!filteredRows.value.length) return 0;
  const wins = filteredRows.value.filter((r) => r.recommended_mode === "codemode").length;
  return Math.round((wins / filteredRows.value.length) * 100);
});

onMounted(() => {
  loadData();
  pollTimer = setInterval(loadData, 3000);
});

onUnmounted(() => {
  if (pollTimer) clearInterval(pollTimer);
});
</script>

<template>
  <div class="space-y-6">
    <div class="flex flex-wrap items-center justify-between gap-3">
      <div>
        <div class="flex items-center gap-2">
          <h1 class="text-xl font-semibold tracking-tight text-[#09090B]">Model Performance</h1>
          <span
            class="inline-flex items-center gap-1 rounded-full border border-[#99F6E4] bg-[#F0FDFA] px-2 py-0.5 text-[11px] font-medium text-[#0F766E]"
          >
            <span class="h-1.5 w-1.5 rounded-full bg-[#0F766E]" />
            3s
          </span>
        </div>
        <p class="text-sm text-[#71717A]">
          Real-time Classic MCP vs. Code Mode comparison per model, MCP server, and tool.
        </p>
      </div>
      <div class="flex items-center gap-1.5">
        <button
          v-for="w in ['1h', '24h', '7d', '30d']"
          :key="w"
          type="button"
          class="rounded border px-2.5 py-1 text-xs font-mono-data transition"
          :class="
            selectedWindow === w
              ? 'border-[#0F766E] bg-[#F0FDFA] font-semibold text-[#0F766E]'
              : 'border-[#E4E4E7] bg-white text-[#52525B] hover:bg-[#FAFAFA]'
          "
          @click="selectedWindow = w; void loadData()"
        >
          {{ w }}
        </button>
      </div>
    </div>

    <!-- Summary KPIs -->
    <div class="grid grid-cols-2 gap-4 sm:grid-cols-4">
      <div class="rounded-lg border border-[#E4E4E7] bg-white p-4">
        <div class="text-xs text-[#71717A]">Filtered Runs</div>
        <div class="mt-1 font-mono-data text-2xl font-semibold tabular-nums text-[#09090B]">
          {{ totalRuns }}
        </div>
      </div>
      <div class="rounded-lg border border-[#E4E4E7] bg-white p-4">
        <div class="text-xs text-[#71717A]">Models Tracked</div>
        <div class="mt-1 font-mono-data text-2xl font-semibold tabular-nums text-[#09090B]">
          {{ models.length }}
        </div>
      </div>
      <div class="rounded-lg border border-[#E4E4E7] bg-white p-4">
        <div class="text-xs text-[#71717A]">MCP Servers</div>
        <div class="mt-1 font-mono-data text-2xl font-semibold tabular-nums text-[#09090B]">
          {{ servers.length }}
        </div>
      </div>
      <div class="rounded-lg border border-[#E4E4E7] bg-white p-4">
        <div class="text-xs text-[#71717A]">Code Mode Win Share</div>
        <div class="mt-1 font-mono-data text-2xl font-semibold tabular-nums text-[#0F766E]">
          {{ codemodeWinPct }}%
        </div>
      </div>
    </div>

    <!-- Filters -->
    <div class="rounded-lg border border-[#E4E4E7] bg-white p-4">
      <div class="grid grid-cols-1 gap-3 sm:grid-cols-2 lg:grid-cols-5">
        <Input
          v-model="searchQuery"
          placeholder="Search model, server, or tool..."
          class="text-xs"
        />
        <select
          v-model="selectedServer"
          aria-label="Filter by MCP server"
          class="rounded border border-[#D4D4D8] bg-white px-2.5 py-1.5 text-xs text-[#09090B]"
        >
          <option value="all">All MCP Servers</option>
          <option v-for="s in servers" :key="s" :value="s">{{ s }}</option>
        </select>
        <select
          v-model="selectedModel"
          aria-label="Filter by model"
          class="rounded border border-[#D4D4D8] bg-white px-2.5 py-1.5 text-xs text-[#09090B]"
        >
          <option value="all">All Models</option>
          <option v-for="m in models" :key="m" :value="m">{{ m }}</option>
        </select>
        <select
          v-model="selectedMode"
          aria-label="Filter by mode"
          class="rounded border border-[#D4D4D8] bg-white px-2.5 py-1.5 text-xs text-[#09090B]"
        >
          <option value="all">All Modes</option>
          <option value="codemode">Code Mode</option>
          <option value="traditional">Classic MCP</option>
        </select>
        <select
          v-model="selectedWinner"
          aria-label="Filter by winner"
          class="rounded border border-[#D4D4D8] bg-white px-2.5 py-1.5 text-xs text-[#09090B]"
        >
          <option value="all">All Winners</option>
          <option value="codemode">Winner: Code Mode</option>
          <option value="traditional">Winner: Classic MCP</option>
        </select>
      </div>
    </div>

    <!-- Table -->
    <div class="rounded-lg border border-[#E4E4E7] bg-white p-4">
      <div v-if="loading" class="py-8 text-center text-xs text-[#71717A]">
        Loading model performance metrics...
      </div>
      <div v-else-if="filteredRows.length === 0" class="py-8 text-center text-xs text-[#71717A]">
        No matching model performance runs. Adjust filters or trigger traffic from Demo Control.
      </div>
      <div v-else class="overflow-x-auto">
        <table class="w-full text-left text-xs tabular-nums">
          <thead class="border-b border-[#E4E4E7] text-[#71717A]">
            <tr>
              <th class="py-2 pr-3 font-medium">Model</th>
              <th class="py-2 pr-3 font-medium">MCP Server</th>
              <th class="py-2 pr-3 font-medium">Tool</th>
              <th class="py-2 pr-3 font-medium">Mode</th>
              <th class="py-2 pr-3 font-medium text-right">Runs</th>
              <th class="py-2 pr-3 font-medium text-right">Success</th>
              <th class="py-2 pr-3 font-medium text-right">Avg Latency</th>
              <th class="py-2 pr-3 font-medium text-right">Avg Tokens</th>
              <th class="py-2 font-medium text-right">Winner</th>
            </tr>
          </thead>
          <tbody class="divide-y divide-[#E4E4E7]">
            <tr
              v-for="(r, idx) in filteredRows"
              :key="`${r.model}-${r.server}-${r.tool}-${r.mode}-${idx}`"
              class="hover:bg-[#FAFAFA]"
            >
              <td class="py-2.5 pr-3 font-mono-data font-medium text-[#09090B]">{{ r.model }}</td>
              <td class="py-2.5 pr-3 font-mono-data text-[#52525B]">{{ r.server || "gateway" }}</td>
              <td class="py-2.5 pr-3 font-mono-data text-[#52525B]">{{ r.tool }}</td>
              <td class="py-2.5 pr-3">
                <span
                  class="rounded px-1.5 py-0.5 font-mono-data text-[11px]"
                  :class="
                    r.mode === 'codemode'
                      ? 'bg-[#F0FDFA] text-[#0F766E]'
                      : 'bg-[#F4F4F5] text-[#52525B]'
                  "
                >
                  {{ r.mode }}
                </span>
              </td>
              <td class="py-2.5 pr-3 text-right font-mono-data">{{ r.runs }}</td>
              <td class="py-2.5 pr-3 text-right font-mono-data">{{ r.success_rate_pct }}%</td>
              <td class="py-2.5 pr-3 text-right font-mono-data">{{ r.avg_duration_ms }} ms</td>
              <td class="py-2.5 pr-3 text-right font-mono-data">{{ r.avg_tokens }}</td>
              <td class="py-2.5 text-right">
                <span
                  class="rounded px-1.5 py-0.5 font-mono-data text-[11px] font-semibold"
                  :class="
                    r.recommended_mode === 'codemode'
                      ? 'bg-[#CCFBF1] text-[#0F766E]'
                      : 'bg-[#FEF3C7] text-[#92400E]'
                  "
                >
                  {{ r.recommended_mode }}
                </span>
              </td>
            </tr>
          </tbody>
        </table>
      </div>
    </div>
  </div>
</template>
