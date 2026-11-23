<script setup lang="ts">
import { Button } from "@ui-sdk/vue/button";
import { Switch, SwitchThumb } from "@ui-sdk/vue/switch";
import { computed, ref, watch } from "vue";
import PromptBundleDrawer from "~/components/PromptBundleDrawer.vue";
import WorkflowEditorDrawer from "~/components/WorkflowEditorDrawer.vue";
import { extractErrorMessage } from "~/composables/useAdminApi";

type Workflow = Record<string, any>;
type Definition = Record<string, any>;

const props = withDefaults(
  defineProps<{
    upstream: string;
    tools: string[];
    canEdit: boolean;
    refreshVersion: number;
    autoDiscoveryEnabled?: boolean;
  }>(),
  { autoDiscoveryEnabled: true },
);
defineEmits<{ (event: "refresh-catalog"): void }>();

const { apiFetch } = useAdminApi();
const localAutoDiscovery = ref(props.autoDiscoveryEnabled);
watch(
  () => props.autoDiscoveryEnabled,
  (val) => {
    localAutoDiscovery.value = val;
  },
);
const togglingAutoDiscovery = ref(false);
async function toggleAutoDiscovery(enabled: boolean) {
  togglingAutoDiscovery.value = true;
  discoverError.value = "";
  try {
    await apiFetch(
      `/admin/upstreams/${encodeURIComponent(props.upstream)}/auto-discovery`,
      { method: "PATCH", body: { enabled } },
    );
    localAutoDiscovery.value = enabled;
  } catch (err) {
    discoverError.value = extractErrorMessage(err);
  } finally {
    togglingAutoDiscovery.value = false;
  }
}

const definition = ref<Definition | null>(null);
const loading = ref(true);
const loadError = ref("");
const editorOpen = ref(false);
const promptDrawerOpen = ref(false);
const editingWorkflow = ref<Workflow | null>(null);
const promotingCandidate = ref<Record<string, any> | null>(null);
const statusMessage = ref("");
const opener = ref<HTMLElement | null>(null);
const discoveredCandidates = ref<Array<Record<string, any>>>([]);
const discovering = ref(false);
const discoverError = ref("");

const dismissCandidate = async (candidate: Record<string, any>) => {
  const candidateId = String(candidate.candidate_id || "");
  if (!candidateId) return;
  discoverError.value = "";
  try {
    await apiFetch(`/admin/workflows/discovered/${encodeURIComponent(candidateId)}/dismiss`, {
      method: "POST",
    });
    discoveredCandidates.value = discoveredCandidates.value.filter(
      (item) => String(item.candidate_id || "") !== candidateId,
    );
  } catch (err) {
    discoverError.value = extractErrorMessage(err);
  }
};

const workflows = computed<Workflow[]>(() => definition.value?.workflows ?? []);

const sourceLabels: Record<string, string> = {
  administrator_estimate: "Administrator estimate",
  sampled_manual_measurement: "Sampled manual measurement",
  validated_external_measurement: "Validated external measurement",
};
const sourceLabel = (source: string) => sourceLabels[source] ?? source.replaceAll("_", " ");

const confidenceLabels: Record<string, string> = {
  unvalidated: "not validated",
  sampled: "sampled",
  validated: "validated",
};
const confidenceLabel = (confidence: string) =>
  confidenceLabels[confidence] ?? confidence.replaceAll("_", " ");

async function load() {
  loading.value = true;
  loadError.value = "";
  try {
    const response = await apiFetch<{ current?: Definition | { definition?: Definition } | null }>(
      `/admin/integration-definitions/${encodeURIComponent(props.upstream)}`,
    );
    const current = response.current;
    definition.value =
      current && "definition" in current ? current.definition ?? null : current ?? null;
  } catch (err) {
    loadError.value = extractErrorMessage(err);
  } finally {
    loading.value = false;
  }
}

watch(
  () => [props.upstream, props.refreshVersion] as const,
  () => {
    void load();
  },
  { immediate: true },
);

watch(editorOpen, (value, previous) => {
  if (previous && !value) requestAnimationFrame(() => opener.value?.focus());
});

function beginCreate(event: MouseEvent) {
  opener.value = event.currentTarget instanceof HTMLElement ? event.currentTarget : null;
  editingWorkflow.value = null;
  promotingCandidate.value = null;
  statusMessage.value = "";
  editorOpen.value = true;
}

