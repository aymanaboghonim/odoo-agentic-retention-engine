"""Langfuse mapping (#128): OBS-4, OBS-15, the run trace, settings. Hermetic, no database.

Repeats a small capture helper: test modules never import each other.
"""

from __future__ import annotations

import base64
import uuid
from collections.abc import Iterator
from dataclasses import dataclass, replace
from typing import Any, cast

import pytest
from fastapi.testclient import TestClient
from opentelemetry.sdk._logs.export import InMemoryLogRecordExporter
from opentelemetry.sdk.metrics.export import InMemoryMetricReader
from opentelemetry.sdk.trace import ReadableSpan
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

from engine import telemetry
from engine.approval_api import create_app
from engine.auth import Principal, TokenVerifier
from engine.db import RunRepository
from engine.runs import RunState

PUBLIC = "pk-lf-" + "1" * 12  # synthetic, low-entropy
SECRET = "sk-lf-" + "2" * 12
LANGFUSE = {
    "TELEMETRY_ENABLED": "YES",
    "TELEMETRY_ENVIRONMENT": "eval",
    "LANGFUSE_OTLP_ENDPOINT": "https://us.cloud.langfuse.com/api/public/otel/",
    "LANGFUSE_PUBLIC_KEY": PUBLIC,
    "LANGFUSE_SECRET_KEY": SECRET,
    "TELEMETRY_DAILY_LANGFUSE_UNIT_CAP": "1000",
}
GRAFANA = {
    "GRAFANA_OTLP_ENDPOINT": "https://otlp-gateway-prod-us-east-2.grafana.net/otlp",
    "GRAFANA_OTLP_TOKEN": "QUJD" + "x" * 30,
    "TELEMETRY_DAILY_SPAN_CAP": "1000",
}
TOKENIZED = 'Hi {{PII_NAME_1}}, your "Lamp" cart (USD 100) is waiting.'
WITH_EMAIL = "Hi layla.haddad@example.test, your cart is waiting."


@dataclass
class Capture:
    handle: telemetry.Telemetry
    grafana: InMemorySpanExporter
    langfuse: InMemorySpanExporter

    def flush(self) -> tuple[list[ReadableSpan], list[ReadableSpan]]:
        self.handle.force_flush()
        return list(self.grafana.get_finished_spans()), list(self.langfuse.get_finished_spans())


def capture(settings: telemetry.TelemetrySettings) -> Iterator[Capture]:
    grafana, langfuse = InMemorySpanExporter(), InMemorySpanExporter()
    handle = telemetry.configure(
        settings,
        service_name="aorte-test",
        span_exporter=grafana,
        metric_reader=InMemoryMetricReader(),
        log_exporter=InMemoryLogRecordExporter(),
        langfuse_exporter=langfuse,
    )
    try:
        yield Capture(handle, grafana, langfuse)
    finally:
        handle.shutdown()


SETTINGS = telemetry.TelemetrySettings.from_env({**LANGFUSE, **GRAFANA})


@pytest.fixture
def both() -> Iterator[Capture]:
    yield from capture(SETTINGS)


@pytest.fixture
def with_text() -> Iterator[Capture]:
    yield from capture(replace(SETTINGS, eval_text=True))


def proposal_trace(run_id: uuid.UUID, *, output: str = TOKENIZED) -> None:
    with telemetry.span(
        "aorte.proposal",
        run_id=run_id,
        **{"aorte.run_id": str(run_id), "langfuse.trace.name": "aorte.run"},
    ):
        with telemetry.span(
            "aorte.llm.call",
            **{
                "gen_ai.request.model": "gpt-6-luna",
                "langfuse.observation.type": "generation",
                "gen_ai.usage.input_tokens": 10,
                "gen_ai.usage.output_tokens": 20,
                "gen_ai.usage.cost": 0.0000113,
            },
        ) as call:
            if telemetry.eval_text_enabled():
                call.set("langfuse.observation.input", '{"request": "remind {{PII_NAME_1}}"}')
                call.set("langfuse.observation.output", output)


# --- OBS-4 --------------------------------------------------------------------------------


def test_eval_text_refused_in_erp_mode() -> None:
    """OBS-4: tokenized text is for synthetic evaluation runs only."""
    ok = telemetry.TelemetrySettings.from_env({**LANGFUSE, "TELEMETRY_EVAL_TEXT": "YES"})
    assert ok.eval_text is True
    assert telemetry.TelemetrySettings.from_env(LANGFUSE).eval_text is False
    for key, value in telemetry.ERP_FLAGS:
        with pytest.raises(telemetry.TelemetryConfigError, match=f"{key}={value}"):
            telemetry.TelemetrySettings.from_env(
                {**LANGFUSE, "TELEMETRY_EVAL_TEXT": "YES", key: value}
            )
    with pytest.raises(telemetry.TelemetryConfigError, match="needs the Langfuse settings"):
        telemetry.TelemetrySettings.from_env(
            {
                "TELEMETRY_ENABLED": "YES",
                "TELEMETRY_ENVIRONMENT": "eval",
                **GRAFANA,
                "TELEMETRY_EVAL_TEXT": "YES",
            }
        )
    with pytest.raises(telemetry.TelemetryConfigError, match="TELEMETRY_EVAL_TEXT"):
        telemetry.TelemetrySettings.from_env({**LANGFUSE, "TELEMETRY_EVAL_TEXT": "yes"})


