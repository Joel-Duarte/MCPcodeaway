<script setup lang="ts">
import { extractErrorMessage } from "~/composables/useAdminApi";

interface UpstreamEntry {
  name: string;
  type: string;
  url?: string | null;
  auth_type: string;
  stateful_session?: boolean;
  preferred_mode?: string;
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
}

interface ExperimentsResponse {
  configured: {
    upstream: string;
    preferred_mode: string;
    ab_experiment: { enabled: boolean; experiment_id?: string | null; codemode_traffic_pct: number } | null;
  }[];
  results: Record<
    string,
    {
      arms: Record<
        string,
        { runs: number; error_rate_pct: number; avg_duration_ms: number; avg_tokens_saved: number; total_tokens_saved: number }
      >;
      recommended_winner: string;
    }
  >;
}

const { apiFetch, setRuntimeMeta } = useAdminApi();

const loading = ref(true);
const errorMessage = ref("");
const statusMessage = ref("");
const upstreamsData = ref<UpstreamsResponse | null>(null);
const experimentsData = ref<ExperimentsResponse | null>(null);
const deleteTarget = ref<string | null>(null);
const deleteDialogOpen = ref(false);

async function fetchData() {
  errorMessage.value = "";
  try {
    const [ups, exps] = await Promise.all([
      apiFetch<UpstreamsResponse>("/admin/upstreams"),
      apiFetch<ExperimentsResponse>("/admin/ab/experiments"),
    ]);
    upstreamsData.value = ups;
    experimentsData.value = exps;
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

const experimentRows = computed(() => {
  const results = experimentsData.value?.results || {};
  return Object.entries(results).map(([id, info]) => ({ id, ...info }));
});
</script>

<template>
  <div class="space-y-6">
    <div class="flex flex-col gap-3 sm:flex-row sm:items-center sm:justify-between">
      <div>
        <h1 class="text-[20px] font-semibold tracking-[-0.015em] text-[#09090B]">
          Upstreams Catalog &amp; Live A/B Splits
        </h1>
        <p class="mt-0.5 text-[13px] text-[#52525B]">
          Registered MCP/OpenAPI services, stateful session configuration, schema digests, and live experiment arms.
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
          <span>Onboard &amp; A/B Test</span>
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

    <div v-if="loading" class="h-48 animate-pulse rounded-lg border border-[#E4E4E7] bg-white" />

    <template v-else-if="upstreamsData">
      <!-- Upstreams Table -->
      <section class="rounded-lg border border-[#E4E4E7] bg-white">
        <div class="flex items-center justify-between border-b border-[#E4E4E7] px-4 py-3">
          <h2 class="text-[15px] font-semibold text-[#09090B]">Registered Upstreams</h2>
          <span class="text-xs text-[#52525B]">
            {{ upstreamsData.dynamic_upstreams_permitted ? "Dynamic writes enabled" : "GitOps read-only mode" }}
          </span>
        </div>

        <div v-if="upstreamsData.upstreams.length === 0" class="p-6 text-center text-[13px] text-[#71717A]">
          No upstreams registered in this profile yet.
        </div>

        <div v-else class="overflow-x-auto table-scroll">
          <table class="w-full border-collapse text-left text-[13px]">
            <thead>
              <tr class="border-b border-[#E4E4E7] bg-[#F4F4F5] text-xs font-medium text-[#52525B]">
                <th class="px-4 py-2.5 whitespace-nowrap">Name</th>
                <th class="px-4 py-2.5 whitespace-nowrap">Type / Auth</th>
                <th class="px-4 py-2.5 whitespace-nowrap">Stateful MCP</th>
                <th class="px-4 py-2.5 whitespace-nowrap">Preferred Mode</th>
                <th class="px-4 py-2.5 whitespace-nowrap">Live A/B Split</th>
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
                <td class="px-4 py-2.5 text-xs tabular-nums whitespace-nowrap">
                  <span v-if="u.ab_experiment?.enabled" class="text-[#0F766E] font-medium">
                    {{ u.ab_experiment.codemode_traffic_pct }}% CM / {{ 100 - u.ab_experiment.codemode_traffic_pct }}% CL
                  </span>
                  <span v-else class="text-[#71717A]">Off</span>
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
      </section>

      <!-- Live A/B Experiments Table -->
      <section class="rounded-lg border border-[#E4E4E7] bg-white">
        <div class="border-b border-[#E4E4E7] px-4 py-3">
          <h2 class="text-[15px] font-semibold text-[#09090B]">Live Experiment Results</h2>
        </div>

        <div v-if="experimentRows.length === 0" class="p-6 text-center text-[13px] text-[#71717A]">
          No traffic split experiments have accumulated runs yet. Enable `ab_experiment` on an upstream in Onboarding.
        </div>

        <div v-else class="overflow-x-auto table-scroll">
          <table class="w-full border-collapse text-left text-[13px]">
            <thead>
              <tr class="border-b border-[#E4E4E7] bg-[#F4F4F5] text-xs font-medium text-[#52525B]">
                <th class="px-4 py-2.5 whitespace-nowrap">Experiment ID</th>
                <th class="px-4 py-2.5 whitespace-nowrap">Code Mode Arm</th>
                <th class="px-4 py-2.5 whitespace-nowrap">Classic Arm</th>
                <th class="px-4 py-2.5 whitespace-nowrap">Recommended Winner</th>
              </tr>
            </thead>
            <tbody class="divide-y divide-[#E4E4E7]">
              <tr v-for="exp in experimentRows" :key="exp.id" class="hover:bg-[#FAFAFA]">
                <td class="px-4 py-2.5 font-mono-data font-medium text-[#09090B] whitespace-nowrap">
                  {{ exp.id }}
                </td>
                <td class="px-4 py-2.5 text-xs tabular-nums whitespace-nowrap">
                  <template v-if="exp.arms.codemode">
                    {{ exp.arms.codemode.runs }} runs · {{ exp.arms.codemode.avg_duration_ms.toFixed(1) }} ms · {{ exp.arms.codemode.total_tokens_saved }} tok saved
                  </template>
                  <span v-else class="text-[#71717A]">—</span>
                </td>
                <td class="px-4 py-2.5 text-xs tabular-nums whitespace-nowrap">
                  <template v-if="exp.arms.classic">
                    {{ exp.arms.classic.runs }} runs · {{ exp.arms.classic.avg_duration_ms.toFixed(1) }} ms · err {{ exp.arms.classic.error_rate_pct }}%
                  </template>
                  <span v-else class="text-[#71717A]">—</span>
                </td>
                <td class="px-4 py-2.5 whitespace-nowrap">
                  <span
                    class="rounded border px-2 py-0.5 text-xs font-medium"
                    :class="
                      exp.recommended_winner === 'codemode'
                        ? 'border-[#99F6E4] bg-[#F0FDFA] text-[#0F766E]'
                        : 'border-[#BFDBFE] bg-[#EFF6FF] text-[#2563EB]'
                    "
                  >
                    {{ exp.recommended_winner }}
                  </span>
                </td>
              </tr>
            </tbody>
          </table>
        </div>
      </section>
    </template>

    <!-- Destructive confirmation Dialog -->
    <Dialog v-model:open="deleteDialogOpen">
      <DialogBackdrop />
      <DialogContent>
        <DialogTitle>Remove dynamic upstream?</DialogTitle>
        <DialogDescription>
          This deletes <strong class="font-mono-data text-[#09090B]">{{ deleteTarget }}</strong> from the gateway store and bumps the catalog epoch across replicas.
        </DialogDescription>
        <div class="flex justify-end gap-2 pt-2">
          <DialogClose>Cancel</DialogClose>
          <Button data-variant="danger" @click="confirmDelete">
            Delete Upstream
          </Button>
        </div>
      </DialogContent>
    </Dialog>
  </div>
</template>
