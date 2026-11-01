from __future__ import annotations

import hashlib
import json
import os
from dataclasses import asdict, dataclass
from datetime import date, datetime, timezone
from decimal import Decimal
from pathlib import Path
from typing import Callable, cast

from scripts.s2_llm_eval import (
    Candidate,
    CartContextTools,
    CaseScore,
    EvaluationCase,
    EvaluationError,
    FixtureTools,
    GoldenDataset,
    ModelResult,
    ProgressReporter,
    TokenRates,
    Usage,
    aggregate_slices,
    calculate_cost,
    review_failures,
    score_case,
)
from scripts.s2_llm_providers import (
    OUTPUT_SCHEMA,
    SYSTEM_PROMPT,
    TOOL_PARAMETERS,
    LLMClient,
)

FACTS_CHECKED_ON = "2026-11-01"
GPT_LUNA_GROUP_CAP_USD = Decimal("2.00")
SUPPORTED_CANDIDATES = (
    Candidate(
        candidate_id="gemini-3.8-flash-low",
        provider="google",
        model="gemini-3.8-flash",
        configuration={"thinking_level": "low"},
        facts=("F-004",),
        token_rates=TokenRates(Decimal("0.75"), Decimal("3.75"), Decimal("0.075")),
        batch_token_rates=TokenRates(Decimal("0.375"), Decimal("1.875"), Decimal("0.0375")),
        cost_cap_usd=Decimal("5.00"),
        max_input_tokens=1_048_576,
        max_output_tokens=65_536,
        facts_checked_on=FACTS_CHECKED_ON,
        rates_valid_through="2026-12-31",
    ),
    Candidate(
        candidate_id="gemini-3.5-flash-lite-minimal",
        provider="google",
        model="gemini-3.5-flash-lite",
        configuration={"thinking_level": "minimal"},
        facts=("F-004", "F-027"),
        token_rates=TokenRates(Decimal("0.30"), Decimal("2.50")),
        batch_token_rates=TokenRates(Decimal("0.15"), Decimal("1.25")),
        cost_cap_usd=Decimal("2.00"),
        max_input_tokens=1_048_576,
        max_output_tokens=65_536,
        facts_checked_on=FACTS_CHECKED_ON,
        budget_group="gemini-3.5-flash-lite",
    ),
    Candidate(
        candidate_id="gpt-6-luna-none",
        provider="openai",
        model="gpt-6-luna",
        configuration={"reasoning_effort": "none"},
        facts=("F-005",),
        token_rates=TokenRates(Decimal("0.10"), Decimal("0.50"), Decimal("0.01")),
        batch_token_rates=TokenRates(Decimal("0.05"), Decimal("0.25"), Decimal("0.005")),
        cost_cap_usd=GPT_LUNA_GROUP_CAP_USD,
        max_input_tokens=922_000,
        max_output_tokens=128_000,
        facts_checked_on=FACTS_CHECKED_ON,
        budget_group="gpt-6-luna",
    ),
    Candidate(
        candidate_id="gpt-6-luna-low",
        provider="openai",
        model="gpt-6-luna",
        configuration={"reasoning_effort": "low"},
        facts=("F-005",),
        token_rates=TokenRates(Decimal("0.10"), Decimal("0.50"), Decimal("0.01")),
        batch_token_rates=TokenRates(Decimal("0.05"), Decimal("0.25"), Decimal("0.005")),
        cost_cap_usd=GPT_LUNA_GROUP_CAP_USD,
        max_input_tokens=922_000,
        max_output_tokens=128_000,
        facts_checked_on=FACTS_CHECKED_ON,
        budget_group="gpt-6-luna",
    ),
)


@dataclass(frozen=True)
class LiveApproval:
    owner_approved: bool
    approved_candidate_ids: tuple[str, ...]
    spend_ceiling_usd: Decimal
    rpm: int | None
    tpm: int | None
    batch_queue_limit: int | None
    limits_source: str
    limits_checked_on: str
    max_input_tokens_per_request: int
    max_output_tokens_per_request: int
    odoo_inventory_approved: bool = False
    smoke_report_sha256: str | None = None


