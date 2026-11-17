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
# Use a throwaway data dir per root: the operator's .terraform/ may point at the real S3
# backend (tf_safe.sh), and this gate must never need AWS credentials. Providers come from
# a shared plugin cache, checked against .terraform.lock.hcl.
export TF_PLUGIN_CACHE_DIR="${TF_PLUGIN_CACHE_DIR:-$HOME/.terraform.d/plugin-cache}"
mkdir -p "$TF_PLUGIN_CACHE_DIR"
DATA_ROOT=$(mktemp -d)
trap 'rm -rf "$DATA_ROOT"' EXIT
for root in "${roots[@]}"; do
  echo "terraform: init/validate ${root#"$REPO"/}"
  export TF_DATA_DIR="$DATA_ROOT/$(basename "$root")"
  TF_IN_AUTOMATION=1 "$TF" -chdir="$root" init -backend=false -input=false -no-color >/dev/null
  "$TF" -chdir="$root" validate -no-color
done
unset TF_DATA_DIR

policy_args=()
for root in "${roots[@]}"; do policy_args+=(--source "$root"); done
uv run --no-sync python "$REPO/scripts/check_terraform_policy.py" "${policy_args[@]}"
