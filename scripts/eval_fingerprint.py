"""Behavior fingerprint for the live evaluation and the deploy gate (#135, ADR-0031, EVW-2).

The fingerprint is a SHA-256 over the model, the prompt version, the evaluation token limits,
and every file that can change what the proposal step does or how its result is scored. A
report is valid for a deploy only when its fingerprint equals the deployed commit's.

Every engine module and every policy file must be classified: covered, or excluded with a
reason. A new file in those folders fails EVW-2 until someone decides which it is, so a
behavior change cannot slip past the gate by living in a new file.
"""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path

from engine.proposal_provider import MODEL, VERSION

ROOT = Path(__file__).resolve().parents[1]
SCHEMA = 1
# The approved evaluation token limits (#87, Q17); the CI entry uses exactly these.
INPUT_TOKEN_LIMIT = 4000
OUTPUT_TOKEN_LIMIT = 8192

# Release-gates spec: "a change to the agent, prompts, model, policy, or gate".
COVERED: tuple[str, ...] = (
    # The proposal step: prompt, provider settings (model, reasoning effort), output contract,
    # budget, PII tokenization of the model input, retrieval, and the deployed proposal path.
    "src/engine/capacity_proposal.py",
    "src/engine/full_stack_proposal.py",
    "src/engine/privacy.py",
    "src/engine/proposal.py",
    "src/engine/proposal_budget.py",
    "src/engine/proposal_provider.py",
    "src/engine/retrieval.py",
    # The deterministic gate and the fixed-rule fallback.
    "src/engine/fixed_rule.py",
    "src/engine/gate_ledger.py",
    "src/engine/policy.py",
    # The synthetic policy snapshot the model retrieves from.
    "policies/synthetic.json",
    # The fixed synthetic datasets (tuned and held-out) and how results are scored.
    "evals/llm/s2_golden_set.json",
    "evals/llm/s2_reviews.json",
    "evals/release/heldout_cases.json",
    "scripts/poc2_evaluation_cases.py",
    "scripts/poc2_evaluation_live.py",
    "scripts/poc2_evaluation_runner.py",
    "scripts/release_gate_cases.py",
    "scripts/release_gate_ci.py",
    "scripts/release_gate_eval.py",
    "scripts/release_gate_gates.py",
    "scripts/s2_llm_eval.py",
)

# Engine modules that never change the proposal or the gate decision. Each has a reason.
EXCLUDED: dict[str, str] = {
    "src/engine/__init__.py": "package marker",
    "src/engine/approval_api.py": "operator approval after the proposal; tested in make ci",
    "src/engine/approval_cli.py": "operator approval after the proposal; tested in make ci",
    "src/engine/approval_data.py": "operator approval data; tested in make ci",
    "src/engine/auth.py": "operator authentication; tested in make ci",
    "src/engine/breaker.py": "runtime guard on live traffic, not proposal behavior",
    "src/engine/channels.py": "delivery after approval; the evaluation never dispatches",
    "src/engine/config.py": "database URL parsing",
    "src/engine/container_stats.py": "container metrics export only; never changes a proposal",
    "src/engine/coupon_code.py": "coupon code format after approval",
    "src/engine/db.py": "durable run storage",
    "src/engine/erp_cli.py": "operator CLI",
    "src/engine/execution.py": "execution after approval; the evaluation never executes",
    "src/engine/full_stack_init.py": "stack setup",
    "src/engine/full_stack_worker.py": "scan loop that creates runs",
    "src/engine/odoo.py": "reads carts; the evaluation uses fixture carts",
    "src/engine/ports.py": "interfaces only",
    "src/engine/privacy_cli.py": "operator CLI",
    "src/engine/runs.py": "run states",
    "src/engine/runtime_state.py": "runtime guard on live traffic, not proposal behavior",
    "src/engine/synthetic_scan.py": "scan rules that create runs",
    "src/engine/telemetry.py": "export only; never changes a proposal",
    "src/engine/worker.py": "scan loop that creates runs",
}
# Folders whose files must all be classified (EVW-2). Migrations are storage schema.
CLASSIFIED_GLOBS: tuple[str, ...] = ("src/engine/*.py", "policies/*")


def file_digest(root: Path, relative: str) -> str:
    return hashlib.sha256((root / relative).read_bytes()).hexdigest()


def fingerprint_inputs(root: Path = ROOT) -> dict[str, object]:
    return {
        "schema": SCHEMA,
        "model": MODEL,
        "prompt_version": VERSION,
        "input_token_limit": INPUT_TOKEN_LIMIT,
        "output_token_limit": OUTPUT_TOKEN_LIMIT,
        "files": {path: file_digest(root, path) for path in sorted(COVERED)},
    }


def fingerprint(root: Path = ROOT) -> str:
    canonical = json.dumps(fingerprint_inputs(root), sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def unclassified(root: Path = ROOT) -> list[str]:
    known = set(COVERED) | set(EXCLUDED)
    found = {
        path.relative_to(root).as_posix()
        for pattern in CLASSIFIED_GLOBS
        for path in root.glob(pattern)
        if path.is_file()
    }
    return sorted(found - known)


def main() -> int:
    parser = argparse.ArgumentParser(description="Print the evaluation behavior fingerprint")
    parser.add_argument("--paths", action="store_true", help="print the covered paths instead")
    args = parser.parse_args()
    missing = unclassified()
    if missing:
        print(f"unclassified behavior files (add to COVERED or EXCLUDED): {missing}")
        return 1
    print("\n".join(sorted(COVERED)) if args.paths else fingerprint())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
