<script setup lang="ts">
import { Switch, SwitchThumb } from "@ui-sdk/vue/switch";
import { computed, onMounted, ref, watch } from "vue";
import McpAppHostPreview from "~/components/McpAppHostPreview.vue";
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
    onboarding_guidance?: {
      auth_discovery?: {
        status: string;
        source: string;
        authorization_mode: string;
        requires_operator_confirmation: boolean;
        security_schemes: { name?: string; type?: string; header_name?: string }[];
      };
    };
    tools: {
      qualified_name: string;
      name: string;
      schema_sha256: string;
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

type OnboardingStep = "connect" | "policy" | "workflows" | "publish";
type SchemaInspectionState = "idle" | "inspecting" | "verified" | "failed";

interface WorkflowDraft {
  id: string;
  name: string;
  description: string;
  tools: string[];
  requiredTools: string[];
  evidencePredicates: Record<string, { path: string; type: string }>;
  completionTool: string;
  completionPath: string;
  completionType: string;
  manualMinutes: number;
  baselineSource: string;
  confidence: string;
  effectiveFrom: string;
  evidenceNote: string;
}

interface ExistingWorkflowSummary {
  id: string;
  name: string;
  description: string;
  participating_tools: string[];
  required_evidence: { tool: string }[];
  completion: { tool: string };
  business_value: { manual_baseline_minutes: number; baseline_source: string; confidence: string };
}

function newWorkflowDraft(index: number): WorkflowDraft {
  return {
    id: `workflow_${index}`,
    name: `Workflow ${index}`,
    description: "",
    tools: [],
    requiredTools: [],
    evidencePredicates: {},
    completionTool: "",
    completionPath: "/status",
    completionType: "non_empty_string",
    manualMinutes: 5,
    baselineSource: "administrator_estimate",
    confidence: "unvalidated",
    effectiveFrom: "",
    evidenceNote: "",
  };
}

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
const draftAuthorizationMode = ref("gateway");
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
const workflowDrafts = ref<WorkflowDraft[]>([newWorkflowDraft(1)]);
const workflowDraftSequence = ref(1);
const existingWorkflows = ref<ExistingWorkflowSummary[]>([]);
const existingWorkflowLoadError = ref("");
const workflowSaveBusy = ref(false);
const workflowPublishMessage = ref("");
const authDiscoverySummary = computed(() => {
  const discovery = inspectResult.value?.inspection.onboarding_guidance?.auth_discovery;
  if (!discovery) return "";
  const schemes = discovery.security_schemes
    .map((scheme) => `${scheme.name}: ${scheme.type}${scheme.header_name ? `, ${scheme.header_name}` : ""}`)
    .join("; ");
  const evidence = schemes ? ` (${schemes})` : "";
  return `Authentication evidence: ${discovery.status}${evidence}. A declared API key does not tell whether it is personal or shared. Confirm the credential owner and authorization mode below.`;
});

const abIntent = ref("reconcile inventory and summarize anomalies");
const abSampleBytes = ref("12000");

const abResult = ref<AbTestResult | null>(null);
const abBusy = ref(false);

const promptDialogOpen = ref(false);
const agentPromptText = ref("");
const importDialogOpen = ref(false);
const importJsonText = ref("");
const bundleBusy = ref(false);
const bundleValidationPreview = ref<Record<string, unknown> | null>(null);
const discoveredCandidates = ref<Array<Record<string, any>>>([]);
const discoverBusy = ref(false);

const showManualBuilder = ref(false);
const promptTargetSelection = ref("__new__");
const targetChoiceItems = computed(() => [
  { value: "__new__", label: "New API or MCP server (configure target)" },
  ...existingUpstreams.value.map((name) => ({
    value: name,
    label: `Existing upstream: ${name}`,
  })),
]);
const targetKindItems = [
  { value: "openapi", label: "REST API (OpenAPI / Docs)" },
  { value: "mcp_http", label: "Existing MCP Server" },
];
const generatedPrompt = ref("");
const promptLoading = ref(false);
const agentBundleJsonText = ref("");
const bundleValidating = ref(false);
const bundleApplying = ref(false);
const bundleApplySuccess = ref<{
  upstreamName: string;
  workflows: number;
  experiences: number;
} | null>(null);
const bundlePreview = ref<{
  valid: boolean;
  upstream: Record<string, any>;
  integration_definition: Record<string, any> | null;
  warnings: string[];
  parsedBundle: Record<string, any>;
  editableWorkflows: Array<{
    id: string;
    name: string;
    description: string;
    enabled: boolean;
    manualMinutes: number;
    intentsText: string;
    completionTool: string;
    participatingTools: string[];
  }>;
  experiencePreviews: Array<{
    id: string;
    title: string;
    tool: string;
    resource_uri: string;
    html: string;
    error?: string;
  }>;
} | null>(null);

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
const authorizationModeOptions = computed(() => {
  const options = [{ value: "gateway", label: "Gateway role/group policy" }];
  if (["delegated_token", "user_oauth", "user_api_key"].includes(draftAuthType.value)) {
    options.unshift({ value: "upstream", label: "Upstream authorizes each user" });
  }
  if (["client_credentials", "api_key", "bearer_secret"].includes(draftAuthType.value)) {
    options.unshift({ value: "shared_access", label: "Shared access for admitted callers" });
  }
  return options;
});

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
const accessPolicyValid = computed(() => {
  if (draftAuthorizationMode.value === "upstream") return true;
  if (draftAuthorizationMode.value === "shared_access") return draftAllowAllCallers.value;
  return draftAllowAllCallers.value || selectedRoleSet.value.size > 0 || selectedGroupSet.value.size > 0;
});
const testCallResult = ref<string>("");
const testCallBusy = ref(false);
const availableRoles = ref<string[]>([]);

function selectAuthType(value: string) {
  draftAuthType.value = value;
  draftRoles.value = "";
  draftGroups.value = "";
  if (["delegated_token", "user_oauth", "user_api_key"].includes(value)) {
    draftAuthorizationMode.value = "upstream";
    draftAllowAllCallers.value = false;
  } else if (["client_credentials", "api_key", "bearer_secret"].includes(value)) {
    draftAuthorizationMode.value = "shared_access";
    draftAllowAllCallers.value = true;
  } else {
    draftAuthorizationMode.value = "gateway";
    draftAllowAllCallers.value = false;
  }
}

function selectAuthorizationMode(value: string) {
  draftAuthorizationMode.value = value;
  draftRoles.value = "";
  draftGroups.value = "";
  draftAllowAllCallers.value = value === "shared_access";
}

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
    return "Exchanges the caller's gateway token for a token scoped to this upstream. The upstream authorizes the user.";
  }
  if (draftAuthType.value === "client_credentials") {
    return "Uses a shared machine identity. Every authenticated gateway caller may use the enabled operations.";
  }
  if (draftAuthType.value === "user_oauth") {
    return "Prompts each user once to authorize via OAuth 2.0 PKCE and stores encrypted refresh tokens per user.";
  }
  if (draftAuthType.value === "user_api_key") {
    return "Each user connects a personal API key. The gateway stores it encrypted per tenant, user, and upstream, then injects it only for that user's calls.";
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
];
const restMethodOptions = [
  { value: "get", label: "GET" },
  { value: "post", label: "POST" },
  { value: "put", label: "PUT" },
  { value: "delete", label: "DELETE" },
];

function toolApprovalPoliciesPayload(): Record<string, { mode: string; version: number; confirmation_message?: string }> {
  const policies: Record<string, { mode: string; version: number; confirmation_message?: string }> = {};
  for (const [tool, effect] of Object.entries(draftToolEffects.value)) {
    const message = draftToolApprovalMessages.value[tool]?.trim();
    if (effect === "write") {
      policies[tool] = { mode: "requester_confirmation", version: 1, confirmation_message: message || `Confirm this write to ${tool}.` };
    }
  }
  return policies;
}

