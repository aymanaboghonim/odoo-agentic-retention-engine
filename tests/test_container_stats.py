"""#165, OBS-20: per-container metrics from ECS task stats (ADR-0033).

Hermetic: a fake fetch for the metadata endpoint, and a local HTTP server on 127.0.0.1 for the
APM case. The response shapes follow the AWS examples (F-099).
"""

from __future__ import annotations

import json
import threading
from collections.abc import Iterator
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

import pytest
from opentelemetry.sdk._logs.export import InMemoryLogRecordExporter
from opentelemetry.sdk.metrics.export import InMemoryMetricReader, NumberDataPoint
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

from engine import container_stats, telemetry

BASE = "http://169.254.170.2/v4/0123456789abcdef-1234567890"
SETTINGS = telemetry.TelemetrySettings(
    enabled=True,
    environment="local",
    grafana_endpoint="https://otlp.example.test/otlp",
    grafana_token="QUJD" + "x" * 40,
    daily_span_cap=10_000,
    apm=True,
    daily_apm_span_cap=1_000,
)
WORKER_ID = "a" * 64
ODOO_ID = "b" * 64


def docker_stats(cpu_ns: int, usage: int, inactive: int, rx: int, tx: int) -> dict[str, Any]:
    return {
        "cpu_stats": {"cpu_usage": {"total_usage": cpu_ns}, "online_cpus": 2},
        "memory_stats": {"usage": usage, "limit": 134217728, "stats": {"inactive_file": inactive}},
        "networks": {"eth0": {"rx_bytes": rx, "tx_bytes": tx}},
    }


def task(restart: bool = True) -> dict[str, Any]:
    worker: dict[str, Any] = {
        "DockerId": WORKER_ID,
        "Name": "worker",
        "Type": "NORMAL",
        "KnownStatus": "RUNNING",
    }
    if restart:
        worker["RestartCount"] = 1
    return {
        "Containers": [
            worker,
            {"DockerId": ODOO_ID, "Name": "odoo", "Type": "NORMAL", "KnownStatus": "RUNNING"},
            # Not reported: the ECS pause container, a stopped one-shot, an unknown name.
            {
                "DockerId": "c" * 64,
                "Name": "~internal~ecs~pause",
                "Type": "CNI_PAUSE",
                "KnownStatus": "RUNNING",
            },
            {
                "DockerId": "d" * 64,
                "Name": "initialize",
                "Type": "NORMAL",
                "KnownStatus": "STOPPED",
                "ExitCode": 0,
            },
            {
                "DockerId": "e" * 64,
                "Name": "customer-42",
                "Type": "NORMAL",
                "KnownStatus": "RUNNING",
            },
        ]
    }


def stats() -> dict[str, Any]:
    return {
        WORKER_ID: docker_stats(2_500_000_000, 60_000_000, 10_000_000, 1000, 2000),
        ODOO_ID: docker_stats(7_000_000_000, 150_000_000, 0, 30, 40),
        "c" * 64: docker_stats(1, 1, 0, 1, 1),
        "d" * 64: None,
        "e" * 64: docker_stats(1, 1, 0, 1, 1),
    }


class FakeMetadata:
    def __init__(self, *, fail: bool = False, restart: bool = True) -> None:
        self.calls: list[str] = []
        self.fail = fail
        self.restart = restart

    def __call__(self, url: str) -> Any:
        self.calls.append(url)
        if self.fail:
            raise OSError("metadata endpoint down")
        return task(self.restart) if url.endswith("/task") else stats()


@pytest.fixture
def metrics() -> Iterator[InMemoryMetricReader]:
    reader = InMemoryMetricReader()
    handle = telemetry.configure(
        SETTINGS,
        service_name="aorte-test",
        span_exporter=InMemorySpanExporter(),
        metric_reader=reader,
        log_exporter=InMemoryLogRecordExporter(),
    )
    try:
        yield reader
    finally:
        handle.shutdown()


def collect(reader: InMemoryMetricReader) -> dict[str, list[tuple[float, dict[str, Any]]]]:
    found: dict[str, list[tuple[float, dict[str, Any]]]] = {}
    data = reader.get_metrics_data()
    for rm in data.resource_metrics if data else ():
        for scope in rm.scope_metrics:
            for metric in scope.metrics:
                if metric.name.startswith("aorte.container."):
                    for point in metric.data.data_points:
                        assert isinstance(point, NumberDataPoint)
                        found.setdefault(metric.name, []).append(
                            (point.value, dict(point.attributes or {}))
                        )
    return found


