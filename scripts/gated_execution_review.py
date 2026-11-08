from __future__ import annotations

import argparse
import ipaddress
import json
import os
import socket
import sys
import threading
import time
import uuid
from collections.abc import Mapping
from contextlib import contextmanager
from dataclasses import asdict
from datetime import UTC, datetime, timedelta
from decimal import Decimal, InvalidOperation
from typing import Iterator, Literal, cast
from urllib.parse import urlsplit

import sqlalchemy as sa
from sqlalchemy.engine import make_url

from engine.channels import MailpitEmailAdapter, TelegramBotClient
from engine.config import EngineConfig
from engine.db import RunRepository, agent_proposals, create_engine, runs
from engine.execution import (
    ExecutionService,
    ExecutionSettings,
    TestChannelDispatcher,
    TestDestination,
)
from engine.gate_ledger import PostgresGateLedger
from engine.odoo import (
    AbandonedCart,
    JsonTwoClient,
    OdooCouponCreator,
    OdooReader,
    OdooSettings,
    cart_snapshot,
    cart_to_proposal_inputs,
)
from engine.policy import Gate, GateConfig, OfferProposal, Outcome, as_dict
from engine.proposal import ProposalService, ProposalSettings
from engine.proposal_provider import OpenAIClient
from engine.retrieval import MODEL_ID, LocalE5Embedder, PostgresRetriever
from engine.runs import RunState

Channel = Literal["telegram", "mailpit"]
LIVE_REVIEW_ID = "poc2-67-live-review"
LIVE_REVIEW_BUDGET_USD = Decimal("0.50")
REVIEW_REQUEST = "Please write a short, friendly reminder for this synthetic test cart."
FIXED_MESSAGES = {
    "en": "Your synthetic cart is waiting. This is a fixed review message.",
    "ar": "سلتك التجريبية بانتظارك. هذه رسالة مراجعة ثابتة.",
}


@contextmanager
def progress(stage: str) -> Iterator[None]:
    started = time.monotonic()
    stopped = threading.Event()

    def heartbeat() -> None:
        while not stopped.wait(30):
            elapsed = int(time.monotonic() - started)
            print(f"stage={stage} status=running elapsed={elapsed}s", flush=True)

    print(f"stage={stage} status=started", flush=True)
    thread = threading.Thread(target=heartbeat, daemon=True)
    thread.start()
    try:
        yield
    finally:
        stopped.set()
        thread.join()
        elapsed = time.monotonic() - started
        print(f"stage={stage} status=finished elapsed={elapsed:.1f}s", flush=True)


def _channel(value: str) -> Channel:
    if value not in {"telegram", "mailpit"}:
        raise argparse.ArgumentTypeError("channel must be telegram or mailpit")
    return cast(Channel, value)


def _engine_url() -> str:
    config = EngineConfig.from_env(os.environ)
    _require_local_host(make_url(config.database_url).host, "ENGINE_DATABASE_URL")
    return config.database_url


def _require_local_host(host: str | None, label: str) -> None:
    if host == "localhost":
        return
    try:
        local = host is not None and ipaddress.ip_address(host).is_loopback
    except ValueError:
        local = False
    if not local:
        raise ValueError(f"{label} must point to a loopback host")


def _gate(engine: sa.Engine) -> Gate:
    return Gate(GateConfig.from_env(os.environ), PostgresGateLedger(engine))


def _odoo() -> tuple[OdooSettings, JsonTwoClient, OdooReader]:
    settings = OdooSettings.from_env(os.environ)
    api_url = urlsplit(settings.url)
    if api_url.port != 8070:
        raise ValueError("ODOO_URL must target the documented disposable S2 instance on port 8070")
    _require_local_host(api_url.hostname, "ODOO_URL")
    readonly_url = make_url(settings.readonly_database_url)
    host = readonly_url.host
    try:
        address = ipaddress.ip_address(host or "")
    except ValueError as error:
        raise ValueError(
            "ODOO_READONLY_DATABASE_URL must use the disposable DB container IP"
        ) from error
    if not (address.is_loopback or address.is_private):
        raise ValueError("ODOO_READONLY_DATABASE_URL must use a local DB address")
    if readonly_url.database != settings.database or readonly_url.port != 5432:
        raise ValueError("read-only DB URL must target the selected Odoo database on port 5432")
    return settings, JsonTwoClient(settings), OdooReader(settings.readonly_database_url)


