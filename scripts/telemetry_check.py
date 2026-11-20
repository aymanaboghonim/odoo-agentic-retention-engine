"""Live telemetry export check (#127, OBS-11, smoke SM-6).

Preflight: the telemetry settings parse (the token is never printed), and the endpoint host
resolves; with `--environment aws-mvp` it must have an AAAA record, because the host has
IPv6-only egress (ADR-0024, F-079). Then one span, one metric point, and one log line go
through the production path (the engine's allowlist filters and OTLP/HTTP exporters), and
each export result is reported. The data is synthetic and carries only a check ID.

    # local, token from a file outside the repo
    GRAFANA_OTLP_ENDPOINT=https://otlp-gateway-prod-us-east-2.grafana.net/otlp \
      uv run --no-sync python scripts/telemetry_check.py --token-file ~/.config/aorte/grafana-otlp-token

    # in the AWS operator container (settings come from the task definition and SSM)
    python -m scripts.telemetry_check --environment aws-mvp

What it does not check: that Grafana stored or indexes the data. Look the check ID up in
Grafana Explore (service.name `aorte-telemetry-check`).
"""

from __future__ import annotations

import argparse
import logging
import os
import socket
import sys
import time
import uuid
from collections.abc import Callable, Mapping, Sequence
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from opentelemetry.sdk._logs import ReadableLogRecord  # noqa: E402
from opentelemetry.sdk._logs.export import LogRecordExporter, LogRecordExportResult  # noqa: E402
from opentelemetry.sdk.metrics.export import (  # noqa: E402
    MetricExporter,
    MetricExportResult,
    MetricsData,
    PeriodicExportingMetricReader,
)
from opentelemetry.sdk.trace import ReadableSpan  # noqa: E402
from opentelemetry.sdk.trace.export import SpanExporter, SpanExportResult  # noqa: E402

from engine import telemetry  # noqa: E402

SERVICE = "aorte-telemetry-check"
Resolver = Callable[..., list[Any]]
ExporterFactory = Callable[
    [telemetry.TelemetrySettings], tuple[SpanExporter, MetricExporter, LogRecordExporter]
]


class CheckFailed(Exception):
    """A named stage failed; the message never contains a secret."""


def stage(name: str, status: str, detail: str = "") -> None:
    print(f"stage={name} status={status}{' ' + detail if detail else ''}", flush=True)


class RecordingSpans(SpanExporter):
    def __init__(self, inner: SpanExporter) -> None:
        self.inner = inner
        self.results: list[bool] = []

    def export(self, spans: Sequence[ReadableSpan]) -> SpanExportResult:
        result = self.inner.export(spans)
        self.results.append(result is SpanExportResult.SUCCESS)
        return result

    def shutdown(self) -> None:
        self.inner.shutdown()

    def force_flush(self, timeout_millis: int = 30000) -> bool:
        return self.inner.force_flush(timeout_millis)


class RecordingMetrics(MetricExporter):
    def __init__(self, inner: MetricExporter) -> None:
        super().__init__(
            preferred_temporality=inner._preferred_temporality,
            preferred_aggregation=inner._preferred_aggregation,
        )
        self.inner = inner
        self.results: list[bool] = []

    def export(
        self, metrics_data: MetricsData, timeout_millis: float = 10_000, **kwargs: Any
    ) -> MetricExportResult:
        result = self.inner.export(metrics_data, timeout_millis=timeout_millis, **kwargs)
        self.results.append(result is MetricExportResult.SUCCESS)
        return result

    def force_flush(self, timeout_millis: float = 10_000) -> bool:
        return self.inner.force_flush(timeout_millis)

    def shutdown(self, timeout_millis: float = 30_000, **kwargs: Any) -> None:
        self.inner.shutdown(timeout_millis=timeout_millis, **kwargs)


class RecordingLogs(LogRecordExporter):
    def __init__(self, inner: LogRecordExporter) -> None:
        self.inner = inner
        self.results: list[bool] = []

    def export(self, batch: Sequence[ReadableLogRecord]) -> LogRecordExportResult:
        result = self.inner.export(batch)
        self.results.append(result is LogRecordExportResult.SUCCESS)
        return result

    def shutdown(self) -> None:
        self.inner.shutdown()

    def force_flush(self, timeout_millis: int = 30000) -> bool:
        return self.inner.force_flush(timeout_millis)


