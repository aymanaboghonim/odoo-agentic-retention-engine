"""PII-safe OpenTelemetry for the engine (#126, ADR-0029, spec observability-telemetry.md).

Deny by default. Every span, log record, and resource is rebuilt before export from keys
in an allowlist, and each kept value must pass its validator (UUID, enum, integer, or a
strict pattern), so free text such as a name, an email, a message, or a cart line cannot
leave. Exception events and status descriptions are dropped. Metric labels come from a
closed set and are validated when recorded. Log bodies pass the PII and secret scanner or
are replaced by `[redacted]`.

Telemetry never changes a run (OBS-5): helpers swallow their own errors, exports run in
bounded background queues, and a failed export is dropped. A daily cap per process stops
span and log export for the rest of the UTC day (OBS-13); drops are counted.

This module imports no other engine module (OBS-10 keeps the gate and the LLM adapter from
importing it).
"""

from __future__ import annotations

import base64
import logging
import re
import threading
import uuid
from collections.abc import Callable, Iterable, Iterator, Mapping, Sequence
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field
from datetime import UTC, date, datetime
from typing import Any, Literal, cast
from urllib.parse import urlsplit

from opentelemetry import trace
from opentelemetry._logs import LogRecord, SeverityNumber
from opentelemetry.metrics import CallbackOptions, Meter, NoOpMeter, Observation
from opentelemetry.sdk._logs import LoggerProvider, ReadableLogRecord
from opentelemetry.sdk._logs.export import (
    BatchLogRecordProcessor,
    LogRecordExporter,
    LogRecordExportResult,
)
from opentelemetry.sdk.metrics import MeterProvider
from opentelemetry.sdk.metrics.export import (
    MetricExporter,
    MetricReader,
    PeriodicExportingMetricReader,
)
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import ReadableSpan, TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor, SpanExporter, SpanExportResult
from opentelemetry.sdk.trace.id_generator import RandomIdGenerator
from opentelemetry.trace import Status, StatusCode

LOG = logging.getLogger(__name__)

Environment = Literal["local", "eval", "aws-mvp"]
ENVIRONMENTS: frozenset[str] = frozenset({"local", "eval", "aws-mvp"})

# ---------------------------------------------------------------- validators

Validator = Callable[[object], bool]

_UUID = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}")


def _uuid(value: object) -> bool:
    return isinstance(value, str) and _UUID.fullmatch(value) is not None


def _enum(*allowed: str) -> Validator:
    values = frozenset(allowed)
    return lambda value: isinstance(value, str) and value in values


def _pattern(regex: str) -> Validator:
    compiled = re.compile(regex)
    return lambda value: isinstance(value, str) and compiled.fullmatch(value) is not None


def _count(value: object) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and 0 <= value <= 10**12


def _int_range(low: int, high: int) -> Validator:
    def check(value: object) -> bool:
        return isinstance(value, int) and not isinstance(value, bool) and low <= value <= high

    return check


def _sql(value: object) -> bool:
    """#134, OBS-12: the statement shape only. Parameters are never captured; a literal that
    looks like a contact or a secret drops the whole value."""
    return isinstance(value, str) and 0 < len(value) <= 4000 and not contains_sensitive(value)


def _amount(value: object) -> bool:
    return (
        isinstance(value, (int, float))
        and not isinstance(value, bool)
        and value == value  # not NaN
        and 0 <= value <= 10**6
    )


def _codes(value: object) -> bool:
    """A short tuple of snake_case codes (for example gate reasons)."""
    return (
        isinstance(value, (tuple, list))
        and len(value) <= 16
        and all(isinstance(item, str) and _CODE.fullmatch(item) for item in value)
    )


_CODE = re.compile(r"[a-z][a-z0-9_]{0,47}")

# Kept in step with engine.runs.RunState by test_run_states_match_the_engine.
RUN_STATES = (
    "received",
    "validating_input",
    "loading_context",
    "generating_proposal",
    "evaluating_policy",
    "awaiting_approval",
    "ready_to_execute",
    "executing",
    "completed",
    "rejected",
    "cancelled",
    "failed",
)
STAGES = ("scan", "proposal", "llm_call", "gate", "execution", "effect")
OUTCOMES = (
    "completed",
    "failed",
    "rejected",
    "held",
    "validated",
    "skipped",
    "confirmed",
    "not_sent",
    "unknown",
    "reject",
    "needs_approval",
    "auto_approve",
    "scanned",
    "created",
)

# ADR-0033: the long-running containers of the core task (deploy/terraform/mvp/ecs.tf). Their
# names are the only values of the container label; any other container is not reported.
CONTAINERS = (
    "odoo-db",
    "odoo",
    "engine-db",
    "worker",
    "approval-api",
    "mailpit",
    "operator",
    "alloy",
)
HTTP_METHODS = ("GET", "POST", "PUT", "PATCH", "DELETE", "HEAD", "OPTIONS")
# #134: instrumentation span names are kept only in these shapes: an HTTP method with an
# optional route template (never a raw path), or an SQL keyword with an optional database name.
APM_SPAN_NAME = re.compile(
    r"(?:(?:GET|POST|PUT|PATCH|DELETE|HEAD|OPTIONS)(?: /[A-Za-z0-9_{}./-]{0,120})?"
    r"|(?i:select|insert|update|delete|with|begin|commit|rollback|set|show|create|alter|drop"
    r"|truncate|lock|connect)(?: [a-z][a-z0-9_]{0,62})?)"
)
APM_SCOPE_PREFIX = "opentelemetry.instrumentation."


