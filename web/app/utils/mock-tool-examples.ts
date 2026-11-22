const demoExamples: Record<string, Record<string, unknown>> = {
  "snowflake_ledger__query_debt_ledger": { status: "overdue", region: "EU" },
  "snowflake_ledger__get_customer_balance": { customer_id: "CUST-1003" },
  "snowflake_ledger__get_trial_balance_lines": { only_variances: true },
  "snowflake_ledger__compute_interest_penalty": {
    customer_id: "CUST-1003",
    principal: 1375,
    days_past_due: 75,
    annual_rate_pct: 18,
  },
  "billing_legacy_eu__list_eu_invoices": { status: "overdue", dataset: "standard" },
  "billing_legacy_eu__get_eu_customer": { customer_id: "CUST-1003" },
  "billing_legacy_us__list_us_invoices": { status: "overdue", dataset: "standard" },
  "billing_legacy_us__get_us_customer": { customer_id: "CUST-1024" },
  "ops_ticketing__list_terminal_incidents": { status: "open" },
  "ops_ticketing__get_terminal_health": { terminal_id: "TERM-EU-44" },
  "ops_ticketing__search_sentry_errors": { terminal_id: "TERM-EU-44" },
  "ops_ticketing__get_github_release_commit": { release_sha: "sha-9f4c21a" },
  "ops_ticketing__create_collection_case": {
    customer_id: "CUST-1003",
    reason: "Example only: settlement delay follow-up",
    priority: "high",
  },
  "recovery_workbench__submit_change": {
    idempotency_key: "demo-example-only",
    resource_id: "terminal/TERM-EU-44",
    change: "Example only: update terminal configuration",
    simulate_pending: true,
  },
  "recovery_workbench__get_change_status": { idempotency_key: "demo-example-only" },
};

const demoUpstreams = new Set([
  "billing_legacy_eu",
  "billing_legacy_us",
  "ops_ticketing",
  "recovery_workbench",
  "snowflake_ledger",
]);

export function getMockToolExample(upstream: string, tool: string) {
  const localToolName = tool.startsWith(`${upstream}__`)
    ? tool.slice(upstream.length + 2)
    : tool;
  const args = demoExamples[`${upstream}__${localToolName}`];
  if (!args || !demoUpstreams.has(upstream)) return null;
  return { arguments: args, localToolName };
}
