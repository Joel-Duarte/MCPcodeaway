<script setup lang="ts">
import { extractErrorMessage } from "~/composables/useAdminApi";

interface AnalyticsResponse {
  window_runs: number;
  tenant_scope: string;
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
  by_company_group: {
    group: string;
    unique_users: number;
    runs: number;
    subcalls: number;
    classic_runs: number;
    codemode_runs: number;
    codemode_share_pct: number;
    tokens_saved: number;
    estimated_cost_usd: number;
    estimated_cost_saved_usd: number;
    abac_rows_filtered: number;
    pii_redactions: number;
  }[];
  by_client_group: {
    client_group: string;
    runs: number;
    tokens_saved: number;
    abac_rows_filtered: number;
  }[];
  by_user: {
    user_sub: string;
    user_email: string | null;
    groups: string[];
    runs: number;
    subcalls: number;
    classic_runs: number;
    codemode_runs: number;
    top_upstreams: { server: string; calls: number }[];
    tokens_saved: number;
    estimated_cost_usd: number;
    estimated_cost_saved_usd: number;
  }[];
  mode_decisions: {
    mode: string;
    reason: string;
    runs: number;
    avg_score: number | null;
    avg_tokens_saved: number;
    avg_duration_ms: number;
  }[];
}

const { apiFetch } = useAdminApi();
const { status: demoStatus, busy: demoBusy, runBurst } = useDemoTraffic();
const { totalRunsTick } = useLiveTelemetry();

async function triggerAnalyticsBurst() {
  await runBurst(8);
  await fetchAnalytics();
}

const loading = ref(true);
const errorMessage = ref("");
const activeTab = ref<string>("company-groups");
const selectedModel = ref("qwen3.5-9b");
const groupFilter = ref("");
const userFilter = ref("");
const upstreamFilter = ref("");
const analytics = ref<AnalyticsResponse | null>(null);
const customModelNames = ref<string[]>([]);
const customModelInput = ref("custom-local-9b");
const customInPrice = ref("0.20");
const customOutPrice = ref("0.60");
const pricingSaving = ref(false);
let liveDebounce: ReturnType<typeof setTimeout> | null = null;

async function fetchPricingModels() {
  try {
    const res = await apiFetch<{ custom_models?: string[] }>("/admin/pricing/models", {}, true);
    customModelNames.value = res.custom_models || [];
  } catch {
    customModelNames.value = [];
  }
}

async function saveCustomModel() {
  if (!customModelInput.value.trim()) return;
  pricingSaving.value = true;
  errorMessage.value = "";
  try {
    const name = customModelInput.value.trim();
    await apiFetch("/admin/pricing/models", {
      method: "POST",
      body: {
        model_name: name,
        input_per_1m_usd: Number(customInPrice.value) || 0,
        output_per_1m_usd: Number(customOutPrice.value) || 0,
      },
    });
    selectedModel.value = name;
    await Promise.all([fetchPricingModels(), fetchAnalytics()]);
  } catch (err) {
    errorMessage.value = extractErrorMessage(err);
  } finally {
    pricingSaving.value = false;
  }
}

async function deleteCustomModel(name: string) {
  try {
    await apiFetch(`/admin/pricing/models/${encodeURIComponent(name)}`, { method: "DELETE" });
    if (selectedModel.value === name) {
      selectedModel.value = "qwen3.5-9b";
    }
    await Promise.all([fetchPricingModels(), fetchAnalytics()]);
  } catch (err) {
    errorMessage.value = extractErrorMessage(err);
  }
}

async function fetchAnalytics() {
  errorMessage.value = "";
  const params = new URLSearchParams();
  params.set("model", selectedModel.value || "qwen3.5-9b");
  if (groupFilter.value.trim()) params.set("group", groupFilter.value.trim());
  if (userFilter.value.trim()) params.set("user", userFilter.value.trim());
  if (upstreamFilter.value.trim()) params.set("upstream", upstreamFilter.value.trim());

  try {
    analytics.value = await apiFetch<AnalyticsResponse>(`/audit/analytics?${params.toString()}`);
  } catch (err) {
    errorMessage.value = extractErrorMessage(err);
  } finally {
    loading.value = false;
  }
}

watch(totalRunsTick, () => {
  if (liveDebounce) clearTimeout(liveDebounce);
  liveDebounce = setTimeout(() => {
    void fetchAnalytics();
  }, 700);
});

