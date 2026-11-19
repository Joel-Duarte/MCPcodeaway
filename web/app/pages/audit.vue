<script setup lang="ts">
import { extractErrorMessage } from "~/composables/useAdminApi";

interface AuditSubcall {
  server: string;
  tool: string;
  status: string;
  duration_ms: number;
  raw_bytes: number;
  abac_rows_filtered: number;
  redactions_count: number;
  error?: string | null;
}

interface AuditRun {
  run_id: string;
  created_at?: string;
  mode: string;
  ok: boolean;
  duration_ms: number;
  tenant_id: string;
  user_sub: string;
  user_email?: string | null;
  user_groups?: string[];
  subcall_count: number;
  raw_upstream_bytes: number;
  returned_bytes: number;
  estimated_tokens_saved: number;
  schema_tokens_saved: number;
  abac_rows_filtered: number;
  redactions_count: number;
  grounding_blocked?: boolean;
  tokens_used_est?: number;
  tokens_saved_by?: string;
  monty_spawn_ms?: number;
  monty_exec_ms?: number;
  trace_spans?: {
    stage: string;
    label: string;
    start_ms: number;
    duration_ms: number;
    status: string;
  }[];
  code?: string | null;
  stdout?: string | null;
  error?: string | null;
  mode_decision_reason?: string | null;
  mode_decision_score?: number | null;
  experiment_id?: string | null;
  ab_variant?: string | null;
  provenance?: {
    grounded?: boolean;
    hmac_sha256?: string;
    unverified_identifiers?: string[];
  } | null;
  subcalls?: AuditSubcall[];
}

const { apiFetch } = useAdminApi();
const { events: wsEvents } = useLiveTelemetry();

const loading = ref(true);
const errorMessage = ref("");
const statusMessage = ref("");
const limitFilter = ref("25");
const offset = ref(0);
const totalCount = ref(0);
const statusFilter = ref<"all" | "errors" | "grounding_blocked" | "ok">("all");
const modeFilter = ref("all");
const searchQuery = ref("");
const liveMode = ref(true);
const tenantFilter = ref("");
const runs = ref<AuditRun[]>([]);
let searchDebounce: ReturnType<typeof setTimeout> | null = null;

const drawerOpen = ref(false);
const selectedRun = ref<AuditRun | null>(null);

const pruneDialogOpen = ref(false);
const retentionDays = ref("30");

const limitOptions = [
  { value: "25", label: "25 per page" },
  { value: "50", label: "50 per page" },
  { value: "100", label: "100 per page" },
];

async function fetchRuns(resetOffset = false) {
  if (resetOffset) offset.value = 0;
  errorMessage.value = "";
  const params = new URLSearchParams();
  params.set("limit", limitFilter.value || "25");
  params.set("offset", String(offset.value));
  params.set("status", statusFilter.value);
  if (modeFilter.value !== "all") {
    params.set("mode", modeFilter.value);
  }
  if (searchQuery.value.trim()) {
    params.set("q", searchQuery.value.trim());
  }
  if (tenantFilter.value.trim()) {
    params.set("tenant_id", tenantFilter.value.trim());
  }
  try {
    const res = await apiFetch<{ count: number; total_count?: number; runs: AuditRun[] }>(
      `/audit/runs?${params.toString()}`
    );
    runs.value = res.runs;
    totalCount.value = res.total_count ?? res.count;
  } catch (err) {
    errorMessage.value = extractErrorMessage(err);
  } finally {
    loading.value = false;
  }
}

