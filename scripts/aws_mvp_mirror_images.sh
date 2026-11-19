#!/usr/bin/env bash
# Mirror the pinned third-party images into ECR with identical digests (#97, ADR-0019).
# Source list: deploy/aws-mvp-images.json. Each entry pins `digest` (the image Compose uses,
# often a multi-platform index) and `aws_digest` (its linux/amd64 manifest, the only one the
# host runs; #106). Before copying, the script checks that `aws_digest` is the linux/amd64
# child of `digest` (or equals it for a single-platform pin), so the two pins cannot drift.
# Only the amd64 manifest is copied; task definitions pin it, so a copy that changed the
# digest fails closed at the post-copy check below.
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
  aws_digest=$(jq -r --arg n "$name" '.[$n].aws_digest // empty' "$LIST")
  [ -n "$aws_digest" ] || {
    echo "[mirror] FAILED: $name has no aws_digest" >&2
    exit 1
  }
  # Copy by digest only: skopeo rejects tag+digest references (#97), and the digest is the pin.
  image=$source
  case ${source##*/} in *:*) image=${source%:*} ;; esac
  # The pin relation: aws_digest must be the linux/amd64 manifest of digest (#106).
  raw=$(skopeo inspect --raw --retry-times 3 "docker://$image@$digest")
  if [ "$(jq -r '.manifests | type' <<<"$raw")" = "array" ]; then
    child=$(jq -r '[.manifests[] | select(.platform.os == "linux" and .platform.architecture == "amd64"
      and ((.platform.variant // "") == ""))] | map(.digest) | join(",")' <<<"$raw")
  else
    child=$digest
  fi
  [ "$child" = "$aws_digest" ] || {
    echo "[mirror] FAILED: $name aws_digest ${aws_digest:0:19} is not the linux/amd64 manifest of $digest (found: ${child:-none})" >&2
    exit 1
  }
  ref="$image@$aws_digest"
  tag="amd64-${aws_digest:7:12}"
  if tags=$(aws ecr describe-images --repository-name "$repo" --image-ids "imageDigest=$aws_digest" \
    --query 'imageDetails[0].imageTags' --output text 2>/dev/null); then
    if grep -qw -- "$tag" <<<"$tags"; then
      stage "$n/$total $repo@${aws_digest:0:19}: already in ECR as $tag"
      continue
    fi
    # Present untagged (a child of an earlier multi-platform copy): tag it, same digest.
    if [ "$DRY_RUN" = "--dry-run" ]; then
      stage "$n/$total WOULD tag $repo@${aws_digest:0:19} as $tag"
      continue
    fi
    image_json=$(aws ecr batch-get-image --repository-name "$repo" --image-ids "imageDigest=$aws_digest" \
      --query 'images[0]' --output json)
    aws ecr put-image --repository-name "$repo" --image-tag "$tag" \
      --image-manifest "$(jq -r .imageManifest <<<"$image_json")" \
      --image-manifest-media-type "$(jq -r .imageManifestMediaType <<<"$image_json")" >/dev/null
    stage "$n/$total $repo@${aws_digest:0:19}: tagged $tag"
    continue
  fi
  if [ "$DRY_RUN" = "--dry-run" ]; then
    stage "$n/$total WOULD copy $ref -> $REGISTRY/$repo:$tag"
    continue
  fi
  stage "$n/$total copying $image@${aws_digest:0:19} (linux/amd64) -> $repo:$tag"
  skopeo copy --preserve-digests --retry-times 3 \
    "docker://$ref" "docker://$REGISTRY/$repo:$tag"
  aws ecr describe-images --repository-name "$repo" --image-ids "imageDigest=$aws_digest" >/dev/null || {
    echo "[mirror] FAILED: $repo does not hold $aws_digest after the copy" >&2
    exit 1
  }
done
stage "done: $total images checked"
