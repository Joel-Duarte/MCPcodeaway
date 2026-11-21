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
    name?: string;
    upstream?: string;
    type: string;
    tool_count: number;
    tools: {
      qualified_name: string;
      name: string;
      description?: string;
      mutating: boolean;
      input_schema?: { properties?: Record<string, { type?: string }> };
      output_schema?: Record<string, unknown> | null;
    }[];
  };
  onboarding_audit: {
    summary?: string;
    warnings?: string[];
  };
}

const { apiFetch } = useAdminApi();

type OnboardingStep = "connect" | "policy" | "publish";
type SchemaInspectionState = "idle" | "inspecting" | "verified" | "failed";

const dynamicPermitted = ref(false);
const upstreamsLoadError = ref("");
const existingUpstreams = ref<string[]>([]);
const errorMessage = ref("");
const statusMessage = ref("");
const registeredUpstream = ref("");

const draftName = ref("inventory_api");
const draftType = ref("openapi");
const draftUrl = ref("http://localhost:8001");
const draftSpecUrl = ref("http://localhost:8001/openapi.json");
const draftAuthType = ref("none");
const draftAudience = ref("");
const draftScope = ref("");
const draftSecretEnv = ref("");
const draftSecretRef = ref("");
const draftOAuthDialect = ref("");
const draftOAuthAuthorizeEndpoint = ref("");
const draftOAuthTokenEndpoint = ref("");
const draftOAuthClientId = ref("");
const draftOAuthClientSecretRef = ref("");
const draftOAuthScopes = ref("");
const draftApiKeyHeader = ref("X-API-Key");
const draftRoles = ref("");
const draftGroups = ref("");
const draftAllowAllCallers = ref(false);
const draftPreferredMode = ref("auto");
const draftHealthCheckEnabled = ref(false);
const draftHealthCheckUrl = ref("");
const draftHealthInterval = ref(30);
const draftRequestTimeout = ref(15);
const draftUpstreamRateLimit = ref(120);
const draftUpstreamConcurrency = ref(10);

const inspectResult = ref<InspectResponse | null>(null);

const abIntent = ref("reconcile inventory and summarize anomalies");
const abSampleBytes = ref("12000");

const abResult = ref<AbTestResult | null>(null);
const abBusy = ref(false);

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
  { value: "delegated_token", label: "delegated_token (OBO / RFC 8693)" },
  { value: "client_credentials", label: "client_credentials (OAuth M2M)" },
  { value: "user_oauth", label: "user_oauth (Per-User OAuth PKCE)" },
  { value: "user_api_key", label: "user_api_key (Per-User PAT / Key)" },
  { value: "api_key", label: "api_key (Shared Service Key)" },
  { value: "bearer_secret", label: "bearer_secret (Shared Bearer)" },
  { value: "none", label: "none" },
];

const modeOptions = [
  { value: "auto", label: "Auto — gateway picks per request / per tool (Recommended)" },
  { value: "codemode", label: "Code Mode for the whole service" },
  { value: "traditional_discover", label: "Classic MCP for the whole service" },
];
const toolModeOptions = [
  { value: "inherit", label: "Inherit service mode" },
  { value: "auto", label: "Auto" },
  { value: "codemode", label: "Code Mode" },
  { value: "traditional_discover", label: "Classic MCP" },
];
const draftToolModes = ref<Record<string, string>>({});
const draftToolApprovalMessages = ref<Record<string, string>>({});
const draftToolEffects = ref<Record<string, string>>({});
const draftToolApprovalGroups = ref<Record<string, string>>({});
const draftToolRecovery = ref<Record<string, {
  enabled: boolean;
  keyArgument: string;
  synchronousBudget: number;
  maxAttempts: number;
  recoveryAge: number;
  keyRetention: number;
  statusTool: string;
  statusKeyArgument: string;
  statusField: string;
}>>({});
const availableGroups = ref<string[]>([]);
const rolesLoadError = ref("");
const rolesLoading = ref(true);

const schemaInspectionState = ref<SchemaInspectionState>("idle");
const schemaVerified = computed(() => schemaInspectionState.value === "verified");
const approvalPoliciesValid = computed(() => Object.entries(draftToolEffects.value).every(([tool, effect]) => {
  if (effect !== "high_impact") return true;
  return Boolean(draftToolApprovalGroups.value[tool]?.split(",").some((group) => group.trim()));
}));
const accessPolicyValid = computed(() => draftAllowAllCallers.value || selectedRoleSet.value.size > 0 || selectedGroupSet.value.size > 0);
const testCallResult = ref<string>("");
const testCallBusy = ref(false);
const availableRoles = ref<string[]>([]);

async function loadRoles() {
  rolesLoading.value = true;
  rolesLoadError.value = "";
  try {
    const res = await apiFetch<{ roles?: string[]; groups?: string[] }>("/admin/roles", {}, true);
    if (res.roles && res.roles.length > 0) {
      availableRoles.value = res.roles;
    }
    if (res.groups && res.groups.length > 0) {
      availableGroups.value = res.groups;
    }
  } catch (err) {
    rolesLoadError.value = extractErrorMessage(err);
  } finally {
    rolesLoading.value = false;
  }
}

onMounted(() => { void loadRoles(); });

const selectedRoleSet = computed(() => {
  return new Set(
    draftRoles.value
      .split(",")
      .map((s) => s.trim())
      .filter(Boolean)
  );
});

const selectedGroupSet = computed(
  () => new Set(draftGroups.value.split(",").map((g) => g.trim()).filter(Boolean)),
);
function toggleGroup(group: string) {
  draftAllowAllCallers.value = false;
  const next = new Set(selectedGroupSet.value);
  if (next.has(group)) next.delete(group);
  else next.add(group);
  draftGroups.value = Array.from(next).join(", ");
}

function toggleRole(role: string) {
  draftAllowAllCallers.value = false;
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
    return "Exchanges the caller's JWT via Entra OBO or Keycloak RFC 8693 for a downstream token scoped to this user.";
  }
  if (draftAuthType.value === "client_credentials") {
    return "Enforces user RBAC at the gateway, then fetches a machine-to-machine OAuth token via client_credentials.";
  }
  if (draftAuthType.value === "user_oauth") {
    return "Prompts each user once to authorize via OAuth 2.0 PKCE and stores encrypted refresh tokens per user.";
  }
  if (draftAuthType.value === "user_api_key") {
    return "Prompts each user to save their personal API key/PAT in the encrypted vault (never exposed to the LLM).";
  }
  if (draftAuthType.value === "api_key") {
    return "Injects a shared API key resolved via secret_ref (env://, file://, vault://, akv://, hcv://) on the gateway host.";
  }
  if (draftAuthType.value === "bearer_secret") {
    return "Sends a shared service Bearer token resolved via secret_ref on the gateway host.";
  }
  return "No downstream credentials sent — suitable only for internal read-only sandboxes.";
});

