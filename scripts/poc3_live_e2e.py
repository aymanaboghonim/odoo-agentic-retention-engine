from __future__ import annotations

import argparse
import json
import os
import socket
import uuid
from datetime import UTC, datetime
from decimal import Decimal
from typing import Literal, cast
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

import sqlalchemy as sa
from sqlalchemy import Engine

from engine.capacity_proposal import EligibleFixtureReader
from engine.channels import MailpitEmailAdapter, TelegramBotClient
from engine.coupon_code import coupon_code_for_run
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
from engine.odoo import JsonTwoClient, OdooCouponCreator, OdooReader, OdooSettings
from engine.policy import Gate, GateConfig
from engine.proposal import ProposalService, ProposalSettings
from engine.proposal_provider import OpenAIClient
from engine.retrieval import (
    MODEL_FILES,
    MODEL_ID,
    MODEL_REPOSITORY,
    MODEL_REVISION,
    LocalE5Embedder,
    PostgresRetriever,
)
from engine.runs import RunState
from engine.synthetic_scan import SyntheticCartScanner
from scripts.gated_execution_review import _show
from scripts.poc2_delivery import SyntheticReader

Channel = Literal["mailpit", "telegram"]
LIVE_E2E_REVIEW_ID = "poc3-live-e2e-demo"
LIVE_E2E_BUDGET_LIMIT_USD = Decimal("0.50")
DEFAULT_REQUEST = "Please write a short, friendly reminder for this synthetic test cart."


def validate_live_e2e_proposal_settings(settings: ProposalSettings) -> None:
    review = settings.review_budget
    if (
        review is None
        or review.review_id != LIVE_E2E_REVIEW_ID
        or review.limit_usd > LIVE_E2E_BUDGET_LIMIT_USD
        or settings.budget_usd > review.limit_usd
    ):
        raise ValueError(
            f"live E2E requires PROPOSAL_REVIEW_ID={LIVE_E2E_REVIEW_ID} "
            f"and budget <= USD {LIVE_E2E_BUDGET_LIMIT_USD}"
        )
    if settings.input_tokens > 4000 or settings.output_tokens > 8192:
        raise ValueError("request token limits exceed approved POC ceilings (4000 / 8192)")


def validate_live_e2e_gate(gate: GateConfig) -> None:
    if (
        gate.currency != "USD"
        or gate.daily_budget != Decimal("450")
        or gate.category_min_margin.get("Furniture / Office") != Decimal("0.20")
        or str(gate.timezone) != "Asia/Riyadh"
    ):
        raise ValueError("approved synthetic financial gate configuration is required")


def validate_scanned_run(
    engine: Engine,
    reader: OdooReader,
    *,
    run_id: uuid.UUID,
    instance_id: str,
) -> tuple[int, int]:
    canonical_instance = str(uuid.UUID(instance_id))
    run = RunRepository(engine).get_run(run_id)
    prefix = f"synthetic-scan:{canonical_instance}:"
    parts = run.trigger_ref.split(":")
    if (
        run.state is not RunState.RECEIVED
        or run.trigger_type != "synthetic"
        or len(parts) != 2
        or not all(part.isascii() and part.isdecimal() and int(part) > 0 for part in parts)
        or run.idempotency_key != prefix + parts[1]
    ):
        raise ValueError("propose requires an unprocessed received run from worker scan")
    partner_id, order_id = map(int, parts)
    cart = reader.get_synthetic_review_cart(order_id, now=datetime.now(UTC))
    if cart is None or cart.partner_id != partner_id or cart.order_id != order_id:
        raise ValueError("scanned synthetic cart is no longer eligible")
    return partner_id, order_id


