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

const { apiFetch, session, authConfig, runtimeMeta, loadAuthState, setRuntimeMeta } = useAdminApi();
type SettingsTab = "workspace" | "pricing" | "alerts" | "retention" | "routing" | "secrets";
const activeSettingsTab = ref<SettingsTab>("workspace");
const settingsTabs: { id: SettingsTab; label: string }[] = [
  { id: "workspace", label: "Workspace & access" },
  { id: "pricing", label: "Model pricing" },
  { id: "alerts", label: "Alerts & webhooks" },
  { id: "retention", label: "Audit & retention" },
  { id: "routing", label: "Routing & calibration" },
  { id: "secrets", label: "Vault secrets" },
];
function setSettingsTab(tab: SettingsTab) {
  activeSettingsTab.value = tab;
}

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
const autoInterval = ref("off");
const archiveS3Bucket = ref("");
const archiving = ref(false);
const pruning = ref(false);
const savingPolicy = ref(false);
const retentionConfirmOpen = ref(false);
const pendingRetentionAction = ref<"archive" | "prune" | null>(null);

function requestRetentionAction(action: "archive" | "prune") {
  pendingRetentionAction.value = action;
  retentionConfirmOpen.value = true;
}

async function confirmRetentionAction() {
  const action = pendingRetentionAction.value;
  retentionConfirmOpen.value = false;
  pendingRetentionAction.value = null;
  if (action === "archive") await archiveAuditRuns(true);
  if (action === "prune") await pruneAuditRuns();
}

async function loadSettings() {
  errorMsg.value = "";
  try {
    await loadAuthState();
    const [p, r, pol] = await Promise.all([
      apiFetch<ModelPricingMap>("/admin/pricing/models"),
      apiFetch<AlertRules>("/admin/alerts/rules"),
      apiFetch<{ retention_days: number; auto_interval: string; s3_bucket: string }>(
        "/admin/audit/retention-policy"
      ),
    ]);
    try {
      const rt = await apiFetch<{ gateway?: { profile?: string; app_env?: string; catalog_version?: string } }>(
        "/admin/telemetry/runtime",
        {},
        true
      );
      if (rt?.gateway) {
        setRuntimeMeta({
          profile: rt.gateway.profile || runtimeMeta.value.profile,
          app_env: rt.gateway.app_env || runtimeMeta.value.app_env,
          catalog_version: rt.gateway.catalog_version || runtimeMeta.value.catalog_version,
        });
      }
    } catch {
      // Optional runtime metadata fetch
    }
    pricing.value = p;
    errorRateMax.value = String(r.error_rate_pct_max);
    p95LatencyMax.value = String(r.p95_latency_ms_max);
    groundingBlocksMax.value = String(r.grounding_blocks_max);
    webhookUrl.value = r.webhook_url || "";
    cooldownSeconds.value = String(r.webhook_cooldown_seconds ?? 60);
    customRules.value = r.custom_rules ? [...r.custom_rules] : [];
    retentionDays.value = String(pol.retention_days ?? 90);
    autoInterval.value = pol.auto_interval || "off";
    archiveS3Bucket.value = pol.s3_bucket || "";
  } catch (err) {
    errorMsg.value = extractErrorMessage(err);
  } finally {
    loading.value = false;
  }
}

const adaptiveMode = ref<"observe" | "active">("observe");
const savingAdaptiveMode = ref(false);

async function loadAdaptiveMode() {
  try {
    const res = await apiFetch<{ adaptive_mode?: "observe" | "active" }>("/admin/adaptive-execution/report?window=30d");
    adaptiveMode.value = res.adaptive_mode === "active" ? "active" : "observe";
  } catch { adaptiveMode.value = "observe"; }
}

async function saveAdaptiveMode() {
  savingAdaptiveMode.value = true;
  try {
    await apiFetch("/admin/adaptive-execution/settings", { method: "POST", body: { mode: adaptiveMode.value } });
    statusMsg.value = `Adaptive routing set to ${adaptiveMode.value}.`;
  } catch (err) { errorMsg.value = extractErrorMessage(err); }
  finally { savingAdaptiveMode.value = false; }
}


const vaultSecrets = ref<Array<{ name: string; updated_by: string; updated_at: string }>>([]);
const newSecretName = ref("");
const newSecretValue = ref("");
const savingSecret = ref(false);
const secretNotice = ref("");

async function loadVaultSecrets() {
  try {
    const res = await apiFetch<{ secrets: Array<{ name: string; updated_by: string; updated_at: string }> }>("/admin/secrets");
    vaultSecrets.value = res.secrets || [];
  } catch {
    // ignore if unavailable
  }
}

