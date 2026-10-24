#!/usr/bin/env bash
set -euo pipefail

mapfile -d '' python_files < <(find . -path './.git' -prune -o -path './.venv' -prune -o -type f -name '*.py' -print0)
if ((${#python_files[@]} > 0)); then
  uv run --no-sync mypy "${python_files[@]}"
else
  echo "No Python source files yet; mypy skipped."
fi
