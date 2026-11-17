#!/usr/bin/env bash
# AWS MVP cleanup (#96, spec INF-8): dry-run unless --apply. See scripts/aws_mvp.py and
# docs/runbooks/aws-mvp.md.
set -euo pipefail
REPO=$(cd "$(dirname "$0")/.." && pwd)
cd "$REPO"
exec uv run --no-sync python -m scripts.aws_mvp cleanup "$@"
