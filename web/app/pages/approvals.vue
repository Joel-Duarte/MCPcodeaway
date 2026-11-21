<script setup lang="ts">
import { onMounted, ref } from "vue";
import { extractErrorMessage } from "~/composables/useAdminApi";

interface ApprovalRecord {
  approval_id: string;
  upstream_name: string;
  tool_name: string;
  action_summary: string;
  requester_sub: string;
  approver_groups: string[];
  status: "pending" | "approved" | "denied";
  created_at: string;
  expires_at: string;
  decided_by?: string | null;
  decided_at?: string | null;
}

const { apiFetch, session } = useAdminApi();
const approvals = ref<ApprovalRecord[]>([]);
const statusFilter = ref("pending");
const loading = ref(true);
const errorMessage = ref("");
const statusMessage = ref("");
const decidingId = ref("");

const statusOptions = [
  { value: "pending", label: "Pending" },
  { value: "approved", label: "Approved" },
  { value: "denied", label: "Denied" },
  { value: "all", label: "All decisions" },
];

function formatTime(value: string): string {
  const date = new Date(value);
  return Number.isNaN(date.getTime()) ? value : date.toLocaleString();
}

async function loadApprovals() {
  loading.value = true;
  errorMessage.value = "";
  try {
    const response = await apiFetch<{ approvals: ApprovalRecord[] }>(
      `/admin/approvals?status=${encodeURIComponent(statusFilter.value)}`,
    );
    approvals.value = response.approvals;
  } catch (error) {
    errorMessage.value = extractErrorMessage(error);
  } finally {
    loading.value = false;
  }
}

async function decide(record: ApprovalRecord, decision: "approve" | "deny") {
  decidingId.value = record.approval_id;
  errorMessage.value = "";
  statusMessage.value = "";
  try {
    await apiFetch(`/admin/approvals/${encodeURIComponent(record.approval_id)}/decision`, {
      method: "POST",
      body: { decision },
    });
    statusMessage.value = decision === "approve"
      ? `Approved ${record.upstream_name} / ${record.tool_name}. The requester must retry the same call.`
      : `Denied ${record.upstream_name} / ${record.tool_name}.`;
    await loadApprovals();
  } catch (error) {
    errorMessage.value = extractErrorMessage(error);
    await loadApprovals();
  } finally {
    decidingId.value = "";
  }
}

onMounted(() => {
  void loadApprovals();
});
</script>

