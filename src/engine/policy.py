from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Mapping
from dataclasses import asdict, dataclass, field
from datetime import datetime
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation
from enum import StrEnum
from typing import Any, Protocol
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

# Policy decisions are documented in docs/adr/0018-financial-safety-gate-decisions.md.
HARD_CAP_PERCENT = Decimal("15")
AUTO_APPROVE_MAX_PERCENT = Decimal("10")
CENT = Decimal("0.01")


class GateConfigError(Exception):
    """Safety configuration is missing or invalid; the gate must not authorize anything."""


class GateRejected(Exception):
    def __init__(self, reasons: tuple[Reason, ...]) -> None:
        super().__init__(", ".join(reason.value for reason in reasons))
        self.reasons = reasons


class Outcome(StrEnum):
    REJECT = "reject"
    NEEDS_APPROVAL = "needs_approval"
    AUTO_APPROVE = "auto_approve"


class Reason(StrEnum):
    INVALID_PROPOSAL = "invalid_proposal"
    CURRENCY_MISMATCH = "currency_mismatch"
    CART_OWNER_MISMATCH = "cart_owner_mismatch"
    DISCOUNT_ABOVE_HARD_CAP = "discount_above_hard_cap"
    UNSUPPORTED_DISCOUNT_CLAIM = "unsupported_discount_claim"
    MISSING_UNIT_COST = "missing_unit_cost"
    MISSING_CATEGORY_MARGIN = "missing_category_margin"
    MARGIN_BELOW_MINIMUM = "margin_below_minimum"
    COUPON_LIMIT_REACHED = "coupon_limit_reached"
    DAILY_BUDGET_EXHAUSTED = "daily_budget_exhausted"
    HUMAN_APPROVAL_REQUIRED = "human_approval_required"
    BULK_CAMPAIGN = "bulk_campaign"
    DISCOUNT_IN_APPROVAL_BAND = "discount_in_approval_band"


@dataclass(frozen=True)
class GateConfig:
    timezone: ZoneInfo
    currency: str
    daily_budget: Decimal
    category_min_margin: Mapping[str, Decimal]
    auto_max_percent: Decimal = AUTO_APPROVE_MAX_PERCENT
    hard_cap_percent: Decimal = HARD_CAP_PERCENT

    @classmethod
    def from_env(cls, env: Mapping[str, str]) -> GateConfig:
        """Build the config or raise GateConfigError. Nothing has a default except thresholds."""

        def required(key: str) -> str:
            value = env.get(key, "").strip()
            if not value:
                raise GateConfigError(f"{key} must be set.")
            return value

        try:
            timezone = ZoneInfo(required("GATE_TIMEZONE"))
        except (ZoneInfoNotFoundError, ValueError) as error:
            raise GateConfigError("GATE_TIMEZONE must be a valid IANA timezone name.") from error
        budget = _decimal(required("GATE_DAILY_BUDGET"), "GATE_DAILY_BUDGET")
        if budget < 0:
            raise GateConfigError("GATE_DAILY_BUDGET must not be negative.")
        try:
            raw_margins = json.loads(required("GATE_CATEGORY_MIN_MARGINS"))
        except json.JSONDecodeError as error:
            raise GateConfigError("GATE_CATEGORY_MIN_MARGINS must be JSON.") from error
        if not isinstance(raw_margins, dict) or not raw_margins:
            raise GateConfigError("GATE_CATEGORY_MIN_MARGINS must be a non-empty JSON object.")
        margins: dict[str, Decimal] = {}
        for category, value in raw_margins.items():
            margin = _decimal(str(value), f"margin for {category}")
            if not Decimal(0) <= margin < Decimal(1):
                raise GateConfigError(f"Margin for {category} must be at least 0 and below 1.")
            margins[str(category)] = margin
        auto_max = _decimal(env.get("GATE_AUTO_MAX_PERCENT", "10"), "GATE_AUTO_MAX_PERCENT")
        hard_cap = _decimal(env.get("GATE_HARD_CAP_PERCENT", "15"), "GATE_HARD_CAP_PERCENT")
        if not Decimal(0) <= auto_max <= hard_cap <= HARD_CAP_PERCENT:
            raise GateConfigError("Thresholds must satisfy 0 <= auto <= hard cap <= 15.")
        return cls(
            timezone=timezone,
            currency=required("GATE_CURRENCY").upper(),
            daily_budget=budget,
            category_min_margin=margins,
            auto_max_percent=auto_max,
            hard_cap_percent=hard_cap,
        )

    def day_for(self, moment: datetime) -> str:
        if moment.tzinfo is None:
            raise GateConfigError("Gate time must be timezone-aware.")
        return moment.astimezone(self.timezone).date().isoformat()


def _decimal(raw: str, label: str) -> Decimal:
    try:
        value = Decimal(raw.strip())
    except InvalidOperation as error:
        raise GateConfigError(f"{label} must be a number.") from error
    if not value.is_finite():
        raise GateConfigError(f"{label} must be finite.")
    return value


