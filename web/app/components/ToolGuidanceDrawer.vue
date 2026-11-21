<script setup lang="ts">
import { Button } from "@ui-sdk/vue/button";
import {
  Drawer, DrawerBackdrop, DrawerClose, DrawerContent, DrawerDescription, DrawerTitle,
} from "@ui-sdk/vue/drawer";
import { Field, FieldDescription, FieldLabel } from "@ui-sdk/vue/field";
import { Input } from "@ui-sdk/vue/input";
import { Textarea } from "@ui-sdk/vue/textarea";
import { computed, ref, watch } from "vue";
import { extractErrorMessage } from "~/composables/useAdminApi";

interface Guidance {
  description: string;
  parameter_hints: Record<string, string>;
}

interface ToolDetails {
  description: string;
  input_schema: { properties?: Record<string, unknown> };
  output_schema: Record<string, unknown> | null;
  guidance: Guidance | null;
  approval_policy: {
    mode: "none" | "requester_confirmation";
    version: number;
    confirmation_message: string;
  };
  can_edit: boolean;
}

const props = defineProps<{
  upstream: string;
  tool: string;
  opener: HTMLElement | null;
}>();
const open = defineModel<boolean>("open", { required: true });
const { apiFetch } = useAdminApi();
const details = ref<ToolDetails | null>(null);
const description = ref("");
const hints = ref<Record<string, string>>({});
const approvalMode = ref<ToolDetails["approval_policy"]["mode"]>("none");
const confirmationMessage = ref("");
const error = ref("");
const saved = ref(false);
const approvalSaved = ref(false);
const saving = ref(false);
const savingApproval = ref(false);
const loadAttempt = ref(0);
const endpoint = computed(() =>
  `/admin/upstreams/${encodeURIComponent(props.upstream)}/tools/${encodeURIComponent(props.tool)}/guidance`
);
const parameters = computed(() => Object.keys(details.value?.input_schema.properties ?? {}));

async function load(signal: AbortSignal) {
  details.value = null;
  error.value = "";
  saved.value = false;
  try {
    const response = await apiFetch<ToolDetails>(endpoint.value, { signal });
    if (signal.aborted) return;
    details.value = response;
    description.value = response.guidance?.description ?? "";
    approvalMode.value = response.approval_policy.mode;
    confirmationMessage.value = response.approval_policy.confirmation_message;
    hints.value = Object.fromEntries(parameters.value.map((name) => [
      name, response.guidance?.parameter_hints[name] ?? "",
    ]));
  } catch (err) {
    if (!signal.aborted) error.value = extractErrorMessage(err);
  }
}

async function saveApprovalPolicy() {
  if (!details.value?.can_edit || savingApproval.value) return;
  error.value = "";
  approvalSaved.value = false;
  savingApproval.value = true;
  try {
    const response = await apiFetch<{ approval_policy: ToolDetails["approval_policy"] }>(
      `/admin/upstreams/${encodeURIComponent(props.upstream)}/tools/${encodeURIComponent(props.tool)}/approval-policy`,
      {
        method: "PUT",
        body: {
          mode: approvalMode.value,
          confirmation_message: confirmationMessage.value.trim(),
        },
      }
    );
    if (response) {
      approvalMode.value = response.approval_policy.mode;
      confirmationMessage.value = response.approval_policy.confirmation_message;
      approvalSaved.value = true;
    }
  } catch (err) {
    error.value = extractErrorMessage(err);
  } finally {
    savingApproval.value = false;
  }
}

watch([open, endpoint, loadAttempt], ([isOpen], _, onCleanup) => {
  if (!isOpen) return;
  const controller = new AbortController();
  onCleanup(() => controller.abort());
  void load(controller.signal);
}, { immediate: true });

async function save() {
  if (!details.value?.can_edit || saving.value) return;
  error.value = "";
  saved.value = false;
  saving.value = true;
  const target = endpoint.value;
  try {
    await apiFetch(target, {
      method: "PUT",
      body: {
        description: description.value.trim(),
        parameter_hints: Object.fromEntries(Object.entries(hints.value)
          .map(([name, hint]) => [name, hint.trim()])
          .filter(([, hint]) => hint)),
      } satisfies Guidance,
    });
    if (endpoint.value === target) saved.value = true;
  } catch (err) {
    if (endpoint.value === target) error.value = extractErrorMessage(err);
  } finally {
    saving.value = false;
  }
}
</script>

