<script setup lang="ts">
import { onMounted, onUnmounted, ref, watch } from "vue";
import { extractErrorMessage } from "~/composables/useAdminApi";

interface RecoveryOperation {
  operation_id: string;
  requester_sub: string;
  client_id: string | null;
  upstream_name: string;
  tool_name: string;
  status: string;
  attempt_count: number;
  max_attempts: number;
  created_at: string;
  updated_at: string;
  recovery_deadline: string;
  last_error_type: string | null;
  last_error: string | null;
}

interface OperationMetrics {
  by_status: Record<string, number>;
  recent_by_status: Record<string, number>;
  attempts: number;
  open: number;
  needs_attention: number;
  completed_24h: number;
  failed_24h: number;
  cancelled_24h: number;
  failure_rate_24h_pct: number | null;
  oldest_open_at: string | null;
  by_upstream_24h: { upstream_name: string; status: string; count: number; attempts: number }[];
  upstream_health: { server: string; status: string; checked_at: string; error_type: string | null; consecutive_failures: number }[];
}

const { apiFetch, session } = useAdminApi();
const operations = ref<RecoveryOperation[]>([]);
const metrics = ref<OperationMetrics | null>(null);
const statusFilter = ref("all");
const loading = ref(true);
const errorMessage = ref("");
const statusOptions = [
  { value: "all", label: "All states" },
  { value: "pending", label: "Pending" },
  { value: "working", label: "Working" },
  { value: "completed", label: "Completed" },
  { value: "failed", label: "Failed" },
  { value: "needs_attention", label: "Needs attention" },
  { value: "cancelled", label: "Cancelled" },
];

function formatTime(value: string): string {
  const date = new Date(value);
  return Number.isNaN(date.getTime()) ? value : date.toLocaleString();
}

async function loadOperations(silent = false) {
  if (!silent) loading.value = true;
  errorMessage.value = "";
  try {
    const [response, summary] = await Promise.all([
      apiFetch<{ operations: RecoveryOperation[] }>(
        `/admin/operations?status=${encodeURIComponent(statusFilter.value)}`,
      ),
      apiFetch<OperationMetrics>("/admin/operations/metrics"),
    ]);
    operations.value = response.operations;
    metrics.value = summary;
  } catch (error) {
    errorMessage.value = extractErrorMessage(error);
  } finally {
    loading.value = false;
  }
}

watch(statusFilter, () => { void loadOperations(); });
let refreshTimer: ReturnType<typeof setInterval> | undefined;
onMounted(() => {
  void loadOperations();
  refreshTimer = setInterval(() => {
    if (document.visibilityState === "visible") void loadOperations(true);
  }, 4000);
});
onUnmounted(() => {
  if (refreshTimer) clearInterval(refreshTimer);
});
</script>

