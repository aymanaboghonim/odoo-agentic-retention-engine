"""scripts/aws_mvp_rollout.sh waits on the deployment's rolloutState (#109). Hermetic.

With the stack stopped (no connected container instance) it does not wait (#148).

A fake `aws` on PATH answers from fixtures and records every call; no AWS access happens.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
SCRIPT = REPO / "scripts" / "aws_mvp_rollout.sh"
ARN = "arn:aws:ecs:us-east-1:000000000000:task-definition/aorte-mvp-core:8"

TASK_DEF = {
    "family": "aorte-mvp-core",
    "networkMode": "bridge",
    "containerDefinitions": [
        {
            "name": "worker",
            "image": "000000000000.dkr.ecr.us-east-1.amazonaws.com/aorte-mvp/engine:old",
            "environment": [{"name": "TELEMETRY_ENABLED", "value": "NO"}],
        },
        {
            "name": "mailpit",
            "image": "000000000000.dkr.ecr.us-east-1.amazonaws.com/aorte-mvp/mailpit@sha256:abc",
        },
    ],
}

# Each rolloutState query pops the next line of $FAKE_STATES (the last line repeats).
FAKE_AWS = """#!/usr/bin/env bash
echo "aws $*" >> "$FAKE_LOG"
case "$1 $2" in
  "ecs describe-task-definition") cat "$FAKE_TASK_DEF" ;;
  "ecs register-task-definition") echo "$FAKE_ARN" ;;
  "ecs update-service") ;;
  "ecs list-container-instances") echo "${FAKE_AGENTS:-1}" ;;
  "ecs describe-services")
    case "$*" in
      *desiredCount*) echo 1 ;;
      *rolloutState*)
        state=$(head -n1 "$FAKE_STATES")
        [ "$(wc -l < "$FAKE_STATES")" -gt 1 ] && sed -i 1d "$FAKE_STATES"
        echo "$state" ;;
    esac ;;
  *) exit 2 ;;
esac
"""

pytestmark = pytest.mark.skipif(shutil.which("jq") is None, reason="jq is required")


@pytest.fixture
def env(tmp_path: Path) -> dict[str, str]:
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    fake = bin_dir / "aws"
    fake.write_text(FAKE_AWS, encoding="utf-8")
    fake.chmod(0o755)
    task_def = tmp_path / "task.json"
    task_def.write_text(json.dumps(TASK_DEF), encoding="utf-8")
    return {
        **os.environ,
        "PATH": f"{bin_dir}{os.pathsep}{os.environ['PATH']}",
        "FAKE_LOG": str(tmp_path / "calls.log"),
        "FAKE_TASK_DEF": str(task_def),
        "FAKE_STATES": str(tmp_path / "states"),
        "FAKE_ARN": ARN,
        "AORTE_ROLLOUT_POLL": "0",
    }


def run(env: dict[str, str], states: list[str]) -> subprocess.CompletedProcess[str]:
    Path(env["FAKE_STATES"]).write_text("\n".join(states) + "\n", encoding="utf-8")
    return subprocess.run(
        ["bash", str(SCRIPT), "newtag"],
        env=env,
        cwd=REPO,
        capture_output=True,
        text=True,
        check=False,
        timeout=60,
    )


def calls(env: dict[str, str]) -> list[str]:
    return Path(env["FAKE_LOG"]).read_text(encoding="utf-8").splitlines()


def test_waits_until_rollout_completed(env: dict[str, str]) -> None:
    result = run(env, ["IN_PROGRESS", "IN_PROGRESS", "COMPLETED"])
    assert result.returncode == 0, result.stderr
    assert "rollout COMPLETED" in result.stdout
    polls = [c for c in calls(env) if "rolloutState" in c]
    assert len(polls) == 3
    assert all(ARN in c for c in polls), "polls the deployment of the new revision"
    assert not any("wait services-stable" in c for c in calls(env))
    log = Path(env["FAKE_LOG"]).read_text(encoding="utf-8")
    assert log.count("register-task-definition") == 1
    assert "aorte-mvp/engine:newtag" in log and "engine:old" not in log
    assert "aorte-mvp/mailpit@sha256:abc" in log, "only the engine image changes"


def test_fails_on_failed_rollout(env: dict[str, str]) -> None:
    result = run(env, ["IN_PROGRESS", "FAILED"])
    assert result.returncode == 1
    assert "rolloutState FAILED" in result.stderr


def test_fails_on_timeout(env: dict[str, str]) -> None:
    env["AORTE_ROLLOUT_TIMEOUT"] = "0"
    result = run(env, ["IN_PROGRESS"])
    assert result.returncode == 1
    assert "no COMPLETED rollout after 0s" in result.stderr


def test_refuses_task_definition_without_required_engine_setting(
    env: dict[str, str], tmp_path: Path
) -> None:
    # #126: the rollout copies the current environment, so a new required setting must be
    # applied by Terraform first; nothing is registered otherwise.
    stale = json.loads(json.dumps(TASK_DEF))
    del stale["containerDefinitions"][0]["environment"]
    Path(env["FAKE_TASK_DEF"]).write_text(json.dumps(stale), encoding="utf-8")
    result = run(env, ["COMPLETED"])
    assert result.returncode == 1
    assert "task definition lacks TELEMETRY_ENABLED in: worker" in result.stderr
    assert not any("register-task-definition" in c for c in calls(env))


def test_running_stack_checks_for_a_connected_agent(env: dict[str, str]) -> None:
    result = run(env, ["COMPLETED"])
    assert result.returncode == 0, result.stderr
    checks = [c for c in calls(env) if "list-container-instances" in c]
    assert len(checks) == 1
    assert "--cluster aorte-mvp" in checks[0]
    assert "agentConnected==true" in checks[0] and "--status ACTIVE" in checks[0]


def test_stopped_stack_registers_without_waiting(env: dict[str, str]) -> None:
    # #148: the instance is stopped, desired count stays 1, no agent is connected.
    env["FAKE_AGENTS"] = "0"
    result = run(env, ["IN_PROGRESS"])
    assert result.returncode == 0, result.stderr
    assert "stack stopped" in result.stdout and ARN in result.stdout
    log = calls(env)
    assert sum("register-task-definition" in c for c in log) == 1
    assert sum("update-service" in c for c in log) == 1
    assert not any("rolloutState" in c for c in log), "no wait on a stopped stack"