def _report_review_usage(engine: sa.Engine) -> None:
    with engine.connect() as conn:
        used, calls = conn.execute(
            sa.text(
                "SELECT COALESCE(SUM(GREATEST(reserved_usd, "
                "COALESCE(actual_usd, reserved_usd))), 0), count(*) "
                "FROM proposal_calls WHERE review_id=:review_id"
            ),
            {"review_id": LIVE_REVIEW_ID},
        ).one()
    print(f"review_budget_estimate_usd={used} ceiling_usd={LIVE_REVIEW_BUDGET_USD}")
    print(f"review_budget_calls={calls}")


def _as_records(value: object, label: str) -> list[dict[str, object]]:
    if not isinstance(value, list) or not all(isinstance(item, dict) for item in value):
        raise ValueError(f"Odoo returned an invalid {label} result")
    return cast(list[dict[str, object]], value)


def _review_product(value: object) -> tuple[int, Decimal, Decimal, str]:
    products = _as_records(value, "costed synthetic product lookup")
    if len(products) != 1:
        raise ValueError("local Odoo must have one saleable, costed Customizable Desk")
    product = products[0]
    product_id = product.get("id")
    category = product.get("categ_id")
    raw_cost = product.get("standard_price")
    if raw_cost is None or raw_cost is False:
        raise ValueError("local Odoo review product must have a known unit cost")
    try:
        price = Decimal(str(product.get("list_price")))
        cost = Decimal(str(raw_cost))
    except InvalidOperation as error:
        raise ValueError("local Odoo review product price or cost is invalid") from error
    if type(product_id) is not int:
        raise ValueError("local Odoo review product ID is invalid")
    if not price.is_finite() or price <= 0:
        raise ValueError("local Odoo review product price must be positive")
    if not cost.is_finite() or cost < 0 or cost >= price:
        raise ValueError("local Odoo review product must have cost below its price")
    if (
        not isinstance(category, list)
        or len(category) != 2
        or type(category[0]) is not int
        or not isinstance(category[1], str)
        or not category[1]
    ):
        raise ValueError("local Odoo review product category is invalid")
    return product_id, price, cost, category[1]


def _review_website(value: object) -> tuple[int, str]:
    websites = _as_records(value, "website lookup")
    if not websites:
        raise ValueError("local Odoo must have at least one website")
    website = websites[0]
    website_id = website.get("id")
    name = website.get("name")
    if type(website_id) is not int or not isinstance(name, str) or not name:
        raise ValueError("local Odoo review website is invalid")
    return website_id, name


