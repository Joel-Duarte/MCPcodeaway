<script setup lang="ts">
import { extractErrorMessage } from "~/composables/useAdminApi";

interface UpstreamEntry {
  name: string;
  type: string;
  url?: string | null;
  auth_type: string;
  stateful_session?: boolean;
  preferred_mode?: string;
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
const statefulFilter = ref<"all" | "stateful" | "stateless">("all");
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
    if (statefulFilter.value === "stateful") params.set("stateful", "true");
    if (statefulFilter.value === "stateless") params.set("stateful", "false");
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

watch([groupFilter, authFilter, statefulFilter, modeFilter], () => {
  void fetchData(true);
});

function clearFilters() {
  searchQuery.value = "";
  groupFilter.value = "";
  authFilter.value = "";
  statefulFilter.value = "all";
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
        <h1 class="text-[20px] font-semibold tracking-[-0.015em] text-[#09090B]">
          Upstreams Service Catalog
        </h1>
        <p class="mt-0.5 text-[13px] text-[#52525B]">
          Search, filter, and manage registered MCP/OpenAPI services, stateful session configuration, and schema digests.
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
          <label class="block text-xs font-medium text-[#52525B]">Session &amp; Mode</label>
          <div class="mt-1 flex gap-1.5">
            <select v-model="statefulFilter" class="w-1/2 rounded-md border border-[#E4E4E7] bg-white px-2 py-1.5 text-xs">
              <option value="all">All Sessions</option>
              <option value="stateful">Stateful</option>
              <option value="stateless">Stateless</option>
            </select>
            <select v-model="modeFilter" class="w-1/2 rounded-md border border-[#E4E4E7] bg-white px-2 py-1.5 text-xs">
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

        <div v-else class="overflow-x-auto table-scroll">
          <table class="w-full border-collapse text-left text-[13px]">
            <thead>
              <tr class="border-b border-[#E4E4E7] bg-[#F4F4F5] text-xs font-medium text-[#52525B]">
                <th class="px-4 py-2.5 whitespace-nowrap">Name</th>
                <th class="px-4 py-2.5 whitespace-nowrap">Type / Auth</th>
                <th class="px-4 py-2.5 whitespace-nowrap">Groups</th>
                <th class="px-4 py-2.5 whitespace-nowrap">Stateful MCP</th>
                <th class="px-4 py-2.5 whitespace-nowrap">Preferred Mode</th>
                <th class="px-4 py-2.5 whitespace-nowrap">Digest</th>
                <th v-if="upstreamsData.dynamic_upstreams_permitted" class="px-4 py-2.5 whitespace-nowrap text-right">
                  Actions
                </th>
              </tr>
            </thead>
            <tbody class="divide-y divide-[#E4E4E7]">
              <tr v-for="u in upstreamsData.upstreams" :key="u.name" class="hover:bg-[#FAFAFA]">
                <td class="px-4 py-2.5 whitespace-nowrap">
                  <div class="font-mono-data font-medium text-[#09090B]">{{ u.name }}</div>
                  <div class="max-w-[240px] truncate text-xs text-[#71717A]">{{ u.url || "stdio/inline" }}</div>
                </td>
                <td class="px-4 py-2.5 text-xs text-[#52525B] whitespace-nowrap">
                  {{ u.type }} · {{ u.auth_type }}
                </td>
                <td class="px-4 py-2.5 text-xs text-[#52525B] whitespace-nowrap">
                  {{ (u.allowed_groups || []).join(", ") || "open" }}
                </td>
                <td class="px-4 py-2.5 text-xs whitespace-nowrap">
                  <span
                    class="rounded border px-2 py-0.5 font-medium"
                    :class="
                      u.stateful_session
                        ? 'border-[#99F6E4] bg-[#F0FDFA] text-[#0F766E]'
                        : 'border-[#E4E4E7] bg-[#F4F4F5] text-[#52525B]'
                    "
                  >
                    {{ u.stateful_session ? "Redis Mcp-Session" : "Stateless" }}
                  </span>
                </td>
                <td class="px-4 py-2.5 font-mono-data text-xs text-[#09090B] whitespace-nowrap">
                  {{ u.preferred_mode || "codemode" }}
                </td>
                <td class="px-4 py-2.5 font-mono-data text-xs text-[#71717A] whitespace-nowrap">
                  {{ (upstreamsData.digests_by_server[u.name] || "—").slice(0, 12) }}
                </td>
                <td v-if="upstreamsData.dynamic_upstreams_permitted" class="px-4 py-2.5 text-right whitespace-nowrap">
                  <Button data-variant="danger" @click="promptDelete(u.name)">
                    <AppIcon name="trash" :size="14" />
                    <span>Remove</span>
                  </Button>
                </td>
              </tr>
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
  </div>
</template>
