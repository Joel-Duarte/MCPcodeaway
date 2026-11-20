<script setup lang="ts">
import { extractErrorMessage } from "~/composables/useAdminApi";

const { apiFetch: _apiFetch } = useAdminApi();
const expandedUpstreamName = ref<string | null>(null);
const perfRows = ref<Array<{ model: string; server: string; tool: string; mode: string; runs: number; success_rate_pct: number; avg_duration_ms: number; avg_tokens: number; recommended_mode: string }>>([]);

async function loadPerf() {
  try {
    const res = await _apiFetch<{ rows: typeof perfRows.value }>("/admin/ab/model-performance");
    if (res?.rows) perfRows.value = res.rows;
  } catch {
    // ignore
  }
}

function toggleExpandUpstream(name: string) {
  expandedUpstreamName.value = expandedUpstreamName.value === name ? null : name;
}

function aggregatedToolsForUpstream(u: UpstreamEntry) {
  const toolNames = new Set<string>(u.tools || []);
  for (const r of perfRows.value) {
    if (r.server === u.name) toolNames.add(r.tool);
  }
  return Array.from(toolNames)
    .sort()
    .map((tool) => {
      const matching = perfRows.value.filter((r) => r.server === u.name && r.tool === tool);
      const runs = matching.reduce((s, r) => s + r.runs, 0);
      const okWeighted = matching.reduce((s, r) => s + (r.success_rate_pct / 100) * r.runs, 0);
      const msWeighted = matching.reduce((s, r) => s + r.avg_duration_ms * r.runs, 0);
      const tokWeighted = matching.reduce((s, r) => s + (r.avg_tokens || 0) * r.runs, 0);
      return {
        tool,
        runs,
        success_rate_pct: runs ? Math.round((okWeighted / runs) * 1000) / 10 : 0,
        avg_duration_ms: runs ? Math.round((msWeighted / runs) * 10) / 10 : 0,
        avg_tokens: runs ? Math.round(tokWeighted / runs) : 0,
        recommended_mode: matching[0]?.recommended_mode || "—",
      };
    });
}

function serverStats(u: UpstreamEntry) {
  const tools = aggregatedToolsForUpstream(u);
  const runs = tools.reduce((s, t) => s + t.runs, 0);
  const okWeighted = tools.reduce((s, t) => s + (t.success_rate_pct / 100) * t.runs, 0);
  const msWeighted = tools.reduce((s, t) => s + t.avg_duration_ms * t.runs, 0);
  const tokWeighted = tools.reduce((s, t) => s + t.avg_tokens * t.runs, 0);
  return {
    toolCount: tools.length,
    runs,
    success_rate_pct: runs ? Math.round((okWeighted / runs) * 1000) / 10 : 0,
    avg_duration_ms: runs ? Math.round((msWeighted / runs) * 10) / 10 : 0,
    avg_tokens: runs ? Math.round(tokWeighted / runs) : 0,
  };
}

async function setUpstreamPreferredMode(u: UpstreamEntry, mode: string) {
  await _apiFetch(`/admin/upstreams/${encodeURIComponent(u.name)}/mode`, {
    method: "PATCH",
    body: JSON.stringify({ preferred_mode: mode }),
  });
  u.preferred_mode = mode;
}

async function setToolModeOverride(u: UpstreamEntry, toolName: string, mode: string) {
  const res = await _apiFetch<{ tool_mode_overrides: Record<string, string> }>(
    `/admin/upstreams/${encodeURIComponent(u.name)}/tools/${encodeURIComponent(toolName)}/mode`,
    { method: "PATCH", body: JSON.stringify({ mode }) }
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
  pendingModeChange.value = { kind, upstream, tool, nextMode };
  modeDialogOpen.value = true;
}

async function confirmModeChange() {
  const pending = pendingModeChange.value;
  if (!pending) return;
  errorMessage.value = "";
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
  }
}

let _pollTimer: ReturnType<typeof setInterval> | null = null;

