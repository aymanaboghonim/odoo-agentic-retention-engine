"""scripts/telemetry_check.py (#127, OBS-11, smoke SM-6). Hermetic: fake exporters and DNS."""

from __future__ import annotations

import socket
import sys
import uuid
from collections.abc import Sequence
from pathlib import Path
from typing import Any

import pytest
from opentelemetry.sdk._logs import ReadableLogRecord
from opentelemetry.sdk._logs.export import LogRecordExporter, LogRecordExportResult
from opentelemetry.sdk.metrics.export import MetricExporter, MetricExportResult, MetricsData
from opentelemetry.sdk.trace import ReadableSpan
from opentelemetry.sdk.trace.export import SpanExporter, SpanExportResult

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from engine import telemetry  # noqa: E402
from scripts import telemetry_check  # noqa: E402

TOKEN = "QUJD" + "x" * 30  # synthetic, low-entropy, base64-shaped
ENV = {
    "GRAFANA_OTLP_ENDPOINT": "https://otlp-gateway-prod-us-east-2.grafana.net/otlp",
    "GRAFANA_OTLP_TOKEN": TOKEN,
}


class Spans(SpanExporter):
    def __init__(self, ok: bool) -> None:
        self.ok = ok
        self.exported: list[ReadableSpan] = []

    def export(self, spans: Sequence[ReadableSpan]) -> SpanExportResult:
        self.exported.extend(spans)
        return SpanExportResult.SUCCESS if self.ok else SpanExportResult.FAILURE

    def shutdown(self) -> None:
        return None


class Metrics(MetricExporter):
    def __init__(self, ok: bool) -> None:
        super().__init__()
        self.ok = ok

    def export(
        self, metrics_data: MetricsData, timeout_millis: float = 10_000, **kwargs: Any
    ) -> MetricExportResult:
        return MetricExportResult.SUCCESS if self.ok else MetricExportResult.FAILURE

    def force_flush(self, timeout_millis: float = 10_000) -> bool:
        return True

    def shutdown(self, timeout_millis: float = 30_000, **kwargs: Any) -> None:
        return None


class Logs(LogRecordExporter):
    def __init__(self, ok: bool) -> None:
        self.ok = ok
        self.bodies: list[object] = []

    def export(self, batch: Sequence[ReadableLogRecord]) -> LogRecordExportResult:
        self.bodies.extend(item.log_record.body for item in batch)
        return LogRecordExportResult.SUCCESS if self.ok else LogRecordExportResult.FAILURE

    def shutdown(self) -> None:
        return None

    def force_flush(self, timeout_millis: int = 30000) -> bool:
        return True


class Factory:
    def __init__(self, spans_ok: bool = True) -> None:
        self.calls = 0
        self.spans = Spans(spans_ok)
        self.logs = Logs(True)

    def __call__(
        self, settings: telemetry.TelemetrySettings
    ) -> tuple[SpanExporter, MetricExporter, LogRecordExporter]:
        self.calls += 1
        return self.spans, Metrics(True), self.logs


def resolver(ipv6: bool) -> Any:
    def resolve(host: str, port: int, family: int) -> list[Any]:
        if family == socket.AF_INET6 and not ipv6:
            raise socket.gaierror("no AAAA")
        address = "2600:1f16::1" if family == socket.AF_INET6 else "3.0.0.1"
        return [(family, socket.SOCK_STREAM, 6, "", (address, port))]

    return resolve


def run(
    capsys: pytest.CaptureFixture[str], argv: list[str], env: dict[str, str], **kwargs: Any
) -> tuple[int, str]:
    code = telemetry_check.main(argv, env, **kwargs)
    captured = capsys.readouterr()
    return code, captured.out + captured.err


def test_check_sends_one_item_per_signal_and_never_prints_the_token(
    capsys: pytest.CaptureFixture[str],
) -> None:
    factory = Factory()
    code, out = run(capsys, [], ENV, exporters=factory, resolve=resolver(True))
    assert code == 0, out
    for signal in ("traces", "metrics", "logs"):
        assert f"stage=export-{signal} status=ok" in out
    assert "stage=check status=passed" in out and "grafana:a=1,aaaa=1" in out
    assert TOKEN not in out
    (span,) = factory.spans.exported
    assert span.resource.attributes["service.name"] == "aorte-telemetry-check"
    assert set((span.attributes or {})) == {"aorte.run_id", "aorte.outcome"}
    assert len(factory.logs.bodies) == 1 and "telemetry check run=" in str(factory.logs.bodies[0])


