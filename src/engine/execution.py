from __future__ import annotations

import hashlib
import uuid
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from email.message import EmailMessage
from email.utils import make_msgid
from typing import Literal, Protocol

from sqlalchemy import Engine

from engine.approval_data import offer_from_record, snapshot_hash
from engine.channels import EmailPort, TelegramMessageSender
from engine.coupon_code import coupon_code_for_run
from engine.db import ApprovalConflict, ExecutionConflict, RunRepository
from engine.odoo import AbandonedCart, cart_snapshot, cart_to_proposal_inputs
from engine.policy import Gate, GateApproval, GateRejected, OfferProposal
from engine.runs import RunState

MAILPIT_REVIEW_RECIPIENT = "retention-review@example.test"
MAILPIT_REVIEW_SENDER = "retention-engine@example.test"


class ExecutionError(RuntimeError):
    """Execution was refused or failed without claiming successful delivery."""


class ExecutionHeld(ExecutionError):
    """An external effect has an unresolved outcome and must not be retried automatically."""


class KnownNotSent(ExecutionError):
    """A port proves it failed before the external effect could be accepted."""


class CurrentCartReader(Protocol):
    def get_abandoned_cart(self, order_id: int, *, now: datetime) -> AbandonedCart | None: ...


class CouponPort(Protocol):
    def create(self, approval: GateApproval, proposal: OfferProposal) -> str: ...


class MessagePort(Protocol):
    def send(
        self, destination: TestDestination, text: str, *, language: Literal["en", "ar"]
    ) -> str: ...


@dataclass(frozen=True)
class TestDestination:
    """An operator-confirmed test sink; never populated from model or cart data."""

    __test__ = False

    channel: Literal["telegram", "mailpit"]
    target: str
    confirmed: bool
    destination_hash: str = ""

    def __post_init__(self) -> None:
        if not self.confirmed:
            raise ExecutionError("dedicated synthetic destination must be explicitly confirmed")
        if self.channel == "telegram" and not self.target.strip():
            raise ExecutionError("confirmed Telegram test destination is required")
        if self.channel == "mailpit" and self.target != MAILPIT_REVIEW_RECIPIENT:
            raise ExecutionError("Mailpit destination must be the fixed synthetic review inbox")
        if self.channel not in {"telegram", "mailpit"}:
            raise ExecutionError("unsupported synthetic test channel")
        object.__setattr__(
            self,
            "destination_hash",
            hashlib.sha256(f"{self.channel}:{self.target}".encode()).hexdigest(),
        )

    @classmethod
    def from_env(
        cls,
        env: Mapping[str, str],
        channel: Literal["telegram", "mailpit"],
        *,
        confirm_dedicated_test_destination: bool = False,
    ) -> TestDestination:
        if env.get("EXECUTION_SYNTHETIC_ONLY") != "YES":
            raise ExecutionError("synthetic-only execution mode must be explicitly enabled")
        if not confirm_dedicated_test_destination:
            raise ExecutionError("explicit test-destination confirmation is required")
        target = (
            env.get("TELEGRAM_TEST_CHAT_ID", "").strip()
            if channel == "telegram"
            else MAILPIT_REVIEW_RECIPIENT
        )
        return cls(channel, target, confirmed=True)


