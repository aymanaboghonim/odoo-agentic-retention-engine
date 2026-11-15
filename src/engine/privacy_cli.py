"""Operator CLI for Q12 retention sweep, right-to-erasure, and consent records.

Output names stages and counts only; it never prints names, emails, phones, or peppers.
"""

from __future__ import annotations

import argparse
import os
import sys
import time
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime

import sqlalchemy as sa

from engine.config import EngineConfig
from engine.db import create_engine
from engine.privacy import PrivacyError, PrivacyRepository, PrivacySettings


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Q12 privacy operations on the Engine DB")
    parser.add_argument(
        "--preflight-only",
        action="store_true",
        help="validate settings, database access, and schema; make no change",
    )
    commands = parser.add_subparsers(dest="command", required=True)
    sweep = commands.add_parser("sweep", help="delete expired terminal runs")
    sweep.add_argument("--batch-size", type=int, default=500)
    sweep.add_argument("--max-batches", type=int, default=100)
    erase = commands.add_parser("erase", help="erase one customer and write a tombstone")
    erase.add_argument("--customer-id", required=True)
    erase.add_argument("--confirm-erasure", action="store_true")
    consent = commands.add_parser("consent", help="record explicit consent")
    consent.add_argument("action", choices=["grant", "revoke", "show"])
    consent.add_argument("--customer-id", required=True)
    consent.add_argument("--channel", choices=["email", "telegram"], required=True)
    consent.add_argument("--source", default="", help="non-contact evidence reference")
    consent.add_argument(
        "--fresh-after-erasure",
        action="store_true",
        help="explicit fresh consent that lifts an erasure tombstone",
    )
    return parser


def _preflight(engine: sa.Engine) -> None:
    with engine.connect() as conn:
        version = conn.execute(sa.text("SELECT version_num FROM alembic_version")).scalar()
        tables = conn.execute(
            sa.text(
                "SELECT count(*) FROM information_schema.tables "
                "WHERE table_name IN ('customer_consents', 'erasure_requests')"
            )
        ).scalar_one()
    if version is None or int(version) < 11 or tables != 2:
        raise RuntimeError("Engine DB schema is older than migration 0011; run upgrade first")


def main(argv: Sequence[str] | None = None, env: Mapping[str, str] | None = None) -> int:
    args = _parser().parse_args(argv)
    values = os.environ if env is None else env
    started = time.monotonic()
    print(f"stage=preflight command={args.command} status=started", flush=True)
    settings = PrivacySettings.from_env(values)
    engine = create_engine(EngineConfig.from_env(values).database_url)
    try:
        _preflight(engine)
        print(
            "stage=preflight status=passed; checked settings, DB access, schema 0011. "
            "Not checked: Odoo state or other workers.",
            flush=True,
        )
        if args.preflight_only:
            return 0
        repo = PrivacyRepository(engine, settings)
        if args.command == "sweep":
            if not 1 <= args.batch_size <= 10_000 or not 1 <= args.max_batches <= 10_000:
                raise ValueError("batch size and batch count must be bounded")
            total = 0
            for batch in range(1, args.max_batches + 1):
                deleted = repo.sweep_expired(now=datetime.now(UTC), limit=args.batch_size)
                total += deleted
                print(
                    f"stage=sweep batch={batch} deleted={deleted} total={total} "
                    f"elapsed_s={time.monotonic() - started:.0f}",
                    flush=True,
                )
                if deleted < args.batch_size:
                    break
            print(f"stage=sweep status=complete runs_deleted={total}", flush=True)
        elif args.command == "erase":
            if not args.confirm_erasure:
                raise ValueError("--confirm-erasure is required; erasure cannot be undone")
            deleted = repo.erase_customer(args.customer_id)
            print(
                f"stage=erase status=complete runs_deleted={deleted} tombstone=written", flush=True
            )
        elif args.action == "show":
            opted = repo.has_opt_in(args.customer_id, args.channel)
            erased = repo.is_erased(args.customer_id)
            print(f"stage=consent opted_in={opted} erased={erased}", flush=True)
        else:
            if not args.source.strip():
                raise ValueError("--source is required to record consent evidence")
            repo.set_consent(
                args.customer_id,
                args.channel,
                "opted_in" if args.action == "grant" else "opted_out",
                source=args.source,
                fresh_after_erasure=args.fresh_after_erasure,
            )
            print(f"stage=consent status=complete action={args.action}", flush=True)
        return 0
    except (PrivacyError, ValueError, RuntimeError) as error:
        print(f"stage={args.command} status=failed reason={error}", file=sys.stderr, flush=True)
        return 1
    finally:
        engine.dispose()


if __name__ == "__main__":
    raise SystemExit(main())
