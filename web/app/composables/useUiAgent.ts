import { createAgent, registerTools } from "@ui-sdk/vue/agent";

export interface AgentConfirmRequest {
  message: string;
  resolve: (allowed: boolean) => void;
}

export interface DemoTrafficStatus {
  enabled: boolean;
  running: boolean;
  requests_sent: number;
  errors_count: number;
  last_target: string;
  last_error: string;
}

export function useUiAgentBridge() {
  const pendingConfirm = useState<AgentConfirmRequest | null>("ui-agent-confirm", () => null);
  const webMcpActive = useState<boolean>("ui-agent-webmcp-active", () => false);
  const initialized = useState<boolean>("ui-agent-initialized", () => false);

  function resolveConfirm(allowed: boolean): void {
    const req = pendingConfirm.value;
    pendingConfirm.value = null;
    if (req) req.resolve(allowed);
  }

  function initAgentBridge(): void {
    if (!import.meta.client || initialized.value) return;
    initialized.value = true;

    const agent = createAgent({
      allow: (step, { signal }) => {
        if (!step.confirm) return true;
        return new Promise<boolean>((resolve) => {
          const onAbort = () => {
            if (pendingConfirm.value?.resolve === resolve) {
              pendingConfirm.value = null;
            }
            resolve(false);
          };
          if (signal?.aborted) {
            resolve(false);
            return;
          }
          signal?.addEventListener("abort", onAbort, { once: true });
          pendingConfirm.value = {
            message: step.confirm,
            resolve: (ok) => {
              signal?.removeEventListener("abort", onAbort);
              resolve(ok);
            },
          };
        });
      },
    });

    registerTools(agent, { allow: () => true });
    const nav = navigator as Navigator & { modelContext?: unknown };
    const doc = document as Document & { modelContext?: unknown };
    webMcpActive.value = Boolean(doc.modelContext || nav.modelContext);
  }

  return {
    pendingConfirm,
    webMcpActive,
    initAgentBridge,
    resolveConfirm,
  };
}

export function useDemoTraffic() {
  const { apiFetch } = useAdminApi();
  const status = useState<DemoTrafficStatus>("demo-traffic-status", () => ({
    enabled: false,
    running: false,
    requests_sent: 0,
    errors_count: 0,
    last_target: "",
    last_error: "",
  }));
  const busy = useState<boolean>("demo-traffic-busy", () => false);

  async function refreshStatus(): Promise<void> {
    try {
      status.value = await apiFetch<DemoTrafficStatus>("/admin/demo/traffic/status", {}, true);
    } catch {
      status.value = { ...status.value, enabled: false, running: false };
    }
  }

  async function startTraffic(): Promise<void> {
    busy.value = true;
    try {
      status.value = await apiFetch<DemoTrafficStatus>("/admin/demo/traffic/start", {
        method: "POST",
      });
    } finally {
      busy.value = false;
    }
  }

  async function stopTraffic(): Promise<void> {
    busy.value = true;
    try {
      status.value = await apiFetch<DemoTrafficStatus>("/admin/demo/traffic/stop", {
        method: "POST",
      });
    } finally {
      busy.value = false;
    }
  }

  async function runBurst(count = 6): Promise<void> {
    busy.value = true;
    try {
      const res = await apiFetch<{ status: DemoTrafficStatus }>(
        `/admin/demo/traffic/burst?count=${count}`,
        { method: "POST" }
      );
      if (res?.status) {
        status.value = res.status;
      }
    } finally {
      busy.value = false;
    }
  }

  return {
    status,
    busy,
    refreshStatus,
    startTraffic,
    stopTraffic,
    runBurst,
  };
}
