<script setup lang="ts">
import { extractErrorMessage } from "~/composables/useAdminApi";
import { summarizeUpstreamHealth } from "~/utils/upstream-health";

const { apiFetch: _apiFetch } = useAdminApi();
const requestedUpstream = useRoute().query.upstream;
const initialUpstream = typeof requestedUpstream === "string" ? requestedUpstream : "";
const expandedUpstreamName = ref<string | null>(initialUpstream || null);
const toolDrawerOpen = ref(false);
const selectedTool = ref<{ upstream: string; tool: string; opener: HTMLElement } | null>(null);
const healthRows = ref<Array<{ server: string; tool: string; count: number; bytes: number; mean_latency_ms: number | null; p95_latency_ms: number | null; errors: number | null; error_types: Record<string, number>; timeouts: number; invalid_arguments: number | null; schema_coverage_pct: number | null; read_calls: number | null; write_calls: number | null }>>([]);
const activeHealth = ref<Array<{ server: string; status: string; checked_at: string; error_type?: string | null; error?: string | null; consecutive_failures: number }>>([]);
const performanceError = ref("");
const performanceLoading = ref(true);
const catalogRefreshVersion = ref(0);

async function loadPerf() {
  performanceError.value = "";
  try {
    const res = await _apiFetch<{ rows: typeof healthRows.value; upstream_health: typeof activeHealth.value }>("/admin/adaptive-execution/tool-health?window=all");
    if (res?.rows) healthRows.value = res.rows;
    activeHealth.value = res?.upstream_health || [];
  } catch (err) {
    performanceError.value = extractErrorMessage(err);
  } finally {
    performanceLoading.value = false;
  }
}

function toggleExpandUpstream(name: string) {
  expandedUpstreamName.value = expandedUpstreamName.value === name ? null : name;
}

function openTool(upstream: string, tool: string, event: MouseEvent) {
  if (!(event.currentTarget instanceof HTMLElement)) return;
  selectedTool.value = { upstream, tool, opener: event.currentTarget };
  toolDrawerOpen.value = true;
}

function aggregatedToolsForUpstream(u: UpstreamEntry) {
  return healthRows.value.filter((row) => row.server === u.name).sort((a, b) => a.tool.localeCompare(b.tool));
}

function healthForUpstream(name: string) {
  return activeHealth.value.find((item) => item.server === name);
}

function serverStats(u: UpstreamEntry) {
  const summary = summarizeUpstreamHealth(aggregatedToolsForUpstream(u));
  return {
    ...summary,
    runs: summary.calls,
    avg_duration_ms: summary.avgLatencyMs,
    avg_tokens: null,
    success_rate_pct: summary.successRatePct,
  };
}

async function setUpstreamPreferredMode(u: UpstreamEntry, mode: string) {
  const res = await _apiFetch<{ preferred_mode: string }>(`/admin/upstreams/${encodeURIComponent(u.name)}/mode`, {
    method: "PATCH",
    body: { preferred_mode: mode },
  });
  if (res) u.preferred_mode = res.preferred_mode;
}

async function setToolModeOverride(u: UpstreamEntry, toolName: string, mode: string) {
  const res = await _apiFetch<{ tool_mode_overrides: Record<string, string> }>(
    `/admin/upstreams/${encodeURIComponent(u.name)}/tools/${encodeURIComponent(toolName)}/mode`,
    { method: "PATCH", body: { mode } }
  );
  if (res) {
    u.tool_mode_overrides = res.tool_mode_overrides;
  }
}

const modeDialogOpen = ref(false);
const pendingModeChange = ref<{
  kind: "server" | "tool";
  upstream: UpstreamEntry;
  tool?: string;
  nextMode: string;
} | null>(null);

function promptModeChange(
  kind: "server" | "tool",
  upstream: UpstreamEntry,
  nextMode: string,
  tool?: string
) {
  modeError.value = "";
  pendingModeChange.value = { kind, upstream, tool, nextMode };
  modeDialogOpen.value = true;
}

async function confirmModeChange() {
  const pending = pendingModeChange.value;
  if (!pending) return;
  errorMessage.value = "";
  modeError.value = "";
  modeSaving.value = true;
  try {
    if (pending.kind === "server") {
      await setUpstreamPreferredMode(pending.upstream, pending.nextMode);
      statusMessage.value = `Updated '${pending.upstream.name}' preferred mode to '${pending.nextMode}'.`;
    } else if (pending.tool) {
      await setToolModeOverride(pending.upstream, pending.tool, pending.nextMode);
      statusMessage.value = `Updated '${pending.tool}' override to '${pending.nextMode}'.`;
    }
    modeDialogOpen.value = false;
    pendingModeChange.value = null;
    await fetchData();
  } catch (err) {
    errorMessage.value = extractErrorMessage(err);
    modeError.value = errorMessage.value;
  } finally {
    modeSaving.value = false;
  }
}