onMounted(() => {
  void loadPerf();
  _pollTimer = setInterval(() => {
    void loadPerf();
    void fetchData();
  }, 3000);
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
const upstreamsData = ref<UpstreamsResponse | null>(null);
const deleteTarget = ref<string | null>(null);
const deleteDialogOpen = ref(false);

// 500-server scale search, filters & pagination
const searchQuery = ref("");
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
  try {
    const res = await apiFetch<{ catalog_version: string; catalog_epoch?: number }>(
      "/admin/catalog/refresh",
      { method: "POST" }
    );
    statusMessage.value = `Catalog refreshed (${res.catalog_version}, epoch ${res.catalog_epoch ?? 0}).`;
    await fetchData();
  } catch (err) {
    errorMessage.value = extractErrorMessage(err);
  }
}

function promptDelete(name: string) {
  deleteTarget.value = name;
  deleteDialogOpen.value = true;
}

async function confirmDelete() {
  if (!deleteTarget.value) return;
  const target = deleteTarget.value;
  errorMessage.value = "";
  try {
    await apiFetch(`/admin/upstreams/${encodeURIComponent(target)}`, { method: "DELETE" });
    statusMessage.value = `Deleted dynamic upstream '${target}'.`;
    deleteDialogOpen.value = false;
    await fetchData();
  } catch (err) {
    errorMessage.value = extractErrorMessage(err);
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
          Search, filter, and manage registered MCP/OpenAPI services and schema digests.
        </p>
      </div>
      <div class="flex flex-wrap items-center gap-2">
        <Button pending-announcement="Refreshing catalog" @click="handleRefreshCatalog">
          <AppIcon name="refresh" :size="15" />
          <span>Refresh Catalog</span>
        </Button>
        <NuxtLink
          to="/onboarding"
          class="no-underline inline-flex items-center gap-1.5 rounded-md border border-[#0F766E] bg-[#0F766E] px-3.5 py-2 text-[13px] font-medium text-white hover:bg-[#115E59]"
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

    <!-- 500-Server Scale Search & Filter Bar -->
    <section class="rounded-lg border border-[#E4E4E7] bg-white p-4 space-y-3">
      <div class="grid gap-3 sm:grid-cols-5">
        <div class="sm:col-span-2">
          <label class="block text-xs font-medium text-[#52525B]">Search Service Name or URL</label>
          <input
            v-model="searchQuery"
            type="search"
            placeholder="e.g. billing, snowflake, https://..."
            class="mt-1 w-full rounded-md border border-[#E4E4E7] bg-white px-3 py-1.5 text-xs text-[#09090B]"
          />
        </div>
        <div>
          <label class="block text-xs font-medium text-[#52525B]">Allowed Group</label>
          <select v-model="groupFilter" class="mt-1 w-full rounded-md border border-[#E4E4E7] bg-white px-2.5 py-1.5 text-xs">
            <option value="">All Groups</option>
            <option v-for="g in upstreamsData?.facets?.groups || []" :key="g" :value="g">{{ g }}</option>
          </select>
        </div>
        <div>
          <label class="block text-xs font-medium text-[#52525B]">Auth Type</label>
          <select v-model="authFilter" class="mt-1 w-full rounded-md border border-[#E4E4E7] bg-white px-2.5 py-1.5 text-xs">
            <option value="">All Auth</option>
            <option v-for="a in upstreamsData?.facets?.auth_types || []" :key="a" :value="a">{{ a }}</option>
          </select>
        </div>
        <div>
          <label class="block text-xs font-medium text-[#52525B]">Preferred Mode</label>
          <div class="mt-1">
            <select v-model="modeFilter" class="w-full rounded-md border border-[#E4E4E7] bg-white px-2 py-1.5 text-xs">
              <option value="">All Modes</option>
              <option v-for="m in upstreamsData?.facets?.modes || []" :key="m" :value="m">{{ m }}</option>
            </select>
          </div>
        </div>
      </div>
      <div class="flex items-center justify-between text-xs text-[#52525B]">
        <span class="tabular-nums">
          Matching {{ upstreamsData?.total ?? upstreamsData?.upstreams?.length ?? 0 }} of
          {{ upstreamsData?.total_registered ?? upstreamsData?.upstreams?.length ?? 0 }} registered servers
        </span>
        <button type="button" class="font-medium text-[#0F766E] hover:underline" @click="clearFilters">
          Reset Filters
        </button>
      </div>
    </section>

    <div v-if="loading" class="h-48 animate-pulse rounded-lg border border-[#E4E4E7] bg-white" />

    <template v-else-if="upstreamsData">
      <section class="rounded-lg border border-[#E4E4E7] bg-white">
        <div class="flex items-center justify-between border-b border-[#E4E4E7] px-4 py-3">
          <h2 class="text-[15px] font-semibold text-[#09090B]">Registered Upstreams</h2>
          <span class="text-xs text-[#52525B]">
            {{ upstreamsData.dynamic_upstreams_permitted ? "Dynamic writes enabled" : "GitOps read-only mode" }}
          </span>
        </div>

        <div v-if="upstreamsData.upstreams.length === 0" class="p-6 text-center text-[13px] text-[#71717A]">
          No upstreams match the active filters.
        </div>

        <div v-else class="overflow-x-auto">
          <table class="w-full border-collapse text-left text-xs">
            <thead>
              <tr class="border-b border-[#E4E4E7] bg-[#F4F4F5] font-medium text-[#52525B]">
                <th class="px-3 py-2">Name</th>
                <th class="hidden lg:table-cell px-2.5 py-2">Type / Auth</th>
                <th class="hidden xl:table-cell px-2.5 py-2">Groups</th>
                <th class="px-2.5 py-2 text-right">Tools</th>
                <th class="px-2.5 py-2 text-right">Runs</th>
                <th class="px-2.5 py-2 text-right">Success</th>
                <th class="hidden md:table-cell px-2.5 py-2 text-right">Latency</th>
                <th class="hidden xl:table-cell px-2.5 py-2 text-right">Tokens</th>
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
                    <div class="flex items-center gap-1.5 font-mono-data font-medium text-[#09090B]">
                      <span class="text-[10px] text-[#71717A]">{{ expandedUpstreamName === u.name ? "▼" : "▶" }}</span>
                      <span class="max-w-[160px] truncate" :title="u.name">{{ u.name }}</span>
                    </div>
                    <div class="max-w-[160px] truncate text-[11px] text-[#71717A]" :title="u.url || 'stdio/inline'">
                      {{ u.url || "stdio/inline" }}
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
                  <td class="px-2.5 py-2 text-right font-mono-data tabular-nums whitespace-nowrap">
                    {{ serverStats(u).runs ? `${serverStats(u).success_rate_pct}%` : "—" }}
                  </td>
                  <td class="hidden md:table-cell px-2.5 py-2 text-right font-mono-data tabular-nums whitespace-nowrap">
                    {{ serverStats(u).runs ? `${serverStats(u).avg_duration_ms} ms` : "—" }}
                  </td>
                  <td class="hidden xl:table-cell px-2.5 py-2 text-right font-mono-data tabular-nums whitespace-nowrap">
                    {{ serverStats(u).runs ? serverStats(u).avg_tokens : "—" }}
                  </td>
                  <td class="px-2.5 py-2 text-center whitespace-nowrap" @click.stop>
                    <select
                      :value="u.preferred_mode || 'auto'"
                      aria-label="Server preferred mode"
                      class="rounded border border-[#D4D4D8] bg-white px-2 py-1 text-xs font-mono-data"
                      @change="(e) => { const el = e.target as HTMLSelectElement; const next = el.value; el.value = u.preferred_mode || 'auto'; promptModeChange('server', u, next); }"
                    >
                      <option value="auto">auto</option>
                      <option value="codemode">codemode</option>
                      <option value="traditional">traditional</option>
                      <option value="hybrid">hybrid</option>
                    </select>
                  </td>
                  <td v-if="upstreamsData.dynamic_upstreams_permitted" class="w-10 px-2 py-2 text-right whitespace-nowrap" @click.stop>
                    <Button
                      data-variant="danger"
                      class="px-2 py-1"
                      aria-label="Remove upstream"
                      title="Remove upstream"
                      @click="promptDelete(u.name)"
                    >
                      <AppIcon name="trash" :size="14" />
                    </Button>
                  </td>
                </tr>
                <tr v-if="expandedUpstreamName === u.name" class="bg-[#FAFAFA]">
                  <td :colspan="upstreamsData.dynamic_upstreams_permitted ? 10 : 9" class="px-6 py-3">
                    <div v-if="aggregatedToolsForUpstream(u).length === 0" class="text-xs text-[#71717A]">
                      No tools discovered for this upstream yet.
                    </div>
                    <div v-else class="overflow-x-auto">
                      <table class="w-full text-left text-xs tabular-nums">
                        <thead class="border-b border-[#E4E4E7] text-[#71717A]">
                          <tr>
                            <th class="py-1.5 pr-3 font-medium">Tool</th>
                            <th class="py-1.5 pr-3 font-medium text-right">Runs</th>
                            <th class="py-1.5 pr-3 font-medium text-right">Success</th>
                            <th class="py-1.5 pr-3 font-medium text-right">Avg Latency</th>
                            <th class="py-1.5 pr-3 font-medium text-right">Avg Tokens</th>
                            <th class="py-1.5 pr-3 font-medium">Best Mode</th>
                            <th class="py-1.5 font-medium">Override (All Models)</th>
                          </tr>
                        </thead>
                        <tbody class="divide-y divide-[#E4E4E7]">
                          <tr v-for="t in aggregatedToolsForUpstream(u)" :key="t.tool">
                            <td class="py-2 pr-3 font-mono-data text-[#09090B]">{{ t.tool }}</td>
                            <td class="py-2 pr-3 text-right font-mono-data">{{ t.runs }}</td>
                            <td class="py-2 pr-3 text-right font-mono-data">{{ t.runs ? `${t.success_rate_pct}%` : "—" }}</td>
                            <td class="py-2 pr-3 text-right font-mono-data">{{ t.runs ? `${t.avg_duration_ms} ms` : "—" }}</td>
                            <td class="py-2 pr-3 text-right font-mono-data">{{ t.runs ? t.avg_tokens : "—" }}</td>
                            <td class="py-2 pr-3 font-mono-data font-semibold text-[#0F766E]">{{ t.recommended_mode }}</td>
                            <td class="py-2">
                              <select
                                :value="u.tool_mode_overrides?.[t.tool] || 'inherit'"
                                aria-label="Tool mode override"
                                class="rounded border border-[#D4D4D8] bg-white px-2 py-1 text-xs"
                                @change="(e) => { const el = e.target as HTMLSelectElement; const next = el.value; el.value = u.tool_mode_overrides?.[t.tool] || 'inherit'; promptModeChange('tool', u, next, t.tool); }"
                              >
                                <option value="inherit">Inherit Server Default</option>
                                <option value="auto">auto</option>
                                <option value="codemode">codemode</option>
                                <option value="traditional">traditional</option>
                              </select>
                            </td>
                          </tr>
                        </tbody>
                      </table>
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

    <Dialog v-model:open="deleteDialogOpen">
      <DialogBackdrop />
      <DialogContent>
        <DialogTitle>Delete dynamic upstream?</DialogTitle>
        <DialogDescription>
          Removing <strong>{{ deleteTarget }}</strong> stops routing new MCP subcalls to it immediately.
        </DialogDescription>
        <div class="flex justify-end gap-2 pt-2">
          <DialogClose>Cancel</DialogClose>
          <Button
            data-variant="danger"
            data-agent-confirm="Permanently delete dynamic upstream?"
            pending-announcement="Deleting upstream"
            @click="confirmDelete"
          >
            Delete Upstream
          </Button>
        </div>
      </DialogContent>
    </Dialog>

    <Dialog v-model:open="modeDialogOpen">
      <DialogContent>
        <DialogTitle>Confirm Mode Change</DialogTitle>
        <DialogDescription v-if="pendingModeChange">
          Switch
          <strong>{{ pendingModeChange.kind === 'server' ? pendingModeChange.upstream.name : pendingModeChange.tool }}</strong>
          to <code class="font-mono-data">{{ pendingModeChange.nextMode }}</code>?
        </DialogDescription>
        <div class="flex justify-end gap-2 pt-2">
          <DialogClose @click="pendingModeChange = null">Cancel</DialogClose>
          <Button data-variant="primary" @click="confirmModeChange">
            Confirm Change
          </Button>
        </div>
      </DialogContent>
    </Dialog>
  </div>
</template>
