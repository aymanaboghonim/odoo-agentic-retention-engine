#!/usr/bin/env bash
set -euo pipefail

root="$(git rev-parse --show-toplevel)"
mapfile -t workflows < <(find "$root/.github/workflows" -maxdepth 1 -type f \( -name '*.yml' -o -name '*.yaml' \) -printf '/repo/.github/workflows/%f\n' | sort)
if ((${#workflows[@]} == 0)); then
  echo "No GitHub Actions workflows found; actionlint skipped."
  exit 0
fi

docker run --rm --volume "$root:/repo:ro" --workdir /repo rhysd/actionlint@sha256:b1934ee5f1c509618f2508e6eb47ee0d3520686341fec936f3b79331f9315667 "${workflows[@]}"
