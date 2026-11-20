import { createAgent, registerTools } from "@ui-sdk/vue/agent";

export interface AgentConfirmRequest {
  message: string;
  resolve: (allowed: boolean) => void;
}

export type DemoTier = "light" | "enterprise" | "stress";

export interface DemoTrafficStatus {
  enabled: boolean;
  running: boolean;
  tier: DemoTier;
  requests_sent: number;
  errors_count: number;
  last_target: string;
  last_error: string;
}

export interface LiveTelemetryEvent {
  type: "run_recorded";
  run_id: string;
  created_at: string;
  mode: string;
  tool_name: string;
  company_group: string;
  user_sub: string;
  ok: boolean;
  grounding_blocked: boolean;
  duration_ms: number;
  tokens_saved: number;
  mode_decision_reason?: string | null;
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
    tier: "enterprise",
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

  async function startTraffic(tier?: DemoTier): Promise<void> {
    busy.value = true;
    try {
      const t = tier || status.value.tier || "enterprise";
      status.value = await apiFetch<DemoTrafficStatus>(`/admin/demo/traffic/start?tier=${t}`, {
        method: "POST",
      });
    } finally {
      busy.value = false;
    }
  }

  async function setTier(tier: DemoTier): Promise<void> {
    status.value = { ...status.value, tier };
    try {
      status.value = await apiFetch<DemoTrafficStatus>(`/admin/demo/traffic/tier?tier=${tier}`, {
        method: "POST",
      });
    } catch {
      // Keep optimistic selection if demo traffic endpoint is unavailable
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

  async function runBurst(count = 8): Promise<void> {
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
    setTier,
    stopTraffic,
    runBurst,
  };
}

export interface LiveAlertItem {
  id: string;
  severity: "critical" | "warning" | string;
  title: string;
  detail: string;
  observed_at: string;
}

export function useLiveTelemetry() {
  const config = useRuntimeConfig();
  const demoStatus = useState<DemoTrafficStatus>("demo-traffic-status");

  const connected = useState<boolean>("ws-telemetry-connected", () => false);
  const events = useState<LiveTelemetryEvent[]>("ws-telemetry-events", () => []);
  const latestRuntime = useState<Record<string, unknown> | null>(
    "ws-telemetry-runtime",
    () => null
  );
  const alerts = useState<LiveAlertItem[]>("ws-telemetry-alerts", () => []);
  const totalRunsTick = useState<number>("ws-telemetry-runs-tick", () => 0);

  let ws: WebSocket | null = null;
  let reconnectTimer: ReturnType<typeof setTimeout> | null = null;
  let intentionalClose = false;

  function buildWsUrl(): string {
    const apiBase = String(config.public.apiBase || "").replace(/\/+$/, "");
    let base: string;
    if (apiBase.startsWith("http://") || apiBase.startsWith("https://")) {
      base = apiBase.replace(/^http/, "ws");
    } else if (import.meta.client) {
      const proto = window.location.protocol === "https:" ? "wss:" : "ws:";
      base = `${proto}//${window.location.host}`;
    } else {
      return "";
    }
    return `${base}/admin/ws/telemetry`;
  }

  function connect(): void {
    if (!import.meta.client || ws) return;
    intentionalClose = false;
    const url = buildWsUrl();
    if (!url) return;

    try {
      const socket = new WebSocket(url);
      ws = socket;

      socket.onopen = () => {
        connected.value = true;
      };

      socket.onmessage = (msg) => {
        try {
          const data = JSON.parse(String(msg.data));
          if (data.type === "snapshot" || data.type === "runtime_tick") {
            if (data.runtime) latestRuntime.value = data.runtime;
            if (Array.isArray(data.alerts)) alerts.value = data.alerts;
            if (data.demo_traffic && demoStatus.value) {
              demoStatus.value = data.demo_traffic;
            }
          } else if (data.type === "run_recorded") {
            events.value = [data as LiveTelemetryEvent, ...events.value].slice(0, 20);
            totalRunsTick.value += 1;
          }
        } catch {
          // Ignore malformed frame
        }
      };

      socket.onclose = () => {
        connected.value = false;
        ws = null;
        if (!intentionalClose && import.meta.client) {
          reconnectTimer = setTimeout(() => connect(), 2500);
        }
      };

      socket.onerror = () => {
        socket.close();
      };
    } catch {
      connected.value = false;
      ws = null;
    }
  }

  function disconnect(): void {
    intentionalClose = true;
    if (reconnectTimer) {
      clearTimeout(reconnectTimer);
      reconnectTimer = null;
    }
    if (ws) {
      ws.close();
      ws = null;
    }
    connected.value = false;
  }

  return {
    connected,
    events,
    latestRuntime,
    alerts,
    totalRunsTick,
    connect,
    disconnect,
  };
}
