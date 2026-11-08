from __future__ import annotations

import os
import threading
import uuid
from collections.abc import Iterator
from concurrent.futures import ThreadPoolExecutor
from decimal import Decimal

import pytest
import sqlalchemy as sa

from engine.config import normalize_database_url
from engine.db import RunRepository, create_engine, upgrade_schema
from engine.proposal_budget import BudgetError, ProposalBudget, Rate, ReviewBudget

URL = os.environ.get("TEST_DATABASE_URL", "")
RATE = Rate(Decimal("1"), Decimal("2"), "test-only rates")

if not URL and os.environ.get("REQUIRE_DB_TESTS") == "1":
    raise RuntimeError("REQUIRE_DB_TESTS=1 but TEST_DATABASE_URL is not set")


@pytest.fixture
def db() -> Iterator[sa.Engine]:
    if not URL:
        pytest.skip("TEST_DATABASE_URL not set")
    engine = create_engine(normalize_database_url(URL))
    upgrade_schema(engine)
    yield engine
    engine.dispose()


@pytest.fixture
def run_id(db: sa.Engine) -> Iterator[uuid.UUID]:
    run, _ = RunRepository(db).create_run(
        trigger_type="test", trigger_ref="synthetic", idempotency_key=str(uuid.uuid4())
    )
    yield run.id
    with db.begin() as conn:
        conn.execute(sa.text("DELETE FROM proposal_calls WHERE run_id=:run"), {"run": run.id})
        conn.execute(sa.text("DELETE FROM proposal_budget WHERE run_id=:run"), {"run": run.id})
        conn.execute(sa.text("DELETE FROM run_events WHERE run_id=:run"), {"run": run.id})
        conn.execute(sa.text("DELETE FROM runs WHERE id=:run"), {"run": run.id})


def test_proposal_budget_concurrent_reservations(db: sa.Engine, run_id: uuid.UUID) -> None:
    budget = ProposalBudget(db)
    barrier = threading.Barrier(8)
    results: list[str] = []
    lock = threading.Lock()

    def worker() -> None:
        barrier.wait()
        try:
            budget.reserve(
                run_id,
                uuid.uuid4(),
                limit=Decimal("0.003"),
                input_limit=1000,
                output_limit=1000,
                rate=RATE,
            )
            result = "reserved"
        except BudgetError:
            result = "blocked"
        with lock:
            results.append(result)

    threads = [threading.Thread(target=worker) for _ in range(8)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert results.count("reserved") == 1
    assert results.count("blocked") == 7


def test_proposal_records_usage_and_failures(db: sa.Engine, run_id: uuid.UUID) -> None:
    budget = ProposalBudget(db)
    call = uuid.uuid4()
    budget.reserve(run_id, call, limit=Decimal("1"), input_limit=1000, output_limit=1000, rate=RATE)
    assert not budget.settle(call, input_tokens=1001, output_tokens=1000, rate=RATE)
    with db.connect() as conn:
        row = (
            conn.execute(sa.text("SELECT * FROM proposal_calls WHERE id=:id"), {"id": call})
            .mappings()
            .one()
        )
    assert row["input_tokens"] == 1001
    assert row["actual_usd"] == Decimal("0.003001")
    assert row["failure_reason"] == "reservation_exceeded"
    with pytest.raises(BudgetError):
        budget.settle(call, input_tokens=0, output_tokens=0, rate=RATE)


def test_proposal_unknown_usage_fails_closed(db: sa.Engine, run_id: uuid.UUID) -> None:
    budget = ProposalBudget(db)
    call = uuid.uuid4()
    budget.reserve(
        run_id, call, limit=Decimal("0.003"), input_limit=1000, output_limit=1000, rate=RATE
    )
    budget.mark_unknown(call, "missing_usage")
    with pytest.raises(BudgetError, match="exhausted"):
        budget.reserve(
            run_id,
            uuid.uuid4(),
            limit=Decimal("0.003"),
            input_limit=1000,
            output_limit=1000,
            rate=RATE,
        )
    with db.connect() as conn:
        row = conn.execute(
            sa.text("SELECT status, actual_usd FROM proposal_calls WHERE id=:id"), {"id": call}
        ).one()
    assert row.status == "unknown" and row.actual_usd is None


def test_live_review_aggregate_budget(db: sa.Engine) -> None:
    repo = RunRepository(db)
    budget = ProposalBudget(db)
    review = ReviewBudget(str(uuid.uuid4()), Decimal("0.50"))
    runs = [
        repo.create_run(
            trigger_type="test", trigger_ref="synthetic", idempotency_key=str(uuid.uuid4())
        )[0].id
        for _ in range(6)
    ]

    def reserve(run_id: uuid.UUID) -> uuid.UUID | None:
        call_id = uuid.uuid4()
        try:
            budget.reserve(
                run_id,
                call_id,
                limit=Decimal("1"),
                input_limit=50_000,
                output_limit=100_000,
                rate=RATE,
                review=review,
            )
        except BudgetError:
            return None
        return call_id

    try:
        with ThreadPoolExecutor(max_workers=6) as pool:
            results = list(pool.map(reserve, runs))
        accepted = [call for call in results if call is not None]
        assert len(accepted) == 2
        budget.mark_unknown(accepted[0], "unknown")
        assert budget.settle(accepted[1], input_tokens=1, output_tokens=1, rate=RATE)
        assert reserve(runs[0]) is None
        with pytest.raises(BudgetError, match="cannot change"):
            budget.reserve(
                runs[0],
                uuid.uuid4(),
                limit=Decimal("1"),
                input_limit=50_000,
                output_limit=100_000,
                rate=RATE,
                review=ReviewBudget(review.review_id, Decimal("1")),
            )
        with db.connect() as conn:
            count = conn.execute(
                sa.text("SELECT count(*) FROM proposal_calls WHERE review_id=:id"),
                {"id": review.review_id},
            ).scalar_one()
        assert count == 2
    finally:
        with db.begin() as conn:
            for run_id in runs:
                for table in ("proposal_calls", "proposal_budget", "run_events"):
                    conn.execute(sa.text(f"DELETE FROM {table} WHERE run_id=:run"), {"run": run_id})
                conn.execute(sa.text("DELETE FROM runs WHERE id=:run"), {"run": run_id})
            conn.execute(
                sa.text("DELETE FROM proposal_review_budgets WHERE id=:id"),
                {"id": review.review_id},
            )