def test_container_gauges_closed_labels(metrics: InMemoryMetricReader) -> None:
    """OBS-20: known running containers only; the container name is a closed-set label."""
    fake = FakeMetadata()
    assert container_stats.register({container_stats.METADATA_ENV: BASE}, fetch=fake) is not None
    found = collect(metrics)
    assert set(found) == {
        "aorte.container.cpu.time",
        "aorte.container.memory.usage",
        "aorte.container.network.io",
        "aorte.container.restarts",
    }
    cpu = {labels["aorte.container"]: value for value, labels in found["aorte.container.cpu.time"]}
    assert cpu == {"worker": 2.5, "odoo": 7.0}
    memory = {labels["aorte.container"]: v for v, labels in found["aorte.container.memory.usage"]}
    assert memory == {"worker": 50_000_000, "odoo": 150_000_000}  # without inactive page cache
    network = {
        (labels["aorte.container"], labels["aorte.direction"]): v
        for v, labels in found["aorte.container.network.io"]
    }
    assert network == {
        ("worker", "rx"): 1000,
        ("worker", "tx"): 2000,
        ("odoo", "rx"): 30,
        ("odoo", "tx"): 40,
    }
    # RestartCount comes only with a restart policy; a container without it reports nothing.
    assert found["aorte.container.restarts"] == [(1, {"aorte.container": "worker"})]
    # Only the closed label set, only the closed values.
    for points in found.values():
        for _, labels in points:
            assert set(labels) <= {"aorte.container", "aorte.direction"}
            assert labels["aorte.container"] in telemetry.CONTAINERS
    dump = json.dumps(found)
    for leaked in ("customer-42", "pause", "initialize", WORKER_ID, "169.254"):
        assert leaked not in dump, leaked
    # One read of /task and /task/stats serves every instrument of the export.
    assert fake.calls == [f"{BASE}/task", f"{BASE}/task/stats"]


def test_container_stats_failures_report_nothing(metrics: InMemoryMetricReader) -> None:
    fake = FakeMetadata(fail=True)
    container_stats.register({container_stats.METADATA_ENV: BASE}, fetch=fake)
    assert collect(metrics) == {}  # no exception reaches the reader or the worker
    assert len(fake.calls) == 1  # no retry loop
    assert container_stats.parse({"Containers": "junk"}, {}) == {}
    assert container_stats.parse({}, {"x": 1}) == {}


def test_container_stats_off_outside_ecs_and_fail_closed() -> None:
    assert container_stats.register({}) is None
    assert container_stats.metadata_url({container_stats.METADATA_ENV: BASE + "/"}) == BASE
    for bad in (
        "https://169.254.170.2/v4/x",
        "http://metadata.example.test/v4/x",
        "http://8.8.8.8/v4",
    ):
        with pytest.raises(telemetry.TelemetryConfigError):
            container_stats.metadata_url({container_stats.METADATA_ENV: bad})


class _Metadata(BaseHTTPRequestHandler):
    def do_GET(self) -> None:  # noqa: N802 - stdlib name
        body = json.dumps(task() if self.path.endswith("/task") else stats()).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args: Any) -> None:
        pass


def test_metadata_reads_make_no_apm_spans() -> None:
    """The reads run every export; they must not use the APM span cap (OBS-13)."""
    spans = InMemorySpanExporter()
    handle = telemetry.configure(
        SETTINGS,
        service_name="aorte-test",
        span_exporter=spans,
        metric_reader=InMemoryMetricReader(),
        log_exporter=InMemoryLogRecordExporter(),
    )
    server = ThreadingHTTPServer(("127.0.0.1", 0), _Metadata)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        reader = container_stats.TaskStats(f"http://127.0.0.1:{server.server_port}/v4/x")
        assert set(reader.samples()) == {"worker", "odoo"}
        handle.force_flush()
        assert spans.get_finished_spans() == ()
    finally:
        server.shutdown()
        handle.shutdown()
