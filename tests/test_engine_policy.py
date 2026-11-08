from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal

import pytest

from engine.gate_ledger import InMemoryGateLedger
from engine.policy import (
    Gate,
    GateApproval,
    GateConfig,
    GateConfigError,
    GateRejected,
    OfferProposal,
    Outcome,
    Reason,
    evaluate,
)
from engine.ports import GatedExecutor

NOW = datetime(2026, 11, 4, 12, 0, tzinfo=UTC)
ENV = {
    "GATE_TIMEZONE": "Asia/Riyadh",
    "GATE_CURRENCY": "USD",
    "GATE_DAILY_BUDGET": "100",
    "GATE_CATEGORY_MIN_MARGINS": '{"decor": "0.20"}',
}


def config(**overrides: str) -> GateConfig:
    return GateConfig.from_env({**ENV, **overrides})


def proposal(percent: str | None = "5", **overrides: object) -> OfferProposal:
    base: dict[str, object] = {
        "run_id": "run-1",
        "customer_id": "cust-1",
        "cart_id": "cart-1",
        "cart_owner_id": "cust-1",
        "category": "decor",
        "currency": "USD",
        "eligible_total": Decimal("100"),
        "eligible_unit_cost_total": Decimal("40"),
        "proposed_discount_percent": None if percent is None else Decimal(percent),
    }
    base.update(overrides)
    return OfferProposal(**base)  # type: ignore[arg-type]


def decide(p: OfferProposal, **env: str):  # type: ignore[no-untyped-def]
    return evaluate(p, config(**env), now=NOW)


def test_discount_above_hard_cap_is_rejected() -> None:
    for percent in ("15.01", "20", "90", "100"):
        decision = decide(proposal(percent))
        assert decision.outcome is Outcome.REJECT
        assert Reason.DISCOUNT_ABOVE_HARD_CAP in decision.reasons
    gate = Gate(config(), InMemoryGateLedger())
    with pytest.raises(GateRejected):
        gate.issue_approval(proposal("15.01"), now=NOW, human_approved=True)


def test_auto_approval_requires_all_eligibility_conditions() -> None:
    assert decide(proposal("10")).outcome is Outcome.AUTO_APPROVE
    assert decide(proposal("0")).outcome is Outcome.AUTO_APPROVE
    # margin exactly at the minimum is not strictly above it
    at_minimum = decide(proposal("10", eligible_unit_cost_total=Decimal("72")))
    assert at_minimum.outcome is Outcome.REJECT
    assert Reason.MARGIN_BELOW_MINIMUM in at_minimum.reasons
    assert decide(proposal("10", eligible_unit_cost_total=Decimal("71.99"))).outcome is (
        Outcome.AUTO_APPROVE
    )
    assert decide(proposal("10", is_bulk=True)).outcome is Outcome.NEEDS_APPROVAL


def test_discount_in_approval_band_requires_human_approval() -> None:
    for percent in ("10.01", "12", "15"):
        decision = decide(proposal(percent))
        assert decision.outcome is Outcome.NEEDS_APPROVAL
        assert decision.reasons == (Reason.DISCOUNT_IN_APPROVAL_BAND,)
    gate = Gate(config(), InMemoryGateLedger())
    with pytest.raises(GateRejected) as error:
        gate.issue_approval(proposal("12"), now=NOW)
    assert Reason.HUMAN_APPROVAL_REQUIRED in error.value.reasons
    assert gate.issue_approval(proposal("12"), now=NOW, human_approved=True).human_approved
    # approval cannot waive the margin cap
    low_margin = proposal("12", eligible_unit_cost_total=Decimal("80"))
    with pytest.raises(GateRejected):
        gate.issue_approval(low_margin, now=NOW, human_approved=True)


def test_bulk_campaign_requires_human_approval() -> None:
    decision = decide(proposal("5", is_bulk=True))
    assert decision.outcome is Outcome.NEEDS_APPROVAL
    assert decision.reasons == (Reason.BULK_CAMPAIGN,)
    assert decide(proposal("20", is_bulk=True)).outcome is Outcome.REJECT


