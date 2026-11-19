<script setup lang="ts">
import { extractErrorMessage } from "~/composables/useAdminApi";

interface AbFactor {
  name: string;
  delta: number;
  detail: string;
}

interface AbTestResult {
  upstream_name: string;
  tool_count: number;
  classic_schema_tokens: number;
  algorithmic: {
    execution: string;
    discovery_mode: string;
    gateway_mode: string;
    score: number;
    reason: string;
    factors: AbFactor[];
  };
  replay_ab?: {
    mode?: string;
    winner: string;
    classic: { total_context_tokens: number; payload_bytes?: number };
    code_mode: { total_context_tokens: number; payload_bytes?: number };
    deltas: { context_token_savings_pct: number };
  } | null;
  live_llm_ab?: {
    model: string;
    winner: string;
    token_savings_pct: number;
    classic: { total_tokens: number; turns: number; duration_ms: number; accuracy_passed: boolean };
    code_mode: { total_tokens: number; turns: number; duration_ms: number; accuracy_passed: boolean };
  } | null;
  recommendation: {
    recommended_mode: string;
    confidence: string;
    reasons: string[];
    ready_config_patch: {
      preferred_mode: string;
      ab_experiment: { enabled: boolean; experiment_id: string; codemode_traffic_pct: number };
    };
  };
}

interface InspectResponse {
  inspection: {
    upstream: string;
    type: string;
    tool_count: number;
    tools: {
      qualified_name: string;
      name: string;
      description?: string;
      mutating: boolean;
      output_schema?: Record<string, unknown> | null;
    }[];
  };
  onboarding_audit: {
    summary?: string;
    warnings?: string[];
  };
}

const { apiFetch } = useAdminApi();

const dynamicPermitted = ref(true);
const existingUpstreams = ref<string[]>([]);
const errorMessage = ref("");
const statusMessage = ref("");

// Draft upstream configuration
const draftName = ref("inventory_api");
const draftType = ref("openapi");
const draftUrl = ref("http://localhost:8001");
const draftSpecUrl = ref("http://localhost:8001/openapi.json");
const draftAuthType = ref("none");
const draftRoles = ref("finance_analyst, ops_engineer, gateway_admin");
const draftStateful = ref(false);
const draftPreferredMode = ref("auto");
const draftAbEnabled = ref(false);
const draftAbExpId = ref("ab_inventory_v1");
const draftAbSplitPct = ref("50");

// Inspection output
const inspectResult = ref<InspectResponse | null>(null);

// A/B Lab controls
const abSource = ref("draft");
const abSelectedUpstream = ref("");
const abTab = ref("replay");
const abIntent = ref("reconcile inventory and summarize anomalies");
const abDryRun = ref(true);
const abSampleBytes = ref("12000");

const llmEnabled = ref(false);
const llmBaseUrl = ref("https://api.openai.com/v1");
const llmModel = ref("gpt-4o-mini");
const llmApiKey = ref("");
const llmPrompt = ref("List active records and return a concise summary.");
const llmExpected = ref("");
const abResult = ref<AbTestResult | null>(null);

// Agent Prompt & JSON Import Dialogs
const promptDialogOpen = ref(false);
const agentPromptText = ref("");
const importDialogOpen = ref(false);
const importJsonText = ref("");

const typeOptions = [
  { value: "openapi", label: "openapi" },
  { value: "mcp_http", label: "mcp_http" },
  { value: "mcp_stdio", label: "mcp_stdio" },
];

const authOptions = [
  { value: "none", label: "none" },
  { value: "delegated_token", label: "delegated_token (OBO)" },
  { value: "api_key", label: "api_key" },
  { value: "bearer_secret", label: "bearer_secret" },
];

const modeOptions = [
  { value: "auto", label: "Auto — Algorithmic Router (Recommended)" },
  { value: "codemode", label: "Code Mode — Deferred Search & Sandbox" },
  { value: "traditional_discover", label: "Classic MCP — Discover & Direct Calls" },
  { value: "hybrid", label: "Hybrid — Code Mode Reads, Confirmed Writes" },
];

const schemaVerified = ref(false);
const testCallResult = ref<string>("");
const testCallBusy = ref(false);
const availableRoles = ref<string[]>([
  "finance_analyst",
  "ops_engineer",
  "support_agent",
  "readonly_auditor",
  "gateway_admin",
]);

onMounted(async () => {
  try {
    const res = await apiFetch<{ roles?: string[] }>("/admin/roles", {}, true);
    if (res.roles && res.roles.length > 0) {
      availableRoles.value = res.roles;
    }
  } catch {
    // Keep default role list if roles endpoint unavailable
  }
});

const selectedRoleSet = computed(() => {
  return new Set(
    draftRoles.value
      .split(",")
      .map((s) => s.trim())
      .filter(Boolean)
  );
});

function toggleRole(role: string) {
  const next = new Set(selectedRoleSet.value);
  if (next.has(role)) {
    next.delete(role);
  } else {
    next.add(role);
  }
  draftRoles.value = Array.from(next).join(", ");
}

const authHelpText = computed(() => {
  if (draftAuthType.value === "delegated_token") {
    return "Exchanges the caller's JWT via OBO/RFC 8693 for a downstream token scoped to this user.";
  }
  if (draftAuthType.value === "api_key") {
    return "Injects a secret API key from an environment variable on the gateway host (never exposed to Monty).";
  }
  if (draftAuthType.value === "bearer_secret") {
    return "Sends a static service Bearer token from a host secret environment variable.";
  }
  return "No downstream credentials sent — suitable only for internal read-only sandboxes.";
});

const authRequestPreview = computed(() => {
  const host = draftUrl.value.trim() || "https://api.example.com";
  if (draftAuthType.value === "delegated_token") {
    return `POST ${host}/...\\nAuthorization: Bearer <OBO_USER_SCOPED_JWT>\\nX-Forwarded-Sub: <caller_sub>`;
  }
  if (draftAuthType.value === "api_key") {
    return `GET ${host}/...\\nX-API-Key: <HOST_ENV_SECRET_KEY>`;
  }
  if (draftAuthType.value === "bearer_secret") {
    return `GET ${host}/...\\nAuthorization: Bearer <HOST_ENV_SERVICE_TOKEN>`;
  }
  return `GET ${host}/...\\n(No Authorization header injected)`;
});

