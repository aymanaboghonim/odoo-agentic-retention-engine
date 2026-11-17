#!/usr/bin/env bash
# CI gate for deploy/terraform (spec: docs/specs/aws-mvp-deployment.md, INF-1..INF-7).
# Runs without AWS credentials: fmt, init -backend=false, validate, source policy check.
set -euo pipefail

REPO=$(cd "$(dirname "$0")/.." && pwd)
TF_DIR="$REPO/deploy/terraform"
TF=${TERRAFORM:-terraform}

if [ ! -d "$TF_DIR" ]; then
  echo "terraform: no deploy/terraform directory; skipped"
  exit 0
fi
command -v "$TF" >/dev/null || {
  echo "terraform is required for deploy/terraform checks (version in docs/facts-register.md F-062)" >&2
  exit 1
}

roots=()
for dir in "$TF_DIR"/*/; do
  ls "$dir"*.tf >/dev/null 2>&1 && roots+=("${dir%/}")
done
[ ${#roots[@]} -gt 0 ] || {
  echo "terraform: no roots with .tf files under deploy/terraform" >&2
  exit 1
}

"$TF" fmt -check -recursive -diff "$TF_DIR"
for root in "${roots[@]}"; do
  echo "terraform: init/validate ${root#"$REPO"/}"
  # CI and local runs share the provider cache; no backend, so no AWS access is needed.
  TF_IN_AUTOMATION=1 "$TF" -chdir="$root" init -backend=false -input=false -no-color >/dev/null
  "$TF" -chdir="$root" validate -no-color
done

policy_args=()
for root in "${roots[@]}"; do policy_args+=(--source "$root"); done
uv run --no-sync python "$REPO/scripts/check_terraform_policy.py" "${policy_args[@]}"
