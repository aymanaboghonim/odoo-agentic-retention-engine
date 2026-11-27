"""OBS-1..OBS-3, OBS-5..OBS-9, OBS-13 (docs/specs/observability-telemetry.md, #126).

Hermetic: in-memory exporters only, no network. Database tests use TEST_DATABASE_URL.
OBS-10 is checked in tests/test_import_boundaries.py (BND-5).
"""

from __future__ import annotations

import json
import logging
import os
import random
import re
import uuid
from collections.abc import Iterator, Mapping, Sequence
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any, Literal, cast

import pytest
import sqlalchemy as sa
from fastapi.testclient import TestClient
from opentelemetry.sdk._logs import ReadableLogRecord
from opentelemetry.sdk._logs.export import (
    InMemoryLogRecordExporter,
    LogRecordExporter,
    LogRecordExportResult,
)
from opentelemetry.sdk.metrics.export import InMemoryMetricReader
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import Event, ReadableSpan
from opentelemetry.sdk.trace.export import SpanExporter, SpanExportResult
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
from opentelemetry.trace import SpanContext, Status, StatusCode, TraceFlags

from engine import erp_cli, telemetry
from engine.approval_api import create_app
from engine.auth import TokenVerifier
from engine.config import normalize_database_url
from engine.coupon_code import coupon_code_for_run
from engine.db import RunRepository, create_engine, upgrade_schema
from engine.execution import (
    MAILPIT_REVIEW_RECIPIENT,
    ErpExecution,
    ExecutionService,
    ExecutionSettings,
    RecipientDestination,
    TestDestination,
)
from engine.full_stack_worker import register_health_gauges, register_run_gauges
from engine.gate_ledger import InMemoryGateLedger
from engine.odoo import AbandonedCart, CartLine
from engine.policy import Gate, GateApproval, GateConfig, OfferProposal
from engine.privacy import AllowlistGuard, PrivacyRepository, PrivacySettings, subject_hash
from engine.proposal import ProposalService, ProposalSettings
from engine.proposal_budget import Rate
from engine.proposal_provider import ProviderResult
from engine.reconcile import ReconcileSettings
from engine.retrieval import SearchResult
from engine.runs import RunState
from engine.runtime_state import RuntimeGuard, RuntimeState
from engine.worker import GuardedScan, Heartbeat

URL = os.environ.get("TEST_DATABASE_URL", "")
if not URL and os.environ.get("REQUIRE_DB_TESTS") == "1":
    raise RuntimeError("REQUIRE_DB_TESTS=1 but TEST_DATABASE_URL is not set")

PEPPER = "obs-test-pepper-" + "x" * 32
NOW = datetime.now(UTC)
NAMES = ("Layla Haddad", "ليلى حداد")
EMAIL = "layla.haddad@example.test"
PHONE = "+966 50 123 4567"
RAW = (*NAMES, "Layla", "Haddad", EMAIL, PHONE, "966501234567")
SECRET = "QUJD" + "s3cr3t" * 8  # synthetic, base64-shaped (a raw glc_ token is refused)
ENABLED = telemetry.TelemetrySettings(
    enabled=True,
    environment="local",
    grafana_endpoint="https://otlp.example.test/otlp",
    grafana_token=SECRET,
    daily_span_cap=10_000,
)


# --- capture ---------------------------------------------------------------------------