const sessionAdvice = computed(() => {
  const count = inspectResult.value?.inspection.tool_count ?? 0;
  return {
    statefulHint:
      sourceKind.value === "mcp_http"
        ? "Auto-detect recommended: only enable Stateful MCP Session if the server rejects stateless POST /mcp without Mcp-Session-Id."
        : "Keep OFF for REST/OpenAPI backends — HTTP OpenAPI calls are naturally stateless.",
    abHint:
      count >= 10
        ? `Recommended: ${count} tools detected — run a Live Traffic A/B Experiment to prove Code Mode token savings.`
        : "Optional: small tool catalog (< 10 tools) works well in Auto or Classic Discover mode.",
  };
});

function applyLocalLlmPreset(kind: "lmstudio" | "ollama") {
  llmEnabled.value = true;
  if (kind === "lmstudio") {
    llmBaseUrl.value = "http://host.docker.internal:1234/v1";
    llmModel.value = "qwen3.5-9b";
  } else {
    llmBaseUrl.value = "http://host.docker.internal:11434/v1";
    llmModel.value = "qwen3:8b";
  }
  statusMessage.value = `Configured local ${kind === "lmstudio" ? "LM Studio" : "Ollama"} endpoint.`;
}

const onboardingStep = ref<1 | 2 | 3>(1);
const promptTab = ref("prompt");
const sourceKind = ref<"openapi_url" | "openapi_inline" | "rest_builder" | "mcp_http">(
  "openapi_url"
);

watch([sourceKind, draftUrl, draftSpecUrl], () => {
  schemaVerified.value = false;
  testCallResult.value = "";
});
const inlineSpecText = ref(
  JSON.stringify(
    {
      openapi: "3.1.0",
      info: { title: "Custom REST Service", version: "1.0.0" },
      paths: {
        "/items": {
          get: { operationId: "list_items", summary: "List records" },
        },
      },
    },
    null,
    2
  )
);
const restEndpoints = ref([
  { method: "get", path: "/health", operationId: "get_health", summary: "Health check" },
  { method: "get", path: "/records", operationId: "list_records", summary: "List records" },
]);

function addRestEndpoint() {
  restEndpoints.value.push({
    method: "get",
    path: "/resource",
    operationId: `op_${restEndpoints.value.length + 1}`,
    summary: "Custom endpoint",
  });
}

function removeRestEndpoint(idx: number) {
  if (restEndpoints.value.length > 1) {
    restEndpoints.value.splice(idx, 1);
  }
}

function buildSynthesizedSpec(): Record<string, unknown> {
  const paths: Record<string, Record<string, unknown>> = {};
  for (const ep of restEndpoints.value) {
    const cleanPath = ep.path.startsWith("/") ? ep.path : `/${ep.path}`;
    paths[cleanPath] ||= {};
    paths[cleanPath][ep.method.toLowerCase()] = {
      operationId: ep.operationId.trim() || undefined,
      summary: ep.summary.trim() || ep.operationId,
    };
  }
  return {
    openapi: "3.1.0",
    info: { title: draftName.value.trim() || "custom_rest_api", version: "1.0.0" },
    paths,
  };
}

function buildUpstreamPayload() {
  const roles = draftRoles.value
    .split(",")
    .map((s) => s.trim())
    .filter(Boolean);
  const splitNum = Math.min(100, Math.max(0, Number.parseInt(draftAbSplitPct.value || "50", 10) || 50));

  let resolvedType = draftType.value;
  let specUrl: string | undefined = undefined;
  let inlineSpec: Record<string, unknown> | undefined = undefined;

  if (sourceKind.value === "openapi_url") {
    resolvedType = "openapi";
    specUrl = draftSpecUrl.value.trim() || undefined;
  } else if (sourceKind.value === "openapi_inline") {
    resolvedType = "openapi";
    inlineSpec = JSON.parse(inlineSpecText.value);
  } else if (sourceKind.value === "rest_builder") {
    resolvedType = "openapi";
    inlineSpec = buildSynthesizedSpec();
  } else if (sourceKind.value === "mcp_http") {
    resolvedType = "mcp_http";
  }

  return {
    name: draftName.value.trim(),
    type: resolvedType,
    url: draftUrl.value.trim() || undefined,
    openapi_spec_url: specUrl,
    openapi_spec: inlineSpec,
    auth_type: draftAuthType.value,
    allowed_roles: roles,
    stateful_session: draftStateful.value,
    preferred_mode: draftPreferredMode.value,
    enabled: true,
    ab_experiment: draftAbEnabled.value
      ? {
          enabled: true,
          experiment_id: draftAbExpId.value.trim() || `ab_${draftName.value.trim()}_v1`,
          codemode_traffic_pct: splitNum,
        }
      : null,
  };
}

const yamlPreview = computed(() => {
  const cfg = buildUpstreamPayload();
  const lines = [
    `upstreams:`,
    `  - name: ${cfg.name}`,
    `    type: ${cfg.type}`,
    cfg.url ? `    url: ${cfg.url}` : "",
    cfg.openapi_spec_url ? `    openapi_spec_url: ${cfg.openapi_spec_url}` : "",
    `    auth_type: ${cfg.auth_type}`,
    `    stateful_session: ${cfg.stateful_session}`,
    `    preferred_mode: ${cfg.preferred_mode}`,
    `    allowed_roles: [${cfg.allowed_roles.join(", ")}]`,
  ].filter(Boolean);
  if (cfg.ab_experiment) {
    lines.push(
      `    ab_experiment:`,
      `      enabled: true`,
      `      experiment_id: ${cfg.ab_experiment.experiment_id}`,
      `      codemode_traffic_pct: ${cfg.ab_experiment.codemode_traffic_pct}`
    );
  }
  return lines.join("\n");
});

const existingUpstreamOptions = computed(() => {
  const base = [{ value: "draft", label: "Draft Configuration (left panel)" }];
  for (const name of existingUpstreams.value) {
    base.push({ value: name, label: `Registered: ${name}` });
  }
  return base;
});