def create_cart(_args: argparse.Namespace) -> None:
    settings, client, reader = _odoo()
    if not sys.stdin.isatty():
        raise ValueError("synthetic Odoo setup must be confirmed from an interactive terminal")
    print(f"local_odoo_host={urlsplit(settings.url).hostname}")
    with progress("create-synthetic-odoo-cart"):
        products = _as_records(
            client.call(
                "product.product",
                "search_read",
                {
                    "domain": [
                        ["name", "=", "Customizable Desk"],
                        ["sale_ok", "=", True],
                        ["list_price", ">", 0],
                        ["standard_price", ">", 0],
                    ],
                    "fields": ["id", "list_price", "standard_price", "categ_id"],
                    "limit": 1,
                },
            ),
            "costed synthetic product lookup",
        )
        websites = _as_records(
            client.call(
                "website",
                "search_read",
                {"domain": [], "fields": ["id", "name"], "order": "id asc", "limit": 1},
            ),
            "website lookup",
        )
        if len(products) != 1:
            raise ValueError("local Odoo must have one costed review product")
        product_id, price, unit_cost, category = _review_product(products)
        website_id, website_name = _review_website(websites)
        print(
            f"selected_product=Customizable Desk category={category} "
            f"unit_price={price} unit_cost={unit_cost}"
        )
        print(f"selected_website={website_name}")
        if input("Type CREATE SYNTHETIC ODOO CART to create one disposable cart: ") != (
            "CREATE SYNTHETIC ODOO CART"
        ):
            raise ValueError("synthetic cart creation was not confirmed")
        tag = uuid.uuid4().hex[:12]
        reference = f"retention-review-{tag}"
        print(f"synthetic_reference={reference}", flush=True)

        partner_ids = client.call(
            "res.partner",
            "create",
            {
                "vals_list": [
                    {
                        "name": f"Synthetic retention review {tag}",
                        "email": f"retention-review-{tag}@example.test",
                        "lang": "en_US",
                    }
                ]
            },
        )
        if (
            not isinstance(partner_ids, list)
            or len(partner_ids) != 1
            or type(partner_ids[0]) is not int
        ):
            raise ValueError("Odoo did not confirm the synthetic partner")
        partner_id = partner_ids[0]
        order_id: int | None = None
        try:
            old_order = (datetime.now(UTC) - timedelta(days=7)).strftime("%Y-%m-%d %H:%M:%S")
            order_ids = client.call(
                "sale.order",
                "create",
                {
                    "vals_list": [
                        {
                            "partner_id": partner_id,
                            "website_id": website_id,
                            "date_order": old_order,
                            "client_order_ref": reference,
                            "order_line": [
                                [
                                    0,
                                    0,
                                    {
                                        "product_id": product_id,
                                        "product_uom_qty": 1,
                                        "price_unit": float(price),
                                    },
                                ]
                            ],
                        }
                    ]
                },
            )
            if (
                not isinstance(order_ids, list)
                or len(order_ids) != 1
                or type(order_ids[0]) is not int
            ):
                raise ValueError("Odoo did not confirm the synthetic cart")
            order_id = order_ids[0]
            cart = reader.get_abandoned_cart(order_id, now=datetime.now(UTC))
            if cart is None or cart.partner_id != partner_id:
                raise ValueError("created cart did not pass the read-only abandoned-cart check")
        except Exception:
            if order_id is not None:
                client.call("sale.order", "unlink", {"ids": [order_id]})
            client.call("res.partner", "unlink", {"ids": [partner_id]})
            raise
    print(f"synthetic_cart_id={order_id}")
    print(f"synthetic_partner_id={partner_id}")
    print("Odoo contact: synthetic example.test address; value omitted")
    print(f"Odoo host: {settings.url}")


def _create_received_run(repo: RunRepository, cart: AbandonedCart) -> uuid.UUID:
    run, created = repo.create_run(
        trigger_type="synthetic",
        trigger_ref=f"{cart.partner_id}:{cart.order_id}",
        idempotency_key=f"gated-review:{uuid.uuid4()}",
        actor="synthetic-review-operator",
    )
    if not created:
        raise RuntimeError("could not create a unique synthetic review run")
    return run.id


def _new_run(repo: RunRepository, cart: AbandonedCart) -> uuid.UUID:
    run_id = _create_received_run(repo, cart)
    for previous, current in (
        (RunState.RECEIVED, RunState.VALIDATING_INPUT),
        (RunState.VALIDATING_INPUT, RunState.LOADING_CONTEXT),
        (RunState.LOADING_CONTEXT, RunState.GENERATING_PROPOSAL),
    ):
        repo.transition(run_id, previous, current, actor="synthetic-review-operator")
    return run_id


