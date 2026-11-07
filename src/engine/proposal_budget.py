from __future__ import annotations

import uuid
from dataclasses import dataclass
from decimal import ROUND_CEILING, Decimal

import sqlalchemy as sa
from sqlalchemy import Engine


class BudgetError(Exception):
    """A proposal request cannot safely use the configured run budget."""


def _positive(value: Decimal, name: str) -> None:
    if (
        not value.is_finite()
        or value <= 0
        or value >= Decimal("1000000000000")
        or value != value.quantize(Decimal("0.000000000001"))
    ):
        raise BudgetError(f"{name} must be positive and fit numeric(24,12)")


@dataclass(frozen=True)
class Rate:
    input_per_million: Decimal
    output_per_million: Decimal
    provenance: str

    def __post_init__(self) -> None:
        _positive(self.input_per_million, "input rate")
        _positive(self.output_per_million, "output rate")
        if not self.provenance.strip():
            raise BudgetError("rate provenance is required")

    def estimate(self, input_tokens: int, output_tokens: int) -> Decimal:
        if (
            not isinstance(input_tokens, int)
            or not isinstance(output_tokens, int)
            or isinstance(input_tokens, bool)
            or isinstance(output_tokens, bool)
            or input_tokens < 0
            or output_tokens < 0
        ):
            raise BudgetError("token counts must be nonnegative integers")
        return (
            self.input_per_million * input_tokens + self.output_per_million * output_tokens
        ) / Decimal(1_000_000)


class ProposalBudget:
    def __init__(self, engine: Engine) -> None:
        self._engine = engine

    def reserve(
        self,
        run_id: uuid.UUID,
        call_id: uuid.UUID,
        *,
        limit: Decimal,
        input_limit: int,
        output_limit: int,
        rate: Rate,
    ) -> Decimal:
        _positive(limit, "run budget")
        if (
            not isinstance(input_limit, int)
            or isinstance(input_limit, bool)
            or not isinstance(output_limit, int)
            or isinstance(output_limit, bool)
            or not 1 <= input_limit <= 2**63 - 1
            or not 1 <= output_limit <= 2**63 - 1
        ):
            raise BudgetError("token limits must be positive")
        reserved = rate.estimate(input_limit, output_limit).quantize(
            Decimal("0.000000000001"), rounding=ROUND_CEILING
        )
        with self._engine.begin() as conn:
            conn.execute(
                sa.text(
                    "INSERT INTO proposal_budget (run_id, limit_usd) VALUES (:run, :limit) "
                    "ON CONFLICT (run_id) DO NOTHING"
                ),
                {"run": run_id, "limit": limit},
            )
            stored_limit = conn.execute(
                sa.text("SELECT limit_usd FROM proposal_budget WHERE run_id=:run FOR UPDATE"),
                {"run": run_id},
            ).scalar_one()
            if stored_limit != limit:
                raise BudgetError("run budget cannot change after first reservation")
            used = conn.execute(
                sa.text(
                    "SELECT COALESCE(SUM(COALESCE(actual_usd, reserved_usd)), 0) "
                    "FROM proposal_calls WHERE run_id=:run"
                ),
                {"run": run_id},
            ).scalar_one()
            if used + reserved > limit:
                raise BudgetError("per-run budget exhausted")
            existing = conn.execute(
                sa.text("SELECT 1 FROM proposal_calls WHERE id=:id"), {"id": call_id}
            ).first()
            if existing is not None:
                raise BudgetError("call ID already exists; refusing duplicate provider request")
            conn.execute(
                sa.text(
                    "INSERT INTO proposal_calls "
                    "(id, run_id, status, reserved_usd, rate_provenance, input_rate, "
                    "output_rate, input_limit, output_limit) "
                    "VALUES (:id, :run, 'reserved', :reserved, :provenance, "
                    ":input_rate, :output_rate, :input_limit, :output_limit)"
                ),
                {
                    "id": call_id,
                    "run": run_id,
                    "reserved": reserved,
                    "provenance": rate.provenance,
                    "input_rate": rate.input_per_million,
                    "output_rate": rate.output_per_million,
                    "input_limit": input_limit,
                    "output_limit": output_limit,
                },
            )
        return reserved

    def settle(
        self,
        call_id: uuid.UUID,
        *,
        input_tokens: int,
        output_tokens: int,
        rate: Rate,
    ) -> bool:
        actual = rate.estimate(input_tokens, output_tokens).quantize(
            Decimal("0.000000000001"), rounding=ROUND_CEILING
        )
        with self._engine.begin() as conn:
            row = (
                conn.execute(
                    sa.text("SELECT * FROM proposal_calls WHERE id=:id FOR UPDATE"), {"id": call_id}
                )
                .mappings()
                .one()
            )
            if (
                row["status"] != "reserved"
                or row["rate_provenance"] != rate.provenance
                or row["input_rate"] != rate.input_per_million
                or row["output_rate"] != rate.output_per_million
            ):
                raise BudgetError("call cannot be reconciled with this rate")
            over_reservation = (
                actual > row["reserved_usd"]
                or input_tokens > row["input_limit"]
                or output_tokens > row["output_limit"]
            )
            conn.execute(
                sa.text(
                    "UPDATE proposal_calls SET status='settled', actual_usd=:actual, "
                    "input_tokens=:input, output_tokens=:output, failure_reason=:reason "
                    "WHERE id=:id"
                ),
                {
                    "id": call_id,
                    "actual": actual,
                    "input": input_tokens,
                    "output": output_tokens,
                    "reason": "reservation_exceeded" if over_reservation else None,
                },
            )
        return not over_reservation

    def mark_unknown(self, call_id: uuid.UUID, reason: str) -> None:
        if not reason.strip():
            raise BudgetError("failure reason is required")
        with self._engine.begin() as conn:
            row = conn.execute(
                sa.text(
                    "UPDATE proposal_calls SET status='unknown', failure_reason=:reason "
                    "WHERE id=:id AND status='reserved' RETURNING id"
                ),
                {"id": call_id, "reason": reason},
            ).first()
            if row is None:
                raise BudgetError("call is not reserved")
