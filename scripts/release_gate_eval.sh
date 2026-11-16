#!/usr/bin/env bash
set -euo pipefail

root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
phase="${1:-}"
mode="${2:-}"
smoke_digest="${3:-}"
if [[ "$phase" != "smoke" && "$phase" != "evaluation" ]]; then
  echo "Usage: bash scripts/release_gate_eval.sh {smoke|evaluation} {preflight|live} [reviewed-smoke-sha256]" >&2
  exit 2
fi
if [[ "$mode" != "preflight" && "$mode" != "live" ]]; then
  echo "Mode must be preflight or live" >&2
  exit 2
fi
if [[ "$phase" == "evaluation" && ! "$smoke_digest" =~ ^[0-9a-f]{64}$ ]]; then
  echo "Evaluation requires the owner-reviewed smoke report SHA-256" >&2
  exit 2
fi
if [[ "$phase" == "smoke" && -n "$smoke_digest" ]]; then
  echo "Smoke does not accept a report digest" >&2
  exit 2
fi
if [[ -z "${OPENAI_API_KEY:-}" ]]; then
  echo "OPENAI_API_KEY must be set in the process environment" >&2
  exit 1
fi
if [[ ! -d "$root/evals/release/results" ]]; then
  echo "The evals/release/results directory is required" >&2
  exit 1
fi
source "$root/scripts/poc2_stack_preflight.sh"
poc2_stack_preflight

args=(--phase "$phase" --output-dir /reports)
if [[ "$phase" == "evaluation" ]]; then
  args+=(--approved-smoke-sha256 "$smoke_digest")
fi
if [[ "$mode" == "live" ]]; then
  args+=(--execute-live)
fi
exec "${compose[@]}" run --rm --no-deps -T --pull never -u "$(id -u):$(id -g)" \
  -v "$root/scripts:/workspace/scripts:ro" \
  -v "$root/src:/workspace/src:ro" \
  -v "$root/evals/llm:/workspace/evals/llm:ro" \
  -v "$root/evals/release/results:/reports" \
  -e OPENAI_API_KEY \
  -e PYTHONDONTWRITEBYTECODE=1 \
  -e ODOO_API_KEY= \
  -e FULL_STACK_CAPACITY_PROPOSAL= \
  -e PROPOSAL_SYNTHETIC_ONLY=YES \
  -e PROPOSAL_BUDGET_USD=2 \
  -e PROPOSAL_INPUT_TOKEN_LIMIT=4000 \
  -e PROPOSAL_OUTPUT_TOKEN_LIMIT=8192 \
  -e PROPOSAL_REVIEW_ID=release-gate-87 \
  -e PROPOSAL_REVIEW_BUDGET_USD=2 \
  -e GATE_TIMEZONE=Asia/Riyadh \
  -e GATE_CURRENCY=USD \
  -e GATE_DAILY_BUDGET=100000 \
  -e 'GATE_CATEGORY_MIN_MARGINS={"Furniture / Office":"0.2"}' \
  proposal python -m scripts.release_gate_eval "${args[@]}"