def settings_from(env: Mapping[str, str], environment: str, token_file: Path | None) -> Any:
    token = env.get("GRAFANA_OTLP_TOKEN", "")
    if token_file is not None:
        try:
            token = token_file.read_text(encoding="utf-8").strip()
        except OSError:
            raise CheckFailed("cannot read the token file") from None
    try:
        return telemetry.TelemetrySettings.from_env(
            {
                "TELEMETRY_ENABLED": "YES",
                "TELEMETRY_ENVIRONMENT": environment,
                "GRAFANA_OTLP_ENDPOINT": env.get("GRAFANA_OTLP_ENDPOINT", ""),
                "GRAFANA_OTLP_TOKEN": token,
                "TELEMETRY_DAILY_SPAN_CAP": "100",
            }
        )
    except telemetry.TelemetryConfigError as error:
        raise CheckFailed(str(error)) from None


def check_dns(endpoint: str, *, require_ipv6: bool, resolve: Resolver) -> tuple[int, int]:
    host = urlsplit(endpoint).hostname or ""
    counts = []
    for family in (socket.AF_INET, socket.AF_INET6):
        try:
            counts.append(len({item[4][0] for item in resolve(host, 443, family)}))
        except OSError:
            counts.append(0)
    ipv4, ipv6 = counts
    if require_ipv6 and ipv6 == 0:
        raise CheckFailed(f"{host} has no AAAA record; the AWS host has IPv6-only egress")
    if ipv4 + ipv6 == 0:
        raise CheckFailed(f"{host} does not resolve")
    return ipv4, ipv6


def main(
    argv: list[str] | None = None,
    env: Mapping[str, str] | None = None,
    *,
    exporters: ExporterFactory = telemetry.otlp_exporters,
    resolve: Resolver = socket.getaddrinfo,
) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "--environment",
        choices=sorted(telemetry.ENVIRONMENTS),
        default=None,
        help="defaults to TELEMETRY_ENVIRONMENT, else local; aws-mvp requires an AAAA record",
    )
    parser.add_argument("--token-file", type=Path, help="read the token from this file")
    args = parser.parse_args(argv)
    source = os.environ if env is None else env
    environment = args.environment or source.get("TELEMETRY_ENVIRONMENT", "") or "local"
    started = time.monotonic()
    check_id = str(uuid.uuid4())
    try:
        stage("preflight", "started")
        settings = settings_from(source, environment, args.token_file)
        ipv4, ipv6 = check_dns(
            settings.grafana_endpoint,
            require_ipv6=environment == "aws-mvp",
            resolve=resolve,
        )
        stage(
            "preflight", "passed", f"a_records={ipv4} aaaa_records={ipv6} environment={environment}"
        )
        spans_exporter, metrics_exporter, logs_exporter = exporters(settings)
        spans = RecordingSpans(spans_exporter)
        metrics = RecordingMetrics(metrics_exporter)
        logs = RecordingLogs(logs_exporter)
        logging.basicConfig(level=logging.INFO, handlers=[logging.NullHandler()])
        handle = telemetry.configure(
            settings,
            service_name=SERVICE,
            span_exporter=spans,
            metric_reader=PeriodicExportingMetricReader(
                metrics, export_interval_millis=3_600_000, export_timeout_millis=10_000
            ),
            log_exporter=logs,
        )
        stage("export", "started", f"check_id={check_id}")
        try:
            with telemetry.span("aorte.scan", **{"aorte.run_id": check_id}) as span:
                span.set("aorte.outcome", "skipped")
            telemetry.count("aorte.telemetry.check", 1)
            log = logging.getLogger("engine.telemetry_check")
            log.setLevel(logging.INFO)  # works even when the root logger is already set up
            log.info("telemetry check run=%s", check_id)
            handle.force_flush()
        finally:
            handle.shutdown()
        failed = []
        for name, recorder in (("traces", spans), ("metrics", metrics), ("logs", logs)):
            ok = bool(recorder.results) and all(recorder.results)
            stage(f"export-{name}", "ok" if ok else "failed")
            if not ok:
                failed.append(name)
        if failed:
            raise CheckFailed(f"export failed for {', '.join(failed)}")
    except CheckFailed as error:
        stage("check", "failed", f"reason={error}")
        return 1
    elapsed = time.monotonic() - started
    stage("check", "passed", f"check_id={check_id} service={SERVICE} elapsed_s={elapsed:.1f}")
    print(
        "not checked: storage and indexing in Grafana; search the check ID in Explore",
        flush=True,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
