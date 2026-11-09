from __future__ import annotations

import os
import time
from collections.abc import Mapping

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


def main() -> None:
    admin_password, reader_password, engine_url = validate(os.environ)
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
                ready = conn.execute(
                    "SELECT to_regclass('public.ir_module_module') IS NOT NULL"
                ).fetchone()
                if ready and ready[0]:
                    installed = conn.execute(
                        "SELECT state = 'installed' FROM ir_module_module "
                        "WHERE name = 'website_sale'"
                    ).fetchone()
                    if installed and installed[0]:
                        break
        except psycopg.OperationalError:
            pass
        time.sleep(2)
    print("stage=configure-readonly-role status=started", flush=True)
    with psycopg.connect(
        host="odoo-db", dbname="odoo", user="odoo", password=admin_password
    ) as conn:
        exists = conn.execute("SELECT 1 FROM pg_roles WHERE rolname='odoo_ro'").fetchone()
        if not exists:
            conn.execute(
                sql.SQL(
                    "CREATE ROLE odoo_ro LOGIN PASSWORD {} NOSUPERUSER NOCREATEDB NOCREATEROLE"
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
    reader_url = psycopg.conninfo.make_conninfo(
        host="odoo-db", dbname="odoo", user="odoo_ro", password=reader_password
    )
    OdooReader(reader_url).verify_synthetic_readonly_role()
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
