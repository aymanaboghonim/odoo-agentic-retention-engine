#!/usr/bin/env bash
set -euo pipefail

root="$(git rev-parse --show-toplevel)"
docker run --rm --volume "$root:/repo:ro" --workdir /repo ghcr.io/gitleaks/gitleaks@sha256:c00b6bd0aeb3071cbcb79009cb16a60dd9e0a7c60e2be9ab65d25e6bc8abbb7f git --redact --no-banner --log-opts=--all