function beginEdit(workflow: Workflow, event: MouseEvent) {
  opener.value = event.currentTarget instanceof HTMLElement ? event.currentTarget : null;
  editingWorkflow.value = workflow;
  promotingCandidate.value = null;
  statusMessage.value = "";
  editorOpen.value = true;
}

async function discoverWorkflows() {
  discovering.value = true;
  discoverError.value = "";
  try {
    const res = await apiFetch<{ candidates?: Array<Record<string, any>> }>(
      `/admin/integration-definitions/${encodeURIComponent(props.upstream)}/discover`,
      { method: "POST", body: {} },
    );
    discoveredCandidates.value = res.candidates ?? [];
  } catch (err) {
    discoverError.value = extractErrorMessage(err);
  } finally {
    discovering.value = false;
  }
}

function useDiscoveredCandidate(candidate: Record<string, any>, event: MouseEvent) {
  opener.value = event.currentTarget instanceof HTMLElement ? event.currentTarget : null;
  editingWorkflow.value = null;
  promotingCandidate.value = candidate;
  statusMessage.value = "";
  editorOpen.value = true;
}

async function handleWorkflowSaved(payload: {
  statusMessage: string;
  upstream: string;
  promotedCandidateId?: string | null;
}) {
  statusMessage.value = payload.statusMessage;
  if (payload.promotedCandidateId) {
    discoveredCandidates.value = discoveredCandidates.value.filter(
      (cand) => String(cand.candidate_id || "") !== payload.promotedCandidateId,
    );
  }
  await load();
}

async function handleBundleApplied(upstreamName: string) {
  statusMessage.value = `Applied and published workflow bundle for “${upstreamName || props.upstream}”.`;
  await load();
}
</script>

