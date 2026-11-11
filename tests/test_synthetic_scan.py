from __future__ import annotations

import os
import threading
import uuid
from collections.abc import Iterator
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime
from decimal import Decimal
from unittest.mock import MagicMock, patch

import pytest
import sqlalchemy as sa

from engine.config import normalize_database_url
from engine.db import create_engine, run_events, runs, upgrade_schema
from engine.odoo import AbandonedCart, OdooConfigError, OdooReader
from engine.synthetic_scan import SyntheticCartScanner
from engine.worker import run_scheduled_scans

NOW = datetime(2026, 11, 8, tzinfo=UTC)
URL = os.environ.get("TEST_DATABASE_URL", "")


def cart(order_id: int) -> AbandonedCart:
    return AbandonedCart(
        order_id,
        f"SO{order_id}",
        100 + order_id,
        "Synthetic",
        "synthetic@example.test",
        "en_US",
        "USD",
        Decimal("10"),
        NOW,
        (),
    )


class FixtureReader:
    def __init__(self, carts: list[AbandonedCart]) -> None:
        self.carts = carts

    def scan_synthetic_review_carts(
        self, *, now: datetime, after_id: int, limit: int
    ) -> list[AbandonedCart]:
        assert now == NOW
        return [item for item in self.carts if item.order_id > after_id][:limit]


@pytest.fixture
def database() -> Iterator[tuple[sa.Engine, str]]:
    if not URL:
        if os.environ.get("REQUIRE_DB_TESTS") == "1":
            pytest.fail("REQUIRE_DB_TESTS=1 but TEST_DATABASE_URL is not set")
        pytest.skip("TEST_DATABASE_URL not set")
    engine = create_engine(normalize_database_url(URL))
    upgrade_schema(engine)
    instance_id = str(uuid.uuid4())
    yield engine, instance_id
    with engine.begin() as conn:
        selected = sa.select(runs.c.id).where(
            runs.c.idempotency_key.like(f"synthetic-scan:{instance_id}:%")
        )
        conn.execute(run_events.delete().where(run_events.c.run_id.in_(selected)))
        conn.execute(runs.delete().where(runs.c.id.in_(selected)))
        conn.execute(
            sa.text("DELETE FROM synthetic_scan_cursors WHERE id=:id"), {"id": instance_id}
        )
    engine.dispose()


def test_synthetic_reader_limits_queries_to_matching_review_fixtures() -> None:
    reader = OdooReader("postgresql://unused")
    connection = MagicMock()
    connection.__enter__.return_value = connection
    connection.execute.return_value.fetchall.return_value = []
    with patch.object(reader, "_connect", return_value=connection):
        assert reader.scan_synthetic_review_carts(now=NOW, after_id=7, limit=3) == []
    query, parameters = connection.execute.call_args.args
    assert "so.client_order_ref ~ '^retention-review-[0-9a-f]{12}$'" in query
    assert "p.email = so.client_order_ref || '@example.test'" in query
    assert "so.id > %(after_id)s" in query
    assert "ORDER BY so.id" in query
    assert parameters == {"now": NOW.replace(tzinfo=None), "after_id": 7, "limit": 3}


def test_synthetic_reload_uses_the_same_marker_and_current_eligibility() -> None:
    reader = OdooReader("postgresql://unused")
    connection = MagicMock()
    connection.__enter__.return_value = connection
    connection.execute.return_value.fetchone.return_value = None
    with patch.object(reader, "_connect", return_value=connection):
        assert reader.get_synthetic_review_cart(7, now=NOW) is None
    query, parameters = connection.execute.call_args.args
    assert "so.client_order_ref ~ '^retention-review-[0-9a-f]{12}$'" in query
    assert "p.email = so.client_order_ref || '@example.test'" in query
    assert "so.date_order <= %(now)s - w.cart_abandoned_delay" in query
    assert "so.id = %(order_id)s" in query
    assert parameters == {"order_id": 7, "now": NOW.replace(tzinfo=None)}


def test_scan_paginates_past_seen_carts(database: tuple[sa.Engine, str]) -> None:
    engine, instance_id = database
    reader = FixtureReader([cart(i) for i in range(1, 6)])
    scanner = SyntheticCartScanner(engine, reader, instance_id=instance_id, page_size=2)
    assert scanner.scan_page(now=NOW) == (2, 2)
    restarted = SyntheticCartScanner(engine, reader, instance_id=instance_id, page_size=2)
    assert restarted.scan_page(now=NOW) == (2, 2)
    assert restarted.scan_page(now=NOW) == (1, 1)
    assert restarted.scan_page(now=NOW) == (2, 0)
    assert restarted.scan_page(now=NOW) == (2, 0)
    assert restarted.scan_page(now=NOW) == (1, 0)


