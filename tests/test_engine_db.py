from __future__ import annotations

import os
import uuid
from collections.abc import Iterator

import pytest
import sqlalchemy as sa

from engine.db import (
    RunNotFound,
    RunRepository,
    StaleRunState,
    create_engine,
    run_events,
    runs,
    upgrade_schema,
)
from engine.runs import InvalidTransition, RunState

URL = os.environ.get("TEST_DATABASE_URL", "")

if not URL and os.environ.get("REQUIRE_DB_TESTS") == "1":
    raise RuntimeError("REQUIRE_DB_TESTS=1 but TEST_DATABASE_URL is not set.")

pytestmark = pytest.mark.skipif(
    not URL, reason="TEST_DATABASE_URL not set; start the engine DB (see docs/runbooks)."
)


@pytest.fixture
def engine() -> Iterator[sa.Engine]:
    from engine.config import normalize_database_url

    eng = create_engine(normalize_database_url(URL))
    upgrade_schema(eng)
    with eng.begin() as conn:
        conn.execute(run_events.delete())
        conn.execute(runs.delete())
    yield eng
    eng.dispose()


def _new_run(repo: RunRepository, key: str = "cart-1") -> uuid.UUID:
    run, created = repo.create_run(
        trigger_type="cart_scan", trigger_ref="SO001", idempotency_key=key
    )
    assert created
    return run.id


def test_run_state_and_audit_survive_restart(engine: sa.Engine) -> None:
    repo = RunRepository(engine)
    run_id = _new_run(repo)
    repo.transition(run_id, RunState.RECEIVED, RunState.VALIDATING_INPUT, actor="worker")
    repo.transition(
        run_id,
        RunState.VALIDATING_INPUT,
        RunState.LOADING_CONTEXT,
        actor="worker",
        policy_result={"checked": True},
    )
    engine.dispose()

    from engine.config import normalize_database_url

    restarted = RunRepository(create_engine(normalize_database_url(URL)))
    run = restarted.get_run(run_id)
    assert run.state is RunState.LOADING_CONTEXT
    assert run.trigger_type == "cart_scan"
    assert run.policy_result == {"checked": True}
    events = restarted.get_events(run_id)
    assert [(e.from_state, e.to_state) for e in events] == [
        (None, RunState.RECEIVED),
        (RunState.RECEIVED, RunState.VALIDATING_INPUT),
        (RunState.VALIDATING_INPUT, RunState.LOADING_CONTEXT),
    ]


def test_invalid_transition_is_not_persisted(engine: sa.Engine) -> None:
    repo = RunRepository(engine)
    run_id = _new_run(repo)
    with pytest.raises(InvalidTransition):
        repo.transition(run_id, RunState.RECEIVED, RunState.EXECUTING, actor="worker")
    assert repo.get_run(run_id).state is RunState.RECEIVED
    assert len(repo.get_events(run_id)) == 1


def test_stale_transition_is_rejected(engine: sa.Engine) -> None:
    repo = RunRepository(engine)
    run_id = _new_run(repo)
    repo.transition(run_id, RunState.RECEIVED, RunState.VALIDATING_INPUT, actor="a")
    with pytest.raises(StaleRunState):
        repo.transition(run_id, RunState.RECEIVED, RunState.CANCELLED, actor="b")
    assert repo.get_run(run_id).state is RunState.VALIDATING_INPUT


def test_unknown_run_raises(engine: sa.Engine) -> None:
    repo = RunRepository(engine)
    with pytest.raises(RunNotFound):
        repo.get_run(uuid.uuid4())
    with pytest.raises(RunNotFound):
        repo.transition(uuid.uuid4(), RunState.RECEIVED, RunState.CANCELLED, actor="a")


def test_failed_run_has_explicit_failure_outcome(engine: sa.Engine) -> None:
    repo = RunRepository(engine)
    run_id = _new_run(repo)
    repo.transition(run_id, RunState.RECEIVED, RunState.VALIDATING_INPUT, actor="a")
    failed = repo.fail(run_id, reason="odoo unreachable")
    assert failed.state is RunState.FAILED
    assert failed.failure_reason == "odoo unreachable"
    assert repo.get_events(run_id)[-1].detail == {"reason": "odoo unreachable"}
    assert repo.fail(run_id, reason="again").failure_reason == "odoo unreachable"
    with pytest.raises(InvalidTransition):
        repo.transition(run_id, RunState.FAILED, RunState.RECEIVED, actor="a")


def test_repeated_trigger_returns_same_run(engine: sa.Engine) -> None:
    repo = RunRepository(engine)
    first, created_first = repo.create_run(
        trigger_type="cart_scan", trigger_ref="SO001", idempotency_key="k"
    )
    second, created_second = repo.create_run(
        trigger_type="cart_scan", trigger_ref="SO001", idempotency_key="k"
    )
    assert created_first and not created_second
    assert first.id == second.id
    assert len(repo.get_events(first.id)) == 1
