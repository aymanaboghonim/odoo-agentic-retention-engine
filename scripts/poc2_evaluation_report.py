from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path
from typing import Any


def publish_report(parent: Path, phase: str, report: dict[str, Any]) -> Path:
    if phase not in {"smoke", "evaluation"}:
        raise ValueError("invalid evaluation phase")
    if not parent.is_dir() or not os.access(parent, os.W_OK):
        raise ValueError("report parent must be an existing writable directory")
    cases = report.get("cases")
    expected = report.get("expected_case_ids")
    if (
        not isinstance(cases, list)
        or not isinstance(expected, list)
        or not expected
        or any(not isinstance(case_id, str) or not case_id for case_id in expected)
        or any(not isinstance(case, dict) for case in cases)
        or [case.get("case_id") for case in cases] != expected
        or len(set(expected)) != len(expected)
    ):
        raise ValueError("report requires all expected cases exactly once and in order")
    destination = parent / f"poc2-69-{phase}"
    if destination.exists():
        raise FileExistsError("evaluation report already exists")
    action_cases = [case for case in cases if case.get("expected_action") is not None]
    action_passed = sum(case.get("action_pass") is True for case in action_cases)
    price_cases = [case for case in cases if case.get("price_checked") is True]
    price_passed = sum(case.get("price_pass") is True for case in price_cases)
    contracts_passed = sum(case.get("contract_pass") is True for case in cases)
    with tempfile.TemporaryDirectory(prefix=".poc2-69-", dir=parent) as temporary:
        path = Path(temporary)
        (path / "raw.json").write_text(
            json.dumps(report, indent=2, sort_keys=True, ensure_ascii=False) + "\n",
            encoding="utf-8",
        )
        lines = [
            f"# POC-2 {phase} evaluation",
            "",
            f"- Dataset: `{report['dataset_version']}` (`{report['dataset_sha256']}`)",
            f"- Model: `{report['model']}`; prompt/schema: `{report['prompt_version']}`",
            f"- Evaluation contract SHA-256: `{report['contract_sha256']}`",
            f"- Valid runtime contracts: {contracts_passed}/{len(cases)}.",
            f"- Action checks passed: {action_passed}/{len(action_cases)} "
            "(cases with no expected action are excluded).",
            f"- Structured price checks passed: {price_passed}/{len(price_cases)}.",
            f"- Estimated generation spend: USD {report['actual_usd']}; "
            f"conservative aggregate reservation: USD {report['reserved_usd']}.",
            "- Model action, stored gate outcome, state and usage are in [raw.json](raw.json).",
            "- Dialect inputs use the Arabic runtime and are exploratory. Fixture prices are "
            "reviewed synthetic values; fixture cost is a synthetic half-price assumption.",
            "- The financial gate uses a fresh in-memory ledger for each evaluation run; "
            "database budget and one-coupon-per-cart contention are not measured here.",
            "- No coupon or message dispatch was performed by this evaluation. Language quality, "
            "actual delivery and provisional release-gate approval require separate owner review.",
            "- Count-only billing and actual account charges are unknown; the generation "
            "estimate is not a bill or a ceiling on unknown charges.",
            "",
            "| Case | Language | Action | Run state | Gate outcome |",
            "| :-- | :-- | :-- | :-- | :-- |",
        ]
        for case in cases:
            gate = case.get("gate_result")
            outcome = (
                gate.get("outcome", "not reached") if isinstance(gate, dict) else "not reached"
            )
            lines.append(
                f"| {case['case_id']} | {case.get('dialect') or case.get('language')} | "
                f"{case.get('action') or 'none'} | {case.get('state')} | {outcome} |"
            )
        lines.append("")
        (path / "README.md").write_text("\n".join(lines), encoding="utf-8")
        os.rename(path, destination)
    return destination
