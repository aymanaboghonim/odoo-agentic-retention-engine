"""Held effects and their evidence (#131; spec docs/specs/execution-reconciliation.md, ADR-0030).

This module has no side effects: settings, the evidence vocabulary, and the tokenized
held-runs view the approval API returns (REC-11). The read-only lookups against Odoo and
Mailpit are in `engine.reconcile_cli` (REC-1); resolution is #132.
"""

from __future__ import annotations

import uuid
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Literal

Evidence = Literal["present", "absent", "unknown", "attestation_required"]
EVIDENCE: tuple[Evidence, ...] = ("present", "absent", "unknown", "attestation_required")

STALE_KEY = "RECONCILE_STALE_STARTED_MINUTES"
MAX_STALE_MINUTES = 7 * 24 * 60


class ReconcileConfigError(ValueError):
    pass


@dataclass(frozen=True)
class ReconcileSettings:
    """Required, no default, fail closed (spec: Settings)."""

    stale_started_minutes: int

    @classmethod
    def from_env(cls, env: Mapping[str, str]) -> ReconcileSettings:
        raw = env.get(STALE_KEY, "").strip()
        if not raw.isdigit() or not 1 <= int(raw) <= MAX_STALE_MINUTES:
            raise ReconcileConfigError(
                f"{STALE_KEY} must be an integer from 1 to {MAX_STALE_MINUTES}"
            )
        return cls(stale_started_minutes=int(raw))

    def stale_before(self, now: datetime) -> datetime:
        return now - timedelta(minutes=self.stale_started_minutes)


@dataclass(frozen=True)
class HeldEffect:
    """One held effect: `unknown`, or `started` past the stale limit, on an executing run."""

    run_id: uuid.UUID
    effect: Literal["coupon", "message"]
    status: Literal["unknown", "started"]
    attempts: int
    updated_at: datetime
    channel: Literal["telegram", "mailpit"]
    message_id: str | None
    evidence: Evidence | None = None  # latest from the worker stage (#132)

    def public(self, now: datetime) -> dict[str, object]:
        """REC-11: codes, counts, and ages only. Never the Message-ID, a name, or a destination."""
        return {
            "effect": self.effect,
            "status": self.status,
            "attempts": self.attempts,
            "age_minutes": max(int((now - self.updated_at).total_seconds() // 60), 0),
            "channel": self.channel,
            "message_id_stored": self.message_id is not None,
            "evidence": self.evidence,
        }


def held_runs_view(effects: list[HeldEffect], now: datetime) -> list[dict[str, object]]:
    """Group held effects by run for `GET /held-runs` (oldest first)."""
    grouped: dict[uuid.UUID, list[HeldEffect]] = {}
    for effect in sorted(effects, key=lambda e: (e.updated_at, str(e.run_id), e.effect)):
        grouped.setdefault(effect.run_id, []).append(effect)
    return [
        {
            "run_id": str(run_id),
            "state": "executing",
            "held_effects": [e.public(now) for e in items],
        }
        for run_id, items in grouped.items()
    ]


# ---------------------------------------------------------------- #132 resolution rules

Resolution = Literal["confirmed", "not_sent"]
Basis = Literal["evidence", "attestation"]
# Run results of a resolution (REC-4, REC-9); also the `aorte.resolution` metric label values.
RESOLUTIONS = (
    "completed",
    "message_not_sent",
    "coupon_not_created",
    "unverified",
    "decision_rejected",
    "coupon_deactivated",
)
STAGE_KEY = "RECONCILE_STAGE"
INTERVAL_KEY = "RECONCILE_INTERVAL_MINUTES"
DEADLINE_KEY = "RECONCILE_DEADLINE_HOURS"


@dataclass(frozen=True)
class StageSettings:
    """The worker stage (#132). `RECONCILE_STAGE` is required (YES or NO, no default); with
    YES the interval and deadline are required too (fail closed)."""

    interval_minutes: int
    deadline_hours: int

    @classmethod
    def from_env(cls, env: Mapping[str, str]) -> StageSettings | None:
        flag = env.get(STAGE_KEY, "").strip()
        if flag == "NO":
            return None
        if flag != "YES":
            raise ReconcileConfigError(f"{STAGE_KEY} must be YES or NO")
        values = {}
        for key, high in ((INTERVAL_KEY, 24 * 60), (DEADLINE_KEY, 30 * 24)):
            raw = env.get(key, "").strip()
            if not raw.isdigit() or not 1 <= int(raw) <= high:
                raise ReconcileConfigError(f"{key} must be an integer from 1 to {high}")
            values[key] = int(raw)
        return cls(interval_minutes=values[INTERVAL_KEY], deadline_hours=values[DEADLINE_KEY])

    def deadline_before(self, now: datetime) -> datetime:
        return now - timedelta(hours=self.deadline_hours)


@dataclass(frozen=True)
class RunOutcome:
    state: Literal["executing", "completed", "failed"]
    reason: str | None  # failure reason or completion outcome
    resolution: str | None  # metric label (RESOLUTIONS)
    orphan_coupon: bool = False


def run_outcome(statuses: Mapping[str, str], *, needs_coupon: bool, deadline: bool) -> RunOutcome:
    """The run's result once its effects are resolved (ADR-0030 decisions 2, 4, 5).

    An effect still `unknown` or `started` keeps the run held, unless the deadline passed:
    then the run fails as `reconciled_unverified` and a confirmed coupon stays active.
    """
    if any(status in {"unknown", "started"} for status in statuses.values()):
        if deadline:
            return RunOutcome("failed", "reconciled_unverified", "unverified")
        return RunOutcome("executing", None, None)
    coupon, message = statuses.get("coupon"), statuses.get("message")
    if needs_coupon and coupon != "confirmed":
        return RunOutcome("failed", "reconciled_coupon_not_created", "coupon_not_created")
    if message == "confirmed":
        outcome = "coupon_and_message_confirmed" if needs_coupon else "message_confirmed"
        return RunOutcome("completed", outcome, "completed")
    # The message was proven not sent, or never started (the execution stopped at the coupon).
    return RunOutcome(
        "failed", "reconciled_message_not_sent", "message_not_sent", orphan_coupon=needs_coupon
    )


def accept_decision(decision: Resolution, basis: Basis, evidence: Evidence) -> bool:
    """REC-6: `confirmed` needs `present` or an attestation; `not_sent` needs `absent` or an
    attestation. An attestation never overrides definitive evidence to the contrary."""
    if decision == "confirmed":
        return evidence == "present" or (basis == "attestation" and evidence != "absent")
    return evidence == "absent" or (basis == "attestation" and evidence != "present")


def evidence_resolution(evidence: Evidence) -> Resolution | None:
    """REC-4: only definitive evidence resolves without a person."""
    return {"present": "confirmed", "absent": "not_sent"}.get(evidence)  # type: ignore[return-value]
