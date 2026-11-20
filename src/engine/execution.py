from __future__ import annotations

import hashlib
import hmac
import uuid
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime
from email.message import EmailMessage
from email.utils import make_msgid
from typing import Literal, Protocol, cast

from sqlalchemy import Engine

from engine import telemetry
from engine.approval_data import offer_from_record, snapshot_hash
from engine.channels import EmailPort, TelegramMessageSender
from engine.coupon_code import coupon_code_for_run
from engine.db import ApprovalConflict, ApprovalExpired, ExecutionConflict, RunRepository
from engine.fixed_rule import FIXED_SCHEMA
from engine.odoo import AbandonedCart, cart_snapshot, cart_to_proposal_inputs
from engine.policy import Gate, GateApproval, GateRejected, OfferProposal
from engine.privacy import (
    TOKEN_LIKE,
    AllowlistGuard,
    PrivacyError,
    PrivacySettings,
    consent_channel,
    has_opt_in,
    is_erased,
    rehydrate,
)
from engine.runs import RunState
from engine.runtime_state import RuntimeGuard, RuntimeState

MAILPIT_REVIEW_RECIPIENT = "retention-review@example.test"
MAILPIT_REVIEW_SENDER = "retention-engine@example.test"


class ExecutionError(RuntimeError):
    """Execution was refused or failed without claiming successful delivery."""


class ExecutionHeld(ExecutionError):
    """An external effect has an unresolved outcome and must not be retried automatically."""


class KnownNotSent(ExecutionError):
    """A port proves it failed before the external effect could be accepted."""


class ApprovalExpiredError(ExecutionError):
    """The human approval passed its `expires_at`; nothing was claimed or sent (spec AA-7)."""


class EngineHeldError(ExecutionError):
    """The runtime state forbids this execution (spec KS-3, KS-4). Nothing was sent."""

    def __init__(self, reason: Literal["engine_halted", "engine_fallback"]) -> None:
        super().__init__(
            "engine is halted" if reason == "engine_halted" else "engine is in fallback mode"
        )
        self.reason = reason


class PrivacyRejected(ExecutionError):
    """A Q12 consent, blacklist, erasure, allowlist, or PII token control refused execution."""


class ReadinessRejected(ExecutionError):
    """An ERP readiness preflight (for example F-042 loyalty module) failed."""


class CurrentCartReader(Protocol):
    def get_abandoned_cart(self, order_id: int, *, now: datetime) -> AbandonedCart | None: ...


@dataclass(frozen=True)
class RecipientDestination:
    """An allowlisted ERP recipient derived from live Odoo context. Never persisted."""

    channel: Literal["telegram", "mailpit"]
    target: str = field(repr=False)
    destination_hash: str


class RecipientPort(Protocol):
    def send_to(
        self, destination: RecipientDestination, text: str, *, language: Literal["en", "ar"]
    ) -> str: ...


@dataclass(frozen=True)
class ErpExecution:
    """Q12 controls for generalized ERP execution; all are required (fail closed)."""

    privacy: PrivacySettings
    allowlist: AllowlistGuard
    dispatcher: RecipientPort
    loyalty_ready: Callable[[], None]
    # Odoo has no customer Telegram field: ERP Telegram goes to this allowlisted chat only.
    telegram_chat_id: str = field(default="", repr=False)


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


class AllowlistedDispatcher:
    """ERP dispatcher; re-checks the allowlist on every send (defense in depth)."""

    SENDER = MAILPIT_REVIEW_SENDER

    def __init__(
        self,
        allowlist: AllowlistGuard,
        telegram: TelegramMessageSender | None,
        email: EmailPort | None,
    ) -> None:
        self._allowlist = allowlist
        self._telegram = telegram
        self._email = email

    def send_to(
        self, destination: RecipientDestination, text: str, *, language: Literal["en", "ar"]
    ) -> str:
        try:
            self._allowlist.require(consent_channel(destination.channel), destination.target)
        except PrivacyError as error:
            raise KnownNotSent("recipient is not allowlisted") from error
        if TOKEN_LIKE.search(text):
            raise KnownNotSent("message still contains a PII token")
        if destination.channel == "telegram":
            if self._telegram is None:
                raise KnownNotSent("Telegram sender is not configured")
            message_id = self._telegram.send_message(destination.target, text)
            if not isinstance(message_id, int) or isinstance(message_id, bool):
                raise ExecutionError("Telegram did not confirm a message ID")
            return str(message_id)
        if self._email is None:
            raise KnownNotSent("email sender is not configured")
        message = EmailMessage()
        message["From"] = self.SENDER
        message["To"] = destination.target
        message["Subject"] = TestChannelDispatcher.mailpit_review_subject(language)
        message["Message-ID"] = make_msgid()
        message.set_content(text)
        self._email.send(message)
        return message["Message-ID"]


