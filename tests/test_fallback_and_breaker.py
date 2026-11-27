"""#87 fallback and breaker invariants KS-3, KS-4, KS-5, EV-5.

Spec: docs/specs/release-gates-and-kill-switch.md. Every run these tests create is deleted.
"""

from __future__ import annotations

import json
import os
import random
import re
import uuid
from collections.abc import Callable, Iterator, Mapping
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest
import sqlalchemy as sa

from engine import proposal as proposal_module
from engine.breaker import (
    BreakerReading,
    BreakerSettings,
    read_window,
    run_breaker,
    trip_reason,
)
from engine.config import normalize_database_url
from engine.db import RunRepository, create_engine, upgrade_schema
from engine.execution import (
    MAILPIT_REVIEW_RECIPIENT,
    EngineHeldError,
    ExecutionError,
    ExecutionService,
    ExecutionSettings,
    TestDestination,
)
from engine.fixed_rule import FIXED_REMINDERS, FIXED_SCHEMA, build_fixed_offer
from engine.gate_ledger import InMemoryGateLedger
from engine.odoo import AbandonedCart, CartLine
from engine.policy import Gate, GateConfig, Outcome, evaluate
from engine.proposal import (
    NON_PROVIDER_FAILURE_REASONS,
    PROVIDER_FAILURE_REASONS,
    ProposalService,
    ProposalSettings,
)
from engine.proposal_budget import Rate
from engine.proposal_provider import ProviderResult
from engine.retrieval import SearchResult
from engine.runs import RunState
from engine.runtime_state import (
    EngineHalted,
    RuntimeGuard,
    RuntimeSnapshot,
    RuntimeState,
    RuntimeStateRepository,
)
from engine.worker import GuardedScan

URL = os.environ.get("TEST_DATABASE_URL", "")
if not URL and os.environ.get("REQUIRE_DB_TESTS") == "1":
    raise RuntimeError("REQUIRE_DB_TESTS=1 but TEST_DATABASE_URL is not set")

DESTINATION = TestDestination("mailpit", MAILPIT_REVIEW_RECIPIENT, confirmed=True)
CHILD_TABLES = (
    "execution_effects",
    "execution_claims",
    "approval_requests",
    "agent_proposals",
    "proposal_calls",
    "proposal_budget",
    "run_events",
)


# --- fixtures and fakes --------------------------------------------------------------


@pytest.fixture
def db() -> Iterator[sa.Engine]:
    if not URL:
        pytest.skip("TEST_DATABASE_URL not set")
    engine = create_engine(normalize_database_url(URL))
    upgrade_schema(engine)
    yield engine
    engine.dispose()


def reset_runtime(engine: sa.Engine) -> None:
    with engine.begin() as conn:
        conn.execute(
            sa.text(
                "INSERT INTO engine_runtime_state "
                "(id, state, reason, changed_by, changed_at, version) "
                "VALUES (1, 'active', 'initial state', 'migration', now(), 1) "
                "ON CONFLICT (id) DO UPDATE SET state='active', reason='initial state', "
                "changed_by='migration', changed_at=now(), version=1"
            )
        )
        conn.execute(sa.text("DELETE FROM engine_runtime_events"))


@pytest.fixture
def runtime(db: sa.Engine) -> Iterator[RuntimeStateRepository]:
    reset_runtime(db)
    yield RuntimeStateRepository(db)
    reset_runtime(db)


@pytest.fixture
def runs(db: sa.Engine) -> Iterator[list[uuid.UUID]]:
    """Collects run IDs so every row a test creates is removed afterwards."""
    created: list[uuid.UUID] = []
    yield created
    with db.begin() as conn:
        for run_id in created:
            for table in CHILD_TABLES:
                conn.execute(sa.text(f"DELETE FROM {table} WHERE run_id=:r"), {"r": run_id})
            conn.execute(sa.text("DELETE FROM runs WHERE id=:r"), {"r": run_id})


def make_cart(**changes: object) -> AbandonedCart:
    cart = AbandonedCart(
        order_id=1,
        reference="S1",
        partner_id=9,
        partner_name="Example Person",
        partner_email="test@example.test",
        language="en",
        currency="USD",
        amount_total=Decimal("100"),
        date_order=datetime.now(UTC) - timedelta(days=2),
        lines=(CartLine(1, "Lamp", "L1", "decor", Decimal(1), Decimal(100), Decimal(40)),),
    )
    return replace(cart, **changes)  # type: ignore[arg-type]