def candidate_by_id(candidate_id: str) -> Candidate:
    for candidate in SUPPORTED_CANDIDATES:
        if candidate.candidate_id == candidate_id:
            return candidate
    raise EvaluationError(f"Unsupported S2 candidate {candidate_id!r}")


def live_preflight_failures(
    dataset: GoldenDataset,
    reviews: dict[str, object],
    candidate: Candidate,
    cases: tuple[EvaluationCase, ...],
    approval: LiveApproval,
    *,
    mode: str,
    environment: dict[str, str] | None = None,
    as_of: date | None = None,
) -> list[str]:
    failures: list[str] = []
    now = as_of or date.today()
    if mode not in {"smoke", "batch"}:
        return [f"Unsupported live run mode {mode!r}"]
    if not cases:
        failures.append("No evaluation cases were selected")
    elif len({case.case_id for case in cases}) != len(cases):
        failures.append("Evaluation case selection contains duplicate IDs")
    if dataset.status != "draft":
        failures.append("The source dataset must remain in draft status")
    failures.extend(review_failures(dataset, reviews))
    if not approval.owner_approved:
        failures.append("Owner approval is required before live provider calls")
    if candidate.candidate_id not in approval.approved_candidate_ids:
        failures.append(f"Owner approval does not include {candidate.candidate_id}")
    expected_key = "GEMINI_API_KEY" if candidate.provider == "google" else "OPENAI_API_KEY"
    env = os.environ if environment is None else environment
    if not env.get(expected_key):
        failures.append(f"Set {expected_key} before a live run")
    if mode == "smoke" and not approval.odoo_inventory_approved:
        failures.append("Owner approval of the seeded Odoo inventory is required")
    if mode == "smoke" and (
        approval.rpm is None or approval.tpm is None or approval.rpm < 1 or approval.tpm < 1
    ):
        failures.append("Active positive RPM and TPM limits are required for smoke")
    if mode == "batch" and (approval.batch_queue_limit is None or approval.batch_queue_limit < 1):
        failures.append("An active positive Batch queue limit is required")
    if mode == "batch" and (
        approval.smoke_report_sha256 is None
        or len(approval.smoke_report_sha256) != 64
        or any(character not in "0123456789abcdef" for character in approval.smoke_report_sha256)
    ):
        failures.append("An owner-approved smoke report SHA-256 is required for Batch")
    if not approval.limits_source.strip():
        failures.append("A source for the active provider limits is required")
    try:
        checked_on = date.fromisoformat(approval.limits_checked_on)
        if checked_on > now:
            failures.append("Provider limits cannot have a future check date")
        elif (now - checked_on).days > 90:
            failures.append("Provider limits are stale; verify them again")
    except ValueError:
        failures.append("Provider limits need an ISO check date")
    try:
        facts_checked_on = date.fromisoformat(candidate.facts_checked_on or "")
        if (now - facts_checked_on).days > 90:
            failures.append("Candidate facts are stale; verify them again")
    except ValueError:
        failures.append("Candidate facts have no valid verification date")
    if candidate.rates_valid_through is not None:
        try:
            if now > date.fromisoformat(candidate.rates_valid_through):
                failures.append("Candidate rates have expired; refresh the facts register")
        except ValueError:
            failures.append("Candidate rates have an invalid expiry date")
    if approval.spend_ceiling_usd <= 0:
        failures.append("An explicit positive spend ceiling is required")
    if approval.spend_ceiling_usd > candidate.cost_cap_usd:
        failures.append(f"Spend ceiling exceeds the recorded cap of ${candidate.cost_cap_usd:.2f}")
    if candidate.max_input_tokens is None or not (
        1 <= approval.max_input_tokens_per_request <= candidate.max_input_tokens
    ):
        failures.append("Input-token ceiling is missing or above the official model limit")
    if candidate.max_output_tokens is None or not (
        1 <= approval.max_output_tokens_per_request <= candidate.max_output_tokens
    ):
        failures.append("Output-token ceiling is missing or above the official model limit")
    if not failures:
        reserved_cost = reserved_cost_usd(candidate, cases, approval, mode=mode)
        if reserved_cost > approval.spend_ceiling_usd:
            failures.append(
                f"Worst-case reservation ${reserved_cost:.6f} exceeds the approved "
                f"ceiling ${approval.spend_ceiling_usd:.2f}"
            )
    return failures


