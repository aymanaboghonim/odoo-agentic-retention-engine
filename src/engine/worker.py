from __future__ import annotations

import logging
import math
import threading
import time
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Protocol

from engine import telemetry
from engine.channels import ChannelError, TelegramMessageSender
from engine.runtime_state import RuntimeGuard, RuntimeState

S4_LIVE_TEST_MESSAGE = "S4 channel verification: synthetic test only. No customer data."
LOG = logging.getLogger(__name__)


class ScanPage(Protocol):
    def scan_page(self, *, now: datetime) -> tuple[int, int]: ...


class Heartbeat:
    """Seconds since the scan loop last finished a page; the worker's liveness signal."""

    def __init__(self, clock: Callable[[], float] = time.monotonic) -> None:
        self._clock = clock
        self._last = clock()  # process start counts as the first beat

    def beat(self) -> None:
        self._last = self._clock()

    def age_seconds(self) -> float:
        return max(0.0, self._clock() - self._last)


class GuardedScan:
    """Spec KS-3: a halted engine creates no runs. Reading the state fails closed."""

    def __init__(
        self, scanner: ScanPage, guard: RuntimeGuard, heartbeat: Heartbeat | None = None
    ) -> None:
        self._scanner = scanner
        self._guard = guard
        self._heartbeat = heartbeat

    def scan_page(self, *, now: datetime) -> tuple[int, int]:
        result = self._scan_page(now=now)
        if self._heartbeat is not None:
            # A halted engine is still alive: the skipped page beats too.
            self._heartbeat.beat()
        return result

    def _scan_page(self, *, now: datetime) -> tuple[int, int]:
        with telemetry.span("aorte.scan") as span:
            if self._guard.state() is RuntimeState.HALTED:
                LOG.info("stage=scan status=skipped reason=engine_halted")
                span.set("aorte.outcome", "skipped")
                return 0, 0
            scanned, created = self._scanner.scan_page(now=now)
            span.set("aorte.scan.scanned", scanned)
            span.set("aorte.scan.created", created)
            telemetry.count("aorte.scan.carts", scanned, {"aorte.outcome": "scanned"})
            telemetry.count("aorte.scan.carts", created, {"aorte.outcome": "created"})
            return scanned, created


def run_scheduled_scans(
    scanner: ScanPage,
    *,
    stop: threading.Event,
    interval_seconds: float,
    maintenance: Callable[[], None] | None = None,
) -> None:
    """Run one bounded scan page per interval until stopped; failures propagate."""
    if not math.isfinite(interval_seconds) or interval_seconds <= 0:
        raise ValueError("scan interval must be finite and positive")
    while not stop.is_set():
        started = time.monotonic()
        if maintenance is not None:
            maintenance()
        scanned, created = scanner.scan_page(now=datetime.now(UTC))
        LOG.info("stage=scan status=completed scanned=%d created=%d", scanned, created)
        next_scan = started + interval_seconds
        while not stop.is_set():
            remaining = next_scan - time.monotonic()
            if remaining <= 0:
                break
            if stop.wait(min(remaining, 30)):
                return
            if next_scan - time.monotonic() > 0:
                LOG.info("stage=scan status=waiting")


class WorkerChannelDispatcher:
    def __init__(self, telegram: TelegramMessageSender) -> None:
        self._telegram = telegram

    def send_s4_live_test(self, *, chat_id: str, approved: bool) -> int:
        if not approved:
            raise ChannelError("Explicit approval of the dedicated Telegram test chat is required.")
        if not chat_id:
            raise ChannelError("TELEGRAM_TEST_CHAT_ID must be set.")
        return self._telegram.send_message(chat_id, S4_LIVE_TEST_MESSAGE)
