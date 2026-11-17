#!/usr/bin/env bash
# Register new task definition revisions with the engine image tag and roll out the core
# service (#97). Terraform owns everything else; the service ignores task_definition, so
# this does not drift. Desired count stays as Terraform set it.
#
#   scripts/aws_mvp_rollout.sh <engine-image-tag>
set -euo pipefail

TAG=${1:?usage: aws_mvp_rollout.sh <engine-image-tag>}
PREFIX=${AORTE_PREFIX:-aorte-mvp}
START=$(date +%s)
stage() { printf '[rollout +%ss] %s\n' "$(($(date +%s) - START))" "$*"; }

for family in core proposal; do
  current=$(aws ecs describe-task-definition --task-definition "$PREFIX-$family" --query taskDefinition --output json)
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
      stage "waiting for the core service to become stable (desired $desired)"
      aws ecs wait services-stable --cluster "$PREFIX" --services core
    fi
    stage "core service uses $arn"
  fi
done
stage "done"