class Reader:
    """Cart source for both the proposal and the execution step."""

    def __init__(self, cart: AbandonedCart | None = None) -> None:
        self.cart = cart or make_cart()
        self.reloads = 0
        self.on_reload: Callable[[int], None] | None = None

    def get_cart(self, order_id: int) -> AbandonedCart | None:
        return self.cart if order_id == self.cart.order_id else None

    def get_abandoned_cart(self, order_id: int, *, now: datetime) -> AbandonedCart | None:
        self.reloads += 1
        if self.on_reload is not None:
            self.on_reload(self.reloads)
        return self.cart if order_id == self.cart.order_id else None


class Policies:
    def __init__(self) -> None:
        self.searches = 0

    def search(self, query: str, language: str) -> list[SearchResult]:
        self.searches += 1
        return [SearchResult("consent", "1", language, "Respect consent.", "hash", 0.1)]


class Provider:
    def __init__(self) -> None:
        self.calls = 0

    def count_tokens(self, payload: Mapping[str, object]) -> int:
        self.calls += 1
        return 10

    def generate(self, payload: Mapping[str, object], output_limit: int) -> ProviderResult:
        self.calls += 1
        body = {
            "action": "draft_message",
            "customer_message": "Your cart is waiting.",
            "price_claim": None,
            "currency": None,
            "proposed_discount_percent": None,
        }
        return ProviderResult(json.dumps(body), 10, 20, True)


class Coupons:
    def __init__(self) -> None:
        self.calls = 0

    def create(self, approval: object, proposal: object) -> str:
        self.calls += 1
        return "unused"


class Dispatcher:
    def __init__(self) -> None:
        self.sent: list[str] = []

    def send(
        self,
        destination: object,
        text: str,
        *,
        language: str = "en",
        message_id: str | None = None,
    ) -> str:
        self.sent.append(text)
        return f"message-{len(self.sent)}"


def gate() -> Gate:
    config = GateConfig.from_env(
        {
            "GATE_TIMEZONE": "Asia/Riyadh",
            "GATE_CURRENCY": "USD",
            "GATE_DAILY_BUDGET": "100",
            "GATE_CATEGORY_MIN_MARGINS": '{"decor":"0.2"}',
        }
    )
    return Gate(config, InMemoryGateLedger())


class Harness:
    def __init__(self, db: sa.Engine, runs: list[uuid.UUID], runtime: RuntimeStateRepository):
        self.db = db
        self.created = runs
        self.runtime = runtime
        self.reader = Reader()
        self.policies = Policies()
        self.provider = Provider()
        self.gate = gate()
        self.coupons = Coupons()
        self.dispatcher = Dispatcher()
        self.guard = RuntimeGuard(runtime)

    def new_run(self) -> uuid.UUID:
        run, _ = RunRepository(self.db).create_run(
            trigger_type="synthetic", trigger_ref="9:1", idempotency_key=f"q13-{uuid.uuid4()}"
        )
        self.created.append(run.id)
        return run.id

    def propose(self, language: str = "en") -> uuid.UUID:
        run_id = self.new_run()
        ProposalService(
            self.db,
            self.reader,
            self.policies,
            self.provider,
            self.gate,
            ProposalSettings(
                True, Decimal("1"), 100, 100, Rate(Decimal("0.125"), Decimal("0.5"), "F-005")
            ),
            runtime=self.guard,
        ).generate(
            run_id,
            customer_id=9,
            cart_id=1,
            language=language,
            request="Please remind me",
            execution_channel="mailpit",
        )
        return run_id

    def execute(self, run_id: uuid.UUID, guard: RuntimeGuard | None = None) -> object:
        return ExecutionService(
            self.db,
            self.reader,
            self.gate,
            self.coupons,
            self.dispatcher,
            ExecutionSettings(synthetic_only=True),
            runtime=guard or self.guard,
        ).execute(run_id, destination=DESTINATION)

    def state_of(self, run_id: uuid.UUID) -> RunState:
        return RunRepository(self.db).get_run(run_id).state

    def count(self, table: str, run_id: uuid.UUID) -> int:
        with self.db.connect() as conn:
            return int(
                conn.execute(
                    sa.text(f"SELECT count(*) FROM {table} WHERE run_id=:r"), {"r": run_id}
                ).scalar_one()
            )

    def set_state(self, state: RuntimeState) -> None:
        self.runtime.set_state(state, actor="admin-test", reason="test state change")