let _pollTimer: ReturnType<typeof setInterval> | null = null;

onMounted(() => {
  void loadPerf();
  _pollTimer = setInterval(() => {
    void loadPerf();
    void fetchData();
  }, 15000);
});

onUnmounted(() => {
  if (_pollTimer) clearInterval(_pollTimer);
});

interface UpstreamEntry {
  name: string;
  type: string;
  url?: string | null;
  auth_type: string;
  preferred_mode?: string;
  tool_mode_overrides?: Record<string, string>;
  tools?: string[];
  allowed_groups?: string[];
  enabled: boolean;
  request_timeout_seconds?: number;
  max_requests_per_minute?: number | null;
  max_concurrent_calls?: number | null;
  health_check?: { enabled: boolean; interval_seconds: number; url?: string | null };
  ab_experiment?: {
    enabled: boolean;
    experiment_id?: string | null;
    codemode_traffic_pct: number;
  } | null;
}

interface UpstreamsResponse {
  app_env: string;
  profile: string;
  dynamic_upstreams_permitted: boolean;
  catalog_version: string;
  digests_by_server: Record<string, string>;
  drift_warnings: string[];
  upstreams: UpstreamEntry[];
  total?: number;
  total_registered?: number;
  page?: number;
  page_size?: number;
  total_pages?: number;
  facets?: {
    groups: string[];
    auth_types: string[];
    modes: string[];
  };
}

const { apiFetch, setRuntimeMeta } = useAdminApi();

const loading = ref(true);
const errorMessage = ref("");
const statusMessage = ref("");
const catalogUpdatedAt = ref<Date | null>(null);
const catalogRefreshing = ref(false);
const deleteSaving = ref(false);
const modeSaving = ref(false);
const modeError = ref("");
const upstreamsData = ref<UpstreamsResponse | null>(null);
const deleteTarget = ref<string | null>(null);
const deleteDialogOpen = ref(false);
const deleteError = ref("");

const searchQuery = ref(initialUpstream);
const groupFilter = ref("");
const authFilter = ref("");
const modeFilter = ref("");
const page = ref(1);
const pageSize = 25;

let searchTimer: ReturnType<typeof setTimeout> | null = null;

async function fetchData(resetPage = false) {
  if (resetPage) page.value = 1;
  errorMessage.value = "";
  try {
    const params = new URLSearchParams({
      page: String(page.value),
      page_size: String(pageSize),
    });
    if (searchQuery.value.trim()) params.set("q", searchQuery.value.trim());
    if (groupFilter.value) params.set("group", groupFilter.value);
    if (authFilter.value) params.set("auth_type", authFilter.value);
    if (modeFilter.value) params.set("mode", modeFilter.value);

    const ups = await apiFetch<UpstreamsResponse>(`/admin/upstreams?${params.toString()}`);
    upstreamsData.value = ups;
    catalogUpdatedAt.value = new Date();
    setRuntimeMeta({
      profile: ups.profile,
      app_env: ups.app_env,
      catalog_version: ups.catalog_version,
    });
  } catch (err) {
    errorMessage.value = extractErrorMessage(err);
  } finally {
    loading.value = false;
  }
}

watch(searchQuery, () => {
  if (searchTimer) clearTimeout(searchTimer);
  searchTimer = setTimeout(() => {
    void fetchData(true);
  }, 200);
});

watch([groupFilter, authFilter, modeFilter], () => {
  void fetchData(true);
});

function clearFilters() {
  searchQuery.value = "";
  groupFilter.value = "";
  authFilter.value = "";
  modeFilter.value = "";
  void fetchData(true);
}

async function handleRefreshCatalog() {
  errorMessage.value = "";
  statusMessage.value = "";
  catalogRefreshing.value = true;
  try {
    const res = await apiFetch<{ catalog_version: string; catalog_epoch?: number }>(
      "/admin/catalog/refresh",
      { method: "POST" }
    );
    statusMessage.value = `Catalog refreshed (${res.catalog_version}, epoch ${res.catalog_epoch ?? 0}).`;
    await fetchData();
    catalogRefreshVersion.value += 1;
  } catch (err) {
    errorMessage.value = extractErrorMessage(err);
  } finally {
    catalogRefreshing.value = false;
  }
}

