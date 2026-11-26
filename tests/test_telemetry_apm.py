"""#134, OBS-12: engine APM spans and metrics are scrubbed and capped.

Hermetic: in-memory exporters, a local HTTP server on 127.0.0.1, and the throwaway test
database for the psycopg case. No live service is called.
"""

from __future__ import annotations

import json
import os
import threading
import urllib.request
from collections.abc import Iterator
from dataclasses import dataclass, replace
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

import psycopg
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from opentelemetry.sdk.metrics.export import InMemoryMetricReader
from opentelemetry.sdk.trace import ReadableSpan
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

from engine import telemetry

URL = os.environ.get("TEST_DATABASE_URL", "")
if not URL and os.environ.get("REQUIRE_DB_TESTS") == "1":
    raise RuntimeError("REQUIRE_DB_TESTS=1 but TEST_DATABASE_URL is not set")

EMAIL = "layla.haddad@example.test"
BOT_TOKEN = "123456789:" + "A" * 35  # synthetic, Telegram-shaped
SETTINGS = telemetry.TelemetrySettings(
    enabled=True,
    environment="local",
    grafana_endpoint="https://otlp.example.test/otlp",
    grafana_token="QUJD" + "x" * 40,
    daily_span_cap=10_000,
    apm=True,
    daily_apm_span_cap=1_000,
)


@dataclass
class Apm:
    handle: telemetry.Telemetry
    spans: InMemorySpanExporter
    reader: InMemoryMetricReader
    langfuse: InMemorySpanExporter

    def finished(self) -> list[ReadableSpan]:
        self.handle.force_flush()
        return list(self.spans.get_finished_spans())

    def dump(self) -> str:
        return json.dumps(
            [{"name": s.name, "attributes": dict(s.attributes or {})} for s in self.finished()],
            default=str,
        )


def start(settings: telemetry.TelemetrySettings) -> Apm:
    spans, reader, langfuse = InMemorySpanExporter(), InMemoryMetricReader(), InMemorySpanExporter()
    from opentelemetry.sdk._logs.export import InMemoryLogRecordExporter

    handle = telemetry.configure(
        settings,
        service_name="aorte-test",
        span_exporter=spans,
        metric_reader=reader,
        log_exporter=InMemoryLogRecordExporter(),
        langfuse_exporter=langfuse,
    )
    return Apm(handle, spans, reader, langfuse)


@pytest.fixture
def apm() -> Iterator[Apm]:
    capture = start(
        replace(
            SETTINGS,
            langfuse_endpoint="https://lf.example.test/api/public/otel",
            langfuse_public_key="pk-lf-" + "1" * 12,
            langfuse_secret_key="sk-lf-" + "2" * 12,
            daily_langfuse_unit_cap=100,
        )
    )
    try:
        yield capture
    finally:
        capture.handle.shutdown()


class _Handler(BaseHTTPRequestHandler):
    def do_POST(self) -> None:  # noqa: N802 - stdlib name
        self.rfile.read(int(self.headers.get("Content-Length", "0")))
        self.send_response(200)
        self.end_headers()
        self.wfile.write(b"{}")

    def log_message(self, *args: Any) -> None:
        pass


def test_apm_spans_are_scrubbed(apm: Apm) -> None:
    """OBS-12: no full URL, query string, bot path, or SQL parameter leaves; names are safe."""
    app = FastAPI()

    @app.get("/items/{item_id}")
    def item(item_id: str) -> dict[str, str]:
        return {"ok": "yes"}

    telemetry.instrument_fastapi(app)
    client = TestClient(app)
    assert client.get(f"/items/secret-item?email={EMAIL}").status_code == 200

    server = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        url = f"http://127.0.0.1:{server.server_port}/bot{BOT_TOKEN}/sendMessage?chat_id=42"
        with urllib.request.urlopen(urllib.request.Request(url, data=b"{}"), timeout=5) as r:
            assert r.status == 200
    finally:
        server.shutdown()

    spans = apm.finished()
    names = {s.name for s in spans}
    assert "GET /items/{item_id}" in names
    assert "POST" in names
    server_span = next(s for s in spans if s.name == "GET /items/{item_id}")
    assert dict(server_span.attributes or {}) == {
        "http.request.method": "GET",
        "http.route": "/items/{item_id}",
        "http.response.status_code": 200,
        "url.scheme": "http",
        "server.address": "testserver",  # Host header; the pattern refuses anything but a host
        "server.port": 80,
    }
    client_span = next(s for s in spans if s.name == "POST")
    assert set(dict(client_span.attributes or {})) <= {
        "http.request.method",
        "http.response.status_code",
        "server.address",
        "server.port",
    }
    dump = apm.dump()
    for leaked in (BOT_TOKEN, "chat_id", "secret-item", EMAIL, "?", "sendMessage", "url.full"):
        assert leaked not in dump, leaked
    # Every exported APM name has a safe shape; anything else would be renamed aorte.other.
    assert all(telemetry.APM_SPAN_NAME.fullmatch(n) for n in names - telemetry.SPAN_NAMES)
    # APM spans never reach Langfuse (OBS-18).
    apm.handle.force_flush()
    assert apm.langfuse.get_finished_spans() == ()

    # Instrumentation metrics: only the server duration histogram, three bounded labels.
    data = apm.reader.get_metrics_data()
    found: dict[str, set[str]] = {}
    for rm in data.resource_metrics if data else ():
        for scope in rm.scope_metrics:
            for metric in scope.metrics:
                for point in metric.data.data_points:
                    found.setdefault(metric.name, set()).update(dict(point.attributes or {}))
    apm_metrics = {name for name in found if name.startswith(("http.", "db."))}
    assert apm_metrics == {"http.server.request.duration"}
    assert found["http.server.request.duration"] <= telemetry.SERVER_DURATION_LABELS


