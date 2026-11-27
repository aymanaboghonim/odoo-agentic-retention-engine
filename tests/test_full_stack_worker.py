from __future__ import annotations

import uuid
from datetime import UTC, datetime
from unittest.mock import MagicMock, patch

import pytest

from engine.full_stack_worker import WorkerSettings, main
from engine.runtime_state import RuntimeSnapshot, RuntimeState
from engine.worker import GuardedScan


class FakeRuntime:
    """Stands in for the runtime-state table, which the mocked engine cannot serve."""

    def __init__(self, state: RuntimeState = RuntimeState.ACTIVE) -> None:
        self.state = state

    def get(self) -> RuntimeSnapshot:
        return RuntimeSnapshot(self.state, "test", "test", datetime.now(UTC), 1)


ENV = {
    "RECONCILE_STALE_STARTED_MINUTES": "30",  # #131: required by the worker
    "FULL_STACK_SYNTHETIC_ISOLATED": "YES",
    "FULL_STACK_INSTANCE_ID": str(uuid.uuid4()),
    "ENGINE_DATABASE_URL": "postgresql://engine:synthetic@engine-db:5432/engine",
    "ODOO_READONLY_DATABASE_URL": "postgresql://odoo_ro:synthetic@odoo-db:5432/odoo",
    "TELEMETRY_ENABLED": "NO",
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
        patch("engine.full_stack_worker.RuntimeStateRepository", return_value=FakeRuntime()),
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


def test_erp_scan_mode_requires_explicit_q12_settings() -> None:
    erp_env = {**ENV, "SCAN_MODE": "erp"}
    with pytest.raises(ValueError, match="ERP_SCAN_CHANNEL"):
        WorkerSettings.from_env(erp_env)
    with pytest.raises(ValueError, match="COOLDOWN"):
        WorkerSettings.from_env({**erp_env, "ERP_SCAN_CHANNEL": "email"})
    with pytest.raises(ValueError, match="PRIVACY_RETENTION_DAYS"):
        WorkerSettings.from_env(
            {**erp_env, "ERP_SCAN_CHANNEL": "email", "CAMPAIGN_COOLDOWN_DAYS": "7"}
        )
    with pytest.raises(ValueError, match="SCAN_MODE"):
        WorkerSettings.from_env({**ENV, "SCAN_MODE": "everything"})
    settings = WorkerSettings.from_env(
        {
            **erp_env,
            "ERP_SCAN_CHANNEL": "email",
            "CAMPAIGN_COOLDOWN_DAYS": "7",
            "PRIVACY_RETENTION_DAYS": "30",
            "PRIVACY_TOMBSTONE_PEPPER": "p" * 32,
        }
    )
    assert settings.scan_mode == "erp" and settings.erp is not None
    assert "p" * 32 not in repr(settings)
    assert WorkerSettings.from_env(ENV).scan_mode == "synthetic"
    reader = MagicMock()
    with (
        patch.dict(
            "os.environ",
            {
                **erp_env,
                **{
                    "ERP_SCAN_CHANNEL": "email",
                    "CAMPAIGN_COOLDOWN_DAYS": "7",
                    "PRIVACY_RETENTION_DAYS": "30",
                    "PRIVACY_TOMBSTONE_PEPPER": "p" * 32,
                },
            },
            clear=True,
        ),
        patch("engine.full_stack_worker.OdooReader", return_value=reader),
        patch("engine.full_stack_worker.create_engine", return_value=MagicMock()),
        patch("engine.full_stack_worker.run_scheduled_scans") as scheduled,
        patch("engine.full_stack_worker.signal.signal"),
    ):
        main([])
    reader.verify_readonly_role.assert_called_once_with()
    guarded = scheduled.call_args.args[0]
    assert isinstance(guarded, GuardedScan)
    assert type(guarded._scanner).__name__ == "_ErpScanPage"


def test_halted_worker_skips_the_scan_but_still_runs_maintenance() -> None:
    """KS-3: a halted runtime state creates no runs; the loop and maintenance still run."""
    reader = MagicMock()
    engine = MagicMock()
    scanner = MagicMock()
    with (
        patch.dict("os.environ", ENV, clear=True),
        patch("engine.full_stack_worker.OdooReader", return_value=reader),
        patch("engine.full_stack_worker.create_engine", return_value=engine),
        patch("engine.full_stack_worker.SyntheticCartScanner", return_value=scanner),
        patch(
            "engine.full_stack_worker.RuntimeStateRepository",
            return_value=FakeRuntime(RuntimeState.HALTED),
        ),
        patch("engine.full_stack_worker.signal.signal"),
    ):
        main(["--once"])
    scanner.scan_page.assert_not_called()


def test_invalid_breaker_settings_stop_the_worker_before_any_database_work() -> None:
    with (
        patch.dict("os.environ", {**ENV, "BREAKER_ENABLED": "YES"}, clear=True),
        patch("engine.full_stack_worker.OdooReader") as odoo,
        patch("engine.full_stack_worker.create_engine") as create,
        pytest.raises(ValueError, match="breaker settings"),
    ):
        main(["--once"])
    odoo.assert_not_called()
    create.assert_not_called()
