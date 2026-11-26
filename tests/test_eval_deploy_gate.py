"""EVW-3, EVW-4: the fingerprint deploy gate (#157, ADR-0031 decision 4).

Hermetic: builds report files in a temporary copy of the covered files; reads the workflow text.
"""

from __future__ import annotations

import json
import re
import shutil
from pathlib import Path
from typing import Any

from scripts.eval_deploy_gate import RESULTS, clean_reason, decide
from scripts.eval_fingerprint import COVERED, ROOT, fingerprint, fingerprint_inputs

WORKFLOW = (ROOT / ".github" / "workflows" / "deploy-aws.yml").read_text(encoding="utf-8")
DEPLOY = WORKFLOW.split("\n  deploy:\n", 1)[1].split("\n  eval-gate:\n", 1)[0]
GATE = WORKFLOW.split("\n  eval-gate:\n", 1)[1]


def tree(tmp: Path) -> Path:
    for relative in COVERED:
        target = tmp / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(ROOT / relative, target)
    (tmp / RESULTS).mkdir(parents=True, exist_ok=True)
    return tmp


def write_report(
    root: Path,
    name: str,
    *,
    safety: bool = True,
    quality: bool = True,
    inputs: dict[str, Any] | None = None,
    digest: str | None = None,
    completed_at: str = "2026-11-26T08:33:00+00:00",
) -> None:
    gates = {"G1": True, "G2": True, "G3": safety, "G4_G5": quality}
    report = {
        "kind": "live-evaluation",
        "fingerprint": digest or fingerprint(root),
        "fingerprint_inputs": inputs or fingerprint_inputs(root),
        "completed_at": completed_at,
        "safety_pass": safety,
        "quality_pass": quality,
        "sets": {"reviewed": {"gates": gates}, "heldout": {"gates": dict(gates)}},
    }
    folder = root / RESULTS / name
    folder.mkdir()
    (folder / "report.json").write_text(json.dumps(report), encoding="utf-8")


def change_prompt(root: Path) -> None:
    path = root / "src/engine/proposal_provider.py"
    path.write_text(path.read_text(encoding="utf-8") + "\n# changed\n", encoding="utf-8")


def test_deploy_gate_blocks_on_safety_failure_or_missing_report(tmp_path: Path) -> None:
    root = tree(tmp_path)
    # No report at all: blocked, and the message names the fingerprint.
    missing = decide(root, "")
    assert missing.blocked and missing.fingerprint in missing.lines[0]

    # A passing report for this fingerprint: deploys.
    write_report(root, "eval-20261126T0829Z-aaaaaaaaaaaa")
    assert not decide(root, "").blocked

    # A behavior change makes the fingerprint differ: the old report no longer counts.
    change_prompt(root)
    changed = decide(root, "any reason")
    assert changed.blocked and changed.report is None

    # A failing safety gate blocks, even with an override reason.
    root2 = tree(tmp_path / "safety")
    write_report(root2, "eval-20261126T0900Z-bbbbbbbbbbbb", safety=False)
    unsafe = decide(root2, "override")
    assert unsafe.blocked and "G1..G3" in unsafe.lines[-1]

    # Abuse: a report edited by hand to claim the current fingerprint, with other inputs.
    root3 = tree(tmp_path / "forged")
    stale = fingerprint_inputs(root3)
    change_prompt(root3)
    write_report(root3, "eval-20261126T1000Z-cccccccccccc", inputs=stale, digest=fingerprint(root3))
    assert decide(root3, "").blocked

    # A summary that says pass while a set's safety gate failed is not trusted.
    root4 = tree(tmp_path / "inconsistent")
    write_report(root4, "eval-20261126T1100Z-dddddddddddd")
    path = root4 / RESULTS / "eval-20261126T1100Z-dddddddddddd" / "report.json"
    report = json.loads(path.read_text(encoding="utf-8"))
    report["sets"]["heldout"]["gates"]["G2"] = False
    path.write_text(json.dumps(report), encoding="utf-8")
    assert decide(root4, "").blocked

    # The newest matching report decides.
    root5 = tree(tmp_path / "newest")
    write_report(
        root5,
        "eval-20261126T0800Z-eeeeeeeeeeee",
        safety=False,
        completed_at="2026-11-26T08:00:00+00:00",
    )
    write_report(
        root5, "eval-20261126T0900Z-ffffffffffff", completed_at="2026-11-26T09:00:00+00:00"
    )
    newest = decide(root5, "")
    assert not newest.blocked and newest.report and newest.report.endswith("ffffffffffff")


def test_quality_override_requires_reason(tmp_path: Path) -> None:
    root = tree(tmp_path)
    write_report(root, "eval-20261126T0829Z-aaaaaaaaaaaa", quality=False)
    for empty in ("", "   ", "\n\t"):
        blocked = decide(root, empty)
        assert blocked.blocked and "quality_override_reason" in blocked.lines[-1]
    allowed = decide(root, "held-out\nG4 `EN` 21/22, owner accepted")
    assert not allowed.blocked
    assert allowed.lines[-1].startswith("WARNING:")
    assert "held-out G4 'EN' 21/22, owner accepted" in allowed.lines[-1]
    assert len(clean_reason("x" * 1000)) == 300

    # The workflow: the input exists, reaches the script through env (never inline in the
    # script), the gate has no secrets or environment, and deploy needs it and runs only on main.
    assert "quality_override_reason:" in WORKFLOW.split("\njobs:\n", 1)[0]
    assert "QUALITY_OVERRIDE_REASON: ${{ inputs.quality_override_reason }}" in GATE
    run_lines = [line for line in GATE.splitlines() if line.strip().startswith("run:")]
    assert run_lines and all("inputs." not in line for line in run_lines)
    assert "secrets." not in GATE and "environment:" not in GATE
    assert "python -m scripts.eval_deploy_gate" in GATE
    assert re.search(r"^    needs: eval-gate$", DEPLOY, re.M)
    assert "    if: github.ref == 'refs/heads/main'" in DEPLOY
    assert "    environment: aws-mvp" in DEPLOY
