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
  timeline_by_group?: {
    group: string;
    runs: number;
    avg_total_ms: number;
    avg_gateway_ms: number;
    avg_monty_spawn_ms: number;
    avg_monty_exec_ms: number;
    avg_upstream_ms: number;
    avg_security_ms: number;
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
const modelSearch = ref("");
const companyGroupPage = ref(1);
const userPage = ref(1);
const clientPage = ref(1);
const breakdownPageSize = 10;
const groupFilter = ref("");
const userFilter = ref("");
const upstreamFilter = ref("");
const analytics = ref<AnalyticsResponse | null>(null);
let liveDebounce: ReturnType<typeof setTimeout> | null = null;
let filterDebounce: ReturnType<typeof setTimeout> | null = null;
let analyticsRequestSequence = 0;


function clearAllFilters() {
  groupFilter.value = "";
  userFilter.value = "";
  upstreamFilter.value = "";
}

const activeFilterChips = computed(() => {
  const chips: { key: "group" | "user" | "upstream"; label: string }[] = [];
  if (groupFilter.value.trim()) chips.push({ key: "group", label: `Group: ${groupFilter.value.trim()}` });
  if (userFilter.value.trim()) chips.push({ key: "user", label: `User: ${userFilter.value.trim()}` });
  if (upstreamFilter.value.trim()) chips.push({ key: "upstream", label: `Upstream: ${upstreamFilter.value.trim()}` });
  return chips;
});

function clearChip(key: "group" | "user" | "upstream") {
  if (key === "group") groupFilter.value = "";
  if (key === "user") userFilter.value = "";
  if (key === "upstream") upstreamFilter.value = "";
}

const { selectedRange: selectedWindow } = useReportRange();

async function fetchAnalytics() {
  const requestId = ++analyticsRequestSequence;
  errorMessage.value = "";
  const params = new URLSearchParams();
  params.set("model", selectedModel.value || "qwen3.5-9b");
  params.set("limit", "5000");
  params.set("window", selectedWindow.value);
  if (groupFilter.value.trim()) params.set("group", groupFilter.value.trim());
  if (userFilter.value.trim()) params.set("user", userFilter.value.trim());
  if (upstreamFilter.value.trim()) params.set("upstream", upstreamFilter.value.trim());

  try {
    const response = await apiFetch<AnalyticsResponse>(`/audit/analytics?${params.toString()}`);
    if (requestId !== analyticsRequestSequence) return;
    const availableModels = response?.pricing?.available_models || [];
    const [firstModel] = availableModels;
    if (firstModel !== undefined && !availableModels.includes(selectedModel.value)) {
      const fallbackModel = response.pricing.model && availableModels.includes(response.pricing.model)
        ? response.pricing.model
        : firstModel;
      selectedModel.value = fallbackModel;
      params.set("model", fallbackModel);
      const fallbackResponse = await apiFetch<AnalyticsResponse>(`/audit/analytics?${params.toString()}`);
      if (requestId !== analyticsRequestSequence) return;
      analytics.value = fallbackResponse;
    } else {
      analytics.value = response;
    }
    companyGroupPage.value = 1;
    userPage.value = 1;
    clientPage.value = 1;
  } catch (err) {
    if (requestId === analyticsRequestSequence) errorMessage.value = extractErrorMessage(err);
  } finally {
    if (requestId === analyticsRequestSequence) loading.value = false;
  }
}

watch(selectedWindow, () => {
  void fetchAnalytics();
});

watch([modelSearch, groupFilter, userFilter, upstreamFilter], () => {
  if (filterDebounce) clearTimeout(filterDebounce);
  filterDebounce = setTimeout(() => {
    const matched = resolveFuzzyModel(modelSearch.value, availableModelsList.value);
    if (matched && matched !== selectedModel.value) {
      selectedModel.value = matched;
    }
    void fetchAnalytics();
  }, 300);
});

watch(totalRunsTick, () => {
  if (liveDebounce) clearTimeout(liveDebounce);
  liveDebounce = setTimeout(() => {
    void fetchAnalytics();
  }, 700);
});

onMounted(() => {
  void fetchAnalytics();
});

onBeforeUnmount(() => {
  if (liveDebounce) {
    clearTimeout(liveDebounce);
    liveDebounce = null;
  }
  if (filterDebounce) {
    clearTimeout(filterDebounce);
    filterDebounce = null;
  }
});

const availableModelsList = computed(() => analytics.value?.pricing?.available_models || ["default"]);

const pairedModeRunCount = computed(() =>
  (analytics.value?.summary.codemode_runs || 0) + (analytics.value?.summary.classic_runs || 0),
);
const otherModeRunCount = computed(() => Math.max(
  0,
  (analytics.value?.summary.total_runs || 0) - pairedModeRunCount.value,
));
const codemodeSharePct = computed(() => pairedModeRunCount.value
  ? (analytics.value!.summary.codemode_runs / pairedModeRunCount.value) * 100
  : 0);
const companyGroupsSorted = computed(() =>
  [...(analytics.value?.by_company_group || [])]
    .map((group) => {
      const pairedRuns = group.codemode_runs + group.classic_runs;
      return { ...group, codemode_share_pct: pairedRuns ? (group.codemode_runs / pairedRuns) * 100 : 0 };
    })
    .sort((a, b) => b.runs - a.runs),
);
const companyGroupsChart = computed(() => {
  const groups = companyGroupsSorted.value;
  const top = groups.slice(0, 5);
  const rest = groups.slice(5);
  if (!rest.length) return top;
  const otherRuns = rest.reduce((sum, g) => sum + g.runs, 0);
  const otherCode = rest.reduce((sum, g) => sum + g.codemode_runs, 0);
  const otherClassic = rest.reduce((sum, g) => sum + g.classic_runs, 0);
  const otherTokens = rest.reduce((sum, g) => sum + g.tokens_saved, 0);
  const otherCost = rest.reduce((sum, g) => sum + g.estimated_cost_saved_usd, 0);
  return [...top, {
    group: `Other ${rest.length} groups`, unique_users: 0, runs: otherRuns, subcalls: 0,
    classic_runs: otherClassic, codemode_runs: otherCode,
    codemode_share_pct: (otherCode + otherClassic) ? (otherCode / (otherCode + otherClassic)) * 100 : 0,
    tokens_saved: otherTokens, estimated_cost_usd: 0, estimated_cost_saved_usd: otherCost,
    abac_rows_filtered: 0, pii_redactions: 0,
  }];
});
const pagedCompanyGroups = computed(() => {
  const rows = companyGroupsSorted.value;
  const start = (companyGroupPage.value - 1) * breakdownPageSize;
  return rows.slice(start, start + breakdownPageSize);
});
const pagedUsers = computed(() => {
  const rows = analytics.value?.by_user || [];
  const start = (userPage.value - 1) * breakdownPageSize;
  return rows.slice(start, start + breakdownPageSize);
});
const pagedClientGroups = computed(() => {
  const rows = analytics.value?.by_client_group || [];
  const start = (clientPage.value - 1) * breakdownPageSize;
  return rows.slice(start, start + breakdownPageSize);
});
const companyGroupPages = computed(() => Math.max(1, Math.ceil(companyGroupsSorted.value.length / breakdownPageSize)));
const userPages = computed(() => Math.max(1, Math.ceil((analytics.value?.by_user.length || 0) / breakdownPageSize)));
const clientPages = computed(() => Math.max(1, Math.ceil((analytics.value?.by_client_group.length || 0) / breakdownPageSize)));

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
        <div class="flex items-center gap-2">
          <h1 class="text-[20px] font-semibold tracking-[-0.015em] text-[#09090B]">
            Gateway Usage, Groups &amp; Cost Attribution
          </h1>
          <span
            class="inline-flex items-center gap-1 rounded-full border border-[#99F6E4] bg-[#F0FDFA] px-2 py-0.5 text-[11px] font-medium text-[#0F766E]"
          >
            <span class="h-1.5 w-1.5 rounded-full bg-[#0F766E]" />
            Live
          </span>
        </div>
        <p class="mt-0.5 text-[13px] text-[#52525B]">
          Track gateway traffic, tenant and client-group usage, user activity, and ABAC/PII governance. Cost figures are gateway estimates based on the configured model price catalogue, not invoice-reconciled spend.
        </p>
      </div>
    </div>

    <section class="rounded-lg border border-[#E4E4E7] bg-white p-4 space-y-3">
      <div class="grid grid-cols-1 gap-3 sm:grid-cols-2 lg:grid-cols-4">
        <Field>
          <FieldLabel>Model</FieldLabel>
          <Input
            v-model="modelSearch"
            list="analytics-detected-models"
            placeholder="Search detected models..."
            aria-label="Search detected models"
          />
          <datalist id="analytics-detected-models">
            <option v-for="m in availableModelsList" :key="m" :value="m" />
          </datalist>
          <p v-if="selectedModel" class="text-[11px] font-mono-data text-[#71717A]">
            Active: {{ selectedModel }}
          </p>
        </Field>

        <Field>
          <FieldLabel>Tenant / Company Group</FieldLabel>
          <Input v-model="groupFilter" placeholder="Search a group name..." />
        </Field>

        <Field>
          <FieldLabel>User Subject / Email (fuzzy)</FieldLabel>
          <Input v-model="userFilter" placeholder="e.g. alice -> alice.fin@..." />
        </Field>

        <Field>
          <FieldLabel>Upstream</FieldLabel>
          <Input v-model="upstreamFilter" placeholder="Search upstream name..." />
        </Field>
      </div>

      <div v-if="activeFilterChips.length > 0" class="flex flex-wrap items-center gap-2 border-t border-[#E4E4E7] pt-2.5 text-xs">
        <span class="text-[#71717A]">Active filters:</span>
        <button
          v-for="chip in activeFilterChips"
          :key="chip.key"
          type="button"
          class="inline-flex items-center gap-1 rounded border border-[#99F6E4] bg-[#F0FDFA] px-2 py-0.5 font-medium text-[#0F766E] hover:bg-[#CCFBF1]"
          @click="clearChip(chip.key)"
        >
          <span>{{ chip.label }}</span>
          <span>×</span>
        </button>
        <button
          type="button"
          class="text-[#52525B] underline hover:text-[#09090B]"
          @click="clearAllFilters"
        >
          Clear all
        </button>
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
          <div class="text-xs font-medium text-[#52525B]">Code Mode share · Code vs Classic</div>
          <div class="mt-1.5 text-2xl font-semibold text-[#0F766E] tabular-nums">
            {{ codemodeSharePct.toFixed(1) }}%
          </div>
          <div class="mt-1 text-xs text-[#71717A] tabular-nums">
            {{ formatInt(analytics.summary.codemode_runs) }} of {{ formatInt(pairedModeRunCount) }} Code / Classic runs
          </div>
          <div v-if="otherModeRunCount > 0" class="mt-1 text-[11px] text-[#71717A]">{{ formatInt(otherModeRunCount) }} other / Hybrid runs excluded</div>
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
          <div class="text-xs font-medium text-[#52525B]">Gateway-estimated LLM cost reduction</div>
          <div class="mt-1.5 text-2xl font-semibold text-[#059669] tabular-nums">
            {{ formatUsd(analytics.summary.estimated_cost_saved_usd) }}
          </div>
          <div class="mt-1 text-xs text-[#71717A]">
            Scope: {{ analytics.tenant_scope }}
          </div>
        </div>
      </section>

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
                    <th class="px-3 py-2.5 whitespace-nowrap min-w-[140px]">Code / Classic share</th>
                    <th class="px-3 py-2.5 whitespace-nowrap text-right">Gateway-reported token reduction</th>
                    <th class="px-3 py-2.5 whitespace-nowrap text-right">Est. Cost</th>
                    <th class="px-3 py-2.5 whitespace-nowrap text-right">Gateway-est. cost reduction</th>
                    <th class="px-3 py-2.5 whitespace-nowrap text-right">ABAC / PII</th>
                  </tr>
                </thead>
                <tbody class="divide-y divide-[#E4E4E7]">
                  <tr v-for="g in pagedCompanyGroups" :key="g.group" class="hover:bg-[#FAFAFA]">
                    <td class="px-3 py-2.5 font-medium text-[#09090B] whitespace-nowrap">{{ g.group }}</td>
                    <td class="px-3 py-2.5 text-right tabular-nums whitespace-nowrap">{{ formatInt(g.unique_users) }}</td>
                    <td class="px-3 py-2.5 text-right tabular-nums whitespace-nowrap">{{ formatInt(g.runs) }}</td>
                    <td class="px-3 py-2.5 whitespace-nowrap">
                      <div class="flex items-center gap-2">
                        <div class="flex h-2 w-24 overflow-hidden rounded bg-[#F4F4F5]" :aria-label="`${g.codemode_runs} Code Mode and ${g.classic_runs} Classic runs out of ${g.runs} total`">
                          <div class="h-full bg-[#0F766E]" :style="{ width: `${g.runs > 0 ? (g.codemode_runs / g.runs) * 100 : 0}%` }" />
                          <div class="h-full bg-[#2563EB]" :style="{ width: `${g.runs > 0 ? (g.classic_runs / g.runs) * 100 : 0}%` }" />
                        </div>
                        <span class="text-xs tabular-nums text-[#52525B]">{{ g.codemode_share_pct.toFixed(0) }}% Code</span>
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
            <div v-if="companyGroupPages > 1" class="mt-3 flex items-center justify-between border-t border-[#E4E4E7] pt-3 text-xs text-[#52525B]">
              <span>Showing {{ (companyGroupPage - 1) * breakdownPageSize + 1 }}–{{ Math.min(companyGroupPage * breakdownPageSize, analytics.by_company_group.length) }} of {{ analytics.by_company_group.length }} groups</span>
              <div class="flex gap-2">
                <Button :disabled="companyGroupPage <= 1" @click="companyGroupPage -= 1">Previous</Button>
                <Button :disabled="companyGroupPage >= companyGroupPages" @click="companyGroupPage += 1">Next</Button>
              </div>
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
                    <th class="px-3 py-2.5 whitespace-nowrap text-right">Gateway-reported token reduction</th>
                    <th class="px-3 py-2.5 whitespace-nowrap text-right">Est. Cost</th>
                    <th class="px-3 py-2.5 whitespace-nowrap text-right">Gateway-est. cost reduction</th>
                  </tr>
                </thead>
                <tbody class="divide-y divide-[#E4E4E7]">
                  <tr v-for="u in pagedUsers" :key="u.user_sub" class="hover:bg-[#FAFAFA]">
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
            <div v-if="userPages > 1" class="mt-3 flex items-center justify-between border-t border-[#E4E4E7] pt-3 text-xs text-[#52525B]">
              <span>Showing {{ (userPage - 1) * breakdownPageSize + 1 }}–{{ Math.min(userPage * breakdownPageSize, analytics.by_user.length) }} of {{ analytics.by_user.length }} users</span>
              <div class="flex gap-2">
                <Button :disabled="userPage <= 1" @click="userPage -= 1">Previous</Button>
                <Button :disabled="userPage >= userPages" @click="userPage += 1">Next</Button>
              </div>
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
                    <th class="px-3 py-2.5 whitespace-nowrap text-right">Gateway-reported token reduction</th>
                    <th class="px-3 py-2.5 whitespace-nowrap text-right">ABAC Rows Filtered</th>
                  </tr>
                </thead>
                <tbody class="divide-y divide-[#E4E4E7]">
                  <tr v-for="cg in pagedClientGroups" :key="cg.client_group" class="hover:bg-[#FAFAFA]">
                    <td class="px-3 py-2.5 font-mono-data font-medium text-[#09090B] whitespace-nowrap">{{ cg.client_group }}</td>
                    <td class="px-3 py-2.5 text-right tabular-nums whitespace-nowrap">{{ formatInt(cg.runs) }}</td>
                    <td class="px-3 py-2.5 text-right tabular-nums whitespace-nowrap">{{ formatInt(cg.tokens_saved) }}</td>
                    <td class="px-3 py-2.5 text-right tabular-nums whitespace-nowrap">{{ formatInt(cg.abac_rows_filtered) }}</td>
                  </tr>
                </tbody>
              </table>
            </div>
            <div v-if="clientPages > 1" class="mt-3 flex items-center justify-between border-t border-[#E4E4E7] pt-3 text-xs text-[#52525B]">
              <span>Showing {{ (clientPage - 1) * breakdownPageSize + 1 }}–{{ Math.min(clientPage * breakdownPageSize, analytics.by_client_group.length) }} of {{ analytics.by_client_group.length }} client groups</span>
              <div class="flex gap-2">
                <Button :disabled="clientPage <= 1" @click="clientPage -= 1">Previous</Button>
                <Button :disabled="clientPage >= clientPages" @click="clientPage += 1">Next</Button>
              </div>
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
                    <th class="px-3 py-2.5 whitespace-nowrap text-right">Avg Gateway-reported token reduction</th>
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

      <section v-if="analytics.timeline_by_group?.length" class="rounded-lg border border-[#E4E4E7] bg-white">
        <div class="border-b border-[#E4E4E7] px-4 py-3">
          <h2 class="text-[15px] font-semibold text-[#09090B]">Aggregated Trace Timeline by Group</h2>
          <p class="mt-0.5 text-xs text-[#52525B]">
            Average stage latency per company group: Gateway Auth/Route, Monty Spawn, Monty Exec, Upstream Subcalls, and Security/Grounding.
          </p>
        </div>
        <div class="overflow-x-auto table-scroll">
          <table class="w-full border-collapse text-left text-[13px]">
            <thead>
              <tr class="border-b border-[#E4E4E7] bg-[#F4F4F5] text-xs font-medium text-[#52525B]">
                <th class="px-4 py-2.5">Group</th>
                <th class="px-4 py-2.5 text-right">Runs</th>
                <th class="px-4 py-2.5 text-right">Gateway</th>
                <th class="px-4 py-2.5 text-right">Monty Spawn</th>
                <th class="px-4 py-2.5 text-right">Monty Exec</th>
                <th class="px-4 py-2.5 text-right">Upstream</th>
                <th class="px-4 py-2.5 text-right">Security</th>
                <th class="px-4 py-2.5 text-right">Avg Total</th>
              </tr>
            </thead>
            <tbody class="divide-y divide-[#E4E4E7]">
              <tr v-for="tb in analytics.timeline_by_group" :key="tb.group" class="hover:bg-[#FAFAFA]">
                <td class="px-4 py-2.5 font-medium text-[#09090B]">{{ tb.group }}</td>
                <td class="px-4 py-2.5 text-right tabular-nums">{{ tb.runs }}</td>
                <td class="px-4 py-2.5 text-right font-mono-data text-xs tabular-nums">{{ tb.avg_gateway_ms.toFixed(2) }} ms</td>
                <td class="px-4 py-2.5 text-right font-mono-data text-xs text-[#0F766E] tabular-nums">{{ tb.avg_monty_spawn_ms.toFixed(2) }} ms</td>
                <td class="px-4 py-2.5 text-right font-mono-data text-xs text-[#0F766E] tabular-nums">{{ tb.avg_monty_exec_ms.toFixed(2) }} ms</td>
                <td class="px-4 py-2.5 text-right font-mono-data text-xs text-[#2563EB] tabular-nums">{{ tb.avg_upstream_ms.toFixed(2) }} ms</td>
                <td class="px-4 py-2.5 text-right font-mono-data text-xs tabular-nums">{{ tb.avg_security_ms.toFixed(2) }} ms</td>
                <td class="px-4 py-2.5 text-right font-semibold tabular-nums">{{ tb.avg_total_ms.toFixed(2) }} ms</td>
              </tr>
            </tbody>
          </table>
        </div>
      </section>
    </template>
  </div>
</template>
