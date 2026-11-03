from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sys
import tempfile
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import cast

from scripts.s2_llm_eval import (
    CaseScore,
    EvaluationCase,
    EvaluationError,
    GoldenDataset,
    ModelResult,
    load_dataset,
    load_reviews,
    publish_report_pair,
    publish_report_set,
)
from scripts.s2_llm_providers import create_live_client
from scripts.s2_llm_runner import (
    SUPPORTED_CANDIDATES,
    Candidate,
    LiveApproval,
    ProviderErrors,
    candidate_by_id,
    candidate_set_preflight_failures,
    live_preflight_failures,
    run_evaluation,
    write_review_assets,
)
from scripts.s2_odoo_smoke import (
    OdooSmokeTools,
    load_approved_inventory,
    validate_odoo_schema,
)
from scripts.spike_s1_odoo import JsonValue, OdooApiError, OdooClient

DEFAULT_APPROVAL_PATH = Path("evals/llm/s2_live_approval.json")
DEFAULT_INVENTORY_PATH = Path("evals/llm/s2_odoo_inventory.json")
DEFAULT_REVIEW_PATH = Path("evals/llm/s2_reviews.json")
DEFAULT_WORKSHEET_PATH = Path("evals/llm/s2_review_worksheet.md")
DEFAULT_RESULTS_PATH = Path("evals/llm/results")
RUN_ID_PATTERN = re.compile(r"^[a-z0-9][a-z0-9-]{0,31}$")
SHA256_PATTERN = re.compile(r"^[0-9a-f]{64}$")


@dataclass(frozen=True)
class SmokeInputs:
    dataset: GoldenDataset
    reviews: dict[str, object]
    candidate: Candidate
    cases: tuple[EvaluationCase, ...]
    approval: LiveApproval
    inventory: dict[str, object]
    odoo: OdooClient
    activity_user_id: int
    activity_type_id: int


@dataclass(frozen=True)
class FullInputs:
    dataset: GoldenDataset
    reviews: dict[str, object]
    candidates: tuple[Candidate, ...]
    approvals: dict[str, LiveApproval]


def _object(value: object, context: str) -> dict[str, object]:
    if not isinstance(value, dict) or not all(isinstance(key, str) for key in value):
        raise EvaluationError(f"{context} must be a JSON object with string keys")
    return cast(dict[str, object], value)


def _read_object(path: Path, context: str) -> dict[str, object]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as error:
        raise EvaluationError(f"{context} file {path} does not exist") from error
    except OSError as error:
        raise EvaluationError(f"Could not read {context} file {path}") from error
    except json.JSONDecodeError as error:
        raise EvaluationError(f"{context} file {path} is not valid JSON") from error
    return _object(value, context)


def _optional_int(value: object) -> int | None:
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int):
        return None
    return value


def _required_int(value: object) -> int:
    result = _optional_int(value)
    return result if result is not None else 0


def _decimal_or_zero(value: object, context: str) -> Decimal:
    if value is None or isinstance(value, bool):
        return Decimal("0")
    if not isinstance(value, (str, int, float)):
        raise EvaluationError(f"{context} must be a decimal string or number")
    try:
        result = Decimal(str(value))
    except InvalidOperation as error:
        raise EvaluationError(f"{context} is not a valid decimal amount") from error
    if not result.is_finite():
        raise EvaluationError(f"{context} must be finite")
    return result


def _load_approval(path: Path) -> dict[str, object]:
    approval = _read_object(path, "S2 live approval")
    approved_ids = approval.get("approved_candidate_ids")
    if not isinstance(approved_ids, list) or not all(
        isinstance(item, str) for item in approved_ids
    ):
        raise EvaluationError("S2 live approval needs an approved_candidate_ids string list")
    if not isinstance(approval.get("owner_approved"), bool):
        raise EvaluationError("S2 live approval needs an explicit owner_approved boolean")
    _object(approval.get("candidates"), "S2 candidate limits")
    _object(approval.get("odoo"), "S2 Odoo approval settings")
    return approval


