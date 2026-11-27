from __future__ import annotations

import os
import time
from collections.abc import Callable, Mapping
from typing import Any

import psycopg
from psycopg import sql
from sqlalchemy.engine import make_url

from engine.config import EngineConfig
from engine.db import create_engine, upgrade_schema
from engine.odoo import OdooReader


def validate(env: Mapping[str, str]) -> tuple[str, str, str]:
    if env.get("FULL_STACK_SYNTHETIC_ISOLATED") != "YES":
        raise ValueError("FULL_STACK_SYNTHETIC_ISOLATED=YES is required")
    admin_password = env.get("FULL_STACK_ODOO_DB_PASSWORD", "")
    reader_password = env.get("FULL_STACK_ODOO_RO_PASSWORD", "")
    if not admin_password or not reader_password or admin_password == reader_password:
        raise ValueError(
            "full-stack Odoo administrator and reader require distinct nonempty passwords"
        )
    engine_url = EngineConfig.from_env(env).database_url
    parsed = make_url(engine_url)
    if (
        parsed.host != "engine-db"
        or parsed.database != "engine"
        or parsed.username != "engine"
        or not parsed.password
        or parsed.port not in {None, 5432}
        or parsed.query
    ):
        raise ValueError("engine database must use the dedicated Compose service")
    return admin_password, reader_password, engine_url


MONITOR_ROLE = "aorte_monitor"
MONITOR_KEYS = ("FULL_STACK_ENGINE_MONITOR_PASSWORD", "FULL_STACK_ODOO_MONITOR_PASSWORD")


def monitor_passwords(env: Mapping[str, str]) -> tuple[str, str] | None:
    """#166: passwords of the PostgreSQL monitoring role, or None when not configured.

    Both or neither: AWS always sets both (SSM, INF-5); local Compose may leave both unset.
    Each must be distinct from every other database password.
    """
    engine_pw, odoo_pw = (env.get(key, "") for key in MONITOR_KEYS)
    if not engine_pw and not odoo_pw:
        return None
    others = {
        env.get("FULL_STACK_ODOO_DB_PASSWORD", ""),
        env.get("FULL_STACK_ODOO_RO_PASSWORD", ""),
        make_url(EngineConfig.from_env(env).database_url).password or "",
    }
    if not engine_pw or not odoo_pw or engine_pw == odoo_pw or {engine_pw, odoo_pw} & others:
        raise ValueError(
            "monitoring role requires two distinct nonempty passwords, unlike the others"
        )
    return engine_pw, odoo_pw


# Odoo is ready for the read-only role only when nothing is still being installed: modules
# installed after website_sale (website_sale_loyalty in AWS, ADR-0019) keep creating tables,
# and a concurrent GRANT then fails with "tuple concurrently updated" (#97).
PENDING_MODULE_STATES = ("to install", "to upgrade", "to remove")
RETRYABLE_ROLE_ERRORS = (
    psycopg.errors.InternalError_,
    psycopg.errors.DeadlockDetected,
    psycopg.errors.SerializationFailure,
)


def odoo_ready(conn: Any) -> bool:
    ready = conn.execute("SELECT to_regclass('public.ir_module_module') IS NOT NULL").fetchone()
    if not (ready and ready[0]):
        return False
    installed = conn.execute(
        "SELECT state = 'installed' FROM ir_module_module WHERE name = 'website_sale'"
    ).fetchone()
    if not (installed and installed[0]):
        return False
    pending = conn.execute(
        "SELECT count(*) FROM ir_module_module WHERE state = ANY(%s)",
        (list(PENDING_MODULE_STATES),),
    ).fetchone()
    return bool(pending) and pending[0] == 0


def configure_readonly_role(
    connect: Callable[[], Any],
    reader_password: str,
    *,
    attempts: int = 5,
    sleep: Callable[[float], None] = time.sleep,
) -> None:
    """Create odoo_ro in one transaction; retry only on transient catalog conflicts."""
    for attempt in range(1, attempts + 1):
        try:
            with connect() as conn:
                exists = conn.execute("SELECT 1 FROM pg_roles WHERE rolname='odoo_ro'").fetchone()
                if not exists:
                    conn.execute(
                        sql.SQL(
                            "CREATE ROLE odoo_ro LOGIN PASSWORD {} "
                            "NOSUPERUSER NOCREATEDB NOCREATEROLE"
                        ).format(sql.Literal(reader_password))
                    )
                    conn.execute("REVOKE CREATE ON SCHEMA public FROM PUBLIC")
                    conn.execute("GRANT CONNECT ON DATABASE odoo TO odoo_ro")
                    conn.execute("GRANT USAGE ON SCHEMA public TO odoo_ro")
                    conn.execute("GRANT SELECT ON ALL TABLES IN SCHEMA public TO odoo_ro")
                    conn.execute(
                        "ALTER DEFAULT PRIVILEGES FOR ROLE odoo IN SCHEMA public "
                        "GRANT SELECT ON TABLES TO odoo_ro"
                    )
            return
        except RETRYABLE_ROLE_ERRORS as error:
            if attempt == attempts:
                raise
            print(
                f"stage=configure-readonly-role retry={attempt} reason={type(error).__name__}",
                flush=True,
            )
            sleep(2.0 * attempt)