def check_review_budget_headroom(engine: Engine, settings: ProposalSettings) -> Decimal:
    with engine.connect() as conn:
        reserved = conn.execute(
            sa.text(
                "SELECT COALESCE(SUM(GREATEST(reserved_usd, "
                "COALESCE(actual_usd, reserved_usd))), 0) "
                "FROM proposal_calls WHERE review_id=:review"
            ),
            {"review": LIVE_E2E_REVIEW_ID},
        ).scalar_one()
    reserved_dec = Decimal(str(reserved))
    next_est = settings.rate.estimate(settings.input_tokens, settings.output_tokens)
    limit = (
        settings.review_budget.limit_usd if settings.review_budget else LIVE_E2E_BUDGET_LIMIT_USD
    )
    if reserved_dec + next_est > limit:
        raise ValueError(
            f"review budget exhausted: reserved={reserved_dec} next={next_est} limit={limit}"
        )
    return reserved_dec


def approve_via_api(
    *,
    api_url: str,
    token: str,
    run_id: uuid.UUID,
    snapshot_hash: str,
) -> dict[str, object]:
    list_req = Request(
        f"{api_url.rstrip('/')}/approvals",
        headers={"Authorization": f"Bearer {token}"},
        method="GET",
    )
    try:
        with urlopen(list_req, timeout=10) as resp:
            pending = json.loads(resp.read().decode("utf-8"))
    except (HTTPError, URLError, OSError) as exc:
        raise RuntimeError(f"approval API GET /approvals failed: {exc}") from exc
    matches = [item for item in pending if item.get("run_id") == str(run_id)]
    if len(matches) != 1:
        raise ValueError(f"run {run_id} is not pending in approval API")
    if matches[0].get("snapshot_hash") != snapshot_hash:
        raise ValueError("approval API snapshot_hash does not match requested token")

    body = json.dumps({"snapshot_hash": snapshot_hash, "decision": "approve"}).encode("utf-8")
    post_req = Request(
        f"{api_url.rstrip('/')}/approvals/{run_id}/decision",
        data=body,
        headers={
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
        },
        method="POST",
    )
    try:
        with urlopen(post_req, timeout=10) as resp:
            result = json.loads(resp.read().decode("utf-8"))
    except (HTTPError, URLError, OSError) as exc:
        raise RuntimeError(f"approval API POST decision failed: {exc}") from exc
    if not isinstance(result, dict):
        raise ValueError("approval API returned invalid payload")
    return cast(dict[str, object], result)


