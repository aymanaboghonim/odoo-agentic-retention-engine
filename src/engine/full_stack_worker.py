from __future__ import annotations

import argparse
import logging
import os
import signal
import threading
import uuid
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime

from sqlalchemy import Engine
from sqlalchemy.engine import make_url
from sqlalchemy.exc import ArgumentError

from engine.config import EngineConfig
from engine.db import create_engine
from engine.odoo import OdooReader
from engine.privacy import CONSENT_CHANNELS, ConsentChannel, PrivacySettings
from engine.synthetic_scan import ErpCartScanner, SyntheticCartScanner
from engine.worker import ScanPage, run_scheduled_scans

LOG = logging.getLogger(__name__)


@dataclass(frozen=True)
class WorkerSettings:
    """Validate expected service names; Compose must separately enforce isolation."""

    engine_url: str = field(repr=False)
    odoo_url: str = field(repr=False)
    instance_id: str
    page_size: int
    interval_seconds: int
    scan_mode: str = field(default="synthetic", repr=False)
    erp: ErpScanSettings | None = field(default=None, repr=False)

    @classmethod
    def from_env(cls, env: Mapping[str, str]) -> WorkerSettings:
        if env.get("FULL_STACK_SYNTHETIC_ISOLATED") != "YES":
            raise ValueError("FULL_STACK_SYNTHETIC_ISOLATED=YES is required")
        engine_url = EngineConfig.from_env(env).database_url
        odoo_url = env.get("ODOO_READONLY_DATABASE_URL", "")
        for raw, host, user, database in (
            (engine_url, "engine-db", "engine", "engine"),
            (odoo_url, "odoo-db", "odoo_ro", "odoo"),
        ):
            try:
                url = make_url(raw)
            except ArgumentError:
                raise ValueError(f"{host} database URL is invalid") from None
            if (
                url.drivername not in {"postgresql", "postgresql+psycopg"}
                or url.host != host
                or url.username != user
                or not url.password
                or url.database != database
                or url.port not in {None, 5432}
                or url.query
            ):
                raise ValueError(f"{host} must use the expected database name and role")
        try:
            instance_id = str(uuid.UUID(env["FULL_STACK_INSTANCE_ID"]))
            page_size = int(env.get("FULL_STACK_SCAN_PAGE_SIZE", "50"))
            interval = int(env.get("FULL_STACK_SCAN_INTERVAL_SECONDS", "60"))
        except (KeyError, ValueError) as error:
            raise ValueError("invalid full-stack instance ID or scan settings") from error
        if not 1 <= page_size <= 1000 or not 1 <= interval <= 86400:
            raise ValueError("full-stack scan bounds are invalid")
        mode = env.get("SCAN_MODE", "synthetic").strip() or "synthetic"
        if mode not in {"synthetic", "erp"}:
            raise ValueError("SCAN_MODE must be synthetic or erp")
        erp = ErpScanSettings.from_env(env) if mode == "erp" else None
        return cls(engine_url, odoo_url, instance_id, page_size, interval, mode, erp)


@dataclass(frozen=True)
class ErpScanSettings:
    """Q12 settings required before any generalized ERP scan; no defaults (fail closed)."""

    privacy: PrivacySettings
    channel: ConsentChannel
    cooldown_days: int

    @classmethod
    def from_env(cls, env: Mapping[str, str]) -> ErpScanSettings:
        channel = env.get("ERP_SCAN_CHANNEL", "").strip()
        if channel not in CONSENT_CHANNELS:
            raise ValueError("ERP_SCAN_CHANNEL must be email or telegram")
        try:
            cooldown = int(env["CAMPAIGN_COOLDOWN_DAYS"])
        except (KeyError, ValueError) as error:
            raise ValueError("CAMPAIGN_COOLDOWN_DAYS must be set to an integer") from error
        if cooldown < 1:
            raise ValueError("CAMPAIGN_COOLDOWN_DAYS must be at least 1")
        return cls(
            PrivacySettings.from_env(env),
            "email" if channel == "email" else "telegram",
            cooldown,
        )


class _ErpScanPage:
    """Adapt the ERP scanner to the shared loop; logs only counts, never customer data."""

    def __init__(self, scanner: ErpCartScanner) -> None:
        self._scanner = scanner

    def scan_page(self, *, now: datetime) -> tuple[int, int]:
        result = self._scanner.scan_page(now=now)
        LOG.info(
            "stage=erp-scan skipped=%s",
            ",".join(f"{reason}:{count}" for reason, count in result.skipped.items()),
        )
        return result.scanned, result.created


def build_scanner(settings: WorkerSettings, engine: Engine, reader: OdooReader) -> ScanPage:
    if settings.scan_mode == "erp":
        erp = settings.erp
        if erp is None:
            raise ValueError("ERP scan settings are missing")
        return _ErpScanPage(
            ErpCartScanner(
                engine,
                reader,
                erp.privacy,
                instance_id=settings.instance_id,
                page_size=settings.page_size,
                channel=erp.channel,
                cooldown_days=erp.cooldown_days,
            )
        )
    return SyntheticCartScanner(
        engine, reader, instance_id=settings.instance_id, page_size=settings.page_size
    )


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description="Scan review carts (SCAN_MODE=synthetic|erp)")
    parser.add_argument(
        "--once", action="store_true", help="scan exactly one bounded page and exit"
    )
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    settings = WorkerSettings.from_env(os.environ)
    reader = OdooReader(settings.odoo_url)
    if settings.scan_mode == "erp":
        reader.verify_readonly_role()
    else:
        reader.verify_synthetic_readonly_role()
    engine = create_engine(settings.engine_url)
    stop = threading.Event()
    for sig in (signal.SIGINT, signal.SIGTERM):
        signal.signal(sig, lambda _signal, _frame: stop.set())
    try:
        scanner = build_scanner(settings, engine, reader)
        if args.once:
            scanned, created = scanner.scan_page(now=datetime.now(UTC))
            logging.info("stage=scan status=completed scanned=%d created=%d", scanned, created)
        else:
            run_scheduled_scans(scanner, stop=stop, interval_seconds=settings.interval_seconds)
    finally:
        engine.dispose()


if __name__ == "__main__":
    main()
