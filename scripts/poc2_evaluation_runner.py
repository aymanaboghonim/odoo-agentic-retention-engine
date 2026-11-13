from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import sqlalchemy as sa
from sqlalchemy import Engine

from engine.db import RunRepository, agent_proposals
from engine.odoo import AbandonedCart, CartLine
from engine.policy import Gate
from engine.proposal import ProposalError, ProposalService, ProposalSettings
from engine.proposal_provider import LLMClient, ProviderResult
from engine.retrieval import Retriever
from scripts.poc2_evaluation_cases import RuntimeCase
from scripts.s2_llm_eval import GoldenDataset

REVIEW_ID = "poc2-69-evaluation"
EVALUATION_PREFIX = "poc2-69-evaluation-v1"


@dataclass(frozen=True)
class FixtureCart:
    cart: AbandonedCart

    def get_cart(self, order_id: int) -> AbandonedCart | None:
        return self.cart if order_id == self.cart.order_id else None


class CapturingProvider:
    def __init__(self, provider: LLMClient) -> None:
        self._provider = provider
        self.result: ProviderResult | None = None

    def count_tokens(self, payload: Mapping[str, object]) -> int:
        return self._provider.count_tokens(payload)

    def generate(self, payload: Mapping[str, object], output_limit: int) -> ProviderResult:
        self.result = self._provider.generate(payload, output_limit)
        return self.result


def fixture_cart(dataset: GoldenDataset, case: RuntimeCase) -> AbandonedCart:
    fixture = dataset.fixtures[case.fixture_id]
    items = fixture.get("items")
    if not isinstance(items, list) or len(items) != 1 or not isinstance(items[0], dict):
        raise ValueError(f"{case.case_id} needs one synthetic fixture item")
    item = items[0]
    name = item.get("name")
    if not isinstance(name, str) or not name or fixture.get("currency") != "USD":
        raise ValueError(f"{case.case_id} has invalid fixture name or currency")
    quantity = Decimal(str(item.get("quantity")))
    price = Decimal(str(item.get("unit_price")))
    subtotal = Decimal(str(fixture.get("subtotal")))
    if not quantity.is_finite() or not price.is_finite() or quantity <= 0 or price <= 0:
        raise ValueError(f"{case.case_id} has invalid fixture values")
    if quantity * price != subtotal:
        raise ValueError(f"{case.case_id} has inconsistent fixture subtotal")
    return AbandonedCart(
        order_id=1,
        reference="synthetic-evaluation",
        partner_id=1,
        partner_name="Evaluation Shopper",
        partner_email="evaluation@example.test",
        language=case.language,
        currency="USD",
        amount_total=subtotal,
        date_order=datetime.now(UTC) - timedelta(days=2),
        lines=(
            CartLine(
                1,
                name,
                "synthetic-product",
                "Furniture / Office",
                quantity,
                price,
                price / 2,
            ),
        ),
    )


def validate_settings(settings: ProposalSettings) -> None:
    review = settings.review_budget
    if (
        review is None
        or review.review_id != REVIEW_ID
        or review.limit_usd != Decimal("1")
        or settings.budget_usd > review.limit_usd
        or settings.input_tokens != 4000
        or settings.output_tokens != 8192
    ):
        raise ValueError("issue 69 requires its fixed aggregate budget and approved token limits")
    if settings.rate.estimate(settings.input_tokens, settings.output_tokens) > settings.budget_usd:
        raise ValueError("per-run budget cannot reserve the approved token limits")


def validate_unattempted(engine: Engine, cases: Sequence[RuntimeCase], *, phase: str) -> None:
    if phase not in {"smoke", "evaluation"}:
        raise ValueError("invalid evaluation phase")
    keys = [f"{EVALUATION_PREFIX}:{phase}:{case.case_id}" for case in cases]
    with engine.connect() as conn:
        existing = (
            conn.execute(
                sa.text("SELECT idempotency_key FROM runs WHERE idempotency_key = ANY(:keys)"),
                {"keys": keys},
            )
            .scalars()
            .all()
        )
        unknown = conn.execute(
            sa.text(
                "SELECT COUNT(*) FROM proposal_calls "
                "WHERE review_id=:review AND status IN ('reserved', 'unknown')"
            ),
            {"review": REVIEW_ID},
        ).scalar_one()
    if existing:
        raise ValueError("evaluation case already attempted; inspect the durable run")
    if unknown:
        raise ValueError("review has unresolved provider reservations; reconcile before continuing")


