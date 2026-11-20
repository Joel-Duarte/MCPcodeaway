<script setup lang="ts">
interface UserConnectionItem {
  upstream_name: string;
  auth_type: "user_oauth" | "user_api_key";
  status: "connected" | "expired" | "not_connected";
  expires_at: number | null;
  updated_at: string | null;
  connect_url: string;
}

const route = useRoute();
const { apiFetch } = useAdminApi();

const loading = ref(true);
const errorMsg = ref("");
const statusMsg = ref("");
const connections = ref<UserConnectionItem[]>([]);
const activePatUpstream = ref<string | null>(null);
const patInput = ref("");
const savingUpstream = ref<string | null>(null);

function extractError(err: unknown): string {
  const e = err as { data?: { detail?: string }; message?: string };
  return e?.data?.detail || e?.message || "Request failed";
}

async function loadConnections() {
  loading.value = true;
  errorMsg.value = "";
  try {
    const res = await apiFetch<{ connections: UserConnectionItem[] }>("/me/connections");
    connections.value = res.connections || [];
  } catch (err) {
    errorMsg.value = extractError(err);
  } finally {
    loading.value = false;
  }
}

function openPatForm(upstreamName: string) {
  activePatUpstream.value = activePatUpstream.value === upstreamName ? null : upstreamName;
  patInput.value = "";
}

async function savePat(upstreamName: string) {
  const cleaned = patInput.value.trim();
  if (!cleaned) return;
  savingUpstream.value = upstreamName;
  errorMsg.value = "";
  statusMsg.value = "";
  try {
    const ticket =
      typeof route.query.ticket === "string" && route.query.upstream === upstreamName
        ? route.query.ticket
        : undefined;
    await apiFetch(`/me/connections/${encodeURIComponent(upstreamName)}/secret`, {
      method: "POST",
      body: { api_key: cleaned, ticket },
    });
    patInput.value = "";
    activePatUpstream.value = null;
    statusMsg.value = `Saved encrypted credential for '${upstreamName}'.`;
    await loadConnections();
  } catch (err) {
    errorMsg.value = extractError(err);
  } finally {
    savingUpstream.value = null;
  }
}

async function disconnect(upstreamName: string) {
  savingUpstream.value = upstreamName;
  errorMsg.value = "";
  statusMsg.value = "";
  try {
    await apiFetch(`/me/connections/${encodeURIComponent(upstreamName)}`, {
      method: "DELETE",
    });
    statusMsg.value = `Disconnected '${upstreamName}'.`;
    await loadConnections();
  } catch (err) {
    errorMsg.value = extractError(err);
  } finally {
    savingUpstream.value = null;
  }
}

function statusClasses(status: UserConnectionItem["status"]): string {
  if (status === "connected") return "bg-[#ECFDF5] text-[#059669] border-[#A7F3D0]";
  if (status === "expired") return "bg-[#FFFBEB] text-[#D97706] border-[#FDE68A]";
  return "bg-[#F4F4F5] text-[#52525B] border-[#E4E4E7]";
}

onMounted(async () => {
  await loadConnections();
  if (typeof route.query.upstream === "string") {
    const match = connections.value.find((c) => c.upstream_name === route.query.upstream);
    if (match?.auth_type === "user_api_key") {
      activePatUpstream.value = match.upstream_name;
    }
  }
});
</script>

