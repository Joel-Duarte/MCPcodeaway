#!/usr/bin/env bash
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$REPO_ROOT"

MODE="full"
WITH_DOCKER=0
for arg in "$@"; do
  case "$arg" in
    --fast) MODE="fast" ;;
    --full) MODE="full" ;;
    --docker) WITH_DOCKER=1 ;;
    *)
      echo "Usage: $0 [--fast|--full] [--docker]" >&2
      exit 2
      ;;
  esac
done

echo "=== MCPcodeaway Pre-CI Gate (mode=$MODE) ==="

if command -v ripwire >/dev/null 2>&1; then
  echo ">> [1/6] Ripwire situational AST impact check..."
  ripwire . --situ >/dev/null || true
else
  echo ">> [1/6] Ripwire not on PATH; skipping advisory AST check."
fi

echo ">> [2/6] Deterministic agent preflight checks..."
uv run python scripts/agent_preflight.py

echo ">> [3/6] Ruff format & lint check..."
uv run ruff format --check src examples benchmarks tests scripts
uv run ruff check src examples benchmarks tests scripts

if [[ "$MODE" == "fast" ]]; then
  echo ">> [4/6] Golden E2E flows & locked benchmark regression suite..."
  uv run pytest tests/test_golden_flows.py tests/test_auto_workflow_golden.py tests/test_golden_agent_onboarding.py tests/test_benchmark_regression.py -q
  uv run python benchmarks/benchmark_workflow_classifier.py --backends synthetic_heuristic,classifier_service --verify-thresholds >/dev/null
  echo "=== Fast Pre-CI Gate PASSED ==="
  exit 0
fi

echo ">> [4/6] Full backend pytest suite (incl. Golden flows & Phase 11)..."
uv run pytest -v

echo ">> [5/6] Frontend Vitest agent checks & Nuxt 4 + @ui-sdk/vue static build..."
(cd "$REPO_ROOT/web" && npm run test:unit && NUXT_TELEMETRY_DISABLED=1 npm run generate)

echo ">> [6/6] Locked offline benchmark threshold verification..."
uv run python benchmarks/run_benchmark.py --output /tmp/mcp_pre_ci_bench.json --verify-thresholds
uv run python benchmarks/benchmark_workflow_classifier.py --backends synthetic_heuristic,classifier_service --output /tmp/mcp_pre_ci_classifier_bench.json --verify-thresholds

if [[ "$WITH_DOCKER" == "1" ]] && command -v docker >/dev/null 2>&1; then
  echo ">> [extra] Docker multi-target build verification..."
  docker build --target gateway -t mcpcodeaway:pre-ci-gateway .
  docker build --target sandbox-worker -t mcpcodeaway:pre-ci-worker .
  docker build --target gateway-ui -t mcpcodeaway:pre-ci-ui .
fi

echo "=== Full Pre-CI Gate PASSED ==="