SPAN_ATTRIBUTES: Mapping[str, Validator] = {
    "aorte.run_id": _uuid,
    "aorte.proposal_id": _uuid,
    "aorte.stage": _enum(*STAGES),
    "aorte.state": _enum(*RUN_STATES),
    "aorte.outcome": _enum(*OUTCOMES),
    "aorte.effect": _enum("coupon", "message"),
    "aorte.channel": _enum("telegram", "mailpit"),
    "aorte.scan_mode": _enum("synthetic", "erp"),
    "aorte.trigger_type": _enum("synthetic", "erp"),
    "aorte.runtime_state": _enum("active", "fallback_fixed_rule", "halted"),
    "aorte.schema_version": _pattern(r"[a-z0-9][a-z0-9.-]{0,63}"),
    "aorte.attempt": _count,
    "aorte.scan.scanned": _count,
    "aorte.scan.created": _count,
    "aorte.gate.outcome": _enum("reject", "needs_approval", "auto_approve"),
    "aorte.gate.reasons": _codes,
    "aorte.llm.cost_usd": _amount,
    "gen_ai.operation.name": _enum("chat"),
    "gen_ai.provider.name": _enum("openai"),
    "gen_ai.request.model": _pattern(r"[a-z0-9][a-z0-9._-]{0,63}"),
    "gen_ai.usage.input_tokens": _count,
    "gen_ai.usage.output_tokens": _count,
    "error.type": _pattern(r"[A-Za-z_][A-Za-z0-9_]{0,63}"),
    # #128: Langfuse mapping (F-078) and the operator decision event (codes only).
    "aorte.decision": _enum("approve", "reject"),
    "langfuse.observation.type": _enum("span", "generation", "event"),
    "langfuse.trace.name": _pattern(r"aorte\.[a-z_]{1,40}"),
    "gen_ai.usage.cost": _amount,
    # #139: the full 32-hex trace ID, added by the exporter to run spans. Tempo search drops
    # leading zeros from trace IDs, so the Grafana-to-Langfuse link uses this value.
    "aorte.trace_id": _pattern(r"[0-9a-f]{32}"),
    # #134: APM, stable semantic conventions only (OTEL_SEMCONV_STABILITY_OPT_IN). Never the
    # full URL, path, query, headers, client address, or SQL parameters (OBS-12).
    "http.request.method": _enum(*HTTP_METHODS, "_OTHER"),
    "http.response.status_code": _int_range(100, 599),
    "http.route": _pattern(r"/[A-Za-z0-9_{}./-]{0,120}"),
    "url.scheme": _enum("http", "https"),
    "server.address": _pattern(r"[A-Za-z0-9][A-Za-z0-9.:-]{0,252}"),
    "server.port": _int_range(1, 65535),
    "db.system.name": _enum("postgresql"),
    "db.namespace": _pattern(r"[a-z][a-z0-9_]{0,62}"),
    "db.query.text": _sql,
}

RESOURCE_ATTRIBUTES: Mapping[str, Validator] = {
    "service.name": _pattern(r"aorte-[a-z][a-z-]{0,39}"),
    "service.version": _pattern(r"[0-9a-f]{7,40}|\d{1,4}\.\d{1,4}\.\d{1,4}"),
    "deployment.environment.name": _enum(*ENVIRONMENTS),
    "telemetry.sdk.language": _enum("python"),
    "telemetry.sdk.name": _enum("opentelemetry"),
    "telemetry.sdk.version": _pattern(r"\d{1,4}\.\d{1,4}\.\d{1,4}"),
}

LOG_ATTRIBUTES: Mapping[str, Validator] = {
    "aorte.logger": _pattern(r"(engine|root)(\.[a-z_][a-z0-9_]{0,40}){0,4}"),
}

SPAN_NAMES = frozenset(
    {
        "aorte.scan",
        "aorte.proposal",
        "aorte.llm.call",
        "aorte.gate",
        "aorte.execution",
        "aorte.effect",
        "aorte.approval",
        "aorte.check",
    }
)
OTHER_SPAN = "aorte.other"
# Only these reach Langfuse: one trace per run, never the once-a-minute scan (Hobby units).
LANGFUSE_SPANS = frozenset(
    {
        "aorte.proposal",
        "aorte.llm.call",
        "aorte.gate",
        "aorte.approval",
        "aorte.execution",
        "aorte.effect",
        "aorte.check",
    }
)

# Metric labels: a closed set with closed values (OBS-8). No run or proposal IDs.
METRIC_LABELS: Mapping[str, Validator] = {
    "aorte.stage": _enum(*STAGES),
    "aorte.state": _enum(*RUN_STATES),
    "aorte.outcome": _enum(*OUTCOMES),
    "aorte.effect": _enum("coupon", "message"),
    "aorte.channel": _enum("telegram", "mailpit"),
    "aorte.runtime_state": _enum("active", "fallback_fixed_rule", "halted"),
    "aorte.signal": _enum("span", "log", "attribute", "label", "langfuse"),
    "aorte.reason": _enum("daily_cap", "filtered", "export_failed"),
    "gen_ai.request.model": _pattern(r"[a-z0-9][a-z0-9._-]{0,63}"),
    "gen_ai.token.type": _enum("input", "output"),
    "aorte.container": _enum(*CONTAINERS),
    "aorte.direction": _enum("rx", "tx"),
    # #132, ADR-0030: engine.reconcile.RESOLUTIONS (repeated here: telemetry is a leaf).
    "aorte.resolution": _enum(
        "completed",
        "message_not_sent",
        "coupon_not_created",
        "unverified",
        "decision_rejected",
        "coupon_deactivated",
    ),
}

# name -> (kind, unit, description)
METRICS: Mapping[str, tuple[str, str, str]] = {
    "aorte.scan.carts": ("counter", "{cart}", "Carts scanned or turned into runs"),
    "aorte.llm.tokens": ("counter", "{token}", "Provider-reported LLM tokens"),
    "aorte.llm.cost_usd": ("counter_float", "USD", "Estimated LLM cost (settled ledger)"),
    "aorte.execution.effects": ("counter", "{effect}", "Execution effect outcomes"),
    "aorte.stage.duration": ("histogram", "s", "Stage duration"),
    "aorte.telemetry.dropped": ("counter", "{item}", "Telemetry items not exported"),
    "aorte.reconcile.resolutions": (
        "counter",
        "{resolution}",
        "Held runs resolved by the reconcile stage, by result (#132, ADR-0030)",
    ),
    "aorte.telemetry.check": (
        "counter",
        "{check}",
        "Live export checks (scripts/telemetry_check.py)",
    ),
}