def _approval_for(
    document: dict[str, object],
    candidate_id: str,
    *,
    inventory_approved: bool = False,
) -> LiveApproval:
    all_settings = _object(document.get("candidates"), "S2 candidate limits")
    raw_settings = all_settings.get(candidate_id)
    settings = _object(raw_settings, f"limits for {candidate_id}") if raw_settings else {}
    approved_ids = document.get("approved_candidate_ids")
    if not isinstance(approved_ids, list):
        raise EvaluationError("S2 live approval candidate list is invalid")
    ids = tuple(item for item in approved_ids if isinstance(item, str))
    source = settings.get("limits_source")
    checked_on = settings.get("limits_checked_on")
    report_hash = document.get("approved_smoke_report_sha256")
    return LiveApproval(
        owner_approved=document.get("owner_approved") is True,
        approved_candidate_ids=ids,
        spend_ceiling_usd=_decimal_or_zero(
            settings.get("spend_ceiling_usd"),
            f"{candidate_id} spend ceiling",
        ),
        rpm=_optional_int(settings.get("rpm")),
        tpm=_optional_int(settings.get("tpm")),
        limits_source=source if isinstance(source, str) else "",
        limits_checked_on=checked_on if isinstance(checked_on, str) else "",
        max_input_tokens_per_request=_required_int(settings.get("max_input_tokens_per_request")),
        max_output_tokens_per_request=_required_int(settings.get("max_output_tokens_per_request")),
        odoo_inventory_approved=inventory_approved,
        smoke_report_sha256=report_hash if isinstance(report_hash, str) else None,
    )


def select_smoke_cases(dataset: GoldenDataset) -> tuple[EvaluationCase, ...]:
    selected: list[EvaluationCase] = []
    for fixture_id, scenario_id, expected_action in (
        ("cart-open-001", "recovery", "draft_message"),
        ("cart-recent-004", "recent_cart", "no_action"),
    ):
        case = next(
            (
                item
                for item in dataset.cases
                if item.fixture_id == fixture_id
                and item.scenario_id == scenario_id
                and item.expected_action == expected_action
                and item.language == "en"
                and item.requires_cart_tool
            ),
            None,
        )
        if case is None:
            raise EvaluationError(f"Dataset has no reviewed smoke case for {fixture_id}")
        selected.append(case)
    return tuple(selected)


def _new_odoo_client() -> OdooClient:
    if os.environ.get("S2_DISPOSABLE_INSTANCE") != "YES":
        raise EvaluationError("Set S2_DISPOSABLE_INSTANCE=YES for a fresh local Odoo instance")
    return OdooClient(
        base_url=os.environ.get("ODOO_URL", "http://localhost:8069"),
        database=os.environ.get("ODOO_DB_NAME", "odoo"),
        api_key=os.environ.get("ODOO_API_KEY", ""),
    )


def _approval_value(document: dict[str, object], key: str) -> int:
    odoo_settings = _object(document.get("odoo"), "S2 Odoo approval settings")
    value = _optional_int(odoo_settings.get(key))
    if value is None or value < 1:
        raise EvaluationError(f"S2 Odoo approval needs a positive {key}")
    return value


def _prepare_smoke(args: argparse.Namespace) -> SmokeInputs:
    dataset = load_dataset()
    reviews = load_reviews(dataset, args.review_ledger)
    inventory = load_approved_inventory(args.inventory_json)
    document = _load_approval(args.approval)
    candidate = candidate_by_id(args.candidate)
    cases = select_smoke_cases(dataset)
    approval = _approval_for(
        document,
        candidate.candidate_id,
        inventory_approved=True,
    )
    failures = live_preflight_failures(
        dataset,
        reviews,
        candidate,
        cases,
        approval,
        mode="smoke",
    )
    if failures:
        raise EvaluationError(f"Live smoke preflight failed: {'; '.join(failures)}")
    odoo = _new_odoo_client()
    return SmokeInputs(
        dataset=dataset,
        reviews=reviews,
        candidate=candidate,
        cases=cases,
        approval=approval,
        inventory=inventory,
        odoo=odoo,
        activity_user_id=_approval_value(document, "activity_user_id"),
        activity_type_id=_approval_value(document, "activity_type_id"),
    )