const authRequestPreview = computed(() => {
  const host = draftUrl.value.trim() || "https://api.example.com";
  if (draftAuthType.value === "delegated_token") {
    return `POST ${host}/...\nAuthorization: Bearer <OBO_OR_RFC8693_USER_JWT>`;
  }
  if (draftAuthType.value === "client_credentials") {
    return `POST ${host}/...\nAuthorization: Bearer <CACHED_M2M_CLIENT_CREDENTIALS_JWT>`;
  }
  if (draftAuthType.value === "user_oauth") {
    return `POST ${host}/...\nAuthorization: Bearer <USER_VAULT_OAUTH_TOKEN>`;
  }
  if (draftAuthType.value === "user_api_key") {
    const hdr = draftApiKeyHeader.value.trim() || "X-API-Key";
    return `GET ${host}/...\n${hdr}: <USER_VAULT_PERSONAL_KEY>`;
  }
  if (draftAuthType.value === "api_key") {
    const hdr = draftApiKeyHeader.value.trim() || "X-API-Key";
    return `GET ${host}/...\n${hdr}: <HOST_RESOLVED_SECRET>`;
  }
  if (draftAuthType.value === "bearer_secret") {
    return `GET ${host}/...\nAuthorization: Bearer <HOST_RESOLVED_SECRET>`;
  }
  return `GET ${host}/...\n(No Authorization header injected)`;
});

const onboardingStep = ref<OnboardingStep>("connect");
const promptTab = ref("prompt");
const sourceKind = ref<"openapi_url" | "openapi_inline" | "rest_builder" | "mcp_http">(
  "openapi_url"
);

watch([sourceKind, draftUrl, draftSpecUrl], () => {
  schemaInspectionState.value = "idle";
  inspectResult.value = null;
  testCallResult.value = "";
});

watch([draftRoles, draftGroups], ([roles, groups]) => {
  if (roles.trim() || groups.trim()) draftAllowAllCallers.value = false;
});

function goToOnboardingStep(nextStep: OnboardingStep): void {
  if (nextStep !== "connect" && !schemaVerified.value) return;
  onboardingStep.value = nextStep;
}
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

function toolOverridesPayload(): Record<string, string> {
  const prefix = draftName.value.trim();
  const out: Record<string, string> = {};
  for (const [tool, mode] of Object.entries(draftToolModes.value)) {
    if (mode && mode !== "inherit") out[`${prefix}__${tool}`] = mode;
  }
  return out;
}

const toolEffectOptions = [
  { value: "disabled", label: "Disabled" },
  { value: "read", label: "Read only" },
  { value: "write", label: "Write — caller confirms" },
  { value: "high_impact", label: "High impact — manager approval" },
];
const restMethodOptions = [
  { value: "get", label: "GET" },
  { value: "post", label: "POST" },
  { value: "put", label: "PUT" },
  { value: "delete", label: "DELETE" },
];

function toolApprovalPoliciesPayload(): Record<string, { mode: string; version: number; confirmation_message?: string; approver_groups?: string[] }> {
  const policies: Record<string, { mode: string; version: number; confirmation_message?: string; approver_groups?: string[] }> = {};
  for (const [tool, effect] of Object.entries(draftToolEffects.value)) {
    const message = draftToolApprovalMessages.value[tool]?.trim();
    if (effect === "write") {
      policies[tool] = { mode: "requester_confirmation", version: 1, confirmation_message: message || `Confirm this write to ${tool}.` };
    } else if (effect === "high_impact") {
      const groups = (draftToolApprovalGroups.value[tool] || "").split(",").map((group) => group.trim()).filter(Boolean);
      policies[tool] = { mode: "manager_approval", version: 1, confirmation_message: message || `Approve this high impact action: ${tool}.`, approver_groups: groups };
    }
  }
  return policies;
}

function toolRecoveryPoliciesPayload() {
  return Object.fromEntries(Object.entries(draftToolRecovery.value)
    .filter(([tool, recovery]) => recovery.enabled && ["write", "high_impact"].includes(draftToolEffects.value[tool] || ""))
    .map(([tool, recovery]) => [tool, {
      enabled: true,
      idempotency_key_argument: recovery.keyArgument.trim(),
      synchronous_budget_seconds: Number(recovery.synchronousBudget),
      max_attempts: Number(recovery.maxAttempts),
      max_recovery_age_seconds: Number(recovery.recoveryAge),
      upstream_key_retention_seconds: Number(recovery.keyRetention),
      ...(recovery.statusTool.trim() ? {
        status_tool: recovery.statusTool.trim(),
        status_key_argument: recovery.statusKeyArgument.trim(),
        status_field: recovery.statusField.trim() || "status",
      } : {}),
    }]));
}

