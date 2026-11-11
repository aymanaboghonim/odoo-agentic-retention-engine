from __future__ import annotations

import json
import uuid
from pathlib import Path
from unittest.mock import patch

import pytest

from scripts.full_stack_measure import PROJECT, bytes_value, preflight, publish, snapshot, totals


def test_docker_memory_units_and_cpu_totals() -> None:
    assert bytes_value("1.5MiB ") == 1572864
    with pytest.raises(ValueError, match="unknown memory unit"):
        bytes_value("1 mystery")
    samples = [
        {"containers": [{"cpu_percent": 20.0, "memory_bytes": 100}]},
        {"containers": [{"cpu_percent": 40.0, "memory_bytes": 200}]},
    ]
    assert totals(samples) == {
        "mean_cpu_percent": 30.0,
        "max_cpu_percent": 40.0,
        "mean_memory_bytes": 150,
        "max_memory_bytes": 200,
    }


def test_snapshot_refreshes_container_list_when_a_run_container_exits() -> None:
    stats = json.dumps(
        {"Name": "poc68-isolated-engine-db-1", "CPUPerc": "1.25%", "MemUsage": "1MiB / 2GiB"}
    )
    with patch(
        "scripts.full_stack_measure.command",
        side_effect=[
            "stale-run-container\nengine-container\n",
            RuntimeError("docker failed with exit status 1"),
            "engine-container\n",
            stats,
        ],
    ):
        result = snapshot()
    assert result["containers"] == [
        {
            "name": "poc68-isolated-engine-db-1",
            "cpu_percent": 1.25,
            "memory_bytes": 1024**2,
        }
    ]


def test_capacity_preflight_fails_before_proposal_if_key_missing(tmp_path: Path) -> None:
    with (
        patch.dict("os.environ", {}, clear=True),
        patch("scripts.full_stack_measure.command") as command,
        pytest.raises(ValueError, match="OPENAI_API_KEY"),
    ):
        preflight(["docker", "compose"], tmp_path, uuid.uuid4(), "initial")
    command.assert_not_called()


def test_recovery_preflight_selects_the_recovery_claim(tmp_path: Path) -> None:
    services = "\n".join(("odoo-db", "odoo", "engine-db", "worker", "approval-api", "mailpit"))
    network = [
        {
            "Internal": True,
            "Labels": {"com.docker.compose.project": PROJECT},
        }
    ]
    with (
        patch.dict("os.environ", {"OPENAI_API_KEY": "test-key"}, clear=True),
        patch(
            "scripts.full_stack_measure.command",
            side_effect=[json.dumps(network), services, "", ""],
        ) as command,
    ):
        preflight(["docker", "compose"], tmp_path, uuid.uuid4(), "recovery")
    recovery_command = command.call_args_list[-1].args[0]
    assert recovery_command[recovery_command.index("--attempt") + 1] == "recovery"
    assert recovery_command[-1] == "--preflight-only"


def test_capacity_reports_match_and_never_overwrite(tmp_path: Path) -> None:
    sample = {"containers": [{"cpu_percent": 12.0, "memory_bytes": 1024**2}]}
    report = {
        "run_id": str(uuid.uuid4()),
        "attempt": "initial",
        "host": {"platform": "test", "logical_cpus": 4},
        "stage_elapsed_seconds": {"scan": 0.1, "proposal": 1.0},
        "idle_samples": [sample],
        "scan_samples": [sample],
        "proposal_samples": [sample],
        "summary": {
            "idle": totals([sample]),
            "scan": totals([sample]),
            "proposal": totals([sample]),
        },
    }
    destination = publish(tmp_path, report)
    assert json.loads((destination / "raw.json").read_text()) == report
    markdown = (destination / "README.md").read_text()
    assert "12.00%" in markdown and "1.0 MiB" in markdown and "(raw.json)" in markdown
    with pytest.raises(FileExistsError):
        publish(tmp_path, report)
    assert json.loads((destination / "raw.json").read_text()) == report