onMounted(async () => {
  try {
    const res = await apiFetch<{
      dynamic_upstreams_permitted: boolean;
      upstreams: { name: string }[];
    }>("/admin/upstreams");
    dynamicPermitted.value = res.dynamic_upstreams_permitted;
    existingUpstreams.value = res.upstreams.map((u) => u.name);
    if (res.upstreams.length > 0) {
      abSelectedUpstream.value = res.upstreams[0]?.name || "";
    }
  } catch {
    // Error handled smoothly if user triggers action
  }
});

async function handleInspect() {
  errorMessage.value = "";
  statusMessage.value = "";
  testCallResult.value = "";
  try {
    inspectResult.value = await apiFetch<InspectResponse>("/admin/upstreams/inspect", {
      method: "POST",
      body: buildUpstreamPayload(),
    });
    schemaVerified.value = true;
    statusMessage.value = `Inspected '${inspectResult.value.inspection.upstream}' (${inspectResult.value.inspection.tool_count} tools). Step 2 unlocked.`;
  } catch (err) {
    schemaVerified.value = false;
    errorMessage.value = extractErrorMessage(err);
  }
}

async function handleTestCall(toolName: string) {
  testCallBusy.value = true;
  errorMessage.value = "";
  testCallResult.value = "";
  try {
    const res = await apiFetch<{ ok: boolean; tool: string; result: unknown }>(
      "/admin/upstreams/test-call",
      {
        method: "POST",
        body: {
          upstream: buildUpstreamPayload(),
          tool_name: toolName,
          arguments: {},
        },
      }
    );
    testCallResult.value = JSON.stringify(res.result, null, 2);
    statusMessage.value = `Live test call '${res.tool}' succeeded.`;
  } catch (err) {
    errorMessage.value = extractErrorMessage(err);
  } finally {
    testCallBusy.value = false;
  }
}

async function handleRegister() {
  errorMessage.value = "";
  statusMessage.value = "";
  try {
    const res = await apiFetch<{ registered: boolean; catalog_version: string }>("/admin/upstreams", {
      method: "POST",
      body: buildUpstreamPayload(),
    });
    statusMessage.value = `Registered upstream '${draftName.value}' (catalog ${res.catalog_version}).`;
    if (!existingUpstreams.value.includes(draftName.value.trim())) {
      existingUpstreams.value.push(draftName.value.trim());
    }
  } catch (err) {
    errorMessage.value = extractErrorMessage(err);
  }
}

async function copyYaml() {
  if (import.meta.client && navigator.clipboard) {
    await navigator.clipboard.writeText(yamlPreview.value);
    statusMessage.value = "Copied GitOps YAML snippet to clipboard.";
  }
}

async function openAgentPrompt() {
  errorMessage.value = "";
  try {
    const res = await apiFetch<{ prompt: string }>("/admin/upstreams/agent-prompt");
    agentPromptText.value = res.prompt;
    promptDialogOpen.value = true;
  } catch (err) {
    errorMessage.value = extractErrorMessage(err);
  }
}

function applyImportedJson() {
  errorMessage.value = "";
  try {
    const parsed = JSON.parse(importJsonText.value) as Record<string, unknown>;
    if (typeof parsed.name === "string") draftName.value = parsed.name;
    if (typeof parsed.type === "string") draftType.value = parsed.type;
    if (typeof parsed.url === "string") draftUrl.value = parsed.url;
    if (typeof parsed.openapi_spec_url === "string") draftSpecUrl.value = parsed.openapi_spec_url;
    if (typeof parsed.auth_type === "string") draftAuthType.value = parsed.auth_type;
    if (typeof parsed.stateful_session === "boolean") draftStateful.value = parsed.stateful_session;
    if (typeof parsed.preferred_mode === "string") draftPreferredMode.value = parsed.preferred_mode;
    if (Array.isArray(parsed.allowed_roles)) {
      draftRoles.value = parsed.allowed_roles.map((r) => String(r)).join(", ");
    }
    importDialogOpen.value = false;
    statusMessage.value = "Imported JSON into draft form.";
  } catch {
    errorMessage.value = "Invalid JSON payload. Paste a single UpstreamConfig JSON object.";
  }
}

async function runAbTest() {
  errorMessage.value = "";
  statusMessage.value = "";
  const body: Record<string, unknown> = {
    intent: abIntent.value.trim() || undefined,
    dry_run: abDryRun.value,
    sample_payload_bytes: Math.max(0, Number.parseInt(abSampleBytes.value || "0", 10) || 0),
  };
  if (abSource.value === "draft") {
    body.upstream = buildUpstreamPayload();
  } else {
    body.upstream_name = abSource.value;
  }
  if (llmEnabled.value) {
    body.llm_config = {
      base_url: llmBaseUrl.value.trim(),
      model: llmModel.value.trim(),
      api_key: llmApiKey.value,
      prompt: llmPrompt.value.trim(),
      expected_substrings: llmExpected.value
        .split(",")
        .map((s) => s.trim())
        .filter(Boolean),
    };
  }
  try {
    abResult.value = await apiFetch<AbTestResult>("/admin/upstreams/ab-test", {
      method: "POST",
      body,
    });
    statusMessage.value = `A/B test completed for '${abResult.value.upstream_name}'. Recommendation: ${abResult.value.recommendation.recommended_mode}.`;
  } catch (err) {
    errorMessage.value = extractErrorMessage(err);
  }
}

function applyRecommendation(enableLiveSplit = false) {
  if (!abResult.value) return;
  const patch = abResult.value.recommendation.ready_config_patch;
  draftPreferredMode.value = patch.preferred_mode;
  draftAbExpId.value = patch.ab_experiment.experiment_id;
  draftAbSplitPct.value = String(patch.ab_experiment.codemode_traffic_pct);
  draftAbEnabled.value = enableLiveSplit;
  statusMessage.value = `Applied recommended mode '${patch.preferred_mode}' to draft form.`;
}

