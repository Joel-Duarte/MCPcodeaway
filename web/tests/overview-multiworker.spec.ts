import { mount } from "@vue/test-utils";
import { Button } from "@ui-sdk/vue/button";
import { Field, FieldLabel } from "@ui-sdk/vue/field";
import { Meter, MeterIndicator, MeterLabel, MeterTrack, MeterValue } from "@ui-sdk/vue/meter";
import { Switch, SwitchThumb } from "@ui-sdk/vue/switch";
import { defineComponent, h, nextTick, ref } from "vue";
import { afterEach, expect, test, vi } from "vitest";
import OverviewPage from "~/pages/index.vue";
import TracesPage from "~/pages/traces.vue";

const NuxtLinkStub = defineComponent({
  name: "NuxtLink",
  props: ["to"],
  render() {
    return h("a", { href: typeof this.to === "string" ? this.to : "#" }, this.$slots.default?.());
  },
});

const baseAuditValue = {
  window_runs: 18,
  efficiency: {
    total_subcalls: 42,
    total_raw_upstream_bytes: 240000,
    total_returned_bytes: 36000,
    payload_reduction_pct: 85.0,
    payload_tokens_saved: 51000,
    schema_tokens_saved: 4200,
    total_tokens_saved: 55200,
    estimated_llm_cost_saved_usd: 0.1656,
    llm_turns_avoided: 12,
    context_overflows_prevented: 2,
    obo_cache_hit_rate_pct: 75.0,
  },
  security_and_governance: {
    abac_rows_filtered: 14,
    pii_secrets_redacted: 7,
    grounded_runs_verified: 16,
    grounding_violations_blocked: 2,
  },
  upstream_breakdown: [
    {
      server: "crm_api",
      calls: 24,
      codemode_calls: 18,
      classic_calls: 6,
      raw_bytes: 180000,
      avg_duration_ms: 42.5,
      abac_rows_filtered: 9,
      redactions_count: 5,
    },
    {
      server: "billing_api",
      calls: 18,
      codemode_calls: 6,
      classic_calls: 12,
      raw_bytes: 60000,
      avg_duration_ms: 61.2,
      abac_rows_filtered: 5,
      redactions_count: 2,
    },
  ],
  recommendations: [
    {
      server: "billing_api",
      reason: "12 classic calls averaged 5000 bytes/call; switch callers to codemode.",
    },
  ],
};

const baseAnalytics = {
  summary: {
    total_runs: 18,
    classic_runs: 6,
    codemode_runs: 12,
    codemode_share_pct: 66.7,
  },
};

const baseBusinessValue = {
  completed_count: 9,
  by_integration_workflow_day: [
    { estimated_manual_baseline_minutes: 15 },
    { estimated_manual_baseline_minutes: 22.5 },
  ],
};

const baseWorkflowsCatalog = {
  upstreams: ["crm_api", "billing_api"],
  workflows: [
    { upstream_name: "crm_api", workflow: { id: "wf-1", name: "Lead Lookup", enabled: true } },
    { upstream_name: "billing_api", workflow: { id: "wf-2", name: "Invoice Audit", enabled: true } },
    { upstream_name: "billing_api", workflow: { id: "wf-3", name: "Legacy Export", enabled: false } },
  ],
  candidates: [
    { candidate_id: "cand-1", status: "pending_review" },
    { candidate_id: "cand-2", status: "pending_review" },
    { candidate_id: "cand-3", status: "promoted" },
  ],
  episodes: [],
};