def test_aws_mvp_requires_an_aaaa_record_before_any_export(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """OBS-11: the IPv6-only host cannot reach an IPv4-only endpoint (ADR-0024)."""
    factory = Factory()
    code, out = run(
        capsys, ["--environment", "aws-mvp"], ENV, exporters=factory, resolve=resolver(False)
    )
    assert code == 1 and "has no AAAA record" in out
    assert factory.calls == 0
    code, _ = run(
        capsys, ["--environment", "local"], ENV, exporters=Factory(), resolve=resolver(False)
    )
    assert code == 0


def test_failed_export_fails_the_check(capsys: pytest.CaptureFixture[str]) -> None:
    code, out = run(capsys, [], ENV, exporters=Factory(spans_ok=False), resolve=resolver(True))
    assert code == 1
    assert "stage=export-traces status=failed" in out and "export failed for traces" in out


def test_bad_settings_fail_preflight_without_echoing_secrets(
    capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    raw = "glc_" + "r" * 40
    code, out = run(capsys, [], {**ENV, "GRAFANA_OTLP_TOKEN": raw}, exporters=Factory())
    assert code == 1 and "base64 of <instance-id>:<token>" in out and raw not in out
    code, out = run(capsys, [], {"GRAFANA_OTLP_TOKEN": TOKEN}, exporters=Factory())
    assert code == 1 and "GRAFANA_OTLP_ENDPOINT" in out
    token_file = tmp_path / "token"
    token_file.write_text(TOKEN + "\n", encoding="utf-8")
    env = {"GRAFANA_OTLP_ENDPOINT": ENV["GRAFANA_OTLP_ENDPOINT"]}
    code, out = run(
        capsys,
        ["--token-file", str(token_file)],
        env,
        exporters=Factory(),
        resolve=resolver(True),
    )
    assert code == 0 and TOKEN not in out
    code, out = run(capsys, ["--token-file", str(tmp_path / "missing")], env, exporters=Factory())
    assert code == 1 and "cannot read the token file" in out


@pytest.mark.parametrize(
    ("token", "message"),
    [("glc_" + "r" * 40, "base64"), ("unset", "SSM placeholder")],
)
def test_raw_or_placeholder_token_is_refused_by_the_engine_settings(
    token: str, message: str
) -> None:
    """A raw API token gets 401 and the SSM placeholder means the secret was never written."""
    with pytest.raises(telemetry.TelemetryConfigError, match=message):
        telemetry.TelemetrySettings.from_env(
            {
                "TELEMETRY_ENABLED": "YES",
                "TELEMETRY_ENVIRONMENT": "aws-mvp",
                "GRAFANA_OTLP_ENDPOINT": ENV["GRAFANA_OTLP_ENDPOINT"],
                "GRAFANA_OTLP_TOKEN": token,
                "TELEMETRY_DAILY_SPAN_CAP": "10",
            }
        )


LANGFUSE_KEYS = "pk-lf-" + "1" * 12 + ":" + "sk-lf-" + "2" * 12  # synthetic


def test_langfuse_check_sends_one_event_in_the_check_trace(
    capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    """#128: with only Langfuse set, one `aorte.check` event goes to the Langfuse exporter."""
    keys = tmp_path / "langfuse"
    keys.write_text(LANGFUSE_KEYS + "\n", encoding="utf-8")
    exported = Spans(True)
    env = {"LANGFUSE_OTLP_ENDPOINT": "https://us.cloud.langfuse.com/api/public/otel"}
    code, out = run(
        capsys,
        ["--langfuse-key-file", str(keys)],
        env,
        exporters=Factory(),
        langfuse=lambda settings: exported,
        resolve=resolver(True),
    )
    assert code == 0, out
    assert "stage=export-langfuse status=ok" in out and "export-traces" not in out
    (event,) = exported.exported
    check_id = (event.attributes or {})["aorte.run_id"]
    assert event.name == "aorte.check" and event.context.trace_id == uuid.UUID(str(check_id)).int
    assert LANGFUSE_KEYS.split(":")[1] not in out


def test_langfuse_host_without_aaaa_fails_on_aws(capsys: pytest.CaptureFixture[str]) -> None:
    """F-078: Langfuse Cloud is IPv4-only; on the AWS host only the relay can pass (#129)."""
    public, _, secret = LANGFUSE_KEYS.partition(":")
    env = {
        "LANGFUSE_OTLP_ENDPOINT": "https://us.cloud.langfuse.com/api/public/otel",
        "LANGFUSE_PUBLIC_KEY": public,
        "LANGFUSE_SECRET_KEY": secret,
        "TELEMETRY_RELAY_KEY": "r" * 43,  # synthetic; aws-mvp needs it (#150)
    }
    code, out = run(
        capsys,
        ["--environment", "aws-mvp"],
        env,
        langfuse=lambda settings: Spans(True),
        resolve=resolver(False),
    )
    assert code == 1 and "has no AAAA record" in out


def test_aws_check_through_the_relay_passes_the_relay_key(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """#150: SM-6 on the AWS host sends the Langfuse check through the relay."""
    public, _, secret = LANGFUSE_KEYS.partition(":")
    env = {
        "LANGFUSE_OTLP_ENDPOINT": "https://d0.cloudfront.example/lf-otel/api/public/otel",
        "LANGFUSE_PUBLIC_KEY": public,
        "LANGFUSE_SECRET_KEY": secret,
        "TELEMETRY_RELAY_KEY": "r" * 43,
    }
    seen = []

    def exporter(settings: Any) -> Spans:
        seen.append(settings.relay_key)
        return Spans(True)

    code, out = run(
        capsys, ["--environment", "aws-mvp"], env, langfuse=exporter, resolve=resolver(True)
    )
    assert code == 0, out
    assert seen == ["r" * 43] and "r" * 43 not in out