<template>
  <div class="space-y-6">
    <header class="flex flex-col gap-3 sm:flex-row sm:items-end sm:justify-between">
      <div>
        <h1 class="text-[20px] font-semibold tracking-[-0.015em] text-[#09090B]">Operations recovery</h1>
        <p class="mt-1 max-w-3xl text-[13px] leading-5 text-[#52525B]">
          Inspect idempotent writes the gateway is confirming with an upstream. The list refreshes every four seconds while this page is open. Demo runs create visible records here; the gateway keeps the final caller result on its MCP task or operation status.
        </p>
      </div>
      <div class="flex items-center gap-2">
        <label for="operation-status" class="text-xs font-medium text-[#52525B]">Show</label>
        <Select :items="statusOptions" :value="statusFilter" @value-change="(value) => { if (value) statusFilter = String(value); }">
          <SelectTrigger id="operation-status" class="min-h-11 min-w-44 rounded-md border border-[#D4D4D8] bg-white px-3 text-sm text-[#09090B] focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-[#0F766E]">
            <SelectValue />
            <AppIcon name="chevron-down" :size="14" />
          </SelectTrigger>
          <SelectContent>
            <SelectItem v-for="option in statusOptions" :key="option.value" :value="option.value">
              {{ option.label }}
              <SelectItemIndicator><AppIcon name="check" :size="14" /></SelectItemIndicator>
            </SelectItem>
          </SelectContent>
        </Select>
        <Button class="min-h-11" :disabled="loading" @click="loadOperations">Refresh</Button>
      </div>
    </header>

    <div v-if="metrics && (metrics.needs_attention > 0 || metrics.failed_24h > 0)" role="alert" class="flex flex-col gap-2 rounded-lg border border-[#FDE68A] bg-[#FFFBEB] px-4 py-3 text-xs text-[#92400E] sm:flex-row sm:items-center sm:justify-between">
      <span>{{ metrics.needs_attention }} operation(s) need attention and {{ metrics.failed_24h }} failed in the last 24 hours.</span>
      <div class="flex flex-wrap gap-2"><Button class="min-h-11 shrink-0" @click="statusFilter = metrics.needs_attention ? 'needs_attention' : 'failed'">Inspect affected operations</Button><NuxtLink to="/upstreams" class="inline-flex min-h-11 items-center px-2 font-medium text-[#0F766E] underline underline-offset-2">Upstream health</NuxtLink></div>
    </div>

    <section v-if="metrics" aria-label="Recovery metrics" class="overflow-hidden rounded-lg border border-[#E4E4E7] bg-white">
      <div class="grid grid-cols-2 divide-x divide-y divide-[#E4E4E7] sm:grid-cols-3 sm:divide-y-0 lg:grid-cols-6">
        <div class="p-3"><div class="text-xs text-[#52525B]">Open now</div><div class="mt-1 tabular-nums text-lg font-semibold text-[#09090B]">{{ metrics.open }}</div></div>
        <div class="p-3"><div class="text-xs text-[#52525B]">Needs attention</div><div class="mt-1 tabular-nums text-lg font-semibold" :class="metrics.needs_attention ? 'text-[#B91C1C]' : 'text-[#09090B]'">{{ metrics.needs_attention }}</div></div>
        <div class="p-3"><div class="text-xs text-[#52525B]">Completed · 24h</div><div class="mt-1 tabular-nums text-lg font-semibold text-[#09090B]">{{ metrics.completed_24h }}</div></div>
        <div class="p-3"><div class="text-xs text-[#52525B]">Failed · 24h</div><div class="mt-1 tabular-nums text-lg font-semibold" :class="metrics.failed_24h ? 'text-[#B91C1C]' : 'text-[#09090B]'">{{ metrics.failed_24h }}</div></div>
        <div class="p-3"><div class="text-xs text-[#52525B]">Failure rate · 24h</div><div class="mt-1 tabular-nums text-lg font-semibold text-[#09090B]">{{ metrics.failure_rate_24h_pct == null ? "—" : `${metrics.failure_rate_24h_pct}%` }}</div></div>
        <div class="p-3"><div class="text-xs text-[#52525B]">Attempts · retained</div><div class="mt-1 tabular-nums text-lg font-semibold text-[#09090B]">{{ metrics.attempts }}</div></div>
      </div>
      <div class="flex flex-col gap-1 border-t border-[#E4E4E7] px-3 py-2 text-xs text-[#52525B] sm:flex-row sm:justify-between">
        <span>24h totals use operation updates in the last 24 hours; failure rate uses completed, failed, and cancelled operations.</span>
        <span v-if="metrics.oldest_open_at" class="tabular-nums">Oldest open: {{ formatTime(metrics.oldest_open_at) }}</span>
      </div>
    </section>

    <section v-if="metrics?.upstream_health.length" aria-label="Configured upstream health checks" class="overflow-hidden rounded-lg border border-[#E4E4E7] bg-white">
      <div class="flex flex-col gap-2 border-b border-[#E4E4E7] px-4 py-3 sm:flex-row sm:items-center sm:justify-between"><div><h2 class="text-[15px] font-semibold text-[#09090B]">Configured upstream health checks</h2><p class="mt-1 text-xs text-[#52525B]">Probe results are shown only for upstreams with a health check configured.</p></div><NuxtLink to="/upstreams" class="inline-flex min-h-11 items-center font-medium text-[#0F766E] underline underline-offset-2">Review upstreams</NuxtLink></div>
      <div v-if="metrics.upstream_health.some((row) => row.status === 'unhealthy' || row.status === 'degraded')" role="alert" class="border-b border-[#FDE68A] bg-[#FFFBEB] px-4 py-2 text-xs text-[#92400E]">{{ metrics.upstream_health.filter((row) => row.status === 'unhealthy' || row.status === 'degraded').length }} configured upstream health check(s) are degraded or unhealthy. Review probe status and recovery activity together.</div>
      <div class="overflow-x-auto"><table class="w-full min-w-[520px] text-left text-xs"><thead class="bg-[#F4F4F5] text-[#52525B]"><tr><th class="px-4 py-2">Upstream</th><th class="px-4 py-2">Health</th><th class="px-4 py-2">Consecutive failures</th><th class="px-4 py-2">Last checked</th><th class="px-4 py-2">Error type</th></tr></thead><tbody class="divide-y divide-[#E4E4E7]"><tr v-for="row in metrics.upstream_health" :key="row.server"><td class="px-4 py-2 font-medium text-[#09090B]">{{ row.server }}</td><td class="px-4 py-2 capitalize" :class="row.status === 'unhealthy' ? 'text-[#B91C1C]' : row.status === 'degraded' ? 'text-[#92400E]' : 'text-[#047857]'">{{ row.status }}</td><td class="px-4 py-2 tabular-nums">{{ row.consecutive_failures }}</td><td class="px-4 py-2 tabular-nums text-[#52525B]">{{ formatTime(row.checked_at) }}</td><td class="px-4 py-2 text-[#52525B]">{{ row.error_type || "—" }}</td></tr></tbody></table></div>
    </section>
    <p v-else-if="metrics" class="text-xs text-[#71717A]">No upstream health probes are configured. Recovery metrics reflect operation outcomes; configure health checks under <NuxtLink to="/upstreams" class="font-medium text-[#0F766E] underline underline-offset-2">Upstreams</NuxtLink> to show availability here.</p>

    <section v-if="metrics?.by_upstream_24h.length" class="overflow-hidden rounded-lg border border-[#E4E4E7] bg-white">
      <div class="border-b border-[#E4E4E7] px-4 py-3"><h2 class="text-[15px] font-semibold text-[#09090B]">Upstream recovery activity · 24h</h2><p class="mt-1 text-xs text-[#52525B]">Operation state updates grouped by upstream. This shows recovery workload; use upstream health and alert views for live availability.</p></div>
      <div class="overflow-x-auto"><table class="w-full min-w-[520px] text-left text-xs"><thead class="bg-[#F4F4F5] text-[#52525B]"><tr><th class="px-4 py-2">Upstream</th><th class="px-4 py-2">State</th><th class="px-4 py-2 text-right">Operations</th><th class="px-4 py-2 text-right">Attempts</th></tr></thead><tbody class="divide-y divide-[#E4E4E7]"><tr v-for="row in metrics.by_upstream_24h" :key="`${row.upstream_name}:${row.status}`"><td class="px-4 py-2 font-medium text-[#09090B]">{{ row.upstream_name }}</td><td class="px-4 py-2 capitalize">{{ row.status.replaceAll("_", " ") }}</td><td class="px-4 py-2 text-right tabular-nums">{{ row.count }}</td><td class="px-4 py-2 text-right tabular-nums">{{ row.attempts }}</td></tr></tbody></table></div>
    </section>

    <div v-if="errorMessage" role="alert" class="flex flex-col gap-2 rounded-lg border border-[#FECACA] bg-[#FEF2F2] px-4 py-3 text-[13px] text-[#991B1B] sm:flex-row sm:items-center sm:justify-between">
      <span>{{ errorMessage }}</span>
      <Button class="min-h-11 shrink-0" @click="loadOperations">Retry loading operations</Button>
    </div>

    <div v-if="loading" aria-label="Loading operations" class="space-y-2" aria-busy="true">
      <div v-for="row in 3" :key="row" class="h-20 animate-pulse rounded border border-[#E4E4E7] bg-white" />
    </div>
    <section v-else-if="!errorMessage" class="overflow-hidden rounded-lg border border-[#E4E4E7] bg-white">
      <div v-if="operations.length === 0" class="px-5 py-12 text-center">
        <h2 class="text-[15px] font-semibold text-[#09090B]">No operations for this state</h2>
        <p class="mx-auto mt-2 max-w-md text-[13px] leading-5 text-[#52525B]">
          Operations appear when a configured idempotent write is sent through the gateway. Start continuous demo traffic or run the golden workload to create local mock operations. <NuxtLink to="/demo" class="font-medium text-[#0F766E] underline underline-offset-2">Open Demo Control</NuxtLink>.
        </p>
      </div>
      <div v-else class="overflow-x-auto">
        <table class="w-full min-w-[900px] border-collapse text-left text-[13px]">
          <caption class="sr-only">Recovery operations for {{ session?.user?.tenant_id || "the current tenant" }}</caption>
          <thead class="bg-[#F4F4F5] text-xs font-medium text-[#52525B]">
            <tr>
              <th scope="col" class="px-4 py-3">Operation</th>
              <th scope="col" class="px-4 py-3">State</th>
              <th scope="col" class="px-4 py-3">Attempts</th>
              <th scope="col" class="px-4 py-3">Requester</th>
              <th scope="col" class="px-4 py-3">Updated</th>
              <th scope="col" class="px-4 py-3">Recovery deadline</th>
            </tr>
          </thead>
          <tbody class="divide-y divide-[#E4E4E7]">
            <tr v-for="operation in operations" :key="operation.operation_id" class="align-top">
              <td class="px-4 py-4">
                <div class="font-medium text-[#09090B]">{{ operation.upstream_name }} / {{ operation.tool_name }}</div>
                <div class="mt-1 font-mono-data text-[11px] text-[#71717A]">{{ operation.operation_id }}</div>
                <p v-if="operation.last_error" class="mt-2 max-w-xl leading-5 text-[#991B1B]">
                  {{ operation.last_error_type }}: {{ operation.last_error }}
                </p>
              </td>
              <td class="px-4 py-4"><span class="font-medium capitalize text-[#09090B]">{{ operation.status.replaceAll("_", " ") }}</span></td>
              <td class="px-4 py-4 tabular-nums text-[#52525B]">{{ operation.attempt_count }} / {{ operation.max_attempts }}</td>
              <td class="px-4 py-4 font-mono-data text-xs text-[#52525B]">
                {{ operation.requester_sub }}
                <div v-if="operation.client_id" class="mt-1 text-[11px] text-[#71717A]">{{ operation.client_id }}</div>
              </td>
              <td class="px-4 py-4 tabular-nums text-[#52525B]">{{ formatTime(operation.updated_at) }}</td>
              <td class="px-4 py-4 tabular-nums text-[#52525B]">{{ formatTime(operation.recovery_deadline) }}</td>
            </tr>
          </tbody>
        </table>
      </div>
    </section>
  </div>
</template>