const multiWorkerRuntime = {
  gateway: {
    profile: "prod",
    app_env: "production",
    auth_mode: "oidc",
    catalog_version: "v2026.11",
    upstreams_count: 3,
    cache_backend: "redis",
    store_backend: "postgres",
    cpu_percent: 34.2,
    rss_mb: 182.4,
    load_avg_1m: 0.72,
    cpu_count: 4,
    uptime_seconds: 3600,
    active_executions: 2,
    total_executions: 140,
  },
  sandbox_worker: {
    mode: "remote",
    status: "ok",
    worker_count: 2,
    healthy_count: 2,
  },
  sandbox_workers: [
    {
      worker_id: "monty-worker-1",
      mode: "remote",
      health_url: "http://worker-1:8090/health",
      status: "ok",
      pool: { min_processes: 2, max_processes: 8, max_concurrent_executions: 16 },
      stats: {
        cpu_percent: 48.5,
        rss_mb: 256.3,
        load_avg_1m: 1.15,
        cpu_count: 2,
        uptime_seconds: 7200,
        active_executions: 3,
        total_executions: 88,
      },
    },
    {
      worker_id: "monty-worker-2",
      mode: "remote",
      health_url: "http://worker-2:8090/health",
      status: "ok",
      pool: { min_processes: 2, max_processes: 8, max_concurrent_executions: 16 },
      stats: {
        cpu_percent: 29.0,
        rss_mb: 198.7,
        load_avg_1m: 0.64,
        cpu_count: 2,
        uptime_seconds: 7180,
        active_executions: 1,
        total_executions: 52,
      },
    },
  ],
  monty_telemetry: {
    ephemeral: true,
    codemode_runs: 12,
    grounding_blocked_runs: 2,
    spawn_p50_ms: 1.42,
    spawn_p95_ms: 3.85,
    exec_p50_ms: 14.6,
    exec_p95_ms: 41.25,
  },
};

function stubOverviewGlobals(
  apiFetch: ReturnType<typeof vi.fn>,
  demoOverrides: { enabled?: boolean; runBurst?: ReturnType<typeof vi.fn> } = {},
) {
  vi.stubGlobal("useAdminApi", () => ({
    apiFetch,
    setRuntimeMeta: vi.fn(),
  }));
  vi.stubGlobal("useDemoTraffic", () => ({
    status: ref({ enabled: demoOverrides.enabled ?? true, running: false, tier: "enterprise" }),
    busy: ref(false),
    runBurst: demoOverrides.runBurst ?? vi.fn().mockResolvedValue(undefined),
  }));
  vi.stubGlobal("useLiveTelemetry", () => ({
    connected: ref(true),
    connectionState: ref("live"),
    events: ref([]),
    latestRuntime: ref(null),
    alerts: ref([]),
    totalRunsTick: ref(0),
  }));
}

function mountOverview() {
  return mount(OverviewPage, {
    global: {
      components: {
        Button,
        Meter,
        MeterIndicator,
        MeterLabel,
        MeterTrack,
        MeterValue,
        NuxtLink: NuxtLinkStub,
      },
      stubs: {
        AppIcon: true,
      },
    },
  });
}

afterEach(() => {
  vi.unstubAllGlobals();
});

test("overview renders multi-worker sandbox telemetry, monty telemetry, governance posture, recommendations, upstream table, and business value snapshot", async () => {
  const apiFetch = vi.fn(async (path: string) => {
    if (path === "/audit/value") return baseAuditValue;
    if (path === "/audit/analytics") return baseAnalytics;
    if (path === "/admin/operations/metrics") {
      return { open: 1, needs_attention: 0, completed_24h: 8, failed_24h: 0, failure_rate_24h_pct: 0, upstream_health: [] };
    }
    if (path === "/admin/business-value/report") return baseBusinessValue;
    if (path === "/admin/workflows/catalog") return baseWorkflowsCatalog;
    if (path === "/admin/telemetry/runtime") return multiWorkerRuntime;
    throw new Error(`Unexpected path: ${path}`);
  });
  stubOverviewGlobals(apiFetch);

  const wrapper = mountOverview();
  await vi.waitFor(() => expect(wrapper.text()).toContain("monty-worker-1"));
  await nextTick();

  const text = wrapper.text();
  expect(text).toContain("2 / 2 workers healthy");
  expect(text).toContain("monty-worker-1");
  expect(text).toContain("http://worker-1:8090/health");
  expect(text).toContain("256.3 MiB");
  expect(text).toContain("3 active / 88");
  expect(text).toContain("2–8 processes");
  expect(text).toContain("16 max concurrent");

  expect(text).toContain("monty-worker-2");
  expect(text).toContain("http://worker-2:8090/health");
  expect(text).toContain("198.7 MiB");
  expect(text).toContain("1 active / 52");

  expect(text).toContain("Monty Sandbox Execution Telemetry");
  expect(text).toContain("1.42 ms / 3.85 ms");
  expect(text).toContain("14.60 ms / 41.25 ms");
  expect(text).toContain("Code Mode Runs");
  expect(text).toContain("Grounding Blocked");

  expect(text).toContain("Security & efficiency posture");
  expect(text).toContain("ABAC rows filtered");
  expect(text).toContain("PII / secrets redacted");
  expect(text).toContain("55,200");

  expect(text).toContain("Overall Gateway Impact & Time Saved");
  expect(text).toContain("Human Time Saved (Workflows)");
  expect(text).toContain("Workflows & ROI");

  expect(text).toContain("Business Value & Workflows");
  expect(text).toContain("2 active / 3 total");
  expect(text).toContain("Pending Candidates");
  expect(text).toContain("Completed Outcomes");
  expect(text).toContain("37.5 min");

  expect(text).toContain("Optimization Recommendations");
  expect(text).toContain("billing_api");
  expect(text).toContain("12 classic calls averaged 5000 bytes/call");

  expect(text).toContain("Upstream Execution Breakdown");
  expect(text).toContain("crm_api");
  expect(text).toContain("42.5 ms");
  expect(text).toContain("61.2 ms");

  wrapper.unmount();
});

