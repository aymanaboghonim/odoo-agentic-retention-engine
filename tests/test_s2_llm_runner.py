import json
from collections.abc import Sequence
from datetime import date
from decimal import Decimal
from pathlib import Path
from typing import cast

from scripts.s2_llm_eval import (
    EXPECTED_DIALECTS,
    CartContextTools,
    CaseScore,
    EvaluationCase,
    FixtureTools,
    GoldenDataset,
    ModelResult,
    Record,
    ToolCall,
    Usage,
    initial_review_ledger,
    load_dataset,
)
from scripts.s2_llm_runner import (
    SUPPORTED_CANDIDATES,
    LiveApproval,
    candidate_by_id,
    candidate_set_preflight_failures,
    live_preflight_failures,
    reserved_cost_usd,
    run_evaluation,
    write_review_assets,
)


def _approved_reviews(dataset: GoldenDataset) -> dict[str, object]:
    reviews = initial_review_ledger(dataset)
    assignments: dict[str, str] = {}
    for dialect in EXPECTED_DIALECTS:
        assignments[dialect] = f"reviewer-{dialect}"
    case_reviews: dict[str, dict[str, str]] = {}
    for case in dataset.cases:
        reviewer_id: str = (
            assignments[case.dialect] if case.dialect is not None else "reviewer-general"
        )
        case_reviews[case.case_id] = {"status": "approved", "reviewer_id": reviewer_id}
    reviews["reviewer_assignments"] = assignments
    reviews["cases"] = case_reviews
    return reviews


def _approval(
    candidate_id: str,
    *,
    mode: str = "smoke",
    spend_ceiling_usd: Decimal = Decimal("5"),
    max_input_tokens: int = 4096,
    max_output_tokens: int = 128,
) -> LiveApproval:
    return LiveApproval(
        owner_approved=True,
        approved_candidate_ids=(candidate_id,),
        spend_ceiling_usd=spend_ceiling_usd,
        rpm=10 if mode == "smoke" else None,
        tpm=20_000 if mode == "smoke" else None,
        batch_queue_limit=100 if mode == "batch" else None,
        limits_source="provider dashboard checked for test",
        limits_checked_on="2026-11-01",
        max_input_tokens_per_request=max_input_tokens,
        max_output_tokens_per_request=max_output_tokens,
        odoo_inventory_approved=(mode == "smoke"),
        smoke_report_sha256=("a" * 64) if mode == "batch" else None,
    )


def test_candidate_catalog_matches_verified_s2_comparison() -> None:
    assert [candidate.candidate_id for candidate in SUPPORTED_CANDIDATES] == [
        "gemini-3.8-flash-low",
        "gemini-3.5-flash-lite-minimal",
        "gpt-6-luna-none",
        "gpt-6-luna-low",
    ]
    assert candidate_by_id("gemini-3.8-flash-low").max_output_tokens == 65_536
    assert candidate_by_id("gpt-6-luna-none").max_output_tokens == 128_000
    assert candidate_by_id("gemini-3.5-flash-lite-minimal").configuration == {
        "thinking_level": "minimal"
    }


def test_live_preflight_rejects_unreviewed_cases_without_provider_call() -> None:
    dataset = load_dataset()
    candidate = candidate_by_id("gemini-3.8-flash-low")
    cases = (dataset.cases[0],)
    approval = _approval(candidate.candidate_id)

    failures = live_preflight_failures(
        dataset,
        initial_review_ledger(dataset),
        candidate,
        cases,
        approval,
        mode="smoke",
        environment={"GEMINI_API_KEY": "synthetic-test-key"},
        as_of=date(2026, 11, 1),
    )

    assert any("not approved" in failure or "no review record" in failure for failure in failures)
    assert any("qualified reviewer" in failure for failure in failures)


def test_live_preflight_requires_owner_inventory_limits_and_credentials() -> None:
    dataset = load_dataset()
    candidate = candidate_by_id("gemini-3.8-flash-low")
    case = dataset.cases[0]
    approval = LiveApproval(
        owner_approved=False,
        approved_candidate_ids=(),
        spend_ceiling_usd=Decimal("5"),
        rpm=None,
        tpm=None,
        batch_queue_limit=None,
        limits_source="",
        limits_checked_on="",
        max_input_tokens_per_request=4096,
        max_output_tokens_per_request=128,
        odoo_inventory_approved=False,
    )

    failures = live_preflight_failures(
        dataset,
        _approved_reviews(dataset),
        candidate,
        (case,),
        approval,
        mode="smoke",
        environment={},
        as_of=date(2026, 11, 1),
    )

    assert any("Owner approval" in failure for failure in failures)
    assert any("Odoo inventory" in failure for failure in failures)
    assert any("RPM and TPM" in failure for failure in failures)
    assert any("GEMINI_API_KEY" in failure for failure in failures)


def test_preflight_reserves_without_cached_input_savings() -> None:
    dataset = load_dataset()
    candidate = candidate_by_id("gemini-3.8-flash-low")
    cases = (dataset.cases[0],)
    approval = _approval(candidate.candidate_id, max_input_tokens=4096, max_output_tokens=1024)

    reserved = reserved_cost_usd(candidate, cases, approval, mode="smoke")
    expected = (
        Decimal(2 * 4096) * Decimal("0.75") + Decimal(2 * 1024) * Decimal("3.75")
    ) / Decimal(1_000_000)

    assert reserved == expected


