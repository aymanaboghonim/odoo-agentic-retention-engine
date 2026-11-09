from __future__ import annotations

import uuid
from unittest.mock import MagicMock, patch

import pytest

from engine.full_stack_worker import WorkerSettings, main

ENV = {
    "FULL_STACK_SYNTHETIC_ISOLATED": "YES",
    "FULL_STACK_INSTANCE_ID": str(uuid.uuid4()),
    "ENGINE_DATABASE_URL": "postgresql://engine:synthetic@engine-db:5432/engine",
    "ODOO_READONLY_DATABASE_URL": "postgresql://odoo_ro:synthetic@odoo-db:5432/odoo",
}


def test_worker_settings_reject_unexpected_target_names() -> None:
    settings = WorkerSettings.from_env(ENV)
    assert settings.instance_id == ENV["FULL_STACK_INSTANCE_ID"]
    assert "synthetic" not in repr(settings)
    for name, value in (
        ("FULL_STACK_SYNTHETIC_ISOLATED", "NO"),
        ("FULL_STACK_INSTANCE_ID", "invalid"),
        ("ODOO_READONLY_DATABASE_URL", "postgresql://odoo_ro:x@remote:5432/odoo"),
        ("ODOO_READONLY_DATABASE_URL", "postgresql://odoo:x@odoo-db:5432/odoo"),
        ("ODOO_READONLY_DATABASE_URL", "postgresql://odoo_ro:x@odoo-db:5432/other"),
        ("ENGINE_DATABASE_URL", "postgresql://engine:x@remote:5432/engine"),
        ("FULL_STACK_SCAN_PAGE_SIZE", "1001"),
        ("FULL_STACK_SCAN_INTERVAL_SECONDS", "0"),
    ):
        with pytest.raises(ValueError):
            WorkerSettings.from_env({**ENV, name: value})


def test_worker_checks_role_before_scanning_without_provider() -> None:
    reader = MagicMock()
    engine = MagicMock()
    with (
        patch.dict("os.environ", ENV, clear=True),
        patch("engine.full_stack_worker.OdooReader", return_value=reader),
        patch("engine.full_stack_worker.create_engine", return_value=engine),
        patch("engine.full_stack_worker.run_scheduled_scans") as scheduled,
        patch("engine.full_stack_worker.signal.signal"),
    ):
        main([])
    reader.verify_synthetic_readonly_role.assert_called_once_with()
    scheduled.assert_called_once()
    engine.dispose.assert_called_once_with()


def test_worker_one_shot_scans_one_page_and_exits() -> None:
    reader = MagicMock()
    engine = MagicMock()
    scanner = MagicMock()
    scanner.scan_page.return_value = (1, 1)
    with (
        patch.dict("os.environ", ENV, clear=True),
        patch("engine.full_stack_worker.OdooReader", return_value=reader),
        patch("engine.full_stack_worker.create_engine", return_value=engine),
        patch("engine.full_stack_worker.SyntheticCartScanner", return_value=scanner),
        patch("engine.full_stack_worker.run_scheduled_scans") as scheduled,
        patch("engine.full_stack_worker.signal.signal"),
    ):
        main(["--once"])
    reader.verify_synthetic_readonly_role.assert_called_once_with()
    scanner.scan_page.assert_called_once()
    assert scanner.scan_page.call_args.kwargs["now"].tzinfo is not None
    scheduled.assert_not_called()
    engine.dispose.assert_called_once_with()


def test_worker_refuses_to_start_if_role_check_fails() -> None:
    reader = MagicMock()
    reader.verify_synthetic_readonly_role.side_effect = ValueError("unsafe role")
    with (
        patch.dict("os.environ", ENV, clear=True),
        patch("engine.full_stack_worker.OdooReader", return_value=reader),
        patch("engine.full_stack_worker.create_engine") as create,
        pytest.raises(ValueError, match="unsafe role"),
    ):
        main([])
    create.assert_not_called()
