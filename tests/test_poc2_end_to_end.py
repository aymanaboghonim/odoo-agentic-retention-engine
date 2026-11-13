from __future__ import annotations

import json
import os
import uuid
from collections.abc import Iterator, Mapping, Sequence
from dataclasses import replace
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from typing import Literal

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
    TestDestination,
)
from engine.gate_ledger import PostgresGateLedger
from engine.odoo import AbandonedCart, CartLine
from engine.policy import Gate, GateApproval, GateConfig, OfferProposal
from engine.proposal import ProposalService, ProposalSettings
from engine.proposal_budget import Rate, ReviewBudget
from engine.proposal_provider import ProviderResult
from engine.retrieval import DIMENSIONS, MODEL_ID, PostgresRetriever, SearchResult
from engine.runs import RunState
from engine.synthetic_scan import SyntheticCartScanner
from scripts.poc2_delivery import PREFIX, preflight_prepare, verify_delivery_run
from scripts.poc2_evaluation_cases import RuntimeCase
from scripts.poc2_evaluation_live import preflight, verify_smoke_run
from scripts.poc2_evaluation_runner import (
    EVALUATION_PREFIX,
    REVIEW_ID,
    evaluate_case,
    validate_unattempted,
)
from scripts.s2_llm_eval import load_dataset

URL = os.environ.get("TEST_DATABASE_URL", "")
if not URL and os.environ.get("REQUIRE_DB_TESTS") == "1":
    raise RuntimeError("REQUIRE_DB_TESTS=1 but TEST_DATABASE_URL is not set")


class Carts:
    def __init__(self, order_id: int) -> None:
        self.cart = AbandonedCart(
            order_id=order_id,
            reference="retention-review-0123456789ab",
            partner_id=order_id + 1,
            partner_name="Synthetic",
            partner_email="retention-review-0123456789ab@example.test",
            language="en",
            currency="USD",
            amount_total=Decimal("100"),
            date_order=datetime(2026, 11, 1, tzinfo=UTC),
            lines=(CartLine(1, "Lamp", "L1", "decor", Decimal(1), Decimal(100), Decimal(40)),),
        )

    def scan_synthetic_review_carts(
        self, *, now: datetime, after_id: int, limit: int
    ) -> list[AbandonedCart]:
        return [self.cart] if after_id < self.cart.order_id and limit else []

    def get_cart(self, order_id: int) -> AbandonedCart | None:
        return self.cart if order_id == self.cart.order_id else None

    def get_abandoned_cart(self, order_id: int, *, now: datetime) -> AbandonedCart | None:
        return self.get_cart(order_id)

    def get_synthetic_review_cart(self, order_id: int, *, now: datetime) -> AbandonedCart | None:
        return self.get_cart(order_id)


class Policies:
    def search(self, query: str, language: str) -> list[SearchResult]:
        return [SearchResult("policy", "1", language, "Respect consent.", "hash", 0.1)]


class Embedder:
    model_id = MODEL_ID

    def embed(self, texts: Sequence[str]) -> Sequence[Sequence[float]]:
        return [[1.0, *([0.0] * (DIMENSIONS - 1))] for _ in texts]


class Provider:
    def __init__(self, discount: int) -> None:
        self.discount = discount

    def count_tokens(self, payload: Mapping[str, object]) -> int:
        return 10

    def generate(self, payload: Mapping[str, object], output_limit: int) -> ProviderResult:
        return ProviderResult(
            json.dumps(
                {
                    "action": "draft_message",
                    "customer_message": "Your cart is waiting.",
                    "price_claim": None,
                    "currency": None,
                    "proposed_discount_percent": self.discount,
                }
            ),
            10,
            20,
            True,
        )


