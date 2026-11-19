<script setup lang="ts">
import { extractErrorMessage } from "~/composables/useAdminApi";

interface ModelPricingMap {
  models: Record<string, { input: number; output: number }>;
  custom_models: string[];
  defaults: string[];
  billing_note: string;
}

interface CustomAlertRule {
  id: string;
  name: string;
  metric: "error_rate_pct" | "p95_latency_ms" | "grounding_blocks" | "subcall_errors" | "upstream_down";
  operator: ">" | ">=" | "<" | "<=" | "==";
  threshold: number;
  severity: "warning" | "critical";
  webhook_url?: string | null;
  enabled: boolean;
}

interface AlertRules {
  error_rate_pct_max: number;
  p95_latency_ms_max: number;
  grounding_blocks_max: number;
  webhook_url: string | null;
  webhook_cooldown_seconds: number;
  custom_rules?: CustomAlertRule[];
}

const { apiFetch } = useAdminApi();

const loading = ref(true);
const statusMsg = ref("");
const errorMsg = ref("");

// Model Pricing Catalog
const pricing = ref<ModelPricingMap | null>(null);
const modelSearch = ref("");
const modelPage = ref(1);
const modelPageSize = 8;
const editingModel = ref<string | null>(null);
const editInPrice = ref("");
const editOutPrice = ref("");
const customModelName = ref("");
const customInPrice = ref("0.20");
const customOutPrice = ref("0.60");
const savingPricing = ref(false);

// Alert Rules & Custom Rules
const errorRateMax = ref("15.0");
const p95LatencyMax = ref("1500");
const groundingBlocksMax = ref("5");
const webhookUrl = ref("");
const cooldownSeconds = ref("60");
const customRules = ref<CustomAlertRule[]>([]);
const savingAlerts = ref(false);
const testingWebhook = ref(false);
const webhookTestResult = ref<{ delivered: boolean; https_enforced: boolean; detail: string } | null>(null);

// New Custom Rule Form
const newRuleName = ref("");
const newRuleMetric = ref<CustomAlertRule["metric"]>("error_rate_pct");
const newRuleOp = ref<CustomAlertRule["operator"]>(">=");
const newRuleThreshold = ref("10");
const newRuleSeverity = ref<CustomAlertRule["severity"]>("warning");
const newRuleWebhook = ref("");

// Compliance Retention & S3 / JSONL Archive
const retentionDays = ref("90");
const archiveS3Bucket = ref("");
const archiving = ref(false);
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
    cooldownSeconds.value = String(r.webhook_cooldown_seconds ?? 60);
    customRules.value = r.custom_rules ? [...r.custom_rules] : [];
  } catch (err) {
    errorMsg.value = extractErrorMessage(err);
  } finally {
    loading.value = false;
  }
}

onMounted(() => {
  void loadSettings();
});

const allModelEntries = computed(() => {
  const models = pricing.value?.models || {};
  const q = modelSearch.value.trim().toLowerCase();
  const defaultsSet = new Set(pricing.value?.defaults || []);
  const customSet = new Set(pricing.value?.custom_models || []);
  return Object.entries(models)
    .filter(([name]) => !q || name.toLowerCase().includes(q))
    .map(([name, rates]) => ({
      name,
      input: rates.input,
      output: rates.output,
      isDefault: defaultsSet.has(name),
      isOverridden: defaultsSet.has(name) && customSet.has(name),
      isCustomOnly: !defaultsSet.has(name),
    }));
});

const modelTotalPages = computed(() => Math.max(1, Math.ceil(allModelEntries.value.length / modelPageSize)));
const pagedModels = computed(() => {
  const start = (modelPage.value - 1) * modelPageSize;
  return allModelEntries.value.slice(start, start + modelPageSize);
});

watch(modelSearch, () => {
  modelPage.value = 1;
});

function startEditModel(name: string, input: number, output: number) {
  editingModel.value = name;
  editInPrice.value = String(input);
  editOutPrice.value = String(output);
}

async function saveInlineModel(name: string) {
  savingPricing.value = true;
  errorMsg.value = "";
  statusMsg.value = "";
  try {
    await apiFetch("/admin/pricing/models", {
      method: "POST",
      body: {
        model_name: name,
        input_per_1m_usd: Number(editInPrice.value) || 0,
        output_per_1m_usd: Number(editOutPrice.value) || 0,
      },
    });
    editingModel.value = null;
    statusMsg.value = `Updated API token price for '${name}'.`;
    await loadSettings();
  } catch (err) {
    errorMsg.value = extractErrorMessage(err);
  } finally {
    savingPricing.value = false;
  }
}