function buildUpstreamPayload(forInspection = false) {
  const roles = draftRoles.value
    .split(",")
    .map((s) => s.trim())
    .filter(Boolean);
  const groups = draftGroups.value
    .split(",")
    .map((s) => s.trim())
    .filter(Boolean);

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

  const oauthScopesList = draftOAuthScopes.value
    .split(/[,\s]+/)
    .map((s) => s.trim())
    .filter(Boolean);
  const hasOAuthFields =
    Boolean(draftOAuthDialect.value.trim()) ||
    Boolean(draftOAuthAuthorizeEndpoint.value.trim()) ||
    Boolean(draftOAuthTokenEndpoint.value.trim()) ||
    Boolean(draftOAuthClientId.value.trim()) ||
    Boolean(draftOAuthClientSecretRef.value.trim()) ||
    oauthScopesList.length > 0;

  return {
    name: draftName.value.trim(),
    type: resolvedType,
    url: draftUrl.value.trim() || undefined,
    openapi_spec_url: specUrl,
    openapi_spec: inlineSpec,
    auth_type: draftAuthType.value,
    downstream_audience: draftAudience.value.trim() || undefined,
    downstream_scope: draftScope.value.trim() || undefined,
    secret_env_var: draftSecretEnv.value.trim() || undefined,
    secret_ref: draftSecretRef.value.trim() || undefined,
    api_key_header: draftApiKeyHeader.value.trim() || "X-API-Key",
    oauth: hasOAuthFields
      ? {
          token_exchange_dialect: draftOAuthDialect.value.trim() || undefined,
          authorize_endpoint: draftOAuthAuthorizeEndpoint.value.trim() || undefined,
          token_endpoint: draftOAuthTokenEndpoint.value.trim() || undefined,
          client_id: draftOAuthClientId.value.trim() || undefined,
          client_secret_ref: draftOAuthClientSecretRef.value.trim() || undefined,
          scopes: oauthScopesList,
        }
      : undefined,
    allowed_roles: draftAllowAllCallers.value ? [] : roles,
    allowed_groups: draftAllowAllCallers.value ? [] : groups,
    allow_all_callers: draftAllowAllCallers.value,
    preferred_mode: draftPreferredMode.value,
    tool_mode_overrides: toolOverridesPayload(),
    tool_approval_policies: toolApprovalPoliciesPayload(),
    tool_recovery_policies: forInspection ? {} : toolRecoveryPoliciesPayload(),
    allowed_operations: forInspection ? null : Object.entries(draftToolEffects.value).filter(([, effect]) => effect !== "disabled").map(([tool]) => tool),
    tool_effects: forInspection ? {} : Object.fromEntries(Object.entries(draftToolEffects.value).filter(([, effect]) => effect !== "disabled")),
    mutating_tools: forInspection ? [] : Object.entries(draftToolEffects.value).filter(([, effect]) => effect === "write" || effect === "high_impact").map(([tool]) => tool),
    enabled: true,
    health_check: {
      enabled: draftHealthCheckEnabled.value,
      url: draftHealthCheckEnabled.value ? draftHealthCheckUrl.value.trim() : undefined,
      interval_seconds: draftHealthInterval.value,
      timeout_seconds: 3,
      failure_threshold: 3,
      success_threshold: 2,
    },
    request_timeout_seconds: Number(draftRequestTimeout.value),
    max_requests_per_minute: Number(draftUpstreamRateLimit.value),
    max_concurrent_calls: Number(draftUpstreamConcurrency.value),
    ab_experiment: null,
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
    `    preferred_mode: ${cfg.preferred_mode}`,
    `    allowed_roles: [${cfg.allowed_roles.join(", ")}]`,
    `    allowed_groups: [${cfg.allowed_groups.join(", ")}]`,
    `    allow_all_callers: ${cfg.allow_all_callers}`,
    `    request_timeout_seconds: ${cfg.request_timeout_seconds}`,
    `    max_requests_per_minute: ${cfg.max_requests_per_minute}`,
    `    max_concurrent_calls: ${cfg.max_concurrent_calls}`,
    cfg.health_check.enabled ? `    health_check: { enabled: true, url: ${cfg.health_check.url}, interval_seconds: ${cfg.health_check.interval_seconds} }` : "",
  ].filter(Boolean);
  const overrides = Object.entries(cfg.tool_mode_overrides);
  if (overrides.length) {
    lines.push(`    tool_mode_overrides:`, ...overrides.map(([k, v]) => `      ${k}: ${v}`));
  }
  const approvals = Object.entries(cfg.tool_approval_policies);
  if (approvals.length) {
    lines.push(
      `    tool_approval_policies:`,
      ...approvals.map(([tool, policy]) => {
        const message = policy.confirmation_message
          ? `, confirmation_message: ${JSON.stringify(policy.confirmation_message)}`
          : "";
        const groups = policy.approver_groups?.length
          ? `, approver_groups: [${policy.approver_groups.join(", ")}]`
          : "";
        return `      ${tool}: { mode: ${policy.mode}, version: ${policy.version}${message}${groups} }`;
      })
    );
  }
  const recoveries = Object.entries(cfg.tool_recovery_policies || {});
  if (recoveries.length) {
    lines.push(`    tool_recovery_policies:`, ...recoveries.map(([tool, policy]) =>
      `      ${tool}: ${JSON.stringify(policy)}`,
    ));
  }
  lines.push(`    allowed_operations: [${cfg.allowed_operations?.join(", ") || ""}]`);
  if (Object.keys(cfg.tool_effects).length) {
    lines.push(`    tool_effects:`, ...Object.entries(cfg.tool_effects).map(([tool, effect]) => `      ${tool}: ${effect}`));
  }
  return lines.join("\n");
});

async function loadUpstreams() {
  upstreamsLoadError.value = "";
  try {
    const res = await apiFetch<{
      dynamic_upstreams_permitted: boolean;
      upstreams: { name: string }[];
    }>("/admin/upstreams");
    dynamicPermitted.value = res.dynamic_upstreams_permitted;
    existingUpstreams.value = res.upstreams.map((u) => u.name);
  } catch (err) {
    dynamicPermitted.value = false;
    upstreamsLoadError.value = extractErrorMessage(err);
  }
}

onMounted(() => { void loadUpstreams(); });