<template>
  <section
    class="mb-3 min-w-0 max-w-[calc(100vw-4rem)] border-b border-zinc-200 pb-3 lg:max-w-none"
    :aria-label="`Workflow catalog for ${upstream}`"
  >
    <div class="mb-2 flex flex-wrap items-start justify-between gap-2">
      <div class="min-w-0">
        <h3 class="text-[0.9375rem] font-semibold text-zinc-950">Workflow Catalog</h3>
        <p class="mt-0.5 max-w-3xl text-xs leading-5 text-zinc-600">
          Business processes measured for this upstream. Listed tool order is descriptive and is not
          enforced.
        </p>
      </div>
      <div class="flex flex-wrap items-center gap-2">
        <Button v-if="canEdit" class="min-h-9" @click="beginCreate">Create workflow</Button>
        <Button v-if="canEdit" class="min-h-9" @click="promptDrawerOpen = true">
          Generate Workflows via Prompt
        </Button>
      </div>
    </div>

    <p v-if="!canEdit" class="mb-2 text-xs text-[#52525B]">
      Read-only: workflow changes are disabled because dynamic upstream writes are not permitted.
    </p>
    <p v-if="statusMessage" role="status" class="mb-2 text-xs leading-5 text-[#0F766E]">
      {{ statusMessage }}
    </p>
    <div v-if="loading" class="py-3 text-sm text-[#52525B]" role="status">Loading workflows…</div>
    <div v-else-if="loadError" class="py-2 text-sm text-[#991B1B]" role="alert">
      <p>{{ loadError }}</p>
      <Button class="mt-2 min-h-11" @click="load">Retry</Button>
    </div>
    <p v-else-if="!definition" class="py-2 text-sm text-[#52525B]">
      No integration definition or workflows exist for this upstream yet. Create a workflow to
      establish its first definition.
    </p>
    <p v-else-if="!workflows.length" class="py-2 text-sm text-[#52525B]">
      No workflows are defined for this upstream.
    </p>
    <ul v-else class="grid grid-cols-1 gap-2 xl:grid-cols-2">
      <li
        v-for="workflow in workflows"
        :key="workflow.id"
        class="rounded-md border border-[#E4E4E7] bg-white p-3"
      >
        <div class="flex flex-wrap items-start justify-between gap-2">
          <div class="min-w-0">
            <h4 class="text-sm font-semibold text-[#09090B]">{{ workflow.name }}</h4>
            <p class="break-all font-mono-data text-[11px] text-[#71717A]">{{ workflow.id }}</p>
          </div>
          <Button
            v-if="canEdit"
            class="min-h-9"
            :aria-label="`Edit ${workflow.name}`"
            @click="beginEdit(workflow, $event)"
          >
            Edit
          </Button>
        </div>
        <p class="mt-2 text-xs leading-5 text-[#52525B]">{{ workflow.description }}</p>
        <dl class="mt-3 grid gap-x-4 gap-y-2 border-t border-[#E4E4E7] pt-3 text-xs sm:grid-cols-2">
          <div>
            <dt class="font-medium text-[#71717A]">Calls included</dt>
            <dd class="break-words">{{ (workflow.participating_tools ?? []).join(", ") }}</dd>
          </div>
          <div>
            <dt class="font-medium text-[#71717A]">Calls required for completion</dt>
            <dd>
              {{
                (workflow.required_evidence ?? []).length
                  ? (workflow.required_evidence ?? [])
                      .map((item: any) => `${item.tool} (${item.min_successful_calls} successful)`)
                      .join(", ")
                  : "No required calls"
              }}
            </dd>
          </div>
          <div>
            <dt class="font-medium text-[#71717A]">Completion result</dt>
            <dd class="break-words">
              {{ workflow.completion.tool }} · {{ workflow.completion.success_when.path }} ·
              {{ workflow.completion.success_when.type.replaceAll("_", " ") }}
            </dd>
          </div>
          <div>
            <dt class="font-medium text-[#71717A]">Manual baseline</dt>
            <dd class="tabular-nums">
              {{ workflow.business_value.manual_baseline_minutes }} min ·
              {{ sourceLabel(workflow.business_value.baseline_source) }} ({{
                confidenceLabel(workflow.business_value.confidence)
              }})
            </dd>
          </div>
        </dl>
      </li>
    </ul>

    <div class="mt-3 flex flex-wrap items-center justify-between gap-3">
      <div class="flex flex-wrap items-center gap-3">
        <label v-if="canEdit" class="inline-flex min-h-9 items-center gap-2 text-xs text-[#09090B]">
          <Switch
            :checked="localAutoDiscovery"
            :disabled="togglingAutoDiscovery"
            :aria-label="`Toggle background auto-discovery for ${upstream}`"
            @checked-change="(checked) => toggleAutoDiscovery(checked === true)"
          >
            <SwitchThumb />
          </Switch>
          <span>Auto-discover traffic ({{ localAutoDiscovery ? "On" : "Off" }})</span>
        </label>
        <Button v-if="canEdit && localAutoDiscovery" :disabled="discovering" @click="discoverWorkflows">
          {{ discovering ? "Scanning…" : "Auto-discover workflows" }}
        </Button>
      </div>
      <NuxtLink
        :to="{ path: '/workflows', query: { upstream } }"
        class="text-xs font-medium text-[#0F766E] hover:underline"
      >
        Manage &amp; review unconfigured workflows →
      </NuxtLink>
    </div>

    <p
      v-if="discoverError"
      role="alert"
      class="mt-2 rounded-md border border-red-200 bg-red-50 px-3 py-2 text-xs text-red-800"
    >
      {{ discoverError }}
    </p>
    <div v-if="discoveredCandidates.length" class="mt-3">
      <div class="text-xs font-semibold text-[#09090B]">
        Auto-discovered workflow candidates (confirm human baseline to publish)
      </div>
      <ul class="mt-2 divide-y divide-[#E4E4E7] border-y border-[#E4E4E7]">
        <li
          v-for="cand in discoveredCandidates"
          :key="cand.candidate_id || cand.workflow_slug"
          class="flex flex-wrap items-center justify-between gap-2 py-2.5 text-xs"
        >
          <div>
            <span class="font-medium text-[#09090B]">
              {{ cand.draft_definition?.name || cand.workflow_slug }}
            </span>
            <span class="text-[#71717A]">
              · {{ cand.origin }} · observed
              <span class="tabular-nums">{{ cand.observed_count }}×</span>
            </span>
          </div>
          <div class="flex flex-wrap items-center gap-2">
            <Button v-if="canEdit" class="min-h-11" @click="useDiscoveredCandidate(cand, $event)">
              Review &amp; set baseline
            </Button>
            <Button v-if="canEdit" class="min-h-11" @click="dismissCandidate(cand)">
              Dismiss
            </Button>
          </div>
        </li>
      </ul>
    </div>

    <WorkflowEditorDrawer
      v-model:open="editorOpen"
      :upstream="upstream"
      :tools="tools"
      :definition="definition"
      :editing-workflow="editingWorkflow"
      :promoting-candidate="promotingCandidate"
      lock-upstream
      @saved="handleWorkflowSaved"
    />

    <PromptBundleDrawer
      v-model:open="promptDrawerOpen"
      focus="workflows"
      :upstream="upstream"
      @applied="handleBundleApplied"
    />
  </section>
</template>