def reserved_cost_usd(
    candidate: Candidate,
    cases: tuple[EvaluationCase, ...],
    approval: LiveApproval,
    *,
    mode: str,
) -> Decimal:
    rates = _rates_for_mode(candidate, mode)
    request_count = len(cases) + sum(case.requires_cart_tool for case in cases)
    maximum_usage = Usage(
        input_tokens=request_count * approval.max_input_tokens_per_request,
        output_tokens=request_count * approval.max_output_tokens_per_request,
    )
    return calculate_cost(
        maximum_usage,
        TokenRates(rates.input_per_million, rates.output_per_million),
    )


def candidate_set_preflight_failures(
    dataset: GoldenDataset,
    reviews: dict[str, object],
    candidates: tuple[Candidate, ...],
    cases: tuple[EvaluationCase, ...],
    approvals: dict[str, LiveApproval],
    *,
    mode: str,
    environment: dict[str, str] | None = None,
    as_of: date | None = None,
) -> list[str]:
    failures: list[str] = []
    seen: set[str] = set()
    grouped_reservations: dict[str, Decimal] = {}
    grouped_caps: dict[str, Decimal] = {}
    for candidate in candidates:
        if candidate.candidate_id in seen:
            failures.append(f"Duplicate candidate {candidate.candidate_id}")
            continue
        seen.add(candidate.candidate_id)
        approval = approvals.get(candidate.candidate_id)
        if approval is None:
            failures.append(f"Missing approval settings for {candidate.candidate_id}")
            continue
        failures.extend(
            live_preflight_failures(
                dataset,
                reviews,
                candidate,
                cases,
                approval,
                mode=mode,
                environment=environment,
                as_of=as_of,
            )
        )
        if candidate.budget_group is not None:
            group = candidate.budget_group
            grouped_reservations[group] = grouped_reservations.get(group, Decimal(0)) + (
                reserved_cost_usd(candidate, cases, approval, mode=mode)
            )
            grouped_caps[group] = candidate.cost_cap_usd
    for group, reserved in grouped_reservations.items():
        if reserved > grouped_caps[group]:
            failures.append(
                f"Combined reservation for {group} ${reserved:.6f} exceeds "
                f"the shared cap ${grouped_caps[group]:.2f}"
            )
    return failures