@pytest.mark.skipif(not URL, reason="TEST_DATABASE_URL not set")
def test_apm_sql_spans_keep_the_statement_shape_only(apm: Apm) -> None:
    with psycopg.connect(URL) as conn:
        conn.execute("SELECT %s::text AS v", (EMAIL,)).fetchone()
    spans = [s for s in apm.finished() if telemetry.is_apm(s)]
    assert spans, "no psycopg span"
    sql = [s for s in spans if "db.query.text" in dict(s.attributes or {})]
    assert sql and dict(sql[0].attributes or {})["db.query.text"] == "SELECT %s::text AS v"
    assert dict(sql[0].attributes or {})["db.system.name"] == "postgresql"
    assert EMAIL not in apm.dump()
    assert all(s.name != telemetry.OTHER_SPAN for s in sql)


def test_apm_spans_have_their_own_cap() -> None:
    """OBS-13: APM spans never use the engine's span cap, and stop at their own."""
    capture = start(replace(SETTINGS, daily_span_cap=1, daily_apm_span_cap=1))
    try:
        app = FastAPI()

        @app.get("/a")
        def a() -> dict[str, str]:
            return {}

        telemetry.instrument_fastapi(app)
        client = TestClient(app)
        client.get("/a")
        client.get("/a")  # over the APM cap
        with telemetry.span("aorte.check"):
            pass
        names = [s.name for s in capture.finished()]
        assert names.count("GET /a") == 1
        assert "aorte.check" in names  # the engine cap was not used by APM
    finally:
        capture.handle.shutdown()


def test_apm_settings_fail_closed() -> None:
    base = {
        "TELEMETRY_ENABLED": "YES",
        "TELEMETRY_ENVIRONMENT": "local",
        "GRAFANA_OTLP_ENDPOINT": "https://otlp.example.test/otlp",
        "GRAFANA_OTLP_TOKEN": "QUJD" + "x" * 40,
        "TELEMETRY_DAILY_SPAN_CAP": "100",
    }
    assert telemetry.TelemetrySettings.from_env(base).apm is False
    on = {**base, "TELEMETRY_APM": "YES", "TELEMETRY_DAILY_APM_SPAN_CAP": "500"}
    settings = telemetry.TelemetrySettings.from_env(on)
    assert settings.apm is True and settings.daily_apm_span_cap == 500
    for broken in (
        {**base, "TELEMETRY_APM": "maybe"},
        {**base, "TELEMETRY_APM": "YES"},  # no APM cap
    ):
        with pytest.raises(telemetry.TelemetryConfigError):
            telemetry.TelemetrySettings.from_env(broken)
    langfuse_only = {
        "TELEMETRY_ENABLED": "YES",
        "TELEMETRY_ENVIRONMENT": "local",
        "LANGFUSE_OTLP_ENDPOINT": "https://lf.example.test/api/public/otel",
        "LANGFUSE_PUBLIC_KEY": "pk-lf-" + "1" * 12,
        "LANGFUSE_SECRET_KEY": "sk-lf-" + "2" * 12,
        "TELEMETRY_DAILY_LANGFUSE_UNIT_CAP": "10",
        "TELEMETRY_APM": "YES",
        "TELEMETRY_DAILY_APM_SPAN_CAP": "500",
    }
    with pytest.raises(telemetry.TelemetryConfigError, match="Grafana"):
        telemetry.TelemetrySettings.from_env(langfuse_only)
