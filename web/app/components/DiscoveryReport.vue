<script setup lang="ts">
import { Button } from "@ui-sdk/vue/button";
import { computed, onMounted, ref } from "vue";
import { extractErrorMessage } from "~/composables/useAdminApi";

interface Sample {
  request_id: string;
  operation: "listing" | "search";
  status: string;
  duration_ms: number;
  response_bytes?: number;
  discovery_search_ms?: number;
  applied_discovery: string;
  recommended_discovery?: string;
  estimated_discovery_tokens?: number;
  estimated_upfront_tokens?: number | null;
  estimate_is_lower_bound?: boolean;
  authorized_tools?: number;
  token_budget?: number;
  reason?: string;
}

const props = defineProps<{ policyOnly?: boolean }>();
const { apiFetch } = useAdminApi();
const report = ref<{ policy: string; token_budget: number; samples: Sample[] } | null>(null);
const error = ref("");
const loading = ref(true);

async function load() {
  loading.value = true;
  error.value = "";
  try {
    report.value = await apiFetch(`/admin/discovery/report?limit=${props.policyOnly ? 0 : 100}`);
  } catch (err) {
    error.value = extractErrorMessage(err);
  } finally {
    loading.value = false;
  }
}
onMounted(load);

const reasons: Record<string, string> = {
  fits_budget: "Fits the upfront budget",
  over_budget: "Exceeds the upfront budget",
  stub_cap: "Exceeds the stub limit",
  empty_catalog: "No authorized tools",
};
const snapshot = computed(() => report.value?.samples ?? []);
const searches = computed(() => snapshot.value.filter((row) => row.operation === "search").length);

const cohorts = computed(() => {
  const groups = new Map<string, Sample[]>();
  for (const row of snapshot.value) {
    const size = row.authorized_tools;
    const band = size == null ? "Unknown" : size <= 10 ? "0–10" : size <= 50 ? "11–50" : "51+";
    const key = `${row.operation} · ${band} · ${row.applied_discovery}`;
    const group = groups.get(key) ?? [];
    group.push(row);
    groups.set(key, group);
  }
  return [...groups].map(([key, rows]) => {
    const latencies = rows.map((row) => row.duration_ms).sort((a, b) => a - b);
    const tokens = rows.flatMap((row) =>
      row.estimated_discovery_tokens == null ? [] : [row.estimated_discovery_tokens]
    );
    return {
      key,
      count: rows.length,
      errors: rows.filter((row) => row.status !== "ok").length,
      p95: latencies[Math.ceil(latencies.length * .95) - 1] ?? 0,
      meanTokens: tokens.length ? Math.round(tokens.reduce((a, b) => a + b, 0) / tokens.length) : null,
    };
  });
});
</script>

<template>
  <section class="min-w-0 space-y-4" :class="policyOnly ? 'border-t border-zinc-200 pt-3' : 'rounded-lg border border-zinc-200 bg-white p-4'" aria-label="Discovery report">
    <div class="flex flex-wrap items-center justify-between gap-2">
      <h2 class="text-[15px] font-semibold text-zinc-950">{{ policyOnly ? "Discovery policy" : "Discovery requests" }}</h2>
      <Button v-if="!policyOnly || error" :disabled="loading" class="min-h-11" @click="load">
        {{ error ? "Retry discovery report" : "Refresh discovery report" }}
      </Button>
    </div>
    <div v-if="loading" role="status" class="h-10 animate-pulse rounded bg-zinc-100"><span class="sr-only">Loading discovery report</span></div>
    <p v-else-if="error" role="alert" class="text-sm text-red-800">{{ error }}</p>
    <template v-else-if="report">
      <p class="text-sm text-zinc-600"><strong class="text-zinc-950">{{ report.policy === "active" ? "Active" : "Observe" }}</strong> · Upfront budget <span class="tabular-nums">{{ report.token_budget.toLocaleString() }}</span> estimated tokens</p>
      <template v-if="!policyOnly">
        <p class="text-sm text-zinc-600">Recent requests on this replica. Token estimates cover gateway discovery payloads. Verified workflow outcomes appear under Routing and Efficiency.</p>
        <p v-if="!snapshot.length" class="text-sm text-zinc-600">No discovery requests recorded yet.</p>
        <template v-else>
          <p class="text-sm tabular-nums text-zinc-600">{{ searches }} searches across {{ snapshot.length }} discovery requests</p>
          <div class="overflow-x-auto rounded-md border border-zinc-200">
            <table class="w-full whitespace-nowrap text-left text-[13px] tabular-nums">
              <thead class="bg-zinc-100 text-xs text-zinc-600"><tr><th class="p-2">Operation · Catalog size · Applied</th><th class="p-2">Requests</th><th class="p-2">Mean estimated tokens</th><th class="p-2">p95 latency</th><th class="p-2">Errors</th></tr></thead>
              <tbody class="divide-y divide-zinc-200">
                <tr v-for="row in cohorts" :key="row.key"><td class="p-2">{{ row.key }}</td><td class="p-2">{{ row.count }}</td><td class="p-2">{{ row.meanTokens ?? "Unknown" }}</td><td class="p-2">{{ row.p95.toFixed(1) }} ms</td><td class="p-2">{{ row.errors }}</td></tr>
              </tbody>
            </table>
          </div>
          <h3 class="text-[15px] font-semibold">Recent request details</h3>
          <div class="divide-y divide-zinc-200">
            <details v-for="row in snapshot" :key="row.request_id" class="py-1.5 text-sm">
              <summary class="min-h-11 cursor-pointer py-2">{{ row.operation === "search" ? "Search" : "Listing" }} · {{ row.applied_discovery }} <code class="break-all text-xs text-zinc-600">{{ row.request_id }}</code></summary>
              <p class="pb-2 text-xs tabular-nums text-zinc-600">
                {{ row.duration_ms.toFixed(1) }} ms · {{ row.status }}
                <span v-if="row.response_bytes != null"> · {{ row.response_bytes }} response bytes</span>
                <span v-if="row.discovery_search_ms != null"> · {{ row.discovery_search_ms }} ms ranking</span>
                <span v-if="row.recommended_discovery"> · Recommended {{ row.recommended_discovery }} · {{ reasons[row.reason ?? ""] ?? row.reason }}</span>
                <span v-if="row.estimated_upfront_tokens != null"> · {{ row.estimate_is_lower_bound ? "At least " : "" }}{{ row.estimated_upfront_tokens }} estimated upfront tokens against a {{ row.token_budget }} token budget</span>
              </p>
            </details>
          </div>
        </template>
      </template>
    </template>
  </section>
</template>
