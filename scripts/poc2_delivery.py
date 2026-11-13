from __future__ import annotations

import argparse
import os
import socket
import uuid
from datetime import UTC, datetime
from decimal import Decimal
from typing import Literal

import sqlalchemy as sa
from sqlalchemy import Engine

from engine.capacity_proposal import EligibleFixtureReader, FixtureReader
from engine.channels import MailpitEmailAdapter, TelegramBotClient
from engine.db import RunRepository, create_engine
from engine.execution import (
    ExecutionService,
    ExecutionSettings,
    TestChannelDispatcher,
    TestDestination,
)
from engine.full_stack_proposal import progress
from engine.full_stack_worker import WorkerSettings
from engine.gate_ledger import PostgresGateLedger
from engine.odoo import AbandonedCart, JsonTwoClient, OdooCouponCreator, OdooReader, OdooSettings
from engine.policy import Gate, GateConfig
from engine.proposal import ProposalService, ProposalSettings
from engine.proposal_provider import OpenAIClient
from engine.retrieval import MODEL_ID, LocalE5Embedder, PostgresRetriever
from scripts.gated_execution_review import REVIEW_REQUEST, _show
from scripts.poc2_evaluation_live import budget_totals
from scripts.poc2_evaluation_runner import REVIEW_ID, validate_settings, validate_unattempted

Channel = Literal["mailpit", "telegram"]
SLOTS: dict[str, tuple[Channel, Literal["en", "ar"]]] = {
    "mailpit-en": ("mailpit", "en"),
    "mailpit-ar": ("mailpit", "ar"),
    "telegram-en": ("telegram", "en"),
    "telegram-ar": ("telegram", "ar"),
}
PREFIX = "poc2-69-delivery:"


class SyntheticReader:
    def __init__(self, reader: OdooReader) -> None:
        self._reader = reader

    def get_abandoned_cart(self, order_id: int, *, now: datetime) -> AbandonedCart | None:
        return self._reader.get_synthetic_review_cart(order_id, now=now)


def verify_delivery_run(engine: Engine, run_id: uuid.UUID) -> str:
    run = RunRepository(engine).get_run(run_id)
    if run.trigger_type != "synthetic" or not run.idempotency_key.startswith(PREFIX):
        raise ValueError("run is not an issue 69 synthetic delivery")
    slot = run.idempotency_key.removeprefix(PREFIX)
    if slot not in SLOTS:
        raise ValueError("delivery slot is not approved")
    with engine.connect() as conn:
        call = conn.execute(
            sa.text(
                "SELECT status, failure_reason FROM proposal_calls "
                "WHERE run_id=:run AND review_id=:review"
            ),
            {"run": run_id, "review": REVIEW_ID},
        ).first()
    if call is None or call.status != "settled" or call.failure_reason is not None:
        raise ValueError("delivery proposal has no successful settled issue 69 provider call")
    return slot


def validate_gate(gate: GateConfig) -> None:
    if (
        gate.currency != "USD"
        or gate.daily_budget != Decimal("450")
        or gate.category_min_margin.get("Furniture / Office") != Decimal("0.20")
        or str(gate.timezone) != "Asia/Riyadh"
    ):
        raise ValueError("the approved synthetic financial gate configuration is required")


def preflight_prepare(
    engine: Engine,
    reader: FixtureReader,
    settings: ProposalSettings,
    gate: GateConfig,
    *,
    slot: str,
    cart_id: int,
) -> AbandonedCart:
    validate_settings(settings)
    validate_gate(gate)
    if slot not in SLOTS:
        raise ValueError("delivery slot is not approved")
    validate_unattempted(engine, [], phase="evaluation")
    with engine.connect() as conn:
        attempted = conn.execute(
            sa.text("SELECT 1 FROM runs WHERE idempotency_key=:key"),
            {"key": PREFIX + slot},
        ).first()
        languages = set(
            conn.execute(
                sa.text("SELECT DISTINCT language FROM policy_passages WHERE model_id=:model"),
                {"model": MODEL_ID},
            ).scalars()
        )
    if attempted:
        raise ValueError("delivery slot already attempted; inspect the durable run")
    if not {"en", "ar"}.issubset(languages):
        raise ValueError("the pinned policy index must contain English and Arabic")
    reserved, _ = budget_totals(engine)
    if reserved + settings.rate.estimate(settings.input_tokens, settings.output_tokens) > Decimal(
        "1"
    ):
        raise ValueError("the aggregate review budget cannot reserve this delivery call")
    cart = reader.get_synthetic_review_cart(cart_id, now=datetime.now(UTC))
    if cart is None:
        raise ValueError("delivery requires a still-eligible marked synthetic cart")
    return cart