class Effects:
    def __init__(self, *, ambiguous: bool = False) -> None:
        self.coupons: list[str] = []
        self.messages: list[tuple[TestDestination, str, str]] = []
        self.ambiguous = ambiguous

    def create(self, approval: GateApproval, proposal: OfferProposal) -> str:
        code = coupon_code_for_run(proposal.run_id)
        self.coupons.append(code)
        return code

    def send(
        self, destination: TestDestination, text: str, *, language: Literal["en", "ar"]
    ) -> str:
        self.messages.append((destination, text, language))
        if self.ambiguous:
            raise ChannelError("synthetic ambiguous delivery")
        return "synthetic-message"


@pytest.fixture
def scenario() -> Iterator[tuple[sa.Engine, uuid.UUID, Carts, Gate]]:
    if not URL:
        pytest.skip("TEST_DATABASE_URL not set")
    engine = create_engine(normalize_database_url(URL))
    upgrade_schema(engine)
    instance = str(uuid.uuid4())
    carts = Carts(uuid.uuid4().int % 1_000_000_000 + 1)
    policy_id = f"e2e-{instance}"
    with engine.begin() as conn:
        for language, config in (("en", "english"), ("ar", "arabic")):
            conn.execute(
                sa.text(
                    "INSERT INTO policy_passages "
                    "(source_id, language, version, content, content_hash, model_id, "
                    "embedding, search_vector) VALUES "
                    "(:source, :language, '1', :content, 'test', :model, "
                    "CAST(:vector AS vector), to_tsvector(CAST(:config AS regconfig), :content))"
                ),
                {
                    "source": policy_id,
                    "language": language,
                    "content": "Respect consent.",
                    "model": MODEL_ID,
                    "vector": "[1," + ",".join(["0"] * (DIMENSIONS - 1)) + "]",
                    "config": config,
                },
            )
    scanner = SyntheticCartScanner(engine, carts, instance_id=instance, page_size=2)
    assert scanner.scan_page(now=datetime.now(UTC)) == (1, 1)
    assert scanner.scan_page(now=datetime.now(UTC)) == (1, 0)
    with engine.connect() as conn:
        run_id = conn.execute(
            sa.select(runs.c.id).where(
                runs.c.idempotency_key == f"synthetic-scan:{instance}:{carts.cart.order_id}"
            )
        ).scalar_one()
    gate = Gate(
        GateConfig.from_env(
            {
                "GATE_TIMEZONE": "Asia/Riyadh",
                "GATE_CURRENCY": "USD",
                "GATE_DAILY_BUDGET": "100",
                "GATE_CATEGORY_MIN_MARGINS": '{"decor":"0.2"}',
            }
        ),
        PostgresGateLedger(engine),
    )
    yield engine, run_id, carts, gate
    with engine.begin() as conn:
        reservation = conn.execute(
            sa.text("SELECT day, amount FROM gate_coupon_reservations WHERE run_id=:run"),
            {"run": str(run_id)},
        ).first()
        if reservation is not None:
            conn.execute(
                sa.text("DELETE FROM gate_coupon_reservations WHERE run_id=:run"),
                {"run": str(run_id)},
            )
            conn.execute(
                sa.text("UPDATE gate_budget_days SET reserved=reserved-:amount WHERE day=:day"),
                {"amount": reservation.amount, "day": reservation.day},
            )
        for table in (
            "execution_effects",
            "execution_claims",
            "approval_requests",
            "agent_proposals",
            "proposal_calls",
            "proposal_budget",
            "run_events",
        ):
            conn.execute(sa.text(f"DELETE FROM {table} WHERE run_id=:run"), {"run": run_id})
        conn.execute(runs.delete().where(runs.c.id == run_id))
        conn.execute(sa.text("DELETE FROM synthetic_scan_cursors WHERE id=:id"), {"id": instance})
        conn.execute(sa.text("DELETE FROM policy_passages WHERE source_id=:id"), {"id": policy_id})
    engine.dispose()