def evaluate_case(
    engine: Engine,
    dataset: GoldenDataset,
    case: RuntimeCase,
    *,
    phase: str,
    provider: LLMClient,
    retriever: Retriever,
    gate: Gate,
    settings: ProposalSettings,
) -> dict[str, object]:
    validate_settings(settings)
    if phase not in {"smoke", "evaluation"}:
        raise ValueError("invalid evaluation phase")
    validate_unattempted(engine, [case], phase=phase)
    cart = fixture_cart(dataset, case)
    run, created = RunRepository(engine).create_run(
        trigger_type="synthetic",
        trigger_ref="1:1",
        idempotency_key=f"{EVALUATION_PREFIX}:{phase}:{case.case_id}",
        actor="poc2-evaluation",
    )
    if not created:
        raise ValueError(f"case {case.case_id} was already attempted")
    capture = CapturingProvider(provider)
    service = ProposalService(engine, FixtureCart(cart), retriever, capture, gate, settings)
    error: str | None = None
    try:
        service.generate(
            run.id,
            customer_id=1,
            cart_id=1,
            language=case.language,
            request=case.request,
        )
    except ProposalError as exc:
        error = str(exc)
        with engine.connect() as conn:
            call_status = conn.execute(
                sa.text("SELECT status, failure_reason FROM proposal_calls WHERE run_id=:run"),
                {"run": run.id},
            ).first()
        if (
            call_status is None
            or call_status.status != "settled"
            or call_status.failure_reason is not None
        ):
            raise
    current = RunRepository(engine).get_run(run.id)
    with engine.connect() as conn:
        row = conn.execute(
            sa.select(agent_proposals.c.proposal, agent_proposals.c.gate_result).where(
                agent_proposals.c.run_id == run.id
            )
        ).first()
        call = (
            conn.execute(
                sa.text(
                    "SELECT status, input_tokens, output_tokens, reserved_usd, actual_usd "
                    "FROM proposal_calls WHERE run_id=:run"
                ),
                {"run": run.id},
            )
            .mappings()
            .one()
        )
    if capture.result is None or call["status"] != "settled":
        raise RuntimeError(f"case {case.case_id} has no settled provider result")
    try:
        response = json.loads(capture.result.text, parse_float=Decimal, parse_int=Decimal)
    except json.JSONDecodeError:
        response = None
    proposal = row.proposal if row is not None else None
    gate_result = row.gate_result if row is not None else None
    action = response.get("action") if isinstance(response, dict) else None
    price = response.get("price_claim") if isinstance(response, dict) else None
    currency = response.get("currency") if isinstance(response, dict) else None
    discount = response.get("proposed_discount_percent") if isinstance(response, dict) else None
    action_pass = error is None and (case.expected_action is None or action == case.expected_action)
    price_pass = error is None and (
        not case.check_price or (price == cart.amount_total and currency == cart.currency)
    )
    return {
        "case_id": case.case_id,
        "run_id": str(run.id),
        "language": case.language,
        "dialect": case.dialect,
        "state": current.state.value,
        "action": action,
        "expected_action": case.expected_action,
        "action_pass": action_pass,
        "price_checked": case.check_price,
        "price_pass": price_pass,
        "price_claim": str(price) if isinstance(price, Decimal) else None,
        "price_currency": currency if isinstance(currency, str) else None,
        "proposed_discount_percent": str(discount) if isinstance(discount, Decimal) else None,
        "contract_pass": error is None,
        "error": error,
        "proposal": proposal,
        "gate_result": gate_result,
        "usage": {
            "input_tokens": call["input_tokens"],
            "output_tokens": call["output_tokens"],
            "reserved_usd": str(call["reserved_usd"]),
            "actual_usd": str(call["actual_usd"]),
        },
    }
