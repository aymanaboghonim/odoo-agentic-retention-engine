"""Production-scale bilingual release-gate evaluation (#87, spec release-gates-and-kill-switch.md).

Preflight is read-only and never calls the provider. `--execute-live` runs the cases through the
real proposal step (paid, aggregate ceiling USD 2.00) and publishes `raw.json` plus `README.md`
only after the whole run succeeds. A full evaluation needs the SHA-256 of an owner-reviewed
one-case smoke report (the POC-2 rule).
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import socket
import urllib.error
import uuid
from collections.abc import Mapping
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path

import sqlalchemy as sa
from sqlalchemy import Engine

from engine.db import create_engine
from engine.full_stack_proposal import progress
from engine.full_stack_worker import WorkerSettings
from engine.gate_ledger import InMemoryGateLedger
from engine.odoo import OdooReader
from engine.policy import Gate, GateConfig
from engine.proposal import ProposalSettings
from engine.proposal_provider import MODEL, VERSION, OpenAIClient, ProviderError, ProviderResult
from engine.retrieval import (
    MODEL_FILES,
    MODEL_ID,
    MODEL_REPOSITORY,
    MODEL_REVISION,
    LocalE5Embedder,
    PostgresRetriever,
)
from scripts.poc2_evaluation_live import contract_digest
from scripts.poc2_evaluation_runner import evaluate_case, validate_settings, validate_unattempted
from scripts.release_gate_cases import (
    CATEGORY,
    RELEASE_BUDGET_USD,
    RELEASE_PREFIX,
    RELEASE_PROFILE,
    RELEASE_REVIEW_ID,
    CaseInventory,
    ReleaseCase,
    release_fixture_cart,
    select_release_cases,
)
from scripts.release_gate_gates import REPORT_PREFIX, evaluate_gates, publish_release_report
from scripts.s2_llm_eval import GoldenDataset, load_dataset, load_reviews

# Large enough that the daily budget never decides a case; each case is judged on its own merits.
RELEASE_DAILY_BUDGET = Decimal("100000")


def is_timeout(error: ProviderError) -> bool:
    cause: object = error.__cause__
    if isinstance(cause, urllib.error.URLError):
        cause = cause.reason
    return isinstance(cause, TimeoutError | socket.timeout)


class RetryOnTimeout:
    """Evaluation-only: one extra attempt per provider call, and only after a timeout.

    A timed-out call may still have been billed, so a retry can cost one more call; the
    aggregate ceiling covers it. Any other provider error is not retried. The production
    proposal path is unchanged.
    """

    def __init__(self, provider: OpenAIClient) -> None:
        self._provider = provider
        self.retries = 0

    def count_tokens(self, payload: Mapping[str, object]) -> int:
        try:
            return self._provider.count_tokens(payload)
        except ProviderError as error:
            if not is_timeout(error):
                raise
            self.retries += 1
            return self._provider.count_tokens(payload)

    def generate(self, payload: Mapping[str, object], output_limit: int) -> ProviderResult:
        try:
            return self._provider.generate(payload, output_limit)
        except ProviderError as error:
            if not is_timeout(error):
                raise
            self.retries += 1
            return self._provider.generate(payload, output_limit)


def validate_release_gate_config(gate: GateConfig) -> None:
    if (
        gate.currency != "USD"
        or gate.daily_budget != RELEASE_DAILY_BUDGET
        or gate.category_min_margin.get(CATEGORY) != Decimal("0.20")
        or str(gate.timezone) != "Asia/Riyadh"
    ):
        raise ValueError("the fixed synthetic release-gate financial configuration is required")


def budget_totals(engine: Engine) -> tuple[Decimal, Decimal]:
    with engine.connect() as conn:
        reserved, actual = conn.execute(
            sa.text(
                "SELECT COALESCE(SUM(GREATEST(reserved_usd, "
                "COALESCE(actual_usd, reserved_usd))), 0), "
                "COALESCE(SUM(actual_usd), 0) FROM proposal_calls WHERE review_id=:review"
            ),
            {"review": RELEASE_REVIEW_ID},
        ).one()
    return reserved, actual


def verify_smoke(
    parent: Path, dataset: GoldenDataset, cases: tuple[ReleaseCase, ...], digest: str | None
) -> dict[str, object]:
    if not digest or len(digest) != 64 or any(c not in "0123456789abcdef" for c in digest):
        raise ValueError("the owner-reviewed smoke report SHA-256 is required")
    raw = (parent / f"{REPORT_PREFIX}-smoke" / "raw.json").read_bytes()
    if hashlib.sha256(raw).hexdigest() != digest:
        raise ValueError("smoke report does not match the reviewed SHA-256")
    report = json.loads(raw)
    if (
        not isinstance(report, dict)
        or report.get("phase") != "smoke"
        or report.get("dataset_sha256") != dataset.sha256
        or report.get("model") != MODEL
        or report.get("prompt_version") != VERSION
        or report.get("contract_sha256") != contract_digest(cases)
        or report.get("review_id") != RELEASE_REVIEW_ID
        or report.get("expected_case_ids") != [cases[0].case_id]
        or not isinstance(report.get("cases"), list)
        or len(report["cases"]) != 1
    ):
        raise ValueError("smoke report does not match the current evaluation contract")
    smoke_case = report["cases"][0]
    if not isinstance(smoke_case, dict) or smoke_case.get("case_id") != cases[0].case_id:
        raise ValueError("smoke report does not match the current evaluation contract")
    return smoke_case


def verify_smoke_run(engine: Engine, case: ReleaseCase, result: dict[str, object]) -> None:
    try:
        run_id = uuid.UUID(str(result["run_id"]))
    except (KeyError, ValueError) as error:
        raise ValueError("smoke report has no valid durable run ID") from error
    with engine.connect() as conn:
        row = conn.execute(
            sa.text(
                "SELECT r.state, c.status, c.failure_reason, p.proposal, p.gate_result "
                "FROM runs r JOIN proposal_calls c ON c.run_id=r.id "
                "LEFT JOIN agent_proposals p ON p.run_id=r.id "
                "WHERE r.id=:run AND r.idempotency_key=:key AND c.review_id=:review"
            ),
            {
                "run": run_id,
                "key": f"{RELEASE_PREFIX}:smoke:{case.case_id}",
                "review": RELEASE_REVIEW_ID,
            },
        ).first()
    if (
        row is None
        or row.status != "settled"
        or row.failure_reason is not None
        or row.state != result.get("state")
        or row.proposal != result.get("proposal")
        or row.gate_result != result.get("gate_result")
    ):
        raise ValueError("smoke report does not match its settled durable run")


def preflight(
    engine: Engine,
    settings: ProposalSettings,
    gate: GateConfig,
    dataset: GoldenDataset,
    inventory: CaseInventory,
    *,
    phase: str,
    parent: Path,
    smoke_digest: str | None,
) -> tuple[ReleaseCase, ...]:
    """Read-only checks. Returns the cases this phase will run."""
    validate_settings(settings, RELEASE_PROFILE)
    validate_release_gate_config(gate)
    cases = inventory.cases[:1] if phase == "smoke" else inventory.cases
    if not parent.is_dir() or not os.access(parent, os.W_OK):
        raise ValueError("report directory must exist and be writable")
    if (parent / f"{REPORT_PREFIX}-{phase}").exists() or (
        parent / f".{REPORT_PREFIX}-{phase}-partial"
    ).exists():
        raise FileExistsError("a complete or partial report already exists; inspect it first")
    if phase == "evaluation":
        smoke = verify_smoke(parent, dataset, inventory.cases, smoke_digest)
        verify_smoke_run(engine, inventory.cases[0], smoke)
    validate_unattempted(engine, cases, phase=phase, profile=RELEASE_PROFILE)
    with engine.connect() as conn:
        languages = set(
            conn.execute(
                sa.text("SELECT DISTINCT language FROM policy_passages WHERE model_id=:model"),
                {"model": MODEL_ID},
            ).scalars()
        )
        mismatch = conn.execute(
            sa.text("SELECT EXISTS (SELECT 1 FROM policy_passages WHERE model_id<>:model)"),
            {"model": MODEL_ID},
        ).scalar_one()
    if not {"en", "ar"}.issubset(languages) or mismatch:
        raise ValueError("the pinned policy index must contain English and Arabic only")
    reserved, _ = budget_totals(engine)
    required = settings.rate.estimate(settings.input_tokens, settings.output_tokens) * len(cases)
    if reserved + required > RELEASE_BUDGET_USD:
        raise ValueError("the aggregate review budget cannot reserve all planned calls")
    return cases


def main() -> None:
    parser = argparse.ArgumentParser(description="Run the authorized #87 release-gate evaluation")
    parser.add_argument("--phase", choices=("smoke", "evaluation"), required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--approved-smoke-sha256")
    parser.add_argument("--execute-live", action="store_true")
    args = parser.parse_args()

    print("stage=preflight status=started", flush=True)
    worker = WorkerSettings.from_env(os.environ)
    settings = ProposalSettings.from_env(os.environ)
    gate_config = GateConfig.from_env(os.environ)
    dataset = load_dataset()
    inventory = select_release_cases(dataset, load_reviews(dataset))
    summary = inventory.summary()
    print(
        f"stage=inventory included={summary['included']} "
        f"by_language={summary['included_by_language']} excluded={summary['excluded']}",
        flush=True,
    )
    provider = RetryOnTimeout(OpenAIClient(os.environ.get("OPENAI_API_KEY", "")))
    reader = OdooReader(worker.odoo_url)
    from huggingface_hub import snapshot_download  # type: ignore[import-not-found]

    with progress("release-eval-preflight-prerequisites"):
        reader.verify_synthetic_readonly_role()
        snapshot_download(
            repo_id=MODEL_REPOSITORY,
            revision=MODEL_REVISION,
            token=False,
            local_files_only=True,
            allow_patterns=MODEL_FILES,
        )
    engine = create_engine(worker.engine_url)
    try:
        with progress("release-eval-preflight-database"):
            cases = preflight(
                engine,
                settings,
                gate_config,
                dataset,
                inventory,
                phase=args.phase,
                parent=args.output_dir,
                smoke_digest=args.approved_smoke_sha256,
            )
        print(
            "stage=preflight status=passed; provider access, inference, output quality and "
            "delivery were not checked",
            flush=True,
        )
        if not args.execute_live:
            return
        partial = args.output_dir / f".{REPORT_PREFIX}-{args.phase}-partial"
        partial.mkdir()
        with progress("release-eval-model-load"):
            retriever = PostgresRetriever(engine, LocalE5Embedder())
        gate = Gate(gate_config, InMemoryGateLedger())
        results: list[dict[str, object]] = []
        for index, case in enumerate(cases, start=1):
            with progress(f"release-eval-case-{index}-of-{len(cases)}"):
                result = evaluate_case(
                    engine,
                    dataset,
                    case,
                    phase=args.phase,
                    provider=provider,
                    retriever=retriever,
                    gate=gate,
                    settings=settings,
                    profile=RELEASE_PROFILE,
                    build_cart=release_fixture_cart,
                )
            result["provider_timeout_retries"] = provider.retries
            provider.retries = 0
            results.append(result)
            (partial / f"{index:02d}.json").write_text(
                json.dumps(result, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
            )
            print(f"stage=evaluation completed={index}/{len(cases)}", flush=True)
        reserved, actual = budget_totals(engine)
        report: dict[str, object] = {
            "phase": args.phase,
            "completed_at": datetime.now(UTC).isoformat(),
            "dataset_version": dataset.version,
            "dataset_sha256": dataset.sha256,
            "model": MODEL,
            "prompt_version": VERSION,
            "contract_sha256": contract_digest(inventory.cases),
            "review_id": RELEASE_REVIEW_ID,
            "budget_ceiling_usd": str(RELEASE_BUDGET_USD),
            "input_token_limit": settings.input_tokens,
            "output_token_limit": settings.output_tokens,
            "rate_provenance": settings.rate.provenance,
            "reserved_usd": str(reserved),
            "actual_usd": str(actual),
            "inventory": summary,
            "expected_case_ids": [case.case_id for case in cases],
            "cases": results,
        }
        if args.phase == "evaluation":
            report["gates"] = evaluate_gates(cases, results)  # type: ignore[arg-type]
        destination = publish_release_report(args.output_dir, args.phase, report)
        for index in range(1, len(cases) + 1):
            (partial / f"{index:02d}.json").unlink()
        partial.rmdir()
        print(f"stage=evaluation status=complete report={destination}", flush=True)
    finally:
        engine.dispose()


if __name__ == "__main__":
    main()
