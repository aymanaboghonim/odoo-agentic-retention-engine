from __future__ import annotations

import json
import os
import uuid
from collections.abc import Iterator
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict
from datetime import UTC, datetime
from decimal import Decimal

import pytest
import sqlalchemy as sa

from engine.config import normalize_database_url
from engine.db import (
    ExecutionConflict,
    RunRepository,
    agent_proposals,
    approval_requests,
    create_engine,
    execution_effects,
    run_events,
    runs,
    upgrade_schema,
)
from engine.gate_ledger import InMemoryGateLedger
from engine.policy import Gate, GateConfig, OfferProposal, as_dict
from engine.runs import RunState

URL = os.environ.get("TEST_DATABASE_URL", "")
if not URL and os.environ.get("REQUIRE_DB_TESTS") == "1":
    raise RuntimeError("REQUIRE_DB_TESTS=1 but TEST_DATABASE_URL is not set")

BINDING = "a" * 64


@pytest.fixture
def ready_run() -> Iterator[tuple[sa.Engine, uuid.UUID]]:
    if not URL:
        pytest.skip("TEST_DATABASE_URL not set")
    engine = create_engine(normalize_database_url(URL))
    upgrade_schema(engine)
    repo = RunRepository(engine)
    run, _ = repo.create_run(
        trigger_type="synthetic", trigger_ref="journal", idempotency_key=str(uuid.uuid4())
    )
    for current, new in [
        (RunState.RECEIVED, RunState.VALIDATING_INPUT),
        (RunState.VALIDATING_INPUT, RunState.LOADING_CONTEXT),
        (RunState.LOADING_CONTEXT, RunState.GENERATING_PROPOSAL),
    ]:
        repo.transition(run.id, current, new, actor="test")
    offer = OfferProposal(
        run_id=str(run.id),
        customer_id="9",
        cart_id="1",
        cart_owner_id="9",
        category="decor",
        currency="USD",
        eligible_total=Decimal("100"),
        eligible_unit_cost_total=Decimal("40"),
        proposed_discount_percent=Decimal(0),
        customer_message="A synthetic test message.",
    )
    proposal = json.loads(json.dumps(asdict(offer), default=str))
    proposal.update(
        {
            "action": "draft_message",
            "execution_channel": "mailpit",
            "cart_snapshot": {
                "order_id": 1,
                "partner_id": 9,
                "currency": "USD",
                "amount_total": "100",
                "date_order": "2026-11-08T00:00:00+00:00",
                "lines": [{"product_id": 1}],
            },
        }
    )
    gate = Gate(
        GateConfig.from_env(
            {
                "GATE_TIMEZONE": "Asia/Riyadh",
                "GATE_CURRENCY": "USD",
                "GATE_DAILY_BUDGET": "100",
                "GATE_CATEGORY_MIN_MARGINS": '{"decor":"0.2"}',
            }
        ),
        InMemoryGateLedger(),
    )
    repo.persist_proposal(
        run.id,
        schema_version="test-v1",
        fingerprint=offer.fingerprint(),
        proposal=proposal,
        gate_result=as_dict(gate.evaluate(offer, now=datetime.now(UTC))),
    )
    repo.transition(run.id, RunState.EVALUATING_POLICY, RunState.READY_TO_EXECUTE, actor="test")
    yield engine, run.id
    with engine.begin() as conn:
        conn.execute(run_events.delete().where(run_events.c.run_id == run.id))
        conn.execute(approval_requests.delete().where(approval_requests.c.run_id == run.id))
        conn.execute(agent_proposals.delete().where(agent_proposals.c.run_id == run.id))
        conn.execute(runs.delete().where(runs.c.id == run.id))
    engine.dispose()


def test_concurrent_execution_claim(ready_run: tuple[sa.Engine, uuid.UUID]) -> None:
    engine, run_id = ready_run
    repo = RunRepository(engine)
    record = repo.get_execution_record(run_id)

    def claim() -> uuid.UUID | None:
        try:
            return repo.claim_execution(
                run_id, binding_hash=BINDING, context_hash=record.context_hash
            )
        except ExecutionConflict:
            return None

    with ThreadPoolExecutor(max_workers=8) as pool:
        claims = list(pool.map(lambda _: claim(), range(8)))
    assert sum(claim is not None for claim in claims) == 1
    assert repo.get_run(run_id).state is RunState.EXECUTING
    assert sum(event.to_state is RunState.EXECUTING for event in repo.get_events(run_id)) == 1


def test_concurrent_effect_starts_only_one_attempt(ready_run: tuple[sa.Engine, uuid.UUID]) -> None:
    engine, run_id = ready_run
    repo = RunRepository(engine)
    record = repo.get_execution_record(run_id)
    claim = repo.claim_execution(run_id, binding_hash=BINDING, context_hash=record.context_hash)

    def start() -> int | None:
        try:
            return repo.start_effect(
                run_id, claim, binding_hash=BINDING, effect="message", max_attempts=2
            )
        except ExecutionConflict:
            return None

    with ThreadPoolExecutor(max_workers=8) as pool:
        attempts = list(pool.map(lambda _: start(), range(8)))
    assert attempts.count(1) == 1
    assert attempts.count(None) == 7


