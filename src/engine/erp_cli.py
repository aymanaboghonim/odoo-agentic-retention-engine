"""Operator CLI for Q12 generalized ERP runs on the isolated Compose stack (#85).

Flow: the worker (`SCAN_MODE=erp`) creates `erp` runs. Then this CLI runs `propose` (a fixed
token template, or the model), then `show`, then `execute`. `execute` needs the exact run
ID and the destination hash printed by `show`. Output never contains names, emails, phones,
or secrets; the recipient appears only as its domain and HMAC hash.
"""

from __future__ import annotations

import argparse
import json
import os
import socket
import sys
import threading
import time
import uuid
from collections.abc import Callable, Iterator, Mapping, Sequence
from contextlib import contextmanager
from dataclasses import asdict
from datetime import UTC, datetime
from typing import Any, Literal, cast

import sqlalchemy as sa

from engine.channels import MailpitEmailAdapter, TelegramBotClient
from engine.db import RunRepository, create_engine
from engine.execution import (
    AllowlistedDispatcher,
    ErpExecution,
    ExecutionError,
    ExecutionService,
    ExecutionSettings,
)
from engine.fixed_rule import FIXED_SCHEMA, build_fixed_offer
from engine.full_stack_worker import WorkerSettings
from engine.gate_ledger import PostgresGateLedger
from engine.odoo import (
    AbandonedCart,
    JsonTwoClient,
    OdooCouponCreator,
    OdooReader,
    OdooSettings,
    cart_snapshot,
)
from engine.policy import Gate, GateConfig, Outcome, as_dict
from engine.privacy import AllowlistGuard, PiiTokenizer, PrivacySettings, consent_channel
from engine.proposal import approval_ttl_from_env
from engine.runs import RunState
from engine.runtime_state import EngineHalted, RuntimeGuard, RuntimeStateRepository

Channel = Literal["telegram", "mailpit"]
# Fixed templates use only the always-issued name token; rehydrated at dispatch.
FIXED_MESSAGES: Mapping[str, str] = {
    "en": "Hello {{PII_NAME_1}}, your cart is still waiting for you.",
    "ar": "مرحبا {{PII_NAME_1}}، سلتك ما زالت بانتظارك.",
}


@contextmanager
def progress(stage: str) -> Iterator[None]:
    stop = threading.Event()
    start = time.monotonic()

    def heartbeat() -> None:
        while not stop.wait(30):
            print(f"stage={stage} elapsed_s={time.monotonic() - start:.0f}", flush=True)

    print(f"stage={stage} status=started", flush=True)
    thread = threading.Thread(target=heartbeat, daemon=True)
    thread.start()
    try:
        yield
    finally:
        stop.set()
        thread.join()
        print(f"stage={stage} elapsed_s={time.monotonic() - start:.0f} status=ended", flush=True)


class _Live:
    """Isolated-stack dependencies, validated before any state change."""

    def __init__(self, env: Mapping[str, str]) -> None:
        self.worker = WorkerSettings.from_env(env)
        self.odoo = OdooSettings.from_env(env)
        if self.odoo.url != "http://odoo:8069" or self.odoo.database != "odoo":
            raise ValueError("ERP CLI is restricted to the isolated Compose Odoo")
        self.privacy = PrivacySettings.from_env(env)
        self.allowlist = AllowlistGuard.from_env(env)
        self.gate_config = GateConfig.from_env(env)
        self.reader = OdooReader(self.worker.odoo_url)
        self.reader.verify_readonly_role()
        self.engine = create_engine(self.worker.engine_url)
        self.env = env
        # Compose service name of the isolated Mailpit sink (private network only).
        self.mailpit_host = env.get("ERP_MAILPIT_SMTP_HOST", "mailpit").strip() or "mailpit"
        # #87: the operator CLI obeys the same runtime state as the worker (fails closed).
        self.runtime = RuntimeGuard(RuntimeStateRepository(self.engine))

    def gate(self) -> Gate:
        return Gate(self.gate_config, PostgresGateLedger(self.engine))

    def execution(self, channel: Channel | None) -> ExecutionService:
        settings = ExecutionSettings.from_env(self.env)
        if not settings.erp_enabled:
            raise ValueError("EXECUTION_ERP_MODE=YES is required")
        telegram = (
            TelegramBotClient(self.env.get("TELEGRAM_BOT_TOKEN", ""))
            if channel == "telegram"
            else None
        )
        mailpit = (
            MailpitEmailAdapter(self.mailpit_host, 1025, allow_private_service=True)
            if channel == "mailpit"
            else None
        )
        return ExecutionService(
            self.engine,
            self.reader,
            self.gate(),
            OdooCouponCreator(JsonTwoClient(self.odoo), self.odoo),
            _NoSyntheticDispatch(),
            settings,
            runtime=self.runtime,
            erp=ErpExecution(
                self.privacy,
                self.allowlist,
                AllowlistedDispatcher(self.allowlist, telegram, mailpit),
                self.reader.verify_loyalty_module_installed,
                telegram_chat_id=self.env.get("ERP_TELEGRAM_CHAT_ID", ""),
            ),
        )


