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

const loading = ref(true);
const errorMessage = ref("");
const statusMessage = ref("");
const limitFilter = ref("50");
const tenantFilter = ref("");
const runs = ref<AuditRun[]>([]);

const drawerOpen = ref(false);
const selectedRun = ref<AuditRun | null>(null);

const pruneDialogOpen = ref(false);
const retentionDays = ref("30");

const limitOptions = [
  { value: "25", label: "Last 25 runs" },
  { value: "50", label: "Last 50 runs" },
  { value: "100", label: "Last 100 runs" },
  { value: "200", label: "Last 200 runs" },
];

async function fetchRuns() {
  errorMessage.value = "";
  const params = new URLSearchParams();
  params.set("limit", limitFilter.value || "50");
  if (tenantFilter.value.trim()) {
    params.set("tenant_id", tenantFilter.value.trim());
  }
  try {
    const res = await apiFetch<{ count: number; runs: AuditRun[] }>(`/audit/runs?${params.toString()}`);
    runs.value = res.runs;
  } catch (err) {
    errorMessage.value = extractErrorMessage(err);
  } finally {
    loading.value = false;
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

onMounted(() => {
  void fetchRuns();
});
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
        <Button pending-announcement="Refreshing runs" @click="fetchRuns">
          <AppIcon name="refresh" :size="15" />
          <span>Refresh</span>
        </Button>
        <Button data-variant="danger" @click="pruneDialogOpen = true">
          <AppIcon name="trash" :size="15" />
          <span>Prune Old Runs</span>
        </Button>
      </div>
    </div>

    <!-- Filters -->
    <section class="rounded-lg border border-[#E4E4E7] bg-white p-4">
      <div class="grid grid-cols-1 gap-3 sm:grid-cols-2">
        <Field>
          <FieldLabel>Window Size</FieldLabel>
          <Select
            :items="limitOptions"
            :value="limitFilter"
            @value-change="(v) => { if (v) { limitFilter = String(v); void fetchRuns(); } }"
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
                    r.ok
                      ? 'border-[#A7F3D0] bg-[#ECFDF5] text-[#059669]'
                      : 'border-[#FECACA] bg-[#FEF2F2] text-[#DC2626]'
                  "
                >
                  {{ r.ok ? "ok" : "error" }}
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
    </section>

    <!-- Slide-over Audit Inspector Drawer -->
    <Drawer v-model:open="drawerOpen" side="right">
      <DrawerBackdrop />
      <DrawerContent class="w-full max-w-[95vw] sm:w-[32rem] p-0">
        <div class="flex items-center justify-between border-b border-[#E4E4E7] px-5 py-4">
          <div>
            <DrawerTitle class="text-sm font-semibold text-[#09090B]">
              Run {{ selectedRun?.run_id.slice(0, 12) }}
            </DrawerTitle>
            <DrawerDescription class="text-xs text-[#52525B]">
              {{ selectedRun?.mode }} · {{ selectedRun?.user_email || selectedRun?.user_sub }}
            </DrawerDescription>
          </div>
          <DrawerClose aria-label="Close inspector" class="px-2">
            <AppIcon name="x" :size="16" />
          </DrawerClose>
        </div>

        <div v-if="selectedRun" class="min-h-0 flex-1 space-y-4 overflow-y-auto p-5 text-xs">
          <div class="grid grid-cols-2 gap-3 rounded-md border border-[#E4E4E7] bg-[#F4F4F5] p-3">
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
          </div>

          <div v-if="selectedRun.code" class="space-y-1.5">
            <div class="font-semibold text-[#09090B]">Executed Monty Python</div>
            <pre class="overflow-x-auto rounded border border-[#E4E4E7] bg-[#FAFAFA] p-3 font-mono-data text-xs text-[#09090B]">{{ selectedRun.code }}</pre>
          </div>

          <div v-if="selectedRun.error" class="space-y-1.5">
            <div class="font-semibold text-[#DC2626]">Error</div>
            <pre class="overflow-x-auto rounded border border-[#FECACA] bg-[#FEF2F2] p-3 font-mono-data text-xs text-[#DC2626]">{{ selectedRun.error }}</pre>
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
          <Button data-variant="danger" pending-announcement="Pruning runs" @click="confirmPrune">
            Confirm Prune
          </Button>
        </div>
      </DialogContent>
    </Dialog>
  </div>
</template>
