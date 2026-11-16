from __future__ import annotations

import json
import logging
import re
import uuid
from collections.abc import Mapping
from dataclasses import asdict, dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Literal, Protocol

from sqlalchemy import Engine

from engine.coupon_code import coupon_code_for_run
from engine.db import DEFAULT_APPROVAL_TTL, RunRepository
from engine.fixed_rule import FIXED_SCHEMA, build_fixed_offer
from engine.odoo import AbandonedCart, cart_snapshot, cart_to_proposal_inputs
from engine.policy import Gate, OfferProposal, Outcome, as_dict
from engine.privacy import PiiTokenizer, PrivacyError, has_unknown_tokens
from engine.proposal_budget import BudgetError, ProposalBudget, Rate, ReviewBudget
from engine.proposal_provider import VERSION, LLMClient, ProviderError
from engine.retrieval import Retriever
from engine.runs import RunState
from engine.runtime_state import RuntimeGuard, RuntimeState

LOG = logging.getLogger(__name__)
CONTACT = re.compile(
    r"[\w.+-]+@[\w.-]+\.[a-zA-Z]{2,}|(?:\+?[\d\u0660-\u0669\u06f0-\u06f9][\s().-]*){7,}"
)


class ProposalError(Exception):
    """Proposal failed without exposing untrusted content."""


# Spec release-gates-and-kill-switch.md (breaker): failures that mean the provider or its output
# contract failed. Every ProposalError message must be in exactly one of these two sets;
# tests/test_fallback_and_breaker.py fails when a new message is left unclassified.
PROVIDER_FAILURE_REASONS: frozenset[str] = frozenset(
    {
        "provider_error",
        "invalid structured model response",
        "invalid proposal schema",
        "invalid proposal action",
        "no_action cannot contain a message",
        "invalid proposal message",
        "invalid proposed discount",
        "invalid price claim",
        "invalid price currency",
        "missing or invalid provider usage",
        "provider usage exceeded reservation",
        "provider response incomplete",
        "provider output contains customer identity",
        "provider output contains an unknown PII token",
        "provider output contains a contact identifier",
        "price claim does not match the cart",
        "invalid discount type",
        "discount requires a single cart category",
        "discount draft requires a message",
        "rendered proposal message is too long",
    }
)
NON_PROVIDER_FAILURE_REASONS: frozenset[str] = frozenset(
    {
        "synthetic proposals are not enabled",
        "ERP proposals are not enabled",
        "run does not match customer and cart",
        "invalid language or empty request",
        "invalid input length or type",
        "invalid execution channel",
        "contact identifier in model input",
        "cart quantities or prices are invalid",
        "cart is missing or customer scope does not match",
        "cart currency or lines are invalid",
        "input token count exceeds configured limit or is invalid",
    }
)


class CartReader(Protocol):
    def get_cart(self, order_id: int) -> AbandonedCart | None: ...


@dataclass(frozen=True)
class ProposalSettings:
    synthetic_only: bool
    budget_usd: Decimal
    input_tokens: int
    output_tokens: int
    rate: Rate
    review_budget: ReviewBudget | None = None
    # Q12: ERP runs are allowed only when explicitly enabled; PII is always tokenized.
    erp_enabled: bool = False
    # #86: how long a human approval stays valid (spec operator-rbac-and-expiring-approvals).
    approval_ttl: timedelta = DEFAULT_APPROVAL_TTL

    def __post_init__(self) -> None:
        if self.synthetic_only is not True and self.erp_enabled is not True:
            raise ValueError("synthetic-only mode must be explicitly enabled")
        if not timedelta(hours=1) <= self.approval_ttl <= timedelta(hours=168):
            raise ValueError("approval TTL must be between 1 and 168 hours")
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
        review_id = env.get("PROPOSAL_REVIEW_ID", "").strip()
        review_limit = env.get("PROPOSAL_REVIEW_BUDGET_USD", "").strip()
        if bool(review_id) != bool(review_limit):
            raise ValueError("review ID and aggregate budget must both be set or both be omitted")
        review_budget = (
            ReviewBudget(review_id, Decimal(review_limit)) if review_id and review_limit else None
        )
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
            review_budget=review_budget,
            erp_enabled=env.get("PROPOSAL_ERP_MODE") == "YES",
            approval_ttl=approval_ttl_from_env(env),
        )


def approval_ttl_from_env(env: Mapping[str, str]) -> timedelta:
    raw = env.get("APPROVAL_TTL_HOURS", "").strip()
    try:
        return timedelta(hours=int(raw)) if raw else DEFAULT_APPROVAL_TTL
    except ValueError as error:
        raise ValueError("APPROVAL_TTL_HOURS must be an integer") from error