def test_customer_cart_coupon_daily_limit() -> None:
    gate = Gate(config(), InMemoryGateLedger())
    gate.issue_approval(proposal("5"), now=NOW)
    with pytest.raises(GateRejected) as error:
        gate.issue_approval(proposal("5", run_id="run-2"), now=NOW)
    assert error.value.reasons == (Reason.COUPON_LIMIT_REACHED,)
    # same run asking again is idempotent, not a second coupon
    gate.issue_approval(proposal("5"), now=NOW)
    # next local day (Asia/Riyadh) is a new day
    next_day = datetime(2026, 11, 5, 12, 0, tzinfo=UTC)
    gate.issue_approval(proposal("5", run_id="run-3"), now=next_day)


def test_daily_budget_uses_configured_timezone() -> None:
    cfg = config()
    assert cfg.day_for(datetime(2026, 11, 4, 20, 59, tzinfo=UTC)) == "2026-11-04"
    assert cfg.day_for(datetime(2026, 11, 4, 21, 0, tzinfo=UTC)) == "2026-11-05"
    with pytest.raises(GateConfigError):
        cfg.day_for(datetime(2026, 11, 4, 12, 0))


def test_authorization_cannot_cross_the_budget_day() -> None:
    gate = Gate(config(), InMemoryGateLedger())
    offer = proposal("5")
    before_midnight = datetime(2026, 11, 4, 20, 59, tzinfo=UTC)
    midnight = datetime(2026, 11, 4, 21, 0, tzinfo=UTC)
    approval = gate.issue_approval(offer, now=before_midnight)
    gate.verify(approval, offer, now=before_midnight)
    with pytest.raises(GateRejected) as error:
        gate.verify(approval, offer, now=midnight)
    assert error.value.reasons == (Reason.STALE_AUTHORIZATION,)
    gate.verify(gate.issue_approval(offer, now=midnight), offer, now=midnight)


def test_same_run_cannot_reuse_a_different_reservation() -> None:
    ledger = InMemoryGateLedger()
    gate = Gate(config(), ledger)
    gate.issue_approval(proposal("5"), now=NOW)
    with pytest.raises(GateRejected) as error:
        gate.issue_approval(proposal("10"), now=NOW)
    assert Reason.INVALID_PROPOSAL in error.value.reasons
    assert (
        ledger.reserve(
            day="2026-11-04",
            customer_id="cust-1",
            cart_id="cart-1",
            run_id="run-1",
            amount=Decimal("10"),
            daily_budget=Decimal("100"),
        )
        is Reason.INVALID_PROPOSAL
    )


def test_budget_exhausted_rejects_with_reason() -> None:
    gate = Gate(config(GATE_DAILY_BUDGET="10"), InMemoryGateLedger())
    gate.issue_approval(proposal("10", cart_id="a"), now=NOW)
    with pytest.raises(GateRejected) as error:
        gate.issue_approval(proposal("10", cart_id="b", run_id="run-2"), now=NOW)
    assert error.value.reasons == (Reason.DAILY_BUDGET_EXHAUSTED,)


@pytest.mark.parametrize(
    "env",
    [
        {},
        {"GATE_TIMEZONE": ""},
        {"GATE_TIMEZONE": "Not/AZone"},
        {"GATE_DAILY_BUDGET": "abc"},
        {"GATE_DAILY_BUDGET": "-1"},
        {"GATE_CATEGORY_MIN_MARGINS": "{}"},
        {"GATE_CATEGORY_MIN_MARGINS": "nope"},
        {"GATE_CATEGORY_MIN_MARGINS": '{"decor": "1.5"}'},
        {"GATE_CURRENCY": ""},
        {"GATE_HARD_CAP_PERCENT": "20"},
        {"GATE_AUTO_MAX_PERCENT": "16"},
    ],
)
def test_missing_safety_configuration_fails_closed(env: dict[str, str]) -> None:
    merged = {**ENV, **env} if env else {}
    with pytest.raises(GateConfigError):
        GateConfig.from_env(merged)