# ---------------------------------------------------------------- PII and secret scanner

_EMAIL = re.compile(r"[\w.+-]+@[\w-]+(\.[\w-]+)+")
_PHONE = re.compile(r"(?:\+?[\d٠-٩۰-۹][\s().-]*){7,}")
_SECRET = re.compile(
    r"(?i)\b(basic|bearer)\s+\S+|sk-[A-Za-z0-9_-]{8,}|pk-lf-|sk-lf-|\d{6,}:[A-Za-z0-9_-]{30,}"
)
_TOKEN_TEXT = re.compile(r"\{\{\s*PII_|\{\{PII")


def contains_sensitive(text: str) -> bool:
    """True if text looks like an email, a phone, a PII token, or a credential.

    UUIDs are removed first so a run ID's digits are not read as a phone number.
    """
    stripped = _UUID.sub("", text.lower())
    return bool(
        _EMAIL.search(stripped)
        or _PHONE.search(stripped)
        or _TOKEN_TEXT.search(text)
        or _SECRET.search(text)
    )


def _eval_text(value: object) -> bool:
    """Tokenized prompt or output (OBS-4): PII tokens may stay; no contact or credential."""
    if not isinstance(value, str) or not 0 < len(value) <= 8000:
        return False
    stripped = _UUID.sub("", value.lower())
    return not (_EMAIL.search(stripped) or _PHONE.search(stripped) or _SECRET.search(value))


# Exported only by the Langfuse exporter, and only with TELEMETRY_EVAL_TEXT=YES.
TEXT_ATTRIBUTES: Mapping[str, Validator] = {
    "langfuse.observation.input": _eval_text,
    "langfuse.observation.output": _eval_text,
}


def filter_attributes(
    attributes: Mapping[str, Any] | None, allowed: Mapping[str, Validator]
) -> tuple[dict[str, Any], int]:
    """Keep allowlisted keys whose values pass their validator; return the dropped count."""
    kept: dict[str, Any] = {}
    dropped = 0
    for key, value in (attributes or {}).items():
        check = allowed.get(key)
        if check is not None and check(value):
            kept[key] = tuple(value) if isinstance(value, list) else value
        else:
            dropped += 1
    return kept, dropped


# ---------------------------------------------------------------- settings


class TelemetryConfigError(ValueError):
    """Telemetry settings are missing or invalid; startup must stop (OBS-6)."""


ERP_FLAGS = (("SCAN_MODE", "erp"), ("PROPOSAL_ERP_MODE", "YES"), ("EXECUTION_ERP_MODE", "YES"))
GRAFANA_KEYS = ("GRAFANA_OTLP_ENDPOINT", "GRAFANA_OTLP_TOKEN", "TELEMETRY_DAILY_SPAN_CAP")
LANGFUSE_KEYS = (
    "LANGFUSE_OTLP_ENDPOINT",
    "LANGFUSE_PUBLIC_KEY",
    "LANGFUSE_SECRET_KEY",
    "TELEMETRY_DAILY_LANGFUSE_UNIT_CAP",
)

# #129 / #150: the IPv6-only AWS host reaches Langfuse only through the CloudFront relay
# /lf-otel/*, which needs this header (deploy/terraform/mvp/templates/lf_relay.js).
RELAY_KEY = "TELEMETRY_RELAY_KEY"
RELAY_HEADER = "x-aorte-relay-key"


def _https_endpoint(env: Mapping[str, str], key: str) -> str:
    endpoint = env.get(key, "").strip().rstrip("/")
    parts = urlsplit(endpoint)
    if (
        parts.scheme != "https"
        or not parts.hostname
        or parts.username
        or parts.password
        or parts.query
        or parts.fragment
    ):
        raise TelemetryConfigError(f"{key} must be an https URL without credentials or query")
    return endpoint


def _cap(env: Mapping[str, str], key: str) -> int:
    try:
        value = int(env[key])
    except (KeyError, ValueError) as error:
        raise TelemetryConfigError(f"{key} must be an integer") from error
    if not 1 <= value <= 10_000_000:
        raise TelemetryConfigError(f"{key} must be between 1 and 10000000")
    return value


def _secret(env: Mapping[str, str], key: str) -> str:
    """A credential: required, no whitespace, not the SSM placeholder; never echoed (OBS-7)."""
    value = env.get(key, "").strip()
    if not value or any(ch.isspace() for ch in value):
        raise TelemetryConfigError(f"{key} must be set")
    if value == "unset":
        # The Terraform placeholder (ssm.tf); the real value is written outside Terraform.
        raise TelemetryConfigError(
            f"{key} is the SSM placeholder; write it with aws_mvp_secrets.sh"
        )
    return value


def _given(env: Mapping[str, str], keys: Sequence[str]) -> bool:
    """All-or-none group: True when any key is set; the caller then validates every key."""
    return any(env.get(key, "").strip() for key in keys)


