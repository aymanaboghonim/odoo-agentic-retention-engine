#!/usr/bin/env bash
set -euo pipefail

command_name="${1:?Expected check or format}"
mapfile -d '' python_files < <(find . -path './.git' -prune -o -path './.venv' -prune -o -type f -name '*.py' -print0)
if ((${#python_files[@]} == 0)); then
  echo "No Python source files yet; Ruff $command_name skipped."
  exit 0
fi

case "$command_name" in
  check) uv run --no-sync ruff check "${python_files[@]}" ;;
  format) uv run --no-sync ruff format --check "${python_files[@]}" ;;
  *) echo "Unknown Ruff command: $command_name" >&2; exit 2 ;;
esac
