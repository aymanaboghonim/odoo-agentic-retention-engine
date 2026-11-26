"""EVW-2: the behavior fingerprint is deterministic and covers every behavior file (#135)."""

from __future__ import annotations

import re
import shutil
from pathlib import Path

from scripts.eval_fingerprint import (
    CLASSIFIED_GLOBS,
    COVERED,
    EXCLUDED,
    ROOT,
    fingerprint,
    fingerprint_inputs,
    unclassified,
)

WORKFLOW = ROOT / ".github" / "workflows" / "llm-eval.yml"


def copy_covered(destination: Path) -> None:
    for relative in COVERED:
        target = destination / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(ROOT / relative, target)


def workflow_paths() -> list[str]:
    text = WORKFLOW.read_text(encoding="utf-8")
    block = text.split("    paths:\n", 1)[1].split("\n\n", 1)[0]
    return re.findall(r"^      - (\S+)$", block, re.M)


def test_eval_fingerprint_is_stable_and_covers_behavior_files(tmp_path: Path) -> None:
    # Deterministic: same files, same fingerprint; it is a SHA-256 hex digest.
    digest = fingerprint()
    assert digest == fingerprint()
    assert re.fullmatch(r"[0-9a-f]{64}", digest)
    copy_covered(tmp_path)
    assert fingerprint(tmp_path) == digest

    # Any change to a covered file changes it.
    prompt = tmp_path / "src/engine/proposal_provider.py"
    prompt.write_text(prompt.read_text(encoding="utf-8") + "\n# changed\n", encoding="utf-8")
    assert fingerprint(tmp_path) != digest

    # The inputs name the model, the prompt version, and the evaluation token limits.
    inputs = fingerprint_inputs()
    assert inputs["model"] and inputs["prompt_version"]
    assert (inputs["input_token_limit"], inputs["output_token_limit"]) == (4000, 8192)
    files = inputs["files"]
    assert isinstance(files, dict) and sorted(files) == sorted(COVERED)

    # Every engine module and policy file is classified; a new one fails until classified.
    assert unclassified() == []
    assert set(COVERED).isdisjoint(EXCLUDED)
    assert all(reason.strip() for reason in EXCLUDED.values())
    assert all((ROOT / path).is_file() for path in COVERED)
    assert all((ROOT / path).is_file() for path in EXCLUDED)
    for must in ("proposal.py", "proposal_provider.py", "policy.py", "retrieval.py"):
        assert f"src/engine/{must}" in COVERED
    assert "policies/synthetic.json" in COVERED
    (tmp_path / "src/engine/new_behavior.py").write_text("X = 1\n", encoding="utf-8")
    (tmp_path / "policies/extra.json").write_text("{}\n", encoding="utf-8")
    found = unclassified(tmp_path)
    assert "src/engine/new_behavior.py" in found and "policies/extra.json" in found
    assert CLASSIFIED_GLOBS == ("src/engine/*.py", "policies/*")

    # The workflow's push trigger lists exactly the covered files.
    assert sorted(workflow_paths()) == sorted(COVERED)
