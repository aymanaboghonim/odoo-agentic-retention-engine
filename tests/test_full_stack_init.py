from __future__ import annotations

import psycopg
import pytest

from engine.full_stack_init import configure_readonly_role, odoo_ready, validate

ENV = {
    "FULL_STACK_SYNTHETIC_ISOLATED": "YES",
    "FULL_STACK_ODOO_DB_PASSWORD": "admin-example",
    "FULL_STACK_ODOO_RO_PASSWORD": "reader-example",
    "ENGINE_DATABASE_URL": "postgresql://engine:synthetic@engine-db:5432/engine",
}


def test_full_stack_init_rejects_unsafe_configuration() -> None:
    assert validate(ENV)[2].endswith("@engine-db:5432/engine")
    for key, value in (
        ("FULL_STACK_SYNTHETIC_ISOLATED", "NO"),
        ("FULL_STACK_ODOO_DB_PASSWORD", ""),
        ("FULL_STACK_ODOO_RO_PASSWORD", "admin-example"),
        ("ENGINE_DATABASE_URL", "postgresql://engine:secret@remote/engine"),
        ("ENGINE_DATABASE_URL", "postgresql://other:secret@engine-db/engine"),
        ("ENGINE_DATABASE_URL", "postgresql://engine:secret@engine-db/other"),
    ):
        with pytest.raises(ValueError):
            validate({**ENV, key: value})


class _Result:
    def __init__(self, row: object) -> None:
        self.row = row

    def fetchone(self) -> object:
        return self.row


class _Conn:
    """Records SQL; answers readiness queries; can fail a GRANT a given number of times."""

    def __init__(self, rows: dict[str, object], fail_grants: list[int] | None = None) -> None:
        self.rows = rows
        self.sql: list[str] = []
        self.fail_grants = fail_grants if fail_grants is not None else [0]

    def __enter__(self) -> _Conn:
        return self

    def __exit__(self, *exc: object) -> None:
        return None

    def execute(self, query: object, params: object = None) -> _Result:
        text = str(query)
        self.sql.append(text)
        if "GRANT SELECT ON ALL TABLES" in text and self.fail_grants[0] > 0:
            self.fail_grants[0] -= 1
            raise psycopg.errors.InternalError_("tuple concurrently updated")
        for key, row in self.rows.items():
            if key in text:
                return _Result(row)
        return _Result(None)


def test_odoo_ready_waits_for_all_pending_module_installs() -> None:
    base = {"to_regclass": (True,), "name = 'website_sale'": (True,)}
    assert odoo_ready(_Conn({**base, "count(*)": (0,)})) is True
    # website_sale is installed but website_sale_loyalty is still installing (#97).
    assert odoo_ready(_Conn({**base, "count(*)": (1,)})) is False
    assert odoo_ready(_Conn({"to_regclass": (True,), "name = 'website_sale'": (False,)})) is False
    assert odoo_ready(_Conn({"to_regclass": (False,)})) is False


def test_readonly_role_retries_only_transient_catalog_conflicts() -> None:
    failures = [2]
    conns: list[_Conn] = []

    def connect() -> _Conn:
        conns.append(_Conn({}, failures))
        return conns[-1]

    sleeps: list[float] = []
    configure_readonly_role(connect, "reader-example", attempts=5, sleep=sleeps.append)
    assert len(conns) == 3 and sleeps == [2.0, 4.0]  # two conflicts, then success
    assert any("ALTER DEFAULT PRIVILEGES" in q for q in conns[-1].sql)

    with pytest.raises(psycopg.errors.InternalError_):
        configure_readonly_role(
            lambda: _Conn({}, [9]), "reader-example", attempts=3, sleep=lambda _s: None
        )

    class Broken(_Conn):
        def execute(self, query: object, params: object = None) -> _Result:
            raise psycopg.errors.InsufficientPrivilege("permission denied")

    calls: list[int] = []

    def broken() -> Broken:
        calls.append(1)
        return Broken({})

    with pytest.raises(psycopg.errors.InsufficientPrivilege):
        configure_readonly_role(broken, "x", sleep=lambda _s: None)
    assert calls == [1]  # not retried