@dataclass(frozen=True)
class TelemetrySettings:
    enabled: bool
    environment: Environment | None = None
    grafana_endpoint: str | None = None
    grafana_token: str | None = field(default=None, repr=False)
    daily_span_cap: int | None = None
    langfuse_endpoint: str | None = None
    langfuse_public_key: str | None = field(default=None, repr=False)
    langfuse_secret_key: str | None = field(default=None, repr=False)
    daily_langfuse_unit_cap: int | None = None
    relay_key: str | None = field(default=None, repr=False)
    eval_text: bool = False
    apm: bool = False
    daily_apm_span_cap: int | None = None

    @property
    def grafana(self) -> bool:
        return bool(self.grafana_endpoint and self.grafana_token)

    @property
    def langfuse(self) -> bool:
        return bool(
            self.langfuse_endpoint and self.langfuse_public_key and self.langfuse_secret_key
        )

    @classmethod
    def from_env(cls, env: Mapping[str, str]) -> TelemetrySettings:
        flag = env.get("TELEMETRY_ENABLED", "").strip()
        if flag not in {"YES", "NO"}:
            raise TelemetryConfigError("TELEMETRY_ENABLED must be YES or NO")
        if flag == "NO":
            return cls(enabled=False)
        environment = env.get("TELEMETRY_ENVIRONMENT", "").strip()
        if environment not in ENVIRONMENTS:
            raise TelemetryConfigError("TELEMETRY_ENVIRONMENT must be local, eval, or aws-mvp")
        values: dict[str, Any] = {}
        if _given(env, GRAFANA_KEYS):
            endpoint = _https_endpoint(env, "GRAFANA_OTLP_ENDPOINT")
            token = _secret(env, "GRAFANA_OTLP_TOKEN")
            if token.startswith("glc_"):
                # OTLP Basic auth needs base64("<instance-id>:<token>"), the value after "Basic%20".
                raise TelemetryConfigError(
                    "GRAFANA_OTLP_TOKEN must be the base64 of <instance-id>:<token>, "
                    "not the raw token"
                )
            values.update(
                grafana_endpoint=endpoint,
                grafana_token=token,
                daily_span_cap=_cap(env, "TELEMETRY_DAILY_SPAN_CAP"),
            )
        if _given(env, LANGFUSE_KEYS):
            public = _secret(env, "LANGFUSE_PUBLIC_KEY")
            secret = _secret(env, "LANGFUSE_SECRET_KEY")
            if not public.startswith("pk-lf-") or not secret.startswith("sk-lf-"):
                raise TelemetryConfigError(
                    "LANGFUSE_PUBLIC_KEY and LANGFUSE_SECRET_KEY must be Langfuse project keys"
                )
            values.update(
                langfuse_endpoint=_https_endpoint(env, "LANGFUSE_OTLP_ENDPOINT"),
                langfuse_public_key=public,
                langfuse_secret_key=secret,
                daily_langfuse_unit_cap=_cap(env, "TELEMETRY_DAILY_LANGFUSE_UNIT_CAP"),
            )
        if env.get(RELAY_KEY, "").strip():
            if "langfuse_endpoint" not in values:
                raise TelemetryConfigError(f"{RELAY_KEY} needs the Langfuse settings")
            values["relay_key"] = _secret(env, RELAY_KEY)
        elif environment == "aws-mvp" and "langfuse_endpoint" in values:
            # The host has no route to Langfuse but the relay; fail at startup, not per export.
            raise TelemetryConfigError(f"Langfuse on aws-mvp needs {RELAY_KEY} (the relay)")
        if not values:
            raise TelemetryConfigError(
                "TELEMETRY_ENABLED=YES needs the Grafana settings (GRAFANA_OTLP_ENDPOINT, "
                "GRAFANA_OTLP_TOKEN, TELEMETRY_DAILY_SPAN_CAP) or the Langfuse settings"
            )
        text = env.get("TELEMETRY_EVAL_TEXT", "NO").strip() or "NO"
        if text not in {"YES", "NO"}:
            raise TelemetryConfigError("TELEMETRY_EVAL_TEXT must be YES or NO")
        if text == "YES":
            # OBS-4: tokenized text goes to Langfuse only from synthetic evaluation runs.
            if "langfuse_endpoint" not in values:
                raise TelemetryConfigError("TELEMETRY_EVAL_TEXT=YES needs the Langfuse settings")
            for key, value in ERP_FLAGS:
                if env.get(key, "").strip() == value:
                    raise TelemetryConfigError(
                        f"TELEMETRY_EVAL_TEXT=YES is refused with {key}={value} (OBS-4)"
                    )
        apm = env.get("TELEMETRY_APM", "NO").strip() or "NO"
        if apm not in {"YES", "NO"}:
            raise TelemetryConfigError("TELEMETRY_APM must be YES or NO")
        if apm == "YES":
            # #134: APM goes to Grafana only, under its own daily cap (OBS-13).
            if "grafana_endpoint" not in values:
                raise TelemetryConfigError("TELEMETRY_APM=YES needs the Grafana settings")
            values["daily_apm_span_cap"] = _cap(env, "TELEMETRY_DAILY_APM_SPAN_CAP")
        return cls(
            enabled=True,
            environment=cast(Environment, environment),
            eval_text=text == "YES",
            apm=apm == "YES",
            **values,
        )


# ---------------------------------------------------------------- caps and drops


class DailyCap:
    """Thread-safe count of exported items per UTC day for one process (OBS-13)."""

    def __init__(self, limit: int, clock: Callable[[], datetime] | None = None) -> None:
        if limit < 1:
            raise ValueError("cap must be positive")
        self._limit = limit
        self._clock = clock or (lambda: datetime.now(UTC))
        self._day: date | None = None
        self._used = 0
        self._lock = threading.Lock()

    def take(self) -> bool:
        with self._lock:
            today = self._clock().astimezone(UTC).date()
            if today != self._day:
                self._day, self._used = today, 0
            if self._used >= self._limit:
                return False
            self._used += 1
            return True


# ---------------------------------------------------------------- state


@dataclass
class _State:
    tracer: trace.Tracer = field(default_factory=trace.NoOpTracer)
    meter: Meter = field(default_factory=lambda: NoOpMeter("engine"))
    instruments: dict[str, Any] = field(default_factory=dict)
    handle: Telemetry | None = None
    eval_text: bool = False


_STATE = _State()
_LOCK = threading.Lock()