@pytest.mark.parametrize("held_status", ["started", "unknown", "confirmed"])
def test_unresolved_or_confirmed_effect_is_not_retried(
    ready_run: tuple[sa.Engine, uuid.UUID], held_status: str
) -> None:
    engine, run_id = ready_run
    repo = RunRepository(engine)
    record = repo.get_execution_record(run_id)
    claim = repo.claim_execution(run_id, binding_hash=BINDING, context_hash=record.context_hash)
    assert (
        repo.start_effect(run_id, claim, binding_hash=BINDING, effect="message", max_attempts=3)
        == 1
    )
    if held_status != "started":
        repo.finish_effect(
            run_id,
            claim,
            binding_hash=BINDING,
            effect="message",
            attempt=1,
            status=held_status,
            reference="provider-1" if held_status == "confirmed" else None,
        )
    with pytest.raises(ExecutionConflict):
        repo.start_effect(run_id, claim, binding_hash=BINDING, effect="message", max_attempts=3)
    with engine.connect() as conn:
        row = conn.execute(
            sa.select(execution_effects).where(execution_effects.c.run_id == run_id)
        ).one()
    assert row.status == held_status
    assert row.attempts == 1
    assert repo.get_run(run_id).state is RunState.EXECUTING


def test_only_known_not_sent_can_retry_with_original_limit(
    ready_run: tuple[sa.Engine, uuid.UUID],
) -> None:
    engine, run_id = ready_run
    repo = RunRepository(engine)
    record = repo.get_execution_record(run_id)
    claim = repo.claim_execution(run_id, binding_hash=BINDING, context_hash=record.context_hash)
    for attempt in (1, 2):
        assert (
            repo.start_effect(run_id, claim, binding_hash=BINDING, effect="coupon", max_attempts=2)
            == attempt
        )
        repo.finish_effect(
            run_id, claim, binding_hash=BINDING, effect="coupon", attempt=attempt, status="not_sent"
        )
        if attempt == 1:
            with pytest.raises(ExecutionConflict):
                repo.start_effect(
                    run_id, claim, binding_hash=BINDING, effect="coupon", max_attempts=3
                )
    with pytest.raises(ExecutionConflict):
        repo.start_effect(run_id, claim, binding_hash=BINDING, effect="coupon", max_attempts=2)


def test_wrong_claim_or_binding_cannot_start_or_finish(
    ready_run: tuple[sa.Engine, uuid.UUID],
) -> None:
    engine, run_id = ready_run
    repo = RunRepository(engine)
    record = repo.get_execution_record(run_id)
    claim = repo.claim_execution(run_id, binding_hash=BINDING, context_hash=record.context_hash)
    with pytest.raises(ExecutionConflict):
        repo.start_effect(
            run_id, uuid.uuid4(), binding_hash=BINDING, effect="message", max_attempts=1
        )
    with pytest.raises(ExecutionConflict):
        repo.start_effect(run_id, claim, binding_hash="b" * 64, effect="message", max_attempts=1)
    repo.start_effect(run_id, claim, binding_hash=BINDING, effect="message", max_attempts=1)
    with pytest.raises(ExecutionConflict):
        repo.finish_effect(
            run_id, claim, binding_hash=BINDING, effect="message", attempt=1, status="confirmed"
        )
    with pytest.raises(ExecutionConflict):
        repo.finish_effect(
            run_id, claim, binding_hash="b" * 64, effect="message", attempt=1, status="not_sent"
        )


def test_stale_attempt_cannot_finish_a_retry(ready_run: tuple[sa.Engine, uuid.UUID]) -> None:
    engine, run_id = ready_run
    repo = RunRepository(engine)
    record = repo.get_execution_record(run_id)
    claim = repo.claim_execution(run_id, binding_hash=BINDING, context_hash=record.context_hash)
    repo.start_effect(run_id, claim, binding_hash=BINDING, effect="message", max_attempts=2)
    repo.finish_effect(
        run_id, claim, binding_hash=BINDING, effect="message", attempt=1, status="not_sent"
    )
    repo.start_effect(run_id, claim, binding_hash=BINDING, effect="message", max_attempts=2)
    with pytest.raises(ExecutionConflict):
        repo.finish_effect(
            run_id,
            claim,
            binding_hash=BINDING,
            effect="message",
            attempt=1,
            status="confirmed",
            reference="stale",
        )
    repo.finish_effect(
        run_id, claim, binding_hash=BINDING, effect="message", attempt=2, status="unknown"
    )
