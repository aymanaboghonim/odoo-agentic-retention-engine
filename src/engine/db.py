from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import sqlalchemy as sa
from alembic import command
from alembic.config import Config
from sqlalchemy import Connection, Engine

from engine.approval_data import InvalidProposalSnapshot, offer_from_record, snapshot_hash
from engine.policy import Gate, Outcome, as_dict
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
    sa.Column("customer_id", sa.Text(), nullable=True, index=True),
    sa.Column("retention_expires_at", sa.DateTime(timezone=True), nullable=True, index=True),
)

customer_consents = sa.Table(
    "customer_consents",
    metadata,
    sa.Column("customer_id", sa.Text(), primary_key=True),
    sa.Column("channel", sa.Text(), primary_key=True),
    sa.Column("status", sa.Text(), nullable=False),
    sa.Column("source", sa.Text(), nullable=False),
    sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
)

erasure_requests = sa.Table(
    "erasure_requests",
    metadata,
    sa.Column("subject_hash", sa.Text(), primary_key=True),
    sa.Column("erased_at", sa.DateTime(timezone=True), nullable=False),
    sa.Column("runs_deleted", sa.Integer(), nullable=False),
    sa.Column("reconsented_at", sa.DateTime(timezone=True), nullable=True),
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

approval_requests = sa.Table(
    "approval_requests",
    metadata,
    sa.Column("run_id", sa.Uuid(), sa.ForeignKey("runs.id", ondelete="CASCADE"), primary_key=True),
    sa.Column("snapshot", sa.JSON(), nullable=False),
    sa.Column("snapshot_hash", sa.Text(), nullable=False),
    sa.Column("decision", sa.Text(), nullable=True),
    sa.Column("actor", sa.Text(), nullable=True),
    sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    sa.Column("decided_at", sa.DateTime(timezone=True), nullable=True),
    sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False, index=True),
)

engine_runtime_state = sa.Table(
    "engine_runtime_state",
    metadata,
    sa.Column("id", sa.SmallInteger(), primary_key=True),
    sa.Column("state", sa.Text(), nullable=False),
    sa.Column("reason", sa.Text(), nullable=False),
    sa.Column("changed_by", sa.Text(), nullable=False),
    sa.Column("changed_at", sa.DateTime(timezone=True), nullable=False),
    sa.Column("version", sa.Integer(), nullable=False),
)

engine_runtime_events = sa.Table(
    "engine_runtime_events",
    metadata,
    sa.Column("id", sa.BigInteger(), primary_key=True, autoincrement=True),
    sa.Column("from_state", sa.Text(), nullable=False),
    sa.Column("to_state", sa.Text(), nullable=False),
    sa.Column("actor", sa.Text(), nullable=False),
    sa.Column("reason", sa.Text(), nullable=False),
    sa.Column("version", sa.Integer(), nullable=False),
    sa.Column("at", sa.DateTime(timezone=True), nullable=False),
)

execution_claims = sa.Table(
    "execution_claims",
    metadata,
    sa.Column("run_id", sa.Uuid(), sa.ForeignKey("runs.id", ondelete="CASCADE"), primary_key=True),
    sa.Column("claim_id", sa.Uuid(), nullable=False, unique=True),
    sa.Column("binding_hash", sa.Text(), nullable=False),
    sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
)

execution_effects = sa.Table(
    "execution_effects",
    metadata,
    sa.Column(
        "run_id",
        sa.Uuid(),
        sa.ForeignKey("execution_claims.run_id", ondelete="CASCADE"),
        primary_key=True,
    ),
    sa.Column("effect", sa.Text(), primary_key=True),
    sa.Column("status", sa.Text(), nullable=False),
    sa.Column("attempts", sa.Integer(), nullable=False),
    sa.Column("max_attempts", sa.Integer(), nullable=False),
    sa.Column("reference", sa.Text()),
    sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
)


class RunNotFound(Exception):
    pass


