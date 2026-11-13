from __future__ import annotations

from copy import deepcopy
from decimal import Decimal

import pytest

from engine.proposal import ProposalSettings
from engine.proposal_budget import Rate, ReviewBudget
from scripts.poc2_evaluation_cases import CASE_IDS, approved_cases, reviewed_cases
from scripts.poc2_evaluation_runner import fixture_cart, validate_settings
from scripts.s2_llm_eval import load_dataset, load_reviews


def test_poc2_cases_are_reviewed_and_cover_the_approved_matrix() -> None:
    cases = approved_cases()
    assert tuple(case.case_id for case in cases[:15]) == CASE_IDS
    assert len(cases) == 17
    assert {case.dialect for case in cases if case.dialect} == {
        "hijazi",
        "najdi",
        "emirati",
        "egyptian",
        "levantine",
    }
    assert {case.language for case in cases} == {"en", "ar"}
    assert all(case.expected_action is None for case in cases if "injection" in case.case_id)


def test_poc2_cases_reject_missing_or_wrong_dialect_review() -> None:
    dataset = load_dataset()
    reviews = load_reviews(dataset)
    invalid = deepcopy(reviews)
    records = invalid["cases"]
    assert isinstance(records, dict)
    records["dialect-najdi-001-ar_najdi"]["reviewer_id"] = "not-assigned"
    with pytest.raises(ValueError, match="not approved"):
        reviewed_cases(dataset, invalid)
    records.pop("enmsa-001-en")
    with pytest.raises(ValueError, match="not approved"):
        reviewed_cases(dataset, invalid)


def test_poc2_runtime_fixture_preserves_reviewed_price() -> None:
    dataset = load_dataset()
    price_case = next(case for case in approved_cases() if case.case_id == "enmsa-002-en")
    cart = fixture_cart(dataset, price_case)
    assert cart.amount_total == Decimal("129.95")
    assert cart.lines[0].unit_price == cart.amount_total
    assert cart.partner_email.endswith("@example.test")


def test_poc2_live_settings_require_the_fixed_review_budget_and_limits() -> None:
    rate = Rate(Decimal("0.125"), Decimal("0.5"), "test")
    settings = ProposalSettings(
        True, Decimal("1"), 4000, 8192, rate, ReviewBudget("poc2-69-evaluation", Decimal("1"))
    )
    validate_settings(settings)
    with pytest.raises(ValueError, match="aggregate budget"):
        validate_settings(
            ProposalSettings(
                True, Decimal("1"), 4000, 8192, rate, ReviewBudget("other", Decimal(1))
            )
        )
