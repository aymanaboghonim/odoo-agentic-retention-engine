from __future__ import annotations

import uuid
from collections.abc import Mapping
from datetime import UTC, datetime
from decimal import Decimal
from typing import Protocol

import sqlalchemy as sa
from sqlalchemy import Engine

from engine.db import RunRepository
from engine.odoo import AbandonedCart
from engine.proposal import ProposalSettings
from engine.runs import RunState

CAPACITY_REVIEW_ID = "poc2-68-capacity"
CAPACITY_INITIAL_CLAIM_ID = CAPACITY_REVIEW_ID
CAPACITY_RECOVERY_CLAIM_ID = "poc2-68-capacity-recovery"
CAPACITY_LIMIT = Decimal("1.00")
CAPACITY_REQUEST = "Please write a short, friendly reminder for this synthetic test cart."


class FixtureReader(Protocol):
    def get_synthetic_review_cart(
        self, order_id: int, *, now: datetime
    ) -> AbandonedCart | None: ...


class ProposalGenerator(Protocol):
    @property
    def settings(self) -> ProposalSettings: ...

    @property
    def carts(self) -> object: ...

    def generate(
        self,
        run_id: uuid.UUID,
        *,
        customer_id: int,
        cart_id: int,
        language: str,
        request: str,
    ) -> None: ...


def capacity_proposal_settings(env: Mapping[str, str]) -> ProposalSettings:
    if env.get("FULL_STACK_CAPACITY_PROPOSAL") != "YES":
        raise ValueError("FULL_STACK_CAPACITY_PROPOSAL=YES is required")
    settings = ProposalSettings.from_env(env)
    review = settings.review_budget
    if (
        review is None
        or review.review_id != CAPACITY_REVIEW_ID
        or review.limit_usd != CAPACITY_LIMIT
        or settings.budget_usd > CAPACITY_LIMIT
    ):
        raise ValueError("capacity proposal requires the fixed issue 68 aggregate and run budgets")
    reservation = settings.rate.estimate(settings.input_tokens, settings.output_tokens)
    if reservation > settings.budget_usd or reservation > review.limit_usd:
        raise ValueError("capacity proposal token reservation exceeds the configured budget")
    return settings


class EligibleFixtureReader:
    """Recheck the fixture marker and eligibility when ProposalService loads the cart."""

    def __init__(self, reader: FixtureReader) -> None:
        self._reader = reader

    def get_cart(self, order_id: int) -> AbandonedCart | None:
        return self._reader.get_synthetic_review_cart(order_id, now=datetime.now(UTC))

    def wraps(self, reader: FixtureReader) -> bool:
        return self._reader is reader


def validate_capacity_run(
    engine: Engine,
    reader: FixtureReader,
    *,
    run_id: uuid.UUID,
    instance_id: str,
) -> tuple[int, int, AbandonedCart]:
    instance_id = str(uuid.UUID(instance_id))
    run = RunRepository(engine).get_run(run_id)
    prefix = f"synthetic-scan:{instance_id}:"
    parts = run.trigger_ref.split(":")
    if (
        run.state is not RunState.RECEIVED
        or run.trigger_type != "synthetic"
        or len(parts) != 2
        or not all(part.isascii() and part.isdecimal() and int(part) > 0 for part in parts)
        or run.idempotency_key != prefix + parts[1]
    ):
        raise ValueError("capacity proposal requires an unprocessed run from this fixture scan")
    partner_id, order_id = map(int, parts)
    cart = reader.get_synthetic_review_cart(order_id, now=datetime.now(UTC))
    if cart is None or cart.partner_id != partner_id or cart.order_id != order_id:
        raise ValueError("capacity proposal requires a still-eligible marked synthetic fixture")
    return partner_id, order_id, cart


def validate_capacity_recovery(engine: Engine, *, run_id: uuid.UUID) -> None:
    with engine.connect() as conn:
        initial = (
            conn.execute(
                sa.text(
                    "SELECT c.run_id, r.state FROM capacity_proposal_claim c "
                    "JOIN runs r ON r.id=c.run_id WHERE c.id=:id"
                ),
                {"id": CAPACITY_INITIAL_CLAIM_ID},
            )
            .mappings()
            .first()
        )
        if initial is None or initial["run_id"] == run_id or initial["state"] != "rejected":
            raise ValueError("recovery requires a rejected initial measurement run")
        calls = (
            conn.execute(
                sa.text(
                    "SELECT status FROM proposal_calls "
                    "WHERE run_id=:run_id AND review_id=:review_id"
                ),
                {"run_id": initial["run_id"], "review_id": CAPACITY_REVIEW_ID},
            )
            .scalars()
            .all()
        )
    if len(calls) != 1 or calls[0] != "settled":
        raise ValueError("recovery requires exactly one settled initial provider call")


def generate_capacity_proposal(
    engine: Engine,
    reader: FixtureReader,
    generator: ProposalGenerator,
    *,
    run_id: uuid.UUID,
    instance_id: str,
    env: Mapping[str, str],
    claim_id: str = CAPACITY_INITIAL_CLAIM_ID,
) -> None:
    if generator.settings != capacity_proposal_settings(env):
        raise ValueError("capacity proposal generator must use the authorized budget settings")
    if not isinstance(generator.carts, EligibleFixtureReader) or not generator.carts.wraps(reader):
        raise ValueError("capacity proposal generator must recheck the same synthetic fixture")
    if claim_id == CAPACITY_RECOVERY_CLAIM_ID:
        validate_capacity_recovery(engine, run_id=run_id)
    elif claim_id != CAPACITY_INITIAL_CLAIM_ID:
        raise ValueError("capacity proposal claim ID is not authorized")
    partner_id, order_id, cart = validate_capacity_run(
        engine, reader, run_id=run_id, instance_id=instance_id
    )
    with engine.begin() as conn:
        inserted = conn.execute(
            sa.text(
                "INSERT INTO capacity_proposal_claim (id, run_id, created_at) "
                "VALUES (:id, :run_id, :created_at) ON CONFLICT DO NOTHING RETURNING id"
            ),
            {"id": claim_id, "run_id": run_id, "created_at": datetime.now(UTC)},
        ).first()
        if inserted is None:
            raise ValueError("issue 68 capacity proposal attempt was already claimed")
    language = "ar" if (cart.language or "").startswith("ar") else "en"
    generator.generate(
        run_id,
        customer_id=partner_id,
        cart_id=order_id,
        language=language,
        request=CAPACITY_REQUEST,
    )
