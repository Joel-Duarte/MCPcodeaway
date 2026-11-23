# Workflows, ROI & Multi-Worker Telemetry

MCPcodeaway unifies workflow configuration, baseline time-savings governance, gateway-level currency and regional rate calculation, and multi-worker Monty sandbox observability inside the Operate Control Plane (`/dashboard`).

## Control Plane Consolidation & In-Page Drawers

| Surface | Path / Component | What Changed |
| :--- | :--- | :--- |
| **Workflows & ROI** | `/dashboard/workflows` ([`workflows.vue`](../web/app/pages/workflows.vue)) | Replaces separate `Workflows` and `Business Value` sidebar entries. Combines top KPI cards (`Workflow time saved` and `Estimated value & outcomes`), configured/candidate workflows, and the **Outcome & ROI Ledger** tab (`?tab=ledger`, backed by [`BusinessValueReportSection.vue`](../web/app/components/BusinessValueReportSection.vue)). |
| **Legacy Redirect** | `/dashboard/business-value` ([`business-value.vue`](../web/app/pages/business-value.vue)) | Renders the ledger fallback and redirects client-side to `/workflows?tab=ledger`. |
| **Overview Impact** | `/dashboard` ([`index.vue`](../web/app/pages/index.vue)) | Displays the `Overall Gateway Impact & Time Saved` KPI strip at the top alongside per-worker sandbox status cards. |
| **In-Page Drawers** | [`WorkflowEditorDrawer.vue`](../web/app/components/WorkflowEditorDrawer.vue), [`PromptBundleDrawer.vue`](../web/app/components/PromptBundleDrawer.vue) | `Create Workflow`, `Edit Timing & Triggers`, and `Generate Workflows via Prompt` / bundle actions open full-viewport blurred drawers directly on `/workflows`, `/upstreams` ([`UpstreamWorkflowCatalog.vue`](../web/app/components/UpstreamWorkflowCatalog.vue)), and `/mcp-ui` ([`UpstreamMcpUiCatalog.vue`](../web/app/components/UpstreamMcpUiCatalog.vue)) instead of leaving the page. |
| **Active / Pause Switch** | `Switch` in [`workflows.vue`](../web/app/pages/workflows.vue) + `start_business_execution_for_identity()` ([`server.py`](../src/gateway/server.py)) | Toggling a workflow **Off** sets `enabled: false`; the runtime skips starting new tracked episodes for paused workflows without deleting historical outcomes. |

## Baseline Timing, Confidence & Evidence

Each workflow stores structured assumptions on `BusinessValueBaseline` ([`integration_definition.py`](../src/gateway/integration_definition.py)):

- **Time assumptions:** `manual_baseline_minutes`, `review_minutes`, and `correction_minutes` determine net human time saved when an episode completes.
- **Validation invariant:** `BusinessValueBaseline` enforces strict governance rules:
  - `administrator_estimate` sources **cannot** be marked `validated` (rendered as **Unvalidated estimate**).
  - `sampled` or external measurements **cannot** be marked `unvalidated` (rendered as **Sampled baseline** / **Validated baseline**).
- **Evidence rollup:** Configured workflow rows summarize tracked episodes, `completed_verified` outcomes, time saved, and monetary impact alongside status and baseline confidence badges.

## Gateway Reporting Currency & Regional Rate Overrides

Currency and pricing are modeled at the gateway level rather than hardcoding `$`/USD per workflow:

1. **Gateway Default Currency:**
   - Configured via `GATEWAY_DEFAULT_CURRENCY` (`GatewaySettings.default_currency` in [`config.py`](../src/gateway/config.py), defaulting to `"USD"`).
   - Managed live in **Settings -> Workspace & access -> Gateway Reporting Currency** ([`settings.vue`](../web/app/pages/settings.vue)) via `GET /admin/business-value/settings` and `PUT /admin/business-value/settings` ([`server.py`](../src/gateway/server.py)).
   - When timing/pricing edits in [`_apply_baseline_updates()`](../src/gateway/server.py) supply hourly or regional labor rates without an explicit currency, the gateway automatically fills `currency` from [`get_reporting_currency()`](../src/gateway/server.py).
2. **Global Hub Country / Region Overrides:**
   - Each workflow's `BusinessValueBaseline` supports an optional `regional_labor_rates` map (e.g. `{"US": 120, "EU": 85, "BR": 45}` in the workflow/gateway currency), editable in `Edit Timing & Triggers`, [`WorkflowEditorDrawer.vue`](../web/app/components/WorkflowEditorDrawer.vue), and candidate activation.
   - At runtime, [`extract_caller_regions()`](../src/gateway/business_value_runtime.py) inspects verified JWT claims (`country`, `c`, `region`, `geo`, and `client_groups`) and [`apply_regional_baseline()`](../src/gateway/business_value_runtime.py) selects the matching rate override for that episode.
   - [`business_value_report()`](../src/gateway/telemetry.py) (`GET /admin/business-value/report`) returns `reporting_currency`, per-region/workflow breakdowns (`region`), and `totals_by_currency`.

## Multi-Worker Remote Monty Sandbox & Demo Seeding

- **Multi-A DNS Round-Robin Dispatch:** [`RemoteMontyWorkerBackend`](../src/gateway/sandbox.py) resolves multi-A service records (such as Compose's DNS entry for scaled workers) via `_resolve_endpoints()` and rotates execution requests across healthy IPs with `_next_worker_url()`.
- **Per-Replica Health Telemetry:** Each worker exposes `GET /health` in [`sandbox_worker.py`](../src/gateway/sandbox_worker.py) with `worker_id`, `min_processes`, `max_processes`, `max_concurrent_executions`, and `ProcessStatsCollector` metrics. [`collect_runtime_telemetry()`](../src/gateway/runtime_stats.py) concurrently probes all discovered worker endpoints and returns a sorted `sandbox_workers` list surfaced on **Overview** ([`index.vue`](../web/app/pages/index.vue)) and **Traces** ([`traces.vue`](../web/app/pages/traces.vue)).
- **Docker Demo Profile & Golden Evidence:** [`docker-compose.yml`](../docker-compose.yml) sets `sandbox-worker` `deploy.replicas: 2`, `GATEWAY_SANDBOX_BACKEND=remote`, `GATEWAY_SANDBOX_WORKER_URL=ws://sandbox-worker:8002/ws/execute`, and `GATEWAY_DEMO_AUTO_GOLDEN=true`. [`DemoTrafficGenerator`](../src/gateway/demo_traffic.py) (`ensure_demo_solutions()`, `golden_run()`, `_run_passive_workflow_evidences()`) seeds 7 workflows across `global_ticketing`, `hr_directory`, `crm_accounts`, and `devops_ci` with timing, hourly rate, and regional overrides plus verified passive/active execution outcomes.