watch(wsEvents, (list) => {
  if (!liveMode.value || offset.value > 0 || list.length === 0) return;
  const latest = list[0];
  if (!latest) return;
  if (statusFilter.value === "errors" && latest.ok) return;
  if (statusFilter.value === "ok" && !latest.ok) return;
  if (statusFilter.value === "grounding_blocked" && !latest.grounding_blocked) return;
  if (modeFilter.value !== "all" && latest.mode !== modeFilter.value) return;
  if (runs.value.some((r) => r.run_id === latest.run_id)) return;
  const row: AuditRun = {
    run_id: latest.run_id,
    created_at: latest.created_at,
    mode: latest.mode,
    ok: latest.ok,
    duration_ms: latest.duration_ms,
    tenant_id: "",
    user_sub: latest.user_sub,
    subcall_count: 0,
    raw_upstream_bytes: 0,
    returned_bytes: 0,
    estimated_tokens_saved: latest.tokens_saved,
    schema_tokens_saved: 0,
    abac_rows_filtered: 0,
    redactions_count: 0,
    mode_decision_reason: latest.mode_decision_reason,
    provenance: { grounded: !latest.grounding_blocked },
  };
  const pageCap = Number.parseInt(limitFilter.value || "25", 10) || 25;
  runs.value = [row, ...runs.value].slice(0, pageCap);
  totalCount.value += 1;
});

watch([searchQuery, tenantFilter], () => {
  if (searchDebounce) clearTimeout(searchDebounce);
  searchDebounce = setTimeout(() => {
    void fetchRuns(true);
  }, 300);
});

function setStatusFilter(val: "all" | "errors" | "grounding_blocked" | "ok") {
  statusFilter.value = val;
  void fetchRuns(true);
}

function prevPage() {
  const step = Number.parseInt(limitFilter.value || "25", 10) || 25;
  offset.value = Math.max(0, offset.value - step);
  void fetchRuns();
}

function nextPage() {
  const step = Number.parseInt(limitFilter.value || "25", 10) || 25;
  if (offset.value + step < totalCount.value) {
    offset.value += step;
    void fetchRuns();
  }
}

async function inspectRun(runId: string) {
  errorMessage.value = "";
  try {
    selectedRun.value = await apiFetch<AuditRun>(`/audit/runs/${encodeURIComponent(runId)}`);
    drawerOpen.value = true;
  } catch (err) {
    errorMessage.value = extractErrorMessage(err);
  }
}

async function confirmPrune() {
  errorMessage.value = "";
  statusMessage.value = "";
  const days = Math.max(0, Number.parseInt(retentionDays.value || "30", 10) || 30);
  try {
    const res = await apiFetch<{ deleted_runs: number; retention_days: number }>(
      `/admin/audit/prune?retention_days=${days}`,
      { method: "POST" }
    );
    statusMessage.value = `Pruned ${res.deleted_runs} audit runs older than ${res.retention_days} days.`;
    pruneDialogOpen.value = false;
    await fetchRuns();
  } catch (err) {
    errorMessage.value = extractErrorMessage(err);
  }
}

const route = useRoute();

onMounted(() => {
  const qStatus = String(route.query.status || "");
  if (qStatus === "errors" || qStatus === "grounding_blocked" || qStatus === "ok") {
    statusFilter.value = qStatus;
  }
  void fetchRuns();
});

function spanBarStyle(span: { start_ms: number; duration_ms: number }, totalMs: number) {
  const denom = Math.max(totalMs, 0.5);
  const left = Math.min(96, Math.max(0, (span.start_ms / denom) * 100));
  const width = Math.max(2, Math.min(100 - left, (span.duration_ms / denom) * 100));
  return { left: `${left.toFixed(1)}%`, width: `${width.toFixed(1)}%` };
}

function spanBarColor(stage: string, status: string) {
  if (status === "blocked") return "bg-[#D97706]";
  if (status === "error") return "bg-[#DC2626]";
  if (stage === "monty_spawn" || stage === "monty_exec") return "bg-[#0F766E]";
  if (stage === "upstream_subcall") return "bg-[#2563EB]";
  return "bg-[#71717A]";
}
</script>