@pytest.fixture
def h(db: sa.Engine, runs: list[uuid.UUID], runtime: RuntimeStateRepository) -> Harness:
    return Harness(db, runs, runtime)


# --- KS-4 and EV-5: the fixed-rule fallback ------------------------------------------


def test_fallback_makes_no_llm_call_and_uses_gate(h: Harness) -> None:
    """KS-4: no model call, no retrieval, no model budget; the same gate and approval path."""
    h.set_state(RuntimeState.FALLBACK)
    for language in ("en", "ar"):
        run_id = h.propose(language)
        assert h.state_of(run_id) is RunState.EVALUATING_POLICY
        with h.db.connect() as conn:
            row = conn.execute(
                sa.text(
                    "SELECT schema_version, proposal, gate_result FROM agent_proposals "
                    "WHERE run_id=:r"
                ),
                {"r": run_id},
            ).one()
        assert row.schema_version == FIXED_SCHEMA
        assert row.proposal["customer_message"] == FIXED_REMINDERS[language]
        assert Decimal(row.proposal["proposed_discount_percent"]) == 0
        assert row.gate_result["outcome"] == "auto_approve"
        assert "pii_tokens" in row.proposal and row.proposal["execution_channel"] == "mailpit"
        assert h.count("proposal_budget", run_id) == 0 and h.count("proposal_calls", run_id) == 0
    assert h.provider.calls == 0 and h.policies.searches == 0
    # The same call in the active state does use the model (the guard is what changes it).
    h.set_state(RuntimeState.ACTIVE)
    h.propose("en")
    assert h.provider.calls > 0 and h.policies.searches == 1


def test_fallback_respects_safety_gate() -> None:
    """EV-5: for any cart the fixed rule is a 0% offer that the gate accepts as is."""
    rng = random.Random(87)
    config = GateConfig.from_env(
        {
            "GATE_TIMEZONE": "Asia/Riyadh",
            "GATE_CURRENCY": "USD",
            "GATE_DAILY_BUDGET": "100",
            "GATE_CATEGORY_MIN_MARGINS": '{"decor":"0.2"}',
        }
    )
    for index in range(200):
        lines = tuple(
            CartLine(
                n,
                f"item {n}",
                None,
                rng.choice(["decor", "furniture", "unknown category"]),
                Decimal(rng.randint(1, 5)),
                Decimal(rng.randint(1, 100_000)) / 100,
                None if rng.random() < 0.3 else Decimal(rng.randint(0, 100_000)) / 100,
            )
            for n in range(rng.randint(1, 4))
        )
        cart = make_cart(lines=lines, partner_id=rng.randint(1, 10**6), order_id=index + 1)
        language = rng.choice(["en", "ar"])
        offer = build_fixed_offer(uuid.uuid4(), cart, language)  # type: ignore[arg-type]
        decision = evaluate(offer, config, now=datetime.now(UTC), ledger=InMemoryGateLedger())
        assert offer.proposed_discount_percent == 0, cart
        assert decision.outcome is Outcome.AUTO_APPROVE, (cart, decision)
        assert offer.customer_message == FIXED_REMINDERS[language]
    with pytest.raises(ValueError, match="en and ar"):
        build_fixed_offer(uuid.uuid4(), make_cart(), "fr")  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="lines"):
        build_fixed_offer(uuid.uuid4(), make_cart(lines=()), "en")
    # The reminder never states a discount or a price.
    for text in FIXED_REMINDERS.values():
        assert "%" not in text and not re.search(r"\d", text)


