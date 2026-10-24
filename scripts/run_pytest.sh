#!/usr/bin/env bash
set -euo pipefail

if [[ -d tests ]] && find tests -type f \( -name 'test_*.py' -o -name '*_test.py' \) -print -quit | grep -q .; then
  uv run --no-sync pytest -q
else
  echo "No Python tests yet; pytest skipped."
fi