def _instrument(name: str) -> Any:
    instrument = _STATE.instruments.get(name)
    if instrument is not None:
        return instrument
    kind, unit, description = METRICS[name]
    meter = _STATE.meter
    if kind == "histogram":
        instrument = meter.create_histogram(name, unit=unit, description=description)
    else:
        instrument = meter.create_counter(name, unit=unit, description=description)
    _STATE.instruments[name] = instrument
    return instrument


def _labels(labels: Mapping[str, object]) -> dict[str, Any]:
    kept, dropped = filter_attributes(labels, METRIC_LABELS)
    if dropped:
        _drop("label", "filtered", dropped)
    return kept


def _drop(signal: str, reason: str, count: int = 1) -> None:
    try:
        _instrument("aorte.telemetry.dropped").add(
            count, {"aorte.signal": signal, "aorte.reason": reason}
        )
    except Exception:  # noqa: BLE001 - telemetry never fails a run (OBS-5)
        pass


def count(name: str, value: int | float = 1, labels: Mapping[str, object] | None = None) -> None:
    """Add to a counter from METRICS with validated labels (OBS-8). Never raises."""
    try:
        if value < 0:
            return
        _instrument(name).add(value, _labels(labels or {}))
    except Exception:  # noqa: BLE001
        LOG.debug("telemetry count failed")


def observe(name: str, value: float, labels: Mapping[str, object] | None = None) -> None:
    """Record a histogram value from METRICS. Never raises."""
    try:
        _instrument(name).record(value, _labels(labels or {}))
    except Exception:  # noqa: BLE001
        LOG.debug("telemetry observe failed")


class SpanHandle:
    """A thin wrapper so callers set attributes without touching the OTel API."""

    def __init__(self, span: trace.Span) -> None:
        self._span = span

    def set(self, key: str, value: object) -> None:
        try:
            if value is not None:
                self._span.set_attribute(key, value)  # type: ignore[arg-type]
        except Exception:  # noqa: BLE001
            pass


@contextmanager
def span(name: str, *, run_id: object = None, **attributes: object) -> Iterator[SpanHandle]:
    """Start a span; on an exception set `error.type` and ERROR status, never the message.

    With `run_id` and no current span, the span starts the run's own trace: its trace ID is
    the run UUID, so the proposal, approval, and execution spans of one run share a trace
    even when different processes emit them (#128).
    """
    token = None
    try:
        if run_id is not None and not trace.get_current_span().get_span_context().is_valid:
            token = _RUN_TRACE.set(uuid.UUID(str(run_id)).int)
        cm = _STATE.tracer.start_as_current_span(
            name, record_exception=False, set_status_on_exception=False
        )
        otel_span = cm.__enter__()
    except Exception:  # noqa: BLE001
        otel_span = None
    finally:
        if token is not None:
            _RUN_TRACE.reset(token)
    if otel_span is None:
        yield SpanHandle(trace.INVALID_SPAN)
        return
    handle = SpanHandle(otel_span)
    for key, value in attributes.items():
        handle.set(key, value)
    try:
        yield handle
    except BaseException as error:
        handle.set("error.type", type(error).__name__)
        try:
            otel_span.set_status(Status(StatusCode.ERROR))
        except Exception:  # noqa: BLE001
            pass
        cm.__exit__(None, None, None)
        raise
    cm.__exit__(None, None, None)


def observable_gauge(
    name: str,
    unit: str,
    description: str,
    read: Callable[[], Iterable[tuple[int | float, Mapping[str, object]]]],
) -> None:
    """Register a gauge read at export time (for example runs by state). Never raises."""

    def callback(_options: CallbackOptions) -> Iterable[Observation]:
        try:
            return [Observation(value, _labels(labels)) for value, labels in read()]
        except Exception:  # noqa: BLE001
            return []

    try:
        _STATE.meter.create_observable_gauge(
            name, callbacks=[callback], unit=unit, description=description
        )
    except Exception:  # noqa: BLE001
        LOG.debug("telemetry gauge registration failed")


def observable_counter(
    name: str,
    unit: str,
    description: str,
    read: Callable[[], Iterable[tuple[int | float, Mapping[str, object]]]],
) -> None:
    """Register a cumulative counter read at export time (for example CPU seconds). Never raises."""

    def callback(_options: CallbackOptions) -> Iterable[Observation]:
        try:
            return [Observation(value, _labels(labels)) for value, labels in read()]
        except Exception:  # noqa: BLE001
            return []

    try:
        _STATE.meter.create_observable_counter(
            name, callbacks=[callback], unit=unit, description=description
        )
    except Exception:  # noqa: BLE001
        LOG.debug("telemetry counter registration failed")


# ---------------------------------------------------------------- safe exporters


_RUN_TRACE: ContextVar[int | None] = ContextVar("aorte_run_trace", default=None)


class RunTraceIds(RandomIdGenerator):
    """Use the run UUID as the trace ID while `span(run_id=...)` starts a run's root span."""

    def generate_trace_id(self) -> int:
        value = _RUN_TRACE.get()
        return value if value else super().generate_trace_id()


def eval_text_enabled() -> bool:
    """True only with TELEMETRY_EVAL_TEXT=YES and Langfuse configured (OBS-4)."""
    return _STATE.eval_text


def _safe_resource(resource: Resource) -> Resource:
    kept, _ = filter_attributes(resource.attributes, RESOURCE_ATTRIBUTES)
    return Resource(kept)


def is_apm(span: ReadableSpan) -> bool:
    scope = span.instrumentation_scope
    return scope is not None and scope.name.startswith(APM_SCOPE_PREFIX)