test("overview falls back cleanly to legacy single sandbox_worker and tolerates optional 403/500 failures", async () => {
  const legacyRuntime = {
    gateway: {
      ...multiWorkerRuntime.gateway,
    },
    sandbox_worker: {
      mode: "remote",
      status: "ok",
      health_url: "http://legacy-worker:8090/health",
      stats: {
        cpu_percent: 17.5,
        rss_mb: 128.0,
        load_avg_1m: 0.42,
        cpu_count: 2,
        uptime_seconds: 900,
        active_executions: 1,
        total_executions: 25,
      },
    },
  };

  const apiFetch = vi.fn(async (path: string) => {
    if (path === "/audit/value") {
      return {
        ...baseAuditValue,
        upstream_breakdown: undefined,
        by_upstream: {
          crm_api: {
            calls: 10,
            codemode_calls: 8,
            classic_calls: 2,
            raw_bytes: 42000,
            avg_duration_ms: 19.4,
            abac_rows_filtered: 3,
            redactions_count: 1,
          },
        },
        recommendations: [],
      };
    }
    if (path === "/audit/analytics") return baseAnalytics;
    if (path === "/admin/operations/metrics") throw Object.assign(new Error("Forbidden"), { statusCode: 403 });
    if (path === "/admin/business-value/report") throw Object.assign(new Error("Server error"), { statusCode: 500 });
    if (path === "/admin/workflows/catalog") throw Object.assign(new Error("Forbidden"), { statusCode: 403 });
    if (path === "/admin/telemetry/runtime") return legacyRuntime;
    throw new Error(`Unexpected path: ${path}`);
  });
  stubOverviewGlobals(apiFetch);

  const wrapper = mountOverview();
  await vi.waitFor(() => expect(wrapper.text()).toContain("http://legacy-worker:8090/health"));
  await nextTick();

  const text = wrapper.text();
  expect(wrapper.find('[role="alert"]').exists()).toBe(false);
  expect(text).toContain("primary");
  expect(text).toContain("128.0 MiB");
  expect(text).toContain("1 active / 25");
  expect(text).toContain("0.00 ms / 0.00 ms");
  expect(text).toContain("crm_api");
  expect(text).toContain("19.4 ms");
  expect(text).toContain("Overall Gateway Impact & Time Saved");
  expect(text).toContain("Human Time Saved (Workflows)");
  expect(text).toContain("Workflows & ROI");

  expect(text).toContain("Business Value & Workflows");
  expect(text).toContain("Unavailable");

  wrapper.unmount();
});

