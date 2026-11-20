<script setup lang="ts">
import { onMounted, ref } from "vue";

interface DemoModelProfile {
  model: string;
  codemode_success_rate: number;
  traditional_success_rate: number;
  traditional_turns: number;
}

const { apiFetch } = useAdminApi();
const { status: demoStatus, busy: demoBusy, runBurst, refreshStatus } = useDemoTraffic();

const models = ref<DemoModelProfile[]>([]);
const saving = ref(false);
const notice = ref("");

async function loadModels() {
  try {
    const res = await apiFetch<{ models: DemoModelProfile[] }>("/admin/demo/models");
    if (res?.models) models.value = res.models;
  } catch {
    // ignore in non-demo profiles
  }
}

async function saveModels() {
  saving.value = true;
  notice.value = "";
  try {
    await apiFetch("/admin/demo/models", {
      method: "POST",
      body: JSON.stringify({ models: models.value }),
    });
    notice.value = "Saved demo model profiles.";
  } finally {
    saving.value = false;
  }
}

async function runCalibrationBurst() {
  notice.value = "";
  await runBurst(100);
  await refreshStatus();
  notice.value = "Dispatched 100-request A/B calibration burst.";
}

onMounted(() => {
  loadModels();
  refreshStatus();
});
</script>

<template>
  <div class="space-y-6">
    <div class="flex flex-wrap items-center justify-between gap-3">
      <div>
        <h1 class="text-xl font-semibold tracking-tight text-[#09090B]">Demo Control</h1>
        <p class="text-sm text-[#71717A]">
          Configure simulated model performance on Code Mode vs. Classic MCP and trigger 100-request A/B calibration bursts.
        </p>
      </div>
      <div class="flex items-center gap-2">
        <Button :disabled="saving" @click="saveModels">Save Model Profiles</Button>
        <Button :disabled="demoBusy" @click="runCalibrationBurst">
          Run 100-Req Calibration Burst
        </Button>
      </div>
    </div>

    <div v-if="notice" class="rounded border border-[#99F6E4] bg-[#F0FDFA] px-3 py-2 text-xs text-[#0F766E]">
      {{ notice }}
    </div>

    <div class="rounded-lg border border-[#E4E4E7] bg-white p-4">
      <h2 class="text-sm font-semibold text-[#09090B]">Simulated Model Performance Profiles</h2>
      <p class="mb-4 text-xs text-[#71717A]">
        Adjust success rates and average Classic MCP turns per model to observe automatic mode switching.
      </p>
      <div class="overflow-x-auto">
        <table class="w-full text-left text-xs tabular-nums">
          <thead class="border-b border-[#E4E4E7] text-[#71717A]">
            <tr>
              <th class="py-2 pr-3 font-medium">Model</th>
              <th class="py-2 pr-3 font-medium">Code Mode Success (0-1)</th>
              <th class="py-2 pr-3 font-medium">Classic Success (0-1)</th>
              <th class="py-2 font-medium">Classic Avg Turns</th>
            </tr>
          </thead>
          <tbody class="divide-y divide-[#E4E4E7]">
            <tr v-for="m in models" :key="m.model">
              <td class="py-2 pr-3 font-mono-data font-medium text-[#09090B]">{{ m.model }}</td>
              <td class="py-2 pr-3">
                <Input
                  v-model.number="m.codemode_success_rate"
                  type="number"
                  step="0.05"
                  min="0"
                  max="1"
                  class="w-28 font-mono-data text-xs"
                />
              </td>
              <td class="py-2 pr-3">
                <Input
                  v-model.number="m.traditional_success_rate"
                  type="number"
                  step="0.05"
                  min="0"
                  max="1"
                  class="w-28 font-mono-data text-xs"
                />
              </td>
              <td class="py-2">
                <Input
                  v-model.number="m.traditional_turns"
                  type="number"
                  min="1"
                  max="12"
                  class="w-24 font-mono-data text-xs"
                />
              </td>
            </tr>
          </tbody>
        </table>
      </div>
    </div>

    <div v-if="demoStatus" class="rounded-lg border border-[#E4E4E7] bg-white p-4">
      <h2 class="mb-3 text-sm font-semibold text-[#09090B]">Live Demo Traffic Status</h2>
      <div class="grid grid-cols-2 gap-4 text-xs tabular-nums sm:grid-cols-4">
        <div>
          <div class="text-[#71717A]">Requests Sent</div>
          <div class="font-mono-data text-base font-semibold text-[#09090B]">{{ demoStatus.requests_sent }}</div>
        </div>
        <div>
          <div class="text-[#71717A]">Errors</div>
          <div class="font-mono-data text-base font-semibold text-[#09090B]">{{ demoStatus.errors_count }}</div>
        </div>
        <div>
          <div class="text-[#71717A]">Tier</div>
          <div class="font-mono-data text-base font-semibold text-[#09090B]">{{ demoStatus.tier }}</div>
        </div>
        <div>
          <div class="text-[#71717A]">Last Target</div>
          <div class="truncate font-mono-data text-xs text-[#09090B]">{{ demoStatus.last_target || "—" }}</div>
        </div>
      </div>
    </div>
  </div>
</template>