async function handleInspect() {
  errorMessage.value = "";
  statusMessage.value = "";
  testCallResult.value = "";
  schemaInspectionState.value = "inspecting";
  inspectResult.value = null;
  try {
    inspectResult.value = await apiFetch<InspectResponse>("/admin/upstreams/inspect", {
      method: "POST",
      body: buildUpstreamPayload(true),
    });
    schemaInspectionState.value = "verified";
    const inspectedName =
      inspectResult.value.inspection.name ||
      inspectResult.value.inspection["upstream"] ||
      draftName.value.trim();
    statusMessage.value = `Inspected '${inspectedName}' (${inspectResult.value.inspection.tool_count} tools). Step 2 unlocked.`;
    draftToolEffects.value = Object.fromEntries(inspectResult.value.inspection.tools.map((tool) => [tool.name, "disabled"]));
    draftToolModes.value = {};
    draftToolApprovalMessages.value = {};
    draftToolApprovalGroups.value = {};
    draftToolRecovery.value = Object.fromEntries(inspectResult.value.inspection.tools.map((tool) => [tool.name, {
      enabled: false,
      keyArgument: "idempotency_key",
      synchronousBudget: 10,
      maxAttempts: 3,
      recoveryAge: 900,
      keyRetention: 3600,
      statusTool: "",
      statusKeyArgument: "idempotency_key",
      statusField: "status",
    }]));
  } catch (err) {
    schemaInspectionState.value = "failed";
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
  if (draftRequestTimeout.value < 0.2 || draftRequestTimeout.value > 120) {
    errorMessage.value = "Request timeout must be between 0.2 and 120 seconds.";
    return;
  }
  if (draftUpstreamRateLimit.value < 1 || draftUpstreamRateLimit.value > 100000) {
    errorMessage.value = "Requests per minute must be between 1 and 100,000.";
    return;
  }
  if (draftUpstreamConcurrency.value < 1 || draftUpstreamConcurrency.value > 10000) {
    errorMessage.value = "Concurrent calls must be between 1 and 10,000.";
    return;
  }
  if (Object.entries(draftToolRecovery.value).some(([tool, recovery]) => recovery.enabled && (
    !["write", "high_impact"].includes(draftToolEffects.value[tool] || "") ||
    !recovery.keyArgument.trim() || recovery.synchronousBudget <= 0 || recovery.synchronousBudget > 30 ||
    recovery.maxAttempts < 1 || recovery.maxAttempts > 3 || recovery.recoveryAge < 1 || recovery.recoveryAge > 3600 ||
    recovery.keyRetention < recovery.recoveryAge || recovery.keyRetention > 86400 ||
    (recovery.statusTool.trim() && !recovery.statusKeyArgument.trim())
  ))) {
    errorMessage.value = "Recovery needs a mutating operation, key argument, valid bounded recovery limits, and key retention at least as long as recovery age.";
    return;
  }
  if (draftHealthCheckEnabled.value && !draftHealthCheckUrl.value.trim()) {
    errorMessage.value = "Enter the API's health URL or turn off active health checks.";
    return;
  }
  if (!accessPolicyValid.value) {
    errorMessage.value = "Select at least one allowed role or group, or explicitly allow all authenticated callers.";
    return;
  }
  if (!approvalPoliciesValid.value) {
    errorMessage.value = "Choose at least one approver group for each high impact operation.";
    return;
  }
  if (rolesLoadError.value) {
    errorMessage.value = "Role and group choices could not be loaded. Retry the lookup before saving access policy.";
    return;
  }
  errorMessage.value = "";
  statusMessage.value = "";
  registeredUpstream.value = "";
  try {
    const payload = buildUpstreamPayload();
    const res = await apiFetch<{ registered: boolean; catalog_version: string }>("/admin/upstreams", {
      method: "POST",
      body: payload,
    });
    registeredUpstream.value = payload.name;
    statusMessage.value = `Registered upstream '${payload.name}' (catalog ${res.catalog_version}).`;
    if (!existingUpstreams.value.includes(payload.name)) {
      existingUpstreams.value.push(payload.name);
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
    if (typeof parsed.downstream_audience === "string")
      draftAudience.value = parsed.downstream_audience;
    if (typeof parsed.downstream_scope === "string") draftScope.value = parsed.downstream_scope;
    if (typeof parsed.secret_env_var === "string") draftSecretEnv.value = parsed.secret_env_var;
    if (typeof parsed.api_key_header === "string") draftApiKeyHeader.value = parsed.api_key_header;
    if (parsed.stateful_session === true) {
      throw new Error(
        "stateful_session is unsupported. MCP requests are sessionless; use explicit resource IDs in tool arguments."
      );
    }
    if (typeof parsed.preferred_mode === "string") draftPreferredMode.value = parsed.preferred_mode;
    if (Array.isArray(parsed.allowed_roles)) {
      draftRoles.value = parsed.allowed_roles.map((r) => String(r)).join(", ");
    }
    if (Array.isArray(parsed.allowed_groups)) {
      draftGroups.value = parsed.allowed_groups.map((g) => String(g)).join(", ");
    }
    if (parsed.openapi_spec && typeof parsed.openapi_spec === "object") {
      sourceKind.value = "openapi_inline";
      inlineSpecText.value = JSON.stringify(parsed.openapi_spec, null, 2);
    }
    importDialogOpen.value = false;
    statusMessage.value = "Imported JSON into draft form.";
  } catch (err) {
    errorMessage.value =
      err instanceof Error
        ? err.message
        : "Invalid JSON payload. Paste a single UpstreamConfig JSON object.";
  }
}

async function runAbTest() {
  errorMessage.value = "";
  statusMessage.value = "";
  abBusy.value = true;
  try {
    abResult.value = await apiFetch<AbTestResult>("/admin/upstreams/ab-test", {
      method: "POST",
      body: {
        upstream: buildUpstreamPayload(),
        intent: abIntent.value.trim() || undefined,
        dry_run: true,
        sample_payload_bytes: Math.max(0, Number.parseInt(abSampleBytes.value || "0", 10) || 0),
      },
    });
    statusMessage.value = `Estimate complete for '${abResult.value.upstream_name}'. Suggested mode: ${abResult.value.recommendation.recommended_mode}.`;
  } catch (err) {
    errorMessage.value = extractErrorMessage(err);
  } finally {
    abBusy.value = false;
  }
}

function applyRecommendation() {
  if (!abResult.value) return;
  draftPreferredMode.value = abResult.value.recommendation.ready_config_patch.preferred_mode;
  statusMessage.value = `Set service mode to '${draftPreferredMode.value}'.`;
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
          Onboarding
        </h1>
        <p class="mt-0.5 text-[13px] text-[#52525B]">
          Connect an API or MCP server, set who can use it and how it runs, then test and save it.
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
      <NuxtLink
        v-if="registeredUpstream && statusMessage.startsWith('Registered upstream')"
        :to="{ path: '/upstreams', query: { upstream: registeredUpstream } }"
        class="ml-2 inline-flex min-h-11 items-center font-medium text-teal-800 underline sm:min-h-9"
      >
        Open tool catalog
      </NuxtLink>
    </div>

    <div
      v-if="errorMessage"
      role="alert"
      class="flex items-start gap-2 rounded-lg border border-[#FECACA] bg-[#FEF2F2] px-4 py-3 text-[13px] text-[#DC2626]"
    >
      <AppIcon name="alert" :size="16" class="mt-0.5" />
      <span>{{ errorMessage }}</span>
    </div>
    <div v-if="upstreamsLoadError" role="alert" class="flex flex-col gap-2 rounded-lg border border-[#FECACA] bg-[#FEF2F2] px-4 py-3 text-[13px] text-[#991B1B] sm:flex-row sm:items-center sm:justify-between">
      <span>Could not load gateway registration settings: {{ upstreamsLoadError }}</span>
      <Button class="min-h-11" @click="loadUpstreams">Retry settings lookup</Button>
    </div>

    <section
      aria-label="Onboarding workflow steps"
      class="flex flex-col gap-3 rounded-lg border border-[#E4E4E7] bg-white p-4 sm:flex-row sm:items-center sm:justify-between"
    >
      <div class="flex flex-wrap items-center gap-2">
        <Button
          :data-variant="onboardingStep === 'connect' ? 'primary' : undefined"
          class="text-xs"
          @click="goToOnboardingStep('connect')"
        >
          1. Connect &amp; Inspect
        </Button>
        <Button
          :data-variant="onboardingStep === 'policy' ? 'primary' : undefined"
          :disabled="!schemaVerified"
          class="text-xs"
          @click="goToOnboardingStep('policy')"
        >
          2. Policy &amp; Mode
        </Button>
        <Button
          :data-variant="onboardingStep === 'publish' ? 'primary' : undefined"
          :disabled="!schemaVerified"
          class="text-xs"
          @click="goToOnboardingStep('publish')"
        >
          3. Test &amp; Save
        </Button>
      </div>
    </section>

    <div v-if="onboardingStep === 'connect'" class="rounded-lg border border-[#E4E4E7] bg-white p-5 space-y-5">
      <div class="flex flex-wrap items-center justify-between gap-2 border-b border-[#E4E4E7] pb-3">
        <div>
          <h2 class="text-[15px] font-semibold text-[#09090B]">Step 1 — Connect API or MCP Server</h2>
          <p class="text-xs text-[#52525B]">
            Connect a normal REST API and generate MCP tools, import its OpenAPI document, or connect an existing MCP server.
          </p>
        </div>
        <span class="text-xs text-[#52525B]">
          {{ dynamicPermitted ? "Dynamic Registration" : "GitOps Read-Only" }}
        </span>
      </div>

      <div class="grid grid-cols-1 gap-2 sm:grid-cols-2 lg:grid-cols-4">
        <Button
          :data-variant="sourceKind === 'openapi_url' ? 'primary' : undefined"
          class="justify-start text-xs"
          @click="sourceKind = 'openapi_url'; draftType = 'openapi'"
        >
          REST API + OpenAPI URL
        </Button>
        <Button
          :data-variant="sourceKind === 'openapi_inline' ? 'primary' : undefined"
          class="justify-start text-xs"
          @click="sourceKind = 'openapi_inline'; draftType = 'openapi'"
        >
          Paste OpenAPI document
        </Button>
        <Button
          :data-variant="sourceKind === 'rest_builder' ? 'primary' : undefined"
          class="justify-start text-xs"
          @click="sourceKind = 'rest_builder'; draftType = 'openapi'"
        >
          Define REST endpoints
        </Button>
        <Button
          :data-variant="sourceKind === 'mcp_http' ? 'primary' : undefined"
          class="justify-start text-xs"
          @click="sourceKind = 'mcp_http'; draftType = 'mcp_http'"
        >
          Existing MCP Server
        </Button>
      </div>

      <p class="text-xs leading-5 text-[#52525B]">A regular HTTP REST API is enough: provide its base URL plus an OpenAPI document, or define endpoints with the REST builder. The gateway exposes those operations as MCP tools. The API does <strong>not</strong> need to be OpenAI-compatible;</p>

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
          <Select :items="restMethodOptions" :value="ep.method" @value-change="(value) => { if (value) ep.method = String(value) }">
            <SelectTrigger aria-label="HTTP method" class="min-h-11 sm:col-span-2"><SelectValue /><AppIcon name="chevron-down" :size="14" /></SelectTrigger>
            <SelectContent><SelectItem v-for="method in restMethodOptions" :key="method.value" :value="method.value">{{ method.label }}<SelectItemIndicator><AppIcon name="check" :size="14" /></SelectItemIndicator></SelectItem></SelectContent>
          </Select>
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
          <span v-if="schemaInspectionState === 'idle'" class="text-xs text-[#D97706]">
            Inspect schema first to unlock Policy &amp; Guardrails
          </span>
          <span v-else-if="schemaInspectionState === 'inspecting'" role="status" class="text-xs text-[#52525B]">
            Checking the service schema...
          </span>
          <span v-else-if="schemaInspectionState === 'failed'" role="status" class="text-xs text-[#D97706]">
            Inspection failed. Review the error, then retry.
          </span>
          <span v-else role="status" class="text-xs text-[#0F766E]">
            Schema verified. Policy &amp; Guardrails are unlocked.
          </span>
          <Button data-variant="primary" :disabled="!schemaVerified" @click="goToOnboardingStep('policy')">
            Next: Policy &amp; Guardrails →
          </Button>
        </div>
      </div>

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

    <div v-else-if="onboardingStep === 'policy'" class="rounded-lg border border-[#E4E4E7] bg-white p-5 space-y-5">
      <div class="border-b border-[#E4E4E7] pb-3">
        <h2 class="text-[15px] font-semibold text-[#09090B]">Step 2 — Access &amp; Execution</h2>
        <p class="text-xs text-[#52525B]">
          Choose authentication, who can call these tools, and how they execute.
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

          <div v-if="draftAuthType === 'delegated_token'" class="mt-3 grid grid-cols-1 gap-3 sm:grid-cols-2">
            <Field>
              <FieldLabel>Downstream Audience</FieldLabel>
              <Input v-model="draftAudience" placeholder="api://partner-service" class="font-mono-data text-xs" />
            </Field>
            <Field>
              <FieldLabel>Downstream Scope</FieldLabel>
              <Input v-model="draftScope" placeholder="mcp.tools" class="font-mono-data text-xs" />
            </Field>
            <Field>
              <FieldLabel>Dialect Override (optional)</FieldLabel>
              <Input v-model="draftOAuthDialect" placeholder="entra_v2_obo or rfc8693" class="font-mono-data text-xs" />
            </Field>
            <Field>
              <FieldLabel>Token Endpoint Override (optional)</FieldLabel>
              <Input v-model="draftOAuthTokenEndpoint" placeholder="https://sso.company.internal/realms/ent/protocol/openid-connect/token" class="font-mono-data text-xs" />
            </Field>
          </div>
          <div v-else-if="draftAuthType === 'client_credentials' || draftAuthType === 'user_oauth'" class="mt-3 grid grid-cols-1 gap-3 sm:grid-cols-2">
            <Field v-if="draftAuthType === 'user_oauth'">
              <FieldLabel>Authorize Endpoint</FieldLabel>
              <Input v-model="draftOAuthAuthorizeEndpoint" placeholder="https://provider.example.com/oauth/authorize" class="font-mono-data text-xs" />
            </Field>
            <Field>
              <FieldLabel>Token Endpoint</FieldLabel>
              <Input v-model="draftOAuthTokenEndpoint" placeholder="https://provider.example.com/oauth/token" class="font-mono-data text-xs" />
            </Field>
            <Field>
              <FieldLabel>Client ID</FieldLabel>
              <Input v-model="draftOAuthClientId" placeholder="mcp-upstream-client" class="font-mono-data text-xs" />
            </Field>
            <Field>
              <FieldLabel>Client Secret Ref</FieldLabel>
              <Input v-model="draftOAuthClientSecretRef" placeholder="vault://my_client_secret or env://..." class="font-mono-data text-xs" />
            </Field>
            <Field class="sm:col-span-2">
              <FieldLabel>OAuth Scopes</FieldLabel>
              <Input v-model="draftOAuthScopes" placeholder="read write offline_access" class="font-mono-data text-xs" />
            </Field>
          </div>
          <div v-else-if="draftAuthType === 'api_key' || draftAuthType === 'bearer_secret' || draftAuthType === 'user_api_key'" class="mt-3 grid grid-cols-1 gap-3 sm:grid-cols-2">
            <Field v-if="draftAuthType !== 'user_api_key'">
              <FieldLabel>Secret Reference (secret_ref)</FieldLabel>
              <Input v-model="draftSecretRef" placeholder="vault://partner_key or env://PARTNER_API_KEY" class="font-mono-data text-xs" />
            </Field>
            <Field v-if="draftAuthType !== 'user_api_key'">
              <FieldLabel>Legacy Secret Env Var (optional)</FieldLabel>
              <Input v-model="draftSecretEnv" placeholder="PARTNER_API_KEY" class="font-mono-data text-xs" />
            </Field>
            <Field v-if="draftAuthType === 'api_key' || draftAuthType === 'user_api_key'">
              <FieldLabel>API Key Header</FieldLabel>
              <Input v-model="draftApiKeyHeader" placeholder="X-API-Key" class="font-mono-data text-xs" />
            </Field>
          </div>
        </Field>

        <div class="sm:col-span-2 space-y-2">
          <div class="text-xs font-medium text-[#09090B]">Allowed RBAC roles (click to toggle)</div>
          <p class="text-[11px] text-[#71717A]">Enter verified roles from your identity provider. The gateway evaluates these against the authenticated caller.</p>
          <Input v-model="draftRoles" aria-label="Allowed role names" placeholder="role_one, role_two" class="min-h-11 font-mono-data text-xs" />
          <div v-if="rolesLoading" role="status" class="text-xs text-[#71717A]">Loading configured roles and groups…</div>
          <div v-if="rolesLoadError" role="alert" class="rounded border border-[#FECACA] bg-[#FEF2F2] p-3 text-xs text-[#991B1B]">
            {{ rolesLoadError }} <Button class="ml-2 min-h-11" @click="loadRoles">Retry lookup</Button>
          </div>
          <p v-if="!rolesLoading && !rolesLoadError && availableRoles.length === 0" class="text-xs text-[#71717A]">No configured roles were found. Leave this empty to allow any authenticated caller, or configure roles before restricting access.</p>
          <div class="flex flex-wrap gap-1.5">
            <button v-for="role in availableRoles" :key="role" type="button" :aria-pressed="selectedRoleSet.has(role)" class="min-h-11 rounded border px-2.5 py-1 text-xs font-medium transition-colors focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-[#0F766E] sm:min-h-9"
              :class="selectedRoleSet.has(role) ? 'border-[#0F766E] bg-[#F0FDFA] text-[#0F766E]' : 'border-[#E4E4E7] bg-white text-[#52525B] hover:bg-[#F4F4F5]'"
              @click="toggleRole(role)">{{ role }}</button>
          </div>
          <div class="pt-1 text-xs font-medium text-[#09090B]">Allowed groups (click to toggle)</div>
          <Input v-model="draftGroups" aria-label="Allowed group names" placeholder="group_one, group_two" class="min-h-11 font-mono-data text-xs" />
          <div class="flex flex-wrap gap-1.5">
            <button v-for="g in availableGroups" :key="g" type="button" :aria-pressed="selectedGroupSet.has(g)" class="min-h-11 rounded border px-2.5 py-1 text-xs font-medium transition-colors focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-[#0F766E] sm:min-h-9"
              :class="selectedGroupSet.has(g) ? 'border-[#0F766E] bg-[#F0FDFA] text-[#0F766E]' : 'border-[#E4E4E7] bg-white text-[#52525B] hover:bg-[#F4F4F5]'"
              @click="toggleGroup(g)">{{ g }}</button>
          </div>
          <label class="flex min-h-11 items-center gap-2 text-xs text-[#52525B]">
            <Checkbox :checked="draftAllowAllCallers" aria-label="Allow all authenticated callers" @checked-change="(checked) => { draftAllowAllCallers = checked === true; if (draftAllowAllCallers) { draftRoles = ''; draftGroups = ''; } }"><CheckboxIndicator><AppIcon name="check" :size="13" /></CheckboxIndicator></Checkbox>
            Explicitly allow any authenticated caller for this upstream
          </label>
        </div>
      </div>

      <div class="space-y-3 rounded border border-[#E4E4E7] bg-[#FAFAFA] p-4">
        <div>
          <h3 class="text-sm font-semibold text-[#09090B]">Execution mode</h3>
          <p class="text-xs text-[#52525B]">Pick one mode for the whole service, or leave it on Auto and the gateway decides per tool and per request. Tool overrides below are optional.</p>
        </div>
        <Select :items="modeOptions" :value="draftPreferredMode" @value-change="(v) => { if (v) draftPreferredMode = String(v) }">
          <SelectTrigger><SelectValue /><AppIcon name="chevron-down" :size="14" /></SelectTrigger>
          <SelectContent>
            <SelectItem v-for="m in modeOptions" :key="m.value" :value="m.value">{{ m.label }}<SelectItemIndicator><AppIcon name="check" :size="14" /></SelectItemIndicator></SelectItem>
          </SelectContent>
        </Select>
        <details v-if="inspectResult" class="rounded border border-[#E4E4E7] bg-white p-3">
          <summary class="min-h-11 cursor-pointer py-2 text-xs font-semibold text-[#09090B] sm:min-h-9">Per-tool execution and confirmation ({{ inspectResult.inspection.tool_count }} tools)</summary>
          <p class="mt-1 text-xs text-[#52525B]">Tools start disabled. Enable each operation deliberately and classify its effect. Writes require caller confirmation; high impact actions require one eligible group approval. Code Mode cannot invoke gated tools.</p>
          <div class="mt-3 max-h-72 space-y-1.5 overflow-y-auto">
            <div v-for="t in inspectResult.inspection.tools" :key="t.qualified_name" class="grid grid-cols-1 items-start gap-2 border-t border-[#E4E4E7] py-3 sm:grid-cols-[minmax(0,1fr)_minmax(12rem,0.8fr)_minmax(11rem,0.7fr)]">
              <div class="min-w-0"><div class="truncate font-mono-data text-xs text-[#09090B]">{{ t.name }}</div><div class="text-[11px] text-[#71717A]">Upstream reports {{ t.mutating ? "a possible write" : "read-only behavior" }}; classify according to your review.</div></div>
              <Select :items="toolEffectOptions" :value="draftToolEffects[t.name] || 'disabled'" @value-change="(value) => { if (value) draftToolEffects[t.name] = String(value) }">
                <SelectTrigger :aria-label="`Access classification for ${t.name}`" class="min-h-11"><SelectValue /><AppIcon name="chevron-down" :size="14" /></SelectTrigger>
                <SelectContent><SelectItem v-for="effect in toolEffectOptions" :key="effect.value" :value="effect.value">{{ effect.label }}<SelectItemIndicator><AppIcon name="check" :size="14" /></SelectItemIndicator></SelectItem></SelectContent>
              </Select>
              <Select :items="toolModeOptions" :value="draftToolModes[t.name] || 'inherit'" @value-change="(value) => { if (value) draftToolModes[t.name] = String(value) }">
                <SelectTrigger :aria-label="`Execution mode for ${t.name}`" class="min-h-11"><SelectValue /><AppIcon name="chevron-down" :size="14" /></SelectTrigger>
                <SelectContent><SelectItem v-for="mode in toolModeOptions" :key="mode.value" :value="mode.value">{{ mode.label }}<SelectItemIndicator><AppIcon name="check" :size="14" /></SelectItemIndicator></SelectItem></SelectContent>
              </Select>
              <Input v-if="draftToolEffects[t.name] === 'write' || draftToolEffects[t.name] === 'high_impact'" v-model="draftToolApprovalMessages[t.name]" :aria-label="`Action summary for ${t.name}`" :placeholder="draftToolEffects[t.name] === 'write' ? `Confirm this write to ${t.name}.` : `Approve this high impact action: ${t.name}.`" maxlength="300" class="min-h-11 text-xs sm:col-span-3" />
              <Input v-if="draftToolEffects[t.name] === 'high_impact'" v-model="draftToolApprovalGroups[t.name]" :aria-label="`Approver groups for ${t.name}`" placeholder="Approver groups, comma separated" maxlength="300" class="min-h-11 text-xs sm:col-span-3" />
              <div v-if="draftToolEffects[t.name] === 'write' || draftToolEffects[t.name] === 'high_impact'" class="space-y-2 border-t border-[#E4E4E7] pt-3 sm:col-span-3">
                <label class="flex min-h-11 items-center gap-2 text-xs font-medium text-[#09090B]">
                  <Checkbox
                    :checked="draftToolRecovery[t.name]?.enabled === true"
                    :disabled="draftAuthType === 'delegated_token'"
                    :aria-label="`Enable bounded recovery for ${t.name}`"
                    @checked-change="(checked) => { draftToolRecovery[t.name].enabled = checked === true }"
                  ><CheckboxIndicator><AppIcon name="check" :size="13" /></CheckboxIndicator></Checkbox>
                  Enable durable recovery for ambiguous write outcomes
                </label>
                <p class="text-xs leading-5 text-[#52525B]">
                  Recovery requires the upstream API to honor this idempotency key for the configured retention. The gateway stores encrypted operation data and retries only this exact write. Background recovery needs a service or per-user stored credential; delegated-token-only upstreams cannot use it.
                </p>
                <div v-if="draftToolRecovery[t.name]?.enabled" class="grid grid-cols-1 gap-2 sm:grid-cols-2 lg:grid-cols-3">
                  <Field>
                    <FieldLabel>Idempotency key argument</FieldLabel>
                    <Input v-model="draftToolRecovery[t.name].keyArgument" :aria-label="`Idempotency key argument for ${t.name}`" placeholder="idempotency_key" class="min-h-11 font-mono-data text-xs" />
                    <FieldDescription>String property passed to the upstream unchanged.</FieldDescription>
                  </Field>
                  <Field>
                    <FieldLabel>Maximum synchronous wait (seconds)</FieldLabel>
                    <Input v-model.number="draftToolRecovery[t.name].synchronousBudget" type="number" min="0.2" max="30" step="0.1" class="min-h-11 tabular-nums" />
                  </Field>
                  <Field>
                    <FieldLabel>Total write attempts</FieldLabel>
                    <Input v-model.number="draftToolRecovery[t.name].maxAttempts" type="number" min="1" max="3" class="min-h-11 tabular-nums" />
                  </Field>
                  <Field>
                    <FieldLabel>Maximum recovery age (seconds)</FieldLabel>
                    <Input v-model.number="draftToolRecovery[t.name].recoveryAge" type="number" min="1" max="3600" class="min-h-11 tabular-nums" />
                  </Field>
                  <Field>
                    <FieldLabel>Upstream key retention (seconds)</FieldLabel>
                    <Input v-model.number="draftToolRecovery[t.name].keyRetention" type="number" min="1" max="86400" class="min-h-11 tabular-nums" />
                    <FieldDescription>Must cover the full recovery age.</FieldDescription>
                  </Field>
                  <Field>
                    <FieldLabel>Status lookup tool (optional)</FieldLabel>
                    <Input v-model="draftToolRecovery[t.name].statusTool" :aria-label="`Status lookup tool for ${t.name}`" placeholder="get_operation_status" class="min-h-11 font-mono-data text-xs" />
                    <FieldDescription>Configure a read-only operation that looks up the key.</FieldDescription>
                  </Field>
                  <Field v-if="draftToolRecovery[t.name].statusTool.trim()">
                    <FieldLabel>Status lookup key argument</FieldLabel>
                    <Input v-model="draftToolRecovery[t.name].statusKeyArgument" :aria-label="`Status lookup key argument for ${t.name}`" class="min-h-11 font-mono-data text-xs" />
                  </Field>
                  <Field v-if="draftToolRecovery[t.name].statusTool.trim()">
                    <FieldLabel>Status field</FieldLabel>
                    <Input v-model="draftToolRecovery[t.name].statusField" :aria-label="`Status field for ${t.name}`" class="min-h-11 font-mono-data text-xs" />
                    <FieldDescription>Expected values use gateway defaults: pending, completed, not_found, failed.</FieldDescription>
                  </Field>
                </div>
              </div>
            </div>
          </div>
        </details>
      </div>

      <section class="space-y-3 border-t border-[#E4E4E7] pt-4" aria-labelledby="upstream-limits-title">
        <div>
          <h3 id="upstream-limits-title" class="text-sm font-semibold text-[#09090B]">Upstream request limits</h3>
          <p class="text-xs text-[#52525B]">These limits apply to calls leaving each gateway deployment. With Redis configured, request rates and in-flight caps are shared across replicas. The gateway rejects excess calls with a retryable capacity response; it does not queue a burst.</p>
        </div>
        <div class="grid grid-cols-1 gap-3 sm:grid-cols-3">
          <Field>
            <FieldLabel>Request timeout (seconds)</FieldLabel>
            <Input v-model.number="draftRequestTimeout" type="number" min="0.2" max="120" step="0.1" class="min-h-11 tabular-nums" />
            <FieldDescription>Per upstream tool call, including MCP and OpenAPI.</FieldDescription>
          </Field>
          <Field>
            <FieldLabel>Maximum requests per minute</FieldLabel>
            <Input v-model.number="draftUpstreamRateLimit" type="number" min="1" max="100000" class="min-h-11 tabular-nums" />
            <FieldDescription>Shared across replicas when Redis is enabled.</FieldDescription>
          </Field>
          <Field>
            <FieldLabel>Maximum concurrent calls</FieldLabel>
            <Input v-model.number="draftUpstreamConcurrency" type="number" min="1" max="10000" class="min-h-11 tabular-nums" />
            <FieldDescription>In-flight calls; no waiting queue.</FieldDescription>
          </Field>
        </div>
      </section>

      <section class="space-y-3 border-t border-[#E4E4E7] pt-4" aria-labelledby="health-check-title">
        <div>
          <h3 id="health-check-title" class="text-sm font-semibold text-[#09090B]">Upstream health checks</h3>
          <p class="text-xs text-[#52525B]">Optional background GET probe. Configure a dedicated unauthenticated health endpoint; caller credentials are never reused. Tool calls still report their own failures.</p>
        </div>
        <label class="flex min-h-11 items-center gap-2 text-xs font-medium text-[#09090B]">
          <Checkbox :checked="draftHealthCheckEnabled" aria-label="Enable active health checks" @checked-change="(checked) => { draftHealthCheckEnabled = checked === true }"><CheckboxIndicator><AppIcon name="check" :size="13" /></CheckboxIndicator></Checkbox>
          Enable active health checks
        </label>
        <div v-if="draftHealthCheckEnabled" class="grid grid-cols-1 gap-3 sm:grid-cols-2">
          <Field class="sm:col-span-2">
            <FieldLabel>Health endpoint URL</FieldLabel>
            <Input v-model="draftHealthCheckUrl" type="url" required placeholder="https://api.example.com/health" class="min-h-11 font-mono-data text-xs" />
            <FieldDescription>Must return a successful HTTP status and must not require a user token or query-string secret.</FieldDescription>
          </Field>
          <Field>
            <FieldLabel>Probe interval (seconds)</FieldLabel>
            <Input v-model.number="draftHealthInterval" type="number" min="10" max="3600" class="min-h-11 tabular-nums" />
          </Field>
          <p class="self-center text-xs text-[#52525B]">The state changes after 3 consecutive failures and recovers after 2 consecutive successes. Probe timeout: 3 seconds.</p>
        </div>
      </section>

      <div class="flex flex-wrap items-center justify-between gap-2 border-t border-[#E4E4E7] pt-4">
        <Button @click="goToOnboardingStep('connect')">← Back: Connect</Button>
        <Button data-variant="primary" :disabled="!schemaVerified" @click="goToOnboardingStep('publish')">Next: Test &amp; Publish →</Button>
      </div>
    </div>

    <div v-else class="grid grid-cols-1 gap-6 xl:grid-cols-12">
      <section class="space-y-5 rounded-lg border border-[#E4E4E7] bg-white p-5 xl:col-span-7">
        <div class="flex items-center gap-2 border-b border-[#E4E4E7] pb-3">
          <AppIcon name="flask" :size="16" class="text-[#0F766E]" />
          <h2 class="text-[15px] font-semibold text-[#09090B]">Step 3 — Test &amp; Save</h2>
        </div>

        <div class="space-y-2 rounded border border-[#E4E4E7] bg-[#FAFAFA] p-3 text-xs leading-5 text-[#52525B]">
          <div><strong class="text-[#09090B]">What “Estimate Code Mode vs Classic” does:</strong> it measures the tool schemas of <span class="font-mono-data">{{ draftName }}</span> and the payload size you give below, then estimates how many context tokens the model would need in Classic MCP (every tool schema and result in the prompt) versus Code Mode (a short search and one script). It also suggests a mode.</div>
          <div>No model is called and nothing is sent to the service. Real per-model results appear on <strong>Model Performance</strong> once traffic flows, and Auto mode keeps adjusting from those results. This step is optional; you can save without it.</div>
        </div>

        <div class="grid grid-cols-1 gap-3 sm:grid-cols-2">
          <Field>
            <FieldLabel>Typical task (optional)</FieldLabel>
            <Input v-model="abIntent" placeholder="e.g. reconcile invoices and summarize deltas" />
          </Field>
          <Field>
            <FieldLabel>Typical result size (bytes)</FieldLabel>
            <Input v-model="abSampleBytes" inputmode="numeric" />
          </Field>
        </div>

        <div class="flex flex-wrap items-center justify-between gap-2 border-t border-[#E4E4E7] pt-4">
          <Button @click="goToOnboardingStep('policy')">← Back: Policy</Button>
          <div class="flex flex-wrap items-center gap-2">
            <Button :disabled="abBusy" pending-announcement="Estimating" @click="runAbTest">
              <AppIcon name="flask" :size="15" />
              <span>Estimate Code Mode vs Classic</span>
            </Button>
            <Button v-if="dynamicPermitted" data-variant="primary" :disabled="!approvalPoliciesValid || !accessPolicyValid || rolesLoading || Boolean(rolesLoadError)" data-agent-confirm="Register or update this upstream service in the gateway?" pending-announcement="Registering upstream" @click="handleRegister">
              Save Upstream
            </Button>
            <Button @click="copyYaml">
              <AppIcon name="copy" :size="14" />
              <span>Copy GitOps YAML</span>
            </Button>
          </div>
        </div>
      </section>

      <section class="space-y-4 rounded-lg border border-[#E4E4E7] bg-white p-5 xl:col-span-5">
        <h3 class="text-sm font-semibold text-[#09090B]">Configuration preview</h3>
        <pre class="max-h-56 overflow-auto rounded border border-[#E4E4E7] bg-[#F4F4F5] p-3 font-mono-data text-xs text-[#09090B]">{{ yamlPreview }}</pre>

        <div v-if="abResult" class="space-y-4 border-t border-[#E4E4E7] pt-4">
          <div class="rounded-md border border-[#99F6E4] bg-[#F0FDFA] p-3">
            <div class="flex items-center justify-between gap-2">
              <span class="text-xs font-medium text-[#0F766E]">Suggested mode</span>
              <span class="font-mono-data text-sm font-semibold text-[#09090B]">{{ abResult.recommendation.recommended_mode }} ({{ abResult.recommendation.confidence }})</span>
            </div>
            <p class="mt-1 text-xs text-[#52525B]">{{ abResult.algorithmic.reason }}</p>
            <div class="mt-3"><Button data-variant="primary" class="text-xs" @click="applyRecommendation">Use as service mode</Button></div>
          </div>
          <ul v-if="abResult.algorithmic.factors.length" class="space-y-1 text-xs text-[#52525B]">
            <li v-for="f in abResult.algorithmic.factors" :key="f.name" class="flex justify-between gap-2">
              <span>{{ f.detail }}</span>
              <span class="shrink-0 font-mono-data font-medium tabular-nums" :class="f.delta >= 0 ? 'text-[#0F766E]' : 'text-[#2563EB]'">{{ f.delta >= 0 ? `+${f.delta}` : f.delta }}</span>
            </li>
          </ul>
          <div v-if="abResult.replay_ab" class="space-y-1.5 border-t border-[#E4E4E7] pt-3 text-xs">
            <div class="flex items-center justify-between font-semibold text-[#09090B]">
              <span>Estimated context per task</span>
              <span class="tabular-nums text-[#0F766E]">{{ abResult.replay_ab.deltas.context_token_savings_pct }}% fewer tokens with {{ abResult.replay_ab.winner }}</span>
            </div>
            <div class="flex justify-between tabular-nums text-[#52525B]">
              <span class="text-[#2563EB]">Classic: {{ abResult.replay_ab.classic.total_context_tokens }} tok</span>
              <span class="text-[#0F766E]">Code Mode: {{ abResult.replay_ab.code_mode.total_context_tokens }} tok</span>
            </div>
          </div>
        </div>
      </section>
    </div>

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
