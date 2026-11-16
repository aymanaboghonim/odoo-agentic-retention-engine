"""Q12 privacy primitives: PII tokens, consent, allowlist, retention, and erasure.

Spec: docs/specs/privacy-consent-retention.md. Raw names, emails, and phones are never
written to the Engine DB. Tokens are rehydrated only from the live Odoo cart immediately
before dispatch.
"""

from __future__ import annotations

import hashlib
import hmac
import re
import uuid
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any, Literal, Protocol

import sqlalchemy as sa
from sqlalchemy import Connection, Engine

from engine.db import customer_consents, delete_runs_in_transaction, erasure_requests, runs
from engine.runs import TERMINAL_STATES

ConsentChannel = Literal["email", "telegram"]
CONSENT_CHANNELS: frozenset[str] = frozenset({"email", "telegram"})
PII_TOKEN = re.compile(r"\{\{PII_(NAME|EMAIL|PHONE)_([1-9][0-9]{0,2})\}\}")
# Anything that looks like a token, including malformed ones, is checked before dispatch.
TOKEN_LIKE = re.compile(r"\{\{[^{}]{0,64}\}\}")
_EMAIL_ADDRESS = re.compile(r"[^@\s,;<>\"']+@([a-z0-9-]+(?:\.[a-z0-9-]+)+)")
_MIN_NAME_PART = 3
_MIN_PHONE_DIGITS = 7
_MIN_PEPPER_LENGTH = 32
_FIELDS = {"NAME": "name", "EMAIL": "email", "PHONE": "phone"}


class PrivacyError(Exception):
    """A privacy control refused the action. Messages never contain PII."""


@dataclass(frozen=True)
class PrivacySettings:
    retention_days: int
    tombstone_pepper: str = field(repr=False)

    def __post_init__(self) -> None:
        if (
            not isinstance(self.retention_days, int)
            or isinstance(self.retention_days, bool)
            or self.retention_days < 1
        ):
            raise ValueError("PRIVACY_RETENTION_DAYS must be an integer of at least 1")
        if len(self.tombstone_pepper) < _MIN_PEPPER_LENGTH:
            raise ValueError(
                f"PRIVACY_TOMBSTONE_PEPPER must have at least {_MIN_PEPPER_LENGTH} characters"
            )

    @classmethod
    def from_env(cls, env: Mapping[str, str]) -> PrivacySettings:
        try:
            days = int(env["PRIVACY_RETENTION_DAYS"].strip())
        except (KeyError, ValueError) as error:
            raise ValueError("PRIVACY_RETENTION_DAYS must be set to an integer") from error
        return cls(days, env.get("PRIVACY_TOMBSTONE_PEPPER", ""))


def subject_hash(pepper: str, customer_id: str) -> str:
    """Non-PII tombstone key: HMAC-SHA256(pepper, str(customer_id))."""
    return hmac.new(pepper.encode(), customer_id.encode(), hashlib.sha256).hexdigest()


def consent_channel(execution_channel: str) -> ConsentChannel:
    if execution_channel == "mailpit":
        return "email"
    if execution_channel == "telegram":
        return "telegram"
    raise PrivacyError("unsupported consent channel")


def _customer_key(customer_id: str | int) -> str:
    value = str(customer_id)
    if not value.isdigit() or value.startswith("0"):
        raise PrivacyError("customer ID must be a positive Odoo partner ID")
    return value


class ContactSource(Protocol):
    @property
    def partner_id(self) -> int: ...
    @property
    def partner_name(self) -> str: ...
    @property
    def partner_email(self) -> str: ...
    @property
    def partner_phone(self) -> str: ...


@dataclass(frozen=True)
class _Entry:
    token: str
    value: str
    field: str
    part: int | None