def _prepare_fixed(
    engine: sa.Engine,
    gate: Gate,
    cart: AbandonedCart,
    language: Literal["en", "ar"],
    channel: Channel,
) -> uuid.UUID:
    repo = RunRepository(engine)
    run_id = _new_run(repo, cart)
    total, cost = cart_to_proposal_inputs(cart)
    categories = {line.category for line in cart.lines}
    if len(categories) != 1:
        raise ValueError("fixed review requires a single-category synthetic cart")
    offer = OfferProposal(
        run_id=str(run_id),
        customer_id=str(cart.partner_id),
        cart_id=str(cart.order_id),
        cart_owner_id=str(cart.partner_id),
        category=next(iter(categories)),
        currency=cart.currency,
        eligible_total=total,
        eligible_unit_cost_total=cost,
        proposed_discount_percent=Decimal(0),
        customer_message=FIXED_MESSAGES[language],
        language=language,
    )
    decision = gate.evaluate(offer, now=datetime.now(UTC))
    record = json.loads(json.dumps(asdict(offer), default=str))
    record.update(
        {
            "action": "draft_message",
            "policy_sources": [],
            "cart_snapshot": cart_snapshot(cart),
            "execution_channel": channel,
        }
    )
    repo.persist_proposal(
        run_id,
        schema_version="fixed-review-v1",
        fingerprint=offer.fingerprint(),
        proposal=record,
        gate_result=as_dict(decision),
    )
    if decision.outcome is Outcome.REJECT:
        repo.transition(
            run_id, RunState.EVALUATING_POLICY, RunState.REJECTED, actor="synthetic-review-operator"
        )
    elif decision.outcome is Outcome.NEEDS_APPROVAL:
        repo.request_approval(run_id, gate)
    return run_id


def prepare(args: argparse.Namespace) -> None:
    engine = create_engine(_engine_url())
    try:
        gate = _gate(engine)
        _settings, _client, reader = _odoo()
        cart = reader.get_abandoned_cart(args.cart_id, now=datetime.now(UTC))
        if cart is None:
            raise ValueError("cart is missing or does not meet the abandoned-cart delay")
        with progress(f"prepare-{args.mode}-proposal"):
            if args.mode == "fixed":
                run_id = _prepare_fixed(engine, gate, cart, args.language, args.channel)
            else:
                settings = ProposalSettings.from_env(os.environ)
                if (
                    settings.review_budget is None
                    or settings.review_budget.review_id != LIVE_REVIEW_ID
                    or settings.review_budget.limit_usd != LIVE_REVIEW_BUDGET_USD
                    or settings.budget_usd > LIVE_REVIEW_BUDGET_USD
                ):
                    raise ValueError(
                        f"live review requires PROPOSAL_REVIEW_ID={LIVE_REVIEW_ID}, "
                        "PROPOSAL_REVIEW_BUDGET_USD=0.50 and a per-run budget within that ceiling"
                    )
                if not os.environ.get("OPENAI_API_KEY"):
                    raise ValueError("OPENAI_API_KEY must be set for model proposals")
                run_id = _create_received_run(RunRepository(engine), cart)
                service = ProposalService(
                    engine,
                    reader,
                    PostgresRetriever(engine, LocalE5Embedder()),
                    OpenAIClient(os.environ["OPENAI_API_KEY"]),
                    gate,
                    settings,
                )
                service.generate(
                    run_id,
                    customer_id=cart.partner_id,
                    cart_id=cart.order_id,
                    language=args.language,
                    request=args.request,
                    execution_channel=args.channel,
                )
        print(f"run_id={run_id}")
        _show(engine, run_id)
    finally:
        try:
            if args.mode == "model":
                _report_review_usage(engine)
        finally:
            engine.dispose()


def _print_review(
    state: object, proposal: Mapping[str, object], gate_result: Mapping[str, object]
) -> None:
    print(f"state={state}")
    channel = proposal.get("execution_channel", "not bound")
    print(f"channel={channel}")
    language = proposal.get("language", "en")
    print(f"language={language}")
    if channel == "mailpit":
        print("approved_subject=" + TestChannelDispatcher.mailpit_review_subject(language))
    print(f"discount_percent={proposal.get('proposed_discount_percent')}")
    print(f"gate={json.dumps(gate_result, ensure_ascii=False, sort_keys=True)}")
    print("approved_message_begin")
    print(proposal.get("customer_message") or "(no customer message)")
    print("approved_message_end")


def _show(engine: sa.Engine, run_id: uuid.UUID) -> None:
    with engine.connect() as conn:
        row = conn.execute(
            sa.select(runs.c.state, agent_proposals.c.proposal, agent_proposals.c.gate_result)
            .join(agent_proposals, agent_proposals.c.run_id == runs.c.id)
            .where(runs.c.id == run_id)
        ).first()
    if row is None:
        raise ValueError("run or proposal not found")
    _print_review(row.state, row.proposal, row.gate_result)