class TestChannelDispatcher:
    __test__ = False

    MAILPIT_REVIEW_SUBJECTS: Mapping[str, str] = {
        "en": "Synthetic abandoned-cart retention review",
        "ar": "سلتك بانتظارك",
    }

    def __init__(
        self,
        telegram: TelegramMessageSender | None,
        mailpit: EmailPort | None,
    ) -> None:
        self._telegram = telegram
        self._mailpit = mailpit

    @classmethod
    def mailpit_review_subject(cls, language: object) -> str:
        if not isinstance(language, str) or language not in cls.MAILPIT_REVIEW_SUBJECTS:
            raise ExecutionError("unsupported message language")
        return cls.MAILPIT_REVIEW_SUBJECTS[language]

    def send(
        self, destination: TestDestination, text: str, *, language: Literal["en", "ar"] = "en"
    ) -> str:
        if destination.channel == "telegram":
            if self._telegram is None:
                raise ExecutionError("Telegram sender is not configured")
            message_id = self._telegram.send_message(destination.target, text)
            if not isinstance(message_id, int) or isinstance(message_id, bool):
                raise ExecutionError("Telegram did not confirm a message ID")
            return str(message_id)
        if self._mailpit is None:
            raise ExecutionError("Mailpit sender is not configured")
        message = EmailMessage()
        message["From"] = MAILPIT_REVIEW_SENDER
        message["To"] = destination.target
        message["Subject"] = self.mailpit_review_subject(language)
        message["Message-ID"] = make_msgid()
        message.set_content(text)
        self._mailpit.send(message)
        return message["Message-ID"]


@dataclass(frozen=True)
class ExecutionSettings:
    synthetic_only: bool
    max_known_not_sent_attempts: int = 1

    def __post_init__(self) -> None:
        if self.synthetic_only is not True:
            raise ValueError("execution must be explicitly restricted to synthetic data")
        if (
            not isinstance(self.max_known_not_sent_attempts, int)
            or isinstance(self.max_known_not_sent_attempts, bool)
            or not 1 <= self.max_known_not_sent_attempts <= 5
        ):
            raise ValueError("known-not-sent attempts must be between 1 and 5")

    @classmethod
    def from_env(cls, env: Mapping[str, str]) -> ExecutionSettings:
        try:
            attempts = int(env.get("EXECUTION_MAX_KNOWN_NOT_SENT_ATTEMPTS", "1"))
        except ValueError as error:
            raise ValueError("execution retry limit must be an integer") from error
        return cls(
            synthetic_only=env.get("EXECUTION_SYNTHETIC_ONLY") == "YES",
            max_known_not_sent_attempts=attempts,
        )


@dataclass(frozen=True)
class ExecutionResult:
    run_id: uuid.UUID
    state: RunState
    status: Literal["completed", "held"]


