export type ReportRange = "1h" | "24h" | "7d" | "30d" | "all";

/** Shared report window for analytics, optimization and model-performance views. */
export function useReportRange() {
  const selectedRange = useState<ReportRange>("mcpcodeaway-report-range", () => "24h");
  return { selectedRange };
}