def test_text_reaches_only_langfuse_and_only_when_enabled(with_text: Capture) -> None:
    run_id = uuid.uuid4()
    proposal_trace(run_id)
    grafana, langfuse = with_text.flush()
    (call,) = [s for s in langfuse if s.name == "aorte.llm.call"]
    attributes = dict(call.attributes or {})
    assert attributes["langfuse.observation.output"] == TOKENIZED
    assert "{{PII_NAME_1}}" in str(attributes["langfuse.observation.input"])
    for item in grafana:
        assert not {k for k in (item.attributes or {}) if k.startswith("langfuse.observation.in")}
        assert "langfuse.observation.output" not in (item.attributes or {})


def test_text_with_a_contact_is_dropped_even_when_enabled(with_text: Capture) -> None:
    proposal_trace(uuid.uuid4(), output=WITH_EMAIL)
    _, langfuse = with_text.flush()
    (call,) = [s for s in langfuse if s.name == "aorte.llm.call"]
    assert "langfuse.observation.output" not in (call.attributes or {})
    assert "example.test" not in str(dict(call.attributes or {}))


def test_no_text_without_the_setting(both: Capture) -> None:
    assert telemetry.eval_text_enabled() is False
    proposal_trace(uuid.uuid4())
    for item in [*both.flush()[0], *both.flush()[1]]:
        assert "langfuse.observation.output" not in (item.attributes or {})


# --- Langfuse filter and mapping ----------------------------------------------------------


def test_langfuse_gets_run_spans_only_never_the_scan(both: Capture) -> None:
    """The once-a-minute scan would use up the Hobby units; Grafana still gets it."""
    with telemetry.span("aorte.scan"):
        pass
    proposal_trace(uuid.uuid4())
    grafana, langfuse = both.flush()
    assert {s.name for s in langfuse} == {"aorte.proposal", "aorte.llm.call"}
    assert "aorte.scan" in {s.name for s in grafana}
    (call,) = [s for s in langfuse if s.name == "aorte.llm.call"]
    attributes = dict(call.attributes or {})
    assert attributes["langfuse.observation.type"] == "generation"
    assert attributes["gen_ai.usage.cost"] == pytest.approx(0.0000113)
    assert attributes["gen_ai.request.model"] == "gpt-6-luna"


def test_run_spans_share_one_trace_named_by_the_run() -> None:
    """Proposal, approval, and execution of one run land in one trace, across processes."""
    for cap in capture(SETTINGS):
        run_id = uuid.uuid4()
        proposal_trace(run_id)
        with telemetry.span("aorte.execution", run_id=run_id, **{"aorte.run_id": str(run_id)}):
            pass
        with telemetry.span("aorte.scan"):
            pass
        grafana, _ = cap.flush()
        traces = {s.name: s.context.trace_id for s in grafana}
        assert traces["aorte.proposal"] == traces["aorte.llm.call"] == run_id.int
        assert traces["aorte.execution"] == run_id.int
        assert traces["aorte.scan"] != run_id.int


def test_langfuse_unit_cap_is_separate_from_the_grafana_cap() -> None:
    settings = replace(SETTINGS, daily_langfuse_unit_cap=1, daily_span_cap=100)
    for cap in capture(settings):
        proposal_trace(uuid.uuid4())
        grafana, langfuse = cap.flush()
        assert len(grafana) == 2 and len(langfuse) == 1


# --- OBS-15 -------------------------------------------------------------------------------


class _Verifier:
    def verify(self, token: str) -> Principal:
        return Principal("approver@example.test", frozenset({"approver"}))


class _Repo:
    def __init__(self, run_id: uuid.UUID) -> None:
        self.run_id = run_id

    def decide_approval(self, run_id: uuid.UUID, *, token: str, decision: str, actor: str) -> Any:
        return type("Run", (), {"id": run_id, "state": RunState.READY_TO_EXECUTE})()


def test_operator_decision_event_has_code_only(both: Capture) -> None:
    """OBS-15: the decision joins the run trace as an event; no operator identity or text."""
    run_id = uuid.uuid4()
    app = create_app(cast(RunRepository, _Repo(run_id)), cast(TokenVerifier, _Verifier()))
    client = TestClient(app, client=("127.0.0.1", 1234))
    response = client.post(
        f"/approvals/{run_id}/decision",
        json={"snapshot_hash": "a" * 64, "decision": "reject"},
        headers={"Authorization": "Bearer synthetic"},
    )
    assert response.status_code == 200, response.text
    _, langfuse = both.flush()
    (event,) = [s for s in langfuse if s.name == "aorte.approval"]
    assert event.context.trace_id == run_id.int
    assert dict(event.attributes or {}) == {
        "aorte.run_id": str(run_id),
        "aorte.decision": "reject",
        "langfuse.observation.type": "event",
        "langfuse.trace.name": "aorte.run",
        "aorte.trace_id": f"{run_id.int:032x}",
    }
    assert "example.test" not in str(event.attributes)