async function saveVaultSecret() {
  const name = newSecretName.value.trim();
  const value = newSecretValue.value.trim();
  if (!name || !value) return;
  savingSecret.value = true;
  secretNotice.value = "";
  errorMsg.value = "";
  try {
    await apiFetch(`/admin/secrets/${encodeURIComponent(name)}`, {
      method: "PUT",
      body: { value },
    });
    newSecretName.value = "";
    newSecretValue.value = "";
    secretNotice.value = `Encrypted and stored vault://${name}.`;
    await loadVaultSecrets();
  } catch (err) {
    errorMsg.value = extractErrorMessage(err);
  } finally {
    savingSecret.value = false;
  }
}

async function deleteVaultSecret(name: string) {
  secretNotice.value = "";
  errorMsg.value = "";
  try {
    await apiFetch(`/admin/secrets/${encodeURIComponent(name)}`, { method: "DELETE" });
    secretNotice.value = `Deleted vault://${name}.`;
    await loadVaultSecrets();
  } catch (err) {
    errorMsg.value = extractErrorMessage(err);
  }
}

onMounted(() => {
  void loadAuthState(true).catch((err) => { errorMsg.value = extractErrorMessage(err); });
  void loadSettings();
  void loadAdaptiveMode();
  void loadVaultSecrets();
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
  const input = Number(editInPrice.value);
  const output = Number(editOutPrice.value);
  if (!Number.isFinite(input) || !Number.isFinite(output) || input < 0 || output < 0 || input > 1000 || output > 1000) {
    errorMsg.value = "Model prices must be valid numbers between $0 and $1,000 per 1M tokens.";
    return;
  }
  savingPricing.value = true;
  errorMsg.value = "";
  statusMsg.value = "";
  try {
    await apiFetch("/admin/pricing/models", {
      method: "POST",
      body: {
        model_name: name,
        input_per_1m_usd: input,
        output_per_1m_usd: output,
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
        Gateway configuration
      </h1>
      <p class="mt-0.5 text-[13px] text-[#52525B]">
        Administrator access, detected model pricing, alerts, routing policy, and audit retention.
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
      <nav aria-label="Settings sections" class="flex gap-1 overflow-x-auto border-b border-[#E4E4E7]" role="tablist">
        <button v-for="tab in settingsTabs" :key="tab.id" type="button" role="tab" :aria-selected="activeSettingsTab === tab.id" :aria-controls="`settings-panel-${tab.id}`"
          class="min-h-11 shrink-0 border-b-2 px-3 text-xs font-medium transition-colors focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-[#0F766E]"
          :class="activeSettingsTab === tab.id ? 'border-[#0F766E] text-[#0F766E]' : 'border-transparent text-[#71717A] hover:text-[#09090B]'"
          @click="setSettingsTab(tab.id)">{{ tab.label }}</button>
      </nav>

      <section v-if="activeSettingsTab === 'workspace'" id="settings-panel-workspace" role="tabpanel" class="space-y-4">
        <div class="rounded-lg border border-[#E4E4E7] bg-white p-4">
          <h2 class="text-[15px] font-semibold text-[#09090B]">Administrator identity</h2>
          <p class="mt-1 text-xs text-[#71717A]">Read-only details from the gateway's current admin session and authentication configuration.</p>
          <dl class="mt-4 grid grid-cols-1 gap-x-6 gap-y-4 sm:grid-cols-2 xl:grid-cols-3">
            <div><dt class="text-xs text-[#71717A]">Signed-in identity</dt><dd class="mt-1 break-all text-sm font-medium text-[#09090B]">{{ session?.user?.email || session?.user?.sub || (session?.authenticated ? "Authenticated administrator" : "No authenticated user") }}</dd></div>
            <div><dt class="text-xs text-[#71717A]">Admin access</dt><dd class="mt-1 text-sm font-medium text-[#09090B]">{{ session?.can_access_admin_console ? "Granted" : "Not granted" }}</dd></div>
            <div><dt class="text-xs text-[#71717A]">Authentication mode</dt><dd class="mt-1 text-sm font-medium text-[#09090B]">{{ authConfig?.auth_mode || "Unavailable" }}</dd></div>
            <div><dt class="text-xs text-[#71717A]">OIDC</dt><dd class="mt-1 break-all text-sm font-medium text-[#09090B]">{{ authConfig?.oidc_enabled ? `Enabled (${authConfig?.oidc_issuer || session?.user?.iss || "OIDC"})` : "Not enabled" }}</dd></div>
            <div><dt class="text-xs text-[#71717A]">Gateway profile</dt><dd class="mt-1 text-sm font-medium text-[#09090B]">{{ runtimeMeta.profile || authConfig?.profile || "—" }}</dd></div>
            <div><dt class="text-xs text-[#71717A]">Environment</dt><dd class="mt-1 text-sm font-medium text-[#09090B]">{{ runtimeMeta.app_env || "—" }}</dd></div>
            <div><dt class="text-xs text-[#71717A]">Catalog version</dt><dd class="mt-1 text-sm font-medium text-[#09090B]">{{ runtimeMeta.catalog_version || "—" }}</dd></div>
            <div><dt class="text-xs text-[#71717A]">Administrator role</dt><dd class="mt-1 text-sm font-medium text-[#09090B]">{{ session?.is_admin ? "Administrator" : (session?.user?.roles || []).join(", ") || "No role claim returned" }}</dd></div>
            <div><dt class="text-xs text-[#71717A]">Tenant</dt><dd class="mt-1 break-all text-sm font-medium text-[#09090B]">{{ session?.user?.tenant_id || "—" }}</dd></div>
          </dl>
          <div class="mt-4 grid grid-cols-1 gap-4 border-t border-[#E4E4E7] pt-4 sm:grid-cols-2">
            <div><h3 class="text-xs font-semibold text-[#52525B]">Groups</h3><p class="mt-1 text-xs text-[#71717A]">{{ (session?.user?.groups || []).join(", ") || "No group claims returned" }}</p></div>
            <div><h3 class="text-xs font-semibold text-[#52525B]">Client groups</h3><p class="mt-1 text-xs text-[#71717A]">{{ (session?.user?.client_groups || []).join(", ") || "No client-group claims returned" }}</p></div>
          </div>
        </div>
        <div class="rounded-lg border border-[#E4E4E7] bg-white p-4">
          <h2 class="text-sm font-semibold text-[#09090B]">Workspace controls</h2>
          <p class="mt-1 text-xs leading-5 text-[#52525B]">Authentication and role assignment are managed by the gateway's OIDC/admin-auth configuration. This console displays the active claims; it does not edit identity-provider policy.</p>
        </div>
      </section>

      <!-- Detected model pricing -->
      <section v-else-if="activeSettingsTab === 'pricing'" id="settings-panel-pricing" role="tabpanel" class="rounded-lg border border-[#E4E4E7] bg-white">
        <div class="flex flex-col gap-3 border-b border-[#E4E4E7] px-4 py-3 sm:flex-row sm:items-center sm:justify-between">
          <div>
            <h2 class="text-[15px] font-semibold text-[#09090B]">Detected model pricing</h2>
            <p class="mt-0.5 text-xs text-[#52525B]">
              {{ pricing?.billing_note }} Edit input/output rates for models the gateway has detected. Prices are per 1 million tokens.
            </p>
          </div>
          <div class="flex flex-wrap items-center gap-2">
            <input
              v-model="modelSearch"
              type="search"
              placeholder="Search models..."
              aria-label="Search model pricing catalog"
              class="w-full sm:w-56 rounded-md border border-[#E4E4E7] bg-white px-3 py-1.5 text-xs text-[#09090B]"
            />
            <span class="text-xs text-[#71717A] tabular-nums">{{ allModelEntries.length }} models</span>
          </div>
        </div>

        <div class="p-4 space-y-4">
          <p class="text-xs leading-5 text-[#52525B]">Models are populated from the detected model catalogue and configured defaults. Edit the input/output rates for a detected model below; there is no need to register model names manually here.</p>

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
                    <span v-else>${{ m.input.toFixed(2) }}</span>
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
                    <span v-else>${{ m.output.toFixed(2) }}</span>
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
                        type="button"
                        class="text-xs font-medium text-[#DC2626] hover:underline"
                        :title="`Deletes ${m.name} pricing, routing override and demo profile`"
                        @click="deleteModelPrice(m.name, false)"
                      >
                        Delete
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

      <!-- Alert thresholds and webhook destinations -->
      <section v-else-if="activeSettingsTab === 'alerts'" id="settings-panel-alerts" role="tabpanel" class="rounded-lg border border-[#E4E4E7] bg-white">
        <div class="border-b border-[#E4E4E7] px-4 py-3">
          <h2 class="text-[15px] font-semibold text-[#09090B]">Monitoring, Custom Alert Rules &amp; Webhooks</h2>
          <p class="mt-0.5 text-xs text-[#52525B]">
            Built-in SLA rules plus custom metric triggers. Configure one shared webhook URL, with an optional override on each custom rule.
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
            <div class="grid gap-2 sm:grid-cols-8 sm:items-end">
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
              <div class="sm:col-span-2">
                <label class="block text-xs text-[#52525B]">Webhook override (optional)</label>
                <input
                  v-model="newRuleWebhook"
                  type="url"
                  placeholder="https://receiver.example.com/alerts"
                  class="mt-1 w-full rounded-md border border-[#E4E4E7] bg-white px-2.5 py-1.5 text-xs font-mono-data"
                />
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
                  <span class="text-[11px] text-[#71717A]">{{ rule.webhook_url ? "Rule webhook override" : "Shared webhook" }}</span>
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

      <!-- Audit retention and archive schedule -->
      <section v-else-if="activeSettingsTab === 'retention'" id="settings-panel-retention" role="tabpanel" class="rounded-lg border border-[#E4E4E7] bg-white">
        <div class="border-b border-[#E4E4E7] px-4 py-3">
          <h2 class="text-[15px] font-semibold text-[#09090B]">Compliance Audit Archive &amp; Retention</h2>
          <p class="mt-0.5 text-xs text-[#52525B]">
            Archive execution logs older than the retention window to S3-compatible object storage or JSONL before purging the active DB.
          </p>
        </div>
        <div class="p-4 space-y-4">
          <div class="grid gap-3 sm:grid-cols-3">
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
              <label class="block text-xs font-medium text-[#52525B]">Auto-Archive &amp; Purge Schedule</label>
              <select
                v-model="autoInterval"
                class="mt-1 w-full rounded-md border border-[#E4E4E7] bg-white px-3 py-1.5 text-[13px]"
              >
                <option value="off">Manual Only (Off)</option>
                <option value="hourly">Every Hour (Auto-Archive + Purge)</option>
                <option value="daily">Every 24 Hours (Auto-Archive + Purge)</option>
              </select>
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
            <Button
              :disabled="savingPolicy"
              @click="
                savingPolicy = true;
                apiFetch(`/admin/audit/retention-policy?retention_days=${encodeURIComponent(retentionDays)}&auto_interval=${encodeURIComponent(autoInterval)}&s3_bucket=${encodeURIComponent(archiveS3Bucket)}`, { method: 'POST' })
                  .then(() => { statusMsg = `Saved retention schedule (${autoInterval}, ${retentionDays}d).`; })
                  .catch((e) => { errorMsg = extractErrorMessage(e); })
                  .finally(() => { savingPolicy = false; })
              "
            >
              {{ savingPolicy ? "Saving..." : "Save Schedule" }}
            </Button>
            <Button data-variant="primary" :disabled="archiving" @click="requestRetentionAction('archive')">
              Archive &amp; Purge Old Runs
            </Button>
            <Button :disabled="archiving" @click="archiveAuditRuns(false)">
              Export JSONL Only (Keep DB)
            </Button>
            <Button data-variant="danger" :disabled="pruning" @click="requestRetentionAction('prune')">
              Purge Without Archive
            </Button>
          </div>
        </div>
      </section>

      <section v-else-if="activeSettingsTab === 'routing'" id="settings-panel-routing" role="tabpanel" class="rounded-lg border border-[#E4E4E7] bg-white p-4 space-y-3">
        <div>
          <h2 class="text-sm font-semibold text-[#09090B]">
            Adaptive execution routing
          </h2>
          <p class="mt-1 text-xs text-[#71717A]">
            Observe records the mode applied and the evidence recommendation. Active applies qualified workflow evidence to Auto requests for read-only workloads. Explicit Hybrid, manual overrides, configured A/B splits, and dispatcher safety checks keep their existing behavior.
          </p>
        </div>
        <div class="max-w-sm">
          <label class="block text-xs font-medium text-[#52525B]" for="adaptive-routing-mode">Routing mode</label>
          <select id="adaptive-routing-mode" v-model="adaptiveMode" class="mt-1 min-h-11 w-full rounded-md border border-[#D4D4D8] bg-white px-3 text-sm text-[#09090B] focus-visible:outline-2 focus-visible:outline-[#0F766E]">
            <option value="observe">Observe</option>
            <option value="active">Active</option>
          </select>
        </div>
        <div class="rounded-md border border-[#E4E4E7] bg-[#FAFAFA] p-3 text-xs leading-5 text-[#52525B]">
          Eligible traffic uses Auto mode, a known workload profile, and accessible tools that are all read-only. Evidence needs at least five matched pairs within 30 days; the recommended arm must be at least 90% jointly correct and grounded. Higher verified quality wins. Equal quality requires at least 5% lower cost with bounded latency, call, and recovery regressions. Unknown, uncertain, mutating, stale, sparse, or conflicting evidence falls back to deterministic heuristics.
        </div>
        <div class="flex items-center gap-3">
          <Button data-variant="primary" :disabled="savingAdaptiveMode" @click="saveAdaptiveMode">
            {{ savingAdaptiveMode ? "Saving..." : "Save routing mode" }}
          </Button>
          <span v-if="statusMsg" role="status" class="text-xs text-[#0F766E]">{{ statusMsg }}</span>
        </div>
      </section>

      <section v-else-if="activeSettingsTab === 'secrets'" id="settings-panel-secrets" role="tabpanel" class="space-y-4 rounded-lg border border-[#E4E4E7] bg-white p-5">
        <div>
          <h2 class="text-[0.9375rem] font-semibold text-[#09090B]">Vault Secrets (vault://)</h2>
          <p class="mt-1 text-xs text-[#52525B]">
            Store encrypted shared API keys and OAuth client secrets for dynamic upstreams without container restarts. Plaintext values are write-only and never returned.
          </p>
        </div>

        <form class="grid grid-cols-1 gap-3 sm:grid-cols-3" @submit.prevent="saveVaultSecret">
          <div>
            <label class="block text-xs font-medium text-[#52525B]" for="vault-secret-name">Secret Name</label>
            <Input
              id="vault-secret-name"
              v-model="newSecretName"
              placeholder="partner_api_key"
              class="mt-1 w-full font-mono-data text-xs"
            />
          </div>
          <div>
            <label class="block text-xs font-medium text-[#52525B]" for="vault-secret-val">Secret Value (Write-Only)</label>
            <Input
              id="vault-secret-val"
              v-model="newSecretValue"
              type="password"
              autocomplete="off"
              placeholder="Paste secret value"
              class="mt-1 w-full font-mono-data text-xs"
            />
          </div>
          <div class="flex items-end gap-2">
            <Button type="submit" :disabled="savingSecret || !newSecretName.trim() || !newSecretValue.trim()">
              {{ savingSecret ? "Saving..." : "Store Encrypted Secret" }}
            </Button>
          </div>
        </form>
        <p v-if="secretNotice" class="text-xs text-[#0F766E]">{{ secretNotice }}</p>

        <div v-if="vaultSecrets.length > 0" class="overflow-x-auto rounded border border-[#E4E4E7]">
          <table class="w-full border-collapse text-left text-xs">
            <thead>
              <tr class="border-b border-[#E4E4E7] bg-[#F4F4F5] text-[#52525B]">
                <th class="px-3 py-2 font-medium">Reference</th>
                <th class="px-3 py-2 font-medium">Updated By</th>
                <th class="px-3 py-2 font-medium">Updated At</th>
                <th class="px-3 py-2 text-right font-medium">Action</th>
              </tr>
            </thead>
            <tbody class="divide-y divide-[#E4E4E7]">
              <tr v-for="sec in vaultSecrets" :key="sec.name">
                <td class="px-3 py-2 font-mono-data text-[#09090B]">vault://{{ sec.name }}</td>
                <td class="px-3 py-2 text-[#52525B]">{{ sec.updated_by || "admin" }}</td>
                <td class="px-3 py-2 font-mono-data tabular-nums text-[#71717A]">
                  {{ sec.updated_at.slice(0, 19).replace("T", " ") }}
                </td>
                <td class="px-3 py-2 text-right">
                  <Button variant="outline" size="sm" @click="deleteVaultSecret(sec.name)">Delete</Button>
                </td>
              </tr>
            </tbody>
          </table>
        </div>
      </section>

      <Dialog v-model:open="retentionConfirmOpen">
        <DialogBackdrop />
        <DialogContent>
          <DialogTitle>{{ pendingRetentionAction === "archive" ? "Archive and purge old audit runs?" : "Purge old audit runs without an archive?" }}</DialogTitle>
          <DialogDescription>
            <template v-if="pendingRetentionAction === 'archive'">
              The gateway will export runs older than {{ retentionDays }} days and then remove them from the active database. Verify the archive destination before continuing.
            </template>
            <template v-else>
              This removes runs older than {{ retentionDays }} days from the active database without creating an archive. This action is destructive.
            </template>
          </DialogDescription>
          <div class="flex flex-wrap justify-end gap-2 pt-2">
            <DialogClose @click="pendingRetentionAction = null">Cancel</DialogClose>
            <Button :data-variant="pendingRetentionAction === 'prune' ? 'danger' : 'primary'" @click="confirmRetentionAction">
              {{ pendingRetentionAction === "archive" ? "Archive & Purge" : "Purge Without Archive" }}
            </Button>
          </div>
        </DialogContent>
      </Dialog>

    </template>
  </div>
</template>