class _NoSyntheticDispatch:
    def send(self, *args: object, **kwargs: object) -> str:
        raise ExecutionError("the synthetic dispatcher is not used for ERP runs")


def _erp_run(live: _Live, run_id: uuid.UUID) -> tuple[int, int]:
    run = RunRepository(live.engine).get_run(run_id)
    if run.trigger_type != "erp" or run.customer_id is None:
        raise ValueError("run is not an ERP run")
    customer, order = (int(part) for part in run.trigger_ref.split(":", 1))
    if str(customer) != run.customer_id:
        raise ValueError("ERP run customer binding is inconsistent")
    return customer, order


def preflight(live: _Live, args: argparse.Namespace) -> None:
    with live.engine.connect() as conn:
        version = conn.execute(sa.text("SELECT version_num FROM alembic_version")).scalar()
    if version is None or int(version) < 11:
        raise ValueError("Engine DB schema is older than 0011; run the initialize service")
    ExecutionSettings.from_env(live.env)
    if live.env.get("EXECUTION_ERP_MODE") != "YES":
        raise ValueError("EXECUTION_ERP_MODE=YES is required")
    kind = consent_channel(args.channel)
    if kind == "email" and not (live.allowlist.domains or live.allowlist.emails):
        raise ValueError("EXECUTION_ALLOWLIST_DOMAINS or EXECUTION_ALLOWLIST_EMAILS must be set")
    if kind == "telegram":
        TelegramBotClient(live.env.get("TELEGRAM_BOT_TOKEN", ""))
        live.allowlist.require("telegram", live.env.get("ERP_TELEGRAM_CHAT_ID", ""))
    else:
        MailpitEmailAdapter(live.mailpit_host, 1025, allow_private_service=True)
        with socket.create_connection((live.mailpit_host, 1025), timeout=2):
            pass
    try:
        live.reader.verify_loyalty_module_installed()
        loyalty = "installed"
    except Exception:
        loyalty = "missing (positive discounts will be rejected)"
    print(
        f"stage=preflight status=passed channel={args.channel} schema={version} "
        f"website_sale_loyalty={loyalty}",
        flush=True,
    )
    print(
        "checked: isolation, read-only Odoo role, Engine DB schema, privacy settings, "
        "allowlist, channel readiness. not checked: consent per customer, LLM access, "
        "Telegram credentials remotely.",
        flush=True,
    )