def show(args: argparse.Namespace) -> None:
    engine = create_engine(_engine_url())
    try:
        _show(engine, args.run_id)
    finally:
        engine.dispose()


def approve(args: argparse.Namespace) -> None:
    engine = create_engine(_engine_url())
    try:
        if not sys.stdin.isatty():
            raise ValueError("approval must be performed from an interactive terminal")
        _show(engine, args.run_id)
        pending = [
            item
            for item in RunRepository(engine).pending_approvals()
            if item["run_id"] == str(args.run_id)
        ]
        if len(pending) != 1:
            raise ValueError("run has no pending approval")
        phrase = input(f"Type APPROVE {args.run_id} to approve this exact proposal: ")
        if phrase != f"APPROVE {args.run_id}":
            raise ValueError("approval confirmation did not match")
        repo = RunRepository(engine)
        result = repo.decide_approval(
            args.run_id,
            token=str(pending[0]["snapshot_hash"]),
            decision="approve",
            actor=args.actor,
        )
        print(f"approval_state={result.state.value}")
    finally:
        engine.dispose()


def execute(args: argparse.Namespace) -> None:
    if not sys.stdin.isatty():
        raise ValueError("dispatch must be confirmed from an interactive terminal")
    env = os.environ
    execution_settings = ExecutionSettings.from_env(env)
    if env.get("EXECUTION_SYNTHETIC_ONLY") != "YES":
        raise ValueError("EXECUTION_SYNTHETIC_ONLY=YES is required")
    settings, client, reader = _odoo()
    telegram = (
        TelegramBotClient(env.get("TELEGRAM_BOT_TOKEN", "")) if args.channel == "telegram" else None
    )
    mailpit = (
        MailpitEmailAdapter(
            env.get("MAILPIT_SMTP_HOST", "127.0.0.1"),
            int(env.get("MAILPIT_SMTP_PORT", "1025")),
        )
        if args.channel == "mailpit"
        else None
    )
    engine = create_engine(_engine_url())
    try:
        record = RunRepository(engine).get_execution_record(args.run_id)
        if record.proposal.get("execution_channel") != args.channel:
            raise ValueError("requested channel differs from the approved proposal")
        target = (
            env.get("TELEGRAM_TEST_CHAT_ID", "").strip()
            if args.channel == "telegram"
            else "retention-review@example.test"
        )
        print(f"confirmed_channel={args.channel}")
        print(f"confirmed_destination={target}")
        _show(engine, args.run_id)
        if input(f"Type SEND {args.run_id} to authorize this one dispatch: ") != (
            f"SEND {args.run_id}"
        ):
            raise ValueError("dispatch confirmation did not match")
        if input("Confirm this is a dedicated synthetic review destination (type YES): ") != "YES":
            raise ValueError("test-destination confirmation did not match")
        destination = TestDestination.from_env(
            env, args.channel, confirm_dedicated_test_destination=True
        )
        service = ExecutionService(
            engine,
            reader,
            _gate(engine),
            OdooCouponCreator(client, settings),
            TestChannelDispatcher(telegram, mailpit),
            execution_settings,
        )
        with progress(f"dispatch-{args.channel}"):
            result = service.execute(args.run_id, destination=destination)
        print(f"execution_status={result.status} state={result.state.value}")
        print("Inspect the synthetic message in the selected channel before continuing.")
    finally:
        engine.dispose()


