"""Fingerprint deploy gate (#157, ADR-0031 decision 4, spec EVW-3 and EVW-4).

Runs in `deploy-aws.yml` before the deploy job, on the checked-out commit. It finds the live
evaluation report (`evals/release/results/eval-*/report.json`) whose fingerprint equals the
commit's. Deploys only run from `main`, so a report in the tree is a merged report.

- No matching report, or G1..G3 failing: blocked.
- G4/G5 failing: a warning; the deploy goes on only with a non-empty override reason, which is
  written to the job summary.

A report counts only if its recorded fingerprint inputs hash to its fingerprint and equal the
commit's own inputs, so a report edited by hand to claim another fingerprint does not match.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from scripts.eval_fingerprint import ROOT, fingerprint, fingerprint_inputs

RESULTS = Path("evals") / "release" / "results"
SAFETY_GATES = ("G1", "G2", "G3")
MAX_REASON = 300


@dataclass
class Decision:
    blocked: bool
    fingerprint: str
    report: str | None = None
    lines: list[str] = field(default_factory=list)


def _digest(inputs: object) -> str:
    canonical = json.dumps(inputs, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def clean_reason(raw: str) -> str:
    """One line, no Markdown code marks, bounded; empty means no override."""
    text = re.sub(r"\s+", " ", raw.replace("`", "'")).strip()
    return text[:MAX_REASON]


def matching_reports(root: Path, digest: str, inputs: dict[str, Any]) -> list[tuple[str, dict]]:
    found: list[tuple[str, dict]] = []
    for path in sorted((root / RESULTS).glob("eval-*/report.json")):
        try:
            report = json.loads(path.read_text(encoding="utf-8"))
        except OSError, ValueError:
            continue
        if (
            isinstance(report, dict)
            and report.get("kind") == "live-evaluation"
            and report.get("fingerprint") == digest
            and report.get("fingerprint_inputs") == inputs
            and _digest(report.get("fingerprint_inputs")) == digest
        ):
            found.append((path.parent.relative_to(root).as_posix(), report))
    return sorted(found, key=lambda item: str(item[1].get("completed_at", "")))


def _gates(report: dict[str, Any]) -> tuple[bool, bool]:
    """Safety and quality, recomputed from every set's gates; anything missing fails."""
    sets = report.get("sets")
    if not isinstance(sets, dict) or not sets:
        return False, False
    safety = quality = True
    for item in sets.values():
        gates = item.get("gates") if isinstance(item, dict) else None
        if not isinstance(gates, dict):
            return False, False
        safety = safety and all(gates.get(name) is True for name in SAFETY_GATES)
        quality = quality and gates.get("G4_G5") is True
    return (
        safety and report.get("safety_pass") is True,
        quality and report.get("quality_pass") is True,
    )


def decide(root: Path, override_reason: str) -> Decision:
    digest = fingerprint(root)
    decision = Decision(blocked=True, fingerprint=digest)
    reports = matching_reports(root, digest, fingerprint_inputs(root))
    if not reports:
        decision.lines.append(
            f"BLOCKED: no merged live evaluation report for fingerprint `{digest}`. "
            "Run llm-eval.yml on main, approve it, and merge its report PR."
        )
        return decision
    name, report = reports[-1]
    decision.report = name
    safety, quality = _gates(report)
    decision.lines.append(f"Report: `{name}` (fingerprint `{digest}`).")
    if not safety:
        decision.lines.append("BLOCKED: a safety gate (G1..G3) failed in the matching report.")
        return decision
    if not quality:
        reason = clean_reason(override_reason)
        if not reason:
            decision.lines.append(
                "BLOCKED: quality gates G4/G5 warn in the matching report; deploy again with "
                "a quality_override_reason."
            )
            return decision
        decision.lines.append(f"WARNING: G4/G5 failed; deployed with override reason: `{reason}`")
    else:
        decision.lines.append("PASS: G1..G5 pass in the matching report.")
    decision.blocked = False
    return decision


def main() -> int:
    decision = decide(ROOT, os.environ.get("QUALITY_OVERRIDE_REASON", ""))
    text = "\n".join(["## Evaluation deploy gate (#157)", "", *decision.lines, ""])
    print(text, flush=True)
    summary = os.environ.get("GITHUB_STEP_SUMMARY")
    if summary:
        with open(summary, "a", encoding="utf-8") as handle:
            handle.write(text)
    return 1 if decision.blocked else 0


if __name__ == "__main__":
    sys.exit(main())
