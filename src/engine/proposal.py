from __future__ import annotations

import json
import logging
import re
import uuid
from collections.abc import Mapping
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from decimal import Decimal
from typing import Protocol

from sqlalchemy import Engine

from engine.db import RunRepository
from engine.odoo import AbandonedCart, cart_to_proposal_inputs
from engine.policy import Gate, OfferProposal, Outcome, as_dict
from engine.proposal_budget import BudgetError, ProposalBudget, Rate
from engine.proposal_provider import VERSION, LLMClient, ProviderError
from engine.retrieval import Retriever
from engine.runs import RunState

LOG = logging.getLogger(__name__)
CONTACT = re.compile(
    r"[\w.+-]+@[\w.-]+\.[a-zA-Z]{2,}|(?:\+?[\d\u0660-\u0669\u06f0-\u06f9][\s().-]*){7,}"
)


class ProposalError(Exception):
    """Proposal failed without exposing untrusted content."""


class CartReader(Protocol):
    def get_cart(self, order_id: int) -> AbandonedCart | None: ...


@dataclass(frozen=True)
class ProposalSettings:
    synthetic_only: bool
    budget_usd: Decimal
    input_tokens: int
    output_tokens: int
    rate: Rate

    def __post_init__(self) -> None:
        if self.synthetic_only is not True:
            raise ValueError("synthetic-only mode must be explicitly enabled")
        if (
            not isinstance(self.input_tokens, int)
            or isinstance(self.input_tokens, bool)
            or not 1 <= self.input_tokens <= 272_000
            or not isinstance(self.output_tokens, int)
            or isinstance(self.output_tokens, bool)
            or not 1 <= self.output_tokens <= 128_000
        ):
            raise ValueError("invalid request token limits")
        if self.rate.input_per_million < Decimal("0.125") or self.rate.output_per_million < Decimal(
            "0.5"
        ):
            raise ValueError("rates must conservatively cover F-005 standard pricing")
        if not self.budget_usd.is_finite() or self.budget_usd <= 0:
            raise ValueError("per-run budget must be positive")

    @classmethod
    def from_env(cls, env: Mapping[str, str]) -> ProposalSettings:
        try:
            budget = Decimal(env["PROPOSAL_BUDGET_USD"])
            input_tokens = int(env["PROPOSAL_INPUT_TOKEN_LIMIT"])
            output_tokens = int(env["PROPOSAL_OUTPUT_TOKEN_LIMIT"])
        except (KeyError, ValueError, ArithmeticError) as error:
            raise ValueError("proposal budget and token limits must be set and valid") from error
        return cls(
            synthetic_only=env.get("PROPOSAL_SYNTHETIC_ONLY") == "YES",
            budget_usd=budget,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            rate=Rate(
                Decimal("0.125"),
                Decimal("0.50"),
                "F-005 standard text including cache writes; verified 2026-11-06",
            ),
        )


def _clean(text: str, identities: tuple[str, ...]) -> str:
    if not isinstance(text, str) or len(text) > 4000:
        raise ProposalError("invalid input length or type")
    for identity in identities:
        if identity:
            text = re.sub(re.escape(identity), "[customer]", text, flags=re.IGNORECASE)
    if CONTACT.search(text):
        raise ProposalError("contact identifier in model input")
    return text


def _validate_output(raw: str) -> dict[str, object]:
    try:
        data = json.loads(raw, parse_float=Decimal, parse_int=Decimal)
    except (ValueError, TypeError) as error:
        raise ProposalError("invalid structured model response") from error
    keys = {"action", "customer_message", "price_claim", "currency", "proposed_discount_percent"}
    if not isinstance(data, dict) or set(data) != keys:
        raise ProposalError("invalid proposal schema")
    action, message = data["action"], data["customer_message"]
    if action not in {"draft_message", "ask_clarification", "refuse", "no_action"}:
        raise ProposalError("invalid proposal action")
    if action == "no_action":
        if message is not None:
            raise ProposalError("no_action cannot contain a message")
    elif not isinstance(message, str) or not message.strip() or len(message) > 4000:
        raise ProposalError("invalid proposal message")
    discount = data["proposed_discount_percent"]
    if discount is not None and (not isinstance(discount, Decimal) or discount != 0):
        raise ProposalError("discount proposals are disabled")
    price = data["price_claim"]
    currency = data["currency"]
    if price is not None and (not isinstance(price, Decimal) or not price.is_finite() or price < 0):
        raise ProposalError("invalid price claim")
    if (price is None) != (currency is None) or (
        currency is not None and (not isinstance(currency, str) or len(currency) != 3)
    ):
        raise ProposalError("invalid price currency")
    return data


