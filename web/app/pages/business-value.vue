<script setup lang="ts">
import { Button } from "@ui-sdk/vue/button";
import { Field, FieldLabel } from "@ui-sdk/vue/field";
import { Input } from "@ui-sdk/vue/input";
import { Select, SelectContent, SelectItem, SelectLabel, SelectTrigger, SelectValue } from "@ui-sdk/vue/select";
import { Textarea } from "@ui-sdk/vue/textarea";
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
interface DefinitionVersion { revision: string; status: string; created_at?: string; is_current?: boolean; definition_digest?: string }
interface DefinitionDetail { upstream_name: string; current_revision: string | null; current: { definition?: Record<string, unknown> } | null; versions: DefinitionVersion[] }

const { apiFetch, session } = useAdminApi();
const report = ref<BusinessValueReport | null>(null);
const reportLoading = ref(true);
const reportError = ref("");
const definitionText = ref("");
const upstreamName = ref("");
const detail = ref<DefinitionDetail | null>(null);
const revision = ref("");
const validationMessage = ref("");
const actionMessage = ref("");
const authoringError = ref("");
const busy = ref(false);
const isAdmin = computed(() => Boolean(session.value?.is_admin));
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

function parseDefinition() {
  try {
    const parsed = JSON.parse(definitionText.value) as { integration?: { upstream_name?: string } };
    const target = parsed.integration?.upstream_name;
    if (!target) throw new Error("Add integration.upstream_name before continuing.");
    upstreamName.value = target;
    authoringError.value = "";
    return parsed;
  } catch (error) {
    authoringError.value = error instanceof SyntaxError ? `Invalid JSON: ${error.message}` : extractErrorMessage(error);
    return null;
  }
}

async function validateDefinition() {
  const definition = parseDefinition();
  if (!definition) return;
  busy.value = true; authoringError.value = ""; validationMessage.value = ""; actionMessage.value = "";
  try {
    const result = await apiFetch<{ valid: boolean; upstream_name: string; definition_digest: string; tool_count: number; workflow_count: number; standalone_tool_value_count: number }>("/admin/integration-definitions/validate", { method: "POST", body: definition });
    validationMessage.value = `Valid · ${result.tool_count} tools · ${result.workflow_count} workflows · ${result.standalone_tool_value_count} standalone values · ${result.definition_digest}`;
  } catch (error) {
    authoringError.value = extractErrorMessage(error);
  } finally { busy.value = false; }
}

async function saveDraft() {
  const definition = parseDefinition();
  if (!definition) return;
  busy.value = true; authoringError.value = ""; actionMessage.value = "";
  try {
    const result = await apiFetch<{ revision: string; definition_digest: string }>("/admin/integration-definitions/drafts", { method: "POST", body: { definition } });
    actionMessage.value = `Draft ${result.revision} saved · ${result.definition_digest}`;
    await loadDefinition();
  } catch (error) { authoringError.value = extractErrorMessage(error); }
  finally { busy.value = false; }
}

async function loadDefinition() {
  const target = upstreamName.value.trim();
  if (!target) { authoringError.value = "Enter an upstream name or add integration.upstream_name to the JSON."; return; }
  busy.value = true; authoringError.value = "";
  try {
    detail.value = await apiFetch<DefinitionDetail>(`/admin/integration-definitions/${encodeURIComponent(target)}`);
    if (detail.value.current?.definition) definitionText.value = JSON.stringify(detail.value.current.definition, null, 2);
    revision.value = detail.value.current_revision || detail.value.versions[0]?.revision || "";
  } catch (error) { authoringError.value = extractErrorMessage(error); }
  finally { busy.value = false; }
}

async function applyRevision(action: "publish" | "rollback") {
  const target = upstreamName.value.trim();
  if (!target || !revision.value) { authoringError.value = "Load a definition and choose a revision first."; return; }
  busy.value = true; authoringError.value = ""; actionMessage.value = "";
  try {
    const result = await apiFetch<{ revision: string; previous_revision: string | null }>(`/admin/integration-definitions/${encodeURIComponent(target)}/${action}`, { method: "POST", body: { revision: revision.value } });
    actionMessage.value = `${action === "publish" ? "Published" : "Rolled back to"} ${result.revision}${result.previous_revision ? ` · previous ${result.previous_revision}` : ""}`;
    await loadDefinition();
  } catch (error) { authoringError.value = extractErrorMessage(error); }
  finally { busy.value = false; }
}