function applyQuickPreset(preset: "mock-eu" | "mock-snowflake") {
  if (preset === "mock-eu") {
    sourceKind.value = "openapi_url";
    draftName.value = "billing_eu_sandbox";
    draftType.value = "openapi";
    draftUrl.value = "http://mock-backends:8001/billing-eu";
    draftSpecUrl.value = "http://mock-backends:8001/billing-eu/openapi.json";
    draftAuthType.value = "delegated_token";
    draftRoles.value = "finance_analyst, billing_reader, gateway_admin";
  } else {
    sourceKind.value = "mcp_http";
    draftName.value = "snowflake_sandbox";
    draftType.value = "mcp_http";
    draftUrl.value = "http://mock-backends:8001/snowflake/mcp";
    draftSpecUrl.value = "";
    draftAuthType.value = "delegated_token";
    draftRoles.value = "finance_analyst, snowflake_reader, gateway_admin";
  }
  onboardingStep.value = 1;
  statusMessage.value = `Loaded preset '${draftName.value}'. Click Inspect Schema to verify tools.`;
}

async function copyPromptText() {
  if (typeof navigator !== "undefined" && navigator.clipboard && agentPromptText.value) {
    await navigator.clipboard.writeText(agentPromptText.value);
    statusMessage.value = "Copied Agent Onboarding Prompt to clipboard.";
  }
}
</script>

