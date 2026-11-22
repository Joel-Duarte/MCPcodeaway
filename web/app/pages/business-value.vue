<script setup lang="ts">
import { Button } from "@ui-sdk/vue/button";
import { onMounted, ref } from "vue";
import { extractErrorMessage } from "~/composables/useAdminApi";

interface BusinessValueRow {
  integration_id: string;
  integration_display_name: string;
  upstream_name: string;
  selected_kind: string;
  selected_id: string;
  definition_revision: number | null;
  period_utc: string;
  baseline_source: string;
  baseline_confidence: string;
  currency: string | null;
  completed_count: number;
  estimated_manual_baseline_minutes: number | null;
  validated_manual_baseline_minutes: number | null;
  estimated_minutes_saved: number | null;
  estimated_labor_value: { amount: number; currency: string } | null;
}
interface BusinessValueReport {
  tenant_scope: string;
  since: string | null;
  events_returned: number;
  truncated: boolean;
  executions_returned: number;
  executions_truncated: boolean;
  completed_count: number;
  execution_status_counts: Record<string, number>;
  by_integration_workflow_day: BusinessValueRow[];
  totals_by_currency: Record<string, { completed_count: number; estimated_manual_baseline_minutes: number; validated_manual_baseline_minutes: number; estimated_minutes_saved: number | null }>;
  validated_minutes_saved: number | null;
  cost_coverage: { captured_categories: string[]; missing_categories: string[]; roi_available: boolean };
}
const { apiFetch } = useAdminApi();
const report = ref<BusinessValueReport | null>(null);
const reportLoading = ref(true);
const reportError = ref("");
const number = (value: number | null | undefined, digits = 1) => value == null ? "Unavailable" : value.toLocaleString("en-US", { maximumFractionDigits: digits });
const money = (amount: number | null | undefined, currency: string | null | undefined) => amount == null || !currency ? "Unavailable" : `${currency} ${amount.toLocaleString("en-US", { minimumFractionDigits: 2, maximumFractionDigits: 2 })}`;
const statusLabel = (status: string) => status.replaceAll("_", " ");

async function loadReport() {
  reportLoading.value = true;
  reportError.value = "";
  try {
    report.value = await apiFetch<BusinessValueReport>("/admin/business-value/report");
  } catch (error) {
    reportError.value = extractErrorMessage(error);
  } finally {
    reportLoading.value = false;
  }
}

onMounted(() => { void loadReport(); });
</script>