onMounted(() => {
  void Promise.all([fetchPricingModels(), fetchAnalytics()]);
});

onBeforeUnmount(() => {
  if (liveDebounce) {
    clearTimeout(liveDebounce);
    liveDebounce = null;
  }
});

const modelOptions = computed(() => {
  const models = analytics.value?.pricing?.available_models || ["default"];
  return models.map((m) => ({ value: m, label: m }));
});

function formatInt(n: number | undefined): string {
  return Number(n || 0).toLocaleString("en-US");
}

function formatUsd(n: number | undefined): string {
  return `$${Number(n || 0).toFixed(4)}`;
}
</script>

<template>
  <div class="space-y-6">
    <div class="flex flex-col gap-3 sm:flex-row sm:items-center sm:justify-between">
      <div>
        <h1 class="text-[20px] font-semibold tracking-[-0.015em] text-[#09090B]">
          Analytics, Groups &amp; Model Cost
        </h1>
        <p class="mt-0.5 text-[13px] text-[#52525B]">
          Attribute gateway token savings, LLM cost estimates, client groups, and mode selection reasons.
        </p>
      </div>
      <Button data-variant="primary" pending-announcement="Refreshing analytics" @click="fetchAnalytics">
        <AppIcon name="refresh" :size="15" />
        <span>Apply Filters</span>
      </Button>
    </div>

    <!-- Filters Bar -->
    <section class="rounded-lg border border-[#E4E4E7] bg-white p-4">
      <div class="grid grid-cols-1 gap-3 sm:grid-cols-2 lg:grid-cols-4">
        <Field>
          <FieldLabel>Model Pricing</FieldLabel>
          <Select
            :items="modelOptions"
            :value="selectedModel"
            @value-change="(v) => { if (v) { selectedModel = String(v); void fetchAnalytics(); } }"
          >
            <SelectTrigger>
              <SelectValue />
              <AppIcon name="chevron-down" :size="14" />
            </SelectTrigger>
            <SelectContent>
              <SelectItem v-for="item in modelOptions" :key="item.value" :value="item.value">
                {{ item.label }}
                <SelectItemIndicator>
                  <AppIcon name="check" :size="14" />
                </SelectItemIndicator>
              </SelectItem>
            </SelectContent>
          </Select>
        </Field>

        <Field>
          <FieldLabel>Company Group</FieldLabel>
          <Input v-model="groupFilter" placeholder="e.g. finance" />
        </Field>

        <Field>
          <FieldLabel>User Subject / Email</FieldLabel>
          <Input v-model="userFilter" placeholder="e.g. carol@acme.test" />
        </Field>

        <Field>
          <FieldLabel>Upstream Server</FieldLabel>
          <Input v-model="upstreamFilter" placeholder="e.g. ledger_api" />
        </Field>
      </div>
    </section>

    <div
      v-if="errorMessage"
      role="alert"
      class="flex items-start gap-2 rounded-lg border border-[#FECACA] bg-[#FEF2F2] px-4 py-3 text-[13px] text-[#DC2626]"
    >
      <AppIcon name="alert" :size="16" class="mt-0.5" />
      <span>{{ errorMessage }}</span>
    </div>

    <div v-if="loading" class="grid grid-cols-1 gap-4 sm:grid-cols-2 lg:grid-cols-4">
      <div v-for="i in 4" :key="i" class="h-24 animate-pulse rounded-lg border border-[#E4E4E7] bg-white" />
    </div>

    <template v-else-if="analytics">
      <!-- Empty-state quick Demo Traffic CTA -->
      <section
        v-if="demoStatus.enabled && analytics.summary.total_runs === 0"
        class="flex flex-col gap-3 rounded-lg border border-[#99F6E4] bg-[#F0FDFA] p-4 sm:flex-row sm:items-center sm:justify-between"
      >
        <div class="text-[13px] text-[#09090B]">
          <span class="font-semibold text-[#0F766E]">Analytics database is empty.</span>
          <span class="ml-1 text-[#52525B]">
            Run a live Demo Burst to populate group spend, user attribution, and Code Mode savings charts.
          </span>
        </div>
        <Button data-variant="primary" :disabled="demoBusy" @click="triggerAnalyticsBurst">
          Run Demo Burst (+8)
        </Button>
      </section>

      <!-- Custom Model API Pricing Panel -->
      <section aria-label="Custom model token pricing" class="rounded-lg border border-[#E4E4E7] bg-white p-4">
        <div class="flex flex-wrap items-center justify-between gap-2">
          <div>
            <h2 class="text-[15px] font-semibold text-[#09090B]">
              API Token Cost Simulator (Baseline: <span class="font-mono-data text-[#0F766E]">{{ analytics.pricing.model }}</span>)
            </h2>
            <p class="text-xs text-[#71717A]">
              Calculates per-token API spend ($/1M input &amp; output tokens); client subscription plans are not exposed by MCP.
            </p>
          </div>
          <div class="flex flex-wrap items-center gap-1.5">
            <span
              v-for="cm in customModelNames"
              :key="cm"
              class="inline-flex items-center gap-1 rounded border border-[#99F6E4] bg-[#F0FDFA] px-2 py-0.5 text-xs text-[#0F766E]"
            >
              <span class="font-mono-data">{{ cm }}</span>
              <button
                type="button"
                class="ml-0.5 text-[#0F766E] hover:text-[#DC2626]"
                :aria-label="`Remove custom model ${cm}`"
                @click="deleteCustomModel(cm)"
              >
                ×
              </button>
            </span>
          </div>
        </div>
        <div class="mt-3 grid grid-cols-1 gap-3 sm:grid-cols-4 sm:items-end">
          <Field>
            <FieldLabel>Custom Model ID</FieldLabel>
            <Input v-model="customModelInput" class="font-mono-data text-xs" placeholder="e.g. llama-3.3-70b" />
          </Field>
          <Field>
            <FieldLabel>Input $/1M Tokens</FieldLabel>
            <Input v-model="customInPrice" inputmode="decimal" class="tabular-nums text-xs" />
          </Field>
          <Field>
            <FieldLabel>Output $/1M Tokens</FieldLabel>
            <Input v-model="customOutPrice" inputmode="decimal" class="tabular-nums text-xs" />
          </Field>
          <Button :disabled="pricingSaving" @click="saveCustomModel">
            Save Custom Model Price
          </Button>
        </div>
      </section>

      <!-- Summary KPI Strip -->
      <section class="grid grid-cols-1 gap-4 sm:grid-cols-2 lg:grid-cols-4">
        <div class="rounded-lg border border-[#E4E4E7] bg-white p-4">
          <div class="text-xs font-medium text-[#52525B]">Analyzed Runs</div>
          <div class="mt-1.5 text-2xl font-semibold text-[#09090B] tabular-nums">
            {{ formatInt(analytics.summary.total_runs) }}
          </div>
          <div class="mt-1 text-xs text-[#71717A] tabular-nums">
            {{ formatInt(analytics.summary.codemode_runs) }} Code Mode · {{ formatInt(analytics.summary.classic_runs) }} Classic
          </div>
        </div>

        <div class="rounded-lg border border-[#E4E4E7] bg-white p-4">
          <div class="text-xs font-medium text-[#52525B]">Code Mode Share</div>
          <div class="mt-1.5 text-2xl font-semibold text-[#0F766E] tabular-nums">
            {{ analytics.summary.codemode_share_pct.toFixed(1) }}%
          </div>
          <div class="mt-1 text-xs text-[#71717A] tabular-nums">
            {{ formatInt(analytics.summary.total_tokens_saved) }} tokens saved
          </div>
        </div>

        <div class="rounded-lg border border-[#E4E4E7] bg-white p-4">
          <div class="text-xs font-medium text-[#52525B]">
            Estimated LLM Spend ({{ analytics.pricing.model }})
          </div>
          <div class="mt-1.5 text-2xl font-semibold text-[#09090B] tabular-nums">
            {{ formatUsd(analytics.summary.estimated_cost_usd) }}
          </div>
          <div class="mt-1 text-xs text-[#71717A] tabular-nums">
            In: ${{ analytics.pricing.input_per_1m_usd }}/1M · Out: ${{ analytics.pricing.output_per_1m_usd }}/1M
          </div>
        </div>

        <div class="rounded-lg border border-[#E4E4E7] bg-white p-4">
          <div class="text-xs font-medium text-[#52525B]">Estimated Cost Saved</div>
          <div class="mt-1.5 text-2xl font-semibold text-[#059669] tabular-nums">
            {{ formatUsd(analytics.summary.estimated_cost_saved_usd) }}
          </div>
          <div class="mt-1 text-xs text-[#71717A]">
            Scope: {{ analytics.tenant_scope }}
          </div>
        </div>
      </section>

      <!-- Group Savings Comparison Chart (SVG bars) -->
      <section
        v-if="analytics.by_company_group.length > 0"
        aria-label="Company group mode and token comparison"
        class="rounded-lg border border-[#E4E4E7] bg-white p-4"
      >
        <div class="flex flex-wrap items-center justify-between gap-2">
          <h2 class="text-[15px] font-semibold text-[#09090B]">
            Code Mode vs. Classic Volume by Company Group
          </h2>
          <div class="flex items-center gap-4 text-xs text-[#52525B]">
            <span class="inline-flex items-center gap-1.5">
              <span class="inline-block h-2.5 w-2.5 rounded-sm bg-[#0F766E]" />
              Code Mode Runs
            </span>
            <span class="inline-flex items-center gap-1.5">
              <span class="inline-block h-2.5 w-2.5 rounded-sm bg-[#2563EB]" />
              Classic Runs
            </span>
          </div>
        </div>
        <div class="mt-3 space-y-2.5">
          <div
            v-for="g in analytics.by_company_group"
            :key="g.group"
            class="grid grid-cols-1 gap-1.5 sm:grid-cols-12 sm:items-center"
          >
            <div class="sm:col-span-3 text-xs font-medium text-[#09090B] truncate">
              {{ g.group }}
            </div>
            <div class="sm:col-span-6">
              <svg viewBox="0 0 100 8" class="h-3 w-full overflow-hidden rounded bg-[#F4F4F5]" role="img" :aria-label="`${g.group} run split`">
                <rect
                  x="0"
                  y="0"
                  :width="g.runs > 0 ? (g.codemode_runs / g.runs) * 100 : 0"
                  height="8"
                  fill="#0F766E"
                />
                <rect
                  :x="g.runs > 0 ? (g.codemode_runs / g.runs) * 100 : 0"
                  y="0"
                  :width="g.runs > 0 ? (g.classic_runs / g.runs) * 100 : 0"
                  height="8"
                  fill="#2563EB"
                />
              </svg>
            </div>
            <div class="sm:col-span-3 text-right text-xs text-[#52525B] tabular-nums">
              {{ formatInt(g.tokens_saved) }} tok · {{ formatUsd(g.estimated_cost_saved_usd) }}
            </div>
          </div>
        </div>
      </section>

      <!-- Segmented Tables via @ui-sdk/vue Tabs -->
      <section class="rounded-lg border border-[#E4E4E7] bg-white p-4">
        <Tabs v-model:value="activeTab">
          <TabsList aria-label="Analytics breakdowns">
            <TabsTab value="company-groups">Company Groups</TabsTab>
            <TabsTab value="users">Users</TabsTab>
            <TabsTab value="client-groups">Client Groups</TabsTab>
            <TabsTab value="mode-decisions">Mode Decision Attribution</TabsTab>
          </TabsList>

          <TabsPanel value="company-groups">
            <div v-if="analytics.by_company_group.length === 0" class="py-8 text-center text-[13px] text-[#71717A]">
              No company group runs match the current filters.
            </div>
            <div v-else class="overflow-x-auto table-scroll">
              <table class="w-full border-collapse text-left text-[13px]">
                <thead>
                  <tr class="border-b border-[#E4E4E7] bg-[#F4F4F5] text-xs font-medium text-[#52525B]">
                    <th class="px-3 py-2.5 whitespace-nowrap">Company Group</th>
                    <th class="px-3 py-2.5 whitespace-nowrap text-right">Users</th>
                    <th class="px-3 py-2.5 whitespace-nowrap text-right">Runs</th>
                    <th class="px-3 py-2.5 whitespace-nowrap min-w-[140px]">Mode Mix (Teal / Blue)</th>
                    <th class="px-3 py-2.5 whitespace-nowrap text-right">Tokens Saved</th>
                    <th class="px-3 py-2.5 whitespace-nowrap text-right">Est. Cost</th>
                    <th class="px-3 py-2.5 whitespace-nowrap text-right">Est. Saved</th>
                    <th class="px-3 py-2.5 whitespace-nowrap text-right">ABAC / PII</th>
                  </tr>
                </thead>
                <tbody class="divide-y divide-[#E4E4E7]">
                  <tr v-for="g in analytics.by_company_group" :key="g.group" class="hover:bg-[#FAFAFA]">
                    <td class="px-3 py-2.5 font-medium text-[#09090B] whitespace-nowrap">{{ g.group }}</td>
                    <td class="px-3 py-2.5 text-right tabular-nums whitespace-nowrap">{{ formatInt(g.unique_users) }}</td>
                    <td class="px-3 py-2.5 text-right tabular-nums whitespace-nowrap">{{ formatInt(g.runs) }}</td>
                    <td class="px-3 py-2.5 whitespace-nowrap">
                      <div class="flex items-center gap-2">
                        <div class="flex h-2 w-24 overflow-hidden rounded bg-[#F4F4F5]">
                          <div class="h-full bg-[#0F766E]" :style="{ width: `${g.codemode_share_pct}%` }" />
                          <div class="h-full bg-[#2563EB]" :style="{ width: `${Math.max(0, 100 - g.codemode_share_pct)}%` }" />
                        </div>
                        <span class="text-xs tabular-nums text-[#52525B]">{{ g.codemode_share_pct.toFixed(0) }}%</span>
                      </div>
                    </td>
                    <td class="px-3 py-2.5 text-right tabular-nums whitespace-nowrap">{{ formatInt(g.tokens_saved) }}</td>
                    <td class="px-3 py-2.5 text-right tabular-nums whitespace-nowrap">{{ formatUsd(g.estimated_cost_usd) }}</td>
                    <td class="px-3 py-2.5 text-right tabular-nums text-[#0F766E] whitespace-nowrap">{{ formatUsd(g.estimated_cost_saved_usd) }}</td>
                    <td class="px-3 py-2.5 text-right tabular-nums text-xs text-[#52525B] whitespace-nowrap">
                      {{ g.abac_rows_filtered }} / {{ g.pii_redactions }}
                    </td>
                  </tr>
                </tbody>
              </table>
            </div>
          </TabsPanel>

          <TabsPanel value="users">
            <div v-if="analytics.by_user.length === 0" class="py-8 text-center text-[13px] text-[#71717A]">
              No user activity matches the current filters.
            </div>
            <div v-else class="overflow-x-auto table-scroll">
              <table class="w-full border-collapse text-left text-[13px]">
                <thead>
                  <tr class="border-b border-[#E4E4E7] bg-[#F4F4F5] text-xs font-medium text-[#52525B]">
                    <th class="px-3 py-2.5 whitespace-nowrap">User</th>
                    <th class="px-3 py-2.5 whitespace-nowrap">Groups</th>
                    <th class="px-3 py-2.5 whitespace-nowrap text-right">Runs (CM / CL)</th>
                    <th class="px-3 py-2.5 whitespace-nowrap">Top Upstreams</th>
                    <th class="px-3 py-2.5 whitespace-nowrap text-right">Tokens Saved</th>
                    <th class="px-3 py-2.5 whitespace-nowrap text-right">Est. Cost</th>
                    <th class="px-3 py-2.5 whitespace-nowrap text-right">Est. Saved</th>
                  </tr>
                </thead>
                <tbody class="divide-y divide-[#E4E4E7]">
                  <tr v-for="u in analytics.by_user" :key="u.user_sub" class="hover:bg-[#FAFAFA]">
                    <td class="px-3 py-2.5 whitespace-nowrap">
                      <div class="font-medium text-[#09090B]">{{ u.user_email || u.user_sub }}</div>
                      <div class="font-mono-data text-[11px] text-[#71717A]">{{ u.user_sub }}</div>
                    </td>
                    <td class="px-3 py-2.5 text-xs text-[#52525B] whitespace-nowrap">{{ u.groups.join(", ") }}</td>
                    <td class="px-3 py-2.5 text-right tabular-nums whitespace-nowrap">
                      {{ u.runs }} (<span class="text-[#0F766E]">{{ u.codemode_runs }}</span> / <span class="text-[#2563EB]">{{ u.classic_runs }}</span>)
                    </td>
                    <td class="px-3 py-2.5 text-xs font-mono-data text-[#52525B] whitespace-nowrap">
                      {{ u.top_upstreams.map((x) => `${x.server}:${x.calls}`).join(", ") || "—" }}
                    </td>
                    <td class="px-3 py-2.5 text-right tabular-nums whitespace-nowrap">{{ formatInt(u.tokens_saved) }}</td>
                    <td class="px-3 py-2.5 text-right tabular-nums whitespace-nowrap">{{ formatUsd(u.estimated_cost_usd) }}</td>
                    <td class="px-3 py-2.5 text-right tabular-nums text-[#0F766E] whitespace-nowrap">{{ formatUsd(u.estimated_cost_saved_usd) }}</td>
                  </tr>
                </tbody>
              </table>
            </div>
          </TabsPanel>

          <TabsPanel value="client-groups">
            <div v-if="analytics.by_client_group.length === 0" class="py-8 text-center text-[13px] text-[#71717A]">
              No OBO client group telemetry recorded yet.
            </div>
            <div v-else class="overflow-x-auto table-scroll">
              <table class="w-full border-collapse text-left text-[13px]">
                <thead>
                  <tr class="border-b border-[#E4E4E7] bg-[#F4F4F5] text-xs font-medium text-[#52525B]">
                    <th class="px-3 py-2.5 whitespace-nowrap">Client Group</th>
                    <th class="px-3 py-2.5 whitespace-nowrap text-right">Runs</th>
                    <th class="px-3 py-2.5 whitespace-nowrap text-right">Tokens Saved</th>
                    <th class="px-3 py-2.5 whitespace-nowrap text-right">ABAC Rows Filtered</th>
                  </tr>
                </thead>
                <tbody class="divide-y divide-[#E4E4E7]">
                  <tr v-for="cg in analytics.by_client_group" :key="cg.client_group" class="hover:bg-[#FAFAFA]">
                    <td class="px-3 py-2.5 font-mono-data font-medium text-[#09090B] whitespace-nowrap">{{ cg.client_group }}</td>
                    <td class="px-3 py-2.5 text-right tabular-nums whitespace-nowrap">{{ formatInt(cg.runs) }}</td>
                    <td class="px-3 py-2.5 text-right tabular-nums whitespace-nowrap">{{ formatInt(cg.tokens_saved) }}</td>
                    <td class="px-3 py-2.5 text-right tabular-nums whitespace-nowrap">{{ formatInt(cg.abac_rows_filtered) }}</td>
                  </tr>
                </tbody>
              </table>
            </div>
          </TabsPanel>

          <TabsPanel value="mode-decisions">
            <div v-if="analytics.mode_decisions.length === 0" class="py-8 text-center text-[13px] text-[#71717A]">
              No mode decisions recorded yet.
            </div>
            <div v-else class="overflow-x-auto table-scroll">
              <table class="w-full border-collapse text-left text-[13px]">
                <thead>
                  <tr class="border-b border-[#E4E4E7] bg-[#F4F4F5] text-xs font-medium text-[#52525B]">
                    <th class="px-3 py-2.5 whitespace-nowrap">Selected Mode</th>
                    <th class="px-3 py-2.5 whitespace-nowrap">Decision Attribution Reason</th>
                    <th class="px-3 py-2.5 whitespace-nowrap text-right">Runs</th>
                    <th class="px-3 py-2.5 whitespace-nowrap text-right">Avg Auto Score</th>
                    <th class="px-3 py-2.5 whitespace-nowrap text-right">Avg Tokens Saved</th>
                    <th class="px-3 py-2.5 whitespace-nowrap text-right">Avg Latency</th>
                  </tr>
                </thead>
                <tbody class="divide-y divide-[#E4E4E7]">
                  <tr v-for="(d, idx) in analytics.mode_decisions" :key="idx" class="hover:bg-[#FAFAFA]">
                    <td class="px-3 py-2.5 font-mono-data font-medium whitespace-nowrap" :class="d.mode.startsWith('codemode') ? 'text-[#0F766E]' : 'text-[#2563EB]'">
                      {{ d.mode }}
                    </td>
                    <td class="px-3 py-2.5 text-[#09090B] min-w-[240px]">{{ d.reason }}</td>
                    <td class="px-3 py-2.5 text-right tabular-nums whitespace-nowrap">{{ formatInt(d.runs) }}</td>
                    <td class="px-3 py-2.5 text-right tabular-nums whitespace-nowrap">{{ d.avg_score !== null ? d.avg_score.toFixed(1) : "—" }}</td>
                    <td class="px-3 py-2.5 text-right tabular-nums whitespace-nowrap">{{ d.avg_tokens_saved.toFixed(0) }}</td>
                    <td class="px-3 py-2.5 text-right tabular-nums whitespace-nowrap">{{ d.avg_duration_ms.toFixed(1) }} ms</td>
                  </tr>
                </tbody>
              </table>
            </div>
          </TabsPanel>
        </Tabs>
      </section>
    </template>
  </div>
</template>
