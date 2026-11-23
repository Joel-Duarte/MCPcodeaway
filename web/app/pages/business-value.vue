<script setup lang="ts">
import { Button } from "@ui-sdk/vue/button";
import { computed, onMounted, ref } from "vue";
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
const reportRefreshing = ref(false);
const reportError = ref("");
const reportFetchedAt = ref<Date | null>(null);
const number = (value: number | null | undefined, digits = 1) => value == null ? "Unavailable" : value.toLocaleString("en-US", { maximumFractionDigits: digits });
const money = (amount: number | null | undefined, currency: string | null | undefined) => amount == null || !currency ? "Unavailable" : `${currency} ${amount.toLocaleString("en-US", { minimumFractionDigits: 2, maximumFractionDigits: 2 })}`;
const statusLabel = (status: string) => status.replaceAll("_", " ");
const costCategoryLabel = (category: string) => category === "llm_inference" ? "LLM inference" : statusLabel(category);
const reportRows = computed(() => report.value?.by_integration_workflow_day ?? []);
const grossBaselineTotal = computed(() => {
  if (!reportRows.value.length || reportRows.value.some((row) => row.estimated_manual_baseline_minutes == null)) return null;
  return reportRows.value.reduce((sum, row) => sum + (row.estimated_manual_baseline_minutes ?? 0), 0);
});
const validatedBaselineTotal = computed(() => {
  const validatedRows = reportRows.value.filter((row) => row.baseline_confidence === "validated");
  if (!validatedRows.length || validatedRows.some((row) => row.validated_manual_baseline_minutes == null)) return null;
  return validatedRows.reduce((sum, row) => sum + (row.validated_manual_baseline_minutes ?? 0), 0);
});
const reportFetchedLabel = computed(() => reportFetchedAt.value
  ? new Intl.DateTimeFormat(undefined, { dateStyle: "medium", timeStyle: "short" }).format(reportFetchedAt.value)
  : "");
const minutes = (value: number | null | undefined) => value == null ? "Unavailable" : `${number(value)} min`;

