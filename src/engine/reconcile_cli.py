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
import ipaddress
import json
import socket
import sys
import uuid
from collections.abc import Callable, Mapping, Sequence
from datetime import UTC, datetime
from typing import Any
from urllib.parse import urlencode, urlsplit
from urllib.request import urlopen

from engine.config import EngineConfig
from engine.coupon_code import coupon_code_for_run
from engine.db import RunRepository, create_engine
from engine.odoo import JsonTwoClient, OdooSettings
from engine.reconcile import Evidence, HeldEffect, ReconcileSettings

MAILPIT_URL_KEY = "RECONCILE_MAILPIT_API_URL"
TIMEOUT_SECONDS = 10


class CouponLookup:
    """Only `loyalty.card.search_read`; nothing else is ever called (REC-1)."""

    def __init__(self, call: Callable[[str, str, dict[str, object]], object]) -> None:
        self._call = call

    def evidence(self, run_id: uuid.UUID) -> Evidence:
        code = coupon_code_for_run(str(run_id))
        try:
            cards = self._call(
                "loyalty.card",
                "search_read",
                {
                    # Archived cards count: #132 may deactivate a coupon that exists.
                    "domain": [["code", "=", code], ["active", "in", [True, False]]],
                    "fields": ["id", "code"],
                    "limit": 2,
                },
            )
        except Exception:  # noqa: BLE001 - REC-3: an error is unknown, never absent
            return "unknown"
        if not isinstance(cards, list) or not all(isinstance(card, dict) for card in cards):
            return "unknown"
        if not cards:
            return "absent"
        if len(cards) == 1 and cards[0].get("code") == code:
            return "present"
        return "unknown"  # not unique or not the exact code


def mailpit_api_url(env: Mapping[str, str]) -> str:
    """The private Mailpit HTTP API (Compose service), validated like the SMTP host."""
    value = env.get(MAILPIT_URL_KEY, "").strip().rstrip("/")
    parts = urlsplit(value)
    if parts.scheme != "http" or not parts.hostname or parts.path or parts.query:
        raise ValueError(f"{MAILPIT_URL_KEY} must be http://<private host>:<port>")
    try:
        addresses = {
            ipaddress.ip_address(info[4][0])
            for info in socket.getaddrinfo(parts.hostname, parts.port or 80)
        }
    except (OSError, ValueError) as error:
        raise ValueError(f"{MAILPIT_URL_KEY} host does not resolve") from error
    if not addresses or not all(a.is_private or a.is_loopback for a in addresses):
        raise ValueError(f"{MAILPIT_URL_KEY} must resolve to private addresses only")
    return value


class MailpitLookup:
    """Mailpit search by Message-ID; the match must be exact (REC-3)."""

    def __init__(self, base_url: str, *, fetch: Callable[[str], Any] | None = None) -> None:
        self._base = base_url
        self._fetch = fetch or self._get

    @staticmethod
    def _get(url: str) -> Any:
        with urlopen(url, timeout=TIMEOUT_SECONDS) as response:  # noqa: S310 - validated host
            return json.loads(response.read(1_000_000))

    def evidence(self, message_id: str | None) -> Evidence:
        if not message_id:
            return "unknown"  # sent before #131: no stored Message-ID
        bare = message_id.strip().strip("<>")
        query = urlencode({"query": f'message-id:"{bare}"', "limit": 50})
        try:
            result = self._fetch(f"{self._base}/api/v1/search?{query}")
        except Exception:  # noqa: BLE001 - REC-3
            return "unknown"
        messages = result.get("messages") if isinstance(result, dict) else None
        count = result.get("messages_count") if isinstance(result, dict) else None
        if not isinstance(messages, list) or type(count) is not int:
            return "unknown"
        if any(isinstance(m, dict) and m.get("MessageID") == bare for m in messages):
            return "present"
        if count == 0 or count <= len(messages):
            return "absent"  # every substring match was seen and none is exact
        return "unknown"  # more matches than one page: cannot prove absence


def evidence_for(held: HeldEffect, coupons: CouponLookup, mailpit: MailpitLookup) -> Evidence:
    if held.effect == "coupon":
        return coupons.evidence(held.run_id)
    if held.channel == "telegram":
        return "attestation_required"
    return mailpit.evidence(held.message_id)


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
