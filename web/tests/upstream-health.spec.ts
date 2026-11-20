import { expect, test } from "vitest";
import { summarizeUpstreamHealth } from "~/utils/upstream-health";

test("upstream summary counts dispatcher calls and preserves unknown error coverage", () => {
  expect(summarizeUpstreamHealth([
    { tool: "read", count: 3, errors: 1, mean_latency_ms: 10 },
    { tool: "write", count: 1, errors: 0, mean_latency_ms: 30 },
  ])).toEqual({
    toolCount: 2,
    calls: 4,
    errors: 1,
    avgLatencyMs: 15,
    successRatePct: 75,
  });
  expect(summarizeUpstreamHealth([
    { tool: "historical", count: 2, errors: null, mean_latency_ms: null },
  ])).toMatchObject({ calls: 2, errors: null, avgLatencyMs: null, successRatePct: null });
});