def _fixed_proposal(
    live: _Live, run_id: uuid.UUID, cart: AbandonedCart, language: str, channel: Channel
) -> None:
    repo = RunRepository(live.engine)
    for previous, current in (
        (RunState.RECEIVED, RunState.VALIDATING_INPUT),
        (RunState.VALIDATING_INPUT, RunState.LOADING_CONTEXT),
        (RunState.LOADING_CONTEXT, RunState.GENERATING_PROPOSAL),
    ):
        repo.transition(run_id, previous, current, actor="erp-operator")
    categories = {line.category for line in cart.lines}
    if len(categories) != 1:
        repo.fail(run_id, reason="fixed_proposal_requires_single_category", actor="erp-operator")
        raise ValueError("fixed ERP proposal requires a single-category cart")
    tokenizer = PiiTokenizer.for_cart(cart)
    if "{{PII_NAME_1}}" not in tokenizer.issued():
        repo.fail(run_id, reason="fixed_proposal_requires_name", actor="erp-operator")
        raise ValueError("fixed ERP proposal requires a customer name in Odoo")
    offer = build_fixed_offer(
        run_id,
        cart,
        "en" if language == "en" else "ar",
        message=FIXED_MESSAGES[language],
    )
    gate = live.gate()
    decision = gate.evaluate(offer, now=datetime.now(UTC))
    record = json.loads(json.dumps(asdict(offer), default=str))
    record.update(
        {
            "action": "draft_message",
            "policy_sources": [],
            "cart_snapshot": cart_snapshot(cart),
            "execution_channel": channel,
            "pii_tokens": tokenizer.token_map(),
        }
    )
    repo.persist_proposal(
        run_id,
        schema_version=FIXED_SCHEMA,
        fingerprint=offer.fingerprint(),
        proposal=record,
        gate_result=as_dict(decision),
    )
    if decision.outcome is Outcome.REJECT:
        repo.transition(run_id, RunState.EVALUATING_POLICY, RunState.REJECTED, actor="erp-operator")
    elif decision.outcome is Outcome.NEEDS_APPROVAL:
        repo.request_approval(run_id, gate, ttl=approval_ttl_from_env(live.env))


class _LazyProvider:
    """Builds the model client only when a call is made, so fallback needs no API key."""

    def __init__(self, factory: Callable[[], Any]) -> None:
        self._factory = factory
        self._client: Any = None

    def _get(self) -> Any:
        if self._client is None:
            self._client = self._factory()
        return self._client

    def count_tokens(self, payload: Mapping[str, object]) -> int:
        return int(self._get().count_tokens(payload))

    def generate(self, payload: Mapping[str, object], output_limit: int) -> Any:
        return self._get().generate(payload, output_limit)


class _LazyRetriever:
    """Loads the embedding model only when retrieval runs (never in fallback)."""

    def __init__(self, factory: Callable[[], Any]) -> None:
        self._factory = factory
        self._retriever: Any = None

    def search(self, query: str, language: str) -> Any:
        if self._retriever is None:
            self._retriever = self._factory()
        return self._retriever.search(query, language)


def propose(live: _Live, args: argparse.Namespace) -> None:
    live.runtime.require_not_halted()  # KS-3: nothing is proposed while halted
    customer, order = _erp_run(live, args.run_id)
    cart = live.reader.get_abandoned_cart(order, now=datetime.now(UTC))
    if cart is None or cart.partner_id != customer:
        raise ValueError("cart is missing, no longer abandoned, or owned by another customer")
    with progress(f"erp-propose-{args.mode}"):
        if args.mode == "fixed":
            _fixed_proposal(live, args.run_id, cart, args.language, args.channel)
        else:
            from engine.proposal import ProposalService, ProposalSettings
            from engine.proposal_provider import OpenAIClient
            from engine.retrieval import LocalE5Embedder, PostgresRetriever

            api_key = live.env.get("OPENAI_API_KEY", "")

            settings = ProposalSettings.from_env(live.env)
            if not settings.erp_enabled or settings.review_budget is None:
                raise ValueError(
                    "model mode needs PROPOSAL_ERP_MODE=YES and an aggregate review budget"
                )
            ProposalService(
                live.engine,
                live.reader,
                _LazyRetriever(lambda: PostgresRetriever(live.engine, LocalE5Embedder())),
                _LazyProvider(lambda: OpenAIClient(api_key)),
                live.gate(),
                settings,
                runtime=live.runtime,
            ).generate(
                args.run_id,
                customer_id=customer,
                cart_id=order,
                language=args.language,
                request=args.request,
                execution_channel=args.channel,
            )
    show(live, args)


