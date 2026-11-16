"""Circuit breaker: trips active to fallback_fixed_rule only (#87, ADR-0022, spec KS-5).

Every number is a required setting with no default, and the breaker is off until the owner
sets them from the measured baseline. It never re-enables and never halts.
"""

from __future__ import annotations

import logging
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import Decimal, InvalidOperation

import sqlalchemy as sa
from sqlalchemy import Engine

from engine.proposal import PROVIDER_FAILURE_REASONS
from engine.runtime_state import (
    RuntimeState,
    RuntimeStateConflict,
    RuntimeStateError,
    RuntimeStateRepository,
)

LOG = logging.getLogger(__name__)
BREAKER_ACTOR = "circuit-breaker"

# Attempts are runs that reserved a model budget (only the LLM path does), so fixed-rule runs
# never dilute the failure rate or the latency. Latency is generating_proposal to
# evaluating_policy; failed runs have no such event and count only as failures.
_READING_SQL = """
WITH attempts AS (
    SELECT r.id, r.state, r.failure_reason, g.at AS started
    FROM runs r
    JOIN proposal_budget b ON b.run_id = r.id
    JOIN LATERAL (
        SELECT min(at) AS at FROM run_events
        WHERE run_id = r.id AND to_state = 'generating_proposal'
    ) g ON true
    WHERE g.at >= :since AND g.at <= :now
), timed AS (
    SELECT a.id, EXTRACT(EPOCH FROM (min(e.at) - a.started)) AS secs
    FROM attempts a
    JOIN run_events e
      ON e.run_id = a.id AND e.to_state = 'evaluating_policy' AND e.at >= a.started
    GROUP BY a.id, a.started
)
SELECT
    (SELECT count(*) FROM attempts) AS attempts,
    (SELECT count(*) FROM attempts
      WHERE state = 'failed' AND failure_reason = ANY(:reasons)) AS failures,
    (SELECT percentile_cont(0.95) WITHIN GROUP (ORDER BY secs) FROM timed) AS p95
"""


@dataclass(frozen=True)
class BreakerSettings:
    window_minutes: int
    min_sample: int
    max_failure_rate_percent: Decimal
    max_p95_seconds: Decimal

    def __post_init__(self) -> None:
        if not 1 <= self.window_minutes <= 1440:
            raise ValueError("BREAKER_WINDOW_MINUTES must be between 1 and 1440")
        if not 1 <= self.min_sample <= 100_000:
            raise ValueError("BREAKER_MIN_SAMPLE must be between 1 and 100000")
        if not Decimal(0) < self.max_failure_rate_percent <= Decimal(100):
            raise ValueError("BREAKER_MAX_FAILURE_RATE_PERCENT must be above 0 and at most 100")
        if not self.max_p95_seconds.is_finite() or self.max_p95_seconds <= 0:
            raise ValueError("BREAKER_MAX_P95_SECONDS must be positive")

    @classmethod
    def from_env(cls, env: Mapping[str, str]) -> BreakerSettings | None:
        """None means the breaker is off. When enabled, every number must be set explicitly."""
        flag = env.get("BREAKER_ENABLED", "").strip()
        if flag in {"", "NO"}:
            return None
        if flag != "YES":
            raise ValueError("BREAKER_ENABLED must be YES or NO")
        missing = [
            name
            for name in (
                "BREAKER_WINDOW_MINUTES",
                "BREAKER_MIN_SAMPLE",
                "BREAKER_MAX_FAILURE_RATE_PERCENT",
                "BREAKER_MAX_P95_SECONDS",
            )
            if not env.get(name, "").strip()
        ]
        if missing:
            raise ValueError(f"breaker settings must be set explicitly: {', '.join(missing)}")
        try:
            return cls(
                window_minutes=int(env["BREAKER_WINDOW_MINUTES"]),
                min_sample=int(env["BREAKER_MIN_SAMPLE"]),
                max_failure_rate_percent=Decimal(env["BREAKER_MAX_FAILURE_RATE_PERCENT"]),
                max_p95_seconds=Decimal(env["BREAKER_MAX_P95_SECONDS"]),
            )
        except (ValueError, InvalidOperation) as error:
            raise ValueError("breaker settings must be valid numbers") from error


@dataclass(frozen=True)
class BreakerReading:
    attempts: int
    failures: int
    p95_seconds: Decimal | None

    @property
    def failure_rate_percent(self) -> Decimal:
        if self.attempts == 0:
            return Decimal(0)
        return Decimal(self.failures) * 100 / Decimal(self.attempts)


def read_window(engine: Engine, settings: BreakerSettings, *, now: datetime) -> BreakerReading:
    if now.tzinfo is None:
        raise ValueError("breaker time must be timezone-aware")
    with engine.connect() as conn:
        row = conn.execute(
            sa.text(_READING_SQL),
            {
                "since": now - timedelta(minutes=settings.window_minutes),
                "now": now,
                "reasons": sorted(PROVIDER_FAILURE_REASONS),
            },
        ).one()
    p95 = Decimal(str(row.p95)) if row.p95 is not None else None
    return BreakerReading(int(row.attempts), int(row.failures), p95)


def trip_reason(reading: BreakerReading, settings: BreakerSettings) -> str | None:
    """A short, non-PII reason when a threshold is crossed with enough samples, else None."""
    if reading.attempts < settings.min_sample:
        return None
    if reading.failure_rate_percent > settings.max_failure_rate_percent:
        return (
            f"provider failure rate {reading.failure_rate_percent:.1f}% over "
            f"{reading.attempts} attempts exceeds {settings.max_failure_rate_percent}%"
        )
    if reading.p95_seconds is not None and reading.p95_seconds > settings.max_p95_seconds:
        return (
            f"P95 generation time {reading.p95_seconds:.1f}s exceeds "
            f"{settings.max_p95_seconds}s over {reading.attempts} attempts"
        )
    return None


def run_breaker(
    engine: Engine,
    repo: RuntimeStateRepository,
    settings: BreakerSettings | None,
    *,
    now: datetime,
) -> bool:
    """Trip active to fallback_fixed_rule when a threshold is crossed. Returns True on a trip."""
    if settings is None:
        return False
    if repo.get().state is not RuntimeState.ACTIVE:
        return False
    reason = trip_reason(read_window(engine, settings, now=now), settings)
    if reason is None:
        return False
    try:
        repo.set_state(
            RuntimeState.FALLBACK,
            actor=BREAKER_ACTOR,
            reason=reason,
            allowed_from=frozenset({RuntimeState.ACTIVE}),
        )
    except RuntimeStateConflict:
        return False  # an admin changed the state first
    except RuntimeStateError:
        LOG.warning("breaker could not record its trip")
        return False
    LOG.warning("stage=breaker status=tripped state=fallback_fixed_rule")
    return True
