from __future__ import annotations

import json
from dataclasses import asdict
from typing import cast

import pytest

from scripts.s2_llm_eval import (
    CaseScore,
    EvaluationError,
    GoldenDataset,
    aggregate_slices,
    load_dataset,
)
from scripts.s2_llm_prompts import PromptVariant, prompt_fingerprint, prompt_version
from scripts.s2_prompt_compare import compare_prompt_reports


def _report(dataset: GoldenDataset, variant: str, failing_cases: set[str]) -> dict[str, object]:
    scores = tuple(
        CaseScore(
            case_id=case.case_id,
            pair_id=case.pair_id,
            pair_kind=case.pair_kind,
            scenario_id=case.scenario_id,
            language=case.language,
            dialect=case.dialect,
            category=case.category,
            risk_level=case.risk_level,
            schema_pass=case.case_id not in failing_cases,
            tool_call_pass=case.case_id not in failing_cases,
            action_pass=case.case_id not in failing_cases,
            price_fact_pass=True,
            discount_safety_pass=True,
            pii_pass=True,
            passed=case.case_id not in failing_cases,
            failures=("unexpected_tool_call",) if case.case_id in failing_cases else (),
        )
        for case in dataset.cases
    )
    candidate = {
        "candidate_id": "gpt-6-luna-low",
        "provider": "openai",
        "model": "gpt-6-luna",
        "configuration": {"reasoning_effort": "low"},
    }
    return json.loads(
        json.dumps(
            {
                "run_id": f"test-{variant}",
                "run_mode": "full",
                "candidate": candidate,
                "dataset_version": dataset.version,
                "dataset_sha256": dataset.sha256,
                "scoring_method": "deterministic-s2-v3",
                "prompt_variant": variant,
                "prompt_version": prompt_version(cast(PromptVariant, variant)),
                "prompt_schema_sha256": prompt_fingerprint("openai", cast(PromptVariant, variant)),
                "summary": {
                    "input_count": len(scores),
                    "passed_count": sum(score.passed for score in scores),
                    "published_list_cost_usd": "0.01",
                },
                "slices": aggregate_slices(scores),
                "case_results": [
                    {"case_id": score.case_id, "score": asdict(score)} for score in scores
                ],
            }
        )
    )


def test_prompt_comparison_reports_all_slices_and_safety_regressions() -> None:
    dataset = load_dataset()
    cross_customer_case = next(
        case.case_id for case in dataset.cases if case.scenario_id == "cross_customer"
    )
    baseline = _report(dataset, "baseline", set())
    revised = _report(dataset, "tool-clarity", {cross_customer_case})

    comparison = compare_prompt_reports(baseline, revised)

    assert comparison["recommendation"] == "no_clear_winner"
    assert comparison["pass_delta"] == -1
    assert "category:cross_customer_probe" in cast(list[str], comparison["safety_regressions"])
    slices = cast(dict[str, dict[str, object]], comparison["slice_deltas"])
    assert slices["language:ar_msa"]["delta"] == 0
    assert slices["category:cross_customer_probe"]["delta"] == -1
    assert len(cast(list[dict[str, object]], comparison["changed_cases"])) == 1


def test_prompt_comparison_recommends_revision_only_without_safety_regression() -> None:
    dataset = load_dataset()
    cross_customer_case = next(
        case.case_id for case in dataset.cases if case.scenario_id == "cross_customer"
    )
    baseline = _report(dataset, "baseline", {cross_customer_case})
    revised = _report(dataset, "tool-clarity", set())

    comparison = compare_prompt_reports(baseline, revised)

    assert comparison["recommendation"] == "tool-clarity"
    assert comparison["pass_delta"] == 1
    assert comparison["safety_regressions"] == []


def test_prompt_comparison_rejects_mismatched_run_contract() -> None:
    dataset = load_dataset()
    baseline = _report(dataset, "baseline", set())
    revised = _report(dataset, "tool-clarity", set())
    revised["dataset_sha256"] = "0" * 64

    with pytest.raises(EvaluationError, match="dataset_sha256"):
        compare_prompt_reports(baseline, revised)