def sanitize_span(span: ReadableSpan, *, text: bool = False) -> tuple[ReadableSpan, int]:
    allowed = {**SPAN_ATTRIBUTES, **TEXT_ATTRIBUTES} if text else SPAN_ATTRIBUTES
    attributes, dropped = filter_attributes(span.attributes, allowed)
    if is_apm(span):
        name = span.name if APM_SPAN_NAME.fullmatch(span.name) else OTHER_SPAN
    else:
        name = span.name if span.name in SPAN_NAMES else OTHER_SPAN
    if name in LANGFUSE_SPANS and span.context is not None:
        # #139: set by the exporter, never by a caller (a caller's value was filtered above
        # only if it was not 32 hex; overwrite it either way).
        attributes = {**attributes, "aorte.trace_id": f"{span.context.trace_id:032x}"}
    safe = ReadableSpan(
        name=name,
        context=span.context,
        parent=span.parent,
        resource=_safe_resource(span.resource),
        attributes=attributes,
        events=(),
        links=(),
        kind=span.kind,
        status=Status(span.status.status_code),
        start_time=span.start_time,
        end_time=span.end_time,
        instrumentation_scope=span.instrumentation_scope,
    )
    return safe, dropped + len(span.events) + len(span.links)


class SafeSpanExporter(SpanExporter):
    """Cap, rebuild, then export spans; a failing exporter never raises (OBS-1, OBS-5)."""

    def __init__(
        self,
        inner: SpanExporter,
        cap: DailyCap,
        *,
        names: frozenset[str] | None = None,
        text: bool = False,
        signal: str = "span",
        apm_cap: DailyCap | None = None,
    ) -> None:
        self._inner = inner
        self._cap = cap
        self._apm_cap = apm_cap  # #134: APM spans have their own cap; None drops them
        self._names = names  # None: every span; else only these names (Langfuse)
        self._text = text  # OBS-4: tokenized text only on the Langfuse exporter, when enabled
        self._signal = signal

    def export(self, spans: Sequence[ReadableSpan]) -> SpanExportResult:
        safe: list[ReadableSpan] = []
        for item in spans:
            if self._names is not None and item.name not in self._names:
                continue
            if is_apm(item):
                if self._apm_cap is None:
                    _drop(self._signal, "filtered")
                    continue
                if not self._apm_cap.take():
                    _drop(self._signal, "daily_cap")
                    continue
            elif not self._cap.take():
                _drop(self._signal, "daily_cap")
                continue
            clean, dropped = sanitize_span(item, text=self._text)
            if dropped:
                _drop("attribute", "filtered", dropped)
            safe.append(clean)
        if not safe:
            return SpanExportResult.SUCCESS
        try:
            return self._inner.export(safe)
        except Exception:  # noqa: BLE001
            _drop(self._signal, "export_failed", len(safe))
            return SpanExportResult.FAILURE

    def shutdown(self) -> None:
        try:
            self._inner.shutdown()
        except Exception:  # noqa: BLE001
            pass

    def force_flush(self, timeout_millis: int = 30000) -> bool:
        try:
            return self._inner.force_flush(timeout_millis)
        except Exception:  # noqa: BLE001
            return False


class SafeLogExporter(LogRecordExporter):
    """Cap and rebuild log records: allowlisted attributes, scanned body (OBS-1, OBS-7)."""

    def __init__(self, inner: LogRecordExporter, cap: DailyCap) -> None:
        self._inner = inner
        self._cap = cap

    def export(self, batch: Sequence[ReadableLogRecord]) -> LogRecordExportResult:
        safe: list[ReadableLogRecord] = []
        for item in batch:
            if not self._cap.take():
                _drop("log", "daily_cap")
                continue
            record = item.log_record
            body = record.body if isinstance(record.body, str) else ""
            if not body or len(body) > 500 or contains_sensitive(body):
                body = "[redacted]"
                _drop("log", "filtered")
            attributes, dropped = filter_attributes(record.attributes, LOG_ATTRIBUTES)
            if dropped:
                _drop("attribute", "filtered", dropped)
            safe.append(
                ReadableLogRecord(
                    LogRecord(
                        timestamp=record.timestamp,
                        observed_timestamp=record.observed_timestamp,
                        trace_id=record.trace_id,
                        span_id=record.span_id,
                        trace_flags=record.trace_flags,
                        severity_text=record.severity_text,
                        severity_number=record.severity_number,
                        body=body,
                        attributes=attributes,
                    ),
                    resource=_safe_resource(item.resource),
                    instrumentation_scope=item.instrumentation_scope,
                )
            )
        if not safe:
            return LogRecordExportResult.SUCCESS
        try:
            return self._inner.export(safe)
        except Exception:  # noqa: BLE001
            _drop("log", "export_failed", len(safe))
            return LogRecordExportResult.FAILURE

    def shutdown(self) -> None:
        try:
            self._inner.shutdown()
        except Exception:  # noqa: BLE001
            pass

    def force_flush(self, timeout_millis: int = 30000) -> bool:
        try:
            return self._inner.force_flush(timeout_millis)
        except Exception:  # noqa: BLE001
            return False


_SEVERITY = {
    logging.DEBUG: SeverityNumber.DEBUG,
    logging.INFO: SeverityNumber.INFO,
    logging.WARNING: SeverityNumber.WARN,
    logging.ERROR: SeverityNumber.ERROR,
    logging.CRITICAL: SeverityNumber.FATAL,
}


class EngineLogHandler(logging.Handler):
    """Forward engine log records (INFO and above) without exception text or extras."""

    def __init__(self, provider: LoggerProvider) -> None:
        super().__init__(level=logging.INFO)
        self._logger = provider.get_logger("engine")

    def emit(self, record: logging.LogRecord) -> None:
        try:
            if not (record.name == "root" or record.name.startswith("engine")):
                return
            context = trace.get_current_span().get_span_context()
            self._logger.emit(
                LogRecord(
                    timestamp=int(record.created * 1e9),
                    trace_id=context.trace_id if context.is_valid else None,
                    span_id=context.span_id if context.is_valid else None,
                    severity_text=record.levelname,
                    severity_number=_SEVERITY.get(record.levelno, SeverityNumber.INFO),
                    body=record.getMessage(),
                    attributes={"aorte.logger": record.name},
                )
            )
        except Exception:  # noqa: BLE001
            pass


