#!/usr/bin/env bash
set -uo pipefail

root="$(git rev-parse --show-toplevel 2>/dev/null)" || exit 0
script="$root/scripts/pre-ci.sh"
[[ -x "$script" ]] || exit 0

out="$(mktemp)"
trap 'rm -f "$out"' EXIT

bash "$script" --fast >"$out" 2>&1
rc=$?
if [[ "$rc" -eq 0 ]]; then
  exit 0
fi

{
  echo "Pre-CI fast gate FAILED (exit $rc). Fix the issues below and rerun ./scripts/pre-ci.sh --fast:"
  cat "$out"
} >&2
exit 2