def print_run_trace(engine: Engine, run_id: uuid.UUID) -> None:
    repo = RunRepository(engine)
    run = repo.get_run(run_id)
    print(f"run_id={run.id}")
    print(f"trigger_type={run.trigger_type} trigger_ref={run.trigger_ref}")
    print(f"idempotency_key={run.idempotency_key}")
    print(f"state={run.state.value}")
    with engine.connect() as conn:
        proposal = (
            conn.execute(
                sa.text(
                    "SELECT schema_version, fingerprint, proposal, gate_result "
                    "FROM agent_proposals WHERE run_id=:run"
                ),
                {"run": run_id},
            )
            .mappings()
            .first()
        )
        approval = (
            conn.execute(
                sa.text(
                    "SELECT snapshot_hash, decision, actor, decided_at "
                    "FROM approval_requests WHERE run_id=:run"
                ),
                {"run": run_id},
            )
            .mappings()
            .first()
        )
        calls = (
            conn.execute(
                sa.text(
                    "SELECT review_id, status, reserved_usd, actual_usd, "
                    "input_tokens, output_tokens "
                    "FROM proposal_calls WHERE run_id=:run"
                ),
                {"run": run_id},
            )
            .mappings()
            .all()
        )
        effects = (
            conn.execute(
                sa.text(
                    "SELECT effect, status, attempts, reference, updated_at "
                    "FROM execution_effects WHERE run_id=:run ORDER BY updated_at, effect"
                ),
                {"run": run_id},
            )
            .mappings()
            .all()
        )
        events = (
            conn.execute(
                sa.text(
                    "SELECT from_state, to_state, actor, at FROM run_events "
                    "WHERE run_id=:run ORDER BY at, id"
                ),
                {"run": run_id},
            )
            .mappings()
            .all()
        )
    if proposal is not None:
        prop_data = cast(dict[str, object], proposal["proposal"])
        gate_data = cast(dict[str, object], proposal["gate_result"])
        print(
            f"proposal_action={prop_data.get('action')} "
            f"discount_pct={prop_data.get('proposed_discount_percent')} "
            f"language={prop_data.get('language')} "
            f"channel={prop_data.get('execution_channel')}"
        )
        print(
            f"gate_outcome={gate_data.get('outcome')} "
            f"gate_reasons={gate_data.get('reasons')} "
            f"fingerprint={proposal['fingerprint']}"
        )
        print(f"customer_message={prop_data.get('customer_message')}")
    if approval is not None:
        print(
            f"approval_snapshot_hash={approval['snapshot_hash']} "
            f"decision={approval['decision']} actor={approval['actor']}"
        )
    for call in calls:
        print(
            f"proposal_call review_id={call['review_id']} status={call['status']} "
            f"reserved_usd={call['reserved_usd']} actual_usd={call['actual_usd']} "
            f"tokens={call['input_tokens']}/{call['output_tokens']}"
        )
    for entry in effects:
        print(
            f"effect={entry['effect']} status={entry['status']} "
            f"attempts={entry['attempts']} reference={entry['reference']}"
        )
    for ev in events:
        print(f"event {ev['from_state']} -> {ev['to_state']} actor={ev['actor']}")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="POC-3 live E2E walkthrough on poc68-isolated")
    parser.add_argument(
        "command",
        choices=("preflight", "scan", "propose", "show", "approve-api", "execute", "inspect"),
    )
    parser.add_argument("--channel", choices=("mailpit", "telegram"))
    parser.add_argument("--language", choices=("en", "ar"), default="en")
    parser.add_argument("--run-id", type=uuid.UUID)
    parser.add_argument("--request", default=DEFAULT_REQUEST)
    parser.add_argument("--snapshot-hash")
    parser.add_argument("--api-url", default="http://127.0.0.1:8080")
    parser.add_argument("--confirm-destination")
    args = parser.parse_args(argv)

    if args.command == "approve-api":
        if args.run_id is None or not args.snapshot_hash:
            raise ValueError("--run-id and --snapshot-hash are required for approve-api")
        token = os.environ.get("APPROVAL_ACCESS_TOKEN", "")
        if not token:
            raise ValueError("APPROVAL_ACCESS_TOKEN must be set for approve-api")
        api_result = approve_via_api(
            api_url=args.api_url,
            token=token,
            run_id=args.run_id,
            snapshot_hash=args.snapshot_hash,
        )
        print(f"approval_api_result={json.dumps(api_result)}")
        return 0

    worker = WorkerSettings.from_env(os.environ)
    reader = OdooReader(worker.odoo_url)
    reader.verify_synthetic_readonly_role()
    engine = create_engine(worker.engine_url)
    try:
        if args.command == "scan":
            scanner = SyntheticCartScanner(
                engine, reader, instance_id=worker.instance_id, page_size=worker.page_size
            )
            scanned, created = scanner.scan_page(now=datetime.now(UTC))
            print(f"stage=scan status=completed scanned={scanned} created={created}")
            with engine.connect() as conn:
                rows = (
                    conn.execute(
                        sa.text(
                            "SELECT id, trigger_ref, state, created_at FROM runs "
                            "WHERE state='received' ORDER BY created_at DESC LIMIT 5"
                        )
                    )
                    .mappings()
                    .all()
                )
            for row in rows:
                print(
                    f"received_run run_id={row['id']} trigger_ref={row['trigger_ref']} "
                    f"created_at={row['created_at']}"
                )
            return 0

        if args.command == "preflight":
            if args.channel is None:
                raise ValueError("--channel is required for preflight")
            settings = ProposalSettings.from_env(os.environ)
            validate_live_e2e_proposal_settings(settings)
            gate_config = GateConfig.from_env(os.environ)
            validate_live_e2e_gate(gate_config)
            OpenAIClient(os.environ.get("OPENAI_API_KEY", ""))
            with engine.connect() as conn:
                languages = set(
                    conn.execute(
                        sa.text(
                            "SELECT DISTINCT language FROM policy_passages WHERE model_id=:model"
                        ),
                        {"model": MODEL_ID},
                    ).scalars()
                )
            if not {"en", "ar"}.issubset(languages):
                raise ValueError("policy_passages must contain English and Arabic")
            reserved = check_review_budget_headroom(engine, settings)
            from huggingface_hub import snapshot_download  # type: ignore[import-not-found]

            snapshot_download(
                repo_id=MODEL_REPOSITORY,
                revision=MODEL_REVISION,
                token=False,
                local_files_only=True,
                allow_patterns=MODEL_FILES,
            )
            channel = cast(Channel, args.channel)
            destination = TestDestination.from_env(
                os.environ, channel, confirm_dedicated_test_destination=True
            )
            if channel == "mailpit":
                MailpitEmailAdapter()
                with socket.create_connection(("127.0.0.1", 1025), timeout=2):
                    pass
            else:
                TelegramBotClient(os.environ.get("TELEGRAM_BOT_TOKEN", ""))
            if args.run_id is not None:
                validate_scanned_run(
                    engine, reader, run_id=args.run_id, instance_id=worker.instance_id
                )
            print(
                f"stage=live-e2e-preflight status=passed channel={channel} "
                f"destination_sha256={destination.destination_hash} "
                f"review_reserved_usd={reserved}"
            )
            return 0

        if args.run_id is None:
            raise ValueError("--run-id is required")

        if args.command == "inspect":
            print_run_trace(engine, args.run_id)
            return 0

        if args.command == "show":
            if args.channel is None:
                raise ValueError("--channel is required for show")
            channel = cast(Channel, args.channel)
            _show(engine, args.run_id)
            destination = TestDestination.from_env(
                os.environ, channel, confirm_dedicated_test_destination=True
            )
            print(f"destination_channel={channel}")
            print(f"destination={destination.target}")
            print(f"destination_sha256={destination.destination_hash}")
            return 0

        if args.command == "propose":
            if args.channel is None:
                raise ValueError("--channel is required for propose")
            channel = cast(Channel, args.channel)
            settings = ProposalSettings.from_env(os.environ)
            validate_live_e2e_proposal_settings(settings)
            gate_config = GateConfig.from_env(os.environ)
            validate_live_e2e_gate(gate_config)
            provider = OpenAIClient(os.environ.get("OPENAI_API_KEY", ""))
            partner_id, order_id = validate_scanned_run(
                engine, reader, run_id=args.run_id, instance_id=worker.instance_id
            )
            check_review_budget_headroom(engine, settings)
            with progress("live-e2e-model-load"):
                retriever = PostgresRetriever(engine, LocalE5Embedder())
            service = ProposalService(
                engine,
                EligibleFixtureReader(reader),
                retriever,
                provider,
                Gate(gate_config, PostgresGateLedger(engine)),
                settings,
            )
            with progress("live-e2e-proposal"):
                service.generate(
                    args.run_id,
                    customer_id=partner_id,
                    cart_id=order_id,
                    language=cast(Literal["en", "ar"], args.language),
                    request=args.request,
                    execution_channel=channel,
                )
            print_run_trace(engine, args.run_id)
            return 0

        if args.command == "execute":
            if args.channel is None:
                raise ValueError("--channel is required for execute")
            channel = cast(Channel, args.channel)
            destination = TestDestination.from_env(
                os.environ, channel, confirm_dedicated_test_destination=True
            )
            if args.confirm_destination != destination.destination_hash:
                raise ValueError("--confirm-destination must match exact destination_sha256")
            odoo = OdooSettings.from_env(os.environ)
            if odoo.url != "http://odoo:8069" or odoo.database != "odoo":
                raise ValueError("execution is restricted to the isolated Compose Odoo")
            gate_config = GateConfig.from_env(os.environ)
            validate_live_e2e_gate(gate_config)
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
            with progress(f"live-e2e-execute-{channel}"):
                exec_result = execution.execute(args.run_id, destination=destination)
            print(f"execution_status={exec_result.status} state={exec_result.state.value}")
            print(f"expected_coupon_code={coupon_code_for_run(str(args.run_id))}")
            print_run_trace(engine, args.run_id)
            return 0
    finally:
        engine.dispose()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
