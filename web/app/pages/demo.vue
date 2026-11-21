<script setup lang="ts">
import { onMounted, ref } from "vue";

interface DemoModelProfile {
  model: string;
  codemode_success_rate: number;
  traditional_success_rate: number;
  traditional_turns: number;
}

const { apiFetch, extractErrorMessage: _unused } = { apiFetch: useAdminApi().apiFetch, extractErrorMessage };
const {
  status: demoStatus,
  busy: demoBusy,
  statusError: demoStatusError,
  startTraffic,
  stopTraffic,
  setTier,
  runBurst,
  refreshStatus,
} = useDemoTraffic();

const models = ref<DemoModelProfile[]>([]);
const saving = ref(false);
const notice = ref("");
const errorMsg = ref("");

const newModelName = ref("");
const newCodeSuccessRate = ref(0.95);
const newTradSuccessRate = ref(0.90);
const newTradTurns = ref(3);

function readNumber(value: string, previous: number): number {
  return value === "" ? previous : Number(value);
}

function addModelProfile() {
  const name = newModelName.value.trim();
  if (!name) return;
  if (models.value.some((m) => m.model === name)) {
    errorMsg.value = `Model '${name}' already exists.`;
    return;
  }
  models.value.push({
    model: name,
    codemode_success_rate: Math.min(1, Math.max(0, Number(newCodeSuccessRate.value) || 0.95)),
    traditional_success_rate: Math.min(1, Math.max(0, Number(newTradSuccessRate.value) || 0.90)),
    traditional_turns: Math.max(1, Math.min(12, Math.round(Number(newTradTurns.value) || 3))),
  });
  newModelName.value = "";
  notice.value = `Added '${name}'. Click 'Save Model Profiles' to persist.`;
}

function removeModelProfile(modelName: string) {
  models.value = models.value.filter((m) => m.model !== modelName);
  notice.value = `Removed '${modelName}'. Click 'Save Model Profiles' to persist.`;
}

async function loadModels() {
  try {
    const res = await apiFetch<{ models: DemoModelProfile[] }>("/admin/demo/models");
    if (res?.models) models.value = res.models;
  } catch (err) {
    errorMsg.value = extractErrorMessage(err);
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
  } catch (err) {
    errorMsg.value = extractErrorMessage(err);
  } finally {
    saving.value = false;
  }
}

async function runCalibrationBurst(count = 100) {
  notice.value = "";
  errorMsg.value = "";
  try {
    await runBurst(count);
    await refreshStatus();
    notice.value = `Dispatched ${count}-request A/B demo burst.`;
  } catch (err) {
    errorMsg.value = extractErrorMessage(err);
  }
}

async function handleStart() {
  notice.value = "";
  errorMsg.value = "";
  try {
    await startTraffic(demoStatus.value.tier);
    notice.value = `Demo traffic started (${demoStatus.value.tier} tier).`;
  } catch (err) {
    errorMsg.value = extractErrorMessage(err);
  }
}

async function handleStop() {
  notice.value = "";
  errorMsg.value = "";
  try {
    await stopTraffic();
    notice.value = "Demo traffic stopped.";
  } catch (err) {
    errorMsg.value = extractErrorMessage(err);
  }
}

