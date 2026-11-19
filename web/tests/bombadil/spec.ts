/**
 * Antithesis Bombadil property-based UI fuzzing invariants for MCPcodeaway Admin Console.
 * Run with `bombadil test web/tests/bombadil/spec.ts http://127.0.0.1:8000/dashboard/`
 * or via `./scripts/run_bombadil.sh` (which executes the Playwright randomized fuzzer fallback).
 */
export interface BombadilPageState {
  url: string;
  textLength: number;
  overflowPx: number;
  hasHeading: boolean;
  pageErrors: string[];
  http5xx: number[];
}

export const BOMBADIL_INVARIANTS = {
  noUncaughtPageErrors: (s: BombadilPageState): boolean => s.pageErrors.length === 0,
  noServer5xx: (s: BombadilPageState): boolean => s.http5xx.length === 0,
  noBlankCrashScreen: (s: BombadilPageState): boolean => s.textLength > 40 && s.hasHeading,
  noHorizontalOverflow: (s: BombadilPageState): boolean => s.overflowPx <= 2,
};