@dataclass
class Capture:
    handle: telemetry.Telemetry
    spans: InMemorySpanExporter
    reader: InMemoryMetricReader
    logs: InMemoryLogRecordExporter

    def flush(self) -> None:
        self.handle.force_flush()

    def finished_spans(self) -> Sequence[ReadableSpan]:
        self.flush()
        return self.spans.get_finished_spans()

    def finished_logs(self) -> list[ReadableLogRecord]:
        self.flush()
        return list(self.logs.get_finished_logs())

    def metrics(self) -> dict[str, list[tuple[dict[str, Any], float]]]:
        found: dict[str, list[tuple[dict[str, Any], float]]] = {}
        data = self.reader.get_metrics_data()
        for resource_metrics in data.resource_metrics if data else ():
            for scope in resource_metrics.scope_metrics:
                for metric in scope.metrics:
                    for point in metric.data.data_points:
                        raw = getattr(point, "value", None)
                        value = float(getattr(point, "sum", 0) if raw is None else raw)
                        found.setdefault(metric.name, []).append(
                            (dict(point.attributes or {}), value)
                        )
        return found

    def dump(self) -> str:
        """Everything exported, serialized, for leak scans."""
        parts: list[object] = []
        for item in self.finished_spans():
            parts.append(
                {
                    "name": item.name,
                    "attributes": dict(item.attributes or {}),
                    "resource": dict(item.resource.attributes),
                    "events": [event.name for event in item.events],
                    "status": item.status.description,
                }
            )
        for record in self.finished_logs():
            parts.append(
                {
                    "body": record.log_record.body,
                    "attributes": dict(record.log_record.attributes or {}),
                    "resource": dict(record.resource.attributes),
                }
            )
        parts.append(self.metrics())
        return json.dumps(parts, default=str, ensure_ascii=False)


@pytest.fixture
def capture() -> Iterator[Capture]:
    yield from _capture(ENABLED)


def _capture(
    settings: telemetry.TelemetrySettings,
    *,
    span_exporter: SpanExporter | None = None,
    log_exporter: LogRecordExporter | None = None,
    clock: Any = None,
) -> Iterator[Capture]:
    spans, reader, logs = (
        InMemorySpanExporter(),
        InMemoryMetricReader(),
        InMemoryLogRecordExporter(),
    )
    root = logging.getLogger()
    level = root.level
    root.setLevel(logging.INFO)
    handle = telemetry.configure(
        settings,
        service_name="aorte-test",
        span_exporter=span_exporter or spans,
        metric_reader=reader,
        log_exporter=log_exporter or logs,
        clock=clock,
    )
    try:
        yield Capture(handle, spans, reader, logs)
    finally:
        handle.shutdown()
        root.setLevel(level)


# --- engine fakes (repeated from the privacy tests; test modules never import each other) ---


def make_cart(customer: int, order: int, name: str) -> AbandonedCart:
    return AbandonedCart(
        order_id=order,
        reference=f"SO{order}",
        partner_id=customer,
        partner_name=name,
        partner_email=EMAIL,
        language="en_US",
        currency="USD",
        amount_total=Decimal("100"),
        date_order=NOW - timedelta(days=3),
        lines=(CartLine(1, "Lamp", "L1", "decor", Decimal(1), Decimal("100"), Decimal("40")),),
        partner_phone=PHONE,
        email_blacklisted=False,
        abandoned_delay_hours=10.0,
    )


class Reader:
    def __init__(self, cart: AbandonedCart) -> None:
        self.cart = cart

    def get_cart(self, order_id: int) -> AbandonedCart | None:
        return self.cart if order_id == self.cart.order_id else None

    def get_abandoned_cart(self, order_id: int, *, now: datetime) -> AbandonedCart | None:
        return self.cart if order_id == self.cart.order_id else None


class Policies:
    def search(self, query: str, language: str) -> list[SearchResult]:
        return [SearchResult("consent", "1", language, f"Respect consent. {query}", "h", 0.1)]


class Provider:
    def count_tokens(self, payload: Mapping[str, object]) -> int:
        return 10

    def generate(self, payload: Mapping[str, object], output_limit: int) -> ProviderResult:
        body = {
            "action": "draft_message",
            "customer_message": "Hi {{PII_NAME_1}}, your cart is waiting.",
            "price_claim": None,
            "currency": None,
            "proposed_discount_percent": None,
        }
        return ProviderResult(json.dumps(body), 1234, 567, True)


class Coupons:
    def create(self, approval: GateApproval, proposal: OfferProposal) -> str:
        return coupon_code_for_run(proposal.run_id)


class Recipients:
    def __init__(self) -> None:
        self.sent: list[str] = []

    def send_to(
        self,
        destination: RecipientDestination,
        text: str,
        *,
        language: Literal["en", "ar"],
        message_id: str | None = None,
    ) -> str:
        self.sent.append(text)
        return f"message-{len(self.sent)}"