def test_candidate_set_preflight_enforces_combined_gpt_budget() -> None:
    dataset = load_dataset()
    reviews = _approved_reviews(dataset)
    candidates = (
        candidate_by_id("gpt-6-luna-none"),
        candidate_by_id("gpt-6-luna-low"),
    )
    cases = dataset.cases
    approvals = {
        candidate.candidate_id: _approval(
            candidate.candidate_id,
            mode="batch",
            spend_ceiling_usd=Decimal("2"),
            max_input_tokens=922_000,
            max_output_tokens=128_000,
        )
        for candidate in candidates
    }

    failures = candidate_set_preflight_failures(
        dataset,
        reviews,
        candidates,
        cases,
        approvals,
        mode="batch",
        environment={"OPENAI_API_KEY": "synthetic-test-key"},
        as_of=date(2026, 11, 1),
    )

    assert any("Combined reservation" in failure for failure in failures)


def test_full_preflight_rejects_expired_gemini_rates() -> None:
    dataset = load_dataset()
    candidate = candidate_by_id("gemini-3.8-flash-low")
    cases = (dataset.cases[0],)
    approval = _approval(candidate.candidate_id, mode="batch")

    failures = live_preflight_failures(
        dataset,
        _approved_reviews(dataset),
        candidate,
        cases,
        approval,
        mode="batch",
        environment={"GEMINI_API_KEY": "synthetic-test-key"},
        as_of=date(2027, 1, 1),
    )

    assert any("rates have expired" in failure for failure in failures)


class FakeClient:
    def run_case(
        self, case: EvaluationCase, dataset: GoldenDataset, tools: CartContextTools
    ) -> ModelResult:
        del tools
        fixture = dataset.fixtures[case.fixture_id]
        calls = (
            (ToolCall("get_cart_context", {"cart_id": fixture["cart_id"]}),)
            if case.requires_cart_tool
            else ()
        )
        output: Record = {
            "action": case.expected_action,
            "customer_message": "PRIVATE MODEL TEXT MUST NOT BE REPORTED.",
            "price_claim": fixture.get("subtotal") if case.check_cart_price else None,
            "currency": fixture.get("currency") if case.check_cart_price else None,
        }
        return ModelResult(case.case_id, output, calls, Usage(120, 35, 0, 5))

    def run_batch(
        self,
        cases: Sequence[EvaluationCase],
        dataset: GoldenDataset,
        tools: CartContextTools,
        *,
        run_id: str,
    ) -> tuple[ModelResult, ...]:
        del run_id
        return tuple(self.run_case(case, dataset, tools) for case in cases)


def test_run_report_has_provenance_and_excludes_model_messages() -> None:
    dataset = load_dataset()
    case = next(case for case in dataset.cases if case.case_id == "enmsa-001-en")
    candidate = candidate_by_id("gemini-3.8-flash-low")
    approval = _approval(candidate.candidate_id)

    report = run_evaluation(
        dataset,
        candidate,
        FakeClient(),
        (case,),
        reviews=_approved_reviews(dataset),
        mode="smoke",
        approval=approval,
        run_id="test-run",
        tools=FixtureTools(dataset),
        environment={"GEMINI_API_KEY": "synthetic-test-key"},
    )
    serialized = json.dumps(report, ensure_ascii=False)
    summary = cast(dict[str, object], report["summary"])

    assert report["dataset_sha256"] == dataset.sha256
    assert report["scoring_method"] == "deterministic-s2-v2"
    assert summary["passed_count"] == 1
    assert summary["discount_safety_case_count"] == 0
    assert summary["discount_safety_pass_count"] == 0
    assert summary["cached_input_tokens"] == 0
    assert "PRIVATE MODEL TEXT" not in serialized
    assert "case_results" in report


def test_smoke_action_hook_runs_only_after_case_scoring() -> None:
    dataset = load_dataset()
    case = next(case for case in dataset.cases if case.case_id == "enmsa-001-en")
    candidate = candidate_by_id("gemini-3.8-flash-low")
    approval = _approval(candidate.candidate_id)
    observed: list[bool] = []

    def smoke_action(
        _case: EvaluationCase, _result: ModelResult, score: CaseScore
    ) -> tuple[int, bool]:
        observed.append(score.passed)
        return 501, True

    report = run_evaluation(
        dataset,
        candidate,
        FakeClient(),
        (case,),
        reviews=_approved_reviews(dataset),
        mode="smoke",
        approval=approval,
        run_id="test-run",
        tools=FixtureTools(dataset),
        smoke_action=smoke_action,
        environment={"GEMINI_API_KEY": "synthetic-test-key"},
    )

    assert observed == [True]
    assert report["smoke_actions"] == [{"case_id": case.case_id, "activity_created": True}]


def test_review_asset_generation_preserves_existing_ledger(tmp_path: Path) -> None:
    dataset = load_dataset()
    worksheet = tmp_path / "s2-review.md"
    ledger = tmp_path / "s2-reviews.json"

    worksheet_path, ledger_path = write_review_assets(dataset, worksheet, ledger)
    saved_ledger = json.loads(ledger_path.read_text(encoding="utf-8"))
    saved_ledger["owner_note"] = "preserve this review context"
    edited_ledger = json.dumps(saved_ledger, ensure_ascii=False, indent=2) + "\n"
    ledger_path.write_text(edited_ledger, encoding="utf-8")

    assert write_review_assets(dataset, worksheet, ledger) == (worksheet, ledger)
    assert ledger_path.read_text(encoding="utf-8") == edited_ledger
    assert worksheet_path.is_file()
