from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Mapping, Sequence
from pathlib import Path

from scripts.s2_llm_eval import EvaluationError, Record, _record, publish_report_pair

SAFETY_SLICES = (
    "category:cross_customer_probe",
    "category:customer_preference",
    "category:deterministic_money_boundary",
    "category:odoo_fact_fidelity",
    "category:odoo_price_fidelity",
    "category:pii_masking",
    "category:prompt_injection",
)


def _read_report(path: Path) -> Record:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as error:
        raise EvaluationError(f"Prompt comparison input {path} does not exist") from error
    except OSError as error:
        raise EvaluationError(f"Could not read prompt comparison input {path}") from error
    except json.JSONDecodeError as error:
        raise EvaluationError(f"Prompt comparison input {path} is not valid JSON") from error
    return _record(value, f"prompt comparison input {path}")


def _validate_pair(baseline: Record, revised: Record) -> None:
    if baseline.get("run_mode") != "full" or revised.get("run_mode") != "full":
        raise EvaluationError("Prompt comparison requires two full-run reports")
    for key in ("dataset_version", "dataset_sha256", "scoring_method"):
        if baseline.get(key) != revised.get(key):
            raise EvaluationError(f"Prompt reports do not match on {key}")
    baseline_candidate = _record(baseline.get("candidate"), "baseline candidate")
    revised_candidate = _record(revised.get("candidate"), "revised candidate")
    if baseline_candidate != revised_candidate:
        raise EvaluationError("Prompt reports must use the same candidate and configuration")
    if baseline_candidate.get("candidate_id") != "gpt-6-luna-low":
        raise EvaluationError("Prompt comparison is limited to gpt-6-luna-low")
    if baseline.get("prompt_variant") != "baseline":
        raise EvaluationError("Baseline report must use the baseline prompt")
    if revised.get("prompt_variant") != "tool-clarity":
        raise EvaluationError("Revised report must use the tool-clarity prompt")
    if baseline.get("prompt_version") == revised.get("prompt_version"):
        raise EvaluationError("Prompt versions must differ")
    if baseline.get("prompt_schema_sha256") == revised.get("prompt_schema_sha256"):
        raise EvaluationError("Prompt fingerprints must differ")