class StaleRunState(Exception):
    """The run was changed by someone else since it was read."""


class ApprovalConflict(Exception):
    """The proposal or request is no longer eligible for this decision."""


class ExecutionConflict(Exception):
    """A claim or external effect cannot safely be started or changed."""


class ApprovalExpired(ApprovalConflict, ExecutionConflict):
    """The approval request passed its `expires_at` (spec AA-5, AA-7)."""


# Owner decision 2026-11-16 (spec operator-rbac-and-expiring-approvals.md).
DEFAULT_APPROVAL_TTL = timedelta(hours=24)


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
    customer_id: str | None = None


@dataclass(frozen=True)
class RunEvent:
    from_state: RunState | None
    to_state: RunState
    actor: str
    detail: dict[str, Any] | None


@dataclass(frozen=True)
class ExecutionRecord:
    run_id: uuid.UUID
    state: RunState
    schema_version: str
    fingerprint: str
    proposal: dict[str, Any]
    gate_result: dict[str, Any]
    human_approved: bool
    approval_snapshot_hash: str | None
    context_hash: str


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
        customer_id=getattr(row, "customer_id", None),
    )


class RunRepository:
    """The only supported way to change run state; each change writes an audit event."""

    def __init__(self, engine: Engine) -> None:
        self._engine = engine

    def create_run(
        self,
        *,
        trigger_type: str,
        trigger_ref: str,
        idempotency_key: str,
        actor: str = "system",
        customer_id: str | None = None,
        retention_days: int | None = None,
    ) -> tuple[Run, bool]:
        """Return (run, created). A repeated idempotency key returns the existing run."""
        with self._engine.begin() as conn:
            return self.create_run_in_transaction(
                conn,
                trigger_type=trigger_type,
                trigger_ref=trigger_ref,
                idempotency_key=idempotency_key,
                actor=actor,
                customer_id=customer_id,
                retention_days=retention_days,
            )

    def create_run_in_transaction(
        self,
        conn: Connection,
        *,
        trigger_type: str,
        trigger_ref: str,
        idempotency_key: str,
        actor: str = "system",
        customer_id: str | None = None,
        retention_days: int | None = None,
    ) -> tuple[Run, bool]:
        """Create a run and audit event within the caller's transaction.

        `customer_id` is the opaque Odoo partner ID, never a name or contact value.
        `retention_days` sets `retention_expires_at`; it must be at least one day.
        """
        if retention_days is not None and (
            not isinstance(retention_days, int)
            or isinstance(retention_days, bool)
            or retention_days < 1
        ):
            raise ValueError("retention days must be an integer of at least 1")
        if customer_id is not None and (not customer_id.isdigit() or customer_id.startswith("0")):
            raise ValueError("customer ID must be a positive Odoo partner ID")
        now = datetime.now(UTC)
        run_id = uuid.uuid4()
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
                customer_id=customer_id,
                retention_expires_at=(
                    now + timedelta(days=retention_days) if retention_days is not None else None
                ),
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
        row = conn.execute(sa.select(runs).where(runs.c.idempotency_key == idempotency_key)).one()
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

    def request_approval(
        self, run_id: uuid.UUID, gate: Gate, *, ttl: timedelta = DEFAULT_APPROVAL_TTL
    ) -> None:
        """Evaluate and persist one approval request with its state and audit."""
        if not isinstance(ttl, timedelta) or ttl <= timedelta(0):
            raise ValueError("approval TTL must be a positive duration")
        now = datetime.now(UTC)
        with self._engine.begin() as conn:
            run = conn.execute(sa.select(runs).where(runs.c.id == run_id).with_for_update()).first()
            row = conn.execute(
                sa.select(agent_proposals)
                .where(agent_proposals.c.run_id == run_id)
                .with_for_update()
            ).first()
            if run is None or run.state != RunState.EVALUATING_POLICY.value or row is None:
                raise ApprovalConflict("run is not eligible for approval")
            if conn.execute(
                sa.select(approval_requests.c.run_id).where(approval_requests.c.run_id == run_id)
            ).first():
                raise ApprovalConflict("approval request already exists")
            try:
                offer = offer_from_record(row.proposal)
            except InvalidProposalSnapshot as error:
                raise ApprovalConflict("invalid proposal") from error
            if offer.run_id != str(run_id) or offer.fingerprint() != row.fingerprint:
                raise ApprovalConflict("proposal fingerprint mismatch")
            if row.proposal.get("action") != "draft_message":
                raise ApprovalConflict("only draft messages can require approval")
            decision = gate.evaluate(offer, now=now)
            if decision.outcome is not Outcome.NEEDS_APPROVAL:
                raise ApprovalConflict("gate does not require approval")
            gate_result = as_dict(decision)
            snapshot = {
                "schema_version": row.schema_version,
                "fingerprint": row.fingerprint,
                "proposal": row.proposal,
                "gate_result": gate_result,
            }
            token = snapshot_hash(snapshot)
            conn.execute(
                agent_proposals.update()
                .where(agent_proposals.c.run_id == run_id)
                .values(gate_result=gate_result)
            )
            conn.execute(
                approval_requests.insert().values(
                    run_id=run_id,
                    snapshot=snapshot,
                    snapshot_hash=token,
                    created_at=now,
                    expires_at=now + ttl,
                )
            )
            conn.execute(
                runs.update()
                .where(runs.c.id == run_id)
                .values(
                    state=RunState.AWAITING_APPROVAL.value,
                    policy_result=gate_result,
                    updated_at=now,
                )
            )
            conn.execute(
                run_events.insert().values(
                    run_id=run_id,
                    from_state=RunState.EVALUATING_POLICY.value,
                    to_state=RunState.AWAITING_APPROVAL.value,
                    actor="system",
                    detail={"snapshot_hash": token, "expires_at": (now + ttl).isoformat()},
                    at=now,
                )
            )

    def pending_approvals(self) -> list[dict[str, Any]]:
        now = datetime.now(UTC)
        with self._engine.connect() as conn:
            rows = conn.execute(
                sa.select(
                    approval_requests.c.run_id,
                    approval_requests.c.snapshot,
                    approval_requests.c.snapshot_hash,
                )
                .join(runs, runs.c.id == approval_requests.c.run_id)
                .where(
                    approval_requests.c.decision.is_(None),
                    approval_requests.c.expires_at > now,
                    runs.c.state == RunState.AWAITING_APPROVAL.value,
                )
                .order_by(approval_requests.c.created_at, approval_requests.c.run_id)
            ).all()
        return [
            {
                "run_id": str(row.run_id),
                "snapshot": row.snapshot,
                "snapshot_hash": row.snapshot_hash,
            }
            for row in rows
        ]

    def decide_approval(self, run_id: uuid.UUID, *, token: str, decision: str, actor: str) -> Run:
        if decision not in {"approve", "reject"} or not actor.strip():
            raise ValueError("invalid decision or actor")
        now = datetime.now(UTC)
        with self._engine.begin() as conn:
            run = conn.execute(sa.select(runs).where(runs.c.id == run_id).with_for_update()).first()
            request = conn.execute(
                sa.select(approval_requests)
                .where(approval_requests.c.run_id == run_id)
                .with_for_update()
            ).first()
            proposal = conn.execute(
                sa.select(agent_proposals)
                .where(agent_proposals.c.run_id == run_id)
                .with_for_update()
            ).first()
            if (
                run is None
                or run.state != RunState.AWAITING_APPROVAL.value
                or request is None
                or request.decision is not None
                or proposal is None
            ):
                raise ApprovalConflict("approval is not pending")
            if request.expires_at <= now:
                raise ApprovalExpired("approval has expired")
            current = {
                "schema_version": proposal.schema_version,
                "fingerprint": proposal.fingerprint,
                "proposal": proposal.proposal,
                "gate_result": proposal.gate_result,
            }
            try:
                offer = offer_from_record(proposal.proposal)
            except InvalidProposalSnapshot as error:
                raise ApprovalConflict("invalid proposal") from error
            if (
                token != request.snapshot_hash
                or snapshot_hash(request.snapshot) != token
                or snapshot_hash(current) != token
                or offer.run_id != str(run_id)
                or offer.fingerprint() != proposal.fingerprint
            ):
                raise ApprovalConflict("proposal has changed")
            new = RunState.EVALUATING_POLICY if decision == "approve" else RunState.REJECTED
            validate_transition(RunState.AWAITING_APPROVAL, new)
            conn.execute(
                approval_requests.update()
                .where(approval_requests.c.run_id == run_id)
                .values(decision=decision, actor=actor, decided_at=now)
            )
            row = conn.execute(
                runs.update()
                .where(runs.c.id == run_id)
                .values(
                    state=new.value,
                    updated_at=now,
                    outcome="rejected" if decision == "reject" else None,
                )
                .returning(runs)
            ).one()
            conn.execute(
                run_events.insert().values(
                    run_id=run_id,
                    from_state=RunState.AWAITING_APPROVAL.value,
                    to_state=new.value,
                    actor=actor,
                    detail={"decision": decision, "snapshot_hash": token},
                    at=now,
                )
            )
        return _to_run(row)

    def ping(self) -> None:
        """Raise if the engine database cannot answer (health check, no data read)."""
        with self._engine.connect() as conn:
            conn.execute(sa.text("SELECT 1")).scalar_one()

    def state_counts(self) -> dict[str, int]:
        """Runs per state, for the runs-by-state gauge (read-only, no customer data)."""
        with self._engine.connect() as conn:
            rows = conn.execute(
                sa.select(runs.c.state, sa.func.count()).group_by(runs.c.state)
            ).all()
        return {str(state): int(total) for state, total in rows}

    def held_effect_count(self) -> int:
        """Effects with an unknown outcome on runs still executing (EX-4, ADR-0030)."""
        with self._engine.connect() as conn:
            return int(
                conn.execute(
                    sa.select(sa.func.count())
                    .select_from(
                        execution_effects.join(runs, runs.c.id == execution_effects.c.run_id)
                    )
                    .where(
                        execution_effects.c.status == "unknown",
                        runs.c.state == RunState.EXECUTING.value,
                    )
                ).scalar_one()
            )

    def expire_approvals(self, *, now: datetime | None = None, limit: int = 500) -> int:
        """Cancel pending approvals past `expires_at` (spec AA-6); returns the number cancelled."""
        moment = now or datetime.now(UTC)
        if moment.tzinfo is None or not 1 <= limit <= 10_000:
            raise ValueError("sweep time must be timezone-aware and limit must be bounded")
        cancelled = 0
        with self._engine.begin() as conn:
            ids = list(
                conn.execute(
                    sa.select(approval_requests.c.run_id)
                    .join(runs, runs.c.id == approval_requests.c.run_id)
                    .where(
                        approval_requests.c.decision.is_(None),
                        approval_requests.c.expires_at <= moment,
                        runs.c.state == RunState.AWAITING_APPROVAL.value,
                    )
                    .order_by(approval_requests.c.expires_at, approval_requests.c.run_id)
                    .limit(limit)
                    .with_for_update(of=runs, skip_locked=True)
                ).scalars()
            )
            for run_id in ids:
                row = conn.execute(
                    runs.update()
                    .where(runs.c.id == run_id, runs.c.state == RunState.AWAITING_APPROVAL.value)
                    .values(
                        state=RunState.CANCELLED.value,
                        outcome="approval_expired",
                        updated_at=moment,
                    )
                    .returning(runs.c.id)
                ).first()
                if row is None:
                    continue
                conn.execute(
                    run_events.insert().values(
                        run_id=run_id,
                        from_state=RunState.AWAITING_APPROVAL.value,
                        to_state=RunState.CANCELLED.value,
                        actor="system",
                        detail={"reason": "approval_expired"},
                        at=moment,
                    )
                )
                cancelled += 1
        return cancelled

    def current_human_approval(self, run_id: uuid.UUID) -> str | None:
        """Return the approved gate fingerprint only while the stored snapshot still matches."""
        with self._engine.connect() as conn:
            row = conn.execute(
                sa.select(
                    runs.c.state,
                    approval_requests.c.snapshot,
                    approval_requests.c.snapshot_hash,
                    approval_requests.c.decision,
                    approval_requests.c.expires_at,
                    agent_proposals.c.schema_version,
                    agent_proposals.c.fingerprint,
                    agent_proposals.c.proposal,
                    agent_proposals.c.gate_result,
                )
                .join(approval_requests, approval_requests.c.run_id == runs.c.id)
                .join(agent_proposals, agent_proposals.c.run_id == runs.c.id)
                .where(runs.c.id == run_id)
            ).first()
        if (
            row is None
            or row.decision != "approve"
            or row.state
            not in {
                RunState.EVALUATING_POLICY.value,
                RunState.READY_TO_EXECUTE.value,
                RunState.EXECUTING.value,
            }
        ):
            return None
        if row.expires_at <= datetime.now(UTC):
            raise ApprovalExpired("approval has expired")
        current = {
            "schema_version": row.schema_version,
            "fingerprint": row.fingerprint,
            "proposal": row.proposal,
            "gate_result": row.gate_result,
        }
        try:
            offer = offer_from_record(row.proposal)
        except InvalidProposalSnapshot as error:
            raise ApprovalConflict("invalid approved proposal") from error
        if (
            snapshot_hash(row.snapshot) != row.snapshot_hash
            or snapshot_hash(current) != row.snapshot_hash
            or offer.run_id != str(run_id)
            or offer.fingerprint() != row.fingerprint
        ):
            raise ApprovalConflict("approved proposal has changed")
        return row.fingerprint

    def get_execution_record(self, run_id: uuid.UUID) -> ExecutionRecord:
        with self._engine.connect() as conn:
            return self._execution_record(conn, run_id)

    def authorize_execution(self, run_id: uuid.UUID, *, context_hash: str) -> Run:
        """Move a validated proposal to ready only if its approved snapshot is unchanged."""
        now = datetime.now(UTC)
        with self._engine.begin() as conn:
            run = conn.execute(sa.select(runs).where(runs.c.id == run_id).with_for_update()).first()
            if run is None:
                raise RunNotFound(str(run_id))
            record = self._execution_record(conn, run_id)
            if run.state != RunState.EVALUATING_POLICY.value or record.context_hash != context_hash:
                raise ExecutionConflict("proposal is no longer eligible for execution")
            row = conn.execute(
                runs.update()
                .where(runs.c.id == run_id, runs.c.state == RunState.EVALUATING_POLICY.value)
                .values(state=RunState.READY_TO_EXECUTE.value, updated_at=now)
                .returning(*runs.c)
            ).first()
            if row is None:
                raise ExecutionConflict("run changed while authorizing execution")
            conn.execute(
                run_events.insert().values(
                    run_id=run_id,
                    from_state=RunState.EVALUATING_POLICY.value,
                    to_state=RunState.READY_TO_EXECUTE.value,
                    actor="worker",
                    detail={"context_hash": context_hash},
                    at=now,
                )
            )
        return _to_run(row)

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

    def claim_execution(
        self, run_id: uuid.UUID, *, binding_hash: str, context_hash: str
    ) -> uuid.UUID:
        """Claim a ready run once, atomically recording its transition and binding."""
        if len(binding_hash) != 64 or any(c not in "0123456789abcdef" for c in binding_hash):
            raise ExecutionConflict("execution binding must be a SHA-256 hash")
        claim_id = uuid.uuid4()
        now = datetime.now(UTC)
        with self._engine.begin() as conn:
            record = self._execution_record(conn, run_id)
            if record.state is not RunState.READY_TO_EXECUTE or record.context_hash != context_hash:
                raise ExecutionConflict("run is not ready to claim")
            row = conn.execute(
                runs.update()
                .where(runs.c.id == run_id, runs.c.state == RunState.READY_TO_EXECUTE.value)
                .values(state=RunState.EXECUTING.value, updated_at=now)
                .returning(runs.c.id)
            ).first()
            if row is None:
                raise ExecutionConflict("run is not ready to claim")
            conn.execute(
                execution_claims.insert().values(
                    run_id=run_id, claim_id=claim_id, binding_hash=binding_hash, created_at=now
                )
            )
            conn.execute(
                run_events.insert().values(
                    run_id=run_id,
                    from_state=RunState.READY_TO_EXECUTE.value,
                    to_state=RunState.EXECUTING.value,
                    actor="worker",
                    detail={"binding_hash": binding_hash},
                    at=now,
                )
            )
        return claim_id

    @staticmethod
    def _execution_record(conn: sa.Connection, run_id: uuid.UUID) -> ExecutionRecord:
        run = conn.execute(sa.select(runs).where(runs.c.id == run_id).with_for_update()).first()
        proposal_row = conn.execute(
            sa.select(agent_proposals).where(agent_proposals.c.run_id == run_id).with_for_update()
        ).first()
        approval = conn.execute(
            sa.select(approval_requests)
            .where(approval_requests.c.run_id == run_id)
            .with_for_update()
        ).first()
        if run is None or proposal_row is None:
            raise ExecutionConflict("proposal record is missing")
        try:
            offer = offer_from_record(proposal_row.proposal)
        except InvalidProposalSnapshot as error:
            raise ExecutionConflict("proposal record is invalid") from error
        current = {
            "schema_version": proposal_row.schema_version,
            "fingerprint": proposal_row.fingerprint,
            "proposal": proposal_row.proposal,
            "gate_result": proposal_row.gate_result,
        }
        if (
            offer.run_id != str(run_id)
            or offer.fingerprint() != proposal_row.fingerprint
            or proposal_row.proposal.get("action") != "draft_message"
            or not isinstance(proposal_row.proposal.get("cart_snapshot"), dict)
            or proposal_row.proposal.get("execution_channel") not in {"telegram", "mailpit"}
        ):
            raise ExecutionConflict("proposal binding is invalid")
        outcome = proposal_row.gate_result.get("outcome")
        human_approved = False
        approval_hash: str | None = None
        if outcome == Outcome.NEEDS_APPROVAL.value:
            if approval is None or approval.decision != "approve":
                raise ExecutionConflict("human approval is not present")
            if approval.expires_at <= datetime.now(UTC):
                raise ApprovalExpired("approval has expired")
            if (
                snapshot_hash(approval.snapshot) != approval.snapshot_hash
                or snapshot_hash(current) != approval.snapshot_hash
            ):
                raise ExecutionConflict("approved proposal has changed")
            human_approved = True
            approval_hash = approval.snapshot_hash
        elif outcome != Outcome.AUTO_APPROVE.value:
            raise ExecutionConflict("proposal did not pass the recorded policy gate")
        elif approval is not None and approval.decision == "reject":
            raise ExecutionConflict("proposal was rejected")
        return ExecutionRecord(
            run_id=run_id,
            state=RunState(run.state),
            schema_version=proposal_row.schema_version,
            fingerprint=proposal_row.fingerprint,
            proposal=proposal_row.proposal,
            gate_result=proposal_row.gate_result,
            human_approved=human_approved,
            approval_snapshot_hash=approval_hash,
            context_hash=snapshot_hash(
                {"record": current, "approval_snapshot_hash": approval_hash}
            ),
        )

    def start_effect(
        self,
        run_id: uuid.UUID,
        claim_id: uuid.UUID,
        *,
        binding_hash: str,
        effect: str,
        max_attempts: int,
    ) -> int:
        """Record an attempt before a request; an unfinished attempt is never retried."""
        if effect not in {"coupon", "message"} or (
            not isinstance(max_attempts, int)
            or isinstance(max_attempts, bool)
            or not 1 <= max_attempts <= 2**31 - 1
        ):
            raise ExecutionConflict("invalid effect or attempt limit")
        now = datetime.now(UTC)
        with self._engine.begin() as conn:
            self._lock_execution(conn, run_id, claim_id, binding_hash)
            existing = conn.execute(
                sa.select(execution_effects).where(
                    execution_effects.c.run_id == run_id, execution_effects.c.effect == effect
                )
            ).first()
            if existing is None:
                attempt = 1
                conn.execute(
                    execution_effects.insert().values(
                        run_id=run_id,
                        effect=effect,
                        status="started",
                        attempts=attempt,
                        max_attempts=max_attempts,
                        updated_at=now,
                    )
                )
            else:
                if (
                    existing.status != "not_sent"
                    or existing.max_attempts != max_attempts
                    or existing.attempts >= max_attempts
                ):
                    raise ExecutionConflict("effect is not eligible for retry")
                attempt = existing.attempts + 1
                conn.execute(
                    execution_effects.update()
                    .where(
                        execution_effects.c.run_id == run_id, execution_effects.c.effect == effect
                    )
                    .values(status="started", attempts=attempt, reference=None, updated_at=now)
                )
            self._execution_event(
                conn, run_id, {"effect": effect, "status": "started", "attempt": attempt}, now
            )
        return attempt

    def finish_effect(
        self,
        run_id: uuid.UUID,
        claim_id: uuid.UUID,
        *,
        binding_hash: str,
        effect: str,
        attempt: int,
        status: str,
        reference: str | None = None,
    ) -> None:
        """Record a known result; uncertainty is a terminal hold, not a retry."""
        if effect not in {"coupon", "message"} or status not in {
            "confirmed",
            "unknown",
            "not_sent",
        }:
            raise ExecutionConflict("invalid effect result")
        if not isinstance(attempt, int) or isinstance(attempt, bool) or attempt < 1:
            raise ExecutionConflict("invalid effect attempt")
        if status == "confirmed" and (not isinstance(reference, str) or not reference.strip()):
            raise ExecutionConflict("confirmed effects require a provider reference")
        if status != "confirmed" and reference is not None:
            raise ExecutionConflict("unconfirmed effects cannot have a provider reference")
        now = datetime.now(UTC)
        with self._engine.begin() as conn:
            self._lock_execution(conn, run_id, claim_id, binding_hash)
            changed = conn.execute(
                execution_effects.update()
                .where(
                    execution_effects.c.run_id == run_id,
                    execution_effects.c.effect == effect,
                    execution_effects.c.status == "started",
                    execution_effects.c.attempts == attempt,
                )
                .values(status=status, reference=reference, updated_at=now)
                .returning(execution_effects.c.run_id)
            ).first()
            if changed is None:
                raise ExecutionConflict("effect has no active attempt")
            self._execution_event(
                conn, run_id, {"effect": effect, "status": status, "attempt": attempt}, now
            )

    def complete_execution(
        self, run_id: uuid.UUID, claim_id: uuid.UUID, *, binding_hash: str, needs_coupon: bool
    ) -> Run:
        """Complete only after every required effect has a confirmed result."""
        now = datetime.now(UTC)
        with self._engine.begin() as conn:
            self._lock_execution(conn, run_id, claim_id, binding_hash)
            effects = conn.execute(
                sa.select(execution_effects.c.effect, execution_effects.c.status).where(
                    execution_effects.c.run_id == run_id
                )
            ).all()
            required = {"message", "coupon"} if needs_coupon else {"message"}
            statuses = {row.effect: row.status for row in effects}
            if statuses != {effect: "confirmed" for effect in required}:
                raise ExecutionConflict("required effects are not all confirmed")
            row = conn.execute(
                runs.update()
                .where(runs.c.id == run_id, runs.c.state == RunState.EXECUTING.value)
                .values(
                    state=RunState.COMPLETED.value,
                    outcome="coupon_and_message_confirmed" if needs_coupon else "message_confirmed",
                    updated_at=now,
                )
                .returning(*runs.c)
            ).first()
            if row is None:
                raise ExecutionConflict("run is no longer executing")
            conn.execute(
                run_events.insert().values(
                    run_id=run_id,
                    from_state=RunState.EXECUTING.value,
                    to_state=RunState.COMPLETED.value,
                    actor="worker",
                    detail={"effects": sorted(required)},
                    at=now,
                )
            )
        return _to_run(row)

    @staticmethod
    def _lock_execution(
        conn: sa.Connection, run_id: uuid.UUID, claim_id: uuid.UUID, binding_hash: str
    ) -> None:
        claim = conn.execute(
            sa.select(execution_claims.c.claim_id, execution_claims.c.binding_hash, runs.c.state)
            .join(runs, runs.c.id == execution_claims.c.run_id)
            .where(execution_claims.c.run_id == run_id)
            .with_for_update(of=runs)
        ).first()
        if (
            claim is None
            or claim.claim_id != claim_id
            or claim.binding_hash != binding_hash
            or claim.state != RunState.EXECUTING.value
        ):
            raise ExecutionConflict("execution claim or binding is not current")

    @staticmethod
    def _execution_event(
        conn: sa.Connection, run_id: uuid.UUID, detail: dict[str, Any], now: datetime
    ) -> None:
        conn.execute(
            run_events.insert().values(
                run_id=run_id,
                from_state=RunState.EXECUTING.value,
                to_state=RunState.EXECUTING.value,
                actor="worker",
                detail=detail,
                at=now,
            )
        )

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


