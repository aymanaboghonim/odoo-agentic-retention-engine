from __future__ import annotations

import argparse
import hashlib
import json
import os
import uuid
from dataclasses import asdict
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
from engine.proposal_provider import MODEL, SCHEMA, SYSTEM, VERSION, OpenAIClient
from engine.retrieval import (
    MODEL_FILES,
    MODEL_ID,
    MODEL_REPOSITORY,
    MODEL_REVISION,
    LocalE5Embedder,
    PostgresRetriever,
)
from scripts.poc2_evaluation_cases import RuntimeCase, reviewed_cases
from scripts.poc2_evaluation_report import publish_report
from scripts.poc2_evaluation_runner import (
    EVALUATION_PREFIX,
    REVIEW_ID,
    evaluate_case,
    validate_settings,
    validate_unattempted,
)
from scripts.s2_llm_eval import GoldenDataset, load_dataset, load_reviews


def budget_totals(engine: Engine) -> tuple[Decimal, Decimal]:
    with engine.connect() as conn:
        reserved, actual = conn.execute(
            sa.text(
                "SELECT COALESCE(SUM(GREATEST(reserved_usd, "
                "COALESCE(actual_usd, reserved_usd))), 0), "
                "COALESCE(SUM(actual_usd), 0) FROM proposal_calls WHERE review_id=:review"
            ),
            {"review": REVIEW_ID},
        ).one()
    return reserved, actual


def contract_digest(cases: tuple[RuntimeCase, ...]) -> str:
    data = {
        "model": MODEL,
        "version": VERSION,
        "instructions": SYSTEM,
        "schema": SCHEMA,
        "cases": [asdict(case) for case in cases],
    }
    return hashlib.sha256(
        json.dumps(data, sort_keys=True, ensure_ascii=True, separators=(",", ":")).encode()
    ).hexdigest()


def verify_smoke(
    parent: Path, dataset: GoldenDataset, cases: tuple[RuntimeCase, ...], digest: str | None
) -> dict[str, object]:
    if not cases:
        raise ValueError("the approved case inventory is empty")
    if not digest or len(digest) != 64 or any(c not in "0123456789abcdef" for c in digest):
        raise ValueError("the owner-reviewed smoke report SHA-256 is required")
    raw = (parent / "poc2-69-smoke" / "raw.json").read_bytes()
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
        or report.get("review_id") != REVIEW_ID
        or report.get("expected_case_ids") != [cases[0].case_id]
        or not isinstance(report.get("cases"), list)
        or len(report["cases"]) != 1
        or not isinstance(report["cases"][0], dict)
        or report["cases"][0].get("case_id") != cases[0].case_id
    ):
        raise ValueError("smoke report does not match the current evaluation contract")
    return report["cases"][0]


def verify_smoke_run(engine: Engine, case: RuntimeCase, result: dict[str, object]) -> None:
    try:
        run_id = uuid.UUID(str(result["run_id"]))
    except (KeyError, ValueError) as error:
        raise ValueError("smoke report has no valid durable run ID") from error
    usage = result.get("usage")
    if not isinstance(usage, dict):
        raise ValueError("smoke report has no usage record")
    with engine.connect() as conn:
        row = conn.execute(
            sa.text(
                "SELECT r.state, c.status, c.failure_reason, c.input_tokens, c.output_tokens, "
                "c.actual_usd, c.reserved_usd, p.schema_version, p.proposal, p.gate_result "
                "FROM runs r "
                "JOIN proposal_calls c ON c.run_id=r.id "
                "LEFT JOIN agent_proposals p ON p.run_id=r.id "
                "WHERE r.id=:run AND r.idempotency_key=:key AND c.review_id=:review"
            ),
            {
                "run": run_id,
                "key": f"{EVALUATION_PREFIX}:smoke:{case.case_id}",
                "review": REVIEW_ID,
            },
        ).first()
    if (
        row is None
        or row.status != "settled"
        or row.failure_reason is not None
        or row.state != result.get("state")
        or row.input_tokens != usage.get("input_tokens")
        or row.output_tokens != usage.get("output_tokens")
        or str(row.actual_usd) != usage.get("actual_usd")
        or str(row.reserved_usd) != usage.get("reserved_usd")
        or row.proposal != result.get("proposal")
        or row.gate_result != result.get("gate_result")
        or (row.proposal is not None and row.schema_version != VERSION)
    ):
        raise ValueError("smoke report does not match its settled durable run")


