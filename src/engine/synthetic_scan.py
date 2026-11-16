from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Protocol

import sqlalchemy as sa
from sqlalchemy import Connection, Engine

from engine.db import RunRepository
from engine.odoo import AbandonedCart
from engine.privacy import (
    CONSENT_CHANNELS,
    ConsentChannel,
    PrivacySettings,
    has_active_or_recent_run,
    has_opt_in,
    is_erased,
)


class SyntheticCartReader(Protocol):
    def scan_synthetic_review_carts(
        self, *, now: datetime, after_id: int, limit: int
    ) -> list[AbandonedCart]: ...


class SyntheticCartScanner:
    """Create one durable run per marked fixture, advancing a locked cursor."""

    def __init__(
        self, engine: Engine, reader: SyntheticCartReader, *, instance_id: str, page_size: int
    ) -> None:
        if not instance_id or len(instance_id) > 100 or not 1 <= page_size <= 1000:
            raise ValueError("synthetic scan instance and page size must be valid")
        self._engine = engine
        self._reader = reader
        self._instance_id = instance_id
        self._page_size = page_size

    def scan_page(self, *, now: datetime) -> tuple[int, int]:
        if now.tzinfo is None or now.utcoffset() is None:
            raise ValueError("scan time must be timezone-aware")
        with self._engine.begin() as conn:
            conn.execute(
                sa.text(
                    "INSERT INTO synthetic_scan_cursors (id, after_id) VALUES (:id, 0) "
                    "ON CONFLICT (id) DO NOTHING"
                ),
                {"id": self._instance_id},
            )
            after_id = conn.execute(
                sa.text("SELECT after_id FROM synthetic_scan_cursors WHERE id=:id FOR UPDATE"),
                {"id": self._instance_id},
            ).scalar_one()
            carts = self._reader.scan_synthetic_review_carts(
                now=now, after_id=after_id, limit=self._page_size
            )
            if len(carts) > self._page_size:
                raise ValueError("synthetic reader exceeded the requested page size")
            created = 0
            previous = after_id
            repo = RunRepository(self._engine)
            for cart in carts:
                if cart.order_id <= previous or cart.partner_id <= 0:
                    raise ValueError("synthetic reader returned an invalid or unordered cart")
                previous = cart.order_id
                _, inserted = repo.create_run_in_transaction(
                    conn,
                    trigger_type="synthetic",
                    trigger_ref=f"{cart.partner_id}:{cart.order_id}",
                    idempotency_key=f"synthetic-scan:{self._instance_id}:{cart.order_id}",
                    actor="synthetic-scan",
                )
                created += int(inserted)
            next_id = previous if len(carts) == self._page_size else 0
            conn.execute(
                sa.text("UPDATE synthetic_scan_cursors SET after_id=:after_id WHERE id=:id"),
                {"id": self._instance_id, "after_id": next_id},
            )
        return len(carts), created


class ErpCartReader(Protocol):
    def scan_erp_carts(self, *, after_id: int, limit: int) -> list[AbandonedCart]: ...


SKIP_REASONS = (
    "missing_email",
    "blacklisted",
    "recent",
    "erased",
    "no_consent",
    "active_run",
    "cooldown",
)


@dataclass(frozen=True)
class ErpScanResult:
    scanned: int
    created: int
    skipped: dict[str, int]


class ErpCartScanner:
    """Q12 generalized ERP scan (PR-1, PR-4, PR-5, TM-5).

    Every cart is filtered fail-closed before a run exists: no email, Odoo blacklist (or
    unknown blacklist state), not yet past the website abandoned delay, erasure tombstone,
    no explicit opt-in, an active run, or a run inside the cooldown. Only counts are reported.
    """

    def __init__(
        self,
        engine: Engine,
        reader: ErpCartReader,
        privacy: PrivacySettings,
        *,
        instance_id: str,
        page_size: int,
        channel: ConsentChannel,
        cooldown_days: int,
    ) -> None:
        if not instance_id or len(instance_id) > 96 or not 1 <= page_size <= 1000:
            raise ValueError("ERP scan instance and page size must be valid")
        if channel not in CONSENT_CHANNELS:
            raise ValueError("ERP scan channel must be email or telegram")
        if (
            isinstance(cooldown_days, bool)
            or not isinstance(cooldown_days, int)
            or cooldown_days < 1
        ):
            raise ValueError("CAMPAIGN_COOLDOWN_DAYS must be an integer of at least 1")
        self._engine = engine
        self._reader = reader
        self._privacy = privacy
        self._cursor_id = f"erp:{instance_id}"
        self._instance_id = instance_id
        self._page_size = page_size
        self._channel: ConsentChannel = channel
        self._cooldown_days = cooldown_days

    def scan_page(self, *, now: datetime) -> ErpScanResult:
        if now.tzinfo is None or now.utcoffset() is None:
            raise ValueError("scan time must be timezone-aware")
        skipped = dict.fromkeys(SKIP_REASONS, 0)
        with self._engine.begin() as conn:
            conn.execute(
                sa.text(
                    "INSERT INTO synthetic_scan_cursors (id, after_id) VALUES (:id, 0) "
                    "ON CONFLICT (id) DO NOTHING"
                ),
                {"id": self._cursor_id},
            )
            after_id = conn.execute(
                sa.text("SELECT after_id FROM synthetic_scan_cursors WHERE id=:id FOR UPDATE"),
                {"id": self._cursor_id},
            ).scalar_one()
            carts = self._reader.scan_erp_carts(after_id=after_id, limit=self._page_size)
            if len(carts) > self._page_size:
                raise ValueError("ERP reader exceeded the requested page size")
            created = 0
            previous = after_id
            repo = RunRepository(self._engine)
            for cart in carts:
                if cart.order_id <= previous or cart.partner_id <= 0:
                    raise ValueError("ERP reader returned an invalid or unordered cart")
                previous = cart.order_id
                reason = self._skip_reason(conn, cart, now)
                if reason is not None:
                    skipped[reason] += 1
                    continue
                _, inserted = repo.create_run_in_transaction(
                    conn,
                    trigger_type="erp",
                    trigger_ref=f"{cart.partner_id}:{cart.order_id}",
                    idempotency_key=f"erp-scan:{self._instance_id}:{cart.order_id}",
                    actor="erp-scan",
                    customer_id=str(cart.partner_id),
                    retention_days=self._privacy.retention_days,
                )
                created += int(inserted)
            next_id = previous if len(carts) == self._page_size else 0
            conn.execute(
                sa.text("UPDATE synthetic_scan_cursors SET after_id=:after_id WHERE id=:id"),
                {"id": self._cursor_id, "after_id": next_id},
            )
        return ErpScanResult(len(carts), created, skipped)

    def _skip_reason(self, conn: Connection, cart: AbandonedCart, now: datetime) -> str | None:
        if not cart.partner_email.strip():
            return "missing_email"
        if cart.email_blacklisted is not False:
            return "blacklisted"
        if cart.abandoned_delay_hours is None or cart.age_hours(now) < cart.abandoned_delay_hours:
            return "recent"
        if is_erased(conn, self._privacy, cart.partner_id):
            return "erased"
        if not has_opt_in(conn, cart.partner_id, self._channel):
            return "no_consent"
        return has_active_or_recent_run(
            conn, cart.partner_id, now=now, cooldown_days=self._cooldown_days
        )