<template>
  <div class="space-y-6">
    <div class="flex flex-col gap-3 sm:flex-row sm:items-center sm:justify-between">
      <div>
        <h1 class="text-[20px] font-semibold tracking-[-0.015em] text-[#09090B]">
          Audit Runs, Provenance &amp; Retention
        </h1>
        <p class="mt-0.5 text-[13px] text-[#52525B]">
          Inspect Monty scripts, subcall provenance signatures, ABAC filtering, and database retention pruning.
        </p>
      </div>
      <div class="flex flex-wrap items-center gap-2">
        <label class="flex items-center gap-2 rounded-md border border-[#E4E4E7] bg-white px-2.5 py-1.5 text-xs font-medium text-[#09090B] cursor-pointer">
          <Switch v-model:checked="liveMode">
            <SwitchThumb />
          </Switch>
          <span>Real-Time</span>
        </label>
        <Button pending-announcement="Refreshing runs" @click="fetchRuns(false)">
          <AppIcon name="refresh" :size="15" />
          <span>Refresh</span>
        </Button>
        <Button data-variant="danger" @click="pruneDialogOpen = true">
          <AppIcon name="trash" :size="15" />
          <span>Prune Old Runs</span>
        </Button>
      </div>
    </div>

    <!-- Search, Status Chips & Pagination Controls -->
    <section class="rounded-lg border border-[#E4E4E7] bg-white p-4 space-y-3">
      <div class="flex flex-wrap items-center gap-2">
        <span class="text-xs font-medium text-[#52525B]">Status:</span>
        <Button
          v-for="s in [
            { value: 'all', label: 'All Runs' },
            { value: 'errors', label: 'Errors Only' },
            { value: 'grounding_blocked', label: 'Grounding Blocked' },
            { value: 'ok', label: 'Succeeded' },
          ] as const"
          :key="s.value"
          :data-variant="statusFilter === s.value ? 'primary' : undefined"
          class="px-2.5 py-1 text-xs"
          @click="setStatusFilter(s.value)"
        >
          {{ s.label }}
        </Button>
      </div>

      <div class="grid grid-cols-1 gap-3 sm:grid-cols-3">
        <Field>
          <FieldLabel>Search Run / User / Error</FieldLabel>
          <Input v-model="searchQuery" placeholder="run_id, user, or error text..." />
        </Field>

        <Field>
          <FieldLabel>Page Size</FieldLabel>
          <Select
            :items="limitOptions"
            :value="limitFilter"
            @value-change="(v) => { if (v) { limitFilter = String(v); void fetchRuns(true); } }"
          >
            <SelectTrigger>
              <SelectValue />
              <AppIcon name="chevron-down" :size="14" />
            </SelectTrigger>
            <SelectContent>
              <SelectItem v-for="opt in limitOptions" :key="opt.value" :value="opt.value">
                {{ opt.label }}
                <SelectItemIndicator><AppIcon name="check" :size="14" /></SelectItemIndicator>
              </SelectItem>
            </SelectContent>
          </Select>
        </Field>

        <Field>
          <FieldLabel>Tenant Scope (Admin override)</FieldLabel>
          <Input v-model="tenantFilter" placeholder="Leave blank for default scope" />
        </Field>
      </div>
      <div class="rounded-md border border-[#E4E4E7] bg-[#FAFAFA] px-3 py-2 text-xs text-[#52525B]">
        <span class="font-medium text-[#09090B]">Detection Guide:</span>
        <strong class="text-[#DC2626]">Errors Only</strong> shows failed upstream calls, RBAC/ABAC policy denials, or Monty syntax/runtime errors (<code class="font-mono-data">ok = false</code>).
        <strong class="text-[#92400E]">Grounding Blocked</strong> isolates Code Mode runs where output verification caught ungrounded numbers/IDs that did not originate from any upstream subcall (<code class="font-mono-data">grounding_blocked = true</code>).
      </div>
    </section>

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

    <section v-else class="rounded-lg border border-[#E4E4E7] bg-white">
      <div class="border-b border-[#E4E4E7] px-4 py-3">
        <h2 class="text-[15px] font-semibold text-[#09090B]">
          Execution Log ({{ runs.length }})
        </h2>
      </div>

      <div v-if="runs.length === 0" class="p-8 text-center text-[13px] text-[#71717A]">
        No audit runs recorded in this tenant scope yet.
      </div>

      <div v-else class="overflow-x-auto table-scroll">
        <table class="w-full border-collapse text-left text-[13px]">
          <thead>
            <tr class="border-b border-[#E4E4E7] bg-[#F4F4F5] text-xs font-medium text-[#52525B]">
              <th class="px-4 py-2.5 whitespace-nowrap">Run ID</th>
              <th class="px-4 py-2.5 whitespace-nowrap">Mode</th>
              <th class="px-4 py-2.5 whitespace-nowrap">Caller / Groups</th>
              <th class="px-4 py-2.5 whitespace-nowrap">Status</th>
              <th class="px-4 py-2.5 whitespace-nowrap text-right">Subcalls</th>
              <th class="px-4 py-2.5 whitespace-nowrap text-right">Tokens Saved</th>
              <th class="px-4 py-2.5 whitespace-nowrap text-right">Duration</th>
              <th class="px-4 py-2.5 whitespace-nowrap text-right">Details</th>
            </tr>
          </thead>
          <tbody class="divide-y divide-[#E4E4E7]">
            <tr v-for="r in runs" :key="r.run_id" class="hover:bg-[#FAFAFA]">
              <td class="px-4 py-2.5 font-mono-data text-xs text-[#09090B] whitespace-nowrap">
                {{ r.run_id.slice(0, 12) }}
              </td>
              <td class="px-4 py-2.5 font-mono-data text-xs whitespace-nowrap" :class="r.mode.startsWith('codemode') ? 'text-[#0F766E]' : 'text-[#2563EB]'">
                {{ r.mode }}
              </td>
              <td class="px-4 py-2.5 text-xs whitespace-nowrap">
                <div class="text-[#09090B]">{{ r.user_email || r.user_sub }}</div>
                <div class="text-[11px] text-[#71717A]">{{ (r.user_groups || []).join(", ") || "—" }}</div>
              </td>
              <td class="px-4 py-2.5 whitespace-nowrap">
                <span
                  class="rounded border px-2 py-0.5 text-xs font-medium"
                  :class="
                    r.grounding_blocked
                      ? 'border-[#FDE68A] bg-[#FFFBEB] text-[#92400E]'
                      : r.ok
                        ? 'border-[#A7F3D0] bg-[#ECFDF5] text-[#059669]'
                        : 'border-[#FECACA] bg-[#FEF2F2] text-[#DC2626]'
                  "
                >
                  {{ r.grounding_blocked ? "grounding blocked" : r.ok ? "ok" : "error" }}
                </span>
              </td>
              <td class="px-4 py-2.5 text-right tabular-nums whitespace-nowrap">{{ r.subcall_count }}</td>
              <td class="px-4 py-2.5 text-right tabular-nums text-[#0F766E] whitespace-nowrap">
                {{ (r.estimated_tokens_saved + r.schema_tokens_saved).toLocaleString("en-US") }}
              </td>
              <td class="px-4 py-2.5 text-right tabular-nums whitespace-nowrap">{{ r.duration_ms.toFixed(1) }} ms</td>
              <td class="px-4 py-2.5 text-right whitespace-nowrap">
                <Button @click="inspectRun(r.run_id)">Inspect</Button>
              </td>
            </tr>
          </tbody>
        </table>
      </div>
      <div class="flex flex-wrap items-center justify-between gap-2 border-t border-[#E4E4E7] bg-[#FAFAFA] px-4 py-2.5 text-xs text-[#52525B]">
        <span class="tabular-nums">
          Showing {{ runs.length ? offset + 1 : 0 }}–{{ offset + runs.length }} of {{ totalCount }} total runs
        </span>
        <div class="flex items-center gap-2">
          <Button class="px-2.5 py-1 text-xs" :disabled="offset === 0" @click="prevPage">
            Previous
          </Button>
          <Button
            class="px-2.5 py-1 text-xs"
            :disabled="offset + runs.length >= totalCount"
            @click="nextPage"
          >
            Next
          </Button>
        </div>
      </div>
      <div class="rounded-md border border-[#E4E4E7] bg-[#FAFAFA] px-3 py-2 text-xs text-[#52525B]">
        <span class="font-medium text-[#09090B]">Detection Guide:</span>
        <strong class="text-[#DC2626]">Errors Only</strong> shows failed upstream calls, RBAC/ABAC policy denials, or Monty syntax/runtime errors (<code class="font-mono-data">ok = false</code>).
        <strong class="text-[#92400E]">Grounding Blocked</strong> isolates Code Mode runs where output verification caught ungrounded numbers/IDs that did not originate from any upstream subcall (<code class="font-mono-data">grounding_blocked = true</code>).
      </div>
    </section>

    <!-- Slide-over Audit Inspector Drawer -->
    <Drawer v-model:open="drawerOpen" side="right">
      <DrawerBackdrop />
      <DrawerContent class="w-full max-w-[95vw] sm:w-[36rem] p-0">
        <div class="flex items-center justify-between border-b border-[#E4E4E7] bg-[#FAFAFA] px-5 py-4">
          <div class="min-w-0">
            <div class="flex items-center gap-2">
              <DrawerTitle class="font-mono-data text-sm font-semibold text-[#09090B]">
                {{ selectedRun?.run_id.slice(0, 16) }}
              </DrawerTitle>
              <span
                v-if="selectedRun"
                class="rounded border px-2 py-0.5 text-[11px] font-medium"
                :class="
                  selectedRun.grounding_blocked
                    ? 'border-[#FDE68A] bg-[#FFFBEB] text-[#92400E]'
                    : selectedRun.ok
                      ? 'border-[#A7F3D0] bg-[#ECFDF5] text-[#059669]'
                      : 'border-[#FECACA] bg-[#FEF2F2] text-[#DC2626]'
                "
              >
                {{ selectedRun.grounding_blocked ? "Grounding Blocked" : selectedRun.ok ? "Succeeded" : "Error" }}
              </span>
            </div>
            <DrawerDescription class="mt-0.5 truncate text-xs text-[#52525B]">
              {{ selectedRun?.mode }} · {{ selectedRun?.user_email || selectedRun?.user_sub }}
            </DrawerDescription>
          </div>
          <DrawerClose aria-label="Close inspector" class="px-2">
            <AppIcon name="x" :size="16" />
          </DrawerClose>
        </div>

        <div v-if="selectedRun" class="min-h-0 flex-1 space-y-4 overflow-y-auto p-5 text-xs">
          <!-- KPI Strip -->
          <div class="grid grid-cols-2 gap-2 sm:grid-cols-4">
            <div class="rounded-md border border-[#E4E4E7] bg-white p-2.5">
              <div class="text-[11px] text-[#71717A]">Duration</div>
              <div class="mt-0.5 text-sm font-semibold text-[#09090B] tabular-nums">
                {{ selectedRun.duration_ms.toFixed(1) }} ms
              </div>
            </div>
            <div class="rounded-md border border-[#E4E4E7] bg-white p-2.5">
              <div class="text-[11px] text-[#71717A]">Tokens Used</div>
              <div class="mt-0.5 text-sm font-semibold text-[#09090B] tabular-nums">
                {{ (selectedRun.tokens_used_est ?? 0).toLocaleString("en-US") }}
              </div>
            </div>
            <div class="rounded-md border border-[#E4E4E7] bg-white p-2.5">
              <div class="text-[11px] text-[#71717A]">Tokens Saved</div>
              <div class="mt-0.5 text-sm font-semibold text-[#0F766E] tabular-nums">
                {{ (selectedRun.estimated_tokens_saved + selectedRun.schema_tokens_saved).toLocaleString("en-US") }}
              </div>
            </div>
            <div class="rounded-md border border-[#E4E4E7] bg-white p-2.5">
              <div class="text-[11px] text-[#71717A]">Subcalls</div>
              <div class="mt-0.5 text-sm font-semibold text-[#09090B] tabular-nums">
                {{ selectedRun.subcall_count }}
              </div>
            </div>
          </div>

          <div
            v-if="selectedRun.tokens_saved_by"
            class="rounded-md border border-[#99F6E4] bg-[#F0FDFA] px-3 py-2 text-[11px] text-[#0F766E]"
          >
            <strong>Savings Attribution:</strong> {{ selectedRun.tokens_saved_by }}
            <span v-if="selectedRun.monty_spawn_ms || selectedRun.monty_exec_ms" class="ml-2 text-[#52525B] tabular-nums">
              · Monty spawn {{ (selectedRun.monty_spawn_ms ?? 0).toFixed(2) }}ms · exec {{ (selectedRun.monty_exec_ms ?? 0).toFixed(2) }}ms
            </span>
          </div>

          <!-- Linear-style Waterfall Trace Timeline -->
          <div v-if="selectedRun.trace_spans?.length" class="space-y-2 rounded-md border border-[#E4E4E7] bg-white p-3">
            <div class="flex items-center justify-between">
              <div class="font-semibold text-[#09090B]">Request Trace Timeline</div>
              <div class="text-[11px] text-[#71717A] tabular-nums">Total {{ selectedRun.duration_ms.toFixed(1) }} ms</div>
            </div>
            <div class="space-y-1.5">
              <div
                v-for="(sp, idx) in selectedRun.trace_spans"
                :key="idx"
                class="grid grid-cols-[11rem_1fr_4.5rem] items-center gap-2 text-[11px]"
              >
                <div class="truncate font-medium text-[#09090B]" :title="sp.label">{{ sp.label }}</div>
                <div class="relative h-3 rounded bg-[#F4F4F5] overflow-hidden">
                  <div
                    class="absolute top-0 bottom-0 rounded"
                    :class="spanBarColor(sp.stage, sp.status)"
                    :style="spanBarStyle(sp, selectedRun.duration_ms)"
                  />
                </div>
                <div class="text-right font-mono-data text-[#52525B] tabular-nums">
                  {{ sp.duration_ms.toFixed(2) }} ms
                </div>
              </div>
            </div>
          </div>

          <!-- Metadata & Provenance Grid -->
          <div class="grid grid-cols-2 gap-3 rounded-md border border-[#E4E4E7] bg-[#FAFAFA] p-3">
            <div>
              <div class="text-[#71717A]">Grounded Provenance</div>
              <div class="mt-0.5 font-semibold" :class="selectedRun.provenance?.grounded ? 'text-[#059669]' : 'text-[#52525B]'">
                {{ selectedRun.provenance?.grounded ? "Verified HMAC" : "N/A / Unverified" }}
              </div>
            </div>
            <div>
              <div class="text-[#71717A]">Decision Attribution</div>
              <div class="mt-0.5 text-[#09090B]">
                {{ selectedRun.mode_decision_reason || "explicit" }}
              </div>
            </div>
            <div>
              <div class="text-[#71717A]">Groups</div>
              <div class="mt-0.5 font-mono-data text-[#09090B]">
                {{ (selectedRun.user_groups || []).join(", ") || "—" }}
              </div>
            </div>
            <div>
              <div class="text-[#71717A]">Security Filters</div>
              <div class="mt-0.5 text-[#09090B] tabular-nums">
                ABAC: {{ selectedRun.abac_rows_filtered }} · PII: {{ selectedRun.pii_redactions }}
              </div>
            </div>
          </div>

          <div v-if="selectedRun.error" class="space-y-1.5">
            <div class="font-semibold" :class="selectedRun.grounding_blocked ? 'text-[#92400E]' : 'text-[#DC2626]'">
              {{ selectedRun.grounding_blocked ? "Grounding Violation Blocked" : "Execution Error" }}
            </div>
            <pre
              class="overflow-x-auto rounded border p-3 font-mono-data text-xs whitespace-pre-wrap"
              :class="
                selectedRun.grounding_blocked
                  ? 'border-[#FDE68A] bg-[#FFFBEB] text-[#92400E]'
                  : 'border-[#FECACA] bg-[#FEF2F2] text-[#DC2626]'
              "
            >{{ selectedRun.error }}</pre>
          </div>

          <div v-if="selectedRun.code" class="space-y-1.5">
            <div class="font-semibold text-[#09090B]">Executed Monty Python</div>
            <pre class="overflow-x-auto rounded border border-[#E4E4E7] bg-[#FAFAFA] p-3 font-mono-data text-xs text-[#09090B]">{{ selectedRun.code }}</pre>
          </div>

          <div v-if="selectedRun.subcalls?.length" class="space-y-1.5">
            <div class="font-semibold text-[#09090B]">Upstream Subcalls ({{ selectedRun.subcalls.length }})</div>
            <div class="overflow-x-auto rounded border border-[#E4E4E7]">
              <table class="w-full border-collapse text-left text-xs">
                <thead>
                  <tr class="border-b border-[#E4E4E7] bg-[#F4F4F5] text-[#52525B]">
                    <th class="px-2.5 py-1.5">Server / Tool</th>
                    <th class="px-2.5 py-1.5 text-right">Bytes</th>
                    <th class="px-2.5 py-1.5 text-right">ms</th>
                  </tr>
                </thead>
                <tbody class="divide-y divide-[#E4E4E7]">
                  <tr v-for="(sc, idx) in selectedRun.subcalls" :key="idx">
                    <td class="px-2.5 py-1.5 font-mono-data">{{ sc.server }}.{{ sc.tool }}</td>
                    <td class="px-2.5 py-1.5 text-right tabular-nums">{{ sc.raw_bytes }}</td>
                    <td class="px-2.5 py-1.5 text-right tabular-nums">{{ sc.duration_ms.toFixed(1) }}</td>
                  </tr>
                </tbody>
              </table>
            </div>
          </div>

          <details class="rounded-md border border-[#E4E4E7] bg-[#FAFAFA] p-3">
            <summary class="cursor-pointer font-medium text-[#09090B]">Raw Audit JSON</summary>
            <pre class="mt-2 max-h-64 overflow-auto rounded border border-[#E4E4E7] bg-white p-2.5 font-mono-data text-[11px] text-[#09090B]">{{ JSON.stringify(selectedRun, null, 2) }}</pre>
          </details>
        </div>
      </DrawerContent>
    </Drawer>

    <!-- Destructive Retention Prune Confirmation Dialog -->
    <Dialog v-model:open="pruneDialogOpen">
      <DialogBackdrop />
      <DialogContent>
        <DialogTitle>Prune aged audit runs?</DialogTitle>
        <DialogDescription>
          Permanently deletes execution telemetry and subcall records older than the specified retention window.
        </DialogDescription>
        <Field>
          <FieldLabel>Retention Window (days)</FieldLabel>
          <Input v-model="retentionDays" inputmode="numeric" />
          <FieldDescription>Use 0 to delete all recorded runs immediately.</FieldDescription>
        </Field>
        <div class="flex justify-end gap-2 pt-2">
          <DialogClose>Cancel</DialogClose>
          <Button
            data-variant="danger"
            data-agent-confirm="Permanently delete aged audit runs from storage?"
            pending-announcement="Pruning runs"
            @click="confirmPrune"
          >
            Confirm Prune
          </Button>
        </div>
      </DialogContent>
    </Dialog>
  </div>
</template>