def _verify_smoke_report(
    path: Path | None,
    approved_hash: str | None,
    reviewer_id: object,
    dataset: GoldenDataset,
) -> str:
    if approved_hash is None or not SHA256_PATTERN.fullmatch(approved_hash):
        raise EvaluationError("S2 live approval has no valid approved smoke report SHA-256")
    if path is None:
        raise EvaluationError("Full-run preflight requires --smoke-report")
    if not isinstance(reviewer_id, str) or not reviewer_id.strip():
        raise EvaluationError("S2 live approval needs a smoke_report_reviewer_id")
    try:
        digest = hashlib.sha256(path.read_bytes()).hexdigest()
    except FileNotFoundError as error:
        raise EvaluationError(f"Smoke report file {path} does not exist") from error
    except OSError as error:
        raise EvaluationError(f"Could not read smoke report file {path}") from error
    if digest != approved_hash:
        raise EvaluationError("Smoke report does not match the owner-approved SHA-256")
    report = _read_object(path, "S2 smoke report")
    if (
        report.get("run_mode") != "smoke"
        or report.get("dataset_version") != dataset.version
        or report.get("dataset_sha256") != dataset.sha256
    ):
        raise EvaluationError("Smoke report does not match the current S2 dataset")
    candidate = _object(report.get("candidate"), "S2 smoke report candidate")
    candidate_id = candidate.get("candidate_id")
    if not isinstance(candidate_id, str):
        raise EvaluationError("Smoke report has no candidate ID")
    candidate_by_id(candidate_id)
    return digest


def _selected_candidates(ids: list[str] | None) -> tuple[Candidate, ...]:
    selected_ids = ids or [item.candidate_id for item in SUPPORTED_CANDIDATES]
    if len(set(selected_ids)) != len(selected_ids):
        raise EvaluationError("Candidate selection contains duplicate IDs")
    return tuple(candidate_by_id(candidate_id) for candidate_id in selected_ids)


def _prepare_full(args: argparse.Namespace) -> FullInputs:
    dataset = load_dataset()
    reviews = load_reviews(dataset, args.review_ledger)
    document = _load_approval(args.approval)
    candidates = _selected_candidates(args.candidate)
    approved_hash = document.get("approved_smoke_report_sha256")
    _verify_smoke_report(
        args.smoke_report,
        approved_hash if isinstance(approved_hash, str) else None,
        document.get("smoke_report_reviewer_id"),
        dataset,
    )
    approvals = {
        candidate.candidate_id: _approval_for(document, candidate.candidate_id)
        for candidate in candidates
    }
    failures = candidate_set_preflight_failures(
        dataset,
        reviews,
        candidates,
        dataset.cases,
        approvals,
        mode="full",
    )
    if failures:
        raise EvaluationError(f"Live Full-run preflight failed: {'; '.join(failures)}")
    return FullInputs(dataset, reviews, candidates, approvals)


def _verify_odoo_activity_resources(
    client: OdooClient,
    activity_user_id: int,
    activity_type_id: int,
) -> None:
    users = client.search_read(
        "res.users",
        cast(list[JsonValue], [["id", "=", activity_user_id]]),
        ["id", "active"],
        limit=1,
    )
    if len(users) != 1 or users[0].get("active") is not True:
        raise OdooApiError("The approved Odoo activity user is missing or inactive")
    activity_types = client.search_read(
        "mail.activity.type",
        cast(list[JsonValue], [["id", "=", activity_type_id]]),
        ["id", "active"],
        limit=1,
    )
    if len(activity_types) != 1 or activity_types[0].get("active") is False:
        raise OdooApiError("The approved Odoo activity type is missing or inactive")


def _prepare_report_paths(directory: Path, filenames: list[str]) -> list[Path]:
    directory.mkdir(parents=True, exist_ok=True)
    paths = [directory / filename for filename in filenames]
    markdown_paths = [path.with_suffix(".md") for path in paths]
    if any(path.exists() for path in [*paths, *markdown_paths]):
        raise EvaluationError("Refusing to overwrite an existing report for this run ID")
    with tempfile.TemporaryFile(dir=directory):
        pass
    return paths


