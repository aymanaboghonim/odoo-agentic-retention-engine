from __future__ import annotations

import json
import os
import uuid
from collections.abc import Iterator
from dataclasses import asdict, replace
from datetime import UTC, datetime
from decimal import Decimal
from email.message import EmailMessage

import pytest
import sqlalchemy as sa

from engine.channels import ChannelError
from engine.config import normalize_database_url
from engine.coupon_code import coupon_code_for_run
from engine.db import RunRepository, create_engine, runs, upgrade_schema
from engine.execution import (
    MAILPIT_REVIEW_RECIPIENT,
    ExecutionError,
    ExecutionHeld,
    ExecutionService,
    ExecutionSettings,
    KnownNotSent,
    TestChannelDispatcher,
    TestDestination,
)
from engine.gate_ledger import InMemoryGateLedger
from engine.odoo import AbandonedCart, CartLine, cart_snapshot
from engine.policy import Gate, GateApproval, GateConfig, OfferProposal, as_dict
from engine.runs import RunState
from scripts.gated_execution_review import _create_received_run

URL = os.environ.get("TEST_DATABASE_URL", "")
if not URL and os.environ.get("REQUIRE_DB_TESTS") == "1":
    raise RuntimeError("REQUIRE_DB_TESTS=1 but TEST_DATABASE_URL is not set")


@pytest.fixture
def db() -> Iterator[sa.Engine]:
    if not URL:
        pytest.skip("TEST_DATABASE_URL not set")
    engine = create_engine(normalize_database_url(URL))
    upgrade_schema(engine)
    yield engine
    engine.dispose()


def _gate() -> Gate:
    config = GateConfig.from_env(
        {
            "GATE_TIMEZONE": "Asia/Riyadh",
            "GATE_CURRENCY": "USD",
            "GATE_DAILY_BUDGET": "100",
            "GATE_CATEGORY_MIN_MARGINS": '{"decor":"0.2"}',
        }
    )
    return Gate(config, InMemoryGateLedger())


def _cart() -> AbandonedCart:
    return AbandonedCart(
        order_id=1,
        reference="SYNTH-1",
        partner_id=9,
        partner_name="Synthetic",
        partner_email="synthetic@example.test",
        language="en",
        currency="USD",
        amount_total=Decimal("100"),
        date_order=datetime(2026, 11, 1, tzinfo=UTC),
        lines=(CartLine(1, "Lamp", "L1", "decor", Decimal(1), Decimal("100"), Decimal("40")),),
    )


class Carts:
    def __init__(self, value: AbandonedCart) -> None:
        self.value = value
        self.calls = 0
        self.change_after: int | None = None

    def get_abandoned_cart(self, order_id: int, *, now: datetime) -> AbandonedCart | None:
        self.calls += 1
        if self.change_after == self.calls:
            self.value = replace(
                self.value,
                lines=(replace(self.value.lines[0], unit_price=Decimal("90")),),
            )
        return self.value if order_id == self.value.order_id else None


class Coupons:
    def __init__(self, *, code: str | None = None) -> None:
        self.calls = 0
        self.code = code

    def create(self, approval: GateApproval, proposal: OfferProposal) -> str:
        self.calls += 1
        return self.code or coupon_code_for_run(proposal.run_id)


class Dispatcher:
    def __init__(self) -> None:
        self.calls: list[tuple[TestDestination, str]] = []
        self.failures: list[Exception] = []

    def send(
        self,
        destination: TestDestination,
        text: str,
        *,
        language: str = "en",
    ) -> str:
        self.calls.append((destination, text))
        if self.failures:
            raise self.failures.pop(0)
        return f"message-{len(self.calls)}"