def compare_prompt_reports(
    baseline_report: Mapping[str, object], revised_report: Mapping[str, object]
) -> Record:
    baseline = _record(baseline_report, "baseline report")
    revised = _record(revised_report, "revised report")
    _validate_pair(baseline, revised)
    baseline_cases_value = baseline.get("case_results")
    revised_cases_value = revised.get("case_results")
    if not isinstance(baseline_cases_value, list) or not isinstance(revised_cases_value, list):
        raise EvaluationError("Prompt reports must include per-case results")
    if len(baseline_cases_value) != 132 or len(revised_cases_value) != 132:
        raise EvaluationError("Prompt comparison requires all 132 S2 cases per report")

    def by_case(rows: Sequence[object], label: str) -> dict[str, Record]:
        result: dict[str, Record] = {}
        for value in rows:
            row = _record(value, f"{label} case result")
            case_id = row.get("case_id")
            if not isinstance(case_id, str) or case_id in result:
                raise EvaluationError(f"{label} report has an invalid or duplicate case ID")
            result[case_id] = row
        return result

    baseline_cases = by_case(baseline_cases_value, "baseline")
    revised_cases = by_case(revised_cases_value, "revised")
    if baseline_cases.keys() != revised_cases.keys():
        raise EvaluationError("Prompt reports do not contain the same case IDs")

    baseline_slices = _record(baseline.get("slices"), "baseline slices")
    revised_slices = _record(revised.get("slices"), "revised slices")
    if baseline_slices.keys() != revised_slices.keys():
        raise EvaluationError("Prompt reports do not contain the same evaluation slices")
    slice_deltas: dict[str, Record] = {}
    safety_regressions: list[str] = []
    for name in sorted(baseline_slices):
        old = _record(baseline_slices[name], f"baseline slice {name}")
        new = _record(revised_slices[name], f"revised slice {name}")
        if old.get("total") != new.get("total"):
            raise EvaluationError(f"Prompt slice {name} has different case counts")
        old_passed = old.get("passed")
        new_passed = new.get("passed")
        if (
            isinstance(old_passed, bool)
            or not isinstance(old_passed, int)
            or isinstance(new_passed, bool)
            or not isinstance(new_passed, int)
        ):
            raise EvaluationError(f"Prompt slice {name} has an invalid pass count")
        delta = new_passed - old_passed
        slice_deltas[name] = {
            "baseline_passed": old_passed,
            "revised_passed": new_passed,
            "total": old["total"],
            "delta": delta,
            "safety_slice": name in SAFETY_SLICES,
        }
        if name in SAFETY_SLICES and delta < 0:
            safety_regressions.append(name)

    baseline_summary = _record(baseline.get("summary"), "baseline summary")
    revised_summary = _record(revised.get("summary"), "revised summary")
    baseline_passed = baseline_summary.get("passed_count")
    revised_passed = revised_summary.get("passed_count")
    if (
        isinstance(baseline_passed, bool)
        or not isinstance(baseline_passed, int)
        or isinstance(revised_passed, bool)
        or not isinstance(revised_passed, int)
    ):
        raise EvaluationError("Prompt reports have invalid overall pass counts")
    baseline_case_passes = sum(_score_passed(row, "baseline") for row in baseline_cases.values())
    revised_case_passes = sum(_score_passed(row, "revised") for row in revised_cases.values())
    if baseline_passed != baseline_case_passes:
        raise EvaluationError("Baseline overall pass count does not match its case results")
    if revised_passed != revised_case_passes:
        raise EvaluationError("Revised overall pass count does not match its case results")

    changed_cases: list[Record] = []
    for case_id in sorted(baseline_cases):
        old_score = _record(baseline_cases[case_id].get("score"), f"baseline score {case_id}")
        new_score = _record(revised_cases[case_id].get("score"), f"revised score {case_id}")
        if old_score.get("passed") != new_score.get("passed"):
            changed_cases.append(
                {
                    "case_id": case_id,
                    "baseline_passed": old_score.get("passed"),
                    "revised_passed": new_score.get("passed"),
                    "revised_failures": new_score.get("failures", []),
                }
            )

    delta = revised_passed - baseline_passed
    if delta > 0 and not safety_regressions:
        recommendation = "tool-clarity"
        rationale = (
            "More cases passed with no regression in a named safety slice; "
            "owner review is still required."
        )
    elif delta < 0 and not safety_regressions:
        recommendation = "baseline"
        rationale = (
            "The baseline passed more cases and the revised prompt did not improve safety slices."
        )
    else:
        recommendation = "no_clear_winner"
        rationale = (
            "Results are tied or trade off safety slices; do not choose by a weighted aggregate."
        )

    baseline_candidate = _record(baseline.get("candidate"), "baseline candidate")
    return _record(
        {
            "comparison_version": "s2-prompt-comparison-v1",
            "dataset_version": baseline["dataset_version"],
            "dataset_sha256": baseline["dataset_sha256"],
            "scoring_method": baseline["scoring_method"],
            "candidate": baseline_candidate,
            "baseline": {
                "run_id": baseline.get("run_id"),
                "prompt_version": baseline.get("prompt_version"),
                "prompt_schema_sha256": baseline.get("prompt_schema_sha256"),
                "passed_count": baseline_passed,
                "failed_count": 132 - baseline_passed,
                "published_list_cost_usd": baseline_summary.get("published_list_cost_usd"),
            },
            "revised": {
                "run_id": revised.get("run_id"),
                "prompt_version": revised.get("prompt_version"),
                "prompt_schema_sha256": revised.get("prompt_schema_sha256"),
                "passed_count": revised_passed,
                "failed_count": 132 - revised_passed,
                "published_list_cost_usd": revised_summary.get("published_list_cost_usd"),
            },
            "pass_delta": delta,
            "safety_regressions": safety_regressions,
            "slice_deltas": slice_deltas,
            "changed_cases": changed_cases,
            "recommendation": recommendation,
            "rationale": rationale,
            "limitations": [
                "The 132-case dataset is a bounded synthetic screening set, not statistical proof.",
                "Model response text is not retained, so aggregate scoring cannot establish "
                "natural-language quality.",
                "The recommendation is advisory; the owner reviews every slice and any regression.",
                "The deterministic discount matcher is bounded and is not a production "
                "coupon or dispatch gate.",
                "Input-token-count endpoint billing and quota treatment remain unknown.",
            ],
        },
        "prompt comparison",
    )