def _run_smoke(args: argparse.Namespace) -> int:
    if not args.execute_live:
        raise EvaluationError("Pass --execute-live to make the explicitly approved live smoke")
    if not RUN_ID_PATTERN.fullmatch(args.run_id):
        raise EvaluationError("run_id must use lowercase letters, digits, and hyphens")
    prepared = _prepare_smoke(args)
    json_path = _prepare_report_paths(
        args.output_dir,
        [f"{args.run_id}-{prepared.candidate.candidate_id}.json"],
    )[0]
    validate_odoo_schema(prepared.odoo)
    tools = OdooSmokeTools(prepared.odoo, prepared.dataset, prepared.inventory)
    for case in prepared.cases:
        fixture = prepared.dataset.fixtures[case.fixture_id]
        cart_id = fixture.get("cart_id")
        if not isinstance(cart_id, str):
            raise EvaluationError(f"Smoke fixture {case.fixture_id} has no cart ID")
        context = tools.get_cart_context(case, cart_id)
        if "error" in context:
            raise OdooApiError(f"Odoo rejected the reviewed smoke cart {cart_id}")
    _verify_odoo_activity_resources(
        prepared.odoo,
        prepared.activity_user_id,
        prepared.activity_type_id,
    )
    live_client = create_live_client(
        prepared.candidate,
        max_input_tokens=(
            prepared.approval.max_input_tokens_per_request
            if prepared.candidate.provider == "google"
            else None
        ),
        max_output_tokens=prepared.approval.max_output_tokens_per_request,
    )
    created_activities: list[tuple[int, str]] = []

    def gated_activity(
        case: EvaluationCase, result: ModelResult, score: CaseScore
    ) -> tuple[int, bool] | None:
        if not score.passed:
            return None
        outcome = tools.create_activity_if_safe(
            case,
            result,
            score,
            activity_user_id=prepared.activity_user_id,
            activity_type_id=prepared.activity_type_id,
        )
        if outcome is not None and outcome[1]:
            cart = tools.carts.get(case.fixture_id)
            cart_id = cart.get("cart_id") if cart is not None else None
            if not isinstance(cart_id, str):
                raise EvaluationError("Created activity has no verified S2 cart")
            created_activities.append((outcome[0], cart_id))
        return outcome

    try:
        report = run_evaluation(
            prepared.dataset,
            prepared.candidate,
            live_client,
            prepared.cases,
            reviews=prepared.reviews,
            mode="smoke",
            approval=prepared.approval,
            run_id=args.run_id,
            tools=tools,
            smoke_action=gated_activity,
        )
    finally:
        for activity_id, cart_id in reversed(created_activities):
            tools.cleanup_activity(activity_id, cart_id)
    report["smoke_activity_cleanup"] = "completed" if created_activities else "not-required"
    report["odoo_inventory_sha256"] = prepared.inventory.get("manifest_sha256")
    publish_report_pair(report, json_path)
    summary = _object(report.get("summary"), "S2 smoke report summary")
    print(f"Published {json_path} and {json_path.with_suffix('.md')}")
    if summary.get("failed_count") != 0:
        print("Smoke completed with deterministic case failures.", file=sys.stderr)
        return 2
    return 0


def _run_full(args: argparse.Namespace) -> int:
    if not args.execute_live:
        raise EvaluationError("Pass --execute-live to make the explicitly approved full run")
    if not RUN_ID_PATTERN.fullmatch(args.run_id):
        raise EvaluationError("run_id must use lowercase letters, digits, and hyphens")
    prepared = _prepare_full(args)
    output_dir = args.output_dir / args.run_id
    json_paths = _prepare_report_paths(
        output_dir,
        [f"{candidate.candidate_id}.json" for candidate in prepared.candidates],
    )
    reports: list[tuple[dict[str, object], Path]] = []
    clients = {
        candidate.candidate_id: create_live_client(
            candidate,
            max_input_tokens=prepared.approvals[
                candidate.candidate_id
            ].max_input_tokens_per_request,
            max_output_tokens=prepared.approvals[
                candidate.candidate_id
            ].max_output_tokens_per_request,
        )
        for candidate in prepared.candidates
    }
    group_spent: dict[str, Decimal] = {}
    for candidate, json_path in zip(prepared.candidates, json_paths, strict=True):
        approval = prepared.approvals[candidate.candidate_id]
        group = candidate.budget_group
        remaining = candidate.cost_cap_usd - group_spent.get(group, Decimal(0)) if group else None
        report = run_evaluation(
            prepared.dataset,
            candidate,
            clients[candidate.candidate_id],
            prepared.dataset.cases,
            reviews=prepared.reviews,
            mode="full",
            approval=approval,
            run_id=args.run_id,
            budget_remaining_usd=remaining,
        )
        if group:
            report_summary = _object(report.get("summary"), "S2 full report summary")
            group_spent[group] = group_spent.get(group, Decimal(0)) + Decimal(
                str(report_summary["published_list_cost_usd"])
            )
        reports.append((report, json_path))
    published = publish_report_set(reports)
    failed = False
    for (json_path, markdown_path), (report, _) in zip(published, reports, strict=True):
        summary = _object(report.get("summary"), "S2 full report summary")
        if summary.get("failed_count") != 0:
            failed = True
        print(f"Published {json_path} and {markdown_path}")
    if failed:
        print("Full comparison completed with deterministic case failures.", file=sys.stderr)
        return 2
    return 0