def main() -> None:
    parser = argparse.ArgumentParser(description="Review one issue 69 synthetic delivery")
    parser.add_argument("command", choices=("preflight", "prepare", "show", "approve", "execute"))
    parser.add_argument("--slot", choices=tuple(SLOTS))
    parser.add_argument("--cart-id", type=int)
    parser.add_argument("--run-id", type=uuid.UUID)
    parser.add_argument("--confirm-run", type=uuid.UUID)
    parser.add_argument("--confirm-destination")
    args = parser.parse_args()

    worker = WorkerSettings.from_env(os.environ)
    reader = OdooReader(worker.odoo_url)
    reader.verify_synthetic_readonly_role()
    engine = create_engine(worker.engine_url)
    try:
        if args.command in {"preflight", "prepare"}:
            if args.slot is None or args.cart_id is None:
                raise ValueError("slot and cart ID are required")
            settings = ProposalSettings.from_env(os.environ)
            gate_config = GateConfig.from_env(os.environ)
            provider = OpenAIClient(os.environ.get("OPENAI_API_KEY", ""))
            cart = preflight_prepare(
                engine, reader, settings, gate_config, slot=args.slot, cart_id=args.cart_id
            )
            from huggingface_hub import snapshot_download  # type: ignore[import-not-found]

            from engine.retrieval import MODEL_FILES, MODEL_REPOSITORY, MODEL_REVISION

            snapshot_download(
                repo_id=MODEL_REPOSITORY,
                revision=MODEL_REVISION,
                token=False,
                local_files_only=True,
                allow_patterns=MODEL_FILES,
            )
            if args.command == "preflight":
                channel, _language = SLOTS[args.slot]
                TestDestination.from_env(
                    os.environ, channel, confirm_dedicated_test_destination=True
                )
                if channel == "mailpit":
                    MailpitEmailAdapter()
                    with socket.create_connection(("127.0.0.1", 1025), timeout=2):
                        pass
                else:
                    TelegramBotClient(os.environ.get("TELEGRAM_BOT_TOKEN", ""))
                print(
                    "stage=delivery-preflight status=passed; no model call or dispatch",
                    flush=True,
                )
                return
            print("stage=delivery-preflight status=passed; no model call or dispatch", flush=True)
            with progress("delivery-model-load"):
                retriever = PostgresRetriever(engine, LocalE5Embedder())
            repo = RunRepository(engine)
            run, created = repo.create_run(
                trigger_type="synthetic",
                trigger_ref=f"{cart.partner_id}:{cart.order_id}",
                idempotency_key=PREFIX + args.slot,
                actor="poc2-delivery-review",
            )
            if not created:
                raise ValueError("delivery slot already attempted; inspect the durable run")
            channel, language = SLOTS[args.slot]
            service = ProposalService(
                engine,
                EligibleFixtureReader(reader),
                retriever,
                provider,
                Gate(gate_config, PostgresGateLedger(engine)),
                settings,
            )
            print(f"run_id={run.id}", flush=True)
            with progress("delivery-proposal"):
                service.generate(
                    run.id,
                    customer_id=cart.partner_id,
                    cart_id=cart.order_id,
                    language=language,
                    request=REVIEW_REQUEST,
                    execution_channel=channel,
                )
            _show(engine, run.id)
            return

        if args.run_id is None:
            raise ValueError("run ID is required")
        slot = verify_delivery_run(engine, args.run_id)
        if args.slot != slot:
            raise ValueError("delivery slot does not match the durable run")
        channel, _language = SLOTS[slot]
        if args.command == "show":
            _show(engine, args.run_id)
            destination = TestDestination.from_env(
                os.environ, channel, confirm_dedicated_test_destination=True
            )
            print(f"destination_channel={channel}")
            print(f"destination={destination.target}")
            print(f"destination_sha256={destination.destination_hash}")
            return
        if args.confirm_run != args.run_id:
            raise ValueError("explicit confirmation of this run ID is required")
        repo = RunRepository(engine)
        if args.command == "approve":
            pending = [
                item for item in repo.pending_approvals() if item["run_id"] == str(args.run_id)
            ]
            if len(pending) != 1:
                raise ValueError("run has no pending approval")
            approval_result = repo.decide_approval(
                args.run_id,
                token=str(pending[0]["snapshot_hash"]),
                decision="approve",
                actor="synthetic-review-operator",
            )
            print(f"approval_state={approval_result.state.value}")
            return
        destination = TestDestination.from_env(
            os.environ, channel, confirm_dedicated_test_destination=True
        )
        if args.confirm_destination != destination.destination_hash:
            raise ValueError(
                "explicit confirmation of this exact dedicated destination is required"
            )
        odoo = OdooSettings.from_env(os.environ)
        if odoo.url != "http://odoo:8069" or odoo.database != "odoo":
            raise ValueError("execution is restricted to the isolated Compose Odoo")
        gate_config = GateConfig.from_env(os.environ)
        validate_gate(gate_config)
        telegram = (
            TelegramBotClient(os.environ.get("TELEGRAM_BOT_TOKEN", ""))
            if channel == "telegram"
            else None
        )
        mailpit = MailpitEmailAdapter() if channel == "mailpit" else None
        if channel == "mailpit":
            with socket.create_connection(("127.0.0.1", 1025), timeout=2):
                pass
        execution = ExecutionService(
            engine,
            SyntheticReader(reader),
            Gate(gate_config, PostgresGateLedger(engine)),
            OdooCouponCreator(JsonTwoClient(odoo), odoo),
            TestChannelDispatcher(telegram, mailpit),
            ExecutionSettings.from_env(os.environ),
        )
        with progress(f"delivery-{channel}"):
            result = execution.execute(args.run_id, destination=destination)
        print(f"execution_status={result.status} state={result.state.value}")
    finally:
        engine.dispose()


if __name__ == "__main__":
    main()
