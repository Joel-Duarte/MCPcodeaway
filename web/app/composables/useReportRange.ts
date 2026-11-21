export type ReportRange = "1h" | "24h" | "7d" | "30d" | "all";

export function useReportRange() {
  const selectedRange = useState<ReportRange>("mcpcodeaway-report-range", () => "24h");
  return { selectedRange };
}
