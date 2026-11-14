from __future__ import annotations

import uuid
from datetime import datetime
from decimal import Decimal
from zoneinfo import ZoneInfo

import pytest

from engine.policy import GateConfig
from engine.proposal import ProposalSettings
from engine.proposal_budget import Rate, ReviewBudget
from scripts.poc3_live_e2e import (
    LIVE_E2E_BUDGET_LIMIT_USD,
    LIVE_E2E_REVIEW_ID,
    validate_live_e2e_gate,
    validate_live_e2e_proposal_settings,
)


def _valid_settings() -> ProposalSettings:
    return ProposalSettings(
        synthetic_only=True,
        budget_usd=Decimal("0.50"),
        input_tokens=4000,
        output_tokens=8192,
        rate=Rate(Decimal("0.125"), Decimal("0.50"), "F-005"),
        review_budget=ReviewBudget(LIVE_E2E_REVIEW_ID, LIVE_E2E_BUDGET_LIMIT_USD),
    )


def _valid_gate() -> GateConfig:
    return GateConfig(
        timezone=ZoneInfo("Asia/Riyadh"),
        currency="USD",
        daily_budget=Decimal("450"),
        category_min_margin={"Furniture / Office": Decimal("0.20")},
    )


def test_poc3_live_e2e_settings_require_bounded_review_and_ceilings() -> None:
    validate_live_e2e_proposal_settings(_valid_settings())

    wrong_review = ProposalSettings(
        synthetic_only=True,
        budget_usd=Decimal("0.50"),
        input_tokens=4000,
        output_tokens=8192,
        rate=Rate(Decimal("0.125"), Decimal("0.50"), "F-005"),
        review_budget=ReviewBudget("other-review", Decimal("0.50")),
    )
    with pytest.raises(ValueError, match="PROPOSAL_REVIEW_ID"):
        validate_live_e2e_proposal_settings(wrong_review)

    over_tokens = ProposalSettings(
        synthetic_only=True,
        budget_usd=Decimal("0.50"),
        input_tokens=4001,
        output_tokens=8192,
        rate=Rate(Decimal("0.125"), Decimal("0.50"), "F-005"),
        review_budget=ReviewBudget(LIVE_E2E_REVIEW_ID, LIVE_E2E_BUDGET_LIMIT_USD),
    )
    with pytest.raises(ValueError, match="token limits"):
        validate_live_e2e_proposal_settings(over_tokens)


def test_poc3_live_e2e_gate_validation_requires_approved_config() -> None:
    validate_live_e2e_gate(_valid_gate())

    bad_gate = GateConfig(
        timezone=ZoneInfo("UTC"),
        currency="USD",
        daily_budget=Decimal("450"),
        category_min_margin={"Furniture / Office": Decimal("0.20")},
    )
    with pytest.raises(ValueError, match="approved synthetic financial gate"):
        validate_live_e2e_gate(bad_gate)


def test_poc3_live_e2e_approve_api_requires_token_and_run_id(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from scripts.poc3_live_e2e import main

    monkeypatch.delenv("APPROVAL_TOKEN", raising=False)
    with pytest.raises(ValueError, match="--run-id and --snapshot-hash"):
        main(["approve-api"])

    with pytest.raises(ValueError, match="APPROVAL_TOKEN"):
        main(
            [
                "approve-api",
                "--run-id",
                str(uuid.uuid4()),
                "--snapshot-hash",
                "a" * 64,
            ]
        )


def test_poc3_live_e2e_preflight_requires_channel(monkeypatch: pytest.MonkeyPatch) -> None:
    from scripts.poc3_live_e2e import main

    monkeypatch.delenv("FULL_STACK_SYNTHETIC_ISOLATED", raising=False)
    with pytest.raises(ValueError):
        main(["preflight"])
    assert isinstance(datetime.now().isoformat(), str)