test("overview empty demo CTA exposes Run Demo Burst (+8) and triggers POST /admin/demo/golden-run", async () => {
  const emptyValue = {
    ...baseAuditValue,
    window_runs: 0,
    upstream_breakdown: [],
    recommendations: [],
  };
  const emptyAnalytics = {
    summary: { total_runs: 0, classic_runs: 0, codemode_runs: 0, codemode_share_pct: 0 },
  };
  const apiFetch = vi.fn(async (path: string, opts?: { method?: string }) => {
    if (path === "/audit/value") return emptyValue;
    if (path === "/audit/analytics") return emptyAnalytics;
    if (path === "/admin/operations/metrics") return null;
    if (path === "/admin/business-value/report") return { completed_count: 0, by_integration_workflow_day: [] };
    if (path === "/admin/workflows/catalog") return { upstreams: [], workflows: [], candidates: [], episodes: [] };
    if (path === "/admin/telemetry/runtime") return multiWorkerRuntime;
    if (path === "/admin/demo/golden-run" && opts?.method === "POST") {
      return { traffic_requests: 24, evidence_rows: 360 };
    }
    throw new Error(`Unexpected path: ${path}`);
  });
  stubOverviewGlobals(apiFetch, { enabled: true });

  const wrapper = mountOverview();
  await vi.waitFor(() => expect(wrapper.text()).toContain("Run Golden Demo"));

  expect(wrapper.text()).toContain("Run Demo Burst (+8)");
  const goldenButton = wrapper.findAll("button").find((btn) => btn.text().includes("Run Golden Demo"));
  expect(goldenButton).toBeDefined();

  await goldenButton!.trigger("click");
  await vi.waitFor(() =>
    expect(apiFetch).toHaveBeenCalledWith(
      "/admin/demo/golden-run",
      expect.objectContaining({ method: "POST" }),
    ),
  );
  await vi.waitFor(() => expect(wrapper.text()).toContain("Golden demo completed: 24 MCP calls and 360 simulated report rows recorded."));

  wrapper.unmount();
});

test("traces renders sandbox_workers health count and active executions, and handles omitted sandbox_workers cleanly", async () => {
  vi.stubGlobal("useLiveTelemetry", () => ({ totalRunsTick: ref(0) }));
  vi.stubGlobal("useReportRange", () => ({ selectedRange: ref("24h") }));
  vi.stubGlobal("extractErrorMessage", (err: unknown) => String(err));

  const apiFetchWithWorkers = vi.fn(async (path: string) => {
    if (path.startsWith("/audit/runs")) return { runs: [] };
    if (path.startsWith("/audit/analytics")) return { timeline_by_group: [] };
    if (path === "/admin/telemetry/runtime") {
      return {
        gateway: { active_executions: 4, total_executions: 55 },
        sandbox_workers: [
          { worker_id: "w1", mode: "remote", status: "ok", stats: { active_executions: 2, total_executions: 30 } },
          { worker_id: "w2", mode: "remote", status: "unreachable", stats: { active_executions: 1, total_executions: 25 } },
        ],
        monty_telemetry: {
          spawn_p50_ms: 1.1,
          spawn_p95_ms: 2.9,
          exec_p50_ms: 9.5,
          exec_p95_ms: 22.4,
          grounding_blocked_runs: 1,
        },
      };
    }
    throw new Error(`Unexpected path: ${path}`);
  });
  vi.stubGlobal("useAdminApi", () => ({ apiFetch: apiFetchWithWorkers }));

  const wrapper = mount(TracesPage, {
    global: {
      components: { Button, Field, FieldLabel, NuxtLink: NuxtLinkStub, Switch, SwitchThumb },
      stubs: { AppIcon: true },
    },
  });

  await vi.waitFor(() => expect(wrapper.text()).toContain("Sandbox Workers"));
  await nextTick();
  expect(wrapper.text()).toContain("1 / 2 healthy");
  expect(wrapper.text()).toContain("Degraded");
  expect(wrapper.text()).toContain("3 active worker executions");
  wrapper.unmount();

  const apiFetchWithoutWorkers = vi.fn(async (path: string) => {
    if (path.startsWith("/audit/runs")) return { runs: [] };
    if (path.startsWith("/audit/analytics")) return { timeline_by_group: [] };
    if (path === "/admin/telemetry/runtime") {
      return {
        gateway: { active_executions: 2, total_executions: 10 },
        monty_telemetry: {
          spawn_p50_ms: 0.8,
          spawn_p95_ms: 1.6,
          exec_p50_ms: 6.2,
          exec_p95_ms: 15.0,
          grounding_blocked_runs: 0,
        },
      };
    }
    throw new Error(`Unexpected path: ${path}`);
  });
  vi.stubGlobal("useAdminApi", () => ({ apiFetch: apiFetchWithoutWorkers }));

  const minimalWrapper = mount(TracesPage, {
    global: {
      components: { Button, Field, FieldLabel, NuxtLink: NuxtLinkStub, Switch, SwitchThumb },
      stubs: { AppIcon: true },
    },
  });

  await vi.waitFor(() => expect(minimalWrapper.text()).toContain("Sandbox Workers"));
  await nextTick();
  expect(minimalWrapper.text()).toContain("Unavailable");
  expect(minimalWrapper.text()).toContain("2 active worker executions");
  minimalWrapper.unmount();
});