def _create_run(
    engine: sa.Engine, *, discount: str = "0", approve: bool = True, channel: str = "mailpit"
) -> tuple[uuid.UUID, Gate, AbandonedCart]:
    repo = RunRepository(engine)
    cart = _cart()
    run, _ = repo.create_run(
        trigger_type="synthetic",
        trigger_ref="9:1",
        idempotency_key=str(uuid.uuid4()),
    )
    for current, new in [
        (RunState.RECEIVED, RunState.VALIDATING_INPUT),
        (RunState.VALIDATING_INPUT, RunState.LOADING_CONTEXT),
        (RunState.LOADING_CONTEXT, RunState.GENERATING_PROPOSAL),
    ]:
        repo.transition(run.id, current, new, actor="test")
    message = "Your cart is waiting."
    if Decimal(discount) > 0:
        message += f"\n\nCoupon code for {discount}% discount: {coupon_code_for_run(str(run.id))}"
    offer = OfferProposal(
        run_id=str(run.id),
        customer_id="9",
        cart_id="1",
        cart_owner_id="9",
        category="decor",
        currency="USD",
        eligible_total=Decimal("100"),
        eligible_unit_cost_total=Decimal("40"),
        proposed_discount_percent=Decimal(discount),
        customer_message=message,
    )
    proposal = json.loads(json.dumps(asdict(offer), default=str))
    proposal.update(
        {
            "action": "draft_message",
            "cart_snapshot": cart_snapshot(cart),
            "execution_channel": channel,
        }
    )
    gate = _gate()
    repo.persist_proposal(
        run.id,
        schema_version="test-v1",
        fingerprint=offer.fingerprint(),
        proposal=proposal,
        gate_result=as_dict(gate.evaluate(offer, now=datetime.now(UTC))),
    )
    if Decimal(discount) > Decimal(10):
        repo.request_approval(run.id, gate)
        if approve:
            pending = next(
                item for item in repo.pending_approvals() if item["run_id"] == str(run.id)
            )
            repo.decide_approval(
                run.id,
                token=pending["snapshot_hash"],
                decision="approve",
                actor="synthetic-operator",
            )
    return run.id, gate, cart


def _service(
    engine: sa.Engine,
    gate: Gate,
    carts: Carts,
    coupons: Coupons,
    dispatcher: Dispatcher,
    *,
    retries: int = 1,
) -> ExecutionService:
    return ExecutionService(
        engine,
        carts,
        gate,
        coupons,
        dispatcher,
        ExecutionSettings(synthetic_only=True, max_known_not_sent_attempts=retries),
    )


def _clean(db: sa.Engine, run_id: uuid.UUID) -> None:
    with db.begin() as conn:
        for table in (
            "execution_effects",
            "execution_claims",
            "approval_requests",
            "agent_proposals",
            "run_events",
        ):
            conn.execute(sa.text(f"DELETE FROM {table} WHERE run_id=:run"), {"run": run_id})
        conn.execute(runs.delete().where(runs.c.id == run_id))


def test_model_review_run_starts_in_received_state(db: sa.Engine) -> None:
    repo = RunRepository(db)
    run_id = _create_received_run(repo, _cart())
    try:
        assert repo.get_run(run_id).state is RunState.RECEIVED
    finally:
        _clean(db, run_id)


def test_zero_discount_sends_without_coupon_and_completes(db: sa.Engine) -> None:
    run_id, gate, cart = _create_run(db)
    coupons, dispatcher = Coupons(), Dispatcher()
    destination = TestDestination("mailpit", MAILPIT_REVIEW_RECIPIENT, confirmed=True)
    try:
        result = _service(db, gate, Carts(cart), coupons, dispatcher).execute(
            run_id, destination=destination
        )
        assert result.status == "completed" and result.state is RunState.COMPLETED
        assert coupons.calls == 0
        assert dispatcher.calls == [(destination, "Your cart is waiting.")]
        with db.connect() as conn:
            effects = conn.execute(
                sa.text("SELECT effect,status FROM execution_effects WHERE run_id=:run"),
                {"run": run_id},
            ).all()
        assert effects == [("message", "confirmed")]
    finally:
        _clean(db, run_id)


def test_repeated_execution_does_not_repeat_completed_effects(db: sa.Engine) -> None:
    run_id, gate, cart = _create_run(db)
    coupons, dispatcher = Coupons(), Dispatcher()
    service = _service(db, gate, Carts(cart), coupons, dispatcher)
    destination = TestDestination("mailpit", MAILPIT_REVIEW_RECIPIENT, confirmed=True)
    try:
        service.execute(run_id, destination=destination)
        with pytest.raises(ExecutionError, match="not awaiting"):
            service.execute(run_id, destination=destination)
        assert coupons.calls == 0
        assert len(dispatcher.calls) == 1
    finally:
        _clean(db, run_id)


def test_approved_discount_creates_matching_coupon_before_dispatch(db: sa.Engine) -> None:
    run_id, gate, cart = _create_run(db, discount="12", channel="telegram")
    coupons, dispatcher = Coupons(), Dispatcher()
    try:
        result = _service(db, gate, Carts(cart), coupons, dispatcher).execute(
            run_id, destination=TestDestination("telegram", "synthetic-chat", confirmed=True)
        )
        assert result.state is RunState.COMPLETED
        assert coupons.calls == 1
        assert len(dispatcher.calls) == 1
        assert coupon_code_for_run(str(run_id)) in dispatcher.calls[0][1]
        with db.connect() as conn:
            effects = conn.execute(
                sa.text(
                    "SELECT effect,status FROM execution_effects WHERE run_id=:run ORDER BY effect"
                ),
                {"run": run_id},
            ).all()
        assert effects == [("coupon", "confirmed"), ("message", "confirmed")]
    finally:
        _clean(db, run_id)


