from __future__ import annotations

import json
from pathlib import Path

import pytest

from scripts.poc2_evaluation_report import publish_report


def report() -> dict[str, object]:
    return {
        "dataset_version": "synthetic-test-v1",
        "dataset_sha256": "abc",
        "model": "synthetic-test-model",
        "prompt_version": "synthetic-test-prompt",
        "contract_sha256": "synthetic-test-hash",
        "actual_usd": "0.001",
        "reserved_usd": "0.01",
        "expected_case_ids": ["one", "two"],
        "cases": [
            {
                "case_id": "one",
                "expected_action": "draft_message",
                "action_pass": True,
                "price_checked": True,
                "price_pass": False,
                "contract_pass": True,
            },
            {
                "case_id": "two",
                "expected_action": None,
                "action_pass": False,
                "price_checked": False,
                "price_pass": True,
                "contract_pass": True,
            },
        ],
    }


def test_poc2_report_publishes_matching_complete_results(tmp_path: Path) -> None:
    data = report()
    result = publish_report(tmp_path, "evaluation", data)
    assert json.loads((result / "raw.json").read_text()) == data
    summary = (result / "README.md").read_text()
    assert "Valid runtime contracts: 2/2" in summary
    assert "Action checks passed: 1/1" in summary
    assert "Structured price checks passed: 0/1" in summary
    with pytest.raises(FileExistsError):
        publish_report(tmp_path, "evaluation", data)
    assert json.loads((result / "raw.json").read_text()) == data


def test_poc2_report_rejects_partial_results(tmp_path: Path) -> None:
    data = report()
    data["expected_case_ids"] = ["one", "two", "three"]
    with pytest.raises(ValueError, match="all expected cases"):
        publish_report(tmp_path, "evaluation", data)
    assert not (tmp_path / "poc2-69-evaluation").exists()
