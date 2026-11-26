"""Per-container metrics from the ECS task metadata endpoint v4 (ADR-0033, OBS-20).

The worker reads `${ECS_CONTAINER_METADATA_URI_V4}/task` (names, restart counts) and
`/task/stats` (Docker stats of every container in the task, keyed by Docker ID) when metrics
are exported. No Docker socket and no IAM permission (F-099). Outside ECS the variable is not
set and nothing is registered. A failed read reports nothing for that export; it never raises
into the worker and never retries in a loop.

Only containers named in `telemetry.CONTAINERS` are reported (closed label set, OBS-8).
"""

from __future__ import annotations

import ipaddress
import json
import threading
import time
import urllib.request
from collections.abc import Callable, Iterator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlsplit

from engine import telemetry

METADATA_ENV = "ECS_CONTAINER_METADATA_URI_V4"
TIMEOUT_SECONDS = 2.0
MAX_AGE_SECONDS = 15.0  # one read serves every instrument of one export
MAX_BYTES = 1_000_000

Fetch = Callable[[str], Any]


@dataclass(frozen=True)
class Sample:
    cpu_seconds: float
    memory_bytes: int
    rx_bytes: int
    tx_bytes: int
    restarts: int | None  # only with a restart policy (F-099)


def metadata_url(env: Mapping[str, str]) -> str | None:
    """The endpoint from ECS, or None outside ECS. A malformed value fails closed."""
    value = env.get(METADATA_ENV, "")
    if not value:
        return None
    parts = urlsplit(value)
    try:
        host = ipaddress.ip_address(parts.hostname or "")
    except ValueError:
        host = None
    if parts.scheme != "http" or host is None or not (host.is_link_local or host.is_private):
        raise telemetry.TelemetryConfigError(f"{METADATA_ENV} must be the local ECS endpoint")
    return value.rstrip("/")


@contextmanager
def _no_apm() -> Iterator[None]:
    """These reads are not engine work: keep them out of APM spans and the APM cap."""
    try:
        from opentelemetry.instrumentation.utils import suppress_instrumentation
    except ImportError:  # instrumentation not installed: nothing to suppress
        yield
        return
    with suppress_instrumentation():
        yield


def _fetch(url: str) -> Any:
    with _no_apm(), urllib.request.urlopen(url, timeout=TIMEOUT_SECONDS) as response:  # noqa: S310
        return json.loads(response.read(MAX_BYTES))


def _memory(stats: Mapping[str, Any]) -> int:
    """Memory in use as `docker stats` shows it: usage minus inactive page cache."""
    usage = int(stats.get("usage") or 0)
    detail = stats.get("stats") or {}
    cache = detail.get("inactive_file", detail.get("total_inactive_file", 0))  # cgroup v2, v1
    return max(usage - int(cache or 0), 0)


def _network(stats: Mapping[str, Any]) -> tuple[int, int]:
    networks = stats.get("networks") or {}
    rx = sum(int(n.get("rx_bytes") or 0) for n in networks.values())
    tx = sum(int(n.get("tx_bytes") or 0) for n in networks.values())
    return rx, tx


def parse(task: Mapping[str, Any], stats: Mapping[str, Any]) -> dict[str, Sample]:
    """Join `/task` and `/task/stats` on the Docker ID; only known, running containers."""
    out: dict[str, Sample] = {}
    containers = task.get("Containers") if isinstance(task, Mapping) else None
    if not isinstance(containers, list) or not isinstance(stats, Mapping):
        return out
    for container in containers:
        if not isinstance(container, Mapping):
            continue
        name = container.get("Name")
        if name not in telemetry.CONTAINERS or container.get("KnownStatus") != "RUNNING":
            continue
        one = stats.get(container.get("DockerId") or "")
        if not isinstance(one, Mapping):
            continue
        restarts = container.get("RestartCount")
        rx, tx = _network(one)
        out[name] = Sample(
            cpu_seconds=int(
                (one.get("cpu_stats") or {}).get("cpu_usage", {}).get("total_usage") or 0
            )
            / 1e9,
            memory_bytes=_memory(one.get("memory_stats") or {}),
            rx_bytes=rx,
            tx_bytes=tx,
            restarts=int(restarts) if isinstance(restarts, int) else None,
        )
    return out


class TaskStats:
    """Reads the task once per export and shares it between the instruments."""

    def __init__(
        self,
        base_url: str,
        *,
        fetch: Fetch = _fetch,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._base = base_url
        self._fetch = fetch
        self._clock = clock
        self._lock = threading.Lock()
        self._at: float | None = None
        self._samples: dict[str, Sample] = {}

    def samples(self) -> dict[str, Sample]:
        with self._lock:
            now = self._clock()
            if self._at is None or now - self._at >= MAX_AGE_SECONDS:
                self._at = now
                try:
                    self._samples = parse(
                        self._fetch(f"{self._base}/task"), self._fetch(f"{self._base}/task/stats")
                    )
                except Exception:  # noqa: BLE001 - a failed read reports nothing this time
                    self._samples = {}
            return self._samples


def register(env: Mapping[str, str], *, fetch: Fetch = _fetch) -> TaskStats | None:
    """Register the container instruments when running on ECS (OBS-20)."""
    url = metadata_url(env)
    if url is None:
        return None
    reader = TaskStats(url, fetch=fetch)

    def each(
        value: Callable[[Sample], float | int | None],
    ) -> list[tuple[float | int, dict[str, str]]]:
        rows = []
        for name, sample in reader.samples().items():
            v = value(sample)
            if v is not None:
                rows.append((v, {"aorte.container": name}))
        return rows

    def network() -> list[tuple[int, dict[str, str]]]:
        rows = []
        for name, sample in reader.samples().items():
            rows.append((sample.rx_bytes, {"aorte.container": name, "aorte.direction": "rx"}))
            rows.append((sample.tx_bytes, {"aorte.container": name, "aorte.direction": "tx"}))
        return rows

    telemetry.observable_counter(
        "aorte.container.cpu.time",
        "s",
        "CPU time used by the container (ECS task stats)",
        lambda: each(lambda s: s.cpu_seconds),
    )
    telemetry.observable_gauge(
        "aorte.container.memory.usage",
        "By",
        "Memory in use by the container, without inactive page cache",
        lambda: each(lambda s: s.memory_bytes),
    )
    telemetry.observable_counter(
        "aorte.container.network.io",
        "By",
        "Bytes received (rx) and sent (tx) by the container",
        network,
    )
    telemetry.observable_gauge(
        "aorte.container.restarts",
        "{restart}",
        "Restarts in place by the ECS restart policy (ADR-0033)",
        lambda: each(lambda s: s.restarts),
    )
    return reader