def configure_monitor_role(
    connect: Callable[[], Any],
    database: str,
    password: str,
    *,
    attempts: int = 5,
    sleep: Callable[[float], None] = time.sleep,
) -> None:
    """#166: create or update aorte_monitor with LOGIN and CONNECT on one database only.

    No pg_monitor and no other role membership (owner, 2026-11-27): the role reads up,
    connections, and database size, cannot read tables, and sees other sessions' SQL text
    as "<insufficient privilege>". The password follows SSM on every start.
    """
    options = sql.SQL(
        "LOGIN PASSWORD {} NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION NOBYPASSRLS "
        "CONNECTION LIMIT 3"
    ).format(sql.Literal(password))
    for attempt in range(1, attempts + 1):
        try:
            with connect() as conn:
                exists = conn.execute(
                    "SELECT 1 FROM pg_roles WHERE rolname = %s", (MONITOR_ROLE,)
                ).fetchone()
                verb = "ALTER ROLE {} WITH " if exists else "CREATE ROLE {} "
                conn.execute(sql.SQL(verb).format(sql.Identifier(MONITOR_ROLE)) + options)
                conn.execute(
                    sql.SQL("GRANT CONNECT ON DATABASE {} TO {}").format(
                        sql.Identifier(database), sql.Identifier(MONITOR_ROLE)
                    )
                )
                member_of = conn.execute(
                    "SELECT count(*) FROM pg_auth_members m JOIN pg_roles r ON r.oid = m.member "
                    "WHERE r.rolname = %s",
                    (MONITOR_ROLE,),
                ).fetchone()
                if member_of is None or member_of[0] != 0:
                    raise RuntimeError(f"{MONITOR_ROLE} must not be a member of any role")
            return
        except RETRYABLE_ROLE_ERRORS as error:
            if attempt == attempts:
                raise
            print(
                f"stage=configure-monitor-role retry={attempt} reason={type(error).__name__}",
                flush=True,
            )
            sleep(2.0 * attempt)


def main() -> None:
    admin_password, reader_password, engine_url = validate(os.environ)
    monitor = monitor_passwords(os.environ)
    started = time.monotonic()
    deadline = started + 600
    next_heartbeat = 0.0
    print("stage=wait-for-odoo status=started", flush=True)
    while True:
        now = time.monotonic()
        if now >= deadline:
            raise TimeoutError("Odoo database did not finish website initialization")
        if now >= next_heartbeat:
            print(f"stage=wait-for-odoo elapsed_s={now - started:.0f}", flush=True)
            next_heartbeat = now + 30
        try:
            with psycopg.connect(
                host="odoo-db",
                dbname="odoo",
                user="odoo",
                password=admin_password,
                connect_timeout=3,
            ) as conn:
                if odoo_ready(conn):
                    break
        except psycopg.OperationalError:
            pass
        time.sleep(2)
    print("stage=configure-readonly-role status=started", flush=True)
    configure_readonly_role(
        lambda: psycopg.connect(
            host="odoo-db", dbname="odoo", user="odoo", password=admin_password
        ),
        reader_password,
    )
    reader_url = psycopg.conninfo.make_conninfo(
        host="odoo-db", dbname="odoo", user="odoo_ro", password=reader_password
    )
    OdooReader(reader_url).verify_synthetic_readonly_role()
    if monitor is not None:
        print("stage=configure-monitor-roles status=started", flush=True)
        engine_monitor, odoo_monitor = monitor
        configure_monitor_role(
            lambda: psycopg.connect(
                host="odoo-db", dbname="odoo", user="odoo", password=admin_password
            ),
            "odoo",
            odoo_monitor,
        )
        engine_parts = make_url(engine_url)
        configure_monitor_role(
            lambda: psycopg.connect(
                host="engine-db",
                dbname="engine",
                user="engine",
                password=engine_parts.password,
            ),
            "engine",
            engine_monitor,
        )
    print("stage=migrate-engine status=started", flush=True)
    engine = create_engine(engine_url)
    try:
        upgrade_schema(engine)
    finally:
        engine.dispose()
    elapsed = time.monotonic() - started
    print(f"stage=initialize status=complete elapsed_s={elapsed:.0f}", flush=True)


if __name__ == "__main__":
    main()
