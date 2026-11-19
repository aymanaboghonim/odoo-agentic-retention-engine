#!/usr/bin/env bash
# Register a new core task definition revision with the engine image tag and roll out the
# core service (#97; ADR-0027: the operator container replaced the proposal task).
# Terraform owns everything else; the service ignores task_definition, so this does not
# drift. Desired count stays as Terraform set it.
#
#   scripts/aws_mvp_rollout.sh <engine-image-tag>
#
# Waits on the new deployment's rolloutState (not `aws ecs wait services-stable`, which
# stops after 10 minutes; a core rollout takes about 10 minutes, #109). Limits:
# AORTE_ROLLOUT_TIMEOUT seconds (default 1200), polled every AORTE_ROLLOUT_POLL (15).
set -euo pipefail

TAG=${1:?usage: aws_mvp_rollout.sh <engine-image-tag>}
PREFIX=${AORTE_PREFIX:-aorte-mvp}
START=$(date +%s)
TIMEOUT=${AORTE_ROLLOUT_TIMEOUT:-1200}
POLL=${AORTE_ROLLOUT_POLL:-15}
# Engine settings without a default that every engine container must carry (#126).
REQUIRED_ENGINE_ENV="TELEMETRY_ENABLED"
stage() { printf '[rollout +%ss] %s\n' "$(($(date +%s) - START))" "$*"; }
fail() { printf 'FAILED (rollout): %s\n' "$*" >&2; exit 1; }

wait_rollout() {
  local arn=$1 deadline state
  deadline=$(($(date +%s) + TIMEOUT))
  while :; do
    state=$(aws ecs describe-services --cluster "$PREFIX" --services core \
      --query 'services[0].deployments[?taskDefinition==`'"$arn"'`].rolloutState | [0]' --output text)
    case "$state" in
      COMPLETED) stage "rollout COMPLETED"; return 0 ;;
      FAILED) fail "ECS reports rolloutState FAILED for $arn (see the service events)" ;;
    esac
    [ "$(date +%s)" -lt "$deadline" ] || fail "no COMPLETED rollout after ${TIMEOUT}s (state $state)"
    stage "rollout $state; next check in ${POLL}s"
    sleep "$POLL"
  done
}

for family in core; do
  current=$(aws ecs describe-task-definition --task-definition "$PREFIX-$family" --query taskDefinition --output json)
  # Preflight (#126): the rollout copies the current environment, so a setting the new engine
  # requires must already be in the task definition. Apply Terraform first (tf_safe.sh).
  for name in $REQUIRED_ENGINE_ENV; do
    missing=$(jq -r --arg name "$name" '
      [.containerDefinitions[] | select(.name == "worker" or .name == "approval-api" or .name == "operator")
       | select(([.environment[]?.name] | index($name)) == null) | .name] | join(",")' <<<"$current")
    [ -z "$missing" ] || fail "task definition lacks $name in: $missing; apply Terraform first (scripts/tf_safe.sh)"
  done
  next=$(jq --arg tag "$TAG" --arg repo "/$PREFIX/engine:" '
    {family, networkMode, requiresCompatibilities, executionRoleArn, taskRoleArn, volumes,
     containerDefinitions: [.containerDefinitions[]
       | if (.image | contains($repo)) then .image = ((.image | split(":")[0:-1] | join(":")) + ":" + $tag) else . end]}
    | with_entries(select(.value != null))' <<<"$current")
  arn=$(aws ecs register-task-definition --cli-input-json "$next" --query taskDefinition.taskDefinitionArn --output text)
  stage "registered $arn"
  if [ "$family" = core ]; then
    aws ecs update-service --cluster "$PREFIX" --service core --task-definition "$arn" >/dev/null
    desired=$(aws ecs describe-services --cluster "$PREFIX" --services core --query 'services[0].desiredCount' --output text)
    if [ "$desired" -gt 0 ]; then
      stage "waiting for the rollout (desired $desired, limit ${TIMEOUT}s)"
      wait_rollout "$arn"
    fi
    stage "core service uses $arn"
  fi
done
stage "done"
