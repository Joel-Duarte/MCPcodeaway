<script setup lang="ts">
import { Button } from "@ui-sdk/vue/button";
import { onBeforeUnmount, onMounted, ref, watch } from "vue";
import { extractErrorMessage } from "~/composables/useAdminApi";

const props = withDefaults(
  defineProps<{
    title: string;
    tool: string;
    resourceUri: string;
    html: string;
    iframeTitle?: string;
    busy?: boolean;
    error?: string;
    compact?: boolean;
  }>(),
  {
    iframeTitle: undefined,
    busy: false,
    error: "",
    compact: false,
  },
);

const { apiFetch } = useAdminApi();
const iframeRef = ref<HTMLIFrameElement | null>(null);
const viewportMode = ref<"inline" | "wide">("inline");
const simulating = ref(false);
const simulationStatus = ref("");
const simulationError = ref("");

watch(
  () => [props.tool, props.resourceUri, props.html] as const,
  () => {
    simulationStatus.value = "";
    simulationError.value = "";
  },
);

async function runSampleSimulation() {
  if (!props.tool) return;
  simulating.value = true;
  simulationError.value = "";
  try {
    const sim = await apiFetch<{ structuredContent: Record<string, any> }>("/admin/mcp-ui/simulate", {
      method: "POST",
      body: { tool: props.tool, arguments: {} },
    });
    const payload = sim.structuredContent ?? { status: "ok" };
    iframeRef.value?.contentWindow?.postMessage(
      {
        jsonrpc: "2.0",
        method: "ui/notifications/tool-result",
        params: { structuredContent: payload },
      },
      "*",
    );
    simulationStatus.value = `Injected simulated result for ${props.tool}`;
  } catch (err) {
    simulationError.value = extractErrorMessage(err);
  } finally {
    simulating.value = false;
  }
}

async function handleIframeMessage(event: MessageEvent) {
  const msg = event.data;
  if (!msg || typeof msg !== "object" || msg.jsonrpc !== "2.0" || msg.id === undefined) return;
  const targetWindow = event.source as Window | null;
  if (!targetWindow) return;

  if (msg.method === "ui/initialize") {
    targetWindow.postMessage(
      {
        jsonrpc: "2.0",
        id: msg.id,
        result: { hostCapabilities: { serverTools: true } },
      },
      "*",
    );
    return;
  }

  if (msg.method === "tools/call") {
    const toolName = String(msg.params?.name || props.tool || "");
    const args =
      msg.params?.arguments && typeof msg.params.arguments === "object"
        ? msg.params.arguments
        : {};
    try {
      const sim = await apiFetch<{ structuredContent: Record<string, any> }>("/admin/mcp-ui/simulate", {
        method: "POST",
        body: { tool: toolName, arguments: args },
      });
      simulationStatus.value = `Simulated ${toolName}(${JSON.stringify(args)})`;
      simulationError.value = "";
      targetWindow.postMessage(
        {
          jsonrpc: "2.0",
          id: msg.id,
          result: { structuredContent: sim.structuredContent, isError: false },
        },
        "*",
      );
    } catch (err) {
      targetWindow.postMessage(
        {
          jsonrpc: "2.0",
          id: msg.id,
          error: { code: -32000, message: extractErrorMessage(err) },
        },
        "*",
      );
    }
  }
}

onMounted(() => {
  if (typeof window !== "undefined") {
    window.addEventListener("message", handleIframeMessage);
  }
});

onBeforeUnmount(() => {
  if (typeof window !== "undefined") {
    window.removeEventListener("message", handleIframeMessage);
  }
});
</script>

<template>
  <div class="rounded-lg border border-[#E4E4E7] bg-[#FAFAFA] overflow-hidden">
    <div class="flex flex-wrap items-center justify-between gap-2 border-b border-[#E4E4E7] bg-white px-3 py-2">
      <div class="flex min-w-0 items-center gap-2 text-[11px]">
        <span class="rounded border border-[#99F6E4] bg-[#F0FDFA] px-1.5 py-0.5 font-medium text-[#0F766E]">
          MCP App Host
        </span>
        <span class="truncate font-mono-data text-[#52525B]" :title="tool">
          {{ tool }}
        </span>
      </div>
      <div class="flex flex-wrap items-center gap-1.5">
        <div
          v-if="!compact"
          class="inline-flex rounded-md border border-[#E4E4E7] bg-[#F4F4F5] p-0.5 text-[11px]"
          role="group"
          aria-label="Preview viewport width"
        >
          <button
            type="button"
            class="rounded px-2 py-0.5 font-medium transition-colors"
            :class="viewportMode === 'inline' ? 'bg-white text-[#09090B] shadow-sm' : 'text-[#52525B]'"
            @click="viewportMode = 'inline'"
          >
            Inline Card
          </button>
          <button
            type="button"
            class="rounded px-2 py-0.5 font-medium transition-colors"
            :class="viewportMode === 'wide' ? 'bg-white text-[#09090B] shadow-sm' : 'text-[#52525B]'"
            @click="viewportMode = 'wide'"
          >
            Wide Panel
          </button>
        </div>
        <Button
          v-if="html && tool"
          class="min-h-8 text-[11px]"
          :disabled="simulating"
          @click="runSampleSimulation"
        >
          {{ simulating ? "Simulating…" : "Simulate Sample Result" }}
        </Button>
      </div>
    </div>

    <div class="space-y-2 p-3">
      <p
        v-if="simulationStatus"
        role="status"
        class="rounded border border-[#99F6E4] bg-[#F0FDFA] px-3 py-1.5 font-mono-data text-[11px] text-[#0F766E]"
      >
        {{ simulationStatus }}
      </p>
      <p
        v-if="simulationError"
        role="alert"
        class="rounded border border-[#FECACA] bg-[#FEF2F2] px-3 py-1.5 text-[11px] text-[#DC2626]"
      >
        {{ simulationError }}
      </p>

      <div v-if="busy" class="py-6 text-center text-[12px] text-[#52525B]" role="status">
        Rendering sandboxed MCP App UI HTML…
      </div>
      <div
        v-else-if="html"
        class="space-y-2 transition-all"
        :class="viewportMode === 'inline' && !compact ? 'mx-auto max-w-[540px]' : 'w-full'"
      >
        <div class="rounded-md border border-[#E4E4E7] bg-white px-3 py-2 text-[11px] text-[#52525B]">
          <span class="font-medium text-[#09090B]">Assistant</span>
          <span class="mx-1.5">invoked</span>
          <code class="font-mono-data text-[#0F766E]">{{ tool }}</code>
          <span class="mx-1.5">·</span>
          <code class="font-mono-data text-[#71717A] break-all">{{ resourceUri }}</code>
        </div>
        <iframe
          ref="iframeRef"
          :title="iframeTitle || `Preview ${title}`"
          sandbox="allow-scripts"
          :srcdoc="html"
          class="w-full rounded-md border border-[#E4E4E7] bg-white"
          :class="compact ? 'min-h-[300px]' : 'min-h-[420px]'"
        />
      </div>
      <p v-else class="py-4 text-[12px] text-[#DC2626]">
        {{ error || "Preview unavailable for this experience." }}
      </p>
    </div>
  </div>
</template>