# ---------------------------------------------------------------- setup


@dataclass
class Telemetry:
    """Handle returned by configure; shutdown flushes with a bounded timeout."""

    tracer_provider: TracerProvider | None = None
    meter_provider: MeterProvider | None = None
    logger_provider: LoggerProvider | None = None
    log_handler: EngineLogHandler | None = None
    apm: bool = False  # #134: psycopg and urllib are instrumented while this handle lives

    @property
    def enabled(self) -> bool:
        return self.tracer_provider is not None

    def force_flush(self) -> None:
        for provider in (self.tracer_provider, self.logger_provider):
            if provider is not None:
                provider.force_flush(5000)
        if self.meter_provider is not None:
            self.meter_provider.force_flush(5000)

    def shutdown(self) -> None:
        if self.apm:
            _uninstrument_apm()
            self.apm = False
        with _LOCK:
            if self.log_handler is not None:
                logging.getLogger().removeHandler(self.log_handler)
            for provider in (self.tracer_provider, self.meter_provider, self.logger_provider):
                if provider is not None:
                    try:
                        provider.shutdown()
                    except Exception:  # noqa: BLE001
                        pass
            if _STATE.handle is self:
                _STATE.tracer = trace.NoOpTracer()
                _STATE.meter = NoOpMeter("engine")
                _STATE.instruments.clear()
                _STATE.eval_text = False
                _STATE.handle = None


def otlp_exporters(
    settings: TelemetrySettings,
) -> tuple[SpanExporter, MetricExporter, LogRecordExporter]:
    """The production OTLP/HTTP exporters to Grafana Cloud (also used by the live check)."""
    from opentelemetry.exporter.otlp.proto.http._log_exporter import OTLPLogExporter
    from opentelemetry.exporter.otlp.proto.http.metric_exporter import OTLPMetricExporter
    from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter

    if not settings.enabled or not settings.grafana:
        raise TelemetryConfigError("telemetry is not enabled with a Grafana endpoint and token")
    base = settings.grafana_endpoint
    headers = {"Authorization": f"Basic {settings.grafana_token}"}
    return (
        OTLPSpanExporter(endpoint=f"{base}/v1/traces", headers=headers, timeout=10),
        OTLPMetricExporter(endpoint=f"{base}/v1/metrics", headers=headers, timeout=10),
        OTLPLogExporter(endpoint=f"{base}/v1/logs", headers=headers, timeout=10),
    )


def langfuse_span_exporter(settings: TelemetrySettings) -> SpanExporter:
    """OTLP/HTTP traces to Langfuse (F-078): Basic auth of public:secret key, ingestion v4.

    With a relay key (AWS, #150) it also sends the relay header the CloudFront path checks.
    """
    from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter

    if not settings.enabled or not settings.langfuse:
        raise TelemetryConfigError("telemetry is not enabled with the Langfuse settings")
    auth = base64.b64encode(
        f"{settings.langfuse_public_key}:{settings.langfuse_secret_key}".encode()
    ).decode()
    headers = {"Authorization": f"Basic {auth}", "x-langfuse-ingestion-version": "4"}
    if settings.relay_key:
        headers[RELAY_HEADER] = settings.relay_key  # removed by the relay before Langfuse
    return OTLPSpanExporter(
        endpoint=f"{settings.langfuse_endpoint}/v1/traces", headers=headers, timeout=10
    )


def periodic_reader(exporter: MetricExporter) -> MetricReader:
    return PeriodicExportingMetricReader(
        exporter, export_interval_millis=60_000, export_timeout_millis=10_000
    )


def configure(
    settings: TelemetrySettings,
    *,
    service_name: str,
    service_version: str | None = None,
    span_exporter: SpanExporter | None = None,
    metric_reader: MetricReader | None = None,
    log_exporter: LogRecordExporter | None = None,
    langfuse_exporter: SpanExporter | None = None,
    clock: Callable[[], datetime] | None = None,
) -> Telemetry:
    """Set up telemetry for this process; with `enabled=False` everything stays a no-op.

    Grafana gets traces, metrics, and logs; Langfuse gets only the run spans (#128). Tests pass
    in-memory exporters; production builds the OTLP/HTTP exporters from the settings.
    """
    if not settings.enabled:
        return Telemetry()
    if not RESOURCE_ATTRIBUTES["service.name"](service_name):
        raise TelemetryConfigError("invalid service name")
    grafana = settings.grafana or any(
        item is not None for item in (span_exporter, metric_reader, log_exporter)
    )
    if grafana and (span_exporter is None or metric_reader is None or log_exporter is None):
        otlp_spans, otlp_metrics, otlp_logs = otlp_exporters(settings)
        span_exporter = span_exporter or otlp_spans
        metric_reader = metric_reader or periodic_reader(otlp_metrics)
        log_exporter = log_exporter or otlp_logs
    if settings.langfuse and langfuse_exporter is None:
        langfuse_exporter = langfuse_span_exporter(settings)
    attributes: dict[str, Any] = {
        "service.name": service_name,
        "deployment.environment.name": settings.environment,
    }
    if service_version:
        attributes["service.version"] = service_version
    resource, _ = filter_attributes(attributes, RESOURCE_ATTRIBUTES)

    def batch(exporter: SpanExporter) -> BatchSpanProcessor:
        return BatchSpanProcessor(
            exporter,
            max_queue_size=2048,
            schedule_delay_millis=5000,
            max_export_batch_size=256,
            export_timeout_millis=10_000,
        )

    with _LOCK:
        if _STATE.handle is not None:
            raise TelemetryConfigError("telemetry is already configured in this process")
        tracer_provider = TracerProvider(resource=Resource(resource), id_generator=RunTraceIds())
        logger_provider: LoggerProvider | None = None
        handler: EngineLogHandler | None = None
        if span_exporter is not None:
            cap = DailyCap(settings.daily_span_cap or 1, clock)
            apm_cap = DailyCap(settings.daily_apm_span_cap or 1, clock) if settings.apm else None
            tracer_provider.add_span_processor(
                batch(SafeSpanExporter(span_exporter, cap, apm_cap=apm_cap))
            )
            logger_provider = LoggerProvider(resource=Resource(resource))
            logger_provider.add_log_record_processor(
                BatchLogRecordProcessor(
                    SafeLogExporter(cast(LogRecordExporter, log_exporter), cap),
                    max_queue_size=2048,
                    schedule_delay_millis=5000,
                    max_export_batch_size=256,
                    export_timeout_millis=10_000,
                )
            )
            handler = EngineLogHandler(logger_provider)
            logging.getLogger().addHandler(handler)
        if langfuse_exporter is not None:
            units = DailyCap(settings.daily_langfuse_unit_cap or 1, clock)
            tracer_provider.add_span_processor(
                batch(
                    SafeSpanExporter(
                        langfuse_exporter,
                        units,
                        names=LANGFUSE_SPANS,
                        text=settings.eval_text,
                        signal="langfuse",
                    )
                )
            )
        meter_provider = MeterProvider(
            resource=Resource(resource),
            metric_readers=[metric_reader] if metric_reader is not None else [],
            views=metric_views(),
        )
        handle = Telemetry(tracer_provider, meter_provider, logger_provider, handler)
        _STATE.tracer = tracer_provider.get_tracer("engine")
        _STATE.meter = meter_provider.get_meter("engine")
        _STATE.instruments.clear()
        _STATE.eval_text = settings.eval_text and langfuse_exporter is not None
        _STATE.handle = handle
        if settings.apm and span_exporter is not None:
            handle.apm = _instrument_apm(tracer_provider, meter_provider)
        if metric_reader is not None:
            # #163: peer presence. Each engine process reports 1 while it runs, so an alert can
            # tell "worker missing while the API runs" from "the whole stack is stopped".
            observable_gauge(
                "aorte.process.up",
                "1",
                "1 while this engine process runs (#163)",
                lambda: [(1, {})],
            )
    return handle