@dataclass(frozen=True)
class ExecutionSettings:
    synthetic_only: bool
    max_known_not_sent_attempts: int = 1
    erp_enabled: bool = False

    def __post_init__(self) -> None:
        if self.synthetic_only is not True and self.erp_enabled is not True:
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
            erp_enabled=env.get("EXECUTION_ERP_MODE") == "YES",
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
        *,
        erp: ErpExecution | None = None,
        runtime: RuntimeGuard | None = None,
    ) -> None:
        self._runtime = runtime
        self._engine = engine
        self._runs = RunRepository(engine)
        self._carts = carts
        self._gate = gate
        self._coupons = coupons
        self._dispatcher = dispatcher
        self._settings = settings
        self._erp = erp

    def execute(self, run_id: uuid.UUID, *, destination: TestDestination) -> ExecutionResult:
        if not self._settings.synthetic_only:
            raise ExecutionError("synthetic execution is not enabled")
        run = self._runs.get_run(run_id)
        if run.trigger_type != "synthetic":
            raise ExecutionError("only a synthetic run can be executed in this path")
        return self._execute(run_id, test_destination=destination)

    def execute_erp(self, run_id: uuid.UUID) -> ExecutionResult:
        """Q12 generalized ERP execution: consent, blacklist, erasure, and allowlist gated."""
        if not self._settings.erp_enabled or self._erp is None:
            raise ExecutionError("ERP execution is not enabled")
        run = self._runs.get_run(run_id)
        if run.trigger_type != "erp":
            raise ExecutionError("only an ERP run can be executed in this path")
        return self._execute(run_id, test_destination=None)

    def preview_erp_recipient(self, run_id: uuid.UUID) -> RecipientDestination:
        """Run every ERP privacy check without any state change, for operator confirmation."""
        if not self._settings.erp_enabled or self._erp is None:
            raise ExecutionError("ERP execution is not enabled")
        if self._runs.get_run(run_id).trigger_type != "erp":
            raise ExecutionError("only an ERP run can be previewed")
        try:
            record = self._runs.get_execution_record(run_id)
        except ExecutionConflict as error:
            raise ExecutionError("proposal or human approval is not ready for execution") from error
        channel = record.proposal.get("execution_channel")
        if record.state is not RunState.EVALUATING_POLICY or channel not in {"telegram", "mailpit"}:
            raise ExecutionError("run is not awaiting execution authorization")
        proposal = offer_from_record(record.proposal)
        cart = self._check_cart(record.proposal, proposal, datetime.now(UTC))
        recipient = self._erp_recipient(record.proposal, proposal, cart, cast(str, channel))
        if proposal.customer_message is not None:
            self._rehydrate(record.proposal, proposal.customer_message, cart)
        return recipient

    def _check_runtime(self, schema_version: str) -> None:
        """Spec KS-3/KS-4: refuse while halted, and hold LLM proposals during fallback."""
        state = self._runtime.state() if self._runtime is not None else RuntimeState.ACTIVE
        if state is RuntimeState.HALTED:
            raise EngineHeldError("engine_halted")
        if state is RuntimeState.FALLBACK and schema_version != FIXED_SCHEMA:
            raise EngineHeldError("engine_fallback")

    def _reject_expired(self, run_id: uuid.UUID) -> ApprovalExpiredError:
        """Record that an expired approval stopped this run, from whichever state it is in."""
        current = self._runs.get_run(run_id)
        if current.state is RunState.EVALUATING_POLICY:
            self._runs.transition(
                run_id,
                RunState.EVALUATING_POLICY,
                RunState.REJECTED,
                actor="worker",
                detail={"reason": "approval_expired"},
                outcome="approval_expired",
            )
        else:
            self._runs.fail(run_id, reason="approval_expired", actor="worker")
        return ApprovalExpiredError("human approval has expired")

    def _execute(
        self, run_id: uuid.UUID, *, test_destination: TestDestination | None
    ) -> ExecutionResult:
        with telemetry.span(
            "aorte.execution",
            run_id=run_id,
            **{"aorte.run_id": str(run_id), "langfuse.trace.name": "aorte.run"},
        ) as span:
            try:
                result = self._execute_run(run_id, test_destination=test_destination)
            except ExecutionHeld:
                span.set("aorte.outcome", "held")
                raise
            except BaseException:
                span.set("aorte.outcome", "failed")
                raise
            span.set("aorte.outcome", "completed" if result.status == "completed" else None)
            return result

    def _execute_run(
        self, run_id: uuid.UUID, *, test_destination: TestDestination | None
    ) -> ExecutionResult:
        try:
            record = self._runs.get_execution_record(run_id)
        except ApprovalExpired as error:
            raise self._reject_expired(run_id) from error
        except ExecutionConflict as error:
            raise ExecutionError("proposal or human approval is not ready for execution") from error
        if record.state is not RunState.EVALUATING_POLICY:
            raise ExecutionError("run is not awaiting execution authorization")
        # Before the gate reserves budget and before any claim: a held run changes nothing.
        self._check_runtime(record.schema_version)
        channel = record.proposal.get("execution_channel")
        if test_destination is not None and channel != test_destination.channel:
            raise ExecutionError("execution channel differs from the approved proposal")
        if channel not in {"telegram", "mailpit"}:
            raise ExecutionError("execution channel is invalid")
        proposal = offer_from_record(record.proposal)
        needs_coupon = (
            proposal.proposed_discount_percent is not None
            and proposal.proposed_discount_percent > 0
        )
        now = datetime.now(UTC)
        recipient: RecipientDestination | None = None
        try:
            cart = self._check_cart(record.proposal, proposal, now)
            if test_destination is None:
                recipient = self._erp_recipient(record.proposal, proposal, cart, channel)
                if needs_coupon:
                    self._loyalty_ready()
            if proposal.customer_message is not None:
                # Validate tokens before any claim; the text itself is discarded here.
                self._rehydrate(record.proposal, proposal.customer_message, cart)
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
        except ExecutionError as error:
            reason = (
                "execution_privacy_rejected"
                if isinstance(error, PrivacyRejected)
                else "execution_readiness_rejected"
                if isinstance(error, ReadinessRejected)
                else "execution_context_rejected"
            )
            self._runs.transition(
                run_id,
                RunState.EVALUATING_POLICY,
                RunState.REJECTED,
                actor="worker",
                detail={"reason": reason},
            )
            raise
        destination_hash = (
            test_destination.destination_hash
            if test_destination is not None
            else cast(RecipientDestination, recipient).destination_hash
        )
        binding_hash = snapshot_hash(
            {"context_hash": record.context_hash, "destination_hash": destination_hash}
        )
        try:
            self._runs.authorize_execution(run_id, context_hash=record.context_hash)
            claim_id = self._runs.claim_execution(
                run_id, binding_hash=binding_hash, context_hash=record.context_hash
            )
        except ApprovalExpired as error:
            raise self._reject_expired(run_id) from error
        message = proposal.customer_message
        if message is None or not message.strip():
            self._runs.fail(run_id, reason="missing_approved_message", actor="worker")
            raise ExecutionError("approved proposal has no message")
        if needs_coupon and coupon_code_for_run(str(run_id)) not in message:
            self._runs.fail(run_id, reason="coupon_not_bound_to_approved_message", actor="worker")
            raise ExecutionError("approved message does not contain its bound coupon code")

        if needs_coupon:
            before_coupon = self._revalidate_or_fail(
                run_id, record.context_hash, record.proposal, proposal, approval
            )
            if test_destination is None:
                try:
                    current = self._erp_recipient(record.proposal, proposal, before_coupon, channel)
                    if current.destination_hash != destination_hash:
                        raise PrivacyRejected("recipient changed since the execution claim")
                except ExecutionError:
                    self._runs.fail(
                        run_id, reason="execution_privacy_revalidation_failed", actor="worker"
                    )
                    raise
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

        live = self._revalidate_or_fail(
            run_id, record.context_hash, record.proposal, proposal, approval
        )
        # TM-2: rehydrate only now, from the live cart; the text is never persisted.
        try:
            text = self._rehydrate(record.proposal, message, live)
            if test_destination is None:
                fresh = self._erp_recipient(record.proposal, proposal, live, channel)
                if fresh.destination_hash != destination_hash:
                    raise PrivacyRejected("recipient changed since the execution claim")
        except ExecutionError:
            self._runs.fail(run_id, reason="execution_privacy_revalidation_failed", actor="worker")
            raise
        if test_destination is not None:
            target = test_destination

            def send() -> str:
                return self._dispatcher.send(target, text, language=proposal.language)

        else:
            erp, bound = cast(ErpExecution, self._erp), fresh

            def send() -> str:
                return erp.dispatcher.send_to(bound, text, language=proposal.language)

        self._run_effect(run_id, claim_id, binding_hash, effect="message", action=send)
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
    ) -> AbandonedCart:
        moment = now or datetime.now(UTC)
        try:
            record = self._runs.get_execution_record(run_id)
        except ApprovalExpired as error:
            raise ApprovalExpiredError("human approval has expired") from error
        except ExecutionConflict as error:
            raise ExecutionError("approved proposal changed during execution") from error
        if record.state is not RunState.EXECUTING or record.context_hash != context_hash:
            raise ExecutionError("approved proposal changed during execution")
        self._check_runtime(record.schema_version)
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
            except ApprovalExpired as error:
                raise ApprovalExpiredError("human approval has expired") from error
            except ApprovalConflict as error:
                raise ExecutionError("human approval is no longer valid") from error
            if approval_fingerprint != proposal.fingerprint():
                raise ExecutionError("human approval is no longer valid")
        return current_cart

    def _revalidate_or_fail(
        self,
        run_id: uuid.UUID,
        context_hash: str,
        stored_proposal: dict[str, object],
        proposal: OfferProposal,
        approval: GateApproval,
    ) -> AbandonedCart:
        try:
            return self._check_before_effect(
                run_id, context_hash, stored_proposal, proposal, approval
            )
        except ExecutionError as error:
            if isinstance(error, ApprovalExpiredError):
                reason = "approval_expired"
            elif isinstance(error, EngineHeldError):
                reason = error.reason
            else:
                reason = "execution_revalidation_failed"
            self._runs.fail(run_id, reason=reason, actor="worker")
            raise

    @staticmethod
    def _rehydrate(stored_proposal: dict[str, object], text: str, cart: AbandonedCart) -> str:
        """TM-2/TM-4: restore tokens from the same customer's live cart, or fail closed."""
        token_map = stored_proposal.get("pii_tokens")
        if token_map is None:
            # Proposals recorded before #85 carry no tokens; any token-like text is refused.
            if TOKEN_LIKE.search(text):
                raise PrivacyRejected("message contains a PII token without a token map")
            return text
        if not isinstance(token_map, dict):
            raise PrivacyRejected("PII token map is invalid")
        try:
            return rehydrate(text, token_map, cart)
        except PrivacyError as error:
            raise PrivacyRejected(str(error)) from error

    def _loyalty_ready(self) -> None:
        try:
            cast(ErpExecution, self._erp).loyalty_ready()
        except Exception as error:
            raise ReadinessRejected("website_sale_loyalty readiness preflight failed") from error

    def _erp_recipient(
        self,
        stored_proposal: dict[str, object],
        proposal: OfferProposal,
        cart: AbandonedCart,
        channel: str,
    ) -> RecipientDestination:
        """Q12 PR-1/PR-2: opt-in, blacklist, erasure, and allowlist, all fail closed."""
        erp = cast(ErpExecution, self._erp)
        token_map = stored_proposal.get("pii_tokens")
        if not isinstance(token_map, dict) or token_map.get("customer_id") != proposal.customer_id:
            raise PrivacyRejected("PII token map does not belong to this customer")
        if str(cart.partner_id) != proposal.customer_id:
            raise PrivacyRejected("live cart belongs to another customer")
        try:
            kind = consent_channel(channel)
            with self._engine.connect() as conn:
                if is_erased(conn, erp.privacy, proposal.customer_id):
                    raise PrivacyRejected("customer has an erasure tombstone")
                if not has_opt_in(conn, proposal.customer_id, kind):
                    raise PrivacyRejected("customer has no explicit opt-in for this channel")
        except PrivacyError as error:
            raise PrivacyRejected(str(error)) from error
        if cart.email_blacklisted is not False:
            raise PrivacyRejected("customer email is blacklisted in Odoo or status is unknown")
        target = cart.partner_email.strip() if kind == "email" else erp.telegram_chat_id.strip()
        if not erp.allowlist.allows(kind, target):
            raise PrivacyRejected("recipient is not in the outbound allowlist")
        digest = hmac.new(
            erp.privacy.tombstone_pepper.encode(),
            f"{channel}:{target.lower()}".encode(),
            hashlib.sha256,
        ).hexdigest()
        return RecipientDestination(cast(Literal["telegram", "mailpit"], channel), target, digest)

    def _check_cart(
        self, stored_proposal: dict[str, object], proposal: OfferProposal, now: datetime
    ) -> AbandonedCart:
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
        return current_cart

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
            with telemetry.span(
                "aorte.effect",
                **{"aorte.run_id": str(run_id), "aorte.effect": effect, "aorte.attempt": attempt},
            ) as span:
                outcome, reference = self._attempt_effect(
                    run_id, claim_id, binding_hash, effect=effect, action=action, attempt=attempt
                )
                span.set("aorte.outcome", outcome)
            telemetry.count(
                "aorte.execution.effects", 1, {"aorte.effect": effect, "aorte.outcome": outcome}
            )
            if outcome == "not_sent":
                if attempt < self._settings.max_known_not_sent_attempts:
                    continue
                self._runs.fail(
                    run_id, reason=f"{effect}_known_not_sent_retry_limit", actor="worker"
                )
                raise ExecutionError(f"{effect} was not sent after configured attempts")
            if outcome == "unknown":
                raise ExecutionHeld(
                    f"{effect} outcome is unknown; operator reconciliation required"
                    if reference is None
                    else f"{effect} returned no confirmation; operator reconciliation required"
                )
            return cast(str, reference)
        raise ExecutionError("effect attempts exhausted")

    def _attempt_effect(
        self,
        run_id: uuid.UUID,
        claim_id: uuid.UUID,
        binding_hash: str,
        *,
        effect: Literal["coupon", "message"],
        action: Callable[[], str],
        attempt: int,
    ) -> tuple[Literal["confirmed", "not_sent", "unknown"], str | None]:
        """One journaled attempt; never retries by itself (EX-4).

        `unknown` with reference None: the action raised. `unknown` with "": no confirmation.
        """
        self._runs.start_effect(
            run_id,
            claim_id,
            binding_hash=binding_hash,
            effect=effect,
            max_attempts=self._settings.max_known_not_sent_attempts,
        )
        status: Literal["confirmed", "not_sent", "unknown"]
        reference: str | None
        try:
            reference = action()
        except KnownNotSent:
            status, reference = "not_sent", None
        except Exception:
            status, reference = "unknown", None
        else:
            if isinstance(reference, str) and reference.strip():
                status = "confirmed"
            else:
                status, reference = "unknown", ""
        self._runs.finish_effect(
            run_id,
            claim_id,
            binding_hash=binding_hash,
            effect=effect,
            attempt=attempt,
            status=status,
            reference=reference if status == "confirmed" else None,
        )
        return status, reference