@dataclass(frozen=True)
class OfferProposal:
    run_id: str
    customer_id: str
    cart_id: str
    cart_owner_id: str
    category: str
    currency: str
    eligible_total: Decimal
    eligible_unit_cost_total: Decimal | None
    proposed_discount_percent: Decimal | None
    is_bulk: bool = False
    customer_message: str | None = None

    def fingerprint(self) -> str:
        canonical = json.dumps(asdict(self), default=str, sort_keys=True, ensure_ascii=True)
        return hashlib.sha256(canonical.encode()).hexdigest()


@dataclass(frozen=True)
class GateDecision:
    outcome: Outcome
    reasons: tuple[Reason, ...]
    fingerprint: str
    discount_amount: Decimal
    margin: Decimal | None


class LedgerPeek(Protocol):
    def check(
        self, *, day: str, customer_id: str, cart_id: str, run_id: str, amount: Decimal
    ) -> Reason | None: ...


_DIGITS = str.maketrans("٠١٢٣٤٥٦٧٨٩٪٫", "0123456789%.")
_PERCENT = re.compile(r"(\d+(?:\.\d+)?)\s*(?:%|percent(?:age)?\b)", re.IGNORECASE)
_CLAIM_PATTERNS = (
    re.compile(
        r"\b(?:apply|applied|offer|offering|grant|granted|give|gave|provide|provided|"
        r"add|added|activate|activated|approve|approved)\b.{0,40}\b"
        r"(?:discount|coupon|promo(?:tion)?|offer|deal)\b",
        re.IGNORECASE,
    ),
    re.compile(
        r"\b(?:discount|coupon|promo(?:tion)?|offer|deal)\b.{0,40}\b"
        r"(?:applied|active|available|added|approved|granted)\b",
        re.IGNORECASE,
    ),
    re.compile(
        r"(?:تم|جرى)\s+(?:تطبيق|إضافة|تفعيل|اعتماد).{0,40}(?:خصم|عرض|قسيمة|كوبون)",
        re.IGNORECASE,
    ),
    re.compile(
        r"(?:أمنحك|أعطيك|سأطبق|طبقنا|أضفنا|استفد من).{0,40}(?:خصم|عرض|قسيمة|كوبون)",
        re.IGNORECASE,
    ),
)
_NEGATION = re.compile(
    r"\b(?:not|no|never|cannot|can't|won't|wouldn't|isn't|aren't|don't|doesn't|"
    r"unable|unavailable|unverified|unconfirmed)\b|لا|لن|لم|ليس|ليست|غير|"
    r"ما\s*(?:أقدر|يمكن|أستطيع)|مش|مو",
    re.IGNORECASE,
)


def message_discount_problem(message: str | None, proposed: Decimal) -> bool:
    """Defense in depth: True if the text claims a discount the structured field does not."""
    if not message:
        return False
    normalized = message.translate(_DIGITS).casefold()
    clauses = re.split(r"[.!?؟;،,\n]+|\b(?:but|however|although|and)\b|لكن|ولكن|بس", normalized)
    for clause in clauses:
        if _NEGATION.search(clause):
            continue
        for match in _PERCENT.finditer(clause):
            if Decimal(match.group(1)) != proposed:
                return True
        if proposed <= 0 and any(pattern.search(clause) for pattern in _CLAIM_PATTERNS):
            return True
    return False


def discount_amount(eligible_total: Decimal, percent: Decimal) -> Decimal:
    return (eligible_total * percent / Decimal(100)).quantize(CENT, rounding=ROUND_HALF_UP)


def _finite(value: Decimal | None) -> bool:
    return value is not None and value.is_finite()


