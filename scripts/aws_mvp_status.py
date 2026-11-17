"""Read-only billable inventory of the AWS MVP (#96, INF-8). See scripts/aws_mvp.py."""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from scripts.aws_mvp import main  # noqa: E402

if __name__ == "__main__":
    raise SystemExit(main(["status", *sys.argv[1:]]))