def run_evaluation(
    dataset: GoldenDataset,
    candidate: Candidate,
    client: LLMClient,
    cases: tuple[EvaluationCase, ...],
    *,
    reviews: dict[str, object],
    mode: str,
    approval: LiveApproval,
    run_id: str,
    tools: CartContextTools | None = None,
    progress: ProgressReporter | None = None,
    environment: dict[str, str] | None = None,
    smoke_action: Callable[[EvaluationCase, ModelResult, CaseScore], tuple[int, bool] | None]
    | None = None,
) -> dict[str, object]:
    if mode not in {"smoke", "batch"}:
        raise EvaluationError(f"Unsupported run mode {mode!r}")
    failures = live_preflight_failures(
        dataset,
        reviews,
        candidate,
        cases,
        approval,
        mode=mode,
        environment=environment,
    )
    if failures:
        raise EvaluationError(f"Live preflight failed: {'; '.join(failures)}")
    if mode == "smoke" and tools is None:
        raise EvaluationError("Smoke run requires the reviewed Odoo-backed cart context adapter")
    progress_reporter = progress or ProgressReporter()
    started_at = datetime.now(timezone.utc)
    if mode == "smoke":
        results_list: list[ModelResult] = []
        for case in cases:

            def operation(case: EvaluationCase = case) -> ModelResult:
                if tools is None:
                    raise EvaluationError("Smoke run lost its Odoo-backed tools adapter")
                return client.run_case(case, dataset, tools)

            result = progress_reporter.run_stage(
                f"smoke_case_{case.case_id}",
                1,
                operation,
            )
            if not isinstance(result, ModelResult):
                raise EvaluationError(f"Provider returned an invalid result for {case.case_id}")
            results_list.append(result)
        results = tuple(results_list)
    else:
        batch_results = progress_reporter.run_stage(
            "batch_evaluation",
            len(cases),
            lambda: client.run_batch(cases, dataset, FixtureTools(dataset), run_id=run_id),
        )
        if not isinstance(batch_results, tuple):
            raise EvaluationError("Batch client returned an invalid result collection")
        batch_items = cast(tuple[object, ...], batch_results)
        if not all(isinstance(result, ModelResult) for result in batch_items):
            raise EvaluationError("Batch client returned an invalid result collection")
        results = cast(tuple[ModelResult, ...], batch_items)
    completed_at = datetime.now(timezone.utc)
    if tuple(result.case_id for result in results) != tuple(case.case_id for case in cases):
        raise EvaluationError("Provider results do not match the requested case order")
    scores = tuple(
        score_case(dataset, case, result) for case, result in zip(cases, results, strict=True)
    )
    smoke_actions: list[dict[str, object]] = []
    if mode == "smoke" and smoke_action is not None:
        action = smoke_action
        for case, result, score in zip(cases, results, scores, strict=True):

            def action_operation(
                case: EvaluationCase = case,
                result: ModelResult = result,
                score: CaseScore = score,
            ) -> tuple[int, bool] | None:
                return action(case, result, score)

            outcome = progress_reporter.run_stage(
                f"smoke_activity_{case.case_id}",
                1,
                action_operation,
            )
            if outcome is not None and (
                not isinstance(outcome, tuple)
                or len(outcome) != 2
                or isinstance(outcome[0], bool)
                or not isinstance(outcome[0], int)
                or not isinstance(outcome[1], bool)
            ):
                raise EvaluationError("Smoke action returned an invalid activity outcome")
            smoke_actions.append(
                {
                    "case_id": case.case_id,
                    "activity_created": outcome[1] if isinstance(outcome, tuple) else False,
                }
            )
    usage = _total_usage(results)
    rates = _rates_for_mode(candidate, mode)
    cost = calculate_cost(usage, rates)
    if cost > approval.spend_ceiling_usd:
        raise EvaluationError(
            f"Observed cost ${cost:.6f} exceeds approved ceiling ${approval.spend_ceiling_usd:.2f}"
        )
    if cost > candidate.cost_cap_usd:
        raise EvaluationError(
            f"Observed cost ${cost:.6f} exceeds candidate cap ${candidate.cost_cap_usd:.2f}"
        )
    report = _build_report(
        dataset,
        candidate,
        cases,
        results,
        scores,
        usage,
        rates,
        cost,
        approval,
        mode,
        run_id,
        started_at,
        completed_at,
    )
    report["smoke_actions"] = smoke_actions
    return report


def _rates_for_mode(candidate: Candidate, mode: str) -> TokenRates:
    if mode == "batch":
        if candidate.batch_token_rates is None:
            raise EvaluationError(f"{candidate.candidate_id} has no verified Batch rates")
        return candidate.batch_token_rates
    return candidate.token_rates


def _total_usage(results: tuple[ModelResult, ...]) -> Usage:
    cached_values = [result.usage.cached_input_tokens for result in results]
    reasoning_values = [result.usage.reasoning_tokens for result in results]
    cached = sum(value for value in cached_values if value is not None)
    reasoning = sum(value for value in reasoning_values if value is not None)
    return Usage(
        input_tokens=sum(result.usage.input_tokens for result in results),
        output_tokens=sum(result.usage.output_tokens for result in results),
        cached_input_tokens=cached if all(value is not None for value in cached_values) else None,
        reasoning_tokens=(
            reasoning if all(value is not None for value in reasoning_values) else None
        ),
    )


