from __future__ import annotations

import os
import threading
from collections.abc import Iterator
from datetime import UTC, datetime
from decimal import Decimal

import pytest
import sqlalchemy as sa

from engine.config import normalize_database_url
from engine.db import create_engine, upgrade_schema
from engine.gate_ledger import PostgresGateLedger
from engine.policy import Gate, GateConfig, GateRejected, OfferProposal, Reason

URL = os.environ.get("TEST_DATABASE_URL", "")

if not URL and os.environ.get("REQUIRE_DB_TESTS") == "1":
    raise RuntimeError("REQUIRE_DB_TESTS=1 but TEST_DATABASE_URL is not set.")

pytestmark = pytest.mark.skipif(
    not URL, reason="TEST_DATABASE_URL not set; start the engine DB (see docs/runbooks)."
)

NOW = datetime(2026, 11, 4, 12, 0, tzinfo=UTC)


@pytest.fixture
def engine() -> Iterator[sa.Engine]:
    eng = create_engine(normalize_database_url(URL))
    upgrade_schema(eng)
    with eng.begin() as conn:
        conn.execute(sa.text("DELETE FROM gate_coupon_reservations"))
        conn.execute(sa.text("DELETE FROM gate_budget_days"))
    yield eng
    eng.dispose()


def _config(budget: str) -> GateConfig:
    return GateConfig.from_env(
        {
            "GATE_TIMEZONE": "Asia/Riyadh",
            "GATE_CURRENCY": "USD",
            "GATE_DAILY_BUDGET": budget,
            "GATE_CATEGORY_MIN_MARGINS": '{"decor": "0.20"}',
        }
    )


def _proposal(n: int, cart: str | None = None) -> OfferProposal:
    return OfferProposal(
        run_id=f"run-{n}",
        customer_id=f"cust-{n}",
        cart_id=cart or f"cart-{n}",
        cart_owner_id=f"cust-{n}",
        category="decor",
        currency="USD",
        eligible_total=Decimal("100"),
        eligible_unit_cost_total=Decimal("40"),
        proposed_discount_percent=Decimal("10"),
    )


def test_daily_discount_budget_is_not_exceeded_under_concurrency(engine: sa.Engine) -> None:
    gate = Gate(_config("50"), PostgresGateLedger(engine))  # each coupon costs 10.00
    results: list[str] = []
    lock = threading.Lock()
    barrier = threading.Barrier(12)

    def worker(n: int) -> None:
        barrier.wait()
        try:
            gate.issue_approval(_proposal(n), now=NOW)
            outcome = "approved"
        except GateRejected as error:
            outcome = error.reasons[0].value
        with lock:
            results.append(outcome)

    threads = [threading.Thread(target=worker, args=(n,)) for n in range(12)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert results.count("approved") == 5
    assert results.count(Reason.DAILY_BUDGET_EXHAUSTED.value) == 7
    with engine.connect() as conn:
        reserved = conn.execute(sa.text("SELECT reserved FROM gate_budget_days")).scalar_one()
        coupons = conn.execute(
            sa.text("SELECT count(*) FROM gate_coupon_reservations")
        ).scalar_one()
    assert reserved == Decimal("50.00")
    assert coupons == 5  # rejected reservations leave no coupon row


def test_db_coupon_limit_and_idempotent_retry(engine: sa.Engine) -> None:
    gate = Gate(_config("500"), PostgresGateLedger(engine))
    first = _proposal(1)
    gate.issue_approval(first, now=NOW)
    gate.issue_approval(first, now=NOW)  # same run: no second reservation
    other_run = OfferProposal(**{**first.__dict__, "run_id": "run-other"})
    with pytest.raises(GateRejected) as error:
        gate.issue_approval(other_run, now=NOW)
    assert error.value.reasons == (Reason.COUPON_LIMIT_REACHED,)
    with engine.connect() as conn:
        reserved = conn.execute(sa.text("SELECT reserved FROM gate_budget_days")).scalar_one()
    assert reserved == Decimal("10.00")


def test_db_same_run_cannot_reuse_a_different_reservation(engine: sa.Engine) -> None:
    ledger = PostgresGateLedger(engine)
    gate = Gate(_config("500"), ledger)
    first = _proposal(1)
    gate.issue_approval(first, now=NOW)
    changed = OfferProposal(**{**first.__dict__, "proposed_discount_percent": Decimal("12")})
    with pytest.raises(GateRejected) as error:
        gate.issue_approval(changed, now=NOW, human_approved=True)
    assert Reason.INVALID_PROPOSAL in error.value.reasons
    assert (
        ledger.reserve(
            day="2026-11-04",
            customer_id=first.customer_id,
            cart_id=first.cart_id,
            run_id=first.run_id,
            amount=Decimal("12"),
            daily_budget=Decimal("500"),
        )
        is Reason.INVALID_PROPOSAL
    )
    with engine.connect() as conn:
        reserved = conn.execute(sa.text("SELECT reserved FROM gate_budget_days")).scalar_one()
    assert reserved == Decimal("10.00")
