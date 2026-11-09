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

from sqlalchemy.engine import make_url
from sqlalchemy.exc import ArgumentError

from engine.config import EngineConfig
from engine.db import create_engine
from engine.odoo import OdooReader
from engine.synthetic_scan import SyntheticCartScanner
from engine.worker import run_scheduled_scans


@dataclass(frozen=True)
class WorkerSettings:
    """Validate expected service names; Compose must separately enforce isolation."""

    engine_url: str = field(repr=False)
    odoo_url: str = field(repr=False)
    instance_id: str
    page_size: int
    interval_seconds: int

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
        return cls(engine_url, odoo_url, instance_id, page_size, interval)


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description="Scan synthetic review carts")
    parser.add_argument(
        "--once", action="store_true", help="scan exactly one bounded page and exit"
    )
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    settings = WorkerSettings.from_env(os.environ)
    reader = OdooReader(settings.odoo_url)
    reader.verify_synthetic_readonly_role()
    engine = create_engine(settings.engine_url)
    stop = threading.Event()
    for sig in (signal.SIGINT, signal.SIGTERM):
        signal.signal(sig, lambda _signal, _frame: stop.set())
    try:
        scanner = SyntheticCartScanner(
            engine, reader, instance_id=settings.instance_id, page_size=settings.page_size
        )
        if args.once:
            scanned, created = scanner.scan_page(now=datetime.now(UTC))
            logging.info("stage=scan status=completed scanned=%d created=%d", scanned, created)
        else:
            run_scheduled_scans(scanner, stop=stop, interval_seconds=settings.interval_seconds)
    finally:
        engine.dispose()


if __name__ == "__main__":
    main()
