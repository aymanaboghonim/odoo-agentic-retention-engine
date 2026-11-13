#!/usr/bin/env bash
set -euo pipefail

root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
command="${1:-}"
slot="${2:-}"
if [[ "$command" != "fixture-preflight" && "$command" != "fixture-create" &&
      "$command" != "preflight" && "$command" != "prepare" && "$command" != "show" &&
      "$command" != "approve" && "$command" != "execute" ]]; then
  echo "Usage: bash scripts/poc2_delivery.sh COMMAND [SLOT] [arguments...]" >&2
  exit 2
fi
source "$root/scripts/poc2_stack_preflight.sh"
poc2_stack_preflight

args=()
env_args=(-e ODOO_API_KEY= -e OPENAI_API_KEY=)
if [[ "$command" == "fixture-preflight" ]]; then
  args=(python -m scripts.full_stack_fixture --preflight-only)
  env_args=(-e OPENAI_API_KEY=)
elif [[ "$command" == "fixture-create" ]]; then
  args=(python -m scripts.full_stack_fixture --confirmed-synthetic)
  env_args=(-e OPENAI_API_KEY=)
else
  if [[ "$slot" != "mailpit-en" && "$slot" != "mailpit-ar" &&
        "$slot" != "telegram-en" && "$slot" != "telegram-ar" ]]; then
    echo "An approved delivery slot is required" >&2
    exit 2
  fi
  shift 2
  args=(python -m scripts.poc2_delivery "$command" --slot "$slot" "$@")
  if [[ "$command" == "preflight" || "$command" == "prepare" ]]; then
    env_args=(-e ODOO_API_KEY= -e OPENAI_API_KEY)
  elif [[ "$command" == "execute" ]]; then
    env_args=(-e OPENAI_API_KEY=)
  fi
  if [[ "$slot" == telegram-* ]]; then
    env_args+=(-e TELEGRAM_BOT_TOKEN -e TELEGRAM_TEST_CHAT_ID)
  fi
  if [[ "$slot" == mailpit-* && ( "$command" == "preflight" || "$command" == "execute" ) ]]; then
    compose+=(-f "$root/deploy/poc2-mailpit-review-compose.yml")
    "${compose[@]}" config --quiet
  fi
fi

exec "${compose[@]}" run --rm --no-deps -T --pull never -u "$(id -u):$(id -g)" \
  -v "$root/scripts:/workspace/scripts:ro" \
  -v "$root/src:/workspace/src:ro" \
  "${env_args[@]}" \
  -e PYTHONDONTWRITEBYTECODE=1 \
  -e FULL_STACK_CAPACITY_PROPOSAL= \
  -e EXECUTION_SYNTHETIC_ONLY=YES \
  -e PROPOSAL_SYNTHETIC_ONLY=YES \
  -e PROPOSAL_BUDGET_USD=1 \
  -e PROPOSAL_INPUT_TOKEN_LIMIT=4000 \
  -e PROPOSAL_OUTPUT_TOKEN_LIMIT=8192 \
  -e PROPOSAL_REVIEW_ID=poc2-69-evaluation \
  -e PROPOSAL_REVIEW_BUDGET_USD=1 \
  -e GATE_TIMEZONE=Asia/Riyadh \
  -e GATE_CURRENCY=USD \
  -e GATE_DAILY_BUDGET=450 \
  -e 'GATE_CATEGORY_MIN_MARGINS={"Furniture / Office":"0.2"}' \
  proposal "${args[@]}"