def evaluate(
    proposal: OfferProposal,
    config: GateConfig,
    *,
    now: datetime,
    ledger: LedgerPeek | None = None,
) -> GateDecision:
    reasons: list[Reason] = []
    percent = proposal.proposed_discount_percent
    if percent is None:
        percent = Decimal(0)

    total = proposal.eligible_total
    valid = (
        _finite(total)
        and total > 0
        and percent.is_finite()
        and Decimal(0) <= percent <= Decimal(100)
        and bool(proposal.run_id and proposal.customer_id and proposal.cart_id)
    )
    if not valid:
        return GateDecision(
            Outcome.REJECT, (Reason.INVALID_PROPOSAL,), proposal.fingerprint(), Decimal(0), None
        )

    if proposal.currency.upper() != config.currency:
        reasons.append(Reason.CURRENCY_MISMATCH)
    if proposal.cart_owner_id != proposal.customer_id:
        reasons.append(Reason.CART_OWNER_MISMATCH)
    if percent > config.hard_cap_percent:
        reasons.append(Reason.DISCOUNT_ABOVE_HARD_CAP)
    if message_discount_problem(proposal.customer_message, percent):
        reasons.append(Reason.UNSUPPORTED_DISCOUNT_CLAIM)

    margin: Decimal | None = None
    amount = Decimal(0)
    if percent > 0 and percent <= config.hard_cap_percent:
        amount = discount_amount(total, percent)
        price_after = total * (Decimal(100) - percent) / Decimal(100)
        minimum = config.category_min_margin.get(proposal.category)
        cost = proposal.eligible_unit_cost_total
        if minimum is None:
            reasons.append(Reason.MISSING_CATEGORY_MARGIN)
        if not _finite(cost) or cost is None or cost < 0:
            reasons.append(Reason.MISSING_UNIT_COST)
        elif minimum is not None:
            margin = (price_after - cost) / price_after
            if margin <= minimum:
                reasons.append(Reason.MARGIN_BELOW_MINIMUM)
        if ledger is not None and not reasons:
            blocked = ledger.check(
                day=config.day_for(now),
                customer_id=proposal.customer_id,
                cart_id=proposal.cart_id,
                run_id=proposal.run_id,
                amount=amount,
            )
            if blocked is not None:
                reasons.append(blocked)

    if reasons:
        outcome = Outcome.REJECT
    elif proposal.is_bulk:
        outcome, reasons = Outcome.NEEDS_APPROVAL, [Reason.BULK_CAMPAIGN]
    elif percent > config.auto_max_percent:
        outcome, reasons = Outcome.NEEDS_APPROVAL, [Reason.DISCOUNT_IN_APPROVAL_BAND]
    else:
        outcome = Outcome.AUTO_APPROVE
    return GateDecision(outcome, tuple(reasons), proposal.fingerprint(), amount, margin)


_MINT = object()


@dataclass(frozen=True)
class GateApproval:
    """Proof that the gate approved one exact proposal. Only the Gate can create it."""

    fingerprint: str
    run_id: str
    discount_amount: Decimal
    day: str
    human_approved: bool
    _mint: object = field(repr=False, compare=False, default=None)

    def __post_init__(self) -> None:
        if self._mint is not _MINT:
            raise TypeError("GateApproval can only be created by the Gate.")


class Ledger(LedgerPeek, Protocol):
    def reserve(
        self,
        *,
        day: str,
        customer_id: str,
        cart_id: str,
        run_id: str,
        amount: Decimal,
        daily_budget: Decimal,
    ) -> Reason | None: ...


class Gate:
    def __init__(self, config: GateConfig, ledger: Ledger) -> None:
        self._config = config
        self._ledger = ledger

    def evaluate(self, proposal: OfferProposal, *, now: datetime) -> GateDecision:
        return evaluate(proposal, self._config, now=now, ledger=self._ledger)

    def issue_approval(
        self, proposal: OfferProposal, *, now: datetime, human_approved: bool = False
    ) -> GateApproval:
        """Re-evaluate, require human approval when needed, and reserve budget atomically."""
        decision = self.evaluate(proposal, now=now)
        if decision.outcome is Outcome.REJECT:
            raise GateRejected(decision.reasons)
        if decision.outcome is Outcome.NEEDS_APPROVAL and not human_approved:
            raise GateRejected((Reason.HUMAN_APPROVAL_REQUIRED, *decision.reasons))
        day = self._config.day_for(now)
        if decision.discount_amount > 0:
            blocked = self._ledger.reserve(
                day=day,
                customer_id=proposal.customer_id,
                cart_id=proposal.cart_id,
                run_id=proposal.run_id,
                amount=decision.discount_amount,
                daily_budget=self._config.daily_budget,
            )
            if blocked is not None:
                raise GateRejected((blocked,))
        return GateApproval(
            fingerprint=decision.fingerprint,
            run_id=proposal.run_id,
            discount_amount=decision.discount_amount,
            day=day,
            human_approved=human_approved,
            _mint=_MINT,
        )

    def verify(self, approval: GateApproval, proposal: OfferProposal, *, now: datetime) -> None:
        """Run immediately before execution (AS-5); approval cannot waive a hard rejection."""
        if not isinstance(approval, GateApproval):
            raise TypeError("A GateApproval is required.")
        if approval.fingerprint != proposal.fingerprint():
            raise GateRejected((Reason.HUMAN_APPROVAL_REQUIRED,))
        decision = evaluate(proposal, self._config, now=now)
        if decision.outcome is Outcome.REJECT:
            raise GateRejected(decision.reasons)
        if decision.outcome is Outcome.NEEDS_APPROVAL and not approval.human_approved:
            raise GateRejected((Reason.HUMAN_APPROVAL_REQUIRED,))


def as_dict(decision: GateDecision) -> dict[str, Any]:
    return {
        "outcome": decision.outcome.value,
        "reasons": [reason.value for reason in decision.reasons],
        "fingerprint": decision.fingerprint,
        "discount_amount": str(decision.discount_amount),
        "margin": None if decision.margin is None else str(decision.margin),
    }