async function handleTierChange(tier: "light" | "enterprise" | "stress") {
  notice.value = "";
  errorMsg.value = "";
  try {
    await setTier(tier);
  } catch (err) {
    errorMsg.value = extractErrorMessage(err);
  }
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
      <div class="flex flex-wrap items-center gap-2">
        <Button :disabled="saving" @click="saveModels">Save Model Profiles</Button>
        <Button :disabled="demoBusy || !demoStatus.enabled" @click="runCalibrationBurst(8)">
          Burst +8
        </Button>
        <Button :disabled="demoBusy || !demoStatus.enabled" @click="runCalibrationBurst(100)">
          Run 100-Req Calibration Burst
        </Button>
      </div>
    </div>

    <div v-if="notice" class="rounded border border-[#99F6E4] bg-[#F0FDFA] px-3 py-2 text-xs text-[#0F766E]">
      {{ notice }}
    </div>
    <div v-if="errorMsg" role="alert" class="rounded border border-[#FECACA] bg-[#FEF2F2] px-3 py-2 text-xs text-[#DC2626]">
      {{ errorMsg }}
    </div>
    <div v-if="demoStatusError" role="alert" class="flex flex-col gap-2 rounded border border-[#FECACA] bg-[#FEF2F2] px-3 py-2 text-xs text-[#991B1B] sm:flex-row sm:items-center sm:justify-between">
      <span>Demo traffic status unavailable: {{ demoStatusError }}</span>
      <Button class="min-h-11" @click="refreshStatus">Retry status</Button>
    </div>

    <div class="rounded-lg border border-[#E4E4E7] bg-white p-4 space-y-4">
      <div class="flex flex-wrap items-center justify-between gap-3">
        <div>
          <h2 class="text-sm font-semibold text-[#09090B]">Continuous Demo Traffic Runner</h2>
          <p class="text-xs text-[#71717A]">
            Stream realistic multi-persona Code Mode vs. Classic MCP requests through the gateway.
          </p>
        </div>
        <span
          class="rounded border px-2.5 py-1 text-xs font-medium"
          :class="demoStatus.running
            ? 'border-[#A7F3D0] bg-[#ECFDF5] text-[#059669]'
            : demoStatus.enabled
              ? 'border-[#E4E4E7] bg-[#F4F4F5] text-[#52525B]'
              : 'border-[#FDE68A] bg-[#FFFBEB] text-[#92400E]'"
        >
          {{ demoStatus.running ? "Traffic Running" : demoStatus.enabled ? "Traffic Stopped" : "Disabled in Production" }}
        </span>
      </div>

      <div class="flex flex-wrap items-center gap-3">
        <div class="flex items-center gap-2">
          <span class="text-xs font-medium text-[#52525B]">Tier</span>
          <Select
            :items="[
              { value: 'light', label: 'Light' },
              { value: 'enterprise', label: 'Enterprise' },
              { value: 'stress', label: 'Stress' },
            ]"
            :value="demoStatus.tier"
            @value-change="(v) => { if (v) void handleTierChange(String(v) as 'light' | 'enterprise' | 'stress'); }"
          >
            <SelectTrigger class="w-36">
              <SelectValue />
              <AppIcon name="chevron-down" :size="14" />
            </SelectTrigger>
            <SelectContent>
              <SelectItem value="light">
                Light
                <SelectItemIndicator><AppIcon name="check" :size="14" /></SelectItemIndicator>
              </SelectItem>
              <SelectItem value="enterprise">
                Enterprise
                <SelectItemIndicator><AppIcon name="check" :size="14" /></SelectItemIndicator>
              </SelectItem>
              <SelectItem value="stress">
                Stress
                <SelectItemIndicator><AppIcon name="check" :size="14" /></SelectItemIndicator>
              </SelectItem>
            </SelectContent>
          </Select>
        </div>

        <Button
          data-variant="primary"
          :disabled="demoBusy || !demoStatus.enabled || demoStatus.running"
          @click="handleStart"
        >
          Start Demo Traffic
        </Button>
        <Button
          data-variant="danger"
          :disabled="demoBusy || !demoStatus.enabled || !demoStatus.running"
          @click="handleStop"
        >
          Stop
        </Button>
      </div>
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
              <th class="py-2 pr-3 font-medium">Classic Avg Turns</th>
              <th class="py-2 text-right font-medium">Actions</th>
            </tr>
          </thead>
          <tbody class="divide-y divide-[#E4E4E7]">
            <tr v-for="m in models" :key="m.model">
              <td class="py-2 pr-3 font-mono-data font-medium text-[#09090B]">{{ m.model }}</td>
              <td class="py-2 pr-3">
                <Input :model-value="String(m.codemode_success_rate)" @update:model-value="m.codemode_success_rate = readNumber($event, m.codemode_success_rate)" type="number" step="0.05" min="0" max="1" class="w-28 font-mono-data text-xs" />
              </td>
              <td class="py-2 pr-3">
                <Input :model-value="String(m.traditional_success_rate)" @update:model-value="m.traditional_success_rate = readNumber($event, m.traditional_success_rate)" type="number" step="0.05" min="0" max="1" class="w-28 font-mono-data text-xs" />
              </td>
              <td class="py-2 pr-3">
                <Input :model-value="String(m.traditional_turns)" @update:model-value="m.traditional_turns = readNumber($event, m.traditional_turns)" type="number" min="1" max="12" class="w-24 font-mono-data text-xs" />
              </td>
              <td class="py-2 text-right">
                <button type="button" class="text-xs font-medium text-[#DC2626] hover:underline" @click="removeModelProfile(m.model)">Delete</button>
              </td>
            </tr>
          </tbody>
        </table>
      </div>
      <form class="mt-4 flex flex-wrap items-end gap-3 border-t border-[#E4E4E7] pt-4" @submit.prevent="addModelProfile">
        <Field><FieldLabel>New model</FieldLabel><Input v-model="newModelName" placeholder="e.g. qwen3.5-9b" class="w-48 font-mono-data text-xs" /></Field>
        <Field><FieldLabel>Code success</FieldLabel><Input :model-value="String(newCodeSuccessRate)" @update:model-value="newCodeSuccessRate = readNumber($event, newCodeSuccessRate)" type="number" step="0.05" min="0" max="1" class="w-24 text-xs" /></Field>
        <Field><FieldLabel>Classic success</FieldLabel><Input :model-value="String(newTradSuccessRate)" @update:model-value="newTradSuccessRate = readNumber($event, newTradSuccessRate)" type="number" step="0.05" min="0" max="1" class="w-24 text-xs" /></Field>
        <Field><FieldLabel>Classic turns</FieldLabel><Input :model-value="String(newTradTurns)" @update:model-value="newTradTurns = readNumber($event, newTradTurns)" type="number" min="1" max="12" class="w-20 text-xs" /></Field>
        <Button type="submit" data-variant="primary">Add model</Button>
      </form>
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
