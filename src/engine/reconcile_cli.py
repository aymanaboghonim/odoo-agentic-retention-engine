"""Read-only evidence check for held effects (#131; ADR-0030 decision 1, REC-1, REC-3).

    python -m engine.reconcile_cli check [--limit N]

Runs in the operator container (ADR-0027, `scripts/aws_mvp.py exec`). For every held effect
(`unknown`, or `started` past `RECONCILE_STALE_STARTED_MINUTES`, on an executing run) it
looks up the evidence and prints one JSON line with codes only:

- coupon: Odoo JSON-2 `loyalty.card` `search_read` by `coupon_code_for_run` (archived cards
  included), `present` or `absent`;
- Mailpit message: Mailpit search by the stored Message-ID, compared exactly, because the
  search matches substrings (#131, measured); no stored ID (runs before #131) is `unknown`;
- Telegram message: `attestation_required` (the Bot API cannot read a message back).

Any lookup error is `unknown`, never `absent` (REC-3). It changes nothing: no Odoo write, no
dispatch, no engine DB write (REC-1). Resolution is #132.
"""

from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime

from engine.config import EngineConfig
from engine.db import RunRepository, create_engine
from engine.odoo import JsonTwoClient, OdooSettings
from engine.reconcile import ReconcileSettings
from engine.reconcile_evidence import (
    TIMEOUT_SECONDS,
    CouponLookup,
    MailpitLookup,
    evidence_for,
    mailpit_api_url,
)


def check(
    repo: RunRepository,
    settings: ReconcileSettings,
    coupons: CouponLookup,
    mailpit: MailpitLookup,
    *,
    limit: int,
    now: datetime | None = None,
) -> list[dict[str, object]]:
    now = now or datetime.now(UTC)
    rows = []
    for held in repo.held_effects(stale_before=settings.stale_before(now), limit=limit):
        rows.append(
            {
                "run_id": str(held.run_id),
                **held.public(now),
                "evidence": evidence_for(held, coupons, mailpit),
            }
        )
    return rows


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="python -m engine.reconcile_cli")
    commands = parser.add_subparsers(dest="command", required=True)
    run = commands.add_parser("check", help="read-only evidence for held effects; no change")
    run.add_argument("--limit", type=int, default=100)
    return parser


def main(argv: Sequence[str] | None = None, env: Mapping[str, str] | None = None) -> int:
    import os

    args = _parser().parse_args(argv)
    env = os.environ if env is None else env
    # Preflight: every setting is validated before any lookup (fail closed).
    settings = ReconcileSettings.from_env(env)
    odoo = OdooSettings.from_env(env)
    mailpit = MailpitLookup(mailpit_api_url(env))
    engine = create_engine(EngineConfig.from_env(env).database_url)
    try:
        print("stage=reconcile-check status=started", file=sys.stderr, flush=True)
        rows = check(
            RunRepository(engine),
            settings,
            CouponLookup(JsonTwoClient(odoo, timeout=TIMEOUT_SECONDS).call),
            mailpit,
            limit=args.limit,
        )
        for row in rows:
            print(json.dumps(row, sort_keys=True))
        print(f"stage=reconcile-check status=complete held={len(rows)}", file=sys.stderr)
    finally:
        engine.dispose()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