def generate(
    scenario: tuple[sa.Engine, uuid.UUID, Carts, Gate],
    *,
    discount: int,
    channel: Literal["mailpit", "telegram"] = "mailpit",
    language: str = "en",
) -> None:
    engine, run_id, carts, gate = scenario
    ProposalService(
        engine,
        carts,
        PostgresRetriever(engine, Embedder()),
        Provider(discount),
        gate,
        ProposalSettings(
            True, Decimal("1"), 100, 100, Rate(Decimal("0.125"), Decimal("0.5"), "F-005")
        ),
    ).generate(
        run_id,
        customer_id=carts.cart.partner_id,
        cart_id=carts.cart.order_id,
        language=language,
        request="Please remind me about my cart.",
        execution_channel=channel,
    )


def approve(engine: sa.Engine, run_id: uuid.UUID) -> None:
    repo = RunRepository(engine)
    pending = next(item for item in repo.pending_approvals() if item["run_id"] == str(run_id))
    repo.decide_approval(
        run_id, token=pending["snapshot_hash"], decision="approve", actor="synthetic-operator"
    )


def service(
    scenario: tuple[sa.Engine, uuid.UUID, Carts, Gate], effects: Effects
) -> ExecutionService:
    engine, _, carts, gate = scenario
    return ExecutionService(engine, carts, gate, effects, effects, ExecutionSettings(True))


@pytest.mark.parametrize(
    ("discount", "channel", "language"),
    [(0, "mailpit", "en"), (12, "telegram", "ar")],
)
def test_poc2_connected_execution(
    scenario: tuple[sa.Engine, uuid.UUID, Carts, Gate],
    discount: int,
    channel: Literal["mailpit", "telegram"],
    language: str,
) -> None:
    engine, run_id, _, _ = scenario
    generate(scenario, discount=discount, channel=channel, language=language)
    repo = RunRepository(engine)
    if discount:
        assert repo.get_run(run_id).state is RunState.AWAITING_APPROVAL
        approve(engine, run_id)
    effects = Effects()
    target = MAILPIT_REVIEW_RECIPIENT if channel == "mailpit" else "synthetic-chat"
    destination = TestDestination(channel, target, confirmed=True)
    execution = service(scenario, effects)
    assert execution.execute(run_id, destination=destination).state is RunState.COMPLETED
    assert len(effects.coupons) == (1 if discount else 0)
    assert len(effects.messages) == 1
    assert effects.messages[0][0] == destination
    assert effects.messages[0][2] == language
    if discount:
        assert effects.coupons[0] in effects.messages[0][1]
    with engine.connect() as conn:
        assert (
            conn.execute(
                sa.text("SELECT status FROM proposal_calls WHERE run_id=:id"), {"id": run_id}
            ).scalar_one()
            == "settled"
        )
        statuses = conn.execute(
            sa.text("SELECT effect,status FROM execution_effects WHERE run_id=:id"),
            {"id": run_id},
        ).all()
    assert sorted(statuses) == (
        [("coupon", "confirmed"), ("message", "confirmed")]
        if discount
        else [("message", "confirmed")]
    )
    with pytest.raises(ExecutionError):
        execution.execute(run_id, destination=destination)
    assert len(effects.messages) == 1


def test_poc2_gate_and_approval_fail_closed(
    scenario: tuple[sa.Engine, uuid.UUID, Carts, Gate],
) -> None:
    engine, run_id, _, _ = scenario
    generate(scenario, discount=12)
    effects = Effects()
    with pytest.raises(ExecutionError):
        service(scenario, effects).execute(
            run_id, destination=TestDestination("mailpit", MAILPIT_REVIEW_RECIPIENT, True)
        )
    assert RunRepository(engine).get_run(run_id).state is RunState.AWAITING_APPROVAL
    assert not effects.coupons and not effects.messages


