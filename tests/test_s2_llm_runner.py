import json
import threading
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
    TokenRates,
    ToolCall,
    Usage,
    calculate_cost,
    initial_review_ledger,
    load_dataset,
)
from scripts.s2_llm_runner import (
    FULL_CONCURRENCY,
    SUPPORTED_CANDIDATES,
    CostCapExceeded,
    CostGuard,
    LiveApproval,
    ProviderErrors,
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
        rpm=10 if mode == "smoke" else 500,
        tpm=20_000 if mode == "smoke" else 1_000_000,
        limits_source="provider dashboard checked for test",
        limits_checked_on="2026-11-01",
        max_input_tokens_per_request=max_input_tokens,
        max_output_tokens_per_request=max_output_tokens,
        odoo_inventory_approved=(mode == "smoke"),
        smoke_report_sha256=("a" * 64) if mode == "full" else None,
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


def test_preflight_reserves_output_cap_including_thinking_without_cache_savings() -> None:
    dataset = load_dataset()
    candidate = candidate_by_id("gemini-3.8-flash-low")
    cases = (dataset.cases[0],)
    approval = _approval(candidate.candidate_id, max_input_tokens=4096, max_output_tokens=8192)

    reserved = reserved_cost_usd(candidate, cases, approval, mode="smoke")
    expected = (
        Decimal(2 * 4096) * Decimal("0.75") + Decimal(2 * 8192) * Decimal("3.75")
    ) / Decimal(1_000_000)

    assert reserved == expected


def test_full_reservation_covers_only_the_parallel_in_flight_window() -> None:
    dataset = load_dataset()
    candidate = candidate_by_id("gemini-3.5-flash-lite-minimal")
    approval = _approval(
        candidate.candidate_id,
        mode="full",
        spend_ceiling_usd=Decimal("2.428160"),
        max_input_tokens=4000,
        max_output_tokens=8192,
    )

    reserved = reserved_cost_usd(candidate, dataset.cases, approval, mode="full")
    window = 2 * FULL_CONCURRENCY
    expected = calculate_cost(
        Usage(window * 4000, window * 8192),
        TokenRates(
            candidate.token_rates.input_per_million, candidate.token_rates.output_per_million
        ),
    )

    assert reserved == expected
    assert reserved <= approval.spend_ceiling_usd <= candidate.cost_cap_usd


def test_candidate_set_preflight_treats_gpt_candidates_as_sequential_shared_budget() -> None:
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
            mode="full",
            spend_ceiling_usd=Decimal("2"),
            max_input_tokens=4000,
            max_output_tokens=8192,
        )
        for candidate in candidates
    }

    failures = candidate_set_preflight_failures(
        dataset,
        reviews,
        candidates,
        cases,
        approvals,
        mode="full",
        environment={"OPENAI_API_KEY": "synthetic-test-key"},
        as_of=date(2026, 11, 1),
    )

    assert not any("Maximum sequential reservation" in failure for failure in failures)


def test_full_preflight_rejects_expired_gemini_rates() -> None:
    dataset = load_dataset()
    candidate = candidate_by_id("gemini-3.8-flash-low")
    cases = (dataset.cases[0],)
    approval = _approval(candidate.candidate_id, mode="full")

    failures = live_preflight_failures(
        dataset,
        _approved_reviews(dataset),
        candidate,
        cases,
        approval,
        mode="full",
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
    cost_basis = cast(dict[str, object], report["cost_basis"])

    assert report["report_version"] == "s2-report-v4"
    assert report["dataset_sha256"] == dataset.sha256
    assert report["scoring_method"] == "deterministic-s2-v3"
    assert report["prompt_variant"] == "baseline"
    assert cost_basis["output_token_scope"] == "billable total including thought/reasoning tokens"
    assert summary["passed_count"] == 1
    assert summary["discount_safety_case_count"] == 1
    assert summary["discount_safety_pass_count"] == 1
    assert summary["cached_input_tokens"] == 0
    assert summary["output_token_scope"] == "billable total including thought/reasoning tokens"
    assert "PRIVATE MODEL TEXT" not in serialized
    assert any(
        item.startswith("Model response text is not retained")
        for item in cast(list[str], report["limitations"])
    )
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


def _full_run(
    client: object, cases: tuple[EvaluationCase, ...], **overrides: object
) -> dict[str, object]:
    dataset = load_dataset()
    candidate = candidate_by_id("gemini-3.8-flash-low")
    approval = _approval(
        candidate.candidate_id,
        mode="full",
        max_input_tokens=1000,
        max_output_tokens=1000,
    )
    return run_evaluation(
        dataset,
        candidate,
        cast(FakeClient, client),
        cases,
        reviews=_approved_reviews(dataset),
        mode="full",
        approval=approval,
        run_id="test-full",
        tools=FixtureTools(dataset),
        environment={"GEMINI_API_KEY": "synthetic-test-key"},
        sleep=lambda _: None,
        **cast(dict[str, object], overrides),  # type: ignore[arg-type]
    )


def test_full_run_is_parallel_and_keeps_case_order() -> None:
    dataset = load_dataset()
    cases = dataset.cases[:40]
    lock = threading.Lock()
    state = {"active": 0, "peak": 0}

    class Slow(FakeClient):
        def run_case(
            self, case: EvaluationCase, dataset: GoldenDataset, tools: CartContextTools
        ) -> ModelResult:
            with lock:
                state["active"] += 1
                state["peak"] = max(state["peak"], state["active"])
            threading.Event().wait(0.01)
            with lock:
                state["active"] -= 1
            return super().run_case(case, dataset, tools)

    report = _full_run(Slow(), cases)
    ids = [row["case_id"] for row in cast(list[dict[str, object]], report["case_results"])]

    assert ids == [case.case_id for case in cases]
    assert 1 < state["peak"] <= FULL_CONCURRENCY


def test_full_run_retries_rate_limits_then_succeeds() -> None:
    dataset = load_dataset()
    case = dataset.cases[0]
    attempts = {"count": 0}

    class RateLimited(FakeClient):
        def run_case(
            self, case: EvaluationCase, dataset: GoldenDataset, tools: CartContextTools
        ) -> ModelResult:
            attempts["count"] += 1
            if attempts["count"] < 3:
                error = RuntimeError("rate limited")
                error.status_code = 429  # type: ignore[attr-defined]
                raise error
            return super().run_case(case, dataset, tools)

    report = _full_run(RateLimited(), (case,))

    assert attempts["count"] == 3
    assert cast(dict[str, object], report["summary"])["input_count"] == 1


def test_full_run_provider_error_is_explicit_and_publishes_no_report() -> None:
    case = load_dataset().cases[0]

    class Broken(FakeClient):
        def run_case(
            self, case: EvaluationCase, dataset: GoldenDataset, tools: CartContextTools
        ) -> ModelResult:
            raise RuntimeError("boom")

    try:
        _full_run(Broken(), (case,))
    except ProviderErrors as error:
        assert "incomplete" in str(error)
    else:
        raise AssertionError("provider error must fail the run")


def test_cost_guard_refuses_work_that_could_exceed_the_cap() -> None:
    rates = TokenRates(Decimal(1), Decimal(1))
    guard = CostGuard(Decimal("0.003"), Decimal("0.002"), rates)
    guard.reserve()
    try:
        guard.reserve()
    except CostCapExceeded:
        pass
    else:
        raise AssertionError("second in-flight reservation must exceed the cap")
    guard.settle(Usage(1000, 0))
    guard.reserve()
    assert guard.spent == Decimal("0.001")