class ExecutionService:
    def __init__(
        self,
        engine: Engine,
        carts: CurrentCartReader,
        gate: Gate,
        coupons: CouponPort,
        dispatcher: MessagePort,
        settings: ExecutionSettings,
    ) -> None:
        self._runs = RunRepository(engine)
        self._carts = carts
        self._gate = gate
        self._coupons = coupons
        self._dispatcher = dispatcher
        self._settings = settings

    def execute(self, run_id: uuid.UUID, *, destination: TestDestination) -> ExecutionResult:
        run = self._runs.get_run(run_id)
        if run.trigger_type != "synthetic":
            raise ExecutionError("only a synthetic run can be executed in this path")
        try:
            record = self._runs.get_execution_record(run_id)
        except ExecutionConflict as error:
            raise ExecutionError("proposal or human approval is not ready for execution") from error
        if record.state is not RunState.EVALUATING_POLICY:
            raise ExecutionError("run is not awaiting execution authorization")
        if record.proposal.get("execution_channel") != destination.channel:
            raise ExecutionError("execution channel differs from the approved proposal")
        proposal = offer_from_record(record.proposal)
        now = datetime.now(UTC)
        try:
            self._check_cart(record.proposal, proposal, now)
            approval = self._gate.issue_approval(
                proposal, now=now, human_approved=record.human_approved
            )
        except GateRejected as error:
            self._runs.transition(
                run_id,
                RunState.EVALUATING_POLICY,
                RunState.REJECTED,
                actor="worker",
                detail={
                    "reason": "execution_gate_rejected",
                    "reasons": [reason.value for reason in error.reasons],
                },
            )
            raise ExecutionError("current financial gate rejected execution") from error
        except ExecutionError:
            self._runs.transition(
                run_id,
                RunState.EVALUATING_POLICY,
                RunState.REJECTED,
                actor="worker",
                detail={"reason": "execution_context_rejected"},
            )
            raise
        self._runs.authorize_execution(run_id, context_hash=record.context_hash)
        binding_hash = snapshot_hash(
            {"context_hash": record.context_hash, "destination_hash": destination.destination_hash}
        )
        claim_id = self._runs.claim_execution(
            run_id, binding_hash=binding_hash, context_hash=record.context_hash
        )
        needs_coupon = (
            proposal.proposed_discount_percent is not None
            and proposal.proposed_discount_percent > 0
        )
        message = proposal.customer_message
        if message is None or not message.strip():
            self._runs.fail(run_id, reason="missing_approved_message", actor="worker")
            raise ExecutionError("approved proposal has no message")
        if needs_coupon and coupon_code_for_run(str(run_id)) not in message:
            self._runs.fail(run_id, reason="coupon_not_bound_to_approved_message", actor="worker")
            raise ExecutionError("approved message does not contain its bound coupon code")

        if needs_coupon:
            self._revalidate_or_fail(
                run_id, record.context_hash, record.proposal, proposal, approval
            )
            code = self._run_effect(
                run_id,
                claim_id,
                binding_hash,
                effect="coupon",
                action=lambda: self._coupons.create(approval, proposal),
            )
            if code != coupon_code_for_run(str(run_id)):
                self._runs.fail(run_id, reason="coupon_code_mismatch", actor="worker")
                raise ExecutionError("Odoo coupon does not match the approved message")

        self._revalidate_or_fail(run_id, record.context_hash, record.proposal, proposal, approval)
        self._run_effect(
            run_id,
            claim_id,
            binding_hash,
            effect="message",
            action=lambda: self._dispatcher.send(destination, message, language=proposal.language),
        )
        self._runs.complete_execution(
            run_id, claim_id, binding_hash=binding_hash, needs_coupon=needs_coupon
        )
        return ExecutionResult(run_id=run_id, state=RunState.COMPLETED, status="completed")

    def _check_before_effect(
        self,
        run_id: uuid.UUID,
        context_hash: str,
        stored_proposal: dict[str, object],
        proposal: OfferProposal,
        approval: GateApproval,
        *,
        now: datetime | None = None,
    ) -> None:
        moment = now or datetime.now(UTC)
        try:
            record = self._runs.get_execution_record(run_id)
        except ExecutionConflict as error:
            raise ExecutionError("approved proposal changed during execution") from error
        if record.state is not RunState.EXECUTING or record.context_hash != context_hash:
            raise ExecutionError("approved proposal changed during execution")
        if record.proposal != stored_proposal:
            raise ExecutionError("execution proposal binding changed")
        try:
            order_id = int(proposal.cart_id)
        except ValueError as error:
            raise ExecutionError("approved cart ID is invalid") from error
        if order_id <= 0 or str(order_id) != proposal.cart_id:
            raise ExecutionError("approved cart ID is invalid")
        current_cart = self._carts.get_abandoned_cart(order_id, now=moment)
        if current_cart is None:
            raise ExecutionError("cart is no longer eligible")
        self._validate_cart_snapshot(stored_proposal, current_cart, proposal)
        try:
            fresh = self._gate.issue_approval(
                proposal, now=moment, human_approved=record.human_approved
            )
            self._gate.verify(fresh, proposal, now=moment)
        except GateRejected as error:
            raise ExecutionError("current financial gate rejected execution") from error
        if fresh.fingerprint != approval.fingerprint:
            raise ExecutionError("gate authorization changed during execution")
        if record.human_approved:
            try:
                approval_fingerprint = self._runs.current_human_approval(run_id)
            except ApprovalConflict as error:
                raise ExecutionError("human approval is no longer valid") from error
            if approval_fingerprint != proposal.fingerprint():
                raise ExecutionError("human approval is no longer valid")

    def _revalidate_or_fail(
        self,
        run_id: uuid.UUID,
        context_hash: str,
        stored_proposal: dict[str, object],
        proposal: OfferProposal,
        approval: GateApproval,
    ) -> None:
        try:
            self._check_before_effect(run_id, context_hash, stored_proposal, proposal, approval)
        except ExecutionError:
            self._runs.fail(run_id, reason="execution_revalidation_failed", actor="worker")
            raise

    def _check_cart(
        self, stored_proposal: dict[str, object], proposal: OfferProposal, now: datetime
    ) -> None:
        try:
            order_id = int(proposal.cart_id)
        except ValueError as error:
            raise ExecutionError("approved cart ID is invalid") from error
        if order_id <= 0 or str(order_id) != proposal.cart_id:
            raise ExecutionError("approved cart ID is invalid")
        current_cart = self._carts.get_abandoned_cart(order_id, now=now)
        if current_cart is None:
            raise ExecutionError("cart is missing or no longer abandoned")
        self._validate_cart_snapshot(stored_proposal, current_cart, proposal)

    @staticmethod
    def _validate_cart_snapshot(
        stored_proposal: dict[str, object], cart: AbandonedCart, proposal: OfferProposal
    ) -> None:
        expected = stored_proposal.get("cart_snapshot")
        if not isinstance(expected, dict) or snapshot_hash(expected) != snapshot_hash(
            cart_snapshot(cart)
        ):
            raise ExecutionError("cart changed since proposal or approval")
        total, cost = cart_to_proposal_inputs(cart)
        categories = {line.category for line in cart.lines}
        category = next(iter(categories)) if len(categories) == 1 else ""
        if (
            str(cart.order_id) != proposal.cart_id
            or str(cart.partner_id) != proposal.customer_id
            or str(cart.partner_id) != proposal.cart_owner_id
            or cart.currency != proposal.currency
            or total != proposal.eligible_total
            or cost != proposal.eligible_unit_cost_total
            or category != proposal.category
        ):
            raise ExecutionError("current cart does not match the approved offer")

    def _run_effect(
        self,
        run_id: uuid.UUID,
        claim_id: uuid.UUID,
        binding_hash: str,
        *,
        effect: Literal["coupon", "message"],
        action: Callable[[], str],
    ) -> str:
        for attempt in range(1, self._settings.max_known_not_sent_attempts + 1):
            self._runs.start_effect(
                run_id,
                claim_id,
                binding_hash=binding_hash,
                effect=effect,
                max_attempts=self._settings.max_known_not_sent_attempts,
            )
            try:
                reference = action()
            except KnownNotSent:
                self._runs.finish_effect(
                    run_id,
                    claim_id,
                    binding_hash=binding_hash,
                    effect=effect,
                    attempt=attempt,
                    status="not_sent",
                )
                if attempt < self._settings.max_known_not_sent_attempts:
                    continue
                self._runs.fail(
                    run_id, reason=f"{effect}_known_not_sent_retry_limit", actor="worker"
                )
                raise ExecutionError(f"{effect} was not sent after configured attempts") from None
            except Exception:
                self._runs.finish_effect(
                    run_id,
                    claim_id,
                    binding_hash=binding_hash,
                    effect=effect,
                    attempt=attempt,
                    status="unknown",
                )
                raise ExecutionHeld(
                    f"{effect} outcome is unknown; operator reconciliation required"
                ) from None
            if not isinstance(reference, str) or not reference.strip():
                self._runs.finish_effect(
                    run_id,
                    claim_id,
                    binding_hash=binding_hash,
                    effect=effect,
                    attempt=attempt,
                    status="unknown",
                )
                raise ExecutionHeld(
                    f"{effect} returned no confirmation; operator reconciliation required"
                )
            self._runs.finish_effect(
                run_id,
                claim_id,
                binding_hash=binding_hash,
                effect=effect,
                attempt=attempt,
                status="confirmed",
                reference=reference,
            )
            return reference
        raise ExecutionError("effect attempts exhausted")