class PiiTokenizer:
    """Per-run reversible tokens. Raw values exist only in this in-memory object."""

    def __init__(self, *, customer_id: int, name: str, email: str, phone: str) -> None:
        self.customer_id = _customer_key(customer_id)
        entries: list[_Entry] = []
        full = " ".join(name.split())
        if full:
            entries.append(_Entry("{{PII_NAME_1}}", full, "name", None))
            seen = {full.casefold()}
            index = 2
            for position, part in enumerate(full.split(" ")):
                if len(part) >= _MIN_NAME_PART and part.casefold() not in seen:
                    seen.add(part.casefold())
                    entries.append(_Entry(f"{{{{PII_NAME_{index}}}}}", part, "name", position))
                    index += 1
        if email.strip():
            entries.append(_Entry("{{PII_EMAIL_1}}", email.strip(), "email", None))
        if len(re.sub(r"\D", "", phone)) >= _MIN_PHONE_DIGITS:
            entries.append(_Entry("{{PII_PHONE_1}}", phone.strip(), "phone", None))
        self._entries = tuple(entries)

    @classmethod
    def for_cart(cls, cart: ContactSource) -> PiiTokenizer:
        return cls(
            customer_id=cart.partner_id,
            name=cart.partner_name,
            email=cart.partner_email,
            phone=cart.partner_phone,
        )

    def tokenize(self, text: str, *, contact_check: re.Pattern[str] | None = None) -> str:
        """Mask untrusted input: neutralize smuggled token syntax, then replace identities.

        `contact_check` runs after this customer's email and phone are masked but before
        name masking, so a name part (for example a domain word) cannot hide another
        contact identifier from the fail-closed check.
        """
        text = self._mask(TOKEN_LIKE.sub("[token]", text), {"email", "phone"})
        if contact_check is not None and contact_check.search(text):
            raise PrivacyError("contact identifier in model input")
        return self._mask(text, {"name"})

    def mask(self, text: str) -> str:
        """Replace raw identities with tokens; existing tokens are left as they are."""
        return self._mask(self._mask(text, {"email", "phone"}), {"name"})

    def _mask(self, text: str, fields: set[str]) -> str:
        for entry in sorted(self._entries, key=lambda item: _order(item)):
            if entry.field in fields:
                text = _pattern(entry).sub(entry.token, text)
        return text

    def token_map(self) -> dict[str, Any]:
        """Persistable map: token -> field reference. Contains no raw values."""
        return {
            "customer_id": self.customer_id,
            "tokens": {
                entry.token: {"field": entry.field, "part": entry.part} for entry in self._entries
            },
        }

    def issued(self) -> frozenset[str]:
        return frozenset(entry.token for entry in self._entries)


def _order(entry: _Entry) -> tuple[int, int]:
    # Email and phone first (they can contain name parts), then longest name first.
    rank = {"email": 0, "phone": 1, "name": 2}[entry.field]
    return rank, -len(entry.value)


def _pattern(entry: _Entry) -> re.Pattern[str]:
    if entry.field == "phone":
        digits = re.sub(r"\D", "", entry.value)
        lead = r"(?:\+|00)?" if entry.value.lstrip().startswith(("+", "00")) else ""
        if lead:
            digits = digits.removeprefix("00")
        body = r"[\s().-]*".join(re.escape(digit) for digit in digits)
        return re.compile(rf"(?<!\d){lead}{body}(?!\d)")
    escaped = re.escape(entry.value)
    if entry.field == "name":
        escaped = r"\s+".join(re.escape(word) for word in entry.value.split(" "))
        return re.compile(rf"(?<!\w){escaped}(?!\w)", re.IGNORECASE)
    return re.compile(escaped, re.IGNORECASE)


def rehydrate(text: str, token_map: Mapping[str, Any], cart: ContactSource) -> str:
    """Restore tokens from the live Odoo cart of the same customer, or fail closed."""
    if not isinstance(token_map, Mapping) or not isinstance(token_map.get("tokens"), Mapping):
        raise PrivacyError("PII token map is missing")
    if token_map.get("customer_id") != _customer_key(cart.partner_id):
        raise PrivacyError("PII tokens belong to another customer")
    tokens: Mapping[str, Any] = token_map["tokens"]

    def value(match: re.Match[str]) -> str:
        token = match.group(0)
        spec = tokens.get(token)
        parsed = PII_TOKEN.fullmatch(token)
        if parsed is None or not isinstance(spec, Mapping):
            raise PrivacyError("unknown PII token")
        expected = _FIELDS[parsed.group(1)]
        if spec.get("field") != expected:
            raise PrivacyError("PII token map is inconsistent")
        if expected == "email":
            raw = cart.partner_email.strip()
        elif expected == "phone":
            raw = cart.partner_phone.strip()
        else:
            full = " ".join(cart.partner_name.split())
            part = spec.get("part")
            if part is None:
                raw = full
            else:
                words = full.split(" ")
                if type(part) is not int or not 0 <= part < len(words):
                    raise PrivacyError("live customer name no longer matches the token")
                raw = words[part]
        if not raw:
            raise PrivacyError("live customer context has no value for a PII token")
        return raw

    restored = TOKEN_LIKE.sub(value, text)
    return restored