def preflight(args: argparse.Namespace) -> None:
    engine = create_engine(_engine_url())
    try:
        _gate(engine)
        settings, _client, reader = _odoo()
        if os.environ.get("EXECUTION_SYNTHETIC_ONLY") != "YES":
            raise ValueError("EXECUTION_SYNTHETIC_ONLY=YES is required")
        with engine.connect() as conn:
            for table in (
                "runs",
                "agent_proposals",
                "approval_requests",
                "execution_claims",
                "execution_effects",
                "gate_budget_days",
                "gate_coupon_reservations",
                "proposal_review_budgets",
                "proposal_calls",
                "policy_passages",
            ):
                conn.execute(sa.text(f"SELECT 1 FROM {table} LIMIT 0"))
        if args.cart_id is not None:
            cart = reader.get_abandoned_cart(args.cart_id, now=datetime.now(UTC))
            if cart is None:
                raise ValueError("cart is missing or does not meet the abandoned-cart delay")
        if args.channel == "mailpit":
            host = os.environ.get("MAILPIT_SMTP_HOST", "127.0.0.1")
            port = int(os.environ.get("MAILPIT_SMTP_PORT", "1025"))
            MailpitEmailAdapter(host, port)
            with socket.create_connection((host, port), timeout=2):
                pass
        else:
            TelegramBotClient(os.environ.get("TELEGRAM_BOT_TOKEN", ""))
            if not os.environ.get("TELEGRAM_TEST_CHAT_ID", "").strip():
                raise ValueError("TELEGRAM_TEST_CHAT_ID must be set")
        if args.mode == "model":
            proposal_settings = ProposalSettings.from_env(os.environ)
            if (
                proposal_settings.review_budget is None
                or proposal_settings.review_budget.review_id != LIVE_REVIEW_ID
                or proposal_settings.review_budget.limit_usd != LIVE_REVIEW_BUDGET_USD
                or proposal_settings.budget_usd > LIVE_REVIEW_BUDGET_USD
            ):
                raise ValueError("live review budget is missing or exceeds the USD 0.50 ceiling")
            OpenAIClient(os.environ.get("OPENAI_API_KEY", ""))
            if args.cart_id is None:
                raise ValueError("model preflight requires --cart-id")
            with engine.connect() as conn:
                indexed_languages = set(
                    conn.execute(
                        sa.text(
                            "SELECT DISTINCT language FROM policy_passages WHERE model_id=:model"
                        ),
                        {"model": MODEL_ID},
                    ).scalars()
                )
            if not {"en", "ar"}.issubset(indexed_languages):
                raise ValueError("pinned local policy index must contain English and Arabic")
        print("preflight=passed")
        print(f"odoo_host={urlsplit(settings.url).hostname}")
        print(f"channel={args.channel} mode={args.mode}")
        print(
            "Checks local engine schema/config, local Odoo configuration and optional cart, "
            "and the selected channel configuration/readiness."
        )
        print(
            "Does not send messages, call the LLM, load embedding models, verify Telegram "
            "credentials remotely, or prove retrieval quality."
        )
    finally:
        engine.dispose()


def parser() -> argparse.ArgumentParser:
    root = argparse.ArgumentParser(description="Operator-controlled #67 synthetic review flow")
    commands = root.add_subparsers(dest="command", required=True)

    create = commands.add_parser("create-cart", help="create a disposable synthetic Odoo cart")
    create.set_defaults(function=create_cart)

    pre = commands.add_parser("preflight", help="check local prerequisites without sending")
    pre.add_argument("--channel", type=_channel, required=True)
    pre.add_argument("--mode", choices=("fixed", "model"), default="fixed")
    pre.add_argument("--cart-id", type=int)
    pre.set_defaults(function=preflight)

    prep = commands.add_parser("prepare", help="create a fixed or live-model proposal")
    prep.add_argument("--cart-id", type=int, required=True)
    prep.add_argument("--channel", type=_channel, required=True)
    prep.add_argument("--language", choices=("en", "ar"), required=True)
    prep.add_argument("--mode", choices=("fixed", "model"), required=True)
    prep.add_argument("--request", default=REVIEW_REQUEST)
    prep.set_defaults(function=prepare)

    display = commands.add_parser("show", help="show the stored proposal for review")
    display.add_argument("--run-id", type=uuid.UUID, required=True)
    display.set_defaults(function=show)

    approval = commands.add_parser("approve", help="approve a gate-eligible proposal interactively")
    approval.add_argument("--run-id", type=uuid.UUID, required=True)
    approval.add_argument("--actor", default="synthetic-review-operator")
    approval.set_defaults(function=approve)

    dispatch = commands.add_parser("execute", help="dispatch one approved proposal interactively")
    dispatch.add_argument("--run-id", type=uuid.UUID, required=True)
    dispatch.add_argument("--channel", type=_channel, required=True)
    dispatch.set_defaults(function=execute)
    return root


def main() -> None:
    args = parser().parse_args()
    args.function(args)


if __name__ == "__main__":
    main()