async function loadReport() {
  if (report.value) reportRefreshing.value = true;
  else reportLoading.value = true;
  reportError.value = "";
  try {
    report.value = await apiFetch<BusinessValueReport>("/admin/business-value/report");
    reportFetchedAt.value = new Date();
  } catch (error) {
    reportError.value = extractErrorMessage(error);
  } finally {
    reportLoading.value = false;
    reportRefreshing.value = false;
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
          <p class="mt-1 max-w-3xl text-[13px] leading-5 text-[#52525B]">Each row groups completed outcomes by integration, workflow, and UTC day. The human-time baseline is an estimate unless its evidence is validated; it is not net savings.</p>
        </div>
        <div class="flex flex-wrap items-center gap-3">
          <p v-if="reportFetchedAt" role="status" class="text-xs text-[#52525B]">Fetched {{ reportFetchedLabel }} local time · periods use UTC</p>
          <Button type="button" class="min-h-11 rounded-md border border-[#D4D4D8] bg-white px-3.5 text-sm font-medium text-[#09090B] hover:bg-[#F4F4F5] focus-visible:outline-2 focus-visible:outline-[#0F766E] disabled:opacity-50" :disabled="reportLoading || reportRefreshing" @click="loadReport">{{ reportRefreshing ? "Refreshing…" : "Refresh report" }}</Button>
        </div>
      </div>
      <div v-if="reportError" role="alert" class="mt-4 flex flex-wrap items-center justify-between gap-2 rounded-md border border-[#FECACA] bg-[#FEF2F2] p-3 text-sm text-[#991B1B]">
        <span>{{ reportError }} Check your access or connection, then retry.</span>
        <Button type="button" class="min-h-11 rounded-md border border-[#D4D4D8] bg-white px-3 text-sm font-medium text-[#09090B] hover:bg-[#F4F4F5] focus-visible:outline-2 focus-visible:outline-[#0F766E]" :disabled="reportRefreshing" @click="loadReport">Retry</Button>
      </div>
      <div v-if="reportLoading" class="mt-5 space-y-3" aria-label="Loading business value report">
        <div class="h-10 animate-pulse rounded bg-[#F4F4F5]"></div><div class="h-28 animate-pulse rounded bg-[#F4F4F5]"></div>
      </div>
      <div v-else-if="report" class="mt-5">
        <dl class="grid grid-cols-1 divide-y divide-[#E4E4E7] border-y border-[#E4E4E7] sm:grid-cols-2 sm:divide-y-0 xl:grid-cols-4 xl:divide-x xl:divide-y-0">
          <div class="py-3 sm:pr-4 xl:pl-4 xl:first:pl-0"><dt class="text-xs font-medium text-[#52525B]">Completed outcomes</dt><dd class="mt-1 text-lg font-semibold tabular-nums text-[#09090B]">{{ number(report.completed_count, 0) }}</dd></div>
          <div class="py-3 sm:pr-4 xl:pl-4"><dt class="text-xs font-medium text-[#52525B]">Gross estimated baseline</dt><dd class="mt-1 text-lg font-semibold tabular-nums text-[#09090B]">{{ minutes(grossBaselineTotal) }}</dd><dd class="mt-1 text-xs leading-5 text-[#52525B]">Estimated human time for completed workflows, before review or correction time.</dd></div>
          <div class="py-3 sm:pr-4 xl:pl-4"><dt class="text-xs font-medium text-[#52525B]">Validated baseline</dt><dd class="mt-1 text-lg font-semibold tabular-nums text-[#09090B]">{{ minutes(validatedBaselineTotal) }}</dd><dd class="mt-1 text-xs leading-5 text-[#52525B]">Unavailable when no row has validated baseline evidence; sampled estimates are shown below.</dd></div>
          <div class="py-3 xl:pl-4"><dt class="text-xs font-medium text-[#52525B]">Validated minutes saved</dt><dd class="mt-1 text-lg font-semibold tabular-nums text-[#09090B]">{{ minutes(report.validated_minutes_saved) }}</dd><dd class="mt-1 text-xs leading-5 text-[#52525B]">{{ report.cost_coverage.roi_available ? "Cost coverage is complete, but this report has no dollar net savings or ROI amount." : `Dollar net savings and ROI unavailable: ${report.cost_coverage.missing_categories.map(costCategoryLabel).join(", ") || "validated savings or required cost coverage"} are not captured. Missing inputs are not treated as zero.` }}</dd></div>
        </dl>
        <div v-if="report.truncated" role="status" class="mt-4 rounded-md border border-[#FDE68A] bg-[#FFFBEB] p-3 text-sm text-[#78350F]">The report reached its event limit. Narrow the reporting period before using totals.</div>
        <div v-if="reportRows.length" class="mt-4 hidden overflow-x-auto rounded-md border border-[#E4E4E7] sm:block">
          <table class="w-full min-w-[920px] border-collapse text-left text-[13px]">
            <thead class="bg-[#F4F4F5] text-xs text-[#52525B]"><tr><th class="px-3 py-2 font-medium">Integration / workflow</th><th class="px-3 py-2 font-medium">UTC period · status</th><th class="px-3 py-2 font-medium">Completions</th><th class="px-3 py-2 font-medium">Gross baseline</th><th class="px-3 py-2 font-medium">Validated baseline</th><th class="px-3 py-2 font-medium">Estimated labor value</th><th class="px-3 py-2 font-medium">Baseline evidence</th></tr></thead>
            <tbody><tr v-for="row in reportRows" :key="`${row.integration_id}:${row.selected_id}:${row.period_utc}:${row.baseline_source}:${row.baseline_confidence}`" class="border-t border-[#E4E4E7] align-top">
              <td class="px-3 py-3"><div class="font-medium text-[#09090B]">{{ row.integration_display_name }}</div><div class="mt-0.5 text-[#52525B]">{{ row.selected_id }} · {{ statusLabel(row.selected_kind) }}</div><div class="mt-0.5 text-xs text-[#71717A]">{{ row.upstream_name }} · revision {{ row.definition_revision ?? "unknown" }}</div><details class="mt-1 text-xs text-[#52525B]"><summary class="min-h-11 cursor-pointer py-3 font-medium text-[#0F766E] underline underline-offset-2 focus-visible:outline-2 focus-visible:outline-[#0F766E]">View evidence basis</summary><p class="pb-2 leading-5">This row groups {{ number(row.completed_count, 0) }} completed outcome{{ row.completed_count === 1 ? "" : "s" }} for this workflow and UTC day. Person-level records and individual event IDs are not included in this report.</p><dl class="grid grid-cols-2 gap-x-3 gap-y-2 border-t border-[#E4E4E7] py-2"><div><dt class="text-[#71717A]">Baseline source</dt><dd>{{ statusLabel(row.baseline_source) }}</dd></div><div><dt class="text-[#71717A]">Evidence status</dt><dd class="capitalize">{{ statusLabel(row.baseline_confidence) }}</dd></div><div><dt class="text-[#71717A]">Report period</dt><dd class="tabular-nums">{{ row.period_utc }} UTC</dd></div><div><dt class="text-[#71717A]">Definition revision</dt><dd class="tabular-nums">{{ row.definition_revision ?? "Unavailable" }}</dd></div><div><dt class="text-[#71717A]">Estimated time saved</dt><dd>{{ minutes(row.estimated_minutes_saved) }}</dd></div><div><dt class="text-[#71717A]">Estimated labor value</dt><dd>{{ money(row.estimated_labor_value?.amount, row.estimated_labor_value?.currency) }}</dd></div></dl></details></td>
              <td class="px-3 py-3 tabular-nums text-[#52525B]">{{ row.period_utc }}<div class="mt-1 text-xs">completed</div></td>
              <td class="px-3 py-3 tabular-nums text-[#09090B]">{{ number(row.completed_count, 0) }}</td>
              <td class="px-3 py-3 tabular-nums text-[#09090B]">{{ minutes(row.estimated_manual_baseline_minutes) }}</td>
              <td class="px-3 py-3 tabular-nums text-[#09090B]">{{ row.baseline_confidence !== "validated" ? "Unavailable" : minutes(row.validated_manual_baseline_minutes) }}</td>
              <td class="px-3 py-3 tabular-nums text-[#09090B]">{{ money(row.estimated_labor_value?.amount, row.estimated_labor_value?.currency) }}</td>
              <td class="px-3 py-3"><span class="capitalize">{{ statusLabel(row.baseline_confidence) }}</span><div class="mt-1 text-xs text-[#52525B]">{{ statusLabel(row.baseline_source) }}</div></td>
            </tr></tbody>
          </table>
        </div>
        <div v-if="reportRows.length" class="mt-4 divide-y divide-[#E4E4E7] sm:hidden">
          <section v-for="row in reportRows" :key="`${row.integration_id}:${row.selected_id}:${row.period_utc}:${row.baseline_source}:${row.baseline_confidence}`" class="py-4">
            <h3 class="text-sm font-semibold text-[#09090B]">{{ row.integration_display_name }}</h3>
            <p class="mt-1 break-words text-xs text-[#52525B]">{{ row.selected_id }} · {{ statusLabel(row.selected_kind) }}</p>
            <p class="mt-1 text-xs text-[#71717A]">{{ row.upstream_name }} · revision {{ row.definition_revision ?? "unknown" }}</p>
            <dl class="mt-3 grid grid-cols-2 gap-x-4 gap-y-3 text-xs">
              <div><dt class="text-[#71717A]">UTC period</dt><dd class="mt-0.5 tabular-nums text-[#09090B]">{{ row.period_utc }}</dd></div>
              <div><dt class="text-[#71717A]">Status</dt><dd class="mt-0.5 capitalize text-[#09090B]">Completed</dd></div>
              <div><dt class="text-[#71717A]">Completions</dt><dd class="mt-0.5 tabular-nums text-[#09090B]">{{ number(row.completed_count, 0) }}</dd></div>
              <div><dt class="text-[#71717A]">Gross baseline</dt><dd class="mt-0.5 tabular-nums text-[#09090B]">{{ minutes(row.estimated_manual_baseline_minutes) }}</dd></div>
              <div><dt class="text-[#71717A]">Validated baseline</dt><dd class="mt-0.5 tabular-nums text-[#09090B]">{{ row.baseline_confidence !== "validated" ? "Unavailable" : minutes(row.validated_manual_baseline_minutes) }}</dd></div>
              <div><dt class="text-[#71717A]">Estimated labor value</dt><dd class="mt-0.5 tabular-nums text-[#09090B]">{{ money(row.estimated_labor_value?.amount, row.estimated_labor_value?.currency) }}</dd></div>
              <div class="col-span-2"><dt class="text-[#71717A]">Baseline evidence</dt><dd class="mt-0.5 capitalize text-[#09090B]">{{ statusLabel(row.baseline_confidence) }} · {{ statusLabel(row.baseline_source) }}</dd></div>
            </dl>
            <details class="mt-2 text-xs text-[#52525B]"><summary class="min-h-11 cursor-pointer py-3 font-medium text-[#0F766E] underline underline-offset-2 focus-visible:outline-2 focus-visible:outline-[#0F766E]">View evidence basis</summary><p class="pb-2 leading-5">This row groups {{ number(row.completed_count, 0) }} completed outcome{{ row.completed_count === 1 ? "" : "s" }} for this workflow and UTC day. Person-level records and individual event IDs are not included in this report.</p><p class="border-t border-[#E4E4E7] py-2">Estimated minutes saved subtract expected review and correction time from the human baseline when those assumptions are available. Estimated labor value also requires a loaded labor cost per hour.</p></details>
          </section>
        </div>
        <div v-else class="mt-4 rounded-md border border-dashed border-[#D4D4D8] p-5 text-sm text-[#52525B]">No completed outcomes are available for this report. The execution counts below show the current workflow population.</div>
        <div class="mt-4 flex flex-wrap gap-x-5 gap-y-2 border-t border-[#E4E4E7] pt-3 text-xs text-[#52525B]"><span v-for="(count, status) in report.execution_status_counts" :key="status" class="capitalize">{{ statusLabel(status) }}: <span class="tabular-nums font-medium text-[#09090B]">{{ number(count, 0) }}</span></span><span>Outcome events returned: <span class="tabular-nums font-medium text-[#09090B]">{{ number(report.events_returned, 0) }}</span>{{ report.truncated ? " · event limit reached" : "" }}</span><span>Executions returned: <span class="tabular-nums font-medium text-[#09090B]">{{ number(report.executions_returned, 0) }}</span>{{ report.executions_truncated ? " · execution limit reached" : "" }}</span></div>
        <details class="mt-3 border-t border-[#E4E4E7] pt-3 text-xs text-[#52525B]"><summary class="min-h-11 cursor-pointer py-3 font-medium text-[#0F766E] underline underline-offset-2 focus-visible:outline-2 focus-visible:outline-[#0F766E]">How estimates and ROI work</summary><div class="space-y-2 pb-2 leading-5"><p>Estimated minutes saved are calculated from the administrator-entered human baseline minus expected review and correction time. Estimated labor value is estimated minutes saved multiplied by the loaded labor cost per hour, divided by 60.</p><p>Gross baseline is not net savings. A dollar ROI requires validated outcome evidence and captured LLM inference, gateway operations, and upstream or vendor costs. This report does not return a dollar ROI amount while those inputs are missing; missing costs are not counted as zero.</p></div></details>
      </div>
      <div v-else-if="!reportError" class="mt-5 rounded-md border border-dashed border-[#D4D4D8] p-6 text-center">
        <h3 class="text-sm font-semibold text-[#09090B]">No attributed business outcomes yet</h3><p class="mt-1 text-sm text-[#52525B]">Create a workflow during upstream onboarding, choose its catalog tools and completion evidence, and record a human-time baseline. Eligible completions will appear here.</p>
      </div>
    </section>

  </div>
</template>
