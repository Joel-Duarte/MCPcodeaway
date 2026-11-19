<script setup lang="ts">
import { extractErrorMessage } from "~/composables/useAdminApi";

interface ModelPricingMap {
  models: Record<string, { input: number; output: number }>;
  custom_models: string[];
  defaults: string[];
  billing_note: string;
}

interface AlertRules {
  error_rate_pct_max: number;
  p95_latency_ms_max: number;
  grounding_blocks_max: number;
  webhook_url: string | null;
  webhook_cooldown_seconds: number;
}

const { apiFetch } = useAdminApi();

const loading = ref(true);
const statusMsg = ref("");
const errorMsg = ref("");

// Custom Model Pricing
const pricing = ref<ModelPricingMap | null>(null);
const customModelName = ref("custom-local-9b");
const customInPrice = ref("0.20");
const customOutPrice = ref("0.60");
const savingPricing = ref(false);

// Alert Rules
const errorRateMax = ref("15.0");
const p95LatencyMax = ref("1500");
const groundingBlocksMax = ref("5");
const webhookUrl = ref("");
const savingAlerts = ref(false);

// Audit Retention
const retentionDays = ref("90");
const pruning = ref(false);

async function loadSettings() {
  errorMsg.value = "";
  try {
    const [p, r] = await Promise.all([
      apiFetch<ModelPricingMap>("/admin/pricing/models"),
      apiFetch<AlertRules>("/admin/alerts/rules"),
    ]);
    pricing.value = p;
    errorRateMax.value = String(r.error_rate_pct_max);
    p95LatencyMax.value = String(r.p95_latency_ms_max);
    groundingBlocksMax.value = String(r.grounding_blocks_max);
    webhookUrl.value = r.webhook_url || "";
  } catch (err) {
    errorMsg.value = extractErrorMessage(err);
  } finally {
    loading.value = false;
  }
}

onMounted(() => {
  void loadSettings();
});

async function saveModelPrice() {
  if (!customModelName.value.trim()) return;
  savingPricing.value = true;
  errorMsg.value = "";
  statusMsg.value = "";
  try {
    await apiFetch("/admin/pricing/models", {
      method: "POST",
      body: {
        model_name: customModelName.value.trim(),
        input_per_1m_usd: Number(customInPrice.value) || 0,
        output_per_1m_usd: Number(customOutPrice.value) || 0,
      },
    });
    statusMsg.value = `Saved custom model pricing for '${customModelName.value.trim()}'.`;
    await loadSettings();
  } catch (err) {
    errorMsg.value = extractErrorMessage(err);
  } finally {
    savingPricing.value = false;
  }
}

async function deleteModelPrice(name: string) {
  errorMsg.value = "";
  statusMsg.value = "";
  try {
    await apiFetch(`/admin/pricing/models/${encodeURIComponent(name)}`, { method: "DELETE" });
    statusMsg.value = `Removed custom model '${name}'.`;
    await loadSettings();
  } catch (err) {
    errorMsg.value = extractErrorMessage(err);
  }
}

async function saveAlertRules() {
  savingAlerts.value = true;
  errorMsg.value = "";
  statusMsg.value = "";
  try {
    await apiFetch("/admin/alerts/rules", {
      method: "POST",
      body: {
        error_rate_pct_max: Number(errorRateMax.value) || 15.0,
        p95_latency_ms_max: Number(p95LatencyMax.value) || 1500.0,
        grounding_blocks_max: Number(groundingBlocksMax.value) || 5,
        webhook_url: webhookUrl.value.trim() || null,
      },
    });
    statusMsg.value = "Updated SLA & security alert rules.";
  } catch (err) {
    errorMsg.value = extractErrorMessage(err);
  } finally {
    savingAlerts.value = false;
  }
}

async function triggerPrune() {
  pruning.value = true;
  errorMsg.value = "";
  statusMsg.value = "";
  try {
    const days = Math.max(0, Number.parseInt(retentionDays.value || "90", 10) || 0);
    const res = await apiFetch<{ deleted_runs: number }>(`/admin/audit/prune?retention_days=${days}`, {
      method: "POST",
    });
    statusMsg.value = `Pruned ${res.deleted_runs} audit run(s) older than ${days} day(s).`;
  } catch (err) {
    errorMsg.value = extractErrorMessage(err);
  } finally {
    pruning.value = false;
  }
}
</script>