def test_repeated_scans_create_one_run(database: tuple[sa.Engine, str]) -> None:
    engine, instance_id = database
    reader = FixtureReader([cart(1)])
    scanner = SyntheticCartScanner(engine, reader, instance_id=instance_id, page_size=2)
    assert scanner.scan_page(now=NOW) == (1, 1)

    restarted = SyntheticCartScanner(engine, reader, instance_id=instance_id, page_size=2)
    assert restarted.scan_page(now=NOW) == (1, 0)
    with engine.connect() as conn:
        count = conn.scalar(
            sa.select(sa.func.count())
            .select_from(runs)
            .where(runs.c.idempotency_key == f"synthetic-scan:{instance_id}:1")
        )
    assert count == 1


def test_concurrent_scans_create_one_run(database: tuple[sa.Engine, str]) -> None:
    engine, instance_id = database
    reader = FixtureReader([cart(1)])
    scanner = SyntheticCartScanner(engine, reader, instance_id=instance_id, page_size=2)
    with ThreadPoolExecutor(max_workers=12) as executor:
        results = list(executor.map(lambda _: scanner.scan_page(now=NOW), range(12)))
    assert sum(created for _, created in results) == 1
    with engine.connect() as conn:
        records = conn.execute(
            sa.select(runs.c.trigger_type, runs.c.trigger_ref).where(
                runs.c.idempotency_key == f"synthetic-scan:{instance_id}:1"
            )
        ).all()
    assert records == [("synthetic", "101:1")]


def test_failed_page_rolls_back_runs_and_cursor(database: tuple[sa.Engine, str]) -> None:
    engine, instance_id = database
    scanner = SyntheticCartScanner(
        engine, FixtureReader([cart(2), cart(1)]), instance_id=instance_id, page_size=2
    )
    with pytest.raises(ValueError, match="unordered"):
        scanner.scan_page(now=NOW)
    with engine.connect() as conn:
        count = conn.scalar(
            sa.select(sa.func.count())
            .select_from(runs)
            .where(runs.c.idempotency_key.like(f"synthetic-scan:{instance_id}:%"))
        )
    assert count == 0
    scanner = SyntheticCartScanner(
        engine, FixtureReader([cart(1), cart(2)]), instance_id=instance_id, page_size=2
    )
    assert scanner.scan_page(now=NOW) == (2, 2)


def test_reader_rejects_bad_scan_bounds() -> None:
    reader = OdooReader("postgresql://unused")
    with pytest.raises(ValueError, match="cursor or page limit"):
        reader.scan_synthetic_review_carts(now=NOW, after_id=-1, limit=1)
    with pytest.raises(ValueError, match="cursor or page limit"):
        reader.scan_synthetic_review_carts(now=NOW, after_id=0, limit=1001)


def test_synthetic_reader_rejects_elevated_database_role() -> None:
    reader = OdooReader("postgresql://unused")
    connection = MagicMock()
    connection.__enter__.return_value = connection
    with patch.object(reader, "_connect", return_value=connection):
        connection.execute.return_value.fetchone.return_value = {
            "rolsuper": False,
            "table_write": False,
        }
        reader.verify_synthetic_readonly_role()
        connection.execute.return_value.fetchone.return_value["table_write"] = True
        with pytest.raises(OdooConfigError, match="unprivileged read-only"):
            reader.verify_synthetic_readonly_role()
        connection.execute.return_value.fetchone.return_value = None
        with pytest.raises(OdooConfigError, match="unprivileged read-only"):
            reader.verify_synthetic_readonly_role()


def test_synthetic_role_check_rejects_engine_database_owner(
    database: tuple[sa.Engine, str],
) -> None:
    with pytest.raises(OdooConfigError, match="unprivileged read-only"):
        OdooReader(URL).verify_synthetic_readonly_role()


def test_scheduled_scan_stops_without_external_effects() -> None:
    stop = threading.Event()
    scanner = MagicMock()

    def stop_after_scan(*, now: datetime) -> tuple[int, int]:
        stop.set()
        return 0, 0

    scanner.scan_page.side_effect = stop_after_scan
    run_scheduled_scans(scanner, stop=stop, interval_seconds=60)
    assert scanner.scan_page.call_count == 1
    assert scanner.scan_page.call_args.kwargs["now"].tzinfo is not None


def test_scheduled_scan_fails_explicitly() -> None:
    scanner = MagicMock()
    scanner.scan_page.side_effect = RuntimeError("database unavailable")
    with pytest.raises(RuntimeError, match="database unavailable"):
        run_scheduled_scans(scanner, stop=threading.Event(), interval_seconds=1)
    with pytest.raises(ValueError, match="interval"):
        run_scheduled_scans(scanner, stop=threading.Event(), interval_seconds=float("nan"))