<template>
  <div class="space-y-6">
    <div class="flex flex-col gap-3 sm:flex-row sm:items-center sm:justify-between">
      <div>
        <h1 class="text-[20px] font-semibold tracking-[-0.015em] text-[#09090B]">
          HITL Onboarding &amp; A/B Test Lab
        </h1>
        <p class="mt-0.5 text-[13px] text-[#52525B]">
          Inspect schemas, verify security hints, and A/B-test Classic vs. Code Mode before publishing an upstream.
        </p>
      </div>
      <div class="flex flex-wrap items-center gap-2">
        <Button @click="openAgentPrompt">Agent Onboarding Prompt</Button>
        <Button @click="importDialogOpen = true">Import Agent JSON</Button>
      </div>
    </div>

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

    <!-- 3-Step Onboarding Stepper & Presets -->
    <section
      aria-label="Onboarding workflow steps"
      class="flex flex-col gap-3 rounded-lg border border-[#E4E4E7] bg-white p-4 sm:flex-row sm:items-center sm:justify-between"
    >
      <div class="flex flex-wrap items-center gap-2">
        <Button
          :data-variant="onboardingStep === 1 ? 'primary' : undefined"
          class="text-xs"
          @click="onboardingStep = 1"
        >
          1. Connect &amp; Inspect
        </Button>
        <Button
          :data-variant="onboardingStep === 2 ? 'primary' : undefined"
          :disabled="!schemaVerified"
          class="text-xs"
          @click="onboardingStep = 2"
        >
          2. Policy &amp; Mode
        </Button>
        <Button
          :data-variant="onboardingStep === 3 ? 'primary' : undefined"
          :disabled="!schemaVerified"
          class="text-xs"
          @click="onboardingStep = 3"
        >
          3. A/B Test &amp; Save
        </Button>
      </div>
      <div class="flex flex-wrap items-center gap-2">
        <span class="text-xs text-[#71717A]">Quick presets:</span>
        <Button class="text-xs" @click="applyQuickPreset('mock-eu')">Mock OpenAPI</Button>
        <Button class="text-xs" @click="applyQuickPreset('mock-snowflake')">Mock MCP HTTP</Button>
      </div>
    </section>

    <!-- 3-Step Progressive Wizard Workspace -->
    <div v-if="onboardingStep === 1" class="rounded-lg border border-[#E4E4E7] bg-white p-5 space-y-5">
      <div class="flex flex-wrap items-center justify-between gap-2 border-b border-[#E4E4E7] pb-3">
        <div>
          <h2 class="text-[15px] font-semibold text-[#09090B]">Step 1 — Connect API or MCP Server</h2>
          <p class="text-xs text-[#52525B]">
            Import an OpenAPI spec, define REST endpoints directly, or connect an existing MCP server.
          </p>
        </div>
        <span class="text-xs text-[#52525B]">
          {{ dynamicPermitted ? "Dynamic Registration" : "GitOps Read-Only" }}
        </span>
      </div>

      <!-- 4 Input Method Cards -->
      <div class="grid grid-cols-1 gap-2 sm:grid-cols-2 lg:grid-cols-4">
        <Button
          :data-variant="sourceKind === 'openapi_url' ? 'primary' : undefined"
          class="justify-start text-xs"
          @click="sourceKind = 'openapi_url'; draftType = 'openapi'"
        >
          OpenAPI Spec URL
        </Button>
        <Button
          :data-variant="sourceKind === 'openapi_inline' ? 'primary' : undefined"
          class="justify-start text-xs"
          @click="sourceKind = 'openapi_inline'; draftType = 'openapi'"
        >
          Paste OpenAPI JSON
        </Button>
        <Button
          :data-variant="sourceKind === 'rest_builder' ? 'primary' : undefined"
          class="justify-start text-xs"
          @click="sourceKind = 'rest_builder'; draftType = 'openapi'"
        >
          Quick REST Builder
        </Button>
        <Button
          :data-variant="sourceKind === 'mcp_http' ? 'primary' : undefined"
          class="justify-start text-xs"
          @click="sourceKind = 'mcp_http'; draftType = 'mcp_http'"
        >
          Existing MCP Server
        </Button>
      </div>

      <div class="grid grid-cols-1 gap-4 sm:grid-cols-2">
        <Field>
          <FieldLabel>Upstream Name</FieldLabel>
          <Input v-model="draftName" class="font-mono-data" />
        </Field>
        <Field>
          <FieldLabel>Base / MCP Endpoint URL</FieldLabel>
          <Input v-model="draftUrl" class="font-mono-data text-xs" />
        </Field>
        <Field v-if="sourceKind === 'openapi_url'" class="sm:col-span-2">
          <FieldLabel>OpenAPI Spec URL</FieldLabel>
          <Input v-model="draftSpecUrl" class="font-mono-data text-xs" />
        </Field>
        <Field v-if="sourceKind === 'openapi_inline'" class="sm:col-span-2">
          <FieldLabel>OpenAPI JSON Document</FieldLabel>
          <Textarea v-model="inlineSpecText" rows="6" class="font-mono-data text-xs" />
        </Field>
      </div>

      <!-- Quick REST Endpoint Builder (for APIs without OpenAPI docs) -->
      <div v-if="sourceKind === 'rest_builder'" class="space-y-2 rounded border border-[#E4E4E7] bg-[#FAFAFA] p-3">
        <div class="flex items-center justify-between">
          <span class="text-xs font-semibold text-[#09090B]">
            Define REST Endpoints (auto-generates MCP tools)
          </span>
          <Button class="text-xs" @click="addRestEndpoint">+ Add Endpoint</Button>
        </div>
        <div
          v-for="(ep, idx) in restEndpoints"
          :key="idx"
          class="grid grid-cols-1 gap-2 sm:grid-cols-12 sm:items-center"
        >
          <select
            v-model="ep.method"
            aria-label="HTTP method"
            class="sm:col-span-2 rounded border border-[#D4D4D8] bg-white px-2 py-1.5 text-xs uppercase"
          >
            <option value="get">GET</option>
            <option value="post">POST</option>
            <option value="put">PUT</option>
            <option value="delete">DELETE</option>
          </select>
          <Input v-model="ep.path" class="sm:col-span-4 font-mono-data text-xs" placeholder="/orders" />
          <Input v-model="ep.operationId" class="sm:col-span-3 font-mono-data text-xs" placeholder="list_orders" />
          <Input v-model="ep.summary" class="sm:col-span-2 text-xs" placeholder="Summary" />
          <Button class="sm:col-span-1 text-xs" @click="removeRestEndpoint(idx)">×</Button>
        </div>
      </div>

      <div class="flex flex-wrap items-center justify-between gap-2 border-t border-[#E4E4E7] pt-4">
        <Button pending-announcement="Inspecting schema" @click="handleInspect">
          Inspect Schema
        </Button>
        <div class="flex items-center gap-2">
          <span v-if="!schemaVerified" class="text-xs text-[#D97706]">
            Inspect schema first to unlock Policy &amp; Guardrails
          </span>
          <Button data-variant="primary" :disabled="!schemaVerified" @click="onboardingStep = 2">
            Next: Policy &amp; Guardrails →
          </Button>
        </div>
      </div>

      <!-- Discovered Tools Preview -->
      <div v-if="inspectResult" class="space-y-3 border-t border-[#E4E4E7] pt-4">
        <div class="flex items-center justify-between">
          <h3 class="text-sm font-semibold text-[#09090B]">
            Discovered MCP Tools ({{ inspectResult.inspection.tool_count }})
          </h3>
          <span class="text-xs text-[#52525B]">{{ inspectResult.onboarding_audit.summary }}</span>
        </div>
        <div class="max-h-56 overflow-x-auto overflow-y-auto rounded border border-[#E4E4E7] table-scroll">
          <table class="w-full border-collapse text-left text-xs">
            <thead>
              <tr class="border-b border-[#E4E4E7] bg-[#F4F4F5] text-[#52525B]">
                <th class="px-3 py-2 whitespace-nowrap">Tool</th>
                <th class="px-3 py-2 whitespace-nowrap">Mutating</th>
                <th class="px-3 py-2 whitespace-nowrap">Output Schema</th>
                <th class="px-3 py-2 text-right whitespace-nowrap">Verify</th>
              </tr>
            </thead>
            <tbody class="divide-y divide-[#E4E4E7]">
              <tr v-for="t in inspectResult.inspection.tools" :key="t.qualified_name">
                <td class="px-3 py-2 font-mono-data text-[#09090B] whitespace-nowrap">{{ t.qualified_name }}</td>
                <td class="px-3 py-2 whitespace-nowrap">{{ t.mutating ? "yes" : "read-only" }}</td>
                <td class="px-3 py-2 whitespace-nowrap">
                  <span :class="t.output_schema ? 'text-[#059669]' : 'text-[#D97706]'">
                    {{ t.output_schema ? "typed" : "untyped" }}
                  </span>
                </td>
                <td class="px-3 py-2 text-right whitespace-nowrap">
                  <Button
                    v-if="!t.mutating"
                    class="px-2 py-0.5 text-[11px]"
                    :disabled="testCallBusy"
                    @click="handleTestCall(t.name)"
                  >
                    Test Call
                  </Button>
                  <span v-else class="text-[#71717A]">write skipped</span>
                </td>
              </tr>
            </tbody>
          </table>
        </div>
        <pre
          v-if="testCallResult"
          class="max-h-40 overflow-auto rounded border border-[#99F6E4] bg-[#F0FDFA] p-3 font-mono-data text-xs text-[#09090B]"
        >{{ testCallResult }}</pre>
      </div>
    </div>

    <!-- Step 2: Access & Guardrails -->
    <div v-else-if="onboardingStep === 2" class="rounded-lg border border-[#E4E4E7] bg-white p-5 space-y-5">
      <div class="border-b border-[#E4E4E7] pb-3">
        <h2 class="text-[15px] font-semibold text-[#09090B]">Step 2 — Access Control &amp; Execution Mode</h2>
        <p class="text-xs text-[#52525B]">
          Choose authentication, which roles can invoke these tools, and whether Code Mode runs automatically.
        </p>
      </div>

      <div class="grid grid-cols-1 gap-4 sm:grid-cols-2">
        <Field>
          <FieldLabel>Upstream Auth Type</FieldLabel>
          <Select
            :items="authOptions"
            :value="draftAuthType"
            @value-change="(v) => { if (v) draftAuthType = String(v) }"
          >
            <SelectTrigger>
              <SelectValue />
              <AppIcon name="chevron-down" :size="14" />
            </SelectTrigger>
            <SelectContent>
              <SelectItem v-for="a in authOptions" :key="a.value" :value="a.value">
                {{ a.label }}
                <SelectItemIndicator><AppIcon name="check" :size="14" /></SelectItemIndicator>
              </SelectItem>
            </SelectContent>
          </Select>
          <FieldDescription>{{ authHelpText }}</FieldDescription>
          <pre class="mt-2 overflow-x-auto rounded border border-[#E4E4E7] bg-[#FAFAFA] p-2 font-mono-data text-[11px] text-[#52525B]">{{ authRequestPreview }}</pre>
        </Field>

        <Field>
          <FieldLabel>Preferred Execution Mode</FieldLabel>
          <Select
            :items="modeOptions"
            :value="draftPreferredMode"
            @value-change="(v) => { if (v) draftPreferredMode = String(v) }"
          >
            <SelectTrigger>
              <SelectValue />
              <AppIcon name="chevron-down" :size="14" />
            </SelectTrigger>
            <SelectContent>
              <SelectItem v-for="m in modeOptions" :key="m.value" :value="m.value">
                {{ m.label }}
                <SelectItemIndicator><AppIcon name="check" :size="14" /></SelectItemIndicator>
              </SelectItem>
            </SelectContent>
          </Select>
          <div class="mt-2 space-y-1 rounded border border-[#E4E4E7] bg-[#FAFAFA] p-2.5 text-[11px] text-[#52525B]">
            <div><strong class="text-[#09090B]">auto:</strong> Gateway picks Code Mode or Classic per request using tool count &amp; payload size.</div>
            <div><strong class="text-[#0F766E]">codemode:</strong> Defers full schemas (<code class="font-mono-data">search_tools</code> on demand) so 100+ tools don't bloat prompts, and runs Python in Monty.</div>
            <div><strong class="text-[#0F766E]">hybrid:</strong> Code Mode for read/aggregation + confirmed Classic MCP calls for mutating writes.</div>
            <div><strong class="text-[#2563EB]">traditional:</strong> Classic MCP (<code class="font-mono-data">discover</code> + <code class="font-mono-data">tools/call</code>) for simple single-tool calls.</div>
          </div>
        </Field>

        <div class="sm:col-span-2 space-y-2">
          <div class="text-xs font-medium text-[#09090B]">Allowed RBAC Roles (click to toggle)</div>
          <div class="flex flex-wrap gap-1.5">
            <button
              v-for="role in availableRoles"
              :key="role"
              type="button"
              class="rounded border px-2.5 py-1 text-xs font-medium transition-colors"
              :class="
                selectedRoleSet.has(role)
                  ? 'border-[#0F766E] bg-[#F0FDFA] text-[#0F766E]'
                  : 'border-[#E4E4E7] bg-white text-[#52525B] hover:bg-[#F4F4F5]'
              "
              @click="toggleRole(role)"
            >
              {{ role }}
            </button>
          </div>
          <Field>
            <FieldLabel>Custom / Selected Roles</FieldLabel>
            <Input v-model="draftRoles" class="font-mono-data text-xs" />
          </Field>
        </div>
      </div>

      <div class="rounded border border-[#99F6E4] bg-[#F0FDFA] p-3 text-xs text-[#09090B] space-y-1">
        <div>
          <strong class="text-[#0F766E]">Stateless by Default:</strong>
          Monty sandboxes are 100% ephemeral, and MCP Streamable HTTP defaults to stateless. Legacy downstream <code class="font-mono-data">Mcp-Session-Id</code> handshakes are handled automatically if a 3rd-party server requires one.
        </div>
        <div><strong class="text-[#0F766E]">A/B Advice:</strong> {{ sessionAdvice.abHint }}</div>
      </div>

      <details class="rounded border border-[#E4E4E7] bg-[#FAFAFA] p-3">
        <summary class="cursor-pointer text-xs font-semibold text-[#09090B]">
          Advanced Session &amp; A/B Settings (Optional — auto-detected by default)
        </summary>
        <div class="mt-3 grid grid-cols-1 gap-4 sm:grid-cols-2">
          <Field>
            <div class="flex items-center justify-between gap-2">
              <div>
                <FieldLabel>Stateful MCP Session</FieldLabel>
                <FieldDescription>Only enable if upstream requires pinned Mcp-Session-Id</FieldDescription>
              </div>
              <Switch v-model:checked="draftStateful">
                <SwitchThumb />
              </Switch>
            </div>
          </Field>
          <Field>
            <div class="flex items-center justify-between gap-2">
              <div>
                <FieldLabel>Live Traffic A/B Experiment</FieldLabel>
                <FieldDescription>Split traffic between Classic and Code Mode in /mcp</FieldDescription>
              </div>
              <Switch v-model:checked="draftAbEnabled">
                <SwitchThumb />
              </Switch>
            </div>
          </Field>
          <Field v-if="draftAbEnabled">
            <FieldLabel>Experiment ID</FieldLabel>
            <Input v-model="draftAbExpId" class="font-mono-data text-xs" />
          </Field>
          <Field v-if="draftAbEnabled">
            <FieldLabel>Code Mode Traffic Split (%)</FieldLabel>
            <Input v-model="draftAbSplitPct" inputmode="numeric" />
          </Field>
        </div>
      </details>

      <div class="flex flex-wrap items-center justify-between gap-2 border-t border-[#E4E4E7] pt-4">
        <Button @click="onboardingStep = 1">← Back: Connect</Button>
        <Button data-variant="primary" @click="onboardingStep = 3">Next: Test &amp; Publish →</Button>
      </div>
    </div>

    <!-- Step 3: Test & Publish -->
    <div v-else class="grid grid-cols-1 gap-6 xl:grid-cols-12">
      <section class="space-y-5 rounded-lg border border-[#E4E4E7] bg-white p-5 xl:col-span-7">
        <div class="flex items-center gap-2 border-b border-[#E4E4E7] pb-3">
          <AppIcon name="flask" :size="16" class="text-[#0F766E]" />
          <h2 class="text-[15px] font-semibold text-[#09090B]">
            Step 3 — Onboarding A/B Test Lab &amp; Publish
          </h2>
        </div>

        <Field>
          <FieldLabel>Target Upstream to Evaluate</FieldLabel>
          <Select
            :items="existingUpstreamOptions"
            :value="abSource"
            @value-change="(v) => { if (v) abSource = String(v) }"
          >
            <SelectTrigger>
              <SelectValue />
              <AppIcon name="chevron-down" :size="14" />
            </SelectTrigger>
            <SelectContent>
              <SelectItem v-for="opt in existingUpstreamOptions" :key="opt.value" :value="opt.value">
                {{ opt.label }}
                <SelectItemIndicator><AppIcon name="check" :size="14" /></SelectItemIndicator>
              </SelectItem>
            </SelectContent>
          </Select>
          <FieldDescription>
            Select which registered service (or your current draft) receives the Classic vs. Code Mode evaluation probe.
          </FieldDescription>
        </Field>

        <Tabs v-model:value="abTab">
          <TabsList aria-label="A/B evaluation modes">
            <TabsTab value="replay">Algorithmic &amp; Replay</TabsTab>
            <TabsTab value="llm">Live Admin LLM (Optional)</TabsTab>
          </TabsList>

          <TabsPanel value="replay" class="space-y-3">
            <Field>
              <FieldLabel>Workload Intent (optional)</FieldLabel>
              <Input v-model="abIntent" placeholder="e.g. reconcile invoices and summarize deltas" />
            </Field>
            <div class="grid grid-cols-1 gap-3 sm:grid-cols-2">
              <Field>
                <FieldLabel>Estimated Payload Bytes</FieldLabel>
                <Input v-model="abSampleBytes" inputmode="numeric" />
              </Field>
              <Field>
                <div class="flex h-full items-center justify-between gap-2 pt-4">
                  <FieldLabel>Dry-Run Estimate Only</FieldLabel>
                  <Switch v-model:checked="abDryRun">
                    <SwitchThumb />
                  </Switch>
                </div>
              </Field>
            </div>
          </TabsPanel>

          <TabsPanel value="llm" class="space-y-3">
            <Field>
              <div class="flex items-center justify-between gap-2">
                <div>
                  <FieldLabel>Enable Live OpenAI-Compatible A/B</FieldLabel>
                  <FieldDescription>Runs both Classic &amp; Code Mode arms against the chosen model</FieldDescription>
                </div>
                <Switch v-model:checked="llmEnabled">
                  <SwitchThumb />
                </Switch>
              </div>
            </Field>
            <div v-if="llmEnabled" class="space-y-3">
              <div class="flex flex-wrap items-center gap-2 text-xs">
                <span class="text-[#52525B]">Local LLM presets:</span>
                <Button class="px-2 py-0.5 text-[11px]" @click="applyLocalLlmPreset('lmstudio')">
                  LM Studio (:1234)
                </Button>
                <Button class="px-2 py-0.5 text-[11px]" @click="applyLocalLlmPreset('ollama')">
                  Ollama (:11434)
                </Button>
              </div>
              <p class="text-[11px] text-[#52525B]">
                Running in Docker? Use <code class="font-mono-data">http://host.docker.internal:1234/v1</code> (LM Studio) or <code class="font-mono-data">http://host.docker.internal:11434/v1</code> (Ollama) with Local Network / CORS enabled.
              </p>
              <Field>
                <FieldLabel>Base URL</FieldLabel>
                <Input v-model="llmBaseUrl" class="font-mono-data text-xs" />
              </Field>
              <div class="grid grid-cols-2 gap-3">
                <Field>
                  <FieldLabel>Model</FieldLabel>
                  <Input v-model="llmModel" class="font-mono-data text-xs" />
                </Field>
                <Field>
                  <FieldLabel>API Key (optional for local)</FieldLabel>
                  <Input v-model="llmApiKey" type="password" class="font-mono-data text-xs" />
                </Field>
              </div>
              <Field>
                <FieldLabel>Test Prompt</FieldLabel>
                <Textarea v-model="llmPrompt" rows="2" />
              </Field>
              <Field>
                <FieldLabel>Expected Substrings (comma-separated)</FieldLabel>
                <Input v-model="llmExpected" placeholder="total, status" />
              </Field>
            </div>
          </TabsPanel>
        </Tabs>

        <div class="flex flex-wrap items-center justify-between gap-2 border-t border-[#E4E4E7] pt-4">
          <Button @click="onboardingStep = 2">← Back: Policy</Button>
          <div class="flex flex-wrap items-center gap-2">
            <Button
              pending-announcement="Running A/B evaluation"
              @click="runAbTest"
            >
              <AppIcon name="flask" :size="15" />
              <span>Run A/B Evaluation</span>
            </Button>
            <Button
              v-if="dynamicPermitted"
              data-variant="primary"
              data-agent-confirm="Register or update this upstream service in the gateway?"
              pending-announcement="Registering upstream"
              @click="handleRegister"
            >
              Register Upstream
            </Button>
            <Button @click="copyYaml">
              <AppIcon name="copy" :size="14" />
              <span>Copy GitOps YAML</span>
            </Button>
          </div>
        </div>
      </section>

      <!-- Right: A/B Results & GitOps YAML Preview -->
      <section class="space-y-4 rounded-lg border border-[#E4E4E7] bg-white p-5 xl:col-span-5">
        <h3 class="text-sm font-semibold text-[#09090B]">Evaluation &amp; GitOps Preview</h3>
        <pre class="max-h-56 overflow-auto rounded border border-[#E4E4E7] bg-[#F4F4F5] p-3 font-mono-data text-xs text-[#09090B]">{{ yamlPreview }}</pre>

        <!-- A/B Evaluation Results -->
        <div v-if="abResult" class="space-y-4 border-t border-[#E4E4E7] pt-4">
          <div class="rounded-md border border-[#99F6E4] bg-[#F0FDFA] p-3">
            <div class="flex items-center justify-between gap-2">
              <span class="text-xs font-medium text-[#0F766E]">Recommended Mode</span>
              <span class="font-mono-data text-sm font-semibold text-[#09090B]">
                {{ abResult.recommendation.recommended_mode }} ({{ abResult.recommendation.confidence }})
              </span>
            </div>
            <p class="mt-1 text-xs text-[#52525B]">{{ abResult.algorithmic.reason }}</p>
            <div class="mt-3 flex flex-wrap gap-2">
              <Button data-variant="primary" class="text-xs" @click="applyRecommendation(false)">
                Apply Preferred Mode
              </Button>
              <Button class="text-xs" @click="applyRecommendation(true)">
                Apply + Enable 50/50 Split
              </Button>
            </div>
          </div>

          <Meter
            :value="abResult.algorithmic.score"
            :min="0"
            :max="100"
            :format="(v: number) => `${v.toFixed(1)} / 100`"
          >
            <MeterLabel>Code Mode Suitability Score</MeterLabel>
            <MeterValue class="tabular-nums text-xs font-semibold" />
            <MeterTrack>
              <MeterIndicator />
            </MeterTrack>
          </Meter>

          <ul v-if="abResult.algorithmic.factors.length" class="space-y-1 text-xs text-[#52525B]">
            <li v-for="f in abResult.algorithmic.factors" :key="f.name" class="flex justify-between gap-2">
              <span>{{ f.detail }}</span>
              <span
                class="font-mono-data font-medium tabular-nums shrink-0"
                :class="f.delta >= 0 ? 'text-[#0F766E]' : 'text-[#2563EB]'"
              >
                {{ f.delta >= 0 ? `+${f.delta}` : f.delta }}
              </span>
            </li>
          </ul>

          <div v-if="abResult.replay_ab" class="border-t border-[#E4E4E7] pt-3 text-xs space-y-1.5">
            <div class="flex items-center justify-between font-semibold text-[#09090B]">
              <span>Replay Context Comparison</span>
              <span class="text-[#0F766E] tabular-nums">
                {{ abResult.replay_ab.deltas.context_token_savings_pct }}% saved ({{ abResult.replay_ab.winner }})
              </span>
            </div>
            <div class="flex justify-between text-[#52525B] tabular-nums">
              <span class="text-[#2563EB]">Classic context: {{ abResult.replay_ab.classic.total_context_tokens }} tok</span>
              <span class="text-[#0F766E]">Code Mode context: {{ abResult.replay_ab.code_mode.total_context_tokens }} tok</span>
            </div>
          </div>

          <div v-if="abResult.live_llm_ab" class="border-t border-[#E4E4E7] pt-3 text-xs space-y-1">
            <div class="flex items-center justify-between font-semibold text-[#09090B]">
              <span>Live LLM ({{ abResult.live_llm_ab.model }})</span>
              <span class="text-[#0F766E]">{{ abResult.live_llm_ab.winner }} ({{ abResult.live_llm_ab.token_savings_pct }}% fewer tok)</span>
            </div>
            <div class="flex justify-between text-[#52525B] tabular-nums">
              <span>Classic: {{ abResult.live_llm_ab.classic.total_tokens }} tok ({{ abResult.live_llm_ab.classic.turns }} turns)</span>
              <span>Code Mode: {{ abResult.live_llm_ab.code_mode.total_tokens }} tok ({{ abResult.live_llm_ab.code_mode.turns }} turns)</span>
            </div>
          </div>
        </div>
      </section>
    </div>

    <!-- Agent Prompt Dialog -->
    <Dialog v-model:open="promptDialogOpen">
      <DialogBackdrop />
      <DialogContent class="sm:max-w-2xl w-[calc(100%-2rem)]">
        <DialogTitle>AI Agent Onboarding (WebMCP, Self-Admin MCP &amp; JSON)</DialogTitle>
        <DialogDescription>
          Agents can fill this UI via WebMCP, configure the gateway directly over POST /admin/mcp, or emit UpstreamConfig JSON.
        </DialogDescription>
        <Tabs v-model:value="promptTab" class="mt-2">
          <TabsList aria-label="Agent onboarding integration modes">
            <TabsTab value="prompt">Copyable Prompt</TabsTab>
            <TabsTab value="admin-mcp">Self-Admin MCP</TabsTab>
          </TabsList>
          <TabsPanel value="prompt" class="pt-3">
            <pre class="max-h-72 overflow-auto rounded border border-[#E4E4E7] bg-[#F4F4F5] p-3 font-mono-data text-xs text-[#09090B]">{{ agentPromptText }}</pre>
          </TabsPanel>
          <TabsPanel value="admin-mcp" class="pt-3 space-y-2 text-xs text-[#52525B]">
            <p>
              Point any MCP client with a <code class="font-mono-data text-[#09090B]">gateway_admin</code> Bearer token at <code class="font-mono-data text-[#09090B]">POST /admin/mcp</code>:
            </p>
            <pre class="overflow-auto rounded border border-[#E4E4E7] bg-[#F4F4F5] p-3 font-mono-data text-xs text-[#09090B]">{"jsonrpc":"2.0","id":1,"method":"tools/list","params":{}}
