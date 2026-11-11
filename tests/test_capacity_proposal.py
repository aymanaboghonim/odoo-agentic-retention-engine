from __future__ import annotations

import os
import uuid
from collections.abc import Iterator
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime
from decimal import Decimal
from unittest.mock import MagicMock

import pytest
import sqlalchemy as sa

from engine.capacity_proposal import (
    CAPACITY_INITIAL_CLAIM_ID,
    CAPACITY_RECOVERY_CLAIM_ID,
    EligibleFixtureReader,
    capacity_proposal_settings,
    generate_capacity_proposal,
)
from engine.config import normalize_database_url
from engine.db import RunRepository, create_engine, run_events, runs, upgrade_schema
from engine.odoo import AbandonedCart
from engine.proposal_budget import ProposalBudget

ENV = {
    "FULL_STACK_CAPACITY_PROPOSAL": "YES",
    "PROPOSAL_SYNTHETIC_ONLY": "YES",
    "PROPOSAL_BUDGET_USD": "0.10",
    "PROPOSAL_INPUT_TOKEN_LIMIT": "4000",
    "PROPOSAL_OUTPUT_TOKEN_LIMIT": "8192",
    "PROPOSAL_REVIEW_ID": "poc2-68-capacity",
    "PROPOSAL_REVIEW_BUDGET_USD": "1.00",
}
URL = os.environ.get("TEST_DATABASE_URL", "")


def test_capacity_settings_require_explicit_shared_budget() -> None:
    settings = capacity_proposal_settings(ENV)
    assert settings.review_budget is not None
    for key, value in (
        ("FULL_STACK_CAPACITY_PROPOSAL", ""),
        ("PROPOSAL_SYNTHETIC_ONLY", ""),
        ("PROPOSAL_REVIEW_ID", "other"),
        ("PROPOSAL_REVIEW_BUDGET_USD", "2"),
        ("PROPOSAL_BUDGET_USD", "1.01"),
        ("PROPOSAL_BUDGET_USD", "0.000001"),
        ("PROPOSAL_INPUT_TOKEN_LIMIT", "0"),
    ):
        with pytest.raises(ValueError):
            capacity_proposal_settings({**ENV, key: value})


def test_fixture_reader_rechecks_marked_fixture() -> None:
    reader = MagicMock()
    result = EligibleFixtureReader(reader).get_cart(12)
    reader.get_synthetic_review_cart.assert_called_once()
    assert reader.get_synthetic_review_cart.call_args.kwargs["now"].tzinfo is not None
    assert result is reader.get_synthetic_review_cart.return_value


@pytest.fixture
def ready_run() -> Iterator[tuple[sa.Engine, str, uuid.UUID]]:
    if not URL:
        if os.environ.get("REQUIRE_DB_TESTS") == "1":
            pytest.fail("REQUIRE_DB_TESTS=1 but TEST_DATABASE_URL is not set")
        pytest.skip("TEST_DATABASE_URL not set")
    engine = create_engine(normalize_database_url(URL))
    upgrade_schema(engine)
    instance = str(uuid.uuid4())
    run, _ = RunRepository(engine).create_run(
        trigger_type="synthetic",
        trigger_ref="9:1",
        idempotency_key=f"synthetic-scan:{instance}:1",
    )
    yield engine, instance, run.id
    with engine.begin() as conn:
        selected = sa.select(runs.c.id).where(
            runs.c.idempotency_key.like(f"synthetic-scan:{instance}:%")
        )
        conn.execute(
            sa.text(
                "DELETE FROM capacity_proposal_claim WHERE run_id IN "
                "(SELECT id FROM runs WHERE idempotency_key LIKE :key)"
            ),
            {"key": f"synthetic-scan:{instance}:%"},
        )
        conn.execute(
            sa.text(
                "DELETE FROM proposal_calls WHERE run_id IN "
                "(SELECT id FROM runs WHERE idempotency_key LIKE :key)"
            ),
            {"key": f"synthetic-scan:{instance}:%"},
        )
        conn.execute(
            sa.text(
                "DELETE FROM proposal_budget WHERE run_id IN "
                "(SELECT id FROM runs WHERE idempotency_key LIKE :key)"
            ),
            {"key": f"synthetic-scan:{instance}:%"},
        )
        conn.execute(run_events.delete().where(run_events.c.run_id.in_(selected)))
        conn.execute(runs.delete().where(runs.c.id.in_(selected)))
    engine.dispose()


def make_cart(order_id: int = 1) -> AbandonedCart:
    return AbandonedCart(
        order_id,
        f"SO{order_id}",
        9,
        "Synthetic",
        "synthetic@example.test",
        "en_US",
        "USD",
        Decimal("10"),
        datetime.now(UTC),
        (),
    )


def test_capacity_proposal_claim_is_one_shot_under_concurrency(
    ready_run: tuple[sa.Engine, str, uuid.UUID],
) -> None:
    engine, instance, run_id = ready_run
    reader = MagicMock()
    reader.get_synthetic_review_cart.return_value = make_cart()
    generator = MagicMock()
    generator.settings = capacity_proposal_settings(ENV)
    generator.carts = EligibleFixtureReader(reader)

    def attempt(_: int) -> str:
        try:
            generate_capacity_proposal(
                engine, reader, generator, run_id=run_id, instance_id=instance, env=ENV
            )
        except ValueError as error:
            return str(error)
        return "started"

    with ThreadPoolExecutor(max_workers=4) as executor:
        results = list(executor.map(attempt, range(4)))
    assert results.count("started") == 1
    assert all(result == "started" or "already claimed" in result for result in results)
    generator.generate.assert_called_once()