def _preflight(args: argparse.Namespace) -> int:
    if args.mode == "smoke":
        _prepare_smoke(args)
    else:
        _prepare_full(args)
    print("Preflight passed. No provider request or Odoo API call was made.")
    return 0


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Review and run the S2 LLM evaluation.")
    commands = parser.add_subparsers(dest="command", required=True)
    review = commands.add_parser("review-assets")
    review.add_argument("--worksheet", type=Path, default=DEFAULT_WORKSHEET_PATH)
    review.add_argument("--review-ledger", type=Path, default=DEFAULT_REVIEW_PATH)

    preflight = commands.add_parser("preflight")
    preflight.add_argument("--mode", choices=("smoke", "full"), required=True)
    preflight.add_argument("--approval", type=Path, default=DEFAULT_APPROVAL_PATH)
    preflight.add_argument("--review-ledger", type=Path, default=DEFAULT_REVIEW_PATH)
    preflight.add_argument("--inventory-json", type=Path, default=DEFAULT_INVENTORY_PATH)
    preflight.add_argument("--smoke-report", type=Path)
    preflight.add_argument("--candidate")
    preflight.add_argument("--candidates", action="append", dest="candidate_ids")

    smoke = commands.add_parser("smoke")
    smoke.add_argument("--execute-live", action="store_true")
    smoke.add_argument("--candidate", required=True)
    smoke.add_argument("--run-id", required=True)
    smoke.add_argument("--approval", type=Path, default=DEFAULT_APPROVAL_PATH)
    smoke.add_argument("--review-ledger", type=Path, default=DEFAULT_REVIEW_PATH)
    smoke.add_argument("--inventory-json", type=Path, default=DEFAULT_INVENTORY_PATH)
    smoke.add_argument("--output-dir", type=Path, default=DEFAULT_RESULTS_PATH / "smoke")

    full = commands.add_parser("full")
    full.add_argument("--execute-live", action="store_true")
    full.add_argument("--run-id", required=True)
    full.add_argument("--approval", type=Path, default=DEFAULT_APPROVAL_PATH)
    full.add_argument("--review-ledger", type=Path, default=DEFAULT_REVIEW_PATH)
    full.add_argument("--smoke-report", type=Path, required=True)
    full.add_argument("--candidates", action="append", dest="candidate")
    full.add_argument("--output-dir", type=Path, default=DEFAULT_RESULTS_PATH / "full")
    return parser


def main() -> int:
    args = _parser().parse_args()
    try:
        if args.command == "review-assets":
            dataset = load_dataset()
            write_review_assets(dataset, args.worksheet, args.review_ledger)
            print(f"Wrote {args.worksheet} and {args.review_ledger}")
            return 0
        if args.command == "preflight":
            if args.mode == "smoke":
                if not args.candidate:
                    raise EvaluationError("Smoke preflight requires --candidate")
            else:
                args.candidate = args.candidate_ids
            return _preflight(args)
        if args.command == "smoke":
            return _run_smoke(args)
        if args.command == "full":
            return _run_full(args)
    except ImportError:
        print(
            "S2 live provider SDK is unavailable; install the locked optional group "
            "with `uv sync --group llm-eval`.",
            file=sys.stderr,
        )
        return 1
    except ProviderErrors as error:
        print(f"S2 evaluation incomplete: {error}", file=sys.stderr)
        return 3
    except (EvaluationError, OdooApiError, OSError, ValueError) as error:
        print(f"S2 evaluation failed: {error}", file=sys.stderr)
        return 1
    raise EvaluationError(f"Unsupported command {args.command!r}")


if __name__ == "__main__":
    raise SystemExit(main())