<template>
  <div class="space-y-6">
    <header class="flex flex-col gap-3 sm:flex-row sm:items-end sm:justify-between">
      <div>
        <h1 class="text-[20px] font-semibold tracking-[-0.015em] text-[#09090B]">Tool approvals</h1>
        <p class="mt-1 max-w-3xl text-[13px] leading-5 text-[#52525B]">
          Review exact tool actions requested by your eligible groups. An approval records permission;
          the requester must retry the same call before the gateway dispatches it.
        </p>
      </div>
      <div class="flex items-center gap-2">
        <label for="approval-status" class="text-xs font-medium text-[#52525B]">Show</label>
        <Select
          :items="statusOptions"
          :value="statusFilter"
          @value-change="(value) => { if (value) { statusFilter = String(value); void loadApprovals(); } }"
        >
          <SelectTrigger id="approval-status" class="min-h-11 min-w-40 rounded-md border border-[#D4D4D8] bg-white px-3 text-sm text-[#09090B] focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-[#0F766E]">
            <SelectValue />
            <AppIcon name="chevron-down" :size="14" />
          </SelectTrigger>
          <SelectContent>
            <SelectItem v-for="option in statusOptions" :key="option.value" :value="option.value">
              {{ option.label }}
              <SelectItemIndicator><AppIcon name="check" :size="14" /></SelectItemIndicator>
            </SelectItem>
          </SelectContent>
        </Select>
      </div>
    </header>

    <div v-if="statusMessage" role="status" class="rounded-lg border border-[#99F6E4] bg-[#F0FDFA] px-4 py-3 text-[13px] text-[#0F766E]">
      {{ statusMessage }}
    </div>
    <div v-if="errorMessage" role="alert" class="flex flex-col gap-2 rounded-lg border border-[#FECACA] bg-[#FEF2F2] px-4 py-3 text-[13px] text-[#991B1B] sm:flex-row sm:items-center sm:justify-between">
      <span>{{ errorMessage }}</span>
      <Button class="min-h-11 shrink-0" @click="loadApprovals">Retry loading approvals</Button>
    </div>

    <div v-if="loading" aria-label="Loading approvals" class="space-y-2" aria-busy="true">
      <div v-for="row in 3" :key="row" class="h-20 animate-pulse rounded border border-[#E4E4E7] bg-white" />
    </div>

    <section v-else class="overflow-hidden rounded-lg border border-[#E4E4E7] bg-white">
      <div v-if="approvals.length === 0" class="px-5 py-12 text-center">
        <h2 class="text-[15px] font-semibold text-[#09090B]">No {{ statusFilter === "all" ? "approval requests" : `${statusFilter} approvals` }}</h2>
        <p class="mx-auto mt-2 max-w-md text-[13px] leading-5 text-[#52525B]">
          Requests appear here only when your verified identity belongs to an eligible approver group.
        </p>
      </div>
      <div v-else class="overflow-x-auto">
        <table class="w-full min-w-[760px] border-collapse text-left text-[13px]">
          <caption class="sr-only">Tool approval requests visible to {{ session?.user?.sub || "the signed-in user" }}</caption>
          <thead class="bg-[#F4F4F5] text-xs font-medium text-[#52525B]">
            <tr>
              <th scope="col" class="px-4 py-3">Requested action</th>
              <th scope="col" class="px-4 py-3">Requester</th>
              <th scope="col" class="px-4 py-3">Eligible group</th>
              <th scope="col" class="px-4 py-3">Expires</th>
              <th scope="col" class="px-4 py-3">Decision</th>
            </tr>
          </thead>
          <tbody class="divide-y divide-[#E4E4E7]">
            <tr v-for="approval in approvals" :key="approval.approval_id" class="align-top">
              <td class="max-w-[26rem] px-4 py-4">
                <div class="font-medium text-[#09090B]">{{ approval.upstream_name }} / {{ approval.tool_name }}</div>
                <p class="mt-1 leading-5 text-[#52525B]">{{ approval.action_summary }}</p>
                <div class="mt-1 font-mono-data text-[11px] text-[#71717A]">{{ approval.approval_id }}</div>
              </td>
              <td class="px-4 py-4 font-mono-data text-xs text-[#52525B]">{{ approval.requester_sub }}</td>
              <td class="px-4 py-4 text-[#52525B]">{{ approval.approver_groups.join(", ") }}</td>
              <td class="px-4 py-4 tabular-nums text-[#52525B]">{{ formatTime(approval.expires_at) }}</td>
              <td class="px-4 py-4">
                <div v-if="approval.status === 'pending'" class="flex flex-col gap-2 sm:flex-row">
                  <Button
                    data-variant="primary"
                    class="min-h-11"
                    :disabled="Boolean(decidingId)"
                    :data-agent-confirm="`Approves ${approval.upstream_name} ${approval.tool_name}: ${approval.action_summary}`"
                    :pending-announcement="`Approving ${approval.tool_name}`"
                    @click="decide(approval, 'approve')"
                  >Approve</Button>
                  <Button
                    class="min-h-11"
                    :disabled="Boolean(decidingId)"
                    :pending-announcement="`Denying ${approval.tool_name}`"
                    @click="decide(approval, 'deny')"
                  >Deny</Button>
                </div>
                <div v-else>
                  <span class="font-medium capitalize text-[#09090B]">{{ approval.status }}</span>
                  <div v-if="approval.decided_by" class="mt-1 text-xs text-[#71717A]">by {{ approval.decided_by }}</div>
                </div>
              </td>
            </tr>
          </tbody>
        </table>
      </div>
    </section>
  </div>
</template>