def show(live: _Live, args: argparse.Namespace) -> None:
    run = RunRepository(live.engine).get_run(args.run_id)
    with live.engine.connect() as conn:
        row = conn.execute(
            sa.text("SELECT proposal, gate_result FROM agent_proposals WHERE run_id=:id"),
            {"id": args.run_id},
        ).first()
    print(f"run_id={run.id} state={run.state.value} trigger_type={run.trigger_type}")
    if row is None:
        return
    proposal = cast(dict[str, object], row.proposal)
    print(f"gate_outcome={cast(dict[str, object], row.gate_result).get('outcome')}")
    print(f"channel={proposal.get('execution_channel')} language={proposal.get('language')}")
    print(f"discount_percent={proposal.get('proposed_discount_percent')}")
    print(f"message_tokenized={json.dumps(proposal.get('customer_message'), ensure_ascii=False)}")
    if run.state is RunState.EVALUATING_POLICY:
        channel = cast(Channel, proposal.get("execution_channel"))
        try:
            recipient = live.execution(channel).preview_erp_recipient(args.run_id)
        except ExecutionError as error:
            print(f"privacy_preview=refused reason={error}")
            return
        domain = recipient.target.rsplit("@", 1)[-1] if "@" in recipient.target else "telegram"
        print(f"privacy_preview=passed recipient_domain={domain}")
        print(f"destination_hash={recipient.destination_hash}")


def execute(live: _Live, args: argparse.Namespace) -> None:
    if args.confirm_run_id != args.run_id:
        raise ValueError("--confirm-run-id must repeat the exact run ID")
    _erp_run(live, args.run_id)
    record = RunRepository(live.engine).get_execution_record(args.run_id)
    channel = cast(Channel, record.proposal.get("execution_channel"))
    service = live.execution(channel)
    recipient = service.preview_erp_recipient(args.run_id)
    if recipient.destination_hash != args.confirm_destination:
        raise ValueError("--confirm-destination does not match the current destination_hash")
    with progress(f"erp-execute-{channel}"):
        result = service.execute_erp(args.run_id)
    print(f"execution_status={result.status} state={result.state.value}")


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Q12 ERP run operator CLI (isolated stack)")
    commands = parser.add_subparsers(dest="command", required=True)
    pre = commands.add_parser("preflight", help="check settings and readiness; no change")
    pre.add_argument("--channel", choices=("mailpit", "telegram"), required=True)
    prop = commands.add_parser("propose", help="create one ERP proposal for a received run")
    prop.add_argument("--run-id", type=uuid.UUID, required=True)
    prop.add_argument("--channel", choices=("mailpit", "telegram"), required=True)
    prop.add_argument("--language", choices=("en", "ar"), default="en")
    prop.add_argument("--mode", choices=("fixed", "model"), default="fixed")
    prop.add_argument("--request", default="Please remind me about my cart.")
    display = commands.add_parser("show", help="show the tokenized proposal and privacy preview")
    display.add_argument("--run-id", type=uuid.UUID, required=True)
    run = commands.add_parser("execute", help="dispatch one ERP run after exact confirmation")
    run.add_argument("--run-id", type=uuid.UUID, required=True)
    run.add_argument("--confirm-run-id", type=uuid.UUID, required=True)
    run.add_argument("--confirm-destination", required=True)
    return parser


def main(argv: Sequence[str] | None = None, env: Mapping[str, str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        live = _Live(os.environ if env is None else env)
    except (ValueError, OSError) as error:
        print(f"stage=preflight status=failed reason={error}", file=sys.stderr, flush=True)
        return 1
    try:
        {"preflight": preflight, "propose": propose, "show": show, "execute": execute}[
            args.command
        ](live, args)
        return 0
    except (ValueError, OSError, ExecutionError, EngineHalted) as error:
        print(f"stage={args.command} status=failed reason={error}", file=sys.stderr, flush=True)
        return 1
    finally:
        live.engine.dispose()


if __name__ == "__main__":
    raise SystemExit(main())