def preflight(
    engine: Engine,
    settings: ProposalSettings,
    gate: GateConfig,
    dataset: GoldenDataset,
    cases: tuple[RuntimeCase, ...],
    *,
    phase: str,
    parent: Path,
    smoke_digest: str | None,
    inventory: tuple[RuntimeCase, ...],
) -> None:
    validate_settings(settings)
    if (
        gate.currency != "USD"
        or gate.daily_budget != Decimal("450")
        or gate.category_min_margin.get("Furniture / Office") != Decimal("0.20")
        or str(gate.timezone) != "Asia/Riyadh"
    ):
        raise ValueError("the approved synthetic financial gate configuration is required")
    if not parent.is_dir() or not os.access(parent, os.W_OK):
        raise ValueError("report directory must exist and be writable")
    if (parent / f"poc2-69-{phase}").exists() or (parent / f".poc2-69-{phase}-partial").exists():
        raise FileExistsError("a complete or partial report already exists; inspect it first")
    if phase == "evaluation":
        smoke = verify_smoke(parent, dataset, inventory, smoke_digest)
        verify_smoke_run(engine, inventory[0], smoke)
    validate_unattempted(engine, cases, phase=phase)
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
    remaining_calls = len(cases) + (4 if phase == "evaluation" else 0)
    required = (
        settings.rate.estimate(settings.input_tokens, settings.output_tokens) * remaining_calls
    )
    if reserved + required > Decimal("1"):
        raise ValueError("the aggregate review budget cannot reserve all planned calls")


def main() -> None:
    parser = argparse.ArgumentParser(description="Run the authorized #69 synthetic evaluation")
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
    selected = reviewed_cases(dataset, load_reviews(dataset))
    cases = selected[:1] if args.phase == "smoke" else selected
    provider = OpenAIClient(os.environ.get("OPENAI_API_KEY", ""))
    reader = OdooReader(worker.odoo_url)
    from huggingface_hub import snapshot_download  # type: ignore[import-not-found]

    with progress("evaluation-preflight-prerequisites"):
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
        with progress("evaluation-preflight-database"):
            preflight(
                engine,
                settings,
                gate_config,
                dataset,
                cases,
                phase=args.phase,
                parent=args.output_dir,
                smoke_digest=args.approved_smoke_sha256,
                inventory=selected,
            )
        print(
            "stage=preflight status=passed; provider access, inference, delivery and "
            "output quality were not checked",
            flush=True,
        )
        if not args.execute_live:
            return
        partial = args.output_dir / f".poc2-69-{args.phase}-partial"
        partial.mkdir()
        with progress("evaluation-model-load"):
            retriever = PostgresRetriever(engine, LocalE5Embedder())
        gate = Gate(gate_config, InMemoryGateLedger())
        results: list[dict[str, object]] = []
        for index, case in enumerate(cases, start=1):
            with progress(f"evaluation-case-{index}-of-{len(cases)}"):
                result = evaluate_case(
                    engine,
                    dataset,
                    case,
                    phase=args.phase,
                    provider=provider,
                    retriever=retriever,
                    gate=gate,
                    settings=settings,
                )
            results.append(result)
            (partial / f"{index:02d}.json").write_text(
                json.dumps(result, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
            )
            print(f"stage=evaluation completed={index}/{len(cases)}", flush=True)
        reserved, actual = budget_totals(engine)
        report = {
            "phase": args.phase,
            "completed_at": datetime.now(UTC).isoformat(),
            "dataset_version": dataset.version,
            "dataset_sha256": dataset.sha256,
            "model": MODEL,
            "prompt_version": VERSION,
            "contract_sha256": contract_digest(selected),
            "review_id": REVIEW_ID,
            "input_token_limit": settings.input_tokens,
            "output_token_limit": settings.output_tokens,
            "rate_provenance": settings.rate.provenance,
            "reserved_usd": str(reserved),
            "actual_usd": str(actual),
            "expected_case_ids": [case.case_id for case in cases],
            "cases": results,
        }
        destination = publish_report(args.output_dir, args.phase, report)
        for index in range(1, len(cases) + 1):
            (partial / f"{index:02d}.json").unlink()
        partial.rmdir()
        print(f"stage=evaluation status=complete report={destination}", flush=True)
    finally:
        engine.dispose()


if __name__ == "__main__":
    main()