const catalogUpdatedLabel = computed(() => catalogUpdatedAt.value
  ? `Updated ${catalogUpdatedAt.value.toLocaleTimeString([], { hour: "2-digit", minute: "2-digit", second: "2-digit" })}`
  : "Waiting for the first successful refresh");

function groupSummary(upstream: UpstreamEntry) {
  return upstream.allowed_groups?.length ? upstream.allowed_groups.join(", ") : "All groups";
}

function upstreamTypeLabel(type: string) {
  return type === "mcp_http" ? "MCP over HTTP" : type === "openapi" ? "OpenAPI" : type.replaceAll("_", " ");
}

function authTypeLabel(authType: string) {
  return authType === "none" ? "No gateway credential" : authType.replaceAll("_", " ");
}

function latencyLabel(upstream: UpstreamEntry) {
  const mean = serverStats(upstream).avg_duration_ms;
  return mean == null ? "Not captured" : `${mean} ms mean`;
}

function modeChangeImpact() {
  if (!pendingModeChange.value) return "";
  return pendingModeChange.value.kind === "server"
    ? "This changes the default mode for calls to the whole upstream. Per-tool overrides and explicit request modes can still take precedence."
    : "This pins only the selected tool to the chosen mode, overriding the upstream default for that tool.";
}

function promptDelete(name: string) {
  deleteError.value = "";
  deleteTarget.value = name;
  deleteDialogOpen.value = true;
}

async function confirmDelete() {
  if (!deleteTarget.value) return;
  const target = deleteTarget.value;
  errorMessage.value = "";
  deleteError.value = "";
  deleteSaving.value = true;
  try {
    await apiFetch(`/admin/upstreams/${encodeURIComponent(target)}`, { method: "DELETE" });
    statusMessage.value = `Deleted dynamic upstream '${target}'.`;
    deleteDialogOpen.value = false;
    await fetchData();
  } catch (err) {
    errorMessage.value = extractErrorMessage(err);
    deleteError.value = errorMessage.value;
  } finally {
    deleteSaving.value = false;
  }
}

onMounted(() => {
  void fetchData();
});
</script>

