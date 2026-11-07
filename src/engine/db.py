from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import sqlalchemy as sa
from alembic import command
from alembic.config import Config
from sqlalchemy import Engine

from engine.runs import TERMINAL_STATES, RunState, validate_transition

metadata = sa.MetaData()

runs = sa.Table(
    "runs",
    metadata,
    sa.Column("id", sa.Uuid(), primary_key=True),
    sa.Column("state", sa.Text(), nullable=False),
    sa.Column("trigger_type", sa.Text(), nullable=False),
    sa.Column("trigger_ref", sa.Text(), nullable=False),
    sa.Column("idempotency_key", sa.Text(), nullable=False, unique=True),
    sa.Column("policy_result", sa.JSON(), nullable=True),
    sa.Column("outcome", sa.Text(), nullable=True),
    sa.Column("failure_reason", sa.Text(), nullable=True),
    sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
)

run_events = sa.Table(
    "run_events",
    metadata,
    sa.Column("id", sa.BigInteger(), primary_key=True, autoincrement=True),
    sa.Column("run_id", sa.Uuid(), sa.ForeignKey("runs.id"), nullable=False),
    sa.Column("from_state", sa.Text(), nullable=True),
    sa.Column("to_state", sa.Text(), nullable=False),
    sa.Column("actor", sa.Text(), nullable=False),
    sa.Column("detail", sa.JSON(), nullable=True),
    sa.Column("at", sa.DateTime(timezone=True), nullable=False),
)

agent_proposals = sa.Table(
    "agent_proposals",
    metadata,
    sa.Column("run_id", sa.Uuid(), sa.ForeignKey("runs.id"), primary_key=True),
    sa.Column("schema_version", sa.Text(), nullable=False),
    sa.Column("fingerprint", sa.Text(), nullable=False),
    sa.Column("proposal", sa.JSON(), nullable=False),
    sa.Column("gate_result", sa.JSON(), nullable=False),
)


class RunNotFound(Exception):
    pass


class StaleRunState(Exception):
    """The run was changed by someone else since it was read."""


@dataclass(frozen=True)
class Run:
    id: uuid.UUID
    state: RunState
    trigger_type: str
    trigger_ref: str
    idempotency_key: str
    policy_result: dict[str, Any] | None
    outcome: str | None
    failure_reason: str | None


@dataclass(frozen=True)
class RunEvent:
    from_state: RunState | None
    to_state: RunState
    actor: str
    detail: dict[str, Any] | None


def create_engine(database_url: str) -> Engine:
    return sa.create_engine(database_url, pool_pre_ping=True)


def upgrade_schema(engine: Engine) -> None:
    config = Config()
    config.set_main_option("script_location", str(Path(__file__).parent / "migrations"))
    with engine.begin() as connection:
        config.attributes["connection"] = connection
        command.upgrade(config, "head")


def _to_run(row: Any) -> Run:
    return Run(
        id=row.id,
        state=RunState(row.state),
        trigger_type=row.trigger_type,
        trigger_ref=row.trigger_ref,
        idempotency_key=row.idempotency_key,
        policy_result=row.policy_result,
        outcome=row.outcome,
        failure_reason=row.failure_reason,
    )


