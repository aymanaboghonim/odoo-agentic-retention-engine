"""Read-only evidence lookups for held effects (#131, #132; REC-1, REC-3) and the one Odoo
change reconciliation makes: deactivating a proven orphan coupon (REC-10).

- coupon: Odoo JSON-2 `loyalty.card` `search_read` by `coupon_code_for_run` (archived cards
  included), `present` or `absent`;
- Mailpit message: Mailpit search by the stored Message-ID, compared exactly, because the
  search matches substrings (`F-101`); no stored ID (runs before #131) is `unknown`;
- Telegram message: `attestation_required` (the Bot API cannot read a message back).

Any lookup error is `unknown`, never `absent` (REC-3).
"""

from __future__ import annotations

import ipaddress
import json
import socket
import uuid
from collections.abc import Callable, Mapping
from typing import Any
from urllib.parse import urlencode, urlsplit
from urllib.request import urlopen

from engine.coupon_code import coupon_code_for_run
from engine.odoo import CouponRef, JsonTwoClient, OdooApiError
from engine.reconcile import Evidence, HeldEffect

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


class CouponDeactivator:
    """REC-10: archive the coupon of a run whose message is proven `not_sent`."""

    def __init__(self, client: JsonTwoClient) -> None:
        self._client = client

    def deactivate(self, run_id: uuid.UUID) -> None:
        code = coupon_code_for_run(str(run_id))
        cards = self._client.call(
            "loyalty.card",
            "search_read",
            {
                "domain": [["code", "=", code], ["active", "in", [True, False]]],
                "fields": ["id", "code", "program_id"],
                "limit": 2,
            },
        )
        if (
            not isinstance(cards, list)
            or len(cards) != 1
            or not isinstance(cards[0], dict)
            or cards[0].get("code") != code
        ):
            raise OdooApiError("orphan coupon is not a unique exact card")
        program = cards[0].get("program_id")
        if not isinstance(program, list) or not program or type(program[0]) is not int:
            raise OdooApiError("orphan coupon has no program")
        card_id = cards[0].get("id")
        self._client.archive_coupon(
            CouponRef(
                program_id=program[0], code=code, card_id=card_id if type(card_id) is int else None
            )
        )