function toolRecoveryPoliciesPayload() {
  return Object.fromEntries(Object.entries(draftToolRecovery.value)
    .filter(([tool, recovery]) => recovery.enabled && draftToolEffects.value[tool] === "write")
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
  const usesSharedSecret = ["api_key", "bearer_secret"].includes(draftAuthType.value);

  return {
    name: draftName.value.trim(),
    type: resolvedType,
    url: draftUrl.value.trim() || undefined,
    openapi_spec_url: specUrl,
    openapi_spec: inlineSpec,
    auth_type: draftAuthType.value,
    authorization_mode: draftAuthorizationMode.value,
    downstream_audience: draftAudience.value.trim() || undefined,
    downstream_scope: draftScope.value.trim() || undefined,
    secret_env_var: usesSharedSecret ? draftSecretEnv.value.trim() || undefined : undefined,
    secret_ref: usesSharedSecret ? draftSecretRef.value.trim() || undefined : undefined,
    api_key_header: draftApiKeyHeader.value.trim() || "X-API-Key",
    oauth: hasOAuthFields && ["delegated_token", "client_credentials", "user_oauth"].includes(draftAuthType.value)
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
    mutating_tools: forInspection ? [] : Object.entries(draftToolEffects.value).filter(([, effect]) => effect === "write").map(([tool]) => tool),
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
    `    authorization_mode: ${cfg.authorization_mode}`,
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
    const route = typeof useRoute === "function" ? useRoute() : null;
    const queryUpstream =
      typeof route?.query?.upstream === "string" ? route.query.upstream.trim() : "";
    if (queryUpstream) {
      if (existingUpstreams.value.includes(queryUpstream)) {
        promptTargetSelection.value = queryUpstream;
      }
      draftName.value = queryUpstream;
    }
  } catch (err) {
    dynamicPermitted.value = false;
    upstreamsLoadError.value = extractErrorMessage(err);
  }
}

onMounted(() => {
  void loadUpstreams();
});

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
    existingWorkflows.value = [];
    existingWorkflowLoadError.value = "";
    try {
      const definitions = await apiFetch<{ current?: { definition?: { workflows?: ExistingWorkflowSummary[] } } | null }>(
        `/admin/integration-definitions/${encodeURIComponent(draftName.value.trim())}`,
      );
      existingWorkflows.value = definitions.current?.definition?.workflows || [];
    } catch (err) {
      existingWorkflowLoadError.value = extractErrorMessage(err);
    }
    const today = new Date().toISOString().slice(0, 10);
    for (const workflow of workflowDrafts.value) {
      if (!workflow.effectiveFrom) workflow.effectiveFrom = today;
    }
    schemaInspectionState.value = "verified";
    const inspectedName =
      inspectResult.value.inspection.name ||
      inspectResult.value.inspection["upstream"] ||
      draftName.value.trim();
    statusMessage.value = `Inspected '${inspectedName}' (${inspectResult.value.inspection.tool_count} tools). Step 2 unlocked.`;
    draftToolEffects.value = Object.fromEntries(inspectResult.value.inspection.tools.map((tool) => [tool.name, "disabled"]));
    draftToolModes.value = {};
    draftToolApprovalMessages.value = {};
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
    draftToolEffects.value[tool] !== "write" ||
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
  if (draftAuthorizationMode.value === "gateway" && rolesLoadError.value) {
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

function addWorkflowDraft() {
  workflowDraftSequence.value += 1;
  workflowDrafts.value.push(newWorkflowDraft(workflowDraftSequence.value));
}

function removeWorkflowDraft(workflowId: string) {
  if (workflowDrafts.value.length > 1) {
    workflowDrafts.value = workflowDrafts.value.filter((workflow) => workflow.id !== workflowId);
  }
}

function toggleWorkflowTool(workflow: WorkflowDraft, toolName: string) {
  if (workflow.tools.includes(toolName)) {
    workflow.tools = workflow.tools.filter((name) => name !== toolName);
    workflow.requiredTools = workflow.requiredTools.filter((name) => name !== toolName);
    const predicates = { ...workflow.evidencePredicates };
    delete predicates[toolName];
    workflow.evidencePredicates = predicates;
    if (workflow.completionTool === toolName) workflow.completionTool = "";
    return;
  }
  workflow.tools = [...workflow.tools, toolName];
}

function moveWorkflowTool(workflow: WorkflowDraft, index: number, direction: -1 | 1) {
  const destination = index + direction;
  if (destination < 0 || destination >= workflow.tools.length) return;
  const tools = [...workflow.tools];
  [tools[index], tools[destination]] = [tools[destination], tools[index]];
  workflow.tools = tools;
}

function setWorkflowBaselineSource(workflow: WorkflowDraft, source: string) {
  workflow.baselineSource = source;
  workflow.confidence = source === "administrator_estimate"
    ? "unvalidated"
    : source === "sampled_manual_measurement" ? "sampled" : "validated";
}

function toggleEvidencePredicate(workflow: WorkflowDraft, toolName: string, enabled: boolean) {
  const predicates = { ...workflow.evidencePredicates };
  if (enabled) predicates[toolName] = { path: "/status", type: "non_empty_string" };
  else delete predicates[toolName];
  workflow.evidencePredicates = predicates;
}

function workflowDefinitionPayload() {
  if (!inspectResult.value) throw new Error("Inspect the tool catalog before defining workflows.");
  if (existingWorkflowLoadError.value) throw new Error("Retry schema inspection so the current published workflows can be preserved before saving.");
  const inspectedTools = inspectResult.value.inspection.tools;
  const toolsByName = new Map(inspectedTools.map((tool) => [tool.qualified_name, tool]));
  const cleanId = (value: string) => value.toLowerCase().replace(/[^a-z0-9]+/g, "_").replace(/^_+|_+$/g, "").slice(0, 48);
  const workflows = workflowDrafts.value.map((workflow, index) => {
    const id = cleanId(workflow.name) || `workflow_${index + 1}`;
    if (!workflow.name.trim() || !workflow.description.trim()) throw new Error("Give each workflow a name and explain what business activity it measures.");
    if (!workflow.tools.length) throw new Error(`Add at least one catalog tool to '${workflow.name}'.`);
    if (!workflow.completionTool || !workflow.tools.includes(workflow.completionTool)) throw new Error(`Choose a terminal outcome tool that appears in '${workflow.name}'.`);
    if (!workflow.completionPath.trim()) throw new Error(`Choose the result field that confirms '${workflow.name}' completed.`);
    if (!Number.isFinite(workflow.manualMinutes) || workflow.manualMinutes <= 0) throw new Error(`Enter a human baseline greater than zero minutes for '${workflow.name}'.`);
    if (!workflow.effectiveFrom) throw new Error(`Choose when the baseline for '${workflow.name}' takes effect.`);
    const requiredEvidence = workflow.requiredTools.map((tool) => ({
      tool,
      min_successful_calls: 1,
      ...(workflow.evidencePredicates[tool] ? { result_when: workflow.evidencePredicates[tool] } : {}),
    }));
    return {
      id,
      name: workflow.name.trim(),
      description: workflow.description.trim(),
      participating_tools: workflow.tools,
      required_evidence: requiredEvidence,
      completion: {
        kind: "terminal_tool_result",
        tool: workflow.completionTool,
        success_when: {
          path: workflow.completionPath.trim(),
          type: workflow.completionType,
          ...(workflow.completionType === "boolean" ? { equals: true } : {}),
        },
      },
      business_value: {
        manual_baseline_minutes: workflow.manualMinutes,
        baseline_source: workflow.baselineSource,
        confidence: workflow.confidence,
        effective_from: workflow.effectiveFrom,
        evidence_note: workflow.evidenceNote.trim() || undefined,
      },
    };
  });
  if (new Set(workflows.map((workflow) => workflow.id)).size !== workflows.length) {
    throw new Error("Workflow names must produce unique IDs. Give each workflow a distinct name.");
  }
  const referencedNames = [...new Set(workflows.flatMap((workflow) => workflow.participating_tools))];
  const toolRefs = referencedNames.map((name) => {
    const tool = toolsByName.get(name);
    if (!tool) throw new Error(`Tool '${name}' is no longer in the inspected catalog. Inspect again.`);
    return { name, schema_sha256: tool.schema_sha256 };
  });
  const integrationId = cleanId(draftName.value) || "integration";
  return {
    manifest_version: "1.0",
    integration: {
      id: integrationId,
      display_name: draftName.value.trim(),
      upstream_name: draftName.value.trim(),
      status: "draft",
      revision: 1,
    },
    tool_refs: toolRefs,
    experiences: [],
    workflows,
    standalone_tool_values: [],
  };
}

async function handleSaveWorkflows() {
  workflowSaveBusy.value = true;
  workflowPublishMessage.value = "";
  errorMessage.value = "";
  try {
    const definition = workflowDefinitionPayload();
    await handleRegister();
    if (!registeredUpstream.value) throw new Error(errorMessage.value || "Save the upstream before publishing its workflows.");
    const current = await apiFetch<{ current?: { definition?: Record<string, unknown> } | null }>(
      `/admin/integration-definitions/${encodeURIComponent(draftName.value.trim())}`,
    );
    const currentDefinition = current.current?.definition || {};
    const publishedDefinition = { ...currentDefinition };
    delete publishedDefinition.publication;
    const existingWorkflows = Array.isArray(currentDefinition.workflows) ? currentDefinition.workflows as { id?: string }[] : [];
    const authoredIds = new Set(definition.workflows.map((workflow) => workflow.id));
    const mergedWorkflows = [
      ...existingWorkflows.filter((workflow) => !workflow.id || !authoredIds.has(workflow.id)),
      ...definition.workflows,
    ];
    const existingRefs = Array.isArray(currentDefinition.tool_refs) ? currentDefinition.tool_refs as { name: string; schema_sha256: string }[] : [];
    const refsByName = new Map(existingRefs.map((reference) => [reference.name, reference]));
    for (const reference of definition.tool_refs) refsByName.set(reference.name, reference);
    const existingIntegration = currentDefinition.integration && typeof currentDefinition.integration === "object"
      ? currentDefinition.integration as { id?: string }
      : {};
    const completeDefinition = {
      ...publishedDefinition,
      ...definition,
      integration: { ...definition.integration, ...(existingIntegration.id ? { id: existingIntegration.id } : {}) },
      tool_refs: [...refsByName.values()],
      workflows: mergedWorkflows,
    };
    const validation = await apiFetch<{ valid: boolean }>("/admin/integration-definitions/validate", {
      method: "POST",
      body: completeDefinition,
    });
    if (!validation.valid) throw new Error("The gateway could not validate this workflow definition.");
    const draft = await apiFetch<{ revision: string }>("/admin/integration-definitions/drafts", {
      method: "POST",
      body: { definition: completeDefinition },
    });
    await apiFetch(`/admin/integration-definitions/${encodeURIComponent(draftName.value.trim())}/publish`, {
      method: "POST",
      body: { revision: draft.revision },
    });
    workflowPublishMessage.value = `Published ${definition.workflows.length} workflow${definition.workflows.length === 1 ? "" : "s"} for ${draftName.value.trim()}.`;
    statusMessage.value = workflowPublishMessage.value;
  } catch (err) {
    errorMessage.value = extractErrorMessage(err);
  } finally {
    workflowSaveBusy.value = false;
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
    const query = draftName.value.trim()
      ? `?upstream_name=${encodeURIComponent(draftName.value.trim())}`
      : "";
    const res = await apiFetch<{ prompt: string }>(`/admin/onboarding/prompt-pack${query}`);
    agentPromptText.value = res.prompt;
    promptDialogOpen.value = true;
  } catch (err) {
    errorMessage.value = extractErrorMessage(err);
  }
}

async function discoverWorkflowCandidates() {
  if (!draftName.value.trim()) return;
  errorMessage.value = "";
  discoverBusy.value = true;
  try {
    const res = await apiFetch<{ candidates?: Array<Record<string, any>> }>(
      `/admin/integration-definitions/${encodeURIComponent(draftName.value.trim())}/discover`,
      { method: "POST", body: {} },
    );
    discoveredCandidates.value = res.candidates ?? [];
    if (discoveredCandidates.value.length) {
      statusMessage.value = `Discovered ${discoveredCandidates.value.length} workflow candidate(s) for '${draftName.value}'.`;
    } else {
      statusMessage.value = "No new workflow candidates were synthesized from current schemas/runs.";
    }
  } catch (err) {
    errorMessage.value = extractErrorMessage(err);
  } finally {
    discoverBusy.value = false;
  }
}

function loadDiscoveredCandidateIntoDraft(candidate: Record<string, any>) {
  const draft = (candidate.draft_definition ?? {}) as Record<string, any>;
  workflowDraftSequence.value += 1;
  const reqTools = Array.isArray(draft.required_evidence) ? draft.required_evidence : [];
  const next: WorkflowDraft = {
    id: `workflow_${workflowDraftSequence.value}`,
    name: String(draft.name ?? candidate.workflow_slug ?? ""),
    description: String(draft.description ?? ""),
    tools: Array.isArray(draft.participating_tools) ? draft.participating_tools.map(String) : [],
    requiredTools: reqTools.map((r: any) => String(r.tool)),
    evidencePredicates: Object.fromEntries(
      reqTools
        .filter((r: any) => r.result_when)
        .map((r: any) => [String(r.tool), { path: String(r.result_when.path), type: String(r.result_when.type) }]),
    ),
    completionTool: String(draft.completion?.tool ?? ""),
    completionPath: String(draft.completion?.success_when?.path ?? "/status"),
    completionType: String(draft.completion?.success_when?.type ?? "non_empty_string"),
    manualMinutes: 10,
    baselineSource: "administrator_estimate",
    confidence: "unvalidated",
    effectiveFrom: new Date().toISOString().slice(0, 10),
    evidenceNote: "Reviewed from auto-discovered workflow candidate.",
  };
  workflowDrafts.value = [next];
  statusMessage.value = `Loaded '${next.name}' into the workflow builder. Confirm human minutes before publishing.`;
}

async function applyAgentBundleJson(publishImmediately = true) {
  errorMessage.value = "";
  bundleBusy.value = true;
  try {
    const parsed = JSON.parse(importJsonText.value) as Record<string, unknown>;
    if ("upstream" in parsed || "integration_definition" in parsed) {
      const payload = { ...parsed, publish: publishImmediately };
      const preview = await apiFetch<Record<string, unknown>>("/admin/onboarding/bundle/validate", {
        method: "POST",
        body: payload,
      });
      bundleValidationPreview.value = preview;
      const applied = await apiFetch<Record<string, any>>("/admin/onboarding/bundle/apply", {
        method: "POST",
        body: payload,
      });
      if (applied.upstream?.upstream?.name) draftName.value = String(applied.upstream.upstream.name);
      importDialogOpen.value = false;
      statusMessage.value = `Applied agent onboarding bundle${applied.integration_definition?.published ? " and published definition" : ""}.`;
      await loadUpstreams();
      return;
    }
    applyImportedJson();
  } catch (err) {
    errorMessage.value = extractErrorMessage(err);
  } finally {
    bundleBusy.value = false;
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
    if (typeof parsed.authorization_mode === "string") draftAuthorizationMode.value = parsed.authorization_mode;
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
    if (typeof parsed.allow_all_callers === "boolean") {
      draftAllowAllCallers.value = parsed.allow_all_callers;
    }
    if (draftAuthorizationMode.value === "upstream") {
      draftRoles.value = "";
      draftGroups.value = "";
      draftAllowAllCallers.value = false;
    } else if (draftAuthorizationMode.value === "shared_access") {
      draftRoles.value = "";
      draftGroups.value = "";
      draftAllowAllCallers.value = true;
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

async function generateGuidedPrompt(copyAfter = false) {
  errorMessage.value = "";
  promptLoading.value = true;
  try {
    const selectedExisting =
      promptTargetSelection.value !== "__new__" ? promptTargetSelection.value : "";
    const targetName = selectedExisting || draftName.value.trim();
    const query = targetName ? `?upstream_name=${encodeURIComponent(targetName)}` : "";
    const res = await apiFetch<{ prompt: string }>(`/admin/onboarding/prompt-pack${query}`);
    const contextLines: string[] = [];
    if (selectedExisting) {
      contextLines.push(`Target Upstream (already registered): ${selectedExisting}`);
    } else {
      if (targetName) contextLines.push(`Target Upstream Name: ${targetName}`);
      contextLines.push(`Target Integration Type: ${draftType.value}`);
      if (draftUrl.value.trim()) {
        contextLines.push(`Target API / MCP Base URL: ${draftUrl.value.trim()}`);
      }
      if (draftType.value === "openapi" && draftSpecUrl.value.trim()) {
        contextLines.push(`OpenAPI / Documentation URL: ${draftSpecUrl.value.trim()}`);
      }
    }
    generatedPrompt.value = contextLines.length
      ? `${contextLines.join("\n")}\n\n${res.prompt}`
      : res.prompt;
    agentPromptText.value = generatedPrompt.value;
    if (copyAfter && typeof navigator !== "undefined" && navigator.clipboard) {
      await navigator.clipboard.writeText(generatedPrompt.value);
      statusMessage.value =
        "Copied clarifying-question prompt to clipboard. Send it to your frontier agent, answer its questions, then paste its JSON below.";
    } else {
      statusMessage.value = "Generated clarifying-question onboarding prompt.";
    }
  } catch (err) {
    errorMessage.value = extractErrorMessage(err);
  } finally {
    promptLoading.value = false;
  }
}

async function validateAndPreviewBundle() {
  errorMessage.value = "";
  bundleApplySuccess.value = null;
  bundleValidating.value = true;
  try {
    const parsed = JSON.parse(agentBundleJsonText.value) as Record<string, any>;
    const payload = { ...parsed, publish: true };
    const validated = await apiFetch<{
      valid: boolean;
      upstream: Record<string, any>;
      integration_definition: Record<string, any> | null;
      warnings?: string[];
    }>("/admin/onboarding/bundle/validate", {
      method: "POST",
      body: payload,
    });
    const def = validated.integration_definition;
    const rawWorkflows = Array.isArray(def?.workflows) ? def.workflows : [];
    const editableWorkflows = rawWorkflows.map((wf: Record<string, any>) => ({
      id: String(wf.id ?? ""),
      name: String(wf.name ?? wf.id ?? ""),
      description: String(wf.description ?? ""),
      enabled: wf.enabled !== false,
      manualMinutes: Number(wf.business_value?.manual_baseline_minutes ?? 10),
      intentsText: Array.isArray(wf.intent_examples) ? wf.intent_examples.join(", ") : "",
      completionTool: String(wf.completion?.tool ?? ""),
      participatingTools: Array.isArray(wf.participating_tools)
        ? wf.participating_tools.map((t: any) => String(t))
        : [],
    }));
    const rawExperiences = Array.isArray(def?.experiences) ? def.experiences : [];
    const experiencePreviews: Array<{
      id: string;
      title: string;
      tool: string;
      resource_uri: string;
      html: string;
      error?: string;
    }> = [];
    for (const exp of rawExperiences) {
      const headingNode = Array.isArray(exp.components)
        ? exp.components.find((c: any) => c?.type === "heading" && c?.text)
        : null;
      const expTitle = String(exp.title ?? headingNode?.text ?? exp.id ?? "MCP App UI");
      try {
        const preview = await apiFetch<{ html: string }>("/admin/mcp-ui/preview", {
          method: "POST",
          body: { experience: exp },
        });
        experiencePreviews.push({
          id: String(exp.id ?? ""),
          title: expTitle,
          tool: String(exp.tool ?? ""),
          resource_uri: String(exp.resource_uri ?? ""),
          html: preview.html,
        });
      } catch (err) {
        experiencePreviews.push({
          id: String(exp.id ?? ""),
          title: expTitle,
          tool: String(exp.tool ?? ""),
          resource_uri: String(exp.resource_uri ?? ""),
          html: "",
          error: extractErrorMessage(err),
        });
      }
    }
    bundlePreview.value = {
      valid: true,
      upstream: validated.upstream,
      integration_definition: def,
      warnings: validated.warnings ?? [],
      parsedBundle: parsed,
      editableWorkflows,
      experiencePreviews,
    };
    statusMessage.value = `Validated bundle for '${validated.upstream?.name}'. Review workflows and MCP App UI previews below, then click Apply.`;
  } catch (err) {
    bundlePreview.value = null;
    errorMessage.value = extractErrorMessage(err);
  } finally {
    bundleValidating.value = false;
  }
}

async function applyGuidedBundle() {
  if (!bundlePreview.value) return;
  errorMessage.value = "";
  bundleApplying.value = true;
  try {
    const bundleToSend = JSON.parse(
      JSON.stringify(bundlePreview.value.parsedBundle),
    ) as Record<string, any>;
    if (
      bundleToSend.integration_definition &&
      Array.isArray(bundleToSend.integration_definition.workflows)
    ) {
      const editsById = new Map(
        bundlePreview.value.editableWorkflows.map((item) => [item.id, item]),
      );
      bundleToSend.integration_definition.workflows =
        bundleToSend.integration_definition.workflows.map((wf: Record<string, any>) => {
          const edited = editsById.get(String(wf.id));
          if (!edited) return wf;
          const nextBv = {
            ...(wf.business_value ?? {}),
            manual_baseline_minutes: Math.max(0.1, Number(edited.manualMinutes) || 10),
          };
          const parsedIntents = edited.intentsText
            .split(",")
            .map((s) => s.trim())
            .filter(Boolean);
          return {
            ...wf,
            enabled: edited.enabled,
            business_value: nextBv,
            ...(parsedIntents.length ? { intent_examples: parsedIntents } : {}),
          };
        });
    }
    const res = await apiFetch<{
      applied: boolean;
      upstream?: { upstream?: { name?: string } };
      integration_definition?: { published?: boolean };
    }>("/admin/onboarding/bundle/apply", {
      method: "POST",
      body: { ...bundleToSend, publish: true },
    });
    const upstreamName = String(
      res.upstream?.upstream?.name ?? bundlePreview.value.upstream?.name ?? "",
    );
    bundleApplySuccess.value = {
      upstreamName,
      workflows: bundlePreview.value.editableWorkflows.length,
      experiences: bundlePreview.value.experiencePreviews.length,
    };
    statusMessage.value = `Applied and published '${upstreamName}' with ${bundleApplySuccess.value.workflows} workflow(s) and ${bundleApplySuccess.value.experiences} MCP App UI view(s).`;
    await loadUpstreams();
  } catch (err) {
    errorMessage.value = extractErrorMessage(err);
  } finally {
    bundleApplying.value = false;
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
          Copy a structured prompt to your agent so it asks clarifying questions, discovers workflows, designs MCP Apps UI, and outputs one JSON bundle to apply.
        </p>
      </div>
      <div class="flex flex-wrap items-center gap-2">
        <Button @click="showManualBuilder = !showManualBuilder">
          {{ showManualBuilder ? "Hide Manual Builder" : "Manual Step-by-Step Builder" }}
        </Button>
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

    <section aria-label="Prompt-first guided onboarding" class="space-y-5 rounded-lg border border-[#E4E4E7] bg-white p-5">
      <div class="border-b border-[#E4E4E7] pb-3">
        <h2 class="text-[16px] font-semibold text-[#09090B]">Prompt-First Agent Onboarding</h2>
        <p class="mt-0.5 text-[12px] text-[#52525B]">
          Choose what API or MCP server you are onboarding, copy the prompt to your frontier agent, answer its clarifying questions, then paste and validate the resulting JSON bundle.
        </p>
      </div>

      <div class="space-y-4">
        <div class="flex flex-wrap items-center justify-between gap-2">
          <div>
            <h3 class="text-[14px] font-semibold text-[#09090B]">Step 1 — Choose Target &amp; Copy Agent Prompt</h3>
            <p class="text-[12px] text-[#52525B]">
              Pick an existing upstream or specify a new REST API / MCP server so your prompt targets the right service.
            </p>
          </div>
          <div class="flex flex-wrap items-center gap-2">
            <Button class="min-h-11 sm:min-h-9" :disabled="promptLoading" @click="generateGuidedPrompt(false)">
              {{ promptLoading ? "Building…" : "Preview Prompt" }}
            </Button>
            <Button data-variant="primary" class="min-h-11 sm:min-h-9" :disabled="promptLoading" @click="generateGuidedPrompt(true)">
              <AppIcon name="copy" :size="14" />
              <span>Copy Onboarding Prompt</span>
            </Button>
          </div>
        </div>
        <div class="grid grid-cols-1 gap-3 sm:grid-cols-2 lg:grid-cols-4">
          <Field>
            <FieldLabel>Target source</FieldLabel>
            <Select
              :items="targetChoiceItems"
              :value="promptTargetSelection"
              @value-change="(val) => { if (val) { promptTargetSelection = String(val); if (val !== '__new__') draftName = String(val); } }"
            >
              <SelectTrigger aria-label="Choose target integration" class="min-h-11 sm:min-h-9">
                <SelectValue />
                <AppIcon name="chevron-down" :size="14" />
              </SelectTrigger>
              <SelectContent>
                <SelectItem v-for="opt in targetChoiceItems" :key="opt.value" :value="opt.value">
                  {{ opt.label }}
                  <SelectItemIndicator><AppIcon name="check" :size="14" /></SelectItemIndicator>
                </SelectItem>
              </SelectContent>
            </Select>
          </Field>
          <template v-if="promptTargetSelection === '__new__'">
            <Field>
              <FieldLabel>Integration kind</FieldLabel>
              <Select
                :items="targetKindItems"
                :value="draftType"
                @value-change="(val) => { if (val) draftType = String(val); }"
              >
                <SelectTrigger aria-label="Target integration type" class="min-h-11 sm:min-h-9">
                  <SelectValue />
                  <AppIcon name="chevron-down" :size="14" />
                </SelectTrigger>
                <SelectContent>
                  <SelectItem v-for="opt in targetKindItems" :key="opt.value" :value="opt.value">
                    {{ opt.label }}
                    <SelectItemIndicator><AppIcon name="check" :size="14" /></SelectItemIndicator>
                  </SelectItem>
                </SelectContent>
              </Select>
            </Field>
            <Field>
              <FieldLabel for="guided-target-slug">Target upstream slug</FieldLabel>
              <Input id="guided-target-slug" v-model="draftName" class="font-mono-data text-xs" placeholder="e.g. zendesk_support" />
            </Field>
            <Field>
              <FieldLabel for="guided-target-url">API or MCP URL</FieldLabel>
              <Input id="guided-target-url" v-model="draftUrl" class="font-mono-data text-xs" placeholder="https://api.example.com" />
            </Field>
          </template>
        </div>
        <pre v-if="generatedPrompt" class="max-h-56 overflow-auto rounded border border-[#E4E4E7] bg-[#F4F4F5] p-3 font-mono-data text-[11px] text-[#09090B]">{{ generatedPrompt }}</pre>
      </div>

      <div class="space-y-3 border-t border-[#E4E4E7] pt-4">
        <div class="flex flex-wrap items-center justify-between gap-2">
          <div>
            <h3 class="text-[14px] font-semibold text-[#09090B]">Step 2 — Paste Agent JSON, Tune Timing &amp; Preview MCP UI</h3>
            <p class="text-[12px] text-[#52525B]">
              After answering your agent's clarifying questions, paste the final JSON bundle here to validate workflows and preview any MCP Apps UI.
            </p>
          </div>
          <Button class="min-h-11 sm:min-h-9" :disabled="bundleValidating || !agentBundleJsonText.trim()" @click="validateAndPreviewBundle">
            {{ bundleValidating ? "Validating…" : "Validate & Preview Bundle" }}
          </Button>
        </div>
        <Textarea
          v-model="agentBundleJsonText"
          aria-label="Paste agent OnboardingBundle JSON"
          rows="7"
          class="font-mono-data text-xs"
          placeholder='{"upstream": {"name": "...", "type": "openapi", "url": "..."}, "integration_definition": {"workflows": [...], "experiences": [...]}, "publish": true}'
        />

        <div v-if="bundlePreview" class="space-y-4 border-t border-[#E4E4E7] pt-4">
          <div class="flex flex-wrap items-center justify-between gap-2 rounded bg-[#F0FDFA] border border-[#99F6E4] px-3 py-2 text-[12px] text-[#0F766E]">
            <span>
              Ready: <strong class="font-mono-data">{{ bundlePreview.upstream.name }}</strong>
              ({{ bundlePreview.upstream.type }} · {{ bundlePreview.upstream.auth_type || "none" }} · {{ bundlePreview.editableWorkflows.length }} workflow(s), {{ bundlePreview.experiencePreviews.length }} UI view(s))
            </span>
            <div class="flex flex-wrap items-center gap-3">
              <span v-if="bundlePreview.warnings.length" class="text-[#B45309]">
                {{ bundlePreview.warnings.length }} warning(s)
              </span>
              <NuxtLink to="/workflows" class="font-medium text-[#0F766E] underline">
                Manage Workflows
              </NuxtLink>
              <NuxtLink to="/mcp-ui" class="font-medium text-[#0F766E] underline">
                Preview MCP Apps UI
              </NuxtLink>
            </div>
          </div>

          <ul v-if="bundlePreview.warnings.length" class="space-y-1 text-[12px] text-[#B45309]">
            <li v-for="(warning, idx) in bundlePreview.warnings" :key="idx">• {{ warning }}</li>
          </ul>

          <div v-if="bundlePreview.editableWorkflows.length" class="space-y-2">
            <h4 class="text-[13px] font-semibold text-[#09090B]">Proposed Workflows (toggle active or adjust human baseline)</h4>
            <div class="divide-y divide-[#E4E4E7] border-y border-[#E4E4E7]">
              <div
                v-for="wf in bundlePreview.editableWorkflows"
                :key="wf.id"
                class="grid grid-cols-1 gap-3 py-3 md:grid-cols-12 md:items-center"
              >
                <div class="md:col-span-5 space-y-0.5">
                  <div class="flex items-center gap-2">
                    <span class="text-[13px] font-semibold text-[#09090B]">{{ wf.name }}</span>
                    <span class="font-mono-data text-[11px] text-[#71717A]">{{ wf.completionTool }}</span>
                  </div>
                  <p class="text-[12px] text-[#52525B]">{{ wf.description }}</p>
                </div>
                <div class="md:col-span-3">
                  <label class="block text-[11px] font-medium text-[#52525B]">
                    Manual baseline (min)
                    <Input
                      :model-value="String(wf.manualMinutes)"
                      type="number"
                      min="0.1"
                      step="0.1"
                      class="mt-1 tabular-nums"
                      @update:model-value="wf.manualMinutes = Number($event)"
                    />
                  </label>
                </div>
                <div class="md:col-span-2">
                  <label class="block text-[11px] font-medium text-[#52525B]">
                    Triggers
                    <Input v-model="wf.intentsText" class="mt-1 text-xs" placeholder="comma separated" />
                  </label>
                </div>
                <div class="md:col-span-2 flex items-center md:justify-end">
                  <label class="inline-flex min-h-11 items-center gap-2 text-[12px] font-medium text-[#09090B]">
                    <Switch
                      :checked="wf.enabled"
                      :aria-label="`Activate ${wf.name}`"
                      @checked-change="(checked) => { wf.enabled = checked === true; }"
                    >
                      <SwitchThumb />
                    </Switch>
                    <span>{{ wf.enabled ? "Active" : "Paused" }}</span>
                  </label>
                </div>
                <div
                  v-if="wf.participatingTools.length"
                  class="md:col-span-12 flex flex-wrap items-center gap-1.5 text-[11px] text-[#52525B]"
                >
                  <span class="font-medium">Steps:</span>
                  <template
                    v-for="(toolName, idx) in wf.participatingTools"
                    :key="`${wf.id}-step-${toolName}-${idx}`"
                  >
                    <span class="rounded border border-[#E4E4E7] bg-[#F4F4F5] px-1.5 py-0.5 font-mono-data text-[#09090B]">
                      {{ toolName }}
                    </span>
                    <span v-if="idx < wf.participatingTools.length - 1" aria-hidden="true">-&gt;</span>
                  </template>
                </div>
              </div>
            </div>
          </div>

          <div v-if="bundlePreview.experiencePreviews.length" class="space-y-3">
            <h4 class="text-[13px] font-semibold text-[#09090B]">MCP Apps UI Presentation Previews</h4>
            <div class="grid grid-cols-1 gap-4 lg:grid-cols-2">
              <div v-for="exp in bundlePreview.experiencePreviews" :key="exp.id" class="space-y-2 border-t border-[#E4E4E7] pt-3">
                <div class="flex items-center justify-between gap-2 text-[12px]">
                  <span class="font-semibold text-[#09090B]">{{ exp.title }}</span>
                  <span class="font-mono-data text-[#0F766E]">{{ exp.tool }}</span>
                </div>
                <McpAppHostPreview
                  :title="exp.title"
                  :iframe-title="`Preview ${exp.title}`"
                  :tool="exp.tool"
                  :resource-uri="exp.resource_uri"
                  :html="exp.html"
                  :error="exp.error"
                  compact
                />
              </div>
            </div>
          </div>
        </div>
      </div>

      <div class="flex flex-wrap items-center justify-between gap-3 border-t border-[#E4E4E7] pt-4">
        <div class="text-[12px] text-[#52525B]">
          <template v-if="bundleApplySuccess">
            Published <strong class="font-mono-data text-[#09090B]">{{ bundleApplySuccess.upstreamName }}</strong>:
            <NuxtLink to="/workflows" class="ml-1 font-medium text-[#0F766E] underline">Open Workflows ({{ bundleApplySuccess.workflows }})</NuxtLink>
            <NuxtLink to="/mcp-ui" class="ml-3 font-medium text-[#0F766E] underline">Open MCP Apps UI ({{ bundleApplySuccess.experiences }})</NuxtLink>
          </template>
          <span v-else>Step 3 — Validate the pasted bundle above, then apply and publish directly to the gateway.</span>
        </div>
        <Button data-variant="primary" class="min-h-11 sm:min-h-9" :disabled="bundleApplying || !bundlePreview" @click="applyGuidedBundle">
          {{ bundleApplying ? "Applying…" : "Step 3 — Apply & Publish Bundle" }}
        </Button>
      </div>
    </section>

    <div v-show="showManualBuilder" class="space-y-6">
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
          :data-variant="onboardingStep === 'workflows' ? 'primary' : undefined"
          :disabled="!schemaVerified"
          class="text-xs"
          @click="goToOnboardingStep('workflows')"
        >
          3. Build workflows
        </Button>
        <Button
          :data-variant="onboardingStep === 'publish' ? 'primary' : undefined"
          :disabled="!schemaVerified"
          class="text-xs"
          @click="goToOnboardingStep('publish')"
        >
          4. Test &amp; Save
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
            Next: Policy &amp; Guardrails
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
        <p v-if="authDiscoverySummary" class="text-xs leading-5 text-[#52525B]">
          {{ authDiscoverySummary }}
        </p>
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
            @value-change="(v) => { if (v) selectAuthType(String(v)) }"
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

        <Field>
          <FieldLabel>Who authorizes the user?</FieldLabel>
          <Select
            :items="authorizationModeOptions"
            :value="draftAuthorizationMode"
            @value-change="(v) => { if (v) selectAuthorizationMode(String(v)) }"
          >
            <SelectTrigger>
              <SelectValue />
              <AppIcon name="chevron-down" :size="14" />
            </SelectTrigger>
            <SelectContent>
              <SelectItem v-for="mode in authorizationModeOptions" :key="mode.value" :value="mode.value">
                {{ mode.label }}
                <SelectItemIndicator><AppIcon name="check" :size="14" /></SelectItemIndicator>
              </SelectItem>
            </SelectContent>
          </Select>
          <FieldDescription v-if="draftAuthorizationMode === 'upstream'">
            The gateway sends a user-scoped credential. The upstream decides whether this user may call each operation; gateway groups are not required.
          </FieldDescription>
          <FieldDescription v-else-if="draftAuthorizationMode === 'shared_access'">
            The upstream sees a shared service identity. Callers admitted by any configured client or scope limits may use the enabled operations; finer user permissions must be enforced upstream using another user-aware mechanism.
          </FieldDescription>
          <FieldDescription v-else>
            The gateway checks the configured caller roles or groups before exposing and invoking this upstream.
          </FieldDescription>
        </Field>

        <div v-if="draftAuthorizationMode === 'gateway'" class="sm:col-span-2 space-y-2">
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
          <p class="mt-1 text-xs text-[#52525B]">Tools start disabled. Enable each operation deliberately and classify its effect. Writes require caller confirmation. Manager approval workflows are handled by upstream systems and are not configured here. Code Mode cannot invoke gated tools.</p>
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
              <Input v-if="draftToolEffects[t.name] === 'write'" v-model="draftToolApprovalMessages[t.name]" :aria-label="`Action summary for ${t.name}`" :placeholder="`Confirm this write to ${t.name}.`" maxlength="300" class="min-h-11 text-xs sm:col-span-3" />
              <div v-if="draftToolEffects[t.name] === 'write'" class="space-y-2 border-t border-[#E4E4E7] pt-3 sm:col-span-3">
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
        <Button @click="goToOnboardingStep('connect')">Back to Connect</Button>
        <Button data-variant="primary" :disabled="!schemaVerified" @click="goToOnboardingStep('workflows')">Next: Build workflows</Button>
      </div>
    </div>

    <div v-else-if="onboardingStep === 'workflows'" class="space-y-5 rounded-lg border border-[#E4E4E7] bg-white p-5">
      <div class="flex flex-col gap-3 border-b border-[#E4E4E7] pb-4 sm:flex-row sm:items-start sm:justify-between">
        <div>
          <h2 class="text-[15px] font-semibold text-[#09090B]">Step 3 — Define business workflows</h2>
          <p class="mt-1 max-w-3xl text-[13px] leading-5 text-[#52525B]">Choose tools from this catalog and arrange them to explain the business activity. The displayed order is for people to understand the flow; calls may happen in any order. Required evidence and the terminal result determine whether a run receives credit.</p>
        </div>
        <div class="flex flex-wrap items-center gap-2">
          <Button class="min-h-11 shrink-0" :disabled="discoverBusy || !draftName.trim()" @click="discoverWorkflowCandidates">{{ discoverBusy ? "Discovering…" : "Auto-discover workflows" }}</Button>
          <Button class="min-h-11 shrink-0" @click="addWorkflowDraft">Add another workflow</Button>
        </div>
      </div>

      <div v-if="discoveredCandidates.length" class="space-y-2">
        <div class="text-xs font-semibold text-[#09090B]">Auto-discovered workflow drafts</div>
        <ul class="divide-y divide-[#E4E4E7] border-y border-[#E4E4E7]">
          <li v-for="cand in discoveredCandidates" :key="cand.candidate_id || cand.workflow_slug" class="flex flex-wrap items-center justify-between gap-2 py-2.5 text-xs">
            <div><span class="font-medium text-[#09090B]">{{ cand.draft_definition?.name || cand.workflow_slug }}</span> <span class="text-[#71717A]">· {{ cand.origin }}</span></div>
            <Button class="min-h-11 text-xs" @click="loadDiscoveredCandidateIntoDraft(cand)">Use draft</Button>
          </li>
        </ul>
      </div>

      <div v-if="!inspectResult?.inspection.tools.length" class="rounded-md border border-[#E4E4E7] bg-[#FAFAFA] p-4 text-sm text-[#52525B]">
        No catalog tools are available. Return to Connect &amp; Inspect and verify the upstream schema.
      </div>
      <div v-else class="space-y-6">
        <section class="space-y-2 border-b border-[#E4E4E7] pb-4" aria-labelledby="existing-workflows-title">
          <h3 id="existing-workflows-title" class="text-xs font-semibold text-[#09090B]">Published workflows for this upstream</h3>
          <p v-if="existingWorkflowLoadError" class="text-xs leading-5 text-[#991B1B]">Could not load the existing workflow list: {{ existingWorkflowLoadError }}. Retry schema inspection before publishing changes.</p>
          <p v-else-if="!existingWorkflows.length" class="text-xs text-[#71717A]">No published workflows are configured yet. Add one or more below.</p>
          <ul v-else class="divide-y divide-[#E4E4E7] border-y border-[#E4E4E7]">
            <li v-for="published in existingWorkflows" :key="published.id" class="py-3 text-xs">
              <div class="font-medium text-[#09090B]">{{ published.name }} <span class="font-normal text-[#71717A]">· {{ published.business_value.manual_baseline_minutes }} min manual baseline</span></div>
              <p class="mt-1 leading-5 text-[#52525B]">{{ published.description }} Tools: {{ published.participating_tools.map((name) => name.replace(`${draftName}__`, "")).join(", ") }}. Required evidence: {{ published.required_evidence.map((item) => item.tool.replace(`${draftName}__`, "")).join(", ") || "terminal outcome only" }}. Completion: {{ published.completion.tool.replace(`${draftName}__`, "") }}. {{ published.business_value.baseline_source.replaceAll("_", " ") }}, {{ published.business_value.confidence }}.</p>
              <p class="mt-1 text-[#71717A]">Use the same workflow name below to replace its definition; other published workflows will be retained.</p>
            </li>
          </ul>
        </section>
        <section v-for="(workflow, workflowIndex) in workflowDrafts" :key="workflow.id" class="space-y-4" :class="workflowIndex ? 'border-t border-[#E4E4E7] pt-5' : ''">
          <div class="flex flex-wrap items-center justify-between gap-2">
            <h3 class="text-sm font-semibold text-[#09090B]">Workflow {{ workflowIndex + 1 }}</h3>
            <Button v-if="workflowDrafts.length > 1" class="min-h-11 text-xs" @click="removeWorkflowDraft(workflow.id)">Remove workflow</Button>
          </div>
          <div class="grid grid-cols-1 gap-3 sm:grid-cols-2">
            <Field>
              <FieldLabel>Workflow name</FieldLabel>
              <Input v-model="workflow.name" :aria-label="`Workflow ${workflowIndex + 1} name`" placeholder="Update and verify a customer record" class="min-h-11" />
              <FieldDescription>Use a name an administrator will recognize in reports.</FieldDescription>
            </Field>
            <Field>
              <FieldLabel>Human time for this activity (minutes)</FieldLabel>
              <Input v-model.number="workflow.manualMinutes" :aria-label="`Workflow ${workflowIndex + 1} human baseline minutes`" type="number" min="0.1" step="0.1" class="min-h-11 tabular-nums" />
              <FieldDescription>This is the manual baseline used when a workflow completes.</FieldDescription>
            </Field>
            <Field class="sm:col-span-2">
              <FieldLabel>What business activity does this measure?</FieldLabel>
              <Input v-model="workflow.description" :aria-label="`Workflow ${workflowIndex + 1} description`" placeholder="Find a customer, update their account, then confirm the saved record." class="min-h-11" />
            </Field>
          </div>

          <div class="grid grid-cols-1 gap-4 lg:grid-cols-[minmax(0,1fr)_minmax(17rem,0.8fr)]">
            <div class="space-y-3">
              <div>
                <h4 class="text-xs font-semibold text-[#09090B]">Choose and arrange catalog tools</h4>
                <p class="mt-1 text-xs leading-5 text-[#52525B]">Use the up/down controls to organize the flow. This order does not constrain runtime calls.</p>
              </div>
              <div class="flex flex-wrap gap-2">
                <button v-for="tool in inspectResult.inspection.tools" :key="tool.qualified_name" type="button" :disabled="draftToolEffects[tool.name] === 'disabled' && !workflow.tools.includes(tool.qualified_name)" class="min-h-11 max-w-full rounded-md border px-3 py-2 text-left text-xs transition-colors focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-[#0F766E] disabled:cursor-not-allowed disabled:bg-[#F4F4F5] disabled:text-[#71717A]" :class="workflow.tools.includes(tool.qualified_name) ? 'border-[#0F766E] bg-[#F0FDFA] text-[#0F766E]' : 'border-[#D4D4D8] bg-white text-[#09090B] hover:bg-[#F4F4F5]'" :aria-pressed="workflow.tools.includes(tool.qualified_name)" :title="draftToolEffects[tool.name] === 'disabled' && !workflow.tools.includes(tool.qualified_name) ? 'Enable this operation in Policy & Mode before adding it.' : undefined" @click="toggleWorkflowTool(workflow, tool.qualified_name)">
                  {{ workflow.tools.includes(tool.qualified_name) ? 'Remove' : draftToolEffects[tool.name] === 'disabled' ? 'Enable in Policy' : 'Add' }} {{ tool.name }}
                </button>
              </div>
              <ol v-if="workflow.tools.length" :aria-label="`Workflow ${workflowIndex + 1} flow order`" class="divide-y divide-[#E4E4E7] border-y border-[#E4E4E7]">
                <li v-for="(toolName, toolIndex) in workflow.tools" :key="toolName" class="flex flex-col gap-2 py-3 sm:flex-row sm:items-center sm:justify-between">
                  <div class="flex min-w-0 items-start gap-3">
                    <span class="flex h-7 w-7 shrink-0 items-center justify-center rounded-full bg-[#F4F4F5] text-xs font-medium tabular-nums text-[#52525B]">{{ toolIndex + 1 }}</span>
                    <div class="min-w-0">
                      <div class="break-all font-mono-data text-xs text-[#09090B]">{{ toolName }}</div>
                      <label class="mt-1 flex min-h-11 items-center gap-2 text-xs text-[#52525B] sm:min-h-9">
                        <Checkbox :checked="workflow.requiredTools.includes(toolName)" :aria-label="`Require ${toolName} as completion evidence`" @checked-change="(checked) => { workflow.requiredTools = checked === true ? [...workflow.requiredTools, toolName] : workflow.requiredTools.filter((name) => name !== toolName) }"><CheckboxIndicator><AppIcon name="check" :size="13" /></CheckboxIndicator></Checkbox>
                        Required evidence for credit
                      </label>
                      <div v-if="workflow.requiredTools.includes(toolName)" class="ml-0 space-y-2 border-t border-[#E4E4E7] pt-2">
                        <label class="flex min-h-11 items-center gap-2 text-xs text-[#52525B] sm:min-h-9">
                          <Checkbox :checked="Boolean(workflow.evidencePredicates[toolName])" :aria-label="`Check returned result from ${toolName}`" @checked-change="(checked) => toggleEvidencePredicate(workflow, toolName, checked === true)"><CheckboxIndicator><AppIcon name="check" :size="13" /></CheckboxIndicator></Checkbox>
                          Also check a result field
                        </label>
                        <div v-if="workflow.evidencePredicates[toolName]" class="grid grid-cols-1 gap-2 sm:grid-cols-2">
                          <Input v-model="workflow.evidencePredicates[toolName].path" :aria-label="`Required evidence result field for ${toolName}`" placeholder="/status" class="min-h-11 font-mono-data text-xs" />
                          <select v-model="workflow.evidencePredicates[toolName].type" :aria-label="`Required evidence result type for ${toolName}`" class="min-h-11 w-full rounded-md border border-[#D4D4D8] bg-white px-3 text-xs text-[#09090B] focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-[#0F766E]">
                            <option value="non_empty_string">Non-empty text</option>
                            <option value="string">Text</option>
                            <option value="boolean">Boolean</option>
                            <option value="non_empty_object">Non-empty object</option>
                            <option value="object">Object</option>
                            <option value="non_empty_array">Non-empty list</option>
                            <option value="array">List</option>
                            <option value="number">Number</option>
                            <option value="integer">Whole number</option>
                          </select>
                        </div>
                      </div>
                    </div>
                  </div>
                  <div class="flex gap-2 pl-10 sm:pl-0">
                    <Button class="min-h-11 px-3 text-xs" :disabled="toolIndex === 0" :aria-label="`Move ${toolName} earlier`" @click="moveWorkflowTool(workflow, toolIndex, -1)">Move up</Button>
                    <Button class="min-h-11 px-3 text-xs" :disabled="toolIndex === workflow.tools.length - 1" :aria-label="`Move ${toolName} later`" @click="moveWorkflowTool(workflow, toolIndex, 1)">Move down</Button>
                  </div>
                </li>
              </ol>
              <p v-else class="rounded-md border border-[#E4E4E7] bg-[#FAFAFA] p-3 text-xs text-[#71717A]">Add the tools that take part in this business activity.</p>
            </div>

            <div class="space-y-3 border-t border-[#E4E4E7] pt-4 lg:border-l lg:border-t-0 lg:pl-4 lg:pt-0">
              <div>
                <h4 class="text-xs font-semibold text-[#09090B]">Confirm the terminal outcome</h4>
                <p class="mt-1 text-xs leading-5 text-[#52525B]">Choose the tool result that confirms the activity finished. Only a successful matching result completes the workflow.</p>
              </div>
              <Field>
                <FieldLabel>Outcome tool</FieldLabel>
                <select v-model="workflow.completionTool" :aria-label="`Workflow ${workflowIndex + 1} outcome tool`" class="min-h-11 w-full rounded-md border border-[#D4D4D8] bg-white px-3 text-sm text-[#09090B] focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-[#0F766E]">
                  <option value="">Choose a tool in this flow</option>
                  <option v-for="toolName in workflow.tools" :key="toolName" :value="toolName">{{ toolName }}</option>
                </select>
              </Field>
              <Field>
                <FieldLabel>Result field that confirms success</FieldLabel>
                <Input v-model="workflow.completionPath" :aria-label="`Workflow ${workflowIndex + 1} completion result field`" placeholder="/status" class="min-h-11 font-mono-data text-xs" />
                <FieldDescription>Use a field path from the selected tool result, for example /status.</FieldDescription>
              </Field>
              <Field>
                <FieldLabel>Expected result</FieldLabel>
                <select v-model="workflow.completionType" :aria-label="`Workflow ${workflowIndex + 1} completion result type`" class="min-h-11 w-full rounded-md border border-[#D4D4D8] bg-white px-3 text-sm text-[#09090B] focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-[#0F766E]">
                  <option value="non_empty_string">Non-empty text</option>
                  <option value="string">Text</option>
                  <option value="boolean">True</option>
                  <option value="non_empty_object">Non-empty object</option>
                  <option value="object">Object</option>
                  <option value="non_empty_array">Non-empty list</option>
                  <option value="array">List</option>
                  <option value="number">Number</option>
                  <option value="integer">Whole number</option>
                </select>
              </Field>
            </div>
          </div>

          <div class="grid grid-cols-1 gap-3 border-t border-[#E4E4E7] pt-4 sm:grid-cols-2 lg:grid-cols-4">
            <Field>
              <FieldLabel>Baseline source</FieldLabel>
              <select :value="workflow.baselineSource" :aria-label="`Workflow ${workflowIndex + 1} baseline source`" class="min-h-11 w-full rounded-md border border-[#D4D4D8] bg-white px-3 text-sm text-[#09090B] focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-[#0F766E]" @change="setWorkflowBaselineSource(workflow, ($event.target as HTMLSelectElement).value)">
                <option value="administrator_estimate">Administrator estimate</option>
                <option value="sampled_manual_measurement">Sampled manual measurement</option>
                <option value="validated_external_measurement">Validated external measurement</option>
              </select>
            </Field>
            <Field>
              <FieldLabel>Confidence</FieldLabel>
              <div :aria-label="`Workflow ${workflowIndex + 1} baseline confidence`" class="flex min-h-11 items-center rounded-md border border-[#E4E4E7] bg-[#F4F4F5] px-3 text-sm text-[#52525B]">{{ workflow.confidence }}</div>
            </Field>
            <Field>
              <FieldLabel>Effective from</FieldLabel>
              <Input v-model="workflow.effectiveFrom" :aria-label="`Workflow ${workflowIndex + 1} baseline effective date`" type="date" class="min-h-11 tabular-nums" />
            </Field>
            <Field>
              <FieldLabel>Measurement note</FieldLabel>
              <Input v-model="workflow.evidenceNote" :aria-label="`Workflow ${workflowIndex + 1} measurement note`" placeholder="How was this estimate established?" class="min-h-11" />
            </Field>
          </div>
          <div class="rounded-md bg-[#F4F4F5] p-3 text-xs leading-5 text-[#52525B]">
            <strong class="text-[#09090B]">This workflow measures:</strong> {{ workflow.description || "Describe the business activity above." }}
            <span v-if="workflow.tools.length"> It includes {{ workflow.tools.map((name) => inspectResult.inspection.tools.find((tool) => tool.qualified_name === name)?.name || name).join(", ") }}.</span>
            <span v-if="workflow.requiredTools.length"> Credit requires successful calls to {{ workflow.requiredTools.join(", ") }}.</span>
            <span v-if="workflow.completionTool"> Completion requires {{ workflow.completionTool }} to return {{ workflow.completionType }} at {{ workflow.completionPath }}.</span>
            <span> Manual baseline: {{ workflow.manualMinutes || 0 }} minutes ({{ workflow.baselineSource.replaceAll("_", " ") }}, {{ workflow.confidence }}).</span>
          </div>
        </section>

        <div class="flex flex-col gap-3 border-t border-[#E4E4E7] pt-4 sm:flex-row sm:items-center sm:justify-between">
          <Button class="min-h-11" @click="goToOnboardingStep('policy')">Back to Policy</Button>
          <div class="flex flex-col gap-2 sm:flex-row sm:items-center">
            <span v-if="workflowPublishMessage" role="status" class="text-sm text-[#0F766E]">{{ workflowPublishMessage }}</span>
            <Button data-variant="primary" class="min-h-11" :disabled="workflowSaveBusy || !dynamicPermitted" pending-announcement="Publishing workflows" @click="handleSaveWorkflows">{{ workflowSaveBusy ? "Publishing workflows…" : "Save and publish workflows" }}</Button>
            <Button class="min-h-11" @click="goToOnboardingStep('publish')">Continue to Test &amp; Save</Button>
          </div>
        </div>
        <p v-if="!dynamicPermitted" class="text-xs leading-5 text-[#71717A]">Workflow publishing requires dynamic registration to be enabled for this gateway. Save the workflow through your GitOps process.</p>
      </div>
    </div>

    <div v-else class="grid grid-cols-1 gap-6 xl:grid-cols-12">
      <section class="space-y-5 rounded-lg border border-[#E4E4E7] bg-white p-5 xl:col-span-7">
        <div class="flex items-center gap-2 border-b border-[#E4E4E7] pb-3">
          <AppIcon name="flask" :size="16" class="text-[#0F766E]" />
          <h2 class="text-[15px] font-semibold text-[#09090B]">Step 4 — Test &amp; Save</h2>
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
          <Button @click="goToOnboardingStep('workflows')">Back: Workflows</Button>
          <div class="flex flex-wrap items-center gap-2">
            <Button :disabled="abBusy" pending-announcement="Estimating" @click="runAbTest">
              <AppIcon name="flask" :size="15" />
              <span>Estimate Code Mode vs Classic</span>
            </Button>
            <Button v-if="dynamicPermitted" data-variant="primary" :disabled="!accessPolicyValid || (draftAuthorizationMode === 'gateway' && (rolesLoading || Boolean(rolesLoadError)))" data-agent-confirm="Register or update this upstream service in the gateway?" pending-announcement="Registering upstream" @click="handleRegister">
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
        <DialogTitle>Import Agent-Generated Onboarding JSON</DialogTitle>
        <DialogDescription>
          Paste either a single UpstreamConfig JSON object or a full OnboardingBundle (upstream, workflows, and MCP App UI experiences) produced by your agent.
        </DialogDescription>
        <Field>
          <FieldLabel>UpstreamConfig or OnboardingBundle JSON</FieldLabel>
          <Textarea
            v-model="importJsonText"
            rows="6"
            class="font-mono-data text-xs"
            placeholder='{"upstream": {...}, "integration_definition": {...}, "publish": true}'
          />
        </Field>
        <div class="flex flex-wrap justify-end gap-2">
          <DialogClose>Cancel</DialogClose>
          <Button @click="applyImportedJson">Load Upstream Draft</Button>
          <Button data-variant="primary" :disabled="bundleBusy" @click="applyAgentBundleJson(true)">{{ bundleBusy ? "Applying…" : "Apply Full Onboarding Bundle" }}</Button>
        </div>
      </DialogContent>
    </Dialog>
  </div>
</template>