# ---------------------------------------------------------------- APM (#134)

# Instrumentation metrics: only the HTTP server duration histogram survives, with three
# bounded labels (OBS-8). Every other http.* and db.* instrument is dropped.
SERVER_DURATION_LABELS = frozenset(
    {"http.route", "http.request.method", "http.response.status_code"}
)
SERVER_DURATION_BUCKETS = (0.05, 0.1, 0.25, 0.5, 1.0, 2.5, 5.0, 10.0)


def metric_views() -> list[Any]:
    from opentelemetry.sdk.metrics.view import (
        DropAggregation,
        ExplicitBucketHistogramAggregation,
        View,
    )

    return [
        View(instrument_name="http.*", aggregation=DropAggregation()),
        View(instrument_name="db.*", aggregation=DropAggregation()),
        View(
            instrument_name="http.server.request.duration",
            attribute_keys=set(SERVER_DURATION_LABELS),
            aggregation=ExplicitBucketHistogramAggregation(SERVER_DURATION_BUCKETS),
        ),
    ]


def _stable_semconv() -> None:
    """Stable attribute names only (http.route, db.query.text), read once per process."""
    import os

    from opentelemetry.instrumentation._semconv import (
        _OpenTelemetrySemanticConventionStability as Stability,
    )

    if os.environ.get("OTEL_SEMCONV_STABILITY_OPT_IN") != "http,database":
        os.environ["OTEL_SEMCONV_STABILITY_OPT_IN"] = "http,database"
        Stability._initialized = False  # noqa: SLF001 - re-read the opt-in above


def _instrument_apm(tracer_provider: TracerProvider, meter_provider: MeterProvider) -> bool:
    """psycopg covers SQLAlchemy and the Odoo reader alike (F-097); never raises."""
    try:
        _stable_semconv()
        from opentelemetry.instrumentation.psycopg import PsycopgInstrumentor
        from opentelemetry.instrumentation.urllib import URLLibInstrumentor

        PsycopgInstrumentor().instrument(
            tracer_provider=tracer_provider,
            meter_provider=meter_provider,
            capture_parameters=False,
            enable_commenter=False,
        )
        URLLibInstrumentor().instrument(
            tracer_provider=tracer_provider, meter_provider=meter_provider
        )
        return True
    except Exception:  # noqa: BLE001 - telemetry never stops the engine (OBS-5)
        LOG.warning("APM instrumentation failed; continuing without it")
        return False


def _uninstrument_apm() -> None:
    try:
        from opentelemetry.instrumentation.psycopg import PsycopgInstrumentor
        from opentelemetry.instrumentation.urllib import URLLibInstrumentor

        PsycopgInstrumentor().uninstrument()
        URLLibInstrumentor().uninstrument()
    except Exception:  # noqa: BLE001
        pass


def instrument_fastapi(app: Any) -> None:
    """Server spans and the request-duration histogram for the approval API (#134)."""
    handle = _STATE.handle
    if handle is None or not handle.apm:
        return
    try:
        from opentelemetry.instrumentation.fastapi import FastAPIInstrumentor

        FastAPIInstrumentor.instrument_app(
            app,
            tracer_provider=handle.tracer_provider,
            meter_provider=handle.meter_provider,
            excluded_urls="/healthz",  # the container health check every 30 s
            exclude_spans=["receive", "send"],
        )
    except Exception:  # noqa: BLE001
        LOG.warning("FastAPI instrumentation failed; continuing without it")


def configure_from_env(env: Mapping[str, str], *, service_name: str) -> Telemetry:
    """Entrypoint helper: fails closed on invalid settings (OBS-6)."""
    settings = TelemetrySettings.from_env(env)
    version = env.get("ENGINE_VERSION", "").strip() or None
    return configure(settings, service_name=service_name, service_version=version)