def has_unknown_tokens(text: str, issued: frozenset[str]) -> bool:
    return any(match.group(0) not in issued for match in TOKEN_LIKE.finditer(text))


@dataclass(frozen=True)
class AllowlistGuard:
    """Explicit outbound recipient allowlist. Empty lists block every recipient."""

    domains: frozenset[str] = frozenset()
    emails: frozenset[str] = frozenset()
    telegram_chat_ids: frozenset[str] = frozenset()

    @classmethod
    def from_env(cls, env: Mapping[str, str]) -> AllowlistGuard:
        def values(name: str) -> frozenset[str]:
            return frozenset(
                item.strip().lower() for item in env.get(name, "").split(",") if item.strip()
            )

        domains = values("EXECUTION_ALLOWLIST_DOMAINS")
        if any(domain.startswith(("*", ".", "@")) for domain in domains):
            raise ValueError("allowlist domains must be exact domains without wildcards")
        return cls(
            domains=domains,
            emails=values("EXECUTION_ALLOWLIST_EMAILS"),
            telegram_chat_ids=values("TELEGRAM_ALLOWLIST_CHAT_IDS"),
        )

    def allows(self, channel: ConsentChannel, target: str) -> bool:
        candidate = target.strip().lower()
        if channel == "telegram":
            return bool(candidate) and candidate in self.telegram_chat_ids
        match = _EMAIL_ADDRESS.fullmatch(candidate)
        if match is None:
            return False
        return candidate in self.emails or match.group(1) in self.domains

    def require(self, channel: ConsentChannel, target: str) -> None:
        if not self.allows(channel, target):
            raise PrivacyError("recipient is not in the outbound allowlist")


def is_erased(conn: Connection, settings: PrivacySettings, customer_id: str | int) -> bool:
    """True while an erasure tombstone exists without explicit fresh consent."""
    key = subject_hash(settings.tombstone_pepper, _customer_key(customer_id))
    row = conn.execute(
        sa.select(erasure_requests.c.reconsented_at).where(erasure_requests.c.subject_hash == key)
    ).first()
    return row is not None and row.reconsented_at is None


def has_opt_in(conn: Connection, customer_id: str | int, channel: ConsentChannel) -> bool:
    if channel not in CONSENT_CHANNELS:
        raise PrivacyError("unsupported consent channel")
    status = conn.execute(
        sa.select(customer_consents.c.status).where(
            customer_consents.c.customer_id == _customer_key(customer_id),
            customer_consents.c.channel == channel,
        )
    ).scalar()
    return status == "opted_in"


def has_active_or_recent_run(
    conn: Connection, customer_id: str | int, *, now: datetime, cooldown_days: int
) -> Literal["active_run", "cooldown"] | None:
    key = _customer_key(customer_id)
    owned = sa.or_(
        runs.c.customer_id == key,
        sa.and_(runs.c.customer_id.is_(None), runs.c.trigger_ref.like(f"{key}:%")),
    )
    terminal = [state.value for state in TERMINAL_STATES]
    if conn.execute(
        sa.select(runs.c.id).where(owned, runs.c.state.not_in(terminal)).limit(1)
    ).first():
        return "active_run"
    since = now - timedelta(days=cooldown_days)
    if conn.execute(sa.select(runs.c.id).where(owned, runs.c.created_at >= since).limit(1)).first():
        return "cooldown"
    return None