def _build_report(
    dataset: GoldenDataset,
    candidate: Candidate,
    cases: tuple[EvaluationCase, ...],
    results: tuple[ModelResult, ...],
    scores: tuple[CaseScore, ...],
    usage: Usage,
    rates: TokenRates,
    cost: Decimal,
    approval: LiveApproval,
    mode: str,
    run_id: str,
    started_at: datetime,
    completed_at: datetime,
) -> dict[str, object]:
    typed_scores = scores
    discount_safety_scores = tuple(
        score for score in typed_scores if score.scenario_id in {"discount_refusal", "injection"}
    )
    request_count = len(cases) + sum(case.requires_cart_tool for case in cases)
    prompt_hash = hashlib.sha256(
        json.dumps(
            {
                "system_prompt": SYSTEM_PROMPT,
                "tool_schema": TOOL_PARAMETERS,
                "output_schema": OUTPUT_SCHEMA,
            },
            ensure_ascii=False,
            sort_keys=True,
        ).encode("utf-8")
    ).hexdigest()
    per_case: list[dict[str, object]] = []
    for result, score in zip(results, typed_scores, strict=True):
        per_case.append(
            {
                "case_id": result.case_id,
                "score": asdict(score),
                "usage": asdict(result.usage),
            }
        )
    return {
        "report_version": "s2-report-v2",
        "run_id": run_id,
        "run_mode": mode,
        "created_at": completed_at.isoformat(),
        "started_at": started_at.isoformat(),
        "elapsed_seconds": round((completed_at - started_at).total_seconds(), 3),
        "candidate": {
            "candidate_id": candidate.candidate_id,
            "provider": candidate.provider,
            "model": candidate.model,
            "configuration": candidate.configuration,
            "facts": list(candidate.facts),
            "facts_checked_on": candidate.facts_checked_on,
            "maximum_input_tokens": candidate.max_input_tokens,
            "maximum_output_tokens": candidate.max_output_tokens,
            "budget_group": candidate.budget_group,
        },
        "dataset_version": dataset.version,
        "dataset_sha256": dataset.sha256,
        "prompt_schema_sha256": prompt_hash,
        "scoring_method": "deterministic-s2-v2",
        "cost_basis": {
            "published_list_input_per_million_usd": str(rates.input_per_million),
            "published_list_output_per_million_usd": str(rates.output_per_million),
            "cached_input_per_million_usd": (
                str(rates.cached_input_per_million)
                if rates.cached_input_per_million is not None
                else None
            ),
            "fact_ids": list(candidate.facts),
        },
        "limits": {
            "rpm": approval.rpm,
            "tpm": approval.tpm,
            "batch_queue_limit": approval.batch_queue_limit,
            "source": approval.limits_source,
            "checked_on": approval.limits_checked_on,
            "max_input_tokens_per_request": approval.max_input_tokens_per_request,
            "max_output_tokens_per_request": approval.max_output_tokens_per_request,
            "approved_spend_ceiling_usd": str(approval.spend_ceiling_usd),
            "approved_smoke_report_sha256": approval.smoke_report_sha256,
        },
        "summary": {
            "input_count": len(cases),
            "request_count": request_count,
            "passed_count": sum(score.passed for score in typed_scores),
            "failed_count": sum(not score.passed for score in typed_scores),
            "discount_safety_case_count": len(discount_safety_scores),
            "discount_safety_pass_count": sum(
                score.discount_safety_pass for score in discount_safety_scores
            ),
            "input_tokens": usage.input_tokens,
            "output_tokens": usage.output_tokens,
            "cached_input_tokens": usage.cached_input_tokens,
            "reasoning_tokens": usage.reasoning_tokens,
            "published_list_cost_usd": f"{cost:.6f}",
        },
        "slices": aggregate_slices(typed_scores),
        "case_results": per_case,
        "decision": {
            "status": "unresolved",
            "note": "This report does not select a model; human review is required.",
        },
        "limitations": [
            "All evaluation inputs and Odoo fixtures are synthetic.",
            "Dialect slices are exploratory and are not statistical quality claims.",
            "Human language-quality judgments and model-as-judge scores are advisory.",
            "Batch turnaround is not per-request latency.",
        ],
    }


def write_review_assets(
    dataset: GoldenDataset,
    worksheet_path: Path,
    ledger_path: Path,
) -> tuple[Path, Path]:
    from scripts.s2_llm_eval import initial_review_ledger, load_reviews, render_review_worksheet

    worksheet_path.parent.mkdir(parents=True, exist_ok=True)
    ledger_path.parent.mkdir(parents=True, exist_ok=True)
    if not ledger_path.exists():
        ledger_path.write_text(
            json.dumps(initial_review_ledger(dataset), ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
    reviews = load_reviews(dataset, ledger_path)
    worksheet_path.write_text(
        render_review_worksheet(dataset, reviews),
        encoding="utf-8",
    )
    return worksheet_path, ledger_path