// Tools: admin_list_upstreams, admin_inspect_upstream, admin_run_ab_test,
//        admin_save_upstream, admin_get_runtime_telemetry, admin_get_analytics, admin_demo_traffic_burst</pre>
          </TabsPanel>
        </Tabs>
        <div class="mt-4 flex justify-end gap-2">
          <Button @click="copyPromptText">
            <AppIcon name="copy" :size="14" />
            <span>Copy Prompt</span>
          </Button>
          <DialogClose data-variant="primary">Close</DialogClose>
        </div>
      </DialogContent>
    </Dialog>

    <!-- Import Agent JSON Dialog -->
    <Dialog v-model:open="importDialogOpen">
      <DialogBackdrop />
      <DialogContent class="sm:max-w-xl w-[calc(100%-2rem)]">
        <DialogTitle>Import Agent-Generated Upstream JSON</DialogTitle>
        <DialogDescription>
          Paste an UpstreamConfig JSON payload produced by your coding agent to populate the draft form.
        </DialogDescription>
        <Field>
          <FieldLabel>UpstreamConfig JSON</FieldLabel>
          <Textarea
            v-model="importJsonText"
            rows="6"
            class="font-mono-data text-xs"
            placeholder='{"name": "crm_api", "type": "openapi", "url": "http://localhost:8001"}'
          />
        </Field>
        <div class="flex justify-end gap-2">
          <DialogClose>Cancel</DialogClose>
          <Button data-variant="primary" @click="applyImportedJson">Apply JSON</Button>
        </div>
      </DialogContent>
    </Dialog>
  </div>
</template>