def test_fallback_executes_fixed_runs_and_holds_llm_runs(h: Harness) -> None:
    """KS-4: LLM-made proposals are held in fallback; fixed-rule ones execute."""
    llm_run = h.propose()  # made while active
    h.set_state(RuntimeState.FALLBACK)
    fixed_run = h.propose()
    with pytest.raises(EngineHeldError, match="fallback") as held:
        h.execute(llm_run)
    assert held.value.reason == "engine_fallback"
    assert h.state_of(llm_run) is RunState.EVALUATING_POLICY  # held, not rejected
    assert h.count("execution_claims", llm_run) == 0 and h.dispatcher.sent == []
    assert h.execute(fixed_run).status == "completed"  # type: ignore[attr-defined]
    assert h.dispatcher.sent == [FIXED_REMINDERS["en"]]
    h.set_state(RuntimeState.ACTIVE)  # re-enabled: the held run resumes
    assert h.execute(llm_run).status == "completed"  # type: ignore[attr-defined]
    # Fallback arriving after the claim fails the run before any send.
    after_claim = h.propose()
    sent_before = len(h.dispatcher.sent)
    h.reader.reloads = 0
    h.reader.on_reload = lambda n: h.set_state(RuntimeState.FALLBACK) if n == 1 else None
    with pytest.raises(ExecutionError, match="fallback"):
        h.execute(after_claim)
    run = RunRepository(h.db).get_run(after_claim)
    assert run.state is RunState.FAILED and run.failure_reason == "engine_fallback"
    assert len(h.dispatcher.sent) == sent_before


# --- KS-3: halted ---------------------------------------------------------------------


def test_halted_creates_no_runs_and_no_effects(h: Harness) -> None:
    """KS-3: a halted engine scans nothing, proposes nothing, claims nothing, sends nothing."""

    class Scanner:
        calls = 0

        def scan_page(self, *, now: datetime) -> tuple[int, int]:
            self.calls += 1
            return 5, 5

    scanner = Scanner()
    guarded = GuardedScan(scanner, h.guard)
    assert guarded.scan_page(now=datetime.now(UTC)) == (5, 5) and scanner.calls == 1
    prepared = h.propose()  # made while active
    h.set_state(RuntimeState.HALTED)
    assert guarded.scan_page(now=datetime.now(UTC)) == (0, 0) and scanner.calls == 1

    # A proposal is refused before any state change, so the run stays resumable.
    refused = h.new_run()
    provider_calls = h.provider.calls
    with pytest.raises(EngineHalted):
        ProposalService(
            h.db,
            h.reader,
            h.policies,
            h.provider,
            h.gate,
            ProposalSettings(
                True, Decimal("1"), 100, 100, Rate(Decimal("0.125"), Decimal("0.5"), "F-005")
            ),
            runtime=h.guard,
        ).generate(refused, customer_id=9, cart_id=1, language="en", request="Please remind me")
    assert h.state_of(refused) is RunState.RECEIVED and h.provider.calls == provider_calls
    assert h.count("proposal_budget", refused) == 0

    # Execution is refused before the gate or any claim; nothing changes.
    with pytest.raises(EngineHeldError, match="halted"):
        h.execute(prepared)
    assert h.state_of(prepared) is RunState.EVALUATING_POLICY
    assert h.count("execution_claims", prepared) == 0 and h.dispatcher.sent == []
    assert h.coupons.calls == 0
    h.set_state(RuntimeState.ACTIVE)
    assert h.execute(prepared).status == "completed"  # type: ignore[attr-defined]
    assert len(h.dispatcher.sent) == 1

    # A halt that arrives after the claim fails the run before its message is sent.
    late = h.propose()
    h.reader.reloads = 0
    h.reader.on_reload = lambda n: h.set_state(RuntimeState.HALTED) if n == 1 else None
    with pytest.raises(ExecutionError, match="halted"):
        h.execute(late)
    run = RunRepository(h.db).get_run(late)
    assert run.state is RunState.FAILED and run.failure_reason == "engine_halted"
    assert len(h.dispatcher.sent) == 1

    # An unreadable state is treated as halted everywhere (KS-6 through the real services).
    h.reader.on_reload = None
    h.set_state(RuntimeState.ACTIVE)
    blind = RuntimeGuard(lambda: (_ for _ in ()).throw(RuntimeError("database is down")))
    unreadable = h.propose()
    with pytest.raises(EngineHeldError):
        h.execute(unreadable, guard=blind)
    assert h.state_of(unreadable) is RunState.EVALUATING_POLICY


# --- KS-5: the circuit breaker ---------------------------------------------------------

SETTINGS = BreakerSettings(
    window_minutes=15,
    min_sample=5,
    max_failure_rate_percent=Decimal("20"),
    max_p95_seconds=Decimal("30"),
)


