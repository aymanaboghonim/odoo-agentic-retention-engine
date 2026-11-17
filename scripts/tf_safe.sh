#!/usr/bin/env bash
# Terraform safety workflow (spec: docs/specs/aws-mvp-deployment.md, INF-7).
#
#   scripts/tf_safe.sh <root> plan         [--allow-replace ADDRESS]...
#   scripts/tf_safe.sh <root> destroy-plan [--allow-replace ADDRESS]...
#   scripts/tf_safe.sh <root> apply --confirm <plan-id>
#
# plan/destroy-plan: static gates, identity preflight, `terraform plan -out`, and the
# plan policy check. Nothing changes in AWS. It prints a plan id for the reviewer.
# apply: applies exactly the checked plan whose id the reviewer confirmed, deletes
# the plan files, then requires a zero-drift re-plan. Never runs a bare apply.
set -euo pipefail

usage() {
  sed -n '4,6p' "$0" | sed 's/^# //' >&2
  exit 2
}

[ $# -ge 2 ] || usage
ROOT=$1
ACTION=$2
shift 2
REPO=$(cd "$(dirname "$0")/.." && pwd)
POLICY=(uv run --no-sync python "$REPO/scripts/check_terraform_policy.py")
TF=${TERRAFORM:-terraform}
STATE_DIR="$ROOT/.tfsafe"
PLAN="$STATE_DIR/plan.tfplan"
PLAN_JSON="$STATE_DIR/plan.json"
PLAN_OK="$STATE_DIR/plan.ok"
START=$(date +%s)

stage() { printf '[tf_safe %s +%ss] %s\n' "$ACTION" "$(($(date +%s) - START))" "$*"; }
fail() {
  printf '[tf_safe %s] FAILED: %s\n' "$ACTION" "$*" >&2
  exit 1
}

ALLOW=()
CONFIRM=""
while [ $# -gt 0 ]; do
  case $1 in
    --allow-replace)
      [ $# -ge 2 ] || usage
      ALLOW+=(--allow-replace "$2")
      shift 2
      ;;
    --confirm)
      [ $# -ge 2 ] || usage
      CONFIRM=$2
      shift 2
      ;;
    *) usage ;;
  esac
done

[ -d "$ROOT" ] || fail "root $ROOT does not exist"
command -v "$TF" >/dev/null || fail "terraform not found (install the version in F-062)"
command -v aws >/dev/null || fail "aws CLI not found"
[ -n "${AWS_PROFILE:-}" ] || fail "set AWS_PROFILE (for example aws-mvp); refusing default credentials"

identity_preflight() {
  stage "identity preflight (profile $AWS_PROFILE)"
  local arn
  if ! arn=$(aws sts get-caller-identity --query Arn --output text 2>&1); then
    case $arn in
      *Token*expired* | *SSO* | *sso* | *expired*)
        fail "SSO session expired: run 'aws sso login --profile $AWS_PROFILE' (the owner approves in the browser)"
        ;;
    esac
    fail "cannot read caller identity: $arn"
  fi
  case $arn in
    *:root) fail "refusing the root identity; use the Identity Center profile" ;;
  esac
  stage "identity ok: ${arn##*/} (account check is enforced by provider allowed_account_ids)"
}

static_gates() {
  stage "static gates: fmt, init, validate, source policy"
  "$TF" -chdir="$ROOT" fmt -check -recursive >/dev/null || fail "terraform fmt -check failed (run terraform fmt)"
  local backend=()
  # The mvp root reads its state bucket from the gitignored backend.hcl (bootstrap output).
  [ -f "$ROOT/backend.hcl" ] && backend=(-backend-config=backend.hcl)
  "$TF" -chdir="$ROOT" init -input=false -no-color "${backend[@]}" >/dev/null || fail "terraform init failed"
  "$TF" -chdir="$ROOT" validate -no-color >/dev/null || fail "terraform validate failed"
  "${POLICY[@]}" --source "$ROOT" || fail "source policy check failed"
}

plan_id() { sha256sum "$PLAN" | cut -c1-12; }

make_plan() {
  local destroy=()
  [ "$ACTION" = destroy-plan ] && destroy=(-destroy)
  static_gates
  identity_preflight
  rm -rf "$STATE_DIR"
  mkdir -p "$STATE_DIR"
  chmod 700 "$STATE_DIR"
  stage "terraform plan ${destroy[*]:-} -out (no AWS changes)"
  "$TF" -chdir="$ROOT" plan -input=false -no-color "${destroy[@]}" -out=.tfsafe/plan.tfplan \
    >"$STATE_DIR/plan.txt" || fail "terraform plan failed (see $STATE_DIR/plan.txt)"
  "$TF" -chdir="$ROOT" show -json .tfsafe/plan.tfplan >"$PLAN_JSON"
  stage "plan policy check"
  "${POLICY[@]}" --source "$ROOT" --plan "$PLAN_JSON" "${ALLOW[@]}" || {
    rm -f "$PLAN" "$PLAN_JSON"
    fail "plan policy check failed; plan discarded"
  }
  printf '%s %s\n' "$(plan_id)" "$ACTION" >"$PLAN_OK"
  stage "review: $STATE_DIR/plan.txt (full plan); destroy/replace lines:"
  grep -E '^\s+# .* (will be destroyed|must be replaced)' "$STATE_DIR/plan.txt" || echo "  (none)"
  stage "plan id $(plan_id). To apply exactly this plan after review:"
  echo "  scripts/tf_safe.sh $ROOT apply --confirm $(plan_id)"
}

apply_plan() {
  [ -f "$PLAN" ] && [ -f "$PLAN_OK" ] || fail "no checked plan in $STATE_DIR; run plan first"
  read -r ok_id ok_action <"$PLAN_OK"
  [ "$(plan_id)" = "$ok_id" ] || fail "plan file changed after the policy check"
  [ -n "$CONFIRM" ] || fail "missing --confirm <plan-id> (the reviewer confirms plan $ok_id)"
  [ "$CONFIRM" = "$ok_id" ] || fail "--confirm $CONFIRM does not match the checked plan $ok_id"
  identity_preflight
  stage "terraform apply $ok_id ($ok_action)"
  if ! "$TF" -chdir="$ROOT" apply -input=false -no-color .tfsafe/plan.tfplan; then
    rm -f "$PLAN" "$PLAN_JSON" "$PLAN_OK"
    fail "terraform apply failed; plan discarded, run plan again to see the current state"
  fi
  rm -f "$PLAN" "$PLAN_JSON" "$PLAN_OK"
  if [ "$ok_action" = destroy-plan ]; then
    local left
    left=$("$TF" -chdir="$ROOT" state list 2>/dev/null | grep -vc '^data\.' || true)
    [ "$left" = 0 ] || fail "destroy left $left managed resources in state"
    stage "destroy complete: no managed resources left in state"
    return
  fi
  stage "post-apply drift check (plan -detailed-exitcode must be 0)"
  local code=0
  "$TF" -chdir="$ROOT" plan -input=false -no-color -detailed-exitcode >"$STATE_DIR/drift.txt" || code=$?
  [ "$code" = 0 ] || fail "post-apply plan is not empty (exit $code); see $STATE_DIR/drift.txt"
  stage "applied and verified: no drift"
}

case $ACTION in
  plan | destroy-plan) make_plan ;;
  apply) apply_plan ;;
  *) usage ;;
esac