onMounted(() => { void loadReport(); });
</script>

<template>
  <div class="space-y-6">
    <header>
      <h1 class="text-[20px] font-semibold tracking-[-0.015em] text-[#09090B]">Business Value</h1>
      <p class="mt-1 max-w-3xl text-[13px] leading-5 text-[#52525B]">Review attributed business outcomes and the baseline definitions that explain what each completed workflow measures.</p>
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
        <h3 class="text-sm font-semibold text-[#09090B]">No attributed business outcomes yet</h3><p class="mt-1 text-sm text-[#52525B]">Publish a definition and record eligible workflow completions to populate this report.</p>
      </div>
    </section>

    <section aria-labelledby="definition-heading" class="rounded-lg border border-[#E4E4E7] bg-white p-4 sm:p-5">
      <div>
        <h2 id="definition-heading" class="text-[15px] font-semibold text-[#09090B]">Integration definition</h2>
        <p class="mt-1 text-[13px] text-[#52525B]">Validate the JSON manifest against the current tool catalog and schemas, save an immutable draft, then publish or roll back a selected revision.</p>
      </div>
      <div v-if="!isAdmin" role="status" class="mt-4 rounded-md border border-[#FDE68A] bg-[#FFFBEB] p-3 text-sm text-[#78350F]">Read-only access. An administrator can validate, save, publish, or roll back definitions.</div>
      <div class="mt-4 grid grid-cols-1 gap-5 xl:grid-cols-[minmax(0,1.4fr)_minmax(280px,0.8fr)]">
        <div class="min-w-0">
          <Field name="definition-json">
            <FieldLabel class="mb-1 block text-xs font-medium text-[#52525B]">Definition JSON</FieldLabel>
            <Textarea v-model:value="definitionText" :disabled="!isAdmin" rows="24" spellcheck="false" class="min-h-[420px] w-full rounded-md border border-[#D4D4D8] bg-white p-3 font-mono text-xs leading-5 text-[#09090B] selection:bg-teal-700/15 selection:text-teal-950 focus-visible:outline-2 focus-visible:outline-[#0F766E] disabled:bg-[#F4F4F5]" placeholder="Paste an integration definition manifest" />
          </Field>
          <div class="mt-3 flex flex-wrap gap-2">
            <Button type="button" class="min-h-11 rounded-md bg-[#0F766E] px-3.5 text-sm font-medium text-white hover:bg-[#115E59] focus-visible:outline-2 focus-visible:outline-[#0F766E] disabled:opacity-50" :disabled="busy || !isAdmin" @click="validateDefinition">{{ busy ? "Working…" : "Validate manifest" }}</Button>
            <Button type="button" class="min-h-11 rounded-md border border-[#D4D4D8] bg-white px-3.5 text-sm font-medium text-[#09090B] hover:bg-[#F4F4F5] focus-visible:outline-2 focus-visible:outline-[#0F766E] disabled:opacity-50" :disabled="busy || !isAdmin" @click="saveDraft">Save draft</Button>
          </div>
          <p v-if="validationMessage" role="status" class="mt-3 break-all rounded-md border border-[#A7F3D0] bg-[#ECFDF5] p-3 text-sm text-[#065F46]">{{ validationMessage }}</p>
          <p v-if="actionMessage" role="status" class="mt-3 break-all rounded-md border border-[#A7F3D0] bg-[#ECFDF5] p-3 text-sm text-[#065F46]">{{ actionMessage }}</p>
          <div v-if="authoringError" role="alert" class="mt-3 rounded-md border border-[#FECACA] bg-[#FEF2F2] p-3 text-sm text-[#991B1B]">{{ authoringError }} <span>Correct the manifest or access issue, then retry.</span></div>
        </div>
        <aside class="min-w-0 border-t border-[#E4E4E7] pt-4 xl:border-l xl:border-t-0 xl:pl-5 xl:pt-0">
          <h3 class="text-sm font-semibold text-[#09090B]">Revision history</h3>
          <Field name="upstream-name" class="mt-3">
            <FieldLabel class="mb-1 block text-xs font-medium text-[#52525B]">Upstream name</FieldLabel>
            <Input v-model:value="upstreamName" :disabled="busy" class="min-h-11 w-full rounded-md border border-[#D4D4D8] px-3 text-sm text-[#09090B] focus-visible:outline-2 focus-visible:outline-[#0F766E] disabled:bg-[#F4F4F5]" placeholder="support_api" />
          </Field>
          <Button type="button" class="mt-2 min-h-11 w-full rounded-md border border-[#D4D4D8] bg-white px-3.5 text-sm font-medium text-[#09090B] hover:bg-[#F4F4F5] focus-visible:outline-2 focus-visible:outline-[#0F766E] disabled:opacity-50" :disabled="busy || !upstreamName.trim()" @click="loadDefinition">Load versions</Button>
          <p v-if="detail" class="mt-3 text-xs text-[#52525B]">Current revision: <span class="font-mono text-[#09090B]">{{ detail.current_revision || "none" }}</span></p>
          <Field v-if="detail?.versions.length" name="definition-revision" class="mt-3">
            <FieldLabel class="mb-1 block text-xs font-medium text-[#52525B]">Revision to publish or restore</FieldLabel>
            <Select v-model:value="revision" :items="detail.versions.map((version) => ({ value: version.revision, label: `${version.revision} · ${version.status}${version.is_current ? ' · current' : ''}` }))">
              <SelectLabel class="mb-1 block text-xs font-medium text-[#52525B]">Choose a revision</SelectLabel>
              <SelectTrigger class="mt-1 flex min-h-11 w-full items-center justify-between rounded-md border border-[#D4D4D8] bg-white px-3 text-sm focus-visible:outline-2 focus-visible:outline-[#0F766E]"><SelectValue /></SelectTrigger>
              <SelectContent class="z-50 max-h-64 overflow-auto rounded-md border border-[#E4E4E7] bg-white p-1 shadow-[0_4px_12px_rgb(9_9_11_/_0.08)]"><SelectItem v-for="version in detail.versions" :key="version.revision" :value="version.revision" class="min-h-11 cursor-pointer rounded px-3 py-2 text-sm data-[highlighted]:bg-[#F4F4F5] data-[selected]:font-medium">{{ version.revision }} · {{ version.status }}{{ version.is_current ? " · current" : "" }}</SelectItem></SelectContent>
            </Select>
          </Field>
          <div v-if="detail?.versions.length" class="mt-3 overflow-x-auto rounded-md border border-[#E4E4E7]">
            <table class="w-full min-w-[320px] text-left text-xs"><thead class="bg-[#F4F4F5] text-[#52525B]"><tr><th class="px-2 py-2 font-medium">Revision</th><th class="px-2 py-2 font-medium">Status</th><th class="px-2 py-2 font-medium">Created</th></tr></thead><tbody><tr v-for="version in detail.versions" :key="version.revision" class="border-t border-[#E4E4E7]"><td class="px-2 py-2 font-mono text-[#09090B]">{{ version.revision }}</td><td class="px-2 py-2 capitalize text-[#52525B]">{{ version.status }}{{ version.is_current ? " · current" : "" }}</td><td class="px-2 py-2 tabular-nums text-[#52525B]">{{ version.created_at || "—" }}</td></tr></tbody></table>
          </div>
          <div class="mt-3 grid grid-cols-1 gap-2 sm:grid-cols-2 xl:grid-cols-1 2xl:grid-cols-2">
            <Button type="button" class="min-h-11 rounded-md bg-[#0F766E] px-3.5 text-sm font-medium text-white hover:bg-[#115E59] focus-visible:outline-2 focus-visible:outline-[#0F766E] disabled:opacity-50" :disabled="busy || !isAdmin || !revision" @click="applyRevision('publish')">Publish revision</Button>
            <Button type="button" class="min-h-11 rounded-md border border-[#D4D4D8] bg-white px-3.5 text-sm font-medium text-[#09090B] hover:bg-[#F4F4F5] focus-visible:outline-2 focus-visible:outline-[#0F766E] disabled:opacity-50" :disabled="busy || !isAdmin || !revision" @click="applyRevision('rollback')">Roll back to revision</Button>
          </div>
          <p class="mt-3 text-xs leading-5 text-[#52525B]">Published revisions are immutable. Rollback publishes the selected historical definition as the new current revision.</p>
        </aside>
      </div>
    </section>
  </div>
</template>