def test_poc2_unsafe_discount_is_rejected(
    scenario: tuple[sa.Engine, uuid.UUID, Carts, Gate],
) -> None:
    engine, run_id, _, _ = scenario
    generate(scenario, discount=16)
    assert RunRepository(engine).get_run(run_id).state is RunState.REJECTED
    effects = Effects()
    with pytest.raises(ExecutionError):
        service(scenario, effects).execute(
            run_id, destination=TestDestination("mailpit", MAILPIT_REVIEW_RECIPIENT, True)
        )
    assert not effects.coupons and not effects.messages


def test_poc2_changed_cart_is_rejected(
    scenario: tuple[sa.Engine, uuid.UUID, Carts, Gate],
) -> None:
    engine, run_id, carts, _ = scenario
    generate(scenario, discount=12)
    approve(engine, run_id)
    carts.cart = replace(
        carts.cart, lines=(replace(carts.cart.lines[0], unit_price=Decimal("90")),)
    )
    effects = Effects()
    with pytest.raises(ExecutionError, match="cart changed"):
        service(scenario, effects).execute(
            run_id, destination=TestDestination("mailpit", MAILPIT_REVIEW_RECIPIENT, True)
        )
    assert not effects.coupons and not effects.messages


def test_poc2_wrong_or_unconfirmed_destination_has_no_effect(
    scenario: tuple[sa.Engine, uuid.UUID, Carts, Gate],
) -> None:
    generate(scenario, discount=0)
    effects = Effects()
    with pytest.raises(ExecutionError, match="confirmed"):
        TestDestination("mailpit", MAILPIT_REVIEW_RECIPIENT, False)
    with pytest.raises(ExecutionError, match="channel differs"):
        service(scenario, effects).execute(
            scenario[1], destination=TestDestination("telegram", "synthetic-chat", True)
        )
    assert not effects.coupons and not effects.messages


def test_poc2_unknown_dispatch_is_not_repeated(
    scenario: tuple[sa.Engine, uuid.UUID, Carts, Gate],
) -> None:
    engine, run_id, _, _ = scenario
    generate(scenario, discount=0)
    effects = Effects(ambiguous=True)
    execution = service(scenario, effects)
    destination = TestDestination("mailpit", MAILPIT_REVIEW_RECIPIENT, True)
    with pytest.raises(ExecutionHeld):
        execution.execute(run_id, destination=destination)
    with pytest.raises(ExecutionError):
        execution.execute(run_id, destination=destination)
    assert len(effects.messages) == 1
    with engine.connect() as conn:
        assert (
            conn.execute(
                sa.text("SELECT status FROM execution_effects WHERE run_id=:id"), {"id": run_id}
            ).scalar_one()
            == "unknown"
        )


def test_poc2_live_runner_records_gate_result_and_prevents_reuse(
    scenario: tuple[sa.Engine, uuid.UUID, Carts, Gate],
) -> None:
    engine, _, _, gate = scenario
    case = RuntimeCase(
        f"test-{uuid.uuid4()}", "en", None, "cart-open-001", "Remind me.", None, False
    )
    key = f"{EVALUATION_PREFIX}:smoke:{case.case_id}"
    settings = ProposalSettings(
        True,
        Decimal("1"),
        4000,
        8192,
        Rate(Decimal("0.125"), Decimal("0.5"), "test"),
        ReviewBudget(REVIEW_ID, Decimal("1")),
    )
    with engine.connect() as conn:
        existing_review = conn.execute(
            sa.text("SELECT 1 FROM proposal_review_budgets WHERE id=:id"), {"id": REVIEW_ID}
        ).first()
    try:
        validate_unattempted(engine, [case], phase="smoke")
        result = evaluate_case(
            engine,
            load_dataset(),
            case,
            phase="smoke",
            provider=Provider(16),
            retriever=Policies(),
            gate=gate,
            settings=settings,
        )
        assert result["state"] == "rejected"
        assert result["contract_pass"] is True
        assert isinstance(result["gate_result"], dict)
        assert result["gate_result"]["outcome"] == "reject"
        verify_smoke_run(engine, case, result)
        with pytest.raises(ValueError, match="durable run"):
            verify_smoke_run(engine, case, {**result, "state": "completed"})
        with pytest.raises(ValueError, match="already attempted"):
            validate_unattempted(engine, [case], phase="smoke")
    finally:
        with engine.begin() as conn:
            found = conn.execute(
                sa.select(runs.c.id).where(runs.c.idempotency_key == key)
            ).scalar_one_or_none()
            if found is not None:
                for table in ("agent_proposals", "proposal_calls", "proposal_budget", "run_events"):
                    conn.execute(sa.text(f"DELETE FROM {table} WHERE run_id=:run"), {"run": found})
                conn.execute(runs.delete().where(runs.c.id == found))
            if existing_review is None:
                conn.execute(
                    sa.text(
                        "DELETE FROM proposal_review_budgets WHERE id=:id AND NOT EXISTS "
                        "(SELECT 1 FROM proposal_calls WHERE review_id=:id)"
                    ),
                    {"id": REVIEW_ID},
                )