def test_rejected_fixture_or_budget_never_calls_generator(
    ready_run: tuple[sa.Engine, str, uuid.UUID],
) -> None:
    engine, instance, run_id = ready_run
    reader = MagicMock()
    reader.get_synthetic_review_cart.return_value = None
    generator = MagicMock()
    generator.settings = capacity_proposal_settings(ENV)
    generator.carts = EligibleFixtureReader(reader)
    with pytest.raises(ValueError, match="still-eligible"):
        generate_capacity_proposal(
            engine, reader, generator, run_id=run_id, instance_id=instance, env=ENV
        )
    reader.get_synthetic_review_cart.return_value = make_cart(2)
    with pytest.raises(ValueError, match="still-eligible"):
        generate_capacity_proposal(
            engine, reader, generator, run_id=run_id, instance_id=instance, env=ENV
        )
    reader.get_synthetic_review_cart.return_value = make_cart()
    with pytest.raises(ValueError, match="required"):
        generate_capacity_proposal(
            engine,
            reader,
            generator,
            run_id=run_id,
            instance_id=instance,
            env={**ENV, "FULL_STACK_CAPACITY_PROPOSAL": ""},
        )
    generator.carts = MagicMock()
    with pytest.raises(ValueError, match="recheck"):
        generate_capacity_proposal(
            engine, reader, generator, run_id=run_id, instance_id=instance, env=ENV
        )
    generator.generate.assert_not_called()


def test_failed_proposal_attempt_remains_claimed(
    ready_run: tuple[sa.Engine, str, uuid.UUID],
) -> None:
    engine, instance, run_id = ready_run
    reader = MagicMock()
    reader.get_synthetic_review_cart.return_value = make_cart()
    generator = MagicMock()
    generator.settings = capacity_proposal_settings(ENV)
    generator.carts = EligibleFixtureReader(reader)
    generator.generate.side_effect = RuntimeError("uncertain provider result")
    with pytest.raises(RuntimeError, match="uncertain"):
        generate_capacity_proposal(
            engine, reader, generator, run_id=run_id, instance_id=instance, env=ENV
        )
    with pytest.raises(ValueError, match="already claimed"):
        generate_capacity_proposal(
            engine, reader, generator, run_id=run_id, instance_id=instance, env=ENV
        )
    generator.generate.assert_called_once()


def test_recovery_claim_requires_one_settled_rejected_initial_attempt(
    ready_run: tuple[sa.Engine, str, uuid.UUID],
) -> None:
    engine, instance, initial_run_id = ready_run
    reader = MagicMock()
    reader.get_synthetic_review_cart.return_value = make_cart()
    generator = MagicMock()
    generator.settings = capacity_proposal_settings(ENV)
    generator.carts = EligibleFixtureReader(reader)

    with pytest.raises(ValueError, match="rejected initial"):
        generate_capacity_proposal(
            engine,
            reader,
            generator,
            run_id=initial_run_id,
            instance_id=instance,
            env=ENV,
            claim_id=CAPACITY_RECOVERY_CLAIM_ID,
        )
    generator.generate.assert_not_called()

    with engine.begin() as conn:
        conn.execute(
            sa.text(
                "INSERT INTO capacity_proposal_claim (id, run_id, created_at) "
                "VALUES (:id, :run, :created)"
            ),
            {
                "id": CAPACITY_INITIAL_CLAIM_ID,
                "run": initial_run_id,
                "created": datetime.now(UTC),
            },
        )
        conn.execute(sa.update(runs).where(runs.c.id == initial_run_id).values(state="rejected"))

    recovery_run, _ = RunRepository(engine).create_run(
        trigger_type="synthetic",
        trigger_ref="9:2",
        idempotency_key=f"synthetic-scan:{instance}:2",
    )
    reader.get_synthetic_review_cart.return_value = make_cart(2)
    with pytest.raises(ValueError, match="exactly one settled"):
        generate_capacity_proposal(
            engine,
            reader,
            generator,
            run_id=recovery_run.id,
            instance_id=instance,
            env=ENV,
            claim_id=CAPACITY_RECOVERY_CLAIM_ID,
        )

    settings = capacity_proposal_settings(ENV)
    call_id = uuid.uuid4()
    budget = ProposalBudget(engine)
    budget.reserve(
        initial_run_id,
        call_id,
        limit=settings.budget_usd,
        input_limit=settings.input_tokens,
        output_limit=settings.output_tokens,
        rate=settings.rate,
        review=settings.review_budget,
    )
    budget.settle(call_id, input_tokens=1, output_tokens=1, rate=settings.rate)
    generate_capacity_proposal(
        engine,
        reader,
        generator,
        run_id=recovery_run.id,
        instance_id=instance,
        env=ENV,
        claim_id=CAPACITY_RECOVERY_CLAIM_ID,
    )
    with pytest.raises(ValueError, match="already claimed"):
        generate_capacity_proposal(
            engine,
            reader,
            generator,
            run_id=recovery_run.id,
            instance_id=instance,
            env=ENV,
            claim_id=CAPACITY_RECOVERY_CLAIM_ID,
        )
    generator.generate.assert_called_once()
    with engine.connect() as conn:
        claims = set(conn.execute(sa.text("SELECT id FROM capacity_proposal_claim")).scalars())
    assert {CAPACITY_INITIAL_CLAIM_ID, CAPACITY_RECOVERY_CLAIM_ID} <= claims