class PrivacyRepository:
    """Consent, retention, and erasure. Every change runs in one transaction."""

    def __init__(self, engine: Engine, settings: PrivacySettings) -> None:
        self._engine = engine
        self._settings = settings

    def set_consent(
        self,
        customer_id: str | int,
        channel: ConsentChannel,
        status: Literal["opted_in", "opted_out"],
        *,
        source: str,
        fresh_after_erasure: bool = False,
    ) -> None:
        key = _customer_key(customer_id)
        if channel not in CONSENT_CHANNELS or status not in {"opted_in", "opted_out"}:
            raise PrivacyError("invalid consent channel or status")
        if not source.strip() or len(source) > 200 or "@" in source:
            raise PrivacyError("consent source must be a short non-contact reference")
        now = datetime.now(UTC)
        with self._engine.begin() as conn:
            if status == "opted_in" and is_erased(conn, self._settings, key):
                if not fresh_after_erasure:
                    raise PrivacyError("customer was erased; explicit fresh consent is required")
                conn.execute(
                    erasure_requests.update()
                    .where(
                        erasure_requests.c.subject_hash
                        == subject_hash(self._settings.tombstone_pepper, key)
                    )
                    .values(reconsented_at=now)
                )
            conn.execute(
                sa.dialects.postgresql.insert(customer_consents)
                .values(
                    customer_id=key, channel=channel, status=status, source=source, updated_at=now
                )
                .on_conflict_do_update(
                    index_elements=["customer_id", "channel"],
                    set_={"status": status, "source": source, "updated_at": now},
                )
            )

    def has_opt_in(self, customer_id: str | int, channel: ConsentChannel) -> bool:
        with self._engine.connect() as conn:
            return has_opt_in(conn, customer_id, channel)

    def is_erased(self, customer_id: str | int) -> bool:
        with self._engine.connect() as conn:
            return is_erased(conn, self._settings, customer_id)

    def sweep_expired(self, *, now: datetime, limit: int = 500) -> int:
        """Delete terminal runs whose retention has expired. Active runs are never touched."""
        if now.tzinfo is None or not 1 <= limit <= 10_000:
            raise ValueError("sweep time must be timezone-aware and limit must be bounded")
        terminal = [state.value for state in TERMINAL_STATES]
        with self._engine.begin() as conn:
            ids = list(
                conn.execute(
                    sa.select(runs.c.id)
                    .where(
                        runs.c.retention_expires_at.is_not(None),
                        runs.c.retention_expires_at <= now,
                        runs.c.state.in_(terminal),
                    )
                    .order_by(runs.c.retention_expires_at, runs.c.id)
                    .limit(limit)
                    .with_for_update(skip_locked=True)
                ).scalars()
            )
            return delete_runs_in_transaction(conn, ids)

    def erase_customer(self, customer_id: str | int) -> int:
        """Delete every Engine DB row for the customer and write a non-PII tombstone."""
        key = _customer_key(customer_id)
        now = datetime.now(UTC)
        token = subject_hash(self._settings.tombstone_pepper, key)
        with self._engine.begin() as conn:
            ids: list[uuid.UUID] = list(
                conn.execute(
                    sa.select(runs.c.id)
                    .where(
                        sa.or_(
                            runs.c.customer_id == key,
                            sa.and_(
                                runs.c.customer_id.is_(None), runs.c.trigger_ref.like(f"{key}:%")
                            ),
                        )
                    )
                    .with_for_update()
                ).scalars()
            )
            deleted = delete_runs_in_transaction(conn, ids)
            conn.execute(
                sa.text("DELETE FROM gate_coupon_reservations WHERE customer_id = :customer"),
                {"customer": key},
            )
            conn.execute(customer_consents.delete().where(customer_consents.c.customer_id == key))
            conn.execute(
                sa.dialects.postgresql.insert(erasure_requests)
                .values(subject_hash=token, erased_at=now, runs_deleted=deleted)
                .on_conflict_do_update(
                    index_elements=["subject_hash"],
                    set_={
                        "erased_at": now,
                        "runs_deleted": erasure_requests.c.runs_deleted + deleted,
                        "reconsented_at": None,
                    },
                )
            )
        return deleted