<template>
  <div class="space-y-6">
    <div class="flex flex-wrap items-center justify-between gap-3">
      <div>
        <h1 class="text-[1.25rem] font-semibold tracking-[-0.015em] text-[#09090B]">
          My Connections
        </h1>
        <p class="mt-1 text-sm text-[#52525B]">
          Link personal OAuth accounts or API keys for non-Entra upstreams. Secrets are encrypted at rest and never exposed to agents.
        </p>
      </div>
      <Button variant="outline" :disabled="loading" @click="loadConnections">
        Refresh
      </Button>
    </div>

    <div
      v-if="errorMsg"
      role="alert"
      class="rounded-lg border border-[#FECACA] bg-[#FEF2F2] px-4 py-3 text-sm text-[#DC2626]"
    >
      {{ errorMsg }}
    </div>
    <div
      v-if="statusMsg"
      class="rounded-lg border border-[#99F6E4] bg-[#F0FDFA] px-4 py-3 text-sm text-[#0F766E]"
    >
      {{ statusMsg }}
    </div>

    <section class="rounded-lg border border-[#E4E4E7] bg-white">
      <div v-if="loading" class="space-y-3 p-5">
        <div class="h-10 animate-pulse rounded bg-[#F4F4F5]" />
        <div class="h-10 animate-pulse rounded bg-[#F4F4F5]" />
      </div>

      <div v-else-if="connections.length === 0" class="p-8 text-center">
        <p class="text-sm font-medium text-[#09090B]">
          No per-user upstreams require linking
        </p>
        <p class="mt-1 text-xs text-[#71717A]">
          Upstreams configured with user_oauth or user_api_key that match your role will appear here.
        </p>
      </div>

      <div v-else class="overflow-x-auto">
        <table class="w-full border-collapse text-left text-[0.8125rem]">
          <thead>
            <tr class="border-b border-[#E4E4E7] bg-[#F4F4F5] text-xs font-medium text-[#52525B]">
              <th class="px-4 py-2.5 whitespace-nowrap">Upstream</th>
              <th class="px-4 py-2.5 whitespace-nowrap">Auth Type</th>
              <th class="px-4 py-2.5 whitespace-nowrap">Status</th>
              <th class="px-4 py-2.5 whitespace-nowrap">Updated</th>
              <th class="px-4 py-2.5 text-right whitespace-nowrap">Actions</th>
            </tr>
          </thead>
          <tbody class="divide-y divide-[#E4E4E7]">
            <template v-for="conn in connections" :key="conn.upstream_name">
              <tr class="hover:bg-[#FAFAFA]">
                <td class="px-4 py-3 font-medium text-[#09090B] whitespace-nowrap">
                  {{ conn.upstream_name }}
                </td>
                <td class="px-4 py-3 font-mono-data text-xs text-[#52525B] whitespace-nowrap">
                  {{ conn.auth_type }}
                </td>
                <td class="px-4 py-3 whitespace-nowrap">
                  <span
                    class="inline-flex items-center rounded border px-2 py-0.5 text-xs font-medium"
                    :class="statusClasses(conn.status)"
                  >
                    {{ conn.status }}
                  </span>
                </td>
                <td class="px-4 py-3 font-mono-data text-xs tabular-nums text-[#71717A] whitespace-nowrap">
                  {{ conn.updated_at ? conn.updated_at.slice(0, 19).replace("T", " ") : "—" }}
                </td>
                <td class="px-4 py-3 text-right whitespace-nowrap">
                  <div class="inline-flex items-center gap-2">
                    <a
                      v-if="conn.auth_type === 'user_oauth'"
                      :href="conn.connect_url"
                      target="_blank"
                      rel="noopener noreferrer"
                      class="inline-flex min-h-[36px] items-center rounded-md bg-[#0F766E] px-3 py-1.5 text-xs font-medium text-white hover:bg-[#115E59] focus-visible:outline-2 focus-visible:outline-[#0F766E]"
                    >
                      {{ conn.status === "connected" ? "Reconnect OAuth" : "Connect OAuth" }}
                    </a>
                    <Button
                      v-else
                      size="sm"
                      @click="openPatForm(conn.upstream_name)"
                    >
                      {{ conn.status === "connected" ? "Rotate Key" : "Enter Key" }}
                    </Button>
                    <Button
                      v-if="conn.status !== 'not_connected'"
                      variant="outline"
                      size="sm"
                      :disabled="savingUpstream === conn.upstream_name"
                      @click="disconnect(conn.upstream_name)"
                    >
                      Disconnect
                    </Button>
                  </div>
                </td>
              </tr>
              <tr v-if="activePatUpstream === conn.upstream_name" class="bg-[#FAFAFA]">
                <td colspan="5" class="px-4 py-3">
                  <form class="flex flex-wrap items-end gap-3" @submit.prevent="savePat(conn.upstream_name)">
                    <div class="min-w-[240px] flex-1">
                      <label
                        :for="`pat-${conn.upstream_name}`"
                        class="block text-xs font-medium text-[#52525B]"
                      >
                        Personal API Key / PAT for {{ conn.upstream_name }}
                      </label>
                      <Input
                        :id="`pat-${conn.upstream_name}`"
                        v-model="patInput"
                        type="password"
                        autocomplete="off"
                        placeholder="Paste personal token (write-only, encrypted at rest)"
                        class="mt-1 w-full font-mono-data text-xs"
                      />
                    </div>
                    <Button
                      type="submit"
                      :disabled="!patInput.trim() || savingUpstream === conn.upstream_name"
                    >
                      {{ savingUpstream === conn.upstream_name ? "Saving..." : "Save Encrypted Key" }}
                    </Button>
                    <Button type="button" variant="outline" @click="activePatUpstream = null">
                      Cancel
                    </Button>
                  </form>
                </td>
              </tr>
            </template>
          </tbody>
        </table>
      </div>
    </section>
  </div>
</template>
