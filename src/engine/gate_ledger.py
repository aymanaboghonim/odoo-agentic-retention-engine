from __future__ import annotations

import threading
from datetime import UTC, date, datetime
from decimal import Decimal

import sqlalchemy as sa
from sqlalchemy import Engine

from engine.policy import Reason


class _BudgetExhausted(Exception):
    pass


class PostgresGateLedger:
    """Atomic daily budget and one-coupon-per-cart-per-day reservation (SG-5, SG-6)."""

    def __init__(self, engine: Engine) -> None:
        self._engine = engine

    def check(
        self, *, day: str, customer_id: str, cart_id: str, run_id: str, amount: Decimal
    ) -> Reason | None:
        """Non-binding preview used while a proposal waits; reserve() is the real check."""
        with self._engine.connect() as conn:
            existing = conn.execute(
                sa.text(
                    "SELECT run_id FROM gate_coupon_reservations "
                    "WHERE day = :day AND customer_id = :c AND cart_id = :k"
                ),
                {"day": date.fromisoformat(day), "c": customer_id, "k": cart_id},
            ).first()
            if existing is not None:
                return None if existing.run_id == run_id else Reason.COUPON_LIMIT_REACHED
        return None

    def reserve(
        self,
        *,
        day: str,
        customer_id: str,
        cart_id: str,
        run_id: str,
        amount: Decimal,
        daily_budget: Decimal,
    ) -> Reason | None:
        if amount > daily_budget:
            return Reason.DAILY_BUDGET_EXHAUSTED
        day_value = date.fromisoformat(day)
        try:
            return self._reserve(day_value, customer_id, cart_id, run_id, amount, daily_budget)
        except _BudgetExhausted:
            return Reason.DAILY_BUDGET_EXHAUSTED

    def _reserve(
        self,
        day_value: date,
        customer_id: str,
        cart_id: str,
        run_id: str,
        amount: Decimal,
        daily_budget: Decimal,
    ) -> Reason | None:
        with self._engine.begin() as conn:
            inserted = conn.execute(
                sa.text(
                    "INSERT INTO gate_coupon_reservations "
                    "(day, customer_id, cart_id, run_id, amount, created_at) "
                    "VALUES (:day, :c, :k, :run, :amount, :now) "
                    "ON CONFLICT ON CONSTRAINT uq_gate_coupon_cart_day DO NOTHING RETURNING id"
                ),
                {
                    "day": day_value,
                    "c": customer_id,
                    "k": cart_id,
                    "run": run_id,
                    "amount": amount,
                    "now": datetime.now(UTC),
                },
            ).first()
            if inserted is None:
                owner = conn.execute(
                    sa.text(
                        "SELECT run_id FROM gate_coupon_reservations "
                        "WHERE day = :day AND customer_id = :c AND cart_id = :k"
                    ),
                    {"day": day_value, "c": customer_id, "k": cart_id},
                ).one()
                return None if owner.run_id == run_id else Reason.COUPON_LIMIT_REACHED
            # The conditional upsert takes a row lock, so concurrent reservations serialize.
            updated = conn.execute(
                sa.text(
                    "INSERT INTO gate_budget_days (day, reserved) VALUES (:day, :amount) "
                    "ON CONFLICT (day) DO UPDATE "
                    "SET reserved = gate_budget_days.reserved + :amount "
                    "WHERE gate_budget_days.reserved + :amount <= :budget RETURNING reserved"
                ),
                {"day": day_value, "amount": amount, "budget": daily_budget},
            ).first()
            if updated is None:
                raise _BudgetExhausted  # rolls back the coupon row above
        return None


class InMemoryGateLedger:
    """Same rules without a database, for unit tests."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._coupons: dict[tuple[str, str, str], str] = {}
        self._reserved: dict[str, Decimal] = {}

    def check(
        self, *, day: str, customer_id: str, cart_id: str, run_id: str, amount: Decimal
    ) -> Reason | None:
        owner = self._coupons.get((day, customer_id, cart_id))
        return Reason.COUPON_LIMIT_REACHED if owner not in (None, run_id) else None

    def reserve(
        self,
        *,
        day: str,
        customer_id: str,
        cart_id: str,
        run_id: str,
        amount: Decimal,
        daily_budget: Decimal,
    ) -> Reason | None:
        with self._lock:
            owner = self._coupons.get((day, customer_id, cart_id))
            if owner is not None:
                return None if owner == run_id else Reason.COUPON_LIMIT_REACHED
            total = self._reserved.get(day, Decimal(0)) + amount
            if total > daily_budget:
                return Reason.DAILY_BUDGET_EXHAUSTED
            self._coupons[(day, customer_id, cart_id)] = run_id
            self._reserved[day] = total
        return None