async function addModelPrice() {
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
    statusMsg.value = `Saved model pricing for '${customModelName.value.trim()}'.`;
    customModelName.value = "";
    await loadSettings();
  } catch (err) {
    errorMsg.value = extractErrorMessage(err);
  } finally {
    savingPricing.value = false;
  }
}

async function deleteModelPrice(name: string, wasDefault: boolean) {
  errorMsg.value = "";
  statusMsg.value = "";
  try {
    await apiFetch(`/admin/pricing/models/${encodeURIComponent(name)}`, { method: "DELETE" });
    statusMsg.value = wasDefault
      ? `Restored default API pricing for '${name}'.`
      : `Removed custom model '${name}'.`;
    await loadSettings();
  } catch (err) {
    errorMsg.value = extractErrorMessage(err);
  }
}

function addCustomRule() {
  if (!newRuleName.value.trim()) return;
  const id = `rule-${Date.now().toString(36)}`;
  customRules.value.push({
    id,
    name: newRuleName.value.trim(),
    metric: newRuleMetric.value,
    operator: newRuleOp.value,
    threshold: Number(newRuleThreshold.value) || 0,
    severity: newRuleSeverity.value,
    webhook_url: newRuleWebhook.value.trim() || null,
    enabled: true,
  });
  newRuleName.value = "";
  newRuleWebhook.value = "";
}

function removeCustomRule(id: string) {
  customRules.value = customRules.value.filter((r) => r.id !== id);
}

async function saveAlertRules() {
  savingAlerts.value = true;
  errorMsg.value = "";
  statusMsg.value = "";
  try {
    await apiFetch("/admin/alerts/rules", {
      method: "POST",
      body: {
        error_rate_pct_max: Number(errorRateMax.value) || 15,
        p95_latency_ms_max: Number(p95LatencyMax.value) || 1500,
        grounding_blocks_max: Number(groundingBlocksMax.value) || 5,
        webhook_url: webhookUrl.value.trim() || null,
        webhook_cooldown_seconds: Number(cooldownSeconds.value) || 60,
        custom_rules: customRules.value,
      },
    });
    statusMsg.value = "Alert rules and custom webhook policies saved.";
  } catch (err) {
    errorMsg.value = extractErrorMessage(err);
  } finally {
    savingAlerts.value = false;
  }
}

async function testWebhook() {
  if (!webhookUrl.value.trim()) {
    errorMsg.value = "Enter a Webhook URL first before testing.";
    return;
  }
  testingWebhook.value = true;
  errorMsg.value = "";
  webhookTestResult.value = null;
  try {
    const q = new URLSearchParams({ webhook_url: webhookUrl.value.trim() });
    webhookTestResult.value = await apiFetch(`/admin/alerts/test-webhook?${q.toString()}`, {
      method: "POST",
    });
  } catch (err) {
    errorMsg.value = extractErrorMessage(err);
  } finally {
    testingWebhook.value = false;
  }
}

function downloadJsonl(filename: string, content: string) {
  const blob = new Blob([content], { type: "application/x-ndjson" });
  const url = URL.createObjectURL(blob);
  const a = document.createElement("a");
  a.href = url;
  a.download = filename;
  a.click();
  URL.revokeObjectURL(url);
}

async function archiveAuditRuns(pruneAfter: boolean) {
  archiving.value = true;
  errorMsg.value = "";
  statusMsg.value = "";
  try {
    const q = new URLSearchParams({
      retention_days: String(Number(retentionDays.value) || 90),
      prune_after_archive: String(pruneAfter),
    });
    if (archiveS3Bucket.value.trim()) {
      q.set("s3_bucket", archiveS3Bucket.value.trim());
    }
    const res = await apiFetch<{
      exported_runs: number;
      deleted_runs: number;
      retention_days: number;
      filename: string;
      sha256: string;
      s3_uri: string | null;
      jsonl_content: string | null;
    }>(`/admin/audit/archive?${q.toString()}`, { method: "POST" });

    if (res.exported_runs === 0) {
      statusMsg.value = `No runs older than ${res.retention_days} days matched archival.`;
      return;
    }
    if (res.jsonl_content) {
      downloadJsonl(res.filename, res.jsonl_content);
    }
    statusMsg.value = res.s3_uri
      ? `Archived ${res.exported_runs} runs to ${res.s3_uri} (SHA-256 ${res.sha256.slice(0, 12)}). Purged ${res.deleted_runs} from active DB.`
      : `Downloaded ${res.filename} (${res.exported_runs} runs, SHA-256 ${res.sha256.slice(0, 12)}). Purged ${res.deleted_runs} from active DB.`;
  } catch (err) {
    errorMsg.value = extractErrorMessage(err);
  } finally {
    archiving.value = false;
  }
}