def test_poc2_preflight_is_read_only(
    scenario: tuple[sa.Engine, uuid.UUID, Carts, Gate], tmp_path: Path
) -> None:
    engine, _, _, _ = scenario
    settings = ProposalSettings(
        True,
        Decimal("1"),
        4000,
        8192,
        Rate(Decimal("0.125"), Decimal("0.5"), "test"),
        ReviewBudget(REVIEW_ID, Decimal("1")),
    )
    config = GateConfig.from_env(
        {
            "GATE_TIMEZONE": "Asia/Riyadh",
            "GATE_CURRENCY": "USD",
            "GATE_DAILY_BUDGET": "450",
            "GATE_CATEGORY_MIN_MARGINS": '{"Furniture / Office":"0.2"}',
        }
    )
    case = RuntimeCase(
        f"test-{uuid.uuid4()}", "en", None, "cart-open-001", "Remind me.", None, False
    )
    with engine.connect() as conn:
        before = conn.execute(sa.text("SELECT COUNT(*) FROM proposal_calls")).scalar_one()
    preflight(
        engine,
        settings,
        config,
        load_dataset(),
        (case,),
        phase="smoke",
        parent=tmp_path,
        smoke_digest=None,
        inventory=(case,),
    )
    with engine.connect() as conn:
        after = conn.execute(sa.text("SELECT COUNT(*) FROM proposal_calls")).scalar_one()
    assert before == after


def test_poc2_delivery_preflight_rejects_reused_slot_and_wrong_review(
    scenario: tuple[sa.Engine, uuid.UUID, Carts, Gate],
) -> None:
    engine, run_id, carts, _ = scenario
    settings = ProposalSettings(
        True,
        Decimal("1"),
        4000,
        8192,
        Rate(Decimal("0.125"), Decimal("0.5"), "test"),
        ReviewBudget(REVIEW_ID, Decimal("1")),
    )
    config = GateConfig.from_env(
        {
            "GATE_TIMEZONE": "Asia/Riyadh",
            "GATE_CURRENCY": "USD",
            "GATE_DAILY_BUDGET": "450",
            "GATE_CATEGORY_MIN_MARGINS": '{"Furniture / Office":"0.2"}',
        }
    )
    assert (
        preflight_prepare(
            engine, carts, settings, config, slot="mailpit-en", cart_id=carts.cart.order_id
        )
        == carts.cart
    )
    with engine.begin() as conn:
        conn.execute(
            runs.update().where(runs.c.id == run_id).values(idempotency_key=PREFIX + "mailpit-en")
        )
    with pytest.raises(ValueError, match="already attempted"):
        preflight_prepare(
            engine, carts, settings, config, slot="mailpit-en", cart_id=carts.cart.order_id
        )
    generate(scenario, discount=0)
    with pytest.raises(ValueError, match="successful settled issue 69"):
        verify_delivery_run(engine, run_id)