def test_missing_human_approval_has_no_side_effect(db: sa.Engine) -> None:
    run_id, gate, cart = _create_run(db, discount="12", approve=False)
    coupons, dispatcher = Coupons(), Dispatcher()
    try:
        with pytest.raises(ExecutionError):
            _service(db, gate, Carts(cart), coupons, dispatcher).execute(
                run_id,
                destination=TestDestination("mailpit", MAILPIT_REVIEW_RECIPIENT, confirmed=True),
            )
        assert coupons.calls == 0 and not dispatcher.calls
        assert RunRepository(db).get_run(run_id).state is RunState.AWAITING_APPROVAL
    finally:
        _clean(db, run_id)


def test_changed_cart_is_rejected_before_claim_or_effect(db: sa.Engine) -> None:
    run_id, gate, cart = _create_run(db, discount="12")
    changed = replace(cart, lines=(replace(cart.lines[0], unit_price=Decimal("90")),))
    coupons, dispatcher = Coupons(), Dispatcher()
    try:
        with pytest.raises(ExecutionError, match="cart changed"):
            _service(db, gate, Carts(changed), coupons, dispatcher).execute(
                run_id,
                destination=TestDestination("mailpit", MAILPIT_REVIEW_RECIPIENT, confirmed=True),
            )
        assert coupons.calls == 0 and not dispatcher.calls
        assert RunRepository(db).get_run(run_id).state is RunState.REJECTED
    finally:
        _clean(db, run_id)


def test_change_after_coupon_holds_before_dispatch(db: sa.Engine) -> None:
    run_id, gate, cart = _create_run(db, discount="12", channel="telegram")
    carts, coupons, dispatcher = Carts(cart), Coupons(), Dispatcher()
    carts.change_after = 3
    try:
        with pytest.raises(ExecutionError, match="cart changed"):
            _service(db, gate, carts, coupons, dispatcher).execute(
                run_id, destination=TestDestination("telegram", "synthetic-chat", confirmed=True)
            )
        assert coupons.calls == 1
        assert dispatcher.calls == []
        assert RunRepository(db).get_run(run_id).state is RunState.FAILED
        with db.connect() as conn:
            status = conn.execute(
                sa.text(
                    "SELECT status FROM execution_effects WHERE run_id=:run AND effect='coupon'"
                ),
                {"run": run_id},
            ).scalar_one()
        assert status == "confirmed"
    finally:
        _clean(db, run_id)


def test_ambiguous_dispatch_is_held_without_automatic_retry(db: sa.Engine) -> None:
    run_id, gate, cart = _create_run(db, channel="telegram")
    dispatcher = Dispatcher()
    dispatcher.failures.append(ChannelError("ambiguous response"))
    try:
        with pytest.raises(ExecutionHeld, match="outcome is unknown"):
            _service(db, gate, Carts(cart), Coupons(), dispatcher, retries=3).execute(
                run_id, destination=TestDestination("telegram", "synthetic-chat", confirmed=True)
            )
        assert len(dispatcher.calls) == 1
        assert RunRepository(db).get_run(run_id).state is RunState.EXECUTING
        with db.connect() as conn:
            status = conn.execute(
                sa.text(
                    "SELECT status FROM execution_effects WHERE run_id=:run AND effect='message'"
                ),
                {"run": run_id},
            ).scalar_one()
        assert status == "unknown"
    finally:
        _clean(db, run_id)


def test_only_known_not_sent_errors_are_retried(db: sa.Engine) -> None:
    run_id, gate, cart = _create_run(db)
    dispatcher = Dispatcher()
    dispatcher.failures.append(KnownNotSent("connection was never established"))
    try:
        result = _service(db, gate, Carts(cart), Coupons(), dispatcher, retries=2).execute(
            run_id, destination=TestDestination("mailpit", MAILPIT_REVIEW_RECIPIENT, confirmed=True)
        )
        assert result.state is RunState.COMPLETED
        assert len(dispatcher.calls) == 2
        with db.connect() as conn:
            row = conn.execute(
                sa.text("SELECT status,attempts FROM execution_effects WHERE run_id=:run"),
                {"run": run_id},
            ).one()
        assert row.status == "confirmed" and row.attempts == 2
    finally:
        _clean(db, run_id)