class Dispatcher:
    def __init__(self) -> None:
        self.sent: list[str] = []

    def send(
        self,
        destination: TestDestination,
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


RATE = Rate(Decimal("0.125"), Decimal("0.5"), "F-005")
PRIVACY = PrivacySettings(30, PEPPER)
ALLOW = AllowlistGuard(domains=frozenset({"example.test"}))


@dataclass
class Run:
    db: sa.Engine
    mode: Literal["erp", "synthetic"]
    customer: int
    order: int
    name: str
    run_id: uuid.UUID = field(default_factory=uuid.uuid4)

    def __post_init__(self) -> None:
        self.reader = Reader(make_cart(self.customer, self.order, self.name))
        self.gate = gate()

    def propose(self) -> None:
        settings = ProposalSettings(
            self.mode == "synthetic",
            Decimal("1"),
            10_000,
            10_000,
            RATE,
            None,
            self.mode == "erp",
        )
        ProposalService(self.db, self.reader, Policies(), Provider(), self.gate, settings).generate(
            self.run_id,
            customer_id=self.customer,
            cart_id=self.order,
            language="en",
            request=f"I am {self.name}, mail {EMAIL} or call {PHONE}",
            execution_channel="mailpit",
        )

    def execute(self) -> None:
        if self.mode == "erp":
            ExecutionService(
                self.db,
                self.reader,
                self.gate,
                Coupons(),
                Dispatcher(),
                ExecutionSettings(False, 1, erp_enabled=True),
                erp=ErpExecution(PRIVACY, ALLOW, Recipients(), lambda: None),
            ).execute_erp(self.run_id)
        else:
            ExecutionService(
                self.db,
                self.reader,
                self.gate,
                Coupons(),
                Dispatcher(),
                ExecutionSettings(synthetic_only=True, max_known_not_sent_attempts=1),
            ).execute(
                self.run_id,
                destination=TestDestination("mailpit", MAILPIT_REVIEW_RECIPIENT, confirmed=True),
            )


@pytest.fixture
def db() -> Iterator[sa.Engine]:
    if not URL:
        pytest.skip("TEST_DATABASE_URL not set")
    engine = create_engine(normalize_database_url(URL))
    upgrade_schema(engine)
    yield engine
    engine.dispose()


@pytest.fixture
def customers(db: sa.Engine) -> Iterator[list[int]]:
    """Every Engine DB row for these synthetic customers is removed afterwards."""
    created: list[int] = []
    yield created
    repo = PrivacyRepository(db, PRIVACY)
    for customer in created:
        repo.erase_customer(customer)
        with db.begin() as conn:
            conn.execute(
                sa.text("DELETE FROM erasure_requests WHERE subject_hash=:h"),
                {"h": subject_hash(PEPPER, str(customer))},
            )


def new_run(
    db: sa.Engine, customers: list[int], mode: Literal["erp", "synthetic"], name: str
) -> Run:
    customer = random.randint(10**8, 10**9)
    order = random.randint(10**8, 10**9)
    customers.append(customer)
    run = Run(db, mode, customer, order, name)
    if mode == "erp":
        PrivacyRepository(db, PRIVACY).set_consent(customer, "email", "opted_in", source="test")
        created, _ = RunRepository(db).create_run(
            trigger_type="erp",
            trigger_ref=f"{customer}:{order}",
            idempotency_key=f"obs-test:{uuid.uuid4()}",
            customer_id=str(customer),
            retention_days=PRIVACY.retention_days,
        )
    else:
        created, _ = RunRepository(db).create_run(
            trigger_type="synthetic",
            trigger_ref=f"{customer}:{order}",
            idempotency_key=f"obs-test:{uuid.uuid4()}",
        )
    run.run_id = created.id
    return run


def full_run(run: Run) -> None:
    run.propose()
    run.execute()
    assert RunRepository(run.db).get_run(run.run_id).state is RunState.COMPLETED


# --- OBS-1 -----------------------------------------------------------------------------


def _readable(**changes: Any) -> ReadableSpan:
    context = SpanContext(1, 2, is_remote=False, trace_flags=TraceFlags(1))
    base: dict[str, Any] = {
        "name": "aorte.proposal",
        "context": context,
        "resource": Resource({"service.name": "aorte-worker", "host.name": "ip-10-0-0-1"}),
        "attributes": {"aorte.run_id": str(uuid.uuid4())},
        "events": (Event("exception", {"exception.message": f"Hello {NAMES[0]}"}),),
        "status": Status(StatusCode.ERROR, f"failed for {EMAIL}"),
        "start_time": 1,
        "end_time": 2,
    }
    base.update(changes)
    return ReadableSpan(**base)


def test_exporter_drops_non_allowlisted_attributes(capture: Capture) -> None:
    """OBS-1: unknown keys, invalid values, events, and status text never leave."""
    run_id = str(uuid.uuid4())
    clean, dropped = telemetry.sanitize_span(
        _readable(
            attributes={
                "aorte.run_id": run_id,
                "customer_id": 42,
                "aorte.outcome": EMAIL,  # allowlisted key, invalid value
                "gen_ai.prompt": f"Hello {NAMES[0]}",
                "aorte.gate.reasons": ["discount_above_hard_cap"],
            }
        )
    )
    assert dict(clean.attributes or {}) == {
        "aorte.run_id": run_id,
        "aorte.gate.reasons": ("discount_above_hard_cap",),
        "aorte.trace_id": "0" * 31 + "1",  # #139: padded, set by the exporter
    }
    assert clean.events == () and clean.links == ()
    assert clean.status.status_code is StatusCode.ERROR and clean.status.description is None
    assert dict(clean.resource.attributes) == {"service.name": "aorte-worker"}
    assert dropped == 3 + 1  # three attributes and one event
    renamed, _ = telemetry.sanitize_span(_readable(name=f"GET /customers/{EMAIL}"))
    assert renamed.name == telemetry.OTHER_SPAN

    with telemetry.span("aorte.scan", **{"customer_id": 7, "aorte.scan.scanned": 3}) as handle:
        handle.set("partner_name", NAMES[1])
    logging.getLogger("engine.test").info("scan finished for run=%s", run_id)
    logging.getLogger("urllib3.connectionpool").info("https://api.telegram.org/bot123:abc")
    (exported,) = capture.finished_spans()
    assert dict(exported.attributes or {}) == {"aorte.scan.scanned": 3}
    assert dict(exported.resource.attributes) == {
        "service.name": "aorte-test",
        "deployment.environment.name": "local",
    }
    (log,) = capture.finished_logs()  # third-party loggers are not forwarded
    assert log.log_record.body == f"scan finished for run={run_id}"
    assert dict(log.log_record.attributes or {}) == {"aorte.logger": "engine.test"}
    drops = capture.metrics()["aorte.telemetry.dropped"]
    assert ({"aorte.signal": "attribute", "aorte.reason": "filtered"}, 2) in drops


def test_exception_message_and_stack_never_leave(capture: Capture) -> None:
    """OBS-1: a failing stage exports only `error.type` and the ERROR status."""
    with pytest.raises(ValueError):
        with telemetry.span("aorte.gate", **{"aorte.run_id": str(uuid.uuid4())}):
            raise ValueError(f"cart of {NAMES[0]} <{EMAIL}>")
    (exported,) = capture.finished_spans()
    assert exported.attributes is not None and exported.attributes["error.type"] == "ValueError"
    assert exported.events == () and exported.status.description is None
    assert NAMES[0] not in capture.dump() and EMAIL not in capture.dump()


# --- OBS-2, OBS-3 (database) -------------------------------------------------------------


def _assert_no_pii(text: str) -> None:
    lowered = text.lower()
    for value in RAW:
        assert value.lower() not in lowered, "raw PII exported"
    assert "{{pii_" not in lowered
    assert not re.search(r"[\w.+-]+@[\w-]+\.[\w.-]+", text), "email-shaped value exported"


def test_full_run_telemetry_contains_no_pii(
    capture: Capture, db: sa.Engine, customers: list[int]
) -> None:
    """OBS-2: English and Arabic names, ERP and synthetic modes, propose and execute."""
    for mode, name in (("erp", NAMES[0]), ("erp", NAMES[1]), ("synthetic", NAMES[0])):
        full_run(new_run(db, customers, mode, name))  # type: ignore[arg-type]
    names = {item.name for item in capture.finished_spans()}
    assert {
        "aorte.proposal",
        "aorte.llm.call",
        "aorte.gate",
        "aorte.execution",
        "aorte.effect",
    } <= names
    _assert_no_pii(capture.dump())


def test_customer_identifiers_are_never_exported(
    capture: Capture, db: sa.Engine, customers: list[int]
) -> None:
    """OBS-3: no customer or cart ID, destination, or its hash; the channel is not text."""
    run = new_run(db, customers, "erp", NAMES[0])
    full_run(run)
    dump = capture.dump()
    for value in (str(run.customer), str(run.order), f"SO{run.order}", MAILPIT_REVIEW_RECIPIENT):
        assert value not in dump
    exported = capture.finished_spans()
    assert str(run.run_id) in {str((s.attributes or {}).get("aorte.run_id")) for s in exported}
    keys = {key for item in exported for key in (item.attributes or {})}
    assert not {key for key in keys if "customer" in key or "cart" in key or "destination" in key}


# --- OBS-5 ---------------------------------------------------------------------------------


class BrokenSpans(SpanExporter):
    def export(self, spans: Sequence[ReadableSpan]) -> SpanExportResult:
        raise ConnectionError("collector unreachable")

    def shutdown(self) -> None:
        raise ConnectionError("collector unreachable")


class BrokenLogs(LogRecordExporter):
    def export(self, batch: Sequence[ReadableLogRecord]) -> LogRecordExportResult:
        raise ConnectionError("collector unreachable")

    def shutdown(self) -> None:
        return None

    def force_flush(self, timeout_millis: int = 30000) -> bool:
        return True


def test_exporter_failure_does_not_affect_run(db: sa.Engine, customers: list[int]) -> None:
    """OBS-5: a failing exporter changes nothing in the run and is counted."""
    for cap in _capture(ENABLED, span_exporter=BrokenSpans(), log_exporter=BrokenLogs()):
        run = new_run(db, customers, "erp", NAMES[0])
        full_run(run)
        cap.flush()
        drops = cap.metrics()["aorte.telemetry.dropped"]
        assert any(
            labels == {"aorte.signal": "span", "aorte.reason": "export_failed"} and value > 0
            for labels, value in drops
        )


def test_helpers_never_raise_without_configuration() -> None:
    """OBS-5: with telemetry off, every helper is a silent no-op."""
    with telemetry.span("aorte.scan", **{"aorte.run_id": "not-a-uuid"}) as handle:
        handle.set("anything", object())
    telemetry.count("aorte.scan.carts", 1, {"aorte.outcome": "scanned"})
    telemetry.count("no.such.metric", 1)
    telemetry.observe("aorte.stage.duration", 0.5, {"aorte.stage": "scan"})


# --- OBS-6, OBS-7 --------------------------------------------------------------------------

FULL = {
    "TELEMETRY_ENABLED": "YES",
    "TELEMETRY_ENVIRONMENT": "aws-mvp",
    "GRAFANA_OTLP_ENDPOINT": "https://otlp-gateway-prod-us-east-2.grafana.net/otlp/",
    "GRAFANA_OTLP_TOKEN": SECRET,
    "TELEMETRY_DAILY_SPAN_CAP": "20000",
}


def test_partial_telemetry_config_fails_closed() -> None:
    """OBS-6: the flag is required; with YES every setting is required and valid."""
    for env in ({}, {"TELEMETRY_ENABLED": ""}, {"TELEMETRY_ENABLED": "yes"}):
        with pytest.raises(telemetry.TelemetryConfigError, match="TELEMETRY_ENABLED"):
            telemetry.TelemetrySettings.from_env(env)
    assert telemetry.TelemetrySettings.from_env({"TELEMETRY_ENABLED": "NO"}).enabled is False
    settings = telemetry.TelemetrySettings.from_env(FULL)
    assert settings.enabled and settings.environment == "aws-mvp"
    assert settings.grafana_endpoint == "https://otlp-gateway-prod-us-east-2.grafana.net/otlp"
    for key in FULL:
        if key == "TELEMETRY_ENABLED":
            continue
        partial = {k: v for k, v in FULL.items() if k != key}
        with pytest.raises(telemetry.TelemetryConfigError, match=key):
            telemetry.TelemetrySettings.from_env(partial)
    for key, value in (
        ("TELEMETRY_ENVIRONMENT", "prod"),
        ("GRAFANA_OTLP_ENDPOINT", "http://otlp.example.test/otlp"),
        ("GRAFANA_OTLP_ENDPOINT", "https://user:pw@otlp.example.test/otlp"),
        ("GRAFANA_OTLP_ENDPOINT", "https://otlp.example.test/otlp?x=1"),
        ("GRAFANA_OTLP_TOKEN", "two words"),
        ("TELEMETRY_DAILY_SPAN_CAP", "0"),
        ("TELEMETRY_DAILY_SPAN_CAP", "many"),
    ):
        with pytest.raises(telemetry.TelemetryConfigError, match=key):
            telemetry.TelemetrySettings.from_env({**FULL, key: value})


def test_entrypoint_refuses_missing_telemetry_setting(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """OBS-6: the operator CLI stops before any work when the flag is missing."""
    assert erp_cli.main(["preflight", "--channel", "mailpit"], {}) == 1
    assert "TELEMETRY_ENABLED must be YES or NO" in capsys.readouterr().err


def test_secrets_never_in_telemetry(capture: Capture) -> None:
    """OBS-7: tokens and keys stay out of reprs, errors, attributes, and log bodies."""
    settings = telemetry.TelemetrySettings.from_env(FULL)
    assert SECRET not in repr(settings)
    with pytest.raises(telemetry.TelemetryConfigError) as error:
        telemetry.TelemetrySettings.from_env({**FULL, "GRAFANA_OTLP_TOKEN": f"{SECRET} x"})
    assert SECRET not in str(error.value)
    # Credential-shaped fakes are built at runtime and low-entropy, so the repository holds no
    # literal that a secret scanner would read as a key.
    fake_jwt = "ey" + "J" + "a" * 24
    fake_openai = "sk-" + "proj-" + "a" * 24
    fake_telegram = "1" * 9 + ":" + "A" * 35
    fake_langfuse = "pk-" + "lf-1111 " + "sk-" + "lf-2222"
    log = logging.getLogger("engine.test")
    for line in (
        f"Authorization: Basic {SECRET}",
        f"header Bearer {fake_jwt}",
        f"key {fake_openai}",
        f"bot {fake_telegram}",
        f"langfuse {fake_langfuse}",
    ):
        log.warning("request failed: %s", line)
    with telemetry.span("aorte.llm.call", **{"error.type": f"Basic {SECRET}"}):
        pass
    dump = capture.dump()
    for credential in (SECRET, fake_jwt, fake_openai, fake_telegram, "pk-lf-", "sk-lf-"):
        assert credential not in dump
    assert [record.log_record.body for record in capture.finished_logs()] == ["[redacted]"] * 5


# --- OBS-8 ---------------------------------------------------------------------------------


def test_metric_label_cardinality_is_bounded(capture: Capture) -> None:
    """OBS-8: labels come from a closed set with closed values; no per-run IDs."""
    assert not {key for key in telemetry.METRIC_LABELS if key.endswith("_id") or "id" in key}
    run_id = str(uuid.uuid4())
    telemetry.count(
        "aorte.execution.effects",
        1,
        {"aorte.effect": "coupon", "aorte.outcome": "confirmed", "aorte.run_id": run_id},
    )
    telemetry.count(
        "aorte.execution.effects", 1, {"aorte.effect": "coupon", "aorte.outcome": EMAIL}
    )
    points = capture.metrics()["aorte.execution.effects"]
    assert ({"aorte.effect": "coupon", "aorte.outcome": "confirmed"}, 1) in points
    assert ({"aorte.effect": "coupon"}, 1) in points
    assert run_id not in capture.dump() and EMAIL not in capture.dump()


def test_run_gauges_report_states_and_held_effects(capture: Capture, db: sa.Engine) -> None:
    """OBS-8: runs by state and held effects come from closed labels only."""
    register_run_gauges(RunRepository(db), ReconcileSettings(stale_started_minutes=30))
    found = capture.metrics()
    states = {labels.get("aorte.state") for labels, _ in found.get("aorte.runs", [])}
    assert states <= set(telemetry.RUN_STATES)
    assert [labels for labels, _ in found["aorte.execution.held"]] == [{}]


def test_run_states_match_the_engine() -> None:
    assert telemetry.RUN_STATES == tuple(state.value for state in RunState)


# --- OBS-9 ---------------------------------------------------------------------------------


def test_cost_metric_matches_budget_ledger(
    capture: Capture, db: sa.Engine, customers: list[int]
) -> None:
    """OBS-9: the exported estimated USD and tokens equal the settled proposal_calls row."""
    run = new_run(db, customers, "erp", NAMES[0])
    run.propose()
    with db.connect() as conn:
        row = conn.execute(
            sa.text(
                "SELECT actual_usd, input_tokens, output_tokens FROM proposal_calls "
                "WHERE run_id=:id AND status='settled'"
            ),
            {"id": run.run_id},
        ).one()
    found = capture.metrics()
    ((labels, cost),) = found["aorte.llm.cost_usd"]
    assert labels == {"gen_ai.request.model": "gpt-6-luna"}
    assert cost == float(row.actual_usd)
    tokens = {point[0]["gen_ai.token.type"]: point[1] for point in found["aorte.llm.tokens"]}
    assert tokens == {"input": row.input_tokens, "output": row.output_tokens}
    (call,) = [s for s in capture.finished_spans() if s.name == "aorte.llm.call"]
    attributes = dict(call.attributes or {})
    assert attributes["gen_ai.usage.input_tokens"] == row.input_tokens
    assert attributes["gen_ai.usage.output_tokens"] == row.output_tokens
    # #128: Langfuse maps gen_ai.usage.cost to the generation cost; same ledger value.
    assert attributes["gen_ai.usage.cost"] == float(row.actual_usd)
    assert attributes["langfuse.observation.type"] == "generation"


# --- OBS-13 --------------------------------------------------------------------------------


def test_daily_export_cap_stops_export_and_counts_drops() -> None:
    """OBS-13: at the cap, spans and logs stop until the next UTC day; drops are counted."""
    now = [datetime(2026, 11, 19, 23, 0, tzinfo=UTC)]
    settings = replace(ENABLED, daily_span_cap=3)
    for cap in _capture(settings, clock=lambda: now[0]):
        for _ in range(4):
            with telemetry.span("aorte.scan"):
                pass
        logging.getLogger("engine.test").info("after the cap")
        assert len(cap.finished_spans()) == 3
        assert cap.finished_logs() == []
        drops = dict(
            (labels["aorte.signal"], value)
            for labels, value in cap.metrics()["aorte.telemetry.dropped"]
            if labels.get("aorte.reason") == "daily_cap"
        )
        assert drops == {"span": 1, "log": 1}
        now[0] += timedelta(hours=2)  # next UTC day
        with telemetry.span("aorte.scan"):
            pass
        assert len(cap.finished_spans()) == 4


def test_configure_twice_is_refused(capture: Capture) -> None:
    with pytest.raises(telemetry.TelemetryConfigError, match="already configured"):
        telemetry.configure(ENABLED, service_name="aorte-test")


# --- health signals (#126: liveness for the Grafana alerts in #127) ------------------------


class _Clock:
    def __init__(self) -> None:
        self.now = 100.0

    def __call__(self) -> float:
        return self.now


class _Guard:
    def __init__(self, state: RuntimeState | Exception) -> None:
        self.value = state

    def get(self) -> Any:
        if isinstance(self.value, Exception):
            raise self.value
        return type("Snapshot", (), {"state": self.value})()


class _Scanner:
    def __init__(self, fail: bool = False) -> None:
        self.fail = fail

    def scan_page(self, *, now: datetime) -> tuple[int, int]:
        if self.fail:
            raise ConnectionError("odoo down")
        return 5, 1


def test_worker_heartbeat_tracks_completed_scan_pages() -> None:
    """A finished page (also a halted skip) beats; a failing page does not."""
    clock = _Clock()
    heartbeat = Heartbeat(clock)
    clock.now += 40
    assert heartbeat.age_seconds() == 40
    GuardedScan(_Scanner(), RuntimeGuard(_Guard(RuntimeState.ACTIVE)), heartbeat).scan_page(now=NOW)
    assert heartbeat.age_seconds() == 0
    clock.now += 10
    GuardedScan(_Scanner(), RuntimeGuard(_Guard(RuntimeState.HALTED)), heartbeat).scan_page(now=NOW)
    assert heartbeat.age_seconds() == 0
    clock.now += 25
    with pytest.raises(ConnectionError):
        GuardedScan(
            _Scanner(fail=True), RuntimeGuard(_Guard(RuntimeState.ACTIVE)), heartbeat
        ).scan_page(now=NOW)
    assert heartbeat.age_seconds() == 25


def test_health_gauges_report_liveness_and_runtime_state(capture: Capture) -> None:
    clock = _Clock()
    heartbeat = Heartbeat(clock)
    clock.now += 90
    register_health_gauges(heartbeat, RuntimeGuard(_Guard(RuntimeState.FALLBACK)))
    found = capture.metrics()
    assert found["aorte.worker.last_scan_age"] == [({}, 90.0)]
    assert found["aorte.runtime.state"] == [({"aorte.runtime_state": "fallback_fixed_rule"}, 1.0)]


def test_unreadable_runtime_state_reports_halted(capture: Capture) -> None:
    """KS-6: an unreadable state is reported as halted, so the alert sees it."""
    register_health_gauges(Heartbeat(), RuntimeGuard(_Guard(ConnectionError("db down"))))
    assert capture.metrics()["aorte.runtime.state"] == [({"aorte.runtime_state": "halted"}, 1.0)]


class _PingRepo:
    def __init__(self, healthy: bool) -> None:
        self.healthy = healthy

    def ping(self) -> None:
        if not self.healthy:
            raise ConnectionError(f"could not connect as engine:{SECRET}@engine-db")


def _health_client(repo: _PingRepo, host: str, secret: str | None = None) -> TestClient:
    app = create_app(
        cast(RunRepository, repo),
        cast(TokenVerifier, object()),
        origin_verify_secret=secret,
        root_path="/api",
    )
    return TestClient(app, client=(host, 1234))


def test_health_endpoint_is_loopback_only_and_reveals_nothing() -> None:
    """Liveness for the container health check: no auth, no data, never via the edge."""
    for secret in (None, "origin-secret"):
        ok = _health_client(_PingRepo(True), "127.0.0.1", secret).get("/healthz")
        assert ok.status_code == 200 and ok.json() == {"status": "ok"}
        assert ok.headers["cache-control"] == "no-store"
        down = _health_client(_PingRepo(False), "::1", secret).get("/healthz")
        assert down.status_code == 503 and down.json() == {"status": "unavailable"}
        assert SECRET not in down.text
        remote = _health_client(_PingRepo(True), "10.0.0.7", secret)
        assert (
            remote.get("/healthz", headers={"x-origin-verify": "origin-secret"}).status_code == 404
        )
        # The edge forwards only /api/*: the prefixed path does not exist.
        assert _health_client(_PingRepo(True), "127.0.0.1").get("/api/healthz").status_code == 404


def test_run_spans_carry_the_padded_trace_id_for_the_langfuse_link() -> None:
    """#139: Tempo search drops leading zeros; the exporter adds the full ID to run spans."""
    forged = "f" * 32
    clean, _ = telemetry.sanitize_span(
        _readable(name="aorte.llm.call", attributes={"aorte.trace_id": forged})
    )
    assert dict(clean.attributes or {})["aorte.trace_id"] == f"{1:032x}"
    scan, _ = telemetry.sanitize_span(_readable(name="aorte.scan", attributes={}))
    assert "aorte.trace_id" not in dict(scan.attributes or {})


def test_every_process_reports_up_for_peer_presence(capture: Capture) -> None:
    """#163: a running process reports aorte.process.up=1; the alert reads absence as stopped."""
    assert capture.metrics()["aorte.process.up"] == [({}, 1.0)]