# --- settings and exporter ------------------------------------------------------------------


def test_langfuse_settings_are_all_or_none_and_never_echoed() -> None:
    settings = telemetry.TelemetrySettings.from_env(LANGFUSE)
    assert settings.langfuse and not settings.grafana
    assert settings.langfuse_endpoint == "https://us.cloud.langfuse.com/api/public/otel"
    assert PUBLIC not in repr(settings) and SECRET not in repr(settings)
    for key in telemetry.LANGFUSE_KEYS:
        with pytest.raises(telemetry.TelemetryConfigError, match=key):
            telemetry.TelemetrySettings.from_env({k: v for k, v in LANGFUSE.items() if k != key})
    for key, value in (
        ("LANGFUSE_PUBLIC_KEY", "unset"),
        ("LANGFUSE_SECRET_KEY", "key-without-prefix"),
        ("LANGFUSE_OTLP_ENDPOINT", "http://us.cloud.langfuse.com/api/public/otel"),
    ):
        with pytest.raises(telemetry.TelemetryConfigError) as error:
            telemetry.TelemetrySettings.from_env({**LANGFUSE, key: value})
        assert SECRET not in str(error.value)
    with pytest.raises(telemetry.TelemetryConfigError, match="needs the Grafana settings"):
        telemetry.TelemetrySettings.from_env(
            {"TELEMETRY_ENABLED": "YES", "TELEMETRY_ENVIRONMENT": "local"}
        )


def test_langfuse_exporter_uses_basic_auth_and_ingestion_v4() -> None:
    exporter = telemetry.langfuse_span_exporter(SETTINGS)
    expected = base64.b64encode(f"{PUBLIC}:{SECRET}".encode()).decode()
    # Internal client attribute of the pinned OTLP exporter (F-080); keys are lowercased.
    headers = dict(getattr(getattr(exporter, "_client"), "_headers"))
    assert headers["authorization"] == f"Basic {expected}"
    assert headers["x-langfuse-ingestion-version"] == "4"
    assert getattr(exporter, "_endpoint") == (
        "https://us.cloud.langfuse.com/api/public/otel/v1/traces"
    )
    exporter.shutdown()


# --- relay (#150) ---------------------------------------------------------------------------

RELAY = "r" * 43  # synthetic, low-entropy, the real length
RELAYED = {
    **LANGFUSE,
    "TELEMETRY_ENVIRONMENT": "aws-mvp",
    "LANGFUSE_OTLP_ENDPOINT": "https://d0.cloudfront.example/lf-otel/api/public/otel",
    "TELEMETRY_RELAY_KEY": RELAY,
}


def test_relay_exporter_sends_the_relay_header() -> None:
    settings = telemetry.TelemetrySettings.from_env(RELAYED)
    assert RELAY not in repr(settings)
    exporter = telemetry.langfuse_span_exporter(settings)
    headers = dict(getattr(getattr(exporter, "_client"), "_headers"))
    assert headers[telemetry.RELAY_HEADER] == RELAY
    assert headers["x-langfuse-ingestion-version"] == "4"
    assert getattr(exporter, "_endpoint") == (
        "https://d0.cloudfront.example/lf-otel/api/public/otel/v1/traces"
    )
    exporter.shutdown()


def test_no_relay_header_without_a_relay_key() -> None:
    exporter = telemetry.langfuse_span_exporter(SETTINGS)
    headers = dict(getattr(getattr(exporter, "_client"), "_headers"))
    assert telemetry.RELAY_HEADER not in headers
    exporter.shutdown()


def test_relay_key_rules_fail_closed() -> None:
    # aws-mvp has no route to Langfuse but the relay: refuse at startup.
    without = {k: v for k, v in RELAYED.items() if k != "TELEMETRY_RELAY_KEY"}
    with pytest.raises(telemetry.TelemetryConfigError, match="needs TELEMETRY_RELAY_KEY"):
        telemetry.TelemetrySettings.from_env(without)
    # The SSM placeholder is refused, and the value is never echoed.
    with pytest.raises(telemetry.TelemetryConfigError, match="placeholder") as error:
        telemetry.TelemetrySettings.from_env({**RELAYED, "TELEMETRY_RELAY_KEY": "unset"})
    assert RELAY not in str(error.value)
    # A relay key without Langfuse settings is a mistake, not ignored.
    grafana_only = {"TELEMETRY_ENABLED": "YES", "TELEMETRY_ENVIRONMENT": "aws-mvp", **GRAFANA}
    with pytest.raises(telemetry.TelemetryConfigError, match="needs the Langfuse settings"):
        telemetry.TelemetrySettings.from_env({**grafana_only, "TELEMETRY_RELAY_KEY": RELAY})
    # Grafana-only on aws-mvp (today's deployment) still works.
    assert telemetry.TelemetrySettings.from_env(grafana_only).grafana