def _clean(text: str, tokenizer: PiiTokenizer) -> str:
    """TM-2: replace customer identities with opaque per-run tokens before any LLM call."""
    if not isinstance(text, str) or len(text) > 4000:
        raise ProposalError("invalid input length or type")
    try:
        text = tokenizer.tokenize(text, contact_check=CONTACT)
    except PrivacyError as error:
        raise ProposalError(str(error)) from error
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
    if discount is not None and (
        not isinstance(discount, Decimal)
        or not discount.is_finite()
        or discount < 0
        or discount > 100
    ):
        raise ProposalError("invalid proposed discount")
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
        runtime: RuntimeGuard | None = None,
    ) -> None:
        self.engine = engine
        self.runtime = runtime
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
        execution_channel: Literal["telegram", "mailpit"] | None = None,
    ) -> None:
        # A run's trigger reference is bound by the trusted synthetic trigger, not the model.
        run = self.runs.get_run(run_id)
        if run.trigger_ref != f"{customer_id}:{cart_id}":
            raise ProposalError("run does not match customer and cart")
        if run.trigger_type == "erp" and not self.settings.erp_enabled:
            raise ProposalError("ERP proposals are not enabled")
        if run.trigger_type != "erp" and not self.settings.synthetic_only:
            raise ProposalError("synthetic proposals are not enabled")
        # KS-3: a halted engine refuses before any state change, so the run stays resumable.
        state = self.runtime.require_not_halted() if self.runtime else RuntimeState.ACTIVE
        self.runs.transition(run_id, RunState.RECEIVED, RunState.VALIDATING_INPUT, actor="system")
        try:
            if language not in {"en", "ar"} or not request.strip():
                raise ProposalError("invalid language or empty request")
            proposal_language: Literal["en", "ar"] = "en" if language == "en" else "ar"
            if execution_channel not in {None, "telegram", "mailpit"}:
                raise ProposalError("invalid execution channel")
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
            tokenizer = PiiTokenizer.for_cart(cart)
            if state is RuntimeState.FALLBACK:
                # KS-4: no retrieval, no model budget, no provider call; same gate and approval.
                self.runs.transition(
                    run_id, RunState.LOADING_CONTEXT, RunState.GENERATING_PROPOSAL, actor="system"
                )
                self._finish(
                    run_id,
                    build_fixed_offer(run_id, cart, proposal_language),
                    {"action": "draft_message"},
                    [],
                    cart,
                    execution_channel,
                    tokenizer,
                    schema_version=FIXED_SCHEMA,
                )
                return
            query = _clean(request, tokenizer)
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
                            "name": _clean(line.name, tokenizer),
                            "quantity": str(line.quantity),
                            "unit_price": str(line.unit_price),
                        }
                        for line in cart.lines
                    ],
                },
                "policies": [_clean(policy.text, tokenizer) for policy in policies],
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
                review=self.settings.review_budget,
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
                if has_unknown_tokens(message, tokenizer.issued()):
                    raise ProposalError("provider output contains an unknown PII token")
                # Issued tokens may stay; any raw identity or contact pattern fails closed.
                if tokenizer.mask(message) != message:
                    raise ProposalError("provider output contains customer identity")
                if CONTACT.search(message):
                    raise ProposalError("provider output contains a contact identifier")
            if answer["price_claim"] is not None and (
                answer["price_claim"] != total or answer["currency"] != cart.currency
            ):
                raise ProposalError("price claim does not match the cart")
            discount = answer["proposed_discount_percent"]
            if discount is not None and not isinstance(discount, Decimal):
                raise ProposalError("invalid discount type")
            if (
                discount is not None
                and discount > 0
                and len({line.category for line in cart.lines}) != 1
            ):
                raise ProposalError("discount requires a single cart category")
            if discount is not None and discount > 0 and answer["action"] == "draft_message":
                if not isinstance(message, str):
                    raise ProposalError("discount draft requires a message")
                code = coupon_code_for_run(str(run_id))
                percent = format(discount, "f")
                if language == "en":
                    footer = f"Coupon code for {percent}% discount: {code}"
                else:
                    footer = f"رمز خصم {percent}%: {code}"
                message = f"{message}\n\n{footer}"
                if len(message) > 4000:
                    raise ProposalError("rendered proposal message is too long")
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
                language=proposal_language,
            )
            self._finish(
                run_id,
                proposal,
                answer,
                [
                    {"source_id": p.source_id, "version": p.version, "content_hash": p.content_hash}
                    for p in policies
                ],
                cart,
                execution_channel,
                tokenizer,
            )
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

    def _finish(
        self,
        run_id: uuid.UUID,
        proposal: OfferProposal,
        answer: dict[str, object],
        sources: list[dict[str, str]],
        cart: AbandonedCart,
        execution_channel: Literal["telegram", "mailpit"] | None,
        tokenizer: PiiTokenizer,
        *,
        schema_version: str = VERSION,
    ) -> None:
        """Gate, persist, and route one proposal; shared by the model and fixed-rule paths."""
        decision = self.gate.evaluate(proposal, now=datetime.now(UTC))
        self._persist(
            run_id,
            proposal,
            answer,
            as_dict(decision),
            sources,
            cart,
            execution_channel,
            tokenizer.token_map(),
            schema_version,
        )
        if decision.outcome is Outcome.REJECT or answer["action"] != "draft_message":
            self.runs.transition(
                run_id,
                RunState.EVALUATING_POLICY,
                RunState.REJECTED,
                actor="system",
                detail={"reason": "gate_or_non_dispatch_action"},
            )
        elif decision.outcome is Outcome.NEEDS_APPROVAL:
            self.runs.request_approval(run_id, self.gate, ttl=self.settings.approval_ttl)
        LOG.info("proposal run=%s status=validated schema=%s", run_id, schema_version)

    def _persist(
        self,
        run_id: uuid.UUID,
        proposal: OfferProposal,
        answer: dict[str, object],
        decision: dict[str, object],
        sources: list[dict[str, str]],
        cart: AbandonedCart,
        execution_channel: Literal["telegram", "mailpit"] | None,
        pii_tokens: dict[str, object],
        schema_version: str = VERSION,
    ) -> None:
        record = asdict(proposal)
        record["action"] = answer["action"]
        # Token -> field reference only; raw values are reloaded from Odoo at dispatch.
        record["pii_tokens"] = pii_tokens
        record["policy_sources"] = sources
        record["cart_snapshot"] = cart_snapshot(cart)
        if execution_channel is not None:
            record["execution_channel"] = execution_channel
        record = json.loads(json.dumps(record, default=str))
        self.runs.persist_proposal(
            run_id,
            schema_version=schema_version,
            fingerprint=proposal.fingerprint(),
            proposal=record,
            gate_result=decision,
        )