async function pruneAuditRuns() {
  pruning.value = true;
  errorMsg.value = "";
  statusMsg.value = "";
  try {
    const days = Number(retentionDays.value) || 90;
    const res = await apiFetch<{ deleted_runs: number; retention_days: number }>(
      `/admin/audit/prune?retention_days=${encodeURIComponent(String(days))}`,
      { method: "POST" }
    );
    statusMsg.value = `Purged ${res.deleted_runs} runs older than ${res.retention_days} days from active DB.`;
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
        Gateway Settings, Alert Rules &amp; Compliance Retention
      </h1>
      <p class="mt-0.5 text-[13px] text-[#52525B]">
        Manage verified API token pricing, custom SLA/security webhook alerts, and archive-before-purge compliance policies.
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
      <AppIcon name="alert" :size="16" class="mt-0.5 shrink-0" />
      <span>{{ errorMsg }}</span>
    </div>

    <div v-if="loading" class="h-64 animate-pulse rounded-lg border border-[#E4E4E7] bg-white" />

    <template v-else>
      <!-- 1. API Token Cost Catalog -->
      <section class="rounded-lg border border-[#E4E4E7] bg-white">
        <div class="flex flex-col gap-3 border-b border-[#E4E4E7] px-4 py-3 sm:flex-row sm:items-center sm:justify-between">
          <div>
            <h2 class="text-[15px] font-semibold text-[#09090B]">API Token Cost Catalog ($ / 1M Tokens)</h2>
            <p class="mt-0.5 text-xs text-[#52525B]">
              {{ pricing?.billing_note }} Edit any default or custom model rate inline.
            </p>
          </div>
          <div class="flex items-center gap-2">
            <input
              v-model="modelSearch"
              type="search"
              placeholder="Search models..."
              aria-label="Search model pricing catalog"
              class="w-56 rounded-md border border-[#E4E4E7] bg-white px-3 py-1.5 text-xs text-[#09090B]"
            />
            <span class="text-xs text-[#71717A] tabular-nums">{{ allModelEntries.length }} models</span>
          </div>
        </div>

        <div class="p-4 space-y-4">
          <div class="grid gap-3 rounded-md border border-[#E4E4E7] bg-[#FAFAFA] p-3 sm:grid-cols-4 sm:items-end">
            <div>
              <label class="block text-xs font-medium text-[#52525B]">Add Model Name</label>
              <input
                v-model="customModelName"
                type="text"
                placeholder="e.g. llama-3.3-70b"
                class="mt-1 w-full rounded-md border border-[#E4E4E7] bg-white px-3 py-1.5 text-[13px] text-[#09090B]"
              />
            </div>
            <div>
              <label class="block text-xs font-medium text-[#52525B]">Input $/1M</label>
              <input
                v-model="customInPrice"
                type="number"
                step="0.01"
                min="0"
                class="mt-1 w-full rounded-md border border-[#E4E4E7] bg-white px-3 py-1.5 text-[13px] text-[#09090B] tabular-nums"
              />
            </div>
            <div>
              <label class="block text-xs font-medium text-[#52525B]">Output $/1M</label>
              <input
                v-model="customOutPrice"
                type="number"
                step="0.01"
                min="0"
                class="mt-1 w-full rounded-md border border-[#E4E4E7] bg-white px-3 py-1.5 text-[13px] text-[#09090B] tabular-nums"
              />
            </div>
            <Button data-variant="primary" :disabled="savingPricing || !customModelName.trim()" @click="addModelPrice">
              {{ savingPricing ? "Saving..." : "Add Model" }}
            </Button>
          </div>

          <div class="overflow-x-auto table-scroll rounded border border-[#E4E4E7]">
            <table class="w-full border-collapse text-left text-[13px]">
              <thead>
                <tr class="border-b border-[#E4E4E7] bg-[#F4F4F5] text-xs font-medium text-[#52525B]">
                  <th class="px-3 py-2">Model</th>
                  <th class="px-3 py-2 text-right">Input / 1M</th>
                  <th class="px-3 py-2 text-right">Output / 1M</th>
                  <th class="px-3 py-2">Source</th>
                  <th class="px-3 py-2 text-right">Actions</th>
                </tr>
              </thead>
              <tbody class="divide-y divide-[#E4E4E7]">
                <tr v-for="m in pagedModels" :key="m.name" class="hover:bg-[#FAFAFA]">
                  <td class="px-3 py-2 font-mono-data text-xs font-medium text-[#09090B]">{{ m.name }}</td>
                  <td class="px-3 py-2 text-right tabular-nums">
                    <input
                      v-if="editingModel === m.name"
                      v-model="editInPrice"
                      type="number"
                      step="0.01"
                      min="0"
                      class="w-24 rounded border border-[#0F766E] px-2 py-1 text-right text-xs"
                    />
                    <span v-else>\${{ m.input.toFixed(2) }}</span>
                  </td>
                  <td class="px-3 py-2 text-right tabular-nums">
                    <input
                      v-if="editingModel === m.name"
                      v-model="editOutPrice"
                      type="number"
                      step="0.01"
                      min="0"
                      class="w-24 rounded border border-[#0F766E] px-2 py-1 text-right text-xs"
                    />
                    <span v-else>\${{ m.output.toFixed(2) }}</span>
                  </td>
                  <td class="px-3 py-2 text-xs">
                    <span
                      class="rounded border px-2 py-0.5"
                      :class="
                        m.isOverridden
                          ? 'border-[#FDE68A] bg-[#FFFBEB] text-[#92400E]'
                          : m.isCustomOnly
                            ? 'border-[#99F6E4] bg-[#F0FDFA] text-[#0F766E]'
                            : 'border-[#E4E4E7] bg-[#F4F4F5] text-[#52525B]'
                      "
                    >
                      {{ m.isOverridden ? "Overridden Default" : m.isCustomOnly ? "Custom" : "Default" }}
                    </span>
                  </td>
                  <td class="px-3 py-2 text-right whitespace-nowrap space-x-2">
                    <template v-if="editingModel === m.name">
                      <Button data-variant="primary" @click="saveInlineModel(m.name)">Save</Button>
                      <Button @click="editingModel = null">Cancel</Button>
                    </template>
                    <template v-else>
                      <button
                        type="button"
                        class="text-xs font-medium text-[#0F766E] hover:underline"
                        @click="startEditModel(m.name, m.input, m.output)"
                      >
                        Edit
                      </button>
                      <button
                        v-if="m.isOverridden || m.isCustomOnly"
                        type="button"
                        class="text-xs font-medium text-[#DC2626] hover:underline"
                        @click="deleteModelPrice(m.name, m.isDefault)"
                      >
                        {{ m.isDefault ? "Reset" : "Remove" }}
                      </button>
                    </template>
                  </td>
                </tr>
              </tbody>
            </table>
          </div>

          <div v-if="modelTotalPages > 1" class="flex items-center justify-between text-xs text-[#52525B]">
            <span>Page {{ modelPage }} of {{ modelTotalPages }}</span>
            <div class="flex gap-2">
              <Button :disabled="modelPage <= 1" @click="modelPage -= 1">Previous</Button>
              <Button :disabled="modelPage >= modelTotalPages" @click="modelPage += 1">Next</Button>
            </div>
          </div>
        </div>
      </section>

      <!-- 2. Monitoring, Custom Alert Rules & HTTPS Webhooks -->
      <section class="rounded-lg border border-[#E4E4E7] bg-white">
        <div class="border-b border-[#E4E4E7] px-4 py-3">
          <h2 class="text-[15px] font-semibold text-[#09090B]">Monitoring, Custom Alert Rules &amp; Webhooks</h2>
          <p class="mt-0.5 text-xs text-[#52525B]">
            Built-in SLA rules plus custom metric triggers dispatched to SSRF-validated HTTPS webhooks.
          </p>
        </div>

        <div class="p-4 space-y-5">
          <div class="grid gap-3 sm:grid-cols-4">
            <div>
              <label class="block text-xs font-medium text-[#52525B]">Error Rate Max (%)</label>
              <input
                v-model="errorRateMax"
                type="number"
                step="0.5"
                min="0.1"
                max="100"
                class="mt-1 w-full rounded-md border border-[#E4E4E7] bg-white px-3 py-1.5 text-[13px] tabular-nums"
              />
            </div>
            <div>
              <label class="block text-xs font-medium text-[#52525B]">p95 Latency Max (ms)</label>
              <input
                v-model="p95LatencyMax"
                type="number"
                step="50"
                min="10"
                class="mt-1 w-full rounded-md border border-[#E4E4E7] bg-white px-3 py-1.5 text-[13px] tabular-nums"
              />
            </div>
            <div>
              <label class="block text-xs font-medium text-[#52525B]">Grounding Blocks Max</label>
              <input
                v-model="groundingBlocksMax"
                type="number"
                min="1"
                class="mt-1 w-full rounded-md border border-[#E4E4E7] bg-white px-3 py-1.5 text-[13px] tabular-nums"
              />
            </div>
            <div>
              <label class="block text-xs font-medium text-[#52525B]">Cooldown (sec)</label>
              <input
                v-model="cooldownSeconds"
                type="number"
                min="5"
                class="mt-1 w-full rounded-md border border-[#E4E4E7] bg-white px-3 py-1.5 text-[13px] tabular-nums"
              />
            </div>
          </div>

          <div class="grid gap-3 sm:grid-cols-[1fr_auto] sm:items-end">
            <div>
              <label class="block text-xs font-medium text-[#52525B]">Default Alert Webhook URL (Slack / PagerDuty / HTTPS)</label>
              <input
                v-model="webhookUrl"
                type="url"
                placeholder="https://hooks.slack.com/services/... or https://your-tunnel.trycloudflare.com/webhook"
                class="mt-1 w-full rounded-md border border-[#E4E4E7] bg-white px-3 py-1.5 text-[13px] font-mono-data"
              />
            </div>
            <Button :disabled="testingWebhook || !webhookUrl.trim()" @click="testWebhook">
              {{ testingWebhook ? "Testing..." : "Test Webhook" }}
            </Button>
          </div>

          <div
            v-if="webhookTestResult"
            class="rounded-md border px-3 py-2 text-xs"
            :class="
              webhookTestResult.delivered
                ? 'border-[#99F6E4] bg-[#F0FDFA] text-[#0F766E]'
                : 'border-[#FECACA] bg-[#FEF2F2] text-[#DC2626]'
            "
          >
            {{ webhookTestResult.delivered ? "Delivered" : "Delivery failed" }} ({{ webhookTestResult.detail }}) ·
            {{ webhookTestResult.https_enforced ? "HTTPS TLS verified" : "Plain HTTP (dev only)" }}
          </div>

          <div class="rounded-md border border-[#E4E4E7] bg-[#FAFAFA] px-3 py-2 text-xs text-[#52525B]">
            <span class="font-medium text-[#09090B]">HTTPS Webhook Note:</span>
            Production deployments terminate TLS at your load balancer / reverse proxy (AWS ALB, Caddy, Nginx).
            For local HTTPS webhook testing behind NAT, expose your receiver with
            <code class="font-mono-data">cloudflared tunnel --url http://localhost:9000</code> or <code class="font-mono-data">ngrok http 9000</code>.
          </div>

          <!-- Custom Alert Rule Builder -->
          <div class="space-y-3 border-t border-[#E4E4E7] pt-4">
            <h3 class="text-xs font-semibold uppercase tracking-wide text-[#52525B]">Custom Alert Rules</h3>
            <div class="grid gap-2 sm:grid-cols-6 sm:items-end">
              <div class="sm:col-span-2">
                <label class="block text-xs text-[#52525B]">Rule Name</label>
                <input
                  v-model="newRuleName"
                  type="text"
                  placeholder="e.g. Subcall Error Spike"
                  class="mt-1 w-full rounded-md border border-[#E4E4E7] bg-white px-2.5 py-1.5 text-xs"
                />
              </div>
              <div>
                <label class="block text-xs text-[#52525B]">Metric</label>
                <select v-model="newRuleMetric" class="mt-1 w-full rounded-md border border-[#E4E4E7] bg-white px-2 py-1.5 text-xs">
                  <option value="error_rate_pct">Error Rate %</option>
                  <option value="p95_latency_ms">p95 Latency ms</option>
                  <option value="grounding_blocks">Grounding Blocks</option>
                  <option value="subcall_errors">Subcall Errors</option>
                  <option value="upstream_down">Upstreams Down</option>
                </select>
              </div>
              <div class="flex gap-1.5">
                <select v-model="newRuleOp" class="mt-5 w-16 rounded-md border border-[#E4E4E7] bg-white px-1.5 py-1.5 text-xs">
                  <option value=">=">&gt;=</option>
                  <option value=">">&gt;</option>
                  <option value="<=">&lt;=</option>
                  <option value="==">==</option>
                </select>
                <div class="flex-1">
                  <label class="block text-xs text-[#52525B]">Threshold</label>
                  <input
                    v-model="newRuleThreshold"
                    type="number"
                    step="0.5"
                    class="mt-1 w-full rounded-md border border-[#E4E4E7] bg-white px-2 py-1.5 text-xs tabular-nums"
                  />
                </div>
              </div>
              <div>
                <label class="block text-xs text-[#52525B]">Severity</label>
                <select v-model="newRuleSeverity" class="mt-1 w-full rounded-md border border-[#E4E4E7] bg-white px-2 py-1.5 text-xs">
                  <option value="warning">Warning</option>
                  <option value="critical">Critical</option>
                </select>
              </div>
              <Button :disabled="!newRuleName.trim()" @click="addCustomRule">Add Rule</Button>
            </div>

            <div v-if="customRules.length" class="divide-y divide-[#E4E4E7] rounded border border-[#E4E4E7]">
              <div
                v-for="rule in customRules"
                :key="rule.id"
                class="flex flex-wrap items-center justify-between gap-2 px-3 py-2 text-xs"
              >
                <div class="flex items-center gap-2">
                  <input v-model="rule.enabled" type="checkbox" aria-label="Toggle rule" />
                  <span class="font-medium text-[#09090B]">{{ rule.name }}</span>
                  <span class="font-mono-data text-[#52525B]">{{ rule.metric }} {{ rule.operator }} {{ rule.threshold }}</span>
                  <span
                    class="rounded border px-1.5 py-0.5 text-[11px]"
                    :class="
                      rule.severity === 'critical'
                        ? 'border-[#FECACA] bg-[#FEF2F2] text-[#DC2626]'
                        : 'border-[#FDE68A] bg-[#FFFBEB] text-[#92400E]'
                    "
                  >
                    {{ rule.severity }}
                  </span>
                </div>
                <button type="button" class="text-[#DC2626] hover:underline" @click="removeCustomRule(rule.id)">
                  Remove
                </button>
              </div>
            </div>
          </div>

          <div>
            <Button data-variant="primary" :disabled="savingAlerts" @click="saveAlertRules">
              {{ savingAlerts ? "Saving..." : "Save All Alert Policies" }}
            </Button>
          </div>
        </div>
      </section>

      <!-- 3. Compliance Retention & Archive-Before-Purge -->
      <section class="rounded-lg border border-[#E4E4E7] bg-white">
        <div class="border-b border-[#E4E4E7] px-4 py-3">
          <h2 class="text-[15px] font-semibold text-[#09090B]">Compliance Audit Archive &amp; Retention</h2>
          <p class="mt-0.5 text-xs text-[#52525B]">
            Archive execution logs older than the retention window to S3-compatible object storage or JSONL before purging the active DB.
          </p>
        </div>
        <div class="p-4 space-y-4">
          <div class="grid gap-3 sm:grid-cols-2">
            <div>
              <label class="block text-xs font-medium text-[#52525B]">Active DB Retention Window (Days)</label>
              <input
                v-model="retentionDays"
                type="number"
                min="0"
                max="3650"
                class="mt-1 w-full rounded-md border border-[#E4E4E7] bg-white px-3 py-1.5 text-[13px] tabular-nums"
              />
            </div>
            <div>
              <label class="block text-xs font-medium text-[#52525B]">S3 Archive Bucket (optional; blank = JSONL download)</label>
              <input
                v-model="archiveS3Bucket"
                type="text"
                placeholder="e.g. acme-mcp-audit-archive"
                class="mt-1 w-full rounded-md border border-[#E4E4E7] bg-white px-3 py-1.5 text-[13px] font-mono-data"
              />
            </div>
          </div>
          <div class="flex flex-wrap gap-2">
            <Button data-variant="primary" :disabled="archiving" @click="archiveAuditRuns(true)">
              {{ archiving ? "Archiving..." : "Archive & Purge Old Runs" }}
            </Button>
            <Button :disabled="archiving" @click="archiveAuditRuns(false)">
              Export JSONL Only (Keep DB)
            </Button>
            <Button data-variant="danger" :disabled="pruning" @click="pruneAuditRuns">
              {{ pruning ? "Purging..." : "Purge Without Archive" }}
            </Button>
          </div>
        </div>
      </section>
    </template>
  </div>
</template>