# Child tables in foreign-key-safe delete order. Several FKs from migrations 0001-0010 do not
# cascade, so each child is deleted explicitly before `runs`. `approval_requests`,
# `execution_claims`, and `execution_effects` cascade, but are listed for clarity.
_RUN_CHILD_DELETES = (
    "DELETE FROM proposal_calls WHERE run_id = ANY(:ids)",
    "DELETE FROM proposal_budget WHERE run_id = ANY(:ids)",
    "DELETE FROM capacity_proposal_claim WHERE run_id = ANY(:ids)",
    "DELETE FROM execution_effects WHERE run_id = ANY(:ids)",
    "DELETE FROM execution_claims WHERE run_id = ANY(:ids)",
    "DELETE FROM approval_requests WHERE run_id = ANY(:ids)",
    "DELETE FROM agent_proposals WHERE run_id = ANY(:ids)",
    "DELETE FROM run_events WHERE run_id = ANY(:ids)",
    # `gate_coupon_reservations.run_id` is text, not a foreign key.
    "DELETE FROM gate_coupon_reservations WHERE run_id = ANY(:text_ids)",
)


def delete_runs_in_transaction(conn: Connection, run_ids: list[uuid.UUID]) -> int:
    """Delete runs and every dependent Engine DB row inside the caller's transaction."""
    if not run_ids:
        return 0
    params = {"ids": list(run_ids), "text_ids": [str(run_id) for run_id in run_ids]}
    for statement in _RUN_CHILD_DELETES:
        conn.execute(sa.text(statement), params)
    deleted = conn.execute(runs.delete().where(runs.c.id.in_(run_ids)).returning(runs.c.id)).all()
    return len(deleted)
