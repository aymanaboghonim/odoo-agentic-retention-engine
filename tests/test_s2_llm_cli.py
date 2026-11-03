from __future__ import annotations

import hashlib
import json
from decimal import Decimal
from pathlib import Path

import pytest

from scripts.s2_llm_cli import (
    _approval_for,
    _prepare_report_paths,
    _verify_smoke_report,
    select_smoke_cases,
)
from scripts.s2_llm_eval import EvaluationError, load_dataset
from scripts.s2_llm_runner import SUPPORTED_CANDIDATES

APPROVAL_EXAMPLE = (
    Path(__file__).resolve().parents[1] / "evals" / "llm" / "s2_live_approval.example.json"
)


def test_smoke_selection_includes_eligible_and_recent_control_cases() -> None:
    dataset = load_dataset()

    cases = select_smoke_cases(dataset)

    assert len(cases) == 2
    assert {(case.fixture_id, case.scenario_id, case.expected_action) for case in cases} == {
        ("cart-open-001", "recovery", "draft_message"),
        ("cart-recent-004", "recent_cart", "no_action"),
    }


def test_approval_template_is_fail_closed_and_uses_recorded_caps() -> None:
    document = json.loads(APPROVAL_EXAMPLE.read_text(encoding="utf-8"))
    candidate = SUPPORTED_CANDIDATES[0]

    approval = _approval_for(document, candidate.candidate_id)

    assert approval.owner_approved is False
    assert approval.approved_candidate_ids == ()
    assert approval.spend_ceiling_usd == Decimal("5.00")
    assert approval.rpm is None
    assert approval.tpm is None
    assert approval.max_input_tokens_per_request == 0
    assert approval.max_output_tokens_per_request == 8192


def test_full_requires_exact_owner_reviewed_smoke_report_hash(tmp_path: Path) -> None:
    dataset = load_dataset()
    report_path = tmp_path / "smoke.json"
    report_path.write_text(
        json.dumps(
            {
                "run_mode": "smoke",
                "dataset_version": dataset.version,
                "dataset_sha256": dataset.sha256,
                "candidate": {"candidate_id": SUPPORTED_CANDIDATES[0].candidate_id},
            }
        ),
        encoding="utf-8",
    )
    digest = hashlib.sha256(report_path.read_bytes()).hexdigest()

    assert _verify_smoke_report(report_path, digest, "owner-reviewer", dataset) == digest
    with pytest.raises(EvaluationError, match="owner-approved SHA-256"):
        _verify_smoke_report(report_path, "0" * 64, "owner-reviewer", dataset)
    with pytest.raises(EvaluationError, match="smoke_report_reviewer_id"):
        _verify_smoke_report(report_path, digest, "", dataset)


def test_report_paths_are_unique_and_preflighted(tmp_path: Path) -> None:
    output_dir = tmp_path / "nested" / "results"

    paths = _prepare_report_paths(output_dir, ["run-one.json"])

    assert paths == [output_dir / "run-one.json"]
    assert output_dir.is_dir()
    paths[0].write_text("completed report", encoding="utf-8")
    with pytest.raises(EvaluationError, match="Refusing to overwrite"):
        _prepare_report_paths(output_dir, ["run-one.json"])
