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

    def public(self, now: datetime) -> dict[str, object]:
        """REC-11: codes, counts, and ages only. Never the Message-ID, a name, or a destination."""
        return {
            "effect": self.effect,
            "status": self.status,
            "attempts": self.attempts,
            "age_minutes": max(int((now - self.updated_at).total_seconds() // 60), 0),
            "channel": self.channel,
            "message_id_stored": self.message_id is not None,
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
