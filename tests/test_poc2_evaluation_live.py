from __future__ import annotations

import hashlib
import json
import os
import subprocess
from pathlib import Path

import pytest

from engine.proposal_provider import MODEL, VERSION
from scripts.poc2_evaluation_cases import approved_cases
from scripts.poc2_evaluation_live import contract_digest, verify_smoke
from scripts.poc2_evaluation_runner import REVIEW_ID
from scripts.s2_llm_eval import load_dataset


def test_poc2_full_evaluation_requires_exact_reviewed_smoke(tmp_path: Path) -> None:
    dataset = load_dataset()
    cases = approved_cases()
    directory = tmp_path / "poc2-69-smoke"
    directory.mkdir()
    raw = json.dumps(
        {
            "phase": "smoke",
            "dataset_sha256": dataset.sha256,
            "model": MODEL,
            "prompt_version": VERSION,
            "contract_sha256": contract_digest(cases),
            "review_id": REVIEW_ID,
            "expected_case_ids": [cases[0].case_id],
            "cases": [{"case_id": cases[0].case_id}],
        }
    ).encode()
    (directory / "raw.json").write_bytes(raw)
    digest = hashlib.sha256(raw).hexdigest()
    verify_smoke(tmp_path, dataset, cases, digest)
    with pytest.raises(ValueError, match="SHA-256"):
        verify_smoke(tmp_path, dataset, cases, None)
    with pytest.raises(ValueError, match="does not match"):
        verify_smoke(tmp_path, dataset, cases, "0" * 64)
    tampered = raw.replace(b'"smoke"', b'"evaluation"')
    (directory / "raw.json").write_bytes(tampered)
    with pytest.raises(ValueError, match="contract"):
        verify_smoke(tmp_path, dataset, cases, hashlib.sha256(tampered).hexdigest())


def test_poc2_host_preflight_requires_a_provider_key() -> None:
    env = os.environ.copy()
    env.pop("OPENAI_API_KEY", None)
    result = subprocess.run(
        ["bash", "scripts/poc2_evaluation.sh", "smoke", "preflight"],
        capture_output=True,
        text=True,
        check=False,
        env=env,
    )
    assert result.returncode != 0
    assert "OPENAI_API_KEY must be set" in result.stderr