<template>
  <div class="space-y-6">
    <header>
      <h1 class="text-[20px] font-semibold tracking-[-0.015em] text-[#09090B]">Business Value</h1>
      <p class="mt-1 max-w-3xl text-[13px] leading-5 text-[#52525B]">See which administrator-defined workflows completed, what human baseline they measure, and which value estimates are supported by evidence.</p>
    </header>

    <section aria-labelledby="report-heading" class="rounded-lg border border-[#E4E4E7] bg-white p-4 sm:p-5">
      <div class="flex flex-wrap items-start justify-between gap-3">
        <div>
          <h2 id="report-heading" class="text-[15px] font-semibold text-[#09090B]">Outcome report</h2>
          <p class="mt-1 text-[13px] text-[#52525B]">Values are grouped by integration, workflow, and UTC day. A baseline is an estimate unless its provenance is validated.</p>
        </div>
        <Button type="button" class="min-h-11 rounded-md border border-[#D4D4D8] bg-white px-3.5 text-sm font-medium text-[#09090B] hover:bg-[#F4F4F5] focus-visible:outline-2 focus-visible:outline-[#0F766E] disabled:opacity-50" :disabled="reportLoading" @click="loadReport">Refresh report</Button>
      </div>
      <div v-if="reportError" role="alert" class="mt-4 rounded-md border border-[#FECACA] bg-[#FEF2F2] p-3 text-sm text-[#991B1B]">{{ reportError }} <span>Check your admin or viewer access and retry.</span></div>
      <div v-if="reportLoading" class="mt-5 space-y-3" aria-label="Loading business value report">
        <div class="h-10 animate-pulse rounded bg-[#F4F4F5]"></div><div class="h-28 animate-pulse rounded bg-[#F4F4F5]"></div>
      </div>
      <div v-else-if="report && report.executions_returned" class="mt-5">
        <div class="grid grid-cols-1 gap-3 sm:grid-cols-2 xl:grid-cols-4">
          <div class="rounded-md bg-[#F4F4F5] p-3"><div class="text-xs text-[#52525B]">Completed outcomes</div><div class="mt-1 text-lg font-semibold tabular-nums text-[#09090B]">{{ number(report.completed_count, 0) }}</div></div>
          <div class="rounded-md bg-[#F4F4F5] p-3"><div class="text-xs text-[#52525B]">Gross estimated baseline</div><div class="mt-1 text-lg font-semibold tabular-nums text-[#09090B]">{{ number(report.by_integration_workflow_day.reduce((sum, row) => sum + (row.estimated_manual_baseline_minutes || 0), 0)) }} min</div></div>
          <div class="rounded-md bg-[#F4F4F5] p-3"><div class="text-xs text-[#52525B]">Validated baseline</div><div class="mt-1 text-lg font-semibold tabular-nums text-[#09090B]">{{ number(report.by_integration_workflow_day.filter((row) => row.baseline_confidence === "validated").reduce((sum, row) => sum + (row.validated_manual_baseline_minutes || 0), 0)) }} min</div><div class="mt-1 text-xs text-[#52525B]">Validated evidence only; sampled baselines are shown by provenance below.</div></div>
          <div class="rounded-md bg-[#F4F4F5] p-3"><div class="text-xs text-[#52525B]">Net savings / ROI</div><div class="mt-1 text-lg font-semibold tabular-nums text-[#09090B]">{{ report.cost_coverage.roi_available && report.validated_minutes_saved != null ? `${number(report.validated_minutes_saved)} min` : "Unavailable" }}</div><div class="mt-1 text-xs text-[#52525B]">Missing cost coverage: {{ report.cost_coverage.missing_categories.map(statusLabel).join(", ") || "none" }}</div></div>
        </div>
        <div v-if="report.truncated" role="status" class="mt-4 rounded-md border border-[#FDE68A] bg-[#FFFBEB] p-3 text-sm text-[#78350F]">The report reached its event limit. Narrow the reporting period before using totals.</div>
        <div class="mt-4 overflow-x-auto rounded-md border border-[#E4E4E7]">
          <table class="w-full min-w-[920px] border-collapse text-left text-[13px]">
            <thead class="bg-[#F4F4F5] text-xs text-[#52525B]"><tr><th class="px-3 py-2 font-medium">Integration / workflow</th><th class="px-3 py-2 font-medium">Period · status</th><th class="px-3 py-2 font-medium">Completions</th><th class="px-3 py-2 font-medium">Gross baseline</th><th class="px-3 py-2 font-medium">Validated baseline</th><th class="px-3 py-2 font-medium">Estimated value</th><th class="px-3 py-2 font-medium">Provenance</th></tr></thead>
            <tbody><tr v-for="(row, index) in report.by_integration_workflow_day" :key="`${row.integration_id}:${row.selected_id}:${row.period_utc}:${index}`" class="border-t border-[#E4E4E7] align-top">
              <td class="px-3 py-3"><div class="font-medium text-[#09090B]">{{ row.integration_display_name }}</div><div class="mt-0.5 text-[#52525B]">{{ row.selected_id }} · {{ statusLabel(row.selected_kind) }}</div><div class="mt-0.5 text-xs text-[#71717A]">{{ row.upstream_name }} · revision {{ row.definition_revision ?? "unknown" }}</div></td>
              <td class="px-3 py-3 tabular-nums text-[#52525B]">{{ row.period_utc }}<div class="mt-1 text-xs">completed</div></td>
              <td class="px-3 py-3 tabular-nums text-[#09090B]">{{ number(row.completed_count, 0) }}</td>
              <td class="px-3 py-3 tabular-nums text-[#09090B]">{{ row.estimated_manual_baseline_minutes == null ? "Unavailable" : `${number(row.estimated_manual_baseline_minutes)} min` }}</td>
              <td class="px-3 py-3 tabular-nums text-[#09090B]">{{ row.baseline_confidence !== "validated" ? "Unavailable" : `${number(row.validated_manual_baseline_minutes)} min` }}</td>
              <td class="px-3 py-3 tabular-nums text-[#09090B]">{{ money(row.estimated_labor_value?.amount, row.estimated_labor_value?.currency) }}</td>
              <td class="px-3 py-3"><span class="capitalize">{{ statusLabel(row.baseline_confidence) }}</span><div class="mt-1 text-xs text-[#52525B]">{{ statusLabel(row.baseline_source) }}</div></td>
            </tr></tbody>
          </table>
        </div>
        <div class="mt-4 flex flex-wrap gap-x-5 gap-y-2 border-t border-[#E4E4E7] pt-3 text-xs text-[#52525B]"><span v-for="(count, status) in report.execution_status_counts" :key="status" class="capitalize">{{ statusLabel(status) }}: <span class="tabular-nums font-medium text-[#09090B]">{{ number(count, 0) }}</span></span><span>Outcome events returned: <span class="tabular-nums font-medium text-[#09090B]">{{ number(report.events_returned, 0) }}</span>{{ report.truncated ? " · event limit reached" : "" }}</span><span>Executions returned: <span class="tabular-nums font-medium text-[#09090B]">{{ number(report.executions_returned, 0) }}</span>{{ report.executions_truncated ? " · execution limit reached" : "" }}</span></div>
        <p class="mt-3 text-xs text-[#52525B]">Gross baseline is not net savings. Net savings and ROI remain unavailable until validated savings and all required cost categories are present.</p>
      </div>
      <div v-else-if="report && !report.by_integration_workflow_day.length" class="mt-5 rounded-md border border-dashed border-[#D4D4D8] p-5 text-sm text-[#52525B]">No completed outcomes are available in this reporting period. Execution status counts above show the current workflow population.</div>
      <div v-else-if="!reportError" class="mt-5 rounded-md border border-dashed border-[#D4D4D8] p-6 text-center">
        <h3 class="text-sm font-semibold text-[#09090B]">No attributed business outcomes yet</h3><p class="mt-1 text-sm text-[#52525B]">Create a workflow during upstream onboarding, choose its catalog tools and completion evidence, and record a human-time baseline. Eligible completions will appear here.</p>
      </div>
    </section>

  </div>
</template>