class RunRepository:
    """The only supported way to change run state; each change writes an audit event."""

    def __init__(self, engine: Engine) -> None:
        self._engine = engine

    def create_run(
        self, *, trigger_type: str, trigger_ref: str, idempotency_key: str, actor: str = "system"
    ) -> tuple[Run, bool]:
        """Return (run, created). A repeated idempotency key returns the existing run."""
        now = datetime.now(UTC)
        run_id = uuid.uuid4()
        with self._engine.begin() as conn:
            inserted = conn.execute(
                sa.dialects.postgresql.insert(runs)
                .values(
                    id=run_id,
                    state=RunState.RECEIVED.value,
                    trigger_type=trigger_type,
                    trigger_ref=trigger_ref,
                    idempotency_key=idempotency_key,
                    created_at=now,
                    updated_at=now,
                )
                .on_conflict_do_nothing(index_elements=["idempotency_key"])
                .returning(runs.c.id)
            ).first()
            if inserted is not None:
                conn.execute(
                    run_events.insert().values(
                        run_id=run_id,
                        from_state=None,
                        to_state=RunState.RECEIVED.value,
                        actor=actor,
                        detail={"trigger_type": trigger_type, "trigger_ref": trigger_ref},
                        at=now,
                    )
                )
            row = conn.execute(
                sa.select(runs).where(runs.c.idempotency_key == idempotency_key)
            ).one()
        return _to_run(row), inserted is not None

    def get_run(self, run_id: uuid.UUID) -> Run:
        with self._engine.connect() as conn:
            row = conn.execute(sa.select(runs).where(runs.c.id == run_id)).first()
        if row is None:
            raise RunNotFound(str(run_id))
        return _to_run(row)

    def get_events(self, run_id: uuid.UUID) -> list[RunEvent]:
        with self._engine.connect() as conn:
            rows = conn.execute(
                sa.select(run_events).where(run_events.c.run_id == run_id).order_by(run_events.c.id)
            ).all()
        return [
            RunEvent(
                from_state=RunState(r.from_state) if r.from_state else None,
                to_state=RunState(r.to_state),
                actor=r.actor,
                detail=r.detail,
            )
            for r in rows
        ]

    def persist_proposal(
        self,
        run_id: uuid.UUID,
        *,
        schema_version: str,
        fingerprint: str,
        proposal: dict[str, Any],
        gate_result: dict[str, Any],
    ) -> None:
        """Store the proposal and its state transition in the same transaction."""
        expected, new = RunState.GENERATING_PROPOSAL, RunState.EVALUATING_POLICY
        validate_transition(expected, new)
        now = datetime.now(UTC)
        with self._engine.begin() as conn:
            row = conn.execute(
                runs.update()
                .where(runs.c.id == run_id, runs.c.state == expected.value)
                .values(state=new.value, policy_result=gate_result, updated_at=now)
                .returning(runs.c.id)
            ).first()
            if row is None:
                raise StaleRunState("run no longer generating proposal")
            conn.execute(
                agent_proposals.insert().values(
                    run_id=run_id,
                    schema_version=schema_version,
                    fingerprint=fingerprint,
                    proposal=proposal,
                    gate_result=gate_result,
                )
            )
            conn.execute(
                run_events.insert().values(
                    run_id=run_id,
                    from_state=expected.value,
                    to_state=new.value,
                    actor="system",
                    detail={"schema_version": schema_version, "fingerprint": fingerprint},
                    at=now,
                )
            )

    def transition(
        self,
        run_id: uuid.UUID,
        expected: RunState,
        new: RunState,
        *,
        actor: str,
        detail: dict[str, Any] | None = None,
        policy_result: dict[str, Any] | None = None,
        outcome: str | None = None,
        failure_reason: str | None = None,
    ) -> Run:
        """Move a run from `expected` to `new` if it is still in `expected` (compare-and-set)."""
        validate_transition(expected, new)
        now = datetime.now(UTC)
        values: dict[str, Any] = {"state": new.value, "updated_at": now}
        if policy_result is not None:
            values["policy_result"] = policy_result
        if outcome is not None:
            values["outcome"] = outcome
        if failure_reason is not None:
            values["failure_reason"] = failure_reason
        with self._engine.begin() as conn:
            row = conn.execute(
                runs.update()
                .where(runs.c.id == run_id, runs.c.state == expected.value)
                .values(**values)
                .returning(*runs.c)
            ).first()
            if row is None:
                exists = conn.execute(sa.select(runs.c.id).where(runs.c.id == run_id)).first()
                if exists is None:
                    raise RunNotFound(str(run_id))
                raise StaleRunState(f"Run {run_id} is no longer in state {expected.value}.")
            conn.execute(
                run_events.insert().values(
                    run_id=run_id,
                    from_state=expected.value,
                    to_state=new.value,
                    actor=actor,
                    detail=detail,
                    at=now,
                )
            )
        return _to_run(row)

    def fail(self, run_id: uuid.UUID, *, reason: str, actor: str = "system") -> Run:
        """Record an explicit failure from whatever active state the run is in."""
        current = self.get_run(run_id)
        if current.state in TERMINAL_STATES:
            return current
        return self.transition(
            run_id,
            current.state,
            RunState.FAILED,
            actor=actor,
            detail={"reason": reason},
            failure_reason=reason,
        )
