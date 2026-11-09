from __future__ import annotations

import pytest

from engine.full_stack_init import validate

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