class ProposalService:
    def __init__(
        self,
        engine: Engine,
        carts: CartReader,
        retriever: Retriever,
        provider: LLMClient,
        gate: Gate,
        settings: ProposalSettings,
    ) -> None:
        self.engine = engine
        self.runs = RunRepository(engine)
        self.budget = ProposalBudget(engine)
        self.carts = carts
        self.retriever = retriever
        self.provider = provider
        self.gate = gate
        self.settings = settings

    def generate(
        self,
        run_id: uuid.UUID,
        *,
        customer_id: int,
        cart_id: int,
        language: str,
        request: str,
    ) -> None:
        # A run's trigger reference is bound by the trusted synthetic trigger, not the model.
        run = self.runs.get_run(run_id)
        if run.trigger_ref != f"{customer_id}:{cart_id}":
            raise ProposalError("run does not match customer and cart")
        self.runs.transition(run_id, RunState.RECEIVED, RunState.VALIDATING_INPUT, actor="system")
        try:
            if language not in {"en", "ar"} or not request.strip():
                raise ProposalError("invalid language or empty request")
            self.runs.transition(
                run_id, RunState.VALIDATING_INPUT, RunState.LOADING_CONTEXT, actor="system"
            )
            cart = self.carts.get_cart(cart_id)
            if cart is None or cart.partner_id != customer_id or cart.order_id != cart_id:
                raise ProposalError("cart is missing or customer scope does not match")
            if not re.fullmatch(r"[A-Z]{3}", cart.currency) or not cart.lines:
                raise ProposalError("cart currency or lines are invalid")
            if any(
                not line.quantity.is_finite()
                or line.quantity <= 0
                or not line.unit_price.is_finite()
                or line.unit_price < 0
                for line in cart.lines
            ):
                raise ProposalError("cart quantities or prices are invalid")
            identities = (cart.partner_name, cart.partner_email)
            query = _clean(request, identities)
            policies = self.retriever.search(query, language)
            total, cost = cart_to_proposal_inputs(cart)
            payload: dict[str, object] = {
                "language": language,
                "request": query,
                "cart": {
                    "currency": cart.currency,
                    "eligible_total": str(total),
                    "items": [
                        {
                            "name": _clean(line.name, identities),
                            "quantity": str(line.quantity),
                            "unit_price": str(line.unit_price),
                        }
                        for line in cart.lines
                    ],
                },
                "policies": [_clean(policy.text, identities) for policy in policies],
            }
            self.runs.transition(
                run_id, RunState.LOADING_CONTEXT, RunState.GENERATING_PROPOSAL, actor="system"
            )
            call_id = uuid.uuid4()
            self.budget.reserve(
                run_id,
                call_id,
                limit=self.settings.budget_usd,
                input_limit=self.settings.input_tokens,
                output_limit=self.settings.output_tokens,
                rate=self.settings.rate,
            )
            try:
                count = self.provider.count_tokens(payload)
                if (
                    not isinstance(count, int)
                    or isinstance(count, bool)
                    or not 0 <= count <= self.settings.input_tokens
                ):
                    raise ProposalError("input token count exceeds configured limit or is invalid")
                result = self.provider.generate(payload, self.settings.output_tokens)
            except Exception:
                self.budget.mark_unknown(call_id, "provider_error")
                raise
            if (
                not isinstance(result.input_tokens, int)
                or isinstance(result.input_tokens, bool)
                or result.input_tokens < 0
                or not isinstance(result.output_tokens, int)
                or isinstance(result.output_tokens, bool)
                or result.output_tokens < 0
            ):
                self.budget.mark_unknown(call_id, "missing_or_invalid_usage")
                raise ProposalError("missing or invalid provider usage")
            if not self.budget.settle(
                call_id,
                input_tokens=result.input_tokens,
                output_tokens=result.output_tokens,
                rate=self.settings.rate,
            ):
                raise ProposalError("provider usage exceeded reservation")
            if not result.complete:
                raise ProposalError("provider response incomplete")
            answer = _validate_output(result.text)
            message = answer["customer_message"]
            if isinstance(message, str):
                cleaned = _clean(message, identities)
                if cleaned != message:
                    raise ProposalError("provider output contains customer identity")
            if answer["price_claim"] is not None and (
                answer["price_claim"] != total or answer["currency"] != cart.currency
            ):
                raise ProposalError("price claim does not match the cart")
            discount = answer["proposed_discount_percent"]
            if discount is not None and not isinstance(discount, Decimal):
                raise ProposalError("invalid discount type")
            proposal = OfferProposal(
                run_id=str(run_id),
                customer_id=str(customer_id),
                cart_id=str(cart_id),
                cart_owner_id=str(cart.partner_id),
                category=cart.lines[0].category,
                currency=cart.currency,
                eligible_total=total,
                eligible_unit_cost_total=cost,
                proposed_discount_percent=discount,
                customer_message=message if isinstance(message, str) else None,
            )
            decision = self.gate.evaluate(proposal, now=datetime.now(UTC))
            self._persist(
                run_id,
                proposal,
                answer,
                as_dict(decision),
                [
                    {"source_id": p.source_id, "version": p.version, "content_hash": p.content_hash}
                    for p in policies
                ],
            )
            if decision.outcome is Outcome.REJECT or answer["action"] != "draft_message":
                self.runs.transition(
                    run_id,
                    RunState.EVALUATING_POLICY,
                    RunState.REJECTED,
                    actor="system",
                    detail={"reason": "gate_or_non_dispatch_action"},
                )
            LOG.info("proposal run=%s status=validated", run_id)
        except Exception as error:
            if isinstance(error, (ProposalError, BudgetError)):
                reason = str(error)
            elif isinstance(error, ProviderError):
                reason = "provider_error"
            else:
                reason = f"proposal_error:{type(error).__name__}"
            self.runs.fail(run_id, reason=reason)
            LOG.warning("proposal run=%s status=failed reason=%s", run_id, reason)
            raise

    def _persist(
        self,
        run_id: uuid.UUID,
        proposal: OfferProposal,
        answer: dict[str, object],
        decision: dict[str, object],
        sources: list[dict[str, str]],
    ) -> None:
        record = asdict(proposal)
        record["action"] = answer["action"]
        record["policy_sources"] = sources
        record = json.loads(json.dumps(record, default=str))
        self.runs.persist_proposal(
            run_id,
            schema_version=VERSION,
            fingerprint=proposal.fingerprint(),
            proposal=record,
            gate_result=decision,
        )