<template>
  <div class="space-y-6">
    <div>
      <h1 class="text-[20px] font-semibold tracking-[-0.015em] text-[#09090B]">
        Gateway Settings, Model Pricing &amp; Alerts
      </h1>
      <p class="mt-0.5 text-[13px] text-[#52525B]">
        Configure per-token API cost baselines, real-time SLA/security alert thresholds, and audit retention.
      </p>
    </div>

    <div
      v-if="statusMsg"
      role="status"
      class="rounded-lg border border-[#99F6E4] bg-[#F0FDFA] px-4 py-2.5 text-[13px] text-[#0F766E]"
    >
      {{ statusMsg }}
    </div>
    <div
      v-if="errorMsg"
      role="alert"
      class="flex items-start gap-2 rounded-lg border border-[#FECACA] bg-[#FEF2F2] px-4 py-3 text-[13px] text-[#DC2626]"
    >
      <AppIcon name="alert" :size="16" class="mt-0.5" />
      <span>{{ errorMsg }}</span>
    </div>

    <!-- Custom Model API Token Pricing -->
    <section class="rounded-lg border border-[#E4E4E7] bg-white p-5 space-y-4">
      <div class="border-b border-[#E4E4E7] pb-3">
        <h2 class="text-[15px] font-semibold text-[#09090B]">API Token Cost Catalog</h2>
        <p class="text-xs text-[#52525B]">
          {{ pricing?.billing_note || "Models per-token API pricing ($/1M input & output tokens); client subscription plans are not exposed by MCP." }}
        </p>
      </div>

      <div v-if="pricing" class="overflow-x-auto rounded border border-[#E4E4E7]">
        <table class="w-full border-collapse text-left text-xs">
          <thead>
            <tr class="border-b border-[#E4E4E7] bg-[#F4F4F5] text-[#52525B]">
              <th class="px-3 py-2">Model</th>
              <th class="px-3 py-2 text-right">Input ($ / 1M)</th>
              <th class="px-3 py-2 text-right">Output ($ / 1M)</th>
              <th class="px-3 py-2">Source</th>
              <th class="px-3 py-2 text-right">Action</th>
            </tr>
          </thead>
          <tbody class="divide-y divide-[#E4E4E7]">
            <tr v-for="(rates, name) in pricing.models" :key="name">
              <td class="px-3 py-2 font-mono-data font-medium text-[#09090B]">{{ name }}</td>
              <td class="px-3 py-2 text-right tabular-nums">${{ Number(rates.input).toFixed(2) }}</td>
              <td class="px-3 py-2 text-right tabular-nums">${{ Number(rates.output).toFixed(2) }}</td>
              <td class="px-3 py-2">
                <span
                  class="rounded px-1.5 py-0.5 text-[11px] font-medium"
                  :class="pricing.custom_models.includes(String(name)) ? 'bg-[#F0FDFA] text-[#0F766E]' : 'bg-[#F4F4F5] text-[#52525B]'"
                >
                  {{ pricing.custom_models.includes(String(name)) ? "Custom" : "Default" }}
                </span>
              </td>
              <td class="px-3 py-2 text-right">
                <Button
                  v-if="pricing.custom_models.includes(String(name))"
                  data-variant="danger"
                  class="px-2 py-0.5 text-[11px]"
                  @click="deleteModelPrice(String(name))"
                >
                  Remove
                </Button>
                <span v-else class="text-[#71717A]">—</span>
              </td>
            </tr>
          </tbody>
        </table>
      </div>

      <div class="grid grid-cols-1 gap-3 border-t border-[#E4E4E7] pt-4 sm:grid-cols-4 sm:items-end">
        <Field>
          <FieldLabel>Custom Model ID</FieldLabel>
          <Input v-model="customModelName" class="font-mono-data text-xs" placeholder="e.g. llama-4-maverick" />
        </Field>
        <Field>
          <FieldLabel>Input $/1M Tokens</FieldLabel>
          <Input v-model="customInPrice" inputmode="decimal" class="tabular-nums text-xs" />
        </Field>
        <Field>
          <FieldLabel>Output $/1M Tokens</FieldLabel>
          <Input v-model="customOutPrice" inputmode="decimal" class="tabular-nums text-xs" />
        </Field>
        <Button data-variant="primary" :disabled="savingPricing" @click="saveModelPrice">
          Save Custom Model Price
        </Button>
      </div>
    </section>

    <!-- Monitoring & Alert Thresholds -->
    <section class="rounded-lg border border-[#E4E4E7] bg-white p-5 space-y-4">
      <div class="border-b border-[#E4E4E7] pb-3">
        <h2 class="text-[15px] font-semibold text-[#09090B]">Monitoring &amp; Alert Rules</h2>
        <p class="text-xs text-[#52525B]">
          Evaluated live across the last 50 executions, schema drift, and sandbox worker health (paired with Prometheus Alertmanager rules).
        </p>
      </div>
      <div class="grid grid-cols-1 gap-4 sm:grid-cols-2 lg:grid-cols-4">
        <Field>
          <FieldLabel>Max Error Rate (%)</FieldLabel>
          <Input v-model="errorRateMax" inputmode="decimal" class="tabular-nums text-xs" />
        </Field>
        <Field>
          <FieldLabel>Max p95 Latency (ms)</FieldLabel>
          <Input v-model="p95LatencyMax" inputmode="numeric" class="tabular-nums text-xs" />
        </Field>
        <Field>
          <FieldLabel>Max Grounding Blocks</FieldLabel>
          <Input v-model="groundingBlocksMax" inputmode="numeric" class="tabular-nums text-xs" />
        </Field>
        <Field>
          <FieldLabel>Alert Webhook URL (optional)</FieldLabel>
          <Input v-model="webhookUrl" class="font-mono-data text-xs" placeholder="https://hooks.slack.com/..." />
        </Field>
      </div>
      <div class="flex justify-end">
        <Button data-variant="primary" :disabled="savingAlerts" @click="saveAlertRules">
          Save Alert Rules
        </Button>
      </div>
    </section>

    <!-- Audit Retention -->
    <section class="rounded-lg border border-[#E4E4E7] bg-white p-5 space-y-4">
      <div class="border-b border-[#E4E4E7] pb-3">
        <h2 class="text-[15px] font-semibold text-[#09090B]">Audit Retention Policy</h2>
        <p class="text-xs text-[#52525B]">
          Prune aged execution logs from SQLite/Postgres storage cleanly.
        </p>
      </div>
      <div class="flex flex-wrap items-end gap-3">
        <Field class="w-48">
          <FieldLabel>Retention Window (days)</FieldLabel>
          <Input v-model="retentionDays" inputmode="numeric" class="tabular-nums text-xs" />
        </Field>
        <Button
          data-variant="danger"
          data-agent-confirm="Permanently delete aged audit runs from storage?"
          :disabled="pruning"
          @click="triggerPrune"
        >
          Prune Aged Runs
        </Button>
      </div>
    </section>
  </div>
</template>
