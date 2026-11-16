from __future__ import annotations

import logging
import math
import threading
import time
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Protocol

from engine.channels import ChannelError, TelegramMessageSender
from engine.runtime_state import RuntimeGuard, RuntimeState

S4_LIVE_TEST_MESSAGE = "S4 channel verification: synthetic test only. No customer data."
LOG = logging.getLogger(__name__)


class ScanPage(Protocol):
    def scan_page(self, *, now: datetime) -> tuple[int, int]: ...


class GuardedScan:
    """Spec KS-3: a halted engine creates no runs. Reading the state fails closed."""

    def __init__(self, scanner: ScanPage, guard: RuntimeGuard) -> None:
        self._scanner = scanner
        self._guard = guard

    def scan_page(self, *, now: datetime) -> tuple[int, int]:
        if self._guard.state() is RuntimeState.HALTED:
            LOG.info("stage=scan status=skipped reason=engine_halted")
            return 0, 0
        return self._scanner.scan_page(now=now)


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
