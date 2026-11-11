from __future__ import annotations

from datetime import datetime
from typing import Protocol

import sqlalchemy as sa
from sqlalchemy import Engine

from engine.db import RunRepository
from engine.odoo import AbandonedCart


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
