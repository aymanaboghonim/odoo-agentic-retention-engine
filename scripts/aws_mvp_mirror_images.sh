#!/usr/bin/env bash
# Mirror the pinned third-party images into ECR with identical digests (#97, ADR-0019).
# Source list: deploy/aws-mvp-images.json. Task definitions pin these digests, so a copy
# that changed the digest would fail closed at the post-copy check below.
#
#   scripts/aws_mvp_mirror_images.sh <registry> [--dry-run]
#
# Needs: aws CLI with ECR push rights, skopeo, jq. Idempotent: present digests are skipped.
set -euo pipefail

REGISTRY=${1:?usage: aws_mvp_mirror_images.sh <registry> [--dry-run]}
DRY_RUN=${2:-}
PREFIX=${AORTE_PREFIX:-aorte-mvp}
REPO=$(cd "$(dirname "$0")/.." && pwd)
LIST="$REPO/deploy/aws-mvp-images.json"
START=$(date +%s)

stage() { printf '[mirror +%ss] %s\n' "$(($(date +%s) - START))" "$*"; }
for tool in aws skopeo jq; do
  command -v "$tool" >/dev/null || {
    echo "[mirror] FAILED: $tool is required" >&2
    exit 1
  }
done

if [ "$DRY_RUN" != "--dry-run" ]; then
  aws ecr get-login-password | skopeo login --username AWS --password-stdin "$REGISTRY" >/dev/null
fi

total=$(jq 'length' "$LIST")
n=0
for name in $(jq -r 'keys[]' "$LIST"); do
  n=$((n + 1))
  source=$(jq -r --arg n "$name" '.[$n].source' "$LIST")
  digest=$(jq -r --arg n "$name" '.[$n].digest' "$LIST")
  repo="$PREFIX/$name"
  # Copy by digest only: skopeo rejects tag+digest references (#97), and the digest is the pin.
  image=$source
  case ${source##*/} in *:*) image=${source%:*} ;; esac
  ref="$image@$digest"
  if aws ecr describe-images --repository-name "$repo" --image-ids "imageDigest=$digest" >/dev/null 2>&1; then
    stage "$n/$total $repo@${digest:0:19}: already in ECR"
    continue
  fi
  tag="mirror-${digest:7:12}"
  if [ "$DRY_RUN" = "--dry-run" ]; then
    stage "$n/$total WOULD copy $ref -> $REGISTRY/$repo:$tag"
    continue
  fi
  stage "$n/$total copying $image@${digest:0:19} -> $repo:$tag"
  skopeo copy --all --preserve-digests --retry-times 3 \
    "docker://$ref" "docker://$REGISTRY/$repo:$tag"
  aws ecr describe-images --repository-name "$repo" --image-ids "imageDigest=$digest" >/dev/null || {
    echo "[mirror] FAILED: $repo does not hold $digest after the copy" >&2
    exit 1
  }
done
stage "done: $total images checked"
