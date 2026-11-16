"""Durable engine runtime state: active, fallback_fixed_rule, halted (#87, spec KS-1..KS-8).

The API only writes this state; the worker reads it before every customer-facing step
(ADR-0012, ADR-0022). Any failure to read it is treated as `halted` (fail closed).
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any

import sqlalchemy as sa
from sqlalchemy import Engine

from engine.db import engine_runtime_events, engine_runtime_state

LOG = logging.getLogger(__name__)
SINGLETON_ID = 1
MIN_REASON_LENGTH = 3
MAX_REASON_LENGTH = 200
_LONG_DIGITS = re.compile(r"\d[\s().-]*(?:\d[\s().-]*){6,}")


class RuntimeState(StrEnum):
    ACTIVE = "active"
    FALLBACK = "fallback_fixed_rule"
    HALTED = "halted"


class RuntimeStateError(Exception):
    """The requested state change is invalid. Messages never contain the reason text."""


class RuntimeStateConflict(RuntimeStateError):
    """Another change won first, or the current state is not an allowed starting point."""


@dataclass(frozen=True)
class RuntimeSnapshot:
    state: RuntimeState
    reason: str
    changed_by: str
    changed_at: datetime
    version: int

    def as_dict(self) -> dict[str, Any]:
        return {
            "state": self.state.value,
            "reason": self.reason,
            "changed_by": self.changed_by,
            "changed_at": self.changed_at.isoformat(),
            "version": self.version,
        }


def validate_reason(reason: str) -> str:
    """KS-7: a short, single-line, non-PII reason; contact-like text is rejected."""
    value = reason.strip() if isinstance(reason, str) else ""
    if (
        not MIN_REASON_LENGTH <= len(value) <= MAX_REASON_LENGTH
        or not value.isprintable()
        or "@" in value
        or _LONG_DIGITS.search(value)
    ):
        raise RuntimeStateError("reason must be 3-200 printable characters without contact data")
    return value


def _snapshot(row: Any) -> RuntimeSnapshot:
    return RuntimeSnapshot(
        state=RuntimeState(row.state),
        reason=row.reason,
        changed_by=row.changed_by,
        changed_at=row.changed_at,
        version=row.version,
    )


class RuntimeStateRepository:
    def __init__(self, engine: Engine) -> None:
        self._engine = engine

    def get(self) -> RuntimeSnapshot:
        with self._engine.connect() as conn:
            row = conn.execute(
                sa.select(engine_runtime_state).where(engine_runtime_state.c.id == SINGLETON_ID)
            ).first()
        if row is None:
            raise RuntimeStateError("runtime state row is missing")
        return _snapshot(row)

    def set_state(
        self,
        new: RuntimeState,
        *,
        actor: str,
        reason: str,
        expected_version: int | None = None,
        allowed_from: frozenset[RuntimeState] | None = None,
    ) -> RuntimeSnapshot:
        """Change the state in one locked transaction and record the audit event."""
        if not isinstance(new, RuntimeState):
            raise RuntimeStateError("invalid runtime state")
        if not actor.strip() or len(actor) > 128:
            raise RuntimeStateError("actor must be a non-empty identifier")
        text = validate_reason(reason)
        now = datetime.now(UTC)
        with self._engine.begin() as conn:
            row = conn.execute(
                sa.select(engine_runtime_state)
                .where(engine_runtime_state.c.id == SINGLETON_ID)
                .with_for_update()
            ).first()
            if row is None:
                raise RuntimeStateError("runtime state row is missing")
            current = RuntimeState(row.state)
            if expected_version is not None and expected_version != row.version:
                raise RuntimeStateConflict("runtime state changed since it was read")
            if allowed_from is not None and current not in allowed_from:
                raise RuntimeStateConflict("current state does not allow this change")
            if current is new:
                raise RuntimeStateConflict("runtime state is already set to that value")
            version = row.version + 1
            conn.execute(
                engine_runtime_state.update()
                .where(engine_runtime_state.c.id == SINGLETON_ID)
                .values(
                    state=new.value,
                    reason=text,
                    changed_by=actor,
                    changed_at=now,
                    version=version,
                )
            )
            conn.execute(
                engine_runtime_events.insert().values(
                    from_state=current.value,
                    to_state=new.value,
                    actor=actor,
                    reason=text,
                    version=version,
                    at=now,
                )
            )
        return RuntimeSnapshot(new, text, actor, now, version)

    def history(self, limit: int = 50) -> list[dict[str, Any]]:
        if not 1 <= limit <= 500:
            raise ValueError("history limit must be between 1 and 500")
        with self._engine.connect() as conn:
            rows = conn.execute(
                sa.select(engine_runtime_events)
                .order_by(engine_runtime_events.c.id.desc())
                .limit(limit)
            ).all()
        return [
            {
                "from_state": row.from_state,
                "to_state": row.to_state,
                "actor": row.actor,
                "reason": row.reason,
                "version": row.version,
                "at": row.at.isoformat(),
            }
            for row in rows
        ]


class EngineHalted(Exception):
    """The engine is halted (or its state is unreadable); nothing customer-facing may run."""


class RuntimeGuard:
    """Reads the state before a customer-facing step. Any read error means `halted` (KS-6)."""

    def __init__(self, source: Any) -> None:
        # Any object with a `get()` (the repository or a test double) or a plain callable.
        self._read = source.get if hasattr(source, "get") else source

    def state(self) -> RuntimeState:
        try:
            return self._read().state
        except Exception:
            LOG.warning("runtime state unreadable; treating the engine as halted")
            return RuntimeState.HALTED

    def require_not_halted(self) -> RuntimeState:
        state = self.state()
        if state is RuntimeState.HALTED:
            raise EngineHalted("engine is halted")
        return state