<template>
  <Drawer v-model:open="open" side="right">
    <DrawerBackdrop />
    <DrawerContent
      initial-focus="content"
      :final-focus="() => props.opener"
      class="flex h-dvh w-full max-w-full flex-col gap-0 border-l border-zinc-200 bg-white p-0 sm:w-[34rem]"
    >
      <header class="flex items-start justify-between gap-3 border-b border-zinc-200 px-4 py-4 sm:px-5">
        <div class="min-w-0">
          <DrawerTitle class="text-[0.9375rem] font-semibold text-zinc-950">Tool details</DrawerTitle>
          <DrawerDescription class="mt-1 break-all font-mono-data text-xs text-zinc-600">{{ tool }}</DrawerDescription>
        </div>
        <DrawerClose class="min-h-11 sm:min-h-9">Close</DrawerClose>
      </header>

      <div v-if="!details" class="min-h-0 flex-1 overflow-y-auto p-4 sm:p-5">
        <div v-if="error" role="alert" class="rounded-md border border-red-200 bg-red-50 p-3 text-sm text-red-800">
          <p>{{ error }}</p>
          <Button class="mt-2 min-h-11 sm:min-h-9" @click="loadAttempt += 1">Retry loading tool</Button>
        </div>
        <div v-else role="status" class="space-y-3">
          <span class="sr-only">Loading tool details</span>
          <div class="h-4 w-2/5 animate-pulse rounded bg-zinc-100" />
          <div class="h-24 animate-pulse rounded bg-zinc-100" />
        </div>
      </div>

      <form v-else class="flex min-h-0 flex-1 flex-col" @submit.prevent="save">
        <div class="min-h-0 flex-1 space-y-5 overflow-y-auto px-4 py-5 sm:px-5" @input="saved = false; approvalSaved = false">
          <Field>
            <FieldLabel class="text-sm font-medium">Tool description</FieldLabel>
            <Textarea v-model="description" :readonly="!details.can_edit" :disabled="saving" rows="3" maxlength="400" />
            <FieldDescription class="break-words text-xs text-zinc-600">
              Leave blank to use the upstream description.
            </FieldDescription>
            <p v-if="details.description" class="mt-1 break-words text-xs text-zinc-600">{{ details.description }}</p>
          </Field>

          <section v-if="parameters.length" aria-labelledby="tool-parameter-hints" class="space-y-3 border-t border-zinc-200 pt-4">
            <h3 id="tool-parameter-hints" class="text-[0.9375rem] font-semibold">Parameter hints</h3>
            <Field v-for="parameter in parameters" :key="parameter">
              <FieldLabel class="break-all text-sm font-medium">{{ parameter }} hint</FieldLabel>
              <Input v-model="hints[parameter]" :readonly="!details.can_edit" :disabled="saving" maxlength="160" class="min-h-11 sm:min-h-9" />
            </Field>
          </section>

          <section aria-labelledby="tool-approval-policy" class="space-y-3 border-t border-zinc-200 pt-4">
            <div>
              <h3 id="tool-approval-policy" class="text-[0.9375rem] font-semibold">Requester confirmation</h3>
              <p class="mt-1 text-xs text-zinc-600">When enabled, direct calls pause for the MCP client to ask the caller before dispatch. Code Mode calls are rejected. This does not create an external manager approval.</p>
            </div>
            <Field>
              <FieldLabel class="text-sm font-medium">Policy</FieldLabel>
              <select v-model="approvalMode" :disabled="!details.can_edit || savingApproval" class="min-h-11 w-full rounded-md border border-zinc-300 bg-white px-3 text-sm focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-teal-700 sm:min-h-9">
                <option value="none">No extra confirmation</option>
                <option value="requester_confirmation">Ask caller before each call</option>
              </select>
            </Field>
            <Field>
              <FieldLabel class="text-sm font-medium">Prompt message</FieldLabel>
              <Textarea v-model="confirmationMessage" :readonly="!details.can_edit" :disabled="savingApproval" rows="2" maxlength="300" />
              <FieldDescription class="text-xs text-zinc-600">Optional. Use only with MCP clients that show the tool arguments beside this prompt. The gateway cannot verify that a person clicked.</FieldDescription>
            </Field>
            <div v-if="details.can_edit" class="flex justify-end">
              <Button type="button" :disabled="savingApproval" class="min-h-11 sm:min-h-9" @click="saveApprovalPolicy">
                {{ savingApproval ? "Saving policy…" : "Save confirmation policy" }}
              </Button>
            </div>
            <p v-if="approvalSaved" role="status" class="text-sm text-emerald-700">Confirmation policy saved.</p>
          </section>

          <div class="space-y-2 border-t border-zinc-200 pt-3">
            <details>
              <summary class="min-h-11 cursor-pointer py-2 text-sm font-medium sm:min-h-9">Imported input schema</summary>
              <pre class="max-h-56 overflow-auto rounded-md bg-zinc-100 p-3 text-xs">{{ JSON.stringify(details.input_schema, null, 2) }}</pre>
            </details>
            <details v-if="details.output_schema">
              <summary class="min-h-11 cursor-pointer py-2 text-sm font-medium sm:min-h-9">Imported output schema</summary>
              <pre class="max-h-56 overflow-auto rounded-md bg-zinc-100 p-3 text-xs">{{ JSON.stringify(details.output_schema, null, 2) }}</pre>
            </details>
            <p v-else class="text-xs text-zinc-600">This tool has no output schema.</p>
          </div>
        </div>

        <footer class="space-y-2 border-t border-zinc-200 px-4 py-3 sm:px-5">
          <p v-if="error" role="alert" class="break-words text-sm text-red-800">{{ error }} Try saving again.</p>
          <p v-if="saved" role="status" class="text-sm text-emerald-700">Tool guidance saved.</p>
          <div v-if="details.can_edit" class="flex justify-end">
            <Button type="submit" data-variant="primary" :disabled="saving" class="min-h-11 sm:min-h-9">
              {{ saving ? "Saving guidance…" : "Save guidance" }}
            </Button>
          </div>
          <p v-else class="text-xs text-zinc-600">Read-only tool details.</p>
        </footer>
      </form>
    </DrawerContent>
  </Drawer>
</template>