<template>
  <div class="space-y-6">
    <div class="flex flex-col gap-3 sm:flex-row sm:items-center sm:justify-between">
      <div>
        <div class="flex items-center gap-2">
          <h1 class="text-[20px] font-semibold tracking-[-0.015em] text-[#09090B]">
            Upstreams Service Catalog
          </h1>
          <span
            class="inline-flex items-center gap-1 rounded-full border border-[#99F6E4] bg-[#F0FDFA] px-2 py-0.5 text-[11px] font-medium text-[#0F766E]"
          >
            <span class="h-1.5 w-1.5 rounded-full bg-[#0F766E]" />
            3s
          </span>
        </div>
        <p class="mt-0.5 text-[13px] text-[#52525B]">
          Find registered APIs, review their tool access and observed traffic, or open an upstream to manage its catalog.
        </p>
      </div>
      <div class="flex flex-wrap items-center gap-2">
        <Button :disabled="catalogRefreshing" pending-announcement="Refreshing catalog" @click="handleRefreshCatalog">
          <AppIcon name="refresh" :size="15" />
          <span>{{ catalogRefreshing ? "Refreshing…" : "Refresh catalog" }}</span>
        </Button>
        <NuxtLink
          to="/onboarding"
          class="no-underline inline-flex min-h-11 items-center gap-1.5 rounded-md border border-[#0F766E] bg-[#0F766E] px-3.5 py-2 text-[13px] font-medium text-white hover:bg-[#115E59] focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-[#0F766E]"
        >
          <AppIcon name="onboarding" :size="15" />
          <span>Onboard Service</span>
        </NuxtLink>
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
    <div v-if="performanceError" role="alert" class="flex flex-col gap-2 rounded-lg border border-[#FECACA] bg-[#FEF2F2] px-4 py-3 text-[13px] text-[#991B1B] sm:flex-row sm:items-center sm:justify-between">
      <span>Tool performance data is unavailable; health summaries may be incomplete. {{ performanceError }}</span>
      <Button class="min-h-11" @click="loadPerf">Retry performance data</Button>
    </div>
    <div v-else-if="performanceLoading" role="status" class="text-xs text-[#71717A]">Loading tool performance data…</div>

    <div
      v-if="upstreamsData?.drift_warnings?.length"
      role="alert"
      class="rounded-lg border border-[#FDE68A] bg-[#FFFBEB] px-4 py-3 text-[13px] text-[#92400E]"
    >
      <div class="font-semibold">Schema Drift Warnings</div>
      <ul class="mt-1 list-disc pl-5 text-xs">
        <li v-for="(w, idx) in upstreamsData.drift_warnings" :key="idx">{{ w }}</li>
      </ul>
    </div>

    <section class="rounded-lg border border-[#E4E4E7] bg-white p-4 space-y-3">
      <div class="grid gap-3 sm:grid-cols-5">
        <div class="sm:col-span-2">
          <label for="upstreams-search" class="block text-xs font-medium text-[#52525B]">Search service name or URL</label>
          <input
            id="upstreams-search"
            v-model="searchQuery"
            type="search"
            placeholder="e.g. billing, snowflake, https://..."
            class="mt-1 min-h-11 w-full rounded-md border border-[#E4E4E7] bg-white px-3 py-1.5 text-xs text-[#09090B] focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-[#0F766E]"
          />
        </div>
        <div>
          <label class="block text-xs font-medium text-[#52525B]">Allowed Group</label>
          <select v-model="groupFilter" class="mt-1 min-h-11 w-full rounded-md border border-[#E4E4E7] bg-white px-2.5 py-1.5 text-xs focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-[#0F766E]">
            <option value="">All Groups</option>
            <option v-for="g in upstreamsData?.facets?.groups || []" :key="g" :value="g">{{ g }}</option>
          </select>
        </div>
        <div>
          <label class="block text-xs font-medium text-[#52525B]">Auth Type</label>
          <select v-model="authFilter" class="mt-1 min-h-11 w-full rounded-md border border-[#E4E4E7] bg-white px-2.5 py-1.5 text-xs focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-[#0F766E]">
            <option value="">All Auth</option>
            <option v-for="a in upstreamsData?.facets?.auth_types || []" :key="a" :value="a">{{ a }}</option>
          </select>
        </div>
        <div>
          <label class="block text-xs font-medium text-[#52525B]">Preferred Mode</label>
          <div class="mt-1">
            <select v-model="modeFilter" class="min-h-11 w-full rounded-md border border-[#E4E4E7] bg-white px-2 py-1.5 text-xs focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-[#0F766E]">
              <option value="">All Modes</option>
              <option v-for="m in upstreamsData?.facets?.modes || []" :key="m" :value="m">{{ m }}</option>
            </select>
          </div>
        </div>
      </div>
      <div class="flex flex-col gap-1 text-xs text-[#52525B] sm:flex-row sm:items-center sm:justify-between">
        <span class="tabular-nums">
          Matching {{ upstreamsData?.total ?? upstreamsData?.upstreams?.length ?? 0 }} of
          {{ upstreamsData?.total_registered ?? upstreamsData?.upstreams?.length ?? 0 }} registered servers
        </span>
        <button type="button" class="min-h-11 self-start font-medium text-[#0F766E] underline underline-offset-2 hover:text-[#115E59] focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-[#0F766E] sm:self-auto" @click="clearFilters">
          Reset Filters
        </button>
      </div>
    </section>

    <div v-if="loading" class="h-48 animate-pulse rounded-lg border border-[#E4E4E7] bg-white" />

    <template v-else-if="upstreamsData">
      <section class="rounded-lg border border-[#E4E4E7] bg-white">
        <div class="flex flex-col gap-2 border-b border-[#E4E4E7] px-4 py-3 sm:flex-row sm:items-start sm:justify-between">
          <div class="min-w-0">
            <h2 class="text-[15px] font-semibold text-[#09090B]">Registered upstreams</h2>
            <p class="mt-1 text-xs leading-5 text-[#52525B]">
              Calls and latency use all retained gateway-observed tool traffic. “Not captured” means the gateway has no measurement for that field; it does not mean zero.
            </p>
            <p class="mt-1 text-xs leading-5 text-[#52525B]">
              {{ upstreamsData.dynamic_upstreams_permitted
                ? "Changes from this catalog apply to the running gateway after confirmation. Mode changes set a default; removing an upstream stops new calls immediately."
                : "This catalog is read-only. Update the deployment configuration and redeploy to change upstreams or modes." }}
            </p>
          </div>
          <span class="shrink-0 text-xs tabular-nums text-[#52525B]" aria-live="polite">{{ catalogUpdatedLabel }}</span>
        </div>

        <div v-if="upstreamsData.upstreams.length === 0" class="p-6 text-center text-[13px] text-[#71717A]">
          No upstreams match the active filters.
        </div>

        <div v-else class="relative overflow-x-auto">
          <table class="w-full border-collapse text-left text-xs">
            <thead>
              <tr class="border-b border-[#E4E4E7] bg-[#F4F4F5] font-medium text-[#52525B]">
                <th class="px-3 py-2">Name</th>
                <th class="hidden lg:table-cell px-2.5 py-2">Type / Auth</th>
                <th class="hidden xl:table-cell px-2.5 py-2">Groups</th>
                <th class="px-2.5 py-2 text-right">Tools</th>
                <th class="px-2.5 py-2 text-right">Calls</th>
                <th class="px-2.5 py-2 text-right">Errors</th>
                <th class="hidden md:table-cell px-2.5 py-2 text-right">Mean latency</th>
                <th class="hidden xl:table-cell px-2.5 py-2 text-right">Timeouts</th>
                <th class="px-2.5 py-2 text-center">Mode</th>
                <th v-if="upstreamsData.dynamic_upstreams_permitted" class="w-10 px-2 py-2 text-right">
                  <span class="sr-only">Actions</span>
                </th>
              </tr>
            </thead>
            <tbody class="divide-y divide-[#E4E4E7]">
              <template v-for="u in upstreamsData.upstreams" :key="u.name">
                <tr
                  class="cursor-pointer hover:bg-[#FAFAFA]"
                  :class="expandedUpstreamName === u.name ? 'bg-[#F0FDFA]' : ''"
                  @click="toggleExpandUpstream(u.name)"
                >
                  <td class="px-3 py-2">
                    <Button
                      data-variant="ghost"
                      :aria-expanded="expandedUpstreamName === u.name"
                      class="flex min-h-11 items-center gap-1.5 px-0 font-mono-data font-medium text-zinc-950 sm:min-h-9"
                      @click.stop="toggleExpandUpstream(u.name)"
                    >
                      <AppIcon :name="expandedUpstreamName === u.name ? 'chevron-down' : 'chevron-right'" :size="14" class="text-[#71717A]" />
                      <span class="max-w-[160px] truncate" :title="u.name">{{ u.name }}</span>
                    </Button>
                    <div class="max-w-[160px] truncate text-[11px] text-[#71717A]" :title="u.url || 'stdio/inline'">
                      {{ u.url || "stdio/inline" }}
                    </div>
                    <div class="text-[10px] tabular-nums text-[#71717A]">
                      {{ u.request_timeout_seconds ?? 15 }}s · {{ u.max_requests_per_minute ?? "unlimited" }}/min · {{ u.max_concurrent_calls ?? "unlimited" }} in flight
                    </div>
                    <div class="mt-1 grid gap-0.5 text-[11px] leading-4 text-[#52525B] lg:hidden">
                      <span>{{ upstreamTypeLabel(u.type) }} · {{ authTypeLabel(u.auth_type) }}</span>
                      <span class="break-words">Groups: {{ groupSummary(u) }}</span>
                      <span class="tabular-nums md:hidden">{{ latencyLabel(u) }} · {{ aggregatedToolsForUpstream(u).reduce((sum, tool) => sum + tool.timeouts, 0) }} timeouts</span>
                    </div>
                    <div v-if="u.health_check?.enabled" class="mt-1 text-[11px]" :class="healthForUpstream(u.name)?.status === 'healthy' ? 'text-[#059669]' : healthForUpstream(u.name)?.status === 'unhealthy' ? 'text-[#DC2626]' : 'text-[#71717A]'">
                      Probe: {{ healthForUpstream(u.name)?.status || "waiting" }}<span v-if="healthForUpstream(u.name)?.checked_at"> · {{ new Date(healthForUpstream(u.name)!.checked_at).toLocaleTimeString() }}</span>
                    </div>
                  </td>
                  <td class="hidden lg:table-cell px-2.5 py-2 text-[#52525B] whitespace-nowrap">
                    {{ u.type }} · {{ u.auth_type }}
                  </td>
                  <td class="hidden xl:table-cell px-2.5 py-2 text-[#52525B] max-w-[140px] truncate" :title="(u.allowed_groups || []).join(', ') || 'open'">
                    {{ (u.allowed_groups || []).join(", ") || "open" }}
                  </td>
                  <td class="px-2.5 py-2 text-right font-mono-data tabular-nums whitespace-nowrap">
                    {{ serverStats(u).toolCount }}
                  </td>
                  <td class="px-2.5 py-2 text-right font-mono-data tabular-nums whitespace-nowrap">
                    {{ serverStats(u).runs }}
                  </td>
                  <td class="px-2.5 py-2 text-right font-mono-data tabular-nums whitespace-nowrap" :title="serverStats(u).errors == null ? 'No error measurement was captured' : `${serverStats(u).errors} errors in retained telemetry`">
                    {{ serverStats(u).errors == null ? "Not captured" : serverStats(u).errors }}
                  </td>
                  <td class="hidden md:table-cell px-2.5 py-2 text-right font-mono-data tabular-nums whitespace-nowrap">
                    {{ serverStats(u).avg_duration_ms == null ? "Not captured" : `${serverStats(u).avg_duration_ms} ms` }}
                  </td>
                  <td class="hidden xl:table-cell px-2.5 py-2 text-right font-mono-data tabular-nums whitespace-nowrap">
                    {{ aggregatedToolsForUpstream(u).reduce((sum, tool) => sum + tool.timeouts, 0) }}
                  </td>
                  <td class="px-2.5 py-2 text-center whitespace-nowrap" @click.stop>
                    <select
                      :value="u.preferred_mode || 'auto'"
                      aria-label="Server preferred mode"
                      :disabled="!upstreamsData.dynamic_upstreams_permitted"
                      :title="upstreamsData.dynamic_upstreams_permitted ? 'Change the default mode for this upstream. Individual tool overrides and explicit request modes may take precedence.' : 'Modes are read-only here; change config/upstreams.yaml and deploy the configuration'"
                      class="min-h-11 rounded border border-[#D4D4D8] bg-white px-2 py-1 text-xs font-mono-data focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-[#0F766E] disabled:cursor-not-allowed disabled:bg-[#F4F4F5] disabled:text-[#71717A] sm:min-h-9"
                      @change="(e) => { const el = e.target as HTMLSelectElement; const next = el.value; el.value = u.preferred_mode || 'auto'; promptModeChange('server', u, next); }"
                    >
                      <option value="auto">auto</option>
                      <option value="codemode">codemode</option>
                      <option value="traditional">traditional</option>
                    </select>
                  </td>
                  <td v-if="upstreamsData.dynamic_upstreams_permitted" class="w-10 px-2 py-2 text-right whitespace-nowrap" @click.stop>
                    <Button
                      data-variant="danger"
                      class="min-h-11 min-w-11 px-2 py-1 sm:min-h-9 sm:min-w-9"
                      aria-label="Remove upstream"
                      title="Remove upstream"
                      @click="promptDelete(u.name)"
                    >
                      <AppIcon name="trash" :size="14" />
                    </Button>
                  </td>
                </tr>
                <tr v-if="expandedUpstreamName === u.name" class="bg-[#FAFAFA]">
                  <td :colspan="upstreamsData.dynamic_upstreams_permitted ? 10 : 9" class="px-3 py-3 sm:px-6">
                    <section class="mb-3 max-w-[calc(100vw-4rem)] border-b border-zinc-200 pb-3 lg:max-w-none" aria-label="Tool catalog">
                      <h3 class="mb-2 text-[0.9375rem] font-semibold text-zinc-950">Tool catalog</h3>
                      <ul v-if="u.tools?.length" class="flex flex-wrap gap-2">
                        <li v-for="tool in u.tools" :key="tool" class="min-w-0 max-w-full">
                          <Button class="min-h-11 max-w-full whitespace-normal break-all text-left sm:min-h-9" @click="openTool(u.name, tool, $event)">
                            {{ tool.startsWith(`${u.name}__`) ? tool.slice(u.name.length + 2) : tool }}
                          </Button>
                        </li>
                      </ul>
                      <p v-else class="text-xs text-zinc-600">No tools discovered for this upstream yet.</p>
                    </section>
                    <UpstreamWorkflowCatalog
                      :upstream="u.name"
                      :tools="u.tools ?? []"
                      :can-edit="upstreamsData.dynamic_upstreams_permitted"
                      :refresh-version="catalogRefreshVersion"
                      @refresh-catalog="handleRefreshCatalog"
                    />
                    <div v-if="healthForUpstream(u.name)" class="mb-3 border-b border-[#E4E4E7] pb-3 text-xs" role="status">
                      <span class="font-medium text-[#09090B]">Active probe: {{ healthForUpstream(u.name)?.status }}</span>
                      <span class="ml-2 text-[#52525B]">Checked {{ healthForUpstream(u.name)?.checked_at ? new Date(healthForUpstream(u.name)!.checked_at).toLocaleString() : "not yet" }}</span>
                      <span v-if="healthForUpstream(u.name)?.error_type" class="ml-2 font-mono-data text-[#DC2626]">{{ healthForUpstream(u.name)?.error_type }} ({{ healthForUpstream(u.name)?.consecutive_failures }} consecutive failures)</span>
                      <p v-if="healthForUpstream(u.name)?.error" class="mt-1 text-[#52525B]">{{ healthForUpstream(u.name)?.error }}</p>
                    </div>
                    <div v-if="aggregatedToolsForUpstream(u).length === 0" class="text-xs text-[#71717A]">
                      No calls recorded for this upstream yet.
                    </div>
                    <div v-else>
                      <p class="mb-2 text-[11px] leading-4 text-[#71717A]">Observed dispatcher calls, payload sizes, latency, failures, and schema metadata. Values marked “Not captured” were not recorded in the available history.</p>
                      <dl class="divide-y divide-[#E4E4E7] border-y border-[#E4E4E7] text-xs md:hidden">
                        <div v-for="t in aggregatedToolsForUpstream(u)" :key="t.tool" class="space-y-2 py-3">
                          <dt class="break-all font-mono-data font-medium text-[#09090B]">{{ t.tool }}</dt>
                          <dd class="grid grid-cols-2 gap-x-3 gap-y-1 text-[#52525B]">
                            <span class="tabular-nums">Calls: {{ t.count }}</span>
                            <span class="tabular-nums">Bytes: {{ t.bytes.toLocaleString() }}</span>
                            <span class="tabular-nums">Mean / p95: {{ t.mean_latency_ms == null ? "Not captured" : `${t.mean_latency_ms} / ${t.p95_latency_ms ?? "Not captured"} ms` }}</span>
                            <span class="tabular-nums">Errors / timeouts: {{ t.errors == null ? "Not captured" : t.errors }} / {{ t.timeouts }}</span>
                            <span>Error categories: {{ Object.entries(t.error_types || {}).map(([kind, count]) => `${kind}: ${count}`).join(", ") || "None recorded" }}</span>
                            <span>Schema coverage: {{ t.schema_coverage_pct == null ? "Not captured" : `${t.schema_coverage_pct}%` }}</span>
                            <span>Read / write calls: {{ t.read_calls == null ? "Not captured" : `${t.read_calls} / ${t.write_calls ?? "Not captured"}` }}</span>
                            <span>Invalid arguments: {{ t.invalid_arguments == null ? "Not captured" : t.invalid_arguments }}</span>
                          </dd>
                        </div>
                      </dl>
                      <div class="hidden overflow-x-auto md:block">
                      <table class="w-full text-left text-xs tabular-nums">
                        <thead class="border-b border-[#E4E4E7] text-[#71717A]">
                          <tr>
                            <th class="py-1.5 pr-3 font-medium">Tool</th>
                            <th class="py-1.5 pr-3 font-medium text-right">Calls</th>
                            <th class="py-1.5 pr-3 font-medium text-right">Bytes</th>
                            <th class="py-1.5 pr-3 font-medium text-right">Mean / p95 latency</th>
                            <th class="py-1.5 pr-3 font-medium text-right">Errors / timeouts</th>
                            <th class="py-1.5 pr-3 font-medium">Error categories</th>
                            <th class="py-1.5 pr-3 font-medium">Schema coverage</th>
                            <th class="py-1.5 pr-3 font-medium">Read / write calls</th>
                            <th class="py-1.5 font-medium">Invalid arguments</th>
                          </tr>
                        </thead>
                        <tbody class="divide-y divide-[#E4E4E7]">
                          <tr v-for="t in aggregatedToolsForUpstream(u)" :key="t.tool">
                            <td class="py-2 pr-3 font-mono-data text-[#09090B]">{{ t.tool }}</td>
                            <td class="py-2 pr-3 text-right font-mono-data tabular-nums">{{ t.count }}</td>
                            <td class="py-2 pr-3 text-right font-mono-data tabular-nums">{{ t.bytes.toLocaleString() }}</td>
                            <td class="py-2 pr-3 text-right font-mono-data tabular-nums">{{ t.mean_latency_ms == null ? "Not captured" : `${t.mean_latency_ms} / ${t.p95_latency_ms ?? "Not captured"} ms` }}</td>
                            <td class="py-2 pr-3 text-right font-mono-data tabular-nums">{{ t.errors == null ? "Not captured" : t.errors }} / {{ t.timeouts }}</td>
                            <td class="py-2 pr-3 text-[#52525B]">{{ Object.entries(t.error_types || {}).map(([kind, count]) => `${kind}: ${count}`).join(", ") || "None recorded" }}</td>
                            <td class="py-2 pr-3 tabular-nums">{{ t.schema_coverage_pct == null ? "Not captured" : `${t.schema_coverage_pct}%` }}</td>
                            <td class="py-2 pr-3 tabular-nums">{{ t.read_calls == null ? "Not captured" : `${t.read_calls} / ${t.write_calls ?? "Not captured"}` }}</td>
                            <td class="py-2 tabular-nums">{{ t.invalid_arguments == null ? "Not captured" : t.invalid_arguments }}</td>
                          </tr>
                        </tbody>
                      </table>
                      </div>
                    </div>
                  </td>
                </tr>
              </template>
            </tbody>
          </table>
        </div>

        <div
          v-if="(upstreamsData.total_pages ?? 1) > 1"
          class="flex items-center justify-between border-t border-[#E4E4E7] bg-[#FAFAFA] px-4 py-2.5 text-xs text-[#52525B]"
        >
          <span class="tabular-nums">
            Page {{ upstreamsData.page ?? 1 }} of {{ upstreamsData.total_pages ?? 1 }}
          </span>
          <div class="flex gap-2">
            <Button :disabled="(upstreamsData.page ?? 1) <= 1" @click="page -= 1; void fetchData()">
              Previous
            </Button>
            <Button
              :disabled="(upstreamsData.page ?? 1) >= (upstreamsData.total_pages ?? 1)"
              @click="page += 1; void fetchData()"
            >
              Next
            </Button>
          </div>
        </div>
      </section>
    </template>

    <ToolGuidanceDrawer
      v-if="selectedTool"
      v-model:open="toolDrawerOpen"
      :upstream="selectedTool.upstream"
      :tool="selectedTool.tool"
      :opener="selectedTool.opener"
      :upstream-config="upstreamsData?.upstreams.find((upstream) => upstream.name === selectedTool?.upstream) || {}"
      :demo-mode="upstreamsData?.profile === 'demo'"
    />

    <Dialog v-model:open="deleteDialogOpen">
      <DialogBackdrop />
      <DialogContent>
        <DialogTitle>Delete dynamic upstream?</DialogTitle>
        <DialogDescription>
          Removing <strong>{{ deleteTarget }}</strong> stops new MCP calls to that service immediately. Reconnect it later through Onboarding or restore its GitOps configuration.
        </DialogDescription>
        <p v-if="deleteError" role="alert" class="rounded-md border border-[#FECACA] bg-[#FEF2F2] p-3 text-xs text-[#991B1B]">
          Removal failed: {{ deleteError }}. The upstream is still registered; retry or cancel.
        </p>
        <div class="flex justify-end gap-2 pt-2">
          <DialogClose>Cancel</DialogClose>
          <Button
            data-variant="danger"
            :disabled="deleteSaving"
            data-agent-confirm="Permanently delete dynamic upstream?"
            pending-announcement="Deleting upstream"
            @click="confirmDelete"
          >
            {{ deleteSaving ? "Removing…" : "Remove upstream" }}
          </Button>
        </div>
      </DialogContent>
    </Dialog>

    <Dialog v-model:open="modeDialogOpen">
      <DialogContent>
        <DialogTitle>Confirm Mode Change</DialogTitle>
        <DialogDescription v-if="pendingModeChange">
          <p>
            Set <strong>{{ pendingModeChange.kind === 'server' ? pendingModeChange.upstream.name : pendingModeChange.tool }}</strong>
            to <code class="font-mono-data">{{ pendingModeChange.nextMode }}</code>?
          </p>
          <p class="mt-2">{{ modeChangeImpact() }}</p>
        </DialogDescription>
        <p v-if="modeError" role="alert" class="rounded-md border border-[#FECACA] bg-[#FEF2F2] p-3 text-xs text-[#991B1B]">
          Change failed: {{ modeError }}. The saved mode is unchanged; retry or cancel.
        </p>
        <div class="flex justify-end gap-2 pt-2">
          <DialogClose :disabled="modeSaving" @click="pendingModeChange = null">Cancel</DialogClose>
          <Button data-variant="primary" :disabled="modeSaving" @click="confirmModeChange">
            {{ modeSaving ? "Saving…" : "Save mode" }}
          </Button>
        </div>
      </DialogContent>
    </Dialog>
  </div>
</template>
