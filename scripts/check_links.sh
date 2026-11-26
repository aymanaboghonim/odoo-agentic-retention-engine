#!/usr/bin/env bash
set -euo pipefail

root="$(git rev-parse --show-toplevel)"
inputs=()
while IFS= read -r -d '' file; do
	inputs+=("/repo/${file#"$root"/}")
done < <(find "$root" \( -path "$root/.git" -o -path "$root/.venv" -o -path "$root/blueprint-draft.md" -o -name .terraform \) -prune -o -type f -name '*.md' -print0)

if ((${#inputs[@]} == 0)); then
	echo "No Markdown files found; link check skipped."
	exit 0
fi

docker run --rm --volume "$root:/repo:ro" lycheeverse/lychee@sha256:453152c43276f67a344f0973d130bc0e87c330a8dfa33daaddb93d877f4f39ff --offline --no-progress "${inputs[@]}"