def test_wrong_confirmed_channel_is_rejected_before_claim(db: sa.Engine) -> None:
    run_id, gate, cart = _create_run(db)
    coupons, dispatcher = Coupons(), Dispatcher()
    try:
        with pytest.raises(ExecutionError, match="channel differs"):
            _service(db, gate, Carts(cart), coupons, dispatcher).execute(
                run_id, destination=TestDestination("telegram", "synthetic-chat", confirmed=True)
            )
        assert coupons.calls == 0 and not dispatcher.calls
        assert RunRepository(db).get_run(run_id).state is RunState.EVALUATING_POLICY
    finally:
        _clean(db, run_id)


def test_unconfirmed_or_uncontrolled_destination_is_rejected() -> None:
    with pytest.raises(ExecutionError, match="explicitly confirmed"):
        TestDestination("telegram", "chat", confirmed=False)
    with pytest.raises(ExecutionError, match="fixed synthetic"):
        TestDestination("mailpit", "customer@example.com", confirmed=True)


def test_destination_and_retry_settings_fail_closed() -> None:
    with pytest.raises(ExecutionError, match="synthetic-only"):
        TestDestination.from_env({}, "mailpit")
    with pytest.raises(ExecutionError, match="confirmation"):
        TestDestination.from_env({"EXECUTION_SYNTHETIC_ONLY": "YES"}, "telegram")
    with pytest.raises(ExecutionError, match="required"):
        TestDestination.from_env(
            {
                "EXECUTION_SYNTHETIC_ONLY": "YES",
            },
            "telegram",
            confirm_dedicated_test_destination=True,
        )
    with pytest.raises(ValueError, match="retry limit"):
        ExecutionSettings.from_env(
            {
                "EXECUTION_SYNTHETIC_ONLY": "YES",
                "EXECUTION_MAX_KNOWN_NOT_SENT_ATTEMPTS": "many",
            }
        )


def test_channel_dispatcher_uses_confirmed_target_and_localized_subject() -> None:
    class Telegram:
        calls: list[tuple[str, str]] = []

        def send_message(self, chat_id: str, text: str) -> int:
            self.calls.append((chat_id, text))
            return 101

    class Mailpit:
        messages: list[EmailMessage] = []

        def send(self, message: EmailMessage) -> None:
            self.messages.append(message)

    telegram, mailpit = Telegram(), Mailpit()
    sender = TestChannelDispatcher(telegram, mailpit)
    chat = TestDestination("telegram", "dedicated-synthetic-chat", confirmed=True)
    assert sender.send(chat, "Synthetic message") == "101"
    assert telegram.calls == [("dedicated-synthetic-chat", "Synthetic message")]
    inbox = TestDestination("mailpit", MAILPIT_REVIEW_RECIPIENT, confirmed=True)
    message_id = sender.send(inbox, "MSA synthetic message")
    email = mailpit.messages[0]
    assert email["To"] == MAILPIT_REVIEW_RECIPIENT
    assert email["Subject"] == "Synthetic abandoned-cart retention review"
    assert "MSA synthetic message" in email.get_content()
    assert message_id == email["Message-ID"]
    msa_message_id = sender.send(inbox, "رسالة تجريبية بالعربية", language="ar")
    msa_email = mailpit.messages[1]
    assert msa_email["Subject"] == "سلتك بانتظارك"
    assert "رسالة تجريبية بالعربية" in msa_email.get_content()
    assert msa_message_id == msa_email["Message-ID"]


def test_proposal_language_is_bound_to_approval_fingerprint() -> None:
    proposal = OfferProposal(
        run_id="synthetic-run",
        customer_id="9",
        cart_id="1",
        cart_owner_id="9",
        category="decor",
        currency="USD",
        eligible_total=Decimal("100"),
        eligible_unit_cost_total=Decimal("40"),
        proposed_discount_percent=Decimal(0),
        customer_message="Your cart is waiting.",
    )
    assert replace(proposal, language="ar").fingerprint() != proposal.fingerprint()


def test_coupon_mismatch_never_dispatches(db: sa.Engine) -> None:
    run_id, gate, cart = _create_run(db, discount="12")
    coupons, dispatcher = Coupons(code="RET-WRONG"), Dispatcher()
    try:
        with pytest.raises(ExecutionError, match="does not match"):
            _service(db, gate, Carts(cart), coupons, dispatcher).execute(
                run_id,
                destination=TestDestination("mailpit", MAILPIT_REVIEW_RECIPIENT, confirmed=True),
            )
        assert coupons.calls == 1 and not dispatcher.calls
        assert RunRepository(db).get_run(run_id).state is RunState.FAILED
    finally:
        _clean(db, run_id)
