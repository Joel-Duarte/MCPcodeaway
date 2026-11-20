export interface ToolHealthRow {
  tool: string;
  count: number;
  errors: number | null;
  mean_latency_ms: number | null;
}

export function summarizeUpstreamHealth(rows: ToolHealthRow[]) {
  const calls = rows.reduce((total, row) => total + row.count, 0);
  const knownErrors = rows.every((row) => row.errors != null)
    ? rows.reduce((total, row) => total + (row.errors || 0), 0)
    : null;
  const latencyTotal = rows.reduce(
    (total, row) => total + (row.mean_latency_ms == null ? 0 : row.count * row.mean_latency_ms),
    0,
  );
  const latencyCalls = rows.reduce(
    (total, row) => total + (row.mean_latency_ms == null ? 0 : row.count),
    0,
  );
  return {
    toolCount: rows.length,
    calls,
    errors: knownErrors,
    avgLatencyMs: latencyCalls ? Math.round((latencyTotal / latencyCalls) * 10) / 10 : null,
    successRatePct: calls && knownErrors != null ? Math.round((1 - knownErrors / calls) * 1000) / 10 : null,
  };
}
