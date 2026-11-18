"""INF-7: scripts/tf_safe.sh applies only a checked, reviewer-confirmed plan. Hermetic.

Fake `terraform` and `aws` commands on PATH record every call; no AWS access happens.
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
SCRIPT = REPO / "scripts" / "tf_safe.sh"

FAKE_TERRAFORM = """#!/usr/bin/env bash
echo "terraform $*" >> "$FAKE_LOG"
chdir=""
if [[ "$1" == -chdir=* ]]; then chdir="${1#-chdir=}"; shift; fi
case "$1" in
  plan)
    out=""
    for a in "$@"; do
      case "$a" in -out=*) out=1; echo "plan-$FAKE_PLAN_CONTENT" > "$chdir/${a#-out=}";; esac
    done
    # Saved plan: 2 = changes (default), 0 = no changes. Re-plan: drift exit code.
    [ -n "$out" ] && exit "${FAKE_PLAN_EXIT:-2}"
    for a in "$@"; do [ "$a" = -detailed-exitcode ] && exit "${FAKE_DRIFT_EXIT:-0}"; done
    exit 0 ;;
  show) echo '{"resource_changes": []}' ;;
  apply) exit "${FAKE_APPLY_EXIT:-0}" ;;
  state) exit 0 ;;
  *) exit 0 ;;
esac
"""

FAKE_AWS = """#!/usr/bin/env bash
echo "aws $*" >> "$FAKE_LOG"
[ "$1 $2" = "sts get-caller-identity" ] && { echo "$FAKE_ARN"; exit 0; }
exit 0
"""

ROOT_TF = """
provider "aws" {
  region              = "us-east-1"
  allowed_account_ids = ["000000000000"]
}
"""


@pytest.fixture
def env(tmp_path: Path) -> dict[str, str]:
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    for name, body in (("terraform", FAKE_TERRAFORM), ("aws", FAKE_AWS)):
        path = bin_dir / name
        path.write_text(body, encoding="utf-8")
        path.chmod(0o755)
    root = tmp_path / "root"
    root.mkdir()
    (root / "main.tf").write_text(ROOT_TF, encoding="utf-8")
    return {
        **os.environ,
        "PATH": f"{bin_dir}{os.pathsep}{os.environ['PATH']}",
        "FAKE_LOG": str(tmp_path / "calls.log"),
        "FAKE_ARN": "arn:aws:sts::000000000000:assumed-role/AWSReservedSSO_Admin/aws-mvp",
        "FAKE_PLAN_CONTENT": "one",
        "AWS_PROFILE": "aws-mvp",
        "ROOT_DIR": str(root),
    }


def run(env: dict[str, str], *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["bash", str(SCRIPT), env["ROOT_DIR"], *args],
        env=env,
        cwd=REPO,
        capture_output=True,
        text=True,
        check=False,
        timeout=120,
    )


def calls(env: dict[str, str]) -> list[str]:
    log = Path(env["FAKE_LOG"])
    return log.read_text(encoding="utf-8").splitlines() if log.exists() else []


def plan_id(env: dict[str, str]) -> str:
    return (Path(env["ROOT_DIR"]) / ".tfsafe" / "plan.ok").read_text().split()[0]


def applied(env: dict[str, str]) -> bool:
    return any(" apply " in f" {c} " for c in calls(env) if c.startswith("terraform"))


def test_apply_refuses_without_checked_plan(env: dict[str, str]) -> None:
    result = run(env, "apply", "--confirm", "abc")
    assert result.returncode == 1
    assert "no checked plan" in result.stderr
    assert not applied(env)


def test_apply_requires_matching_confirm(env: dict[str, str]) -> None:
    assert run(env, "plan").returncode == 0, "plan must succeed"
    pid = plan_id(env)
    missing = run(env, "apply")
    assert missing.returncode == 1 and "missing --confirm" in missing.stderr
    wrong = run(env, "apply", "--confirm", "000000000000")
    assert wrong.returncode == 1 and "does not match" in wrong.stderr
    assert not applied(env)
    ok = run(env, "apply", "--confirm", pid)
    assert ok.returncode == 0, ok.stderr
    assert applied(env)
    assert "no drift" in ok.stdout
    assert not (Path(env["ROOT_DIR"]) / ".tfsafe" / "plan.tfplan").exists()


def test_apply_refuses_a_plan_changed_after_check(env: dict[str, str]) -> None:
    assert run(env, "plan").returncode == 0
    pid = plan_id(env)
    (Path(env["ROOT_DIR"]) / ".tfsafe" / "plan.tfplan").write_text("tampered\n")
    result = run(env, "apply", "--confirm", pid)
    assert result.returncode == 1 and "changed after the policy check" in result.stderr
    assert not applied(env)


def test_post_apply_drift_fails(env: dict[str, str]) -> None:
    assert run(env, "plan").returncode == 0
    env = {**env, "FAKE_DRIFT_EXIT": "2"}
    result = run(env, "apply", "--confirm", plan_id(env))
    assert result.returncode == 1 and "post-apply plan is not empty" in result.stderr


def test_refuses_root_identity_and_default_credentials(env: dict[str, str]) -> None:
    root = run({**env, "FAKE_ARN": "arn:aws:iam::000000000000:root"}, "plan")
    assert root.returncode == 1 and "root identity" in root.stderr
    no_profile = {k: v for k, v in env.items() if k != "AWS_PROFILE"}
    result = run(no_profile, "plan")
    assert result.returncode == 1 and "AWS_PROFILE" in result.stderr
    assert not any(c.startswith("terraform") and " plan" in c for c in calls(env))


def test_plan_makes_no_apply_call(env: dict[str, str]) -> None:
    result = run(env, "plan")
    assert result.returncode == 0, result.stderr
    assert "plan id" in result.stdout
    assert not applied(env)


def test_no_change_plan_removes_plan_files(env: dict[str, str]) -> None:
    env["FAKE_PLAN_EXIT"] = "0"
    result = run(env, "plan")
    assert result.returncode == 0, result.stderr
    assert "no changes" in result.stdout
    state = Path(env["ROOT_DIR"]) / ".tfsafe"
    assert not (state / "plan.tfplan").exists()
    assert not (state / "plan.json").exists()
    assert not (state / "plan.ok").exists()
    assert "plan id" not in result.stdout


def test_failed_plan_stops(env: dict[str, str]) -> None:
    env["FAKE_PLAN_EXIT"] = "1"
    result = run(env, "plan")
    assert result.returncode == 1
    assert "terraform plan failed" in result.stderr
    assert not applied(env)
