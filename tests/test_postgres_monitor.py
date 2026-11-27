"""#166, OBS-22: the PostgreSQL monitoring role reads metrics only.

The role check runs against the throwaway test database (local-test-db skill); it creates
the cluster role `aorte_monitor` and drops it again.
"""

from __future__ import annotations

import os
from collections.abc import Iterator

import psycopg
import pytest
from psycopg import sql

from engine.full_stack_init import MONITOR_ROLE, configure_monitor_role, monitor_passwords

URL = os.environ.get("TEST_DATABASE_URL", "")
if not URL and os.environ.get("REQUIRE_DB_TESTS") == "1":
    raise RuntimeError("REQUIRE_DB_TESTS=1 but TEST_DATABASE_URL is not set")

ENV = {
    "FULL_STACK_SYNTHETIC_ISOLATED": "YES",
    "FULL_STACK_ODOO_DB_PASSWORD": "admin-example",
    "FULL_STACK_ODOO_RO_PASSWORD": "reader-example",
    "ENGINE_DATABASE_URL": "postgresql://engine:synthetic@engine-db:5432/engine",
}
EMAIL = "layla.haddad@example.test"


def test_monitor_passwords_both_or_neither_and_distinct() -> None:
    assert monitor_passwords(ENV) is None  # local Compose: no monitoring role
    both = {
        **ENV,
        "FULL_STACK_ENGINE_MONITOR_PASSWORD": "engine-monitor-example",
        "FULL_STACK_ODOO_MONITOR_PASSWORD": "odoo-monitor-example",
    }
    assert monitor_passwords(both) == ("engine-monitor-example", "odoo-monitor-example")
    for key, value in (
        ("FULL_STACK_ODOO_MONITOR_PASSWORD", ""),  # only one set
        ("FULL_STACK_ODOO_MONITOR_PASSWORD", "engine-monitor-example"),  # same twice
        ("FULL_STACK_ODOO_MONITOR_PASSWORD", "reader-example"),  # reuses the reader's
        ("FULL_STACK_ENGINE_MONITOR_PASSWORD", "synthetic"),  # reuses the engine's
        ("FULL_STACK_ENGINE_MONITOR_PASSWORD", "admin-example"),  # reuses the admin's
    ):
        with pytest.raises(ValueError):
            monitor_passwords({**both, key: value})


@pytest.fixture
def admin() -> Iterator[psycopg.Connection]:
    with psycopg.connect(URL, autocommit=True) as conn:
        conn.execute("CREATE TABLE IF NOT EXISTS monitor_probe (note text)")
        conn.execute("INSERT INTO monitor_probe VALUES (%s)", (EMAIL,))
        try:
            yield conn
        finally:
            conn.execute("DROP TABLE IF EXISTS monitor_probe")
            if conn.execute(
                "SELECT 1 FROM pg_roles WHERE rolname = %s", (MONITOR_ROLE,)
            ).fetchone():
                role = sql.Identifier(MONITOR_ROLE)
                conn.execute(
                    sql.SQL("REVOKE CONNECT ON DATABASE {} FROM {}").format(
                        sql.Identifier(conn.info.dbname), role
                    )
                )
                conn.execute(sql.SQL("DROP ROLE {}").format(role))


@pytest.mark.skipif(not URL, reason="TEST_DATABASE_URL not set")
def test_monitor_role_reads_metrics_only(admin: psycopg.Connection) -> None:
    """OBS-22: LOGIN and CONNECT only; no pg_monitor, no table access, no others' SQL text."""
    row = admin.execute("SELECT current_database()").fetchone()
    assert row is not None
    database = row[0]

    def connect() -> psycopg.Connection:
        return psycopg.connect(URL)

    configure_monitor_role(connect, database, "monitor-example-1")
    configure_monitor_role(connect, database, "monitor-example-2")  # idempotent; new password

    attributes = admin.execute(
        "SELECT rolcanlogin, rolsuper, rolcreatedb, rolcreaterole, rolreplication, "
        "rolbypassrls, rolconnlimit FROM pg_roles WHERE rolname = %s",
        (MONITOR_ROLE,),
    ).fetchone()
    assert attributes == (True, False, False, False, False, False, 3)
    memberships = admin.execute(
        "SELECT count(*) FROM pg_auth_members m JOIN pg_roles r ON r.oid = m.member "
        "WHERE r.rolname = %s",
        (MONITOR_ROLE,),
    ).fetchone()
    assert memberships == (0,)  # not pg_monitor, not pg_read_all_stats, nothing
    privileges = admin.execute(
        "SELECT has_database_privilege(%s, current_database(), 'CONNECT'), "
        "has_table_privilege(%s, 'monitor_probe', 'SELECT')",
        (MONITOR_ROLE, MONITOR_ROLE),
    ).fetchone()
    assert privileges == (True, False)

    # As the role: the three metrics are readable; tables and other sessions' SQL are not.
    admin.execute("SELECT note FROM monitor_probe WHERE note = %s", (EMAIL,))
    with psycopg.connect(URL, user=MONITOR_ROLE, password="monitor-example-2") as monitor:
        size = monitor.execute("SELECT pg_database_size(current_database())").fetchone()
        assert size is not None and size[0] > 0
        backends = monitor.execute(
            "SELECT numbackends FROM pg_stat_database WHERE datname = current_database()"
        ).fetchone()
        assert backends is not None and backends[0] >= 2
        texts = monitor.execute(
            "SELECT query FROM pg_stat_activity WHERE usename IS NOT NULL AND usename <> %s",
            (MONITOR_ROLE,),
        ).fetchall()
        assert texts and all(text == ("<insufficient privilege>",) for text in texts)
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            monitor.execute("SELECT note FROM monitor_probe")