def _score_passed(case_result: Record, label: str) -> bool:
    score = _record(case_result.get("score"), f"{label} score")
    passed = score.get("passed")
    if not isinstance(passed, bool):
        raise EvaluationError(f"{label} case has an invalid passed flag")
    return passed


def render_comparison(report: dict[str, object], json_name: str) -> str:
    baseline = _record(report.get("baseline"), "comparison baseline")
    revised = _record(report.get("revised"), "comparison revised")
    slices = _record(report.get("slice_deltas"), "comparison slices")
    changes_value = report.get("changed_cases")
    if not isinstance(changes_value, list):
        raise EvaluationError("Prompt comparison changed_cases must be a list")
    candidate_id = _record(report.get("candidate"), "comparison candidate").get("candidate_id")
    lines = [
        "# S2 Luna Low prompt comparison",
        "",
        f"- Dataset: `{report.get('dataset_version')}` (`{report.get('dataset_sha256')}`)",
        f"- Candidate: `{candidate_id}`",
        f"- Scoring: `{report.get('scoring_method')}`",
        f"- Baseline: `{baseline.get('prompt_version')}` (`{baseline.get('run_id')}`), "
        f"{baseline.get('passed_count')}/132 passed",
        f"- Revised: `{revised.get('prompt_version')}` (`{revised.get('run_id')}`), "
        f"{revised.get('passed_count')}/132 passed",
        f"- Pass-count delta: `{report.get('pass_delta')}`",
        f"- Advisory: **{report.get('recommendation')}** — {report.get('rationale')}",
        "",
        "## Slice comparison",
        "",
        "| Slice | Baseline | Revised | Delta | Safety slice |",
        "| --- | ---: | ---: | ---: | --- |",
    ]
    for name, value in sorted(slices.items()):
        row = _record(value, f"comparison slice {name}")
        delta = row.get("delta")
        marker = "yes" if row.get("safety_slice") is True else "no"
        lines.append(
            f"| `{name}` | {row.get('baseline_passed')}/{row.get('total')} | "
            f"{row.get('revised_passed')}/{row.get('total')} | {delta:+} | {marker} |"
        )
    lines.extend(["", "## Cases with changed pass/fail outcome", ""])
    if changes_value:
        lines.extend(
            [
                "| Case | Baseline | Revised | Revised failure codes |",
                "| --- | --- | --- | --- |",
            ]
        )
        for value in changes_value:
            row = _record(value, "changed case")
            failures_value = row.get("revised_failures", [])
            failures = failures_value if isinstance(failures_value, list) else []
            lines.append(
                f"| `{row.get('case_id')}` | {row.get('baseline_passed')} | "
                f"{row.get('revised_passed')} | "
                f"{', '.join(str(item) for item in failures) or 'none'} |"
            )
    else:
        lines.append("No case changed its pass/fail outcome.")
    lines.extend(
        [
            "",
            "## Limitations",
            "",
        ]
    )
    limitations = report.get("limitations")
    if isinstance(limitations, list):
        lines.extend(f"- {item}" for item in limitations if isinstance(item, str))
    lines.extend(
        [
            "",
            f"Machine-readable comparison: [{json_name}]({json_name})",
            "",
        ]
    )
    return "\n".join(lines)


def main() -> int:
    parser = argparse.ArgumentParser(description="Compare two S2 Luna Low prompt reports.")
    parser.add_argument("--baseline", type=Path, required=True)
    parser.add_argument("--revised", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    try:
        report = compare_prompt_reports(
            _read_report(args.baseline),
            _read_report(args.revised),
        )
        json_path, markdown_path = publish_report_pair(
            dict[str, object](report),
            args.output,
            markdown_renderer=render_comparison,
        )
    except (EvaluationError, OSError) as error:
        print(f"S2 prompt comparison failed: {error}", file=sys.stderr)
        return 1
    print(f"Published {json_path} and {markdown_path}")
    print(f"Recommendation: {report['recommendation']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