def add_attempt(
    db: sa.Engine,
    runs: list[uuid.UUID],
    *,
    started: datetime,
    failure_reason: str | None = None,
    seconds: float | None = None,
    budget: bool = True,
) -> None:
    """One proposal attempt: a run that entered generating_proposal at `started`."""
    run, _ = RunRepository(db).create_run(
        trigger_type="synthetic", trigger_ref="9:1", idempotency_key=f"q13-{uuid.uuid4()}"
    )
    runs.append(run.id)
    state = "failed" if failure_reason else "evaluating_policy"
    with db.begin() as conn:
        conn.execute(
            sa.text("UPDATE runs SET state=:s, failure_reason=:f WHERE id=:r"),
            {"s": state, "f": failure_reason, "r": run.id},
        )
        events = [("generating_proposal", started)]
        if seconds is not None:
            events.append(("evaluating_policy", started + timedelta(seconds=seconds)))
        for to_state, at in events:
            conn.execute(
                sa.text(
                    "INSERT INTO run_events (run_id, to_state, actor, at) "
                    "VALUES (:r, :t, 'test', :a)"
                ),
                {"r": run.id, "t": to_state, "a": at},
            )
        if budget:
            conn.execute(
                sa.text("INSERT INTO proposal_budget (run_id, limit_usd) VALUES (:r, 1)"),
                {"r": run.id},
            )


def test_breaker_requires_explicit_settings() -> None:
    """KS-5: no defaults; off unless enabled; every number must be set and valid."""
    assert BreakerSettings.from_env({}) is None
    assert BreakerSettings.from_env({"BREAKER_ENABLED": "NO"}) is None
    full = {
        "BREAKER_ENABLED": "YES",
        "BREAKER_WINDOW_MINUTES": "15",
        "BREAKER_MIN_SAMPLE": "20",
        "BREAKER_MAX_FAILURE_RATE_PERCENT": "10",
        "BREAKER_MAX_P95_SECONDS": "30",
    }
    settings = BreakerSettings.from_env(full)
    assert settings is not None and settings.min_sample == 20
    for name in list(full)[1:]:
        with pytest.raises(ValueError, match="set explicitly"):
            BreakerSettings.from_env({k: v for k, v in full.items() if k != name})
    for bad in (
        {"BREAKER_ENABLED": "maybe"},
        {**full, "BREAKER_WINDOW_MINUTES": "0"},
        {**full, "BREAKER_WINDOW_MINUTES": "1441"},
        {**full, "BREAKER_MIN_SAMPLE": "0"},
        {**full, "BREAKER_MAX_FAILURE_RATE_PERCENT": "0"},
        {**full, "BREAKER_MAX_FAILURE_RATE_PERCENT": "101"},
        {**full, "BREAKER_MAX_P95_SECONDS": "0"},
        {**full, "BREAKER_MAX_P95_SECONDS": "NaN"},
        {**full, "BREAKER_MIN_SAMPLE": "many"},
    ):
        with pytest.raises(ValueError):
            BreakerSettings.from_env(bad)


def test_breaker_trips_only_to_fallback_with_minimum_sample(h: Harness) -> None:
    """KS-5: minimum sample, trip to fallback only, audited, and never re-enables."""
    now = datetime.now(UTC)
    started = now - timedelta(minutes=5)
    for _ in range(4):
        add_attempt(h.db, h.created, started=started, failure_reason="provider_error")
    # Below the minimum sample: 100% failures still do not trip.
    assert run_breaker(h.db, h.runtime, SETTINGS, now=now) is False
    assert h.runtime.get().state is RuntimeState.ACTIVE
    add_attempt(h.db, h.created, started=started, seconds=2)
    assert run_breaker(h.db, h.runtime, None, now=now) is False  # breaker off
    assert run_breaker(h.db, h.runtime, SETTINGS, now=now) is True
    snapshot = h.runtime.get()
    assert snapshot.state is RuntimeState.FALLBACK and snapshot.changed_by == "circuit-breaker"
    assert "failure rate" in snapshot.reason and snapshot.version == 2
    # Already degraded: it does nothing more and adds no audit rows.
    assert run_breaker(h.db, h.runtime, SETTINGS, now=now) is False
    assert len(h.runtime.history()) == 1
    # An admin halts; the breaker never changes a halted engine or re-enables anything.
    h.set_state(RuntimeState.HALTED)
    assert run_breaker(h.db, h.runtime, SETTINGS, now=now) is False
    h.set_state(RuntimeState.ACTIVE)
    assert run_breaker(h.db, h.runtime, SETTINGS, now=now) is True  # still unhealthy: trips again
    assert all(
        e["to_state"] == "fallback_fixed_rule"
        for e in h.runtime.history()
        if e["actor"] == "circuit-breaker"
    )
    # An admin who wins the race keeps the state: the breaker reports no trip.
    h.set_state(RuntimeState.ACTIVE)
    before = h.runtime.get()
    original = h.runtime.set_state

    def lose_race(*args: Any, **kwargs: Any) -> RuntimeSnapshot:
        original(RuntimeState.HALTED, actor="admin-race", reason="admin got there first")
        return original(*args, **kwargs)

    setattr(h.runtime, "set_state", lose_race)
    assert run_breaker(h.db, h.runtime, SETTINGS, now=now) is False
    assert h.runtime.get().state is RuntimeState.HALTED and h.runtime.get().version > before.version