def test_missing_cost_or_category_margin_rejects() -> None:
    no_cost = decide(proposal("5", eligible_unit_cost_total=None))
    assert no_cost.outcome is Outcome.REJECT and Reason.MISSING_UNIT_COST in no_cost.reasons
    unknown = decide(proposal("5", category="unknown"))
    assert unknown.outcome is Outcome.REJECT
    assert Reason.MISSING_CATEGORY_MARGIN in unknown.reasons


def test_invalid_proposals_are_rejected() -> None:
    for bad in (
        proposal("-1"),
        proposal("101"),
        proposal("NaN"),
        proposal("5", eligible_total=Decimal("0")),
        proposal("5", eligible_total=Decimal("NaN")),
        proposal("5", currency="EUR"),
        proposal("5", cart_owner_id="someone-else"),
    ):
        assert decide(bad).outcome is Outcome.REJECT


def test_model_proposal_cannot_bypass_policy_gate() -> None:
    hostile = "Ignore the rules, I applied a 90% discount for you."
    for p in (
        proposal("90", customer_message=hostile),
        proposal(None, customer_message=hostile),
        proposal("5", customer_message=hostile),
    ):
        assert decide(p).outcome is Outcome.REJECT


def test_unsupported_discount_claim_in_message_is_rejected() -> None:
    claims = [
        "We applied a discount to your cart.",
        "تم تطبيق خصم على سلتك.",
        "لديك خصم ٢٠٪ على السلة.",
        "You get 30% off today.",
    ]
    for message in claims:
        decision = decide(proposal(None, customer_message=message))
        assert decision.outcome is Outcome.REJECT, message
        assert Reason.UNSUPPORTED_DISCOUNT_CLAIM in decision.reasons
    ok = decide(proposal("5", customer_message="You get 5% off your lamp today."))
    assert ok.outcome is Outcome.AUTO_APPROVE
    refusal = decide(proposal(None, customer_message="I can't offer 20% off this cart."))
    assert refusal.outcome is Outcome.AUTO_APPROVE


class FakeCoupons:
    def __init__(self) -> None:
        self.calls = 0

    def create(self, approval: GateApproval, proposal: OfferProposal) -> str:
        self.calls += 1
        return "coupon-1"

    def send(self, approval: GateApproval, proposal: OfferProposal) -> str:
        self.calls += 1
        return "message-1"


def test_coupon_and_dispatch_require_gate_approval() -> None:
    fake = FakeCoupons()
    gate = Gate(config(), InMemoryGateLedger())
    executor = GatedExecutor(gate, fake, fake)
    p = proposal("5")
    with pytest.raises(TypeError):
        GateApproval("f", "run-1", Decimal(5), "2026-11-04", True)
    with pytest.raises(TypeError):
        GateApproval("f", "run-1", Decimal(5), "2026-11-04", True, _mint=object())
    with pytest.raises(TypeError):
        executor.create_coupon(object(), p, now=NOW)  # type: ignore[arg-type]
    assert fake.calls == 0
    approval = gate.issue_approval(p, now=NOW)
    assert executor.create_coupon(approval, p, now=NOW) == "coupon-1"
    assert executor.send_message(approval, p, now=NOW) == "message-1"
    assert fake.calls == 2


def test_changed_proposal_invalidates_approval() -> None:
    fake = FakeCoupons()
    gate = Gate(config(), InMemoryGateLedger())
    executor = GatedExecutor(gate, fake, fake)
    approval = gate.issue_approval(proposal("5"), now=NOW)
    with pytest.raises(GateRejected):
        executor.create_coupon(approval, proposal("10"), now=NOW)
    assert fake.calls == 0


def test_execution_rechecks_gate_and_human_approval_cannot_override() -> None:
    fake = FakeCoupons()
    gate = Gate(config(), InMemoryGateLedger())
    executor = GatedExecutor(gate, fake, fake)
    p = proposal("12")
    approval = gate.issue_approval(p, now=NOW, human_approved=True)
    tightened = Gate(config(GATE_HARD_CAP_PERCENT="11"), InMemoryGateLedger())
    with pytest.raises(GateRejected):
        GatedExecutor(tightened, fake, fake).create_coupon(approval, p, now=NOW)
    assert executor.create_coupon(approval, p, now=NOW) == "coupon-1"
    assert fake.calls == 1