def test_breaker_counts_only_llm_attempts_in_the_window(h: Harness) -> None:
    """Fixed-rule runs, old runs, and non-provider failures never inflate the failure rate."""
    now = datetime.now(UTC)
    recent = now - timedelta(minutes=3)
    for _ in range(3):  # outside the window
        add_attempt(
            h.db, h.created, started=now - timedelta(hours=2), failure_reason="provider_error"
        )
    for _ in range(4):  # no model budget: the fixed-rule path, never an LLM attempt
        add_attempt(h.db, h.created, started=recent, failure_reason="provider_error", budget=False)
    for _ in range(3):  # input or scope failures are attempts, not provider failures
        add_attempt(
            h.db,
            h.created,
            started=recent,
            failure_reason="cart is missing or customer scope does not match",
        )
    for _ in range(2):
        add_attempt(h.db, h.created, started=recent, seconds=1)
    reading = read_window(h.db, SETTINGS, now=now)
    assert (reading.attempts, reading.failures) == (5, 0)
    assert trip_reason(reading, SETTINGS) is None
    assert run_breaker(h.db, h.runtime, SETTINGS, now=now) is False


def test_breaker_trips_on_latency_and_reason_is_non_pii(h: Harness) -> None:
    now = datetime.now(UTC)
    started = now - timedelta(minutes=4)
    for seconds in [1, 2, 3, 4, 5, 6, 7, 8, 9, 100]:
        add_attempt(h.db, h.created, started=started, seconds=seconds)
    reading = read_window(h.db, SETTINGS, now=now)
    assert reading.attempts == 10 and reading.failures == 0
    assert reading.p95_seconds is not None and Decimal(59) < reading.p95_seconds < Decimal(60)
    relaxed = replace(SETTINGS, max_p95_seconds=Decimal("120"))
    assert trip_reason(reading, relaxed) is None
    reason = trip_reason(reading, SETTINGS)
    assert reason is not None and "P95" in reason
    assert run_breaker(h.db, h.runtime, SETTINGS, now=now) is True  # the reason passes validation
    assert "P95" in h.runtime.get().reason
    # Pure threshold rules.
    assert trip_reason(BreakerReading(4, 4, None), SETTINGS) is None
    assert trip_reason(BreakerReading(5, 1, None), SETTINGS) is None  # exactly 20%, not above
    assert trip_reason(BreakerReading(5, 2, None), SETTINGS) is not None
    with pytest.raises(ValueError, match="timezone"):
        read_window(h.db, SETTINGS, now=datetime.now())


def test_every_proposal_error_is_classified_for_the_breaker() -> None:
    """The breaker's failure set cannot drift: each ProposalError message is in exactly one set."""
    source = Path(proposal_module.__file__).read_text(encoding="utf-8")
    messages = set(re.findall(r'ProposalError\(\s*"([^"]+)"\s*\)', source))
    assert len(messages) >= 25  # guards against the pattern silently matching nothing
    assert PROVIDER_FAILURE_REASONS.isdisjoint(NON_PROVIDER_FAILURE_REASONS)
    classified = PROVIDER_FAILURE_REASONS | NON_PROVIDER_FAILURE_REASONS
    assert messages <= classified, sorted(messages - classified)
    assert "provider_error" in PROVIDER_FAILURE_REASONS
    # Every classified reason is still produced by the code (no dead entries).
    produced = messages | {"provider_error"}
    assert classified <= produced, sorted(classified - produced)
