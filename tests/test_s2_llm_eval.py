import json
import time
from collections import Counter
from decimal import Decimal
from pathlib import Path

import pytest

import scripts.s2_llm_eval as evaluation
from scripts.s2_llm_eval import (
    EvaluationError,
    FixtureTools,
    ModelResult,
    ProgressReporter,
    TokenRates,
    ToolCall,
    Usage,
    aggregate_slices,
    calculate_cost,
    initial_review_ledger,
    load_dataset,
    render_markdown_report,
    render_review_worksheet,
    review_failures,
    score_case,
)


def _result(dataset, case) -> ModelResult:
    fixture = dataset.fixtures[case.fixture_id]
    output = {
        "action": case.expected_action,
        "customer_message": "A short synthetic response.",
        "price_claim": fixture.get("subtotal") if case.check_cart_price else None,
        "currency": fixture.get("currency") if case.check_cart_price else None,
    }
    calls = (
        (ToolCall("get_cart_context", {"cart_id": fixture["cart_id"]}),)
        if case.requires_cart_tool
        else ()
    )
    return ModelResult(case.case_id, output, calls, Usage(120, 35))


def test_s2_dataset_has_exact_pair_counts_and_variety_slices() -> None:
    dataset = load_dataset()
    pair_counts = Counter(case.pair_kind for case in dataset.cases)
    language_counts = Counter(case.language for case in dataset.cases)
    dialect_counts = Counter(case.dialect for case in dataset.cases if case.dialect)

    assert dataset.version == "s2-golden-v1"
    assert pair_counts == {"english_msa": 60, "msa_dialect": 60, "code_switch": 12}
    assert len({case.pair_id for case in dataset.cases}) == 72
    assert len(dataset.cases) == 132
    assert language_counts["en"] == 30
    assert language_counts["ar_msa"] == 60
    assert language_counts["ar_code_switch"] == 12
    assert dialect_counts == dict.fromkeys(evaluation.EXPECTED_DIALECTS, 6)
    assert len(dataset.sha256) == 64


def test_dataset_cannot_be_loaded_as_runnable_before_review() -> None:
    dataset = load_dataset()
    failures = review_failures(dataset, initial_review_ledger(dataset))

    assert len(failures) >= len(dataset.cases)
    assert any("hijazi" in failure for failure in failures)
    assert any("no review record" in failure for failure in failures)


def test_dataset_is_synthetic_and_worksheet_is_reproducible() -> None:
    dataset = load_dataset()
    ledger = initial_review_ledger(dataset)
    worksheet = render_review_worksheet(dataset, ledger)

    assert all(fixture_id.startswith("cart-") for fixture_id in dataset.fixtures)
    assert "example.invalid" in json.dumps(dataset.fixtures)
    assert "S3" not in worksheet
    assert dataset.sha256 in worksheet
    assert "pending" in worksheet
    assert "`codeswitch-012`" in worksheet


def test_fixture_tool_masks_pii_and_refuses_out_of_scope_cart() -> None:
    dataset = load_dataset()
    pii_case = next(case for case in dataset.cases if case.fixture_id == "cart-pii-006")
    fixture = dataset.fixtures[pii_case.fixture_id]
    cart_id = fixture.get("cart_id")
    customer_name = fixture.get("customer_name")
    email = fixture.get("email")
    phone = fixture.get("phone")
    assert isinstance(cart_id, str)
    assert isinstance(customer_name, str)
    assert isinstance(email, str)
    assert isinstance(phone, str)

    result = FixtureTools(dataset).get_cart_context(pii_case, cart_id)
    serialized = json.dumps(result, ensure_ascii=False)

    assert customer_name not in serialized
    assert email not in serialized
    assert phone not in serialized
    assert "<SYNTHETIC_CUSTOMER>" in serialized
    assert "<SYNTHETIC_EMAIL>" in serialized
    assert "<SYNTHETIC_PHONE>" in serialized
    assert FixtureTools(dataset).get_cart_context(pii_case, "s2-cart-999") == {
        "error": "Cart not found or outside the current customer scope."
    }


def test_exact_fixture_facts_tool_calls_and_action_are_scored_deterministically() -> None:
    dataset = load_dataset()
    case = next(case for case in dataset.cases if case.case_id == "enmsa-002-en")

    score = score_case(dataset, case, _result(dataset, case))

    assert score.passed
    assert score.tool_call_pass
    assert score.price_fact_pass
    assert score.action_pass


def test_wrong_price_or_unexpected_discount_field_fails_the_hard_schema_gate() -> None:
    dataset = load_dataset()
    case = next(case for case in dataset.cases if case.case_id == "enmsa-002-en")
    result = _result(dataset, case)
    wrong_price = ModelResult(
        result.case_id,
        {**result.output, "price_claim": 100.0},
        result.tool_calls,
        result.usage,
    )
    extra_discount = ModelResult(
        result.case_id,
        {**result.output, "discount_percent": 5},
        result.tool_calls,
        result.usage,
    )

    assert not score_case(dataset, case, wrong_price).passed
    assert (
        "price_or_currency_does_not_match_cart" in score_case(dataset, case, wrong_price).failures
    )
    assert not score_case(dataset, case, extra_discount).schema_pass
    assert "output_schema_keys" in score_case(dataset, case, extra_discount).failures


def test_cross_customer_refusal_must_not_call_cart_tool() -> None:
    dataset = load_dataset()
    case = next(case for case in dataset.cases if case.scenario_id == "cross_customer")
    fixture = dataset.fixtures[case.fixture_id]
    result = _result(dataset, case)
    assert not case.requires_cart_tool

    unexpected_call = ModelResult(
        result.case_id,
        result.output,
        (ToolCall("get_cart_context", {"cart_id": fixture["cart_id"]}),),
        result.usage,
    )

    score = score_case(dataset, case, unexpected_call)

    assert not score.passed
    assert "unexpected_tool_call" in score.failures


def test_structured_output_parse_error_is_a_hard_failure() -> None:
    dataset = load_dataset()
    case = next(case for case in dataset.cases if case.case_id == "enmsa-002-en")
    valid = _result(dataset, case)
    malformed = ModelResult(
        valid.case_id,
        valid.output,
        valid.tool_calls,
        valid.usage,
        raw_output_text=valid.raw_output_text,
        parse_error="invalid JSON",
    )

    score = score_case(dataset, case, malformed)

    assert not score.passed
    assert "structured_json_parse_error" in score.failures


def test_slice_report_keeps_language_and_category_distinct() -> None:
    dataset = load_dataset()
    scores = tuple(score_case(dataset, case, _result(dataset, case)) for case in dataset.cases)
    slices = aggregate_slices(scores)

    def total(slice_name: str) -> int:
        value = slices[slice_name]["total"]
        assert isinstance(value, int)
        return value

    assert total("language:en") == 30
    assert total("language:ar_msa") == 60
    assert total("language:ar_hijazi") == 6
    assert total("dialect:hijazi") == 6
    assert total("task:english_msa") == 60
    assert total("challenge:discount_refusal") > 0
    assert total("category:prompt_injection") > 0
    assert total("risk:high") > 0


def test_cost_calculation_uses_usage_and_verified_rate_inputs() -> None:
    usage = Usage(input_tokens=1_000_000, output_tokens=100_000, cached_input_tokens=200_000)
    rates = TokenRates(
        input_per_million=Decimal("0.50"),
        output_per_million=Decimal("2.00"),
        cached_input_per_million=Decimal("0.10"),
    )

    assert calculate_cost(usage, rates) == Decimal("0.62")


def test_report_summary_links_machine_readable_report() -> None:
    report: dict[str, object] = {
        "candidate": {
            "candidate_id": "test-candidate",
            "provider": "fake",
            "model": "fake-model",
            "configuration": {"thinking": "low"},
        },
        "dataset_version": "s2-golden-v1",
        "dataset_sha256": "abc123",
        "scoring_method": "deterministic-v1",
        "summary": {
            "input_count": 1,
            "passed_count": 1,
            "failed_count": 0,
            "published_list_cost_usd": "0.001",
        },
        "slices": {"language:en": {"passed": 1, "total": 1, "pass_rate": 1.0}},
        "smoke_actions": [{"case_id": "s2-test-case", "activity_created": True}],
        "odoo_inventory_sha256": "inventory-hash",
        "smoke_activity_cleanup": "completed",
    }

    markdown = render_markdown_report(report, "s2-test.json")

    assert "does not select a model" in markdown
    assert "[s2-test.json](s2-test.json)" in markdown
    assert "language:en" in markdown
    assert "Smoke-only Odoo activity gate" in markdown
    assert "`inventory-hash`" in markdown


def test_failed_report_pair_publish_preserves_previous_files(tmp_path, monkeypatch) -> None:
    json_path = tmp_path / "report.json"
    markdown_path = tmp_path / "report.md"
    json_path.write_text("old json", encoding="utf-8")
    markdown_path.write_text("old markdown", encoding="utf-8")
    report: dict[str, object] = {
        "candidate": {
            "candidate_id": "test-candidate",
            "provider": "fake",
            "model": "fake-model",
            "configuration": {},
        },
        "dataset_version": "s2-golden-v1",
        "dataset_sha256": "abc123",
        "scoring_method": "deterministic-v1",
        "summary": {
            "input_count": 0,
            "passed_count": 0,
            "failed_count": 0,
            "published_list_cost_usd": "0",
        },
        "slices": {},
    }
    original_replace = evaluation.os.replace
    replace_count = 0

    def fail_markdown_replace(source, target):
        nonlocal replace_count
        replace_count += 1
        if replace_count == 2:
            raise OSError("synthetic publish failure")
        original_replace(source, target)

    monkeypatch.setattr(evaluation.os, "replace", fail_markdown_replace)
    with pytest.raises(OSError, match="synthetic publish failure"):
        evaluation.publish_report_pair(report, json_path)

    assert json_path.read_text(encoding="utf-8") == "old json"
    assert markdown_path.read_text(encoding="utf-8") == "old markdown"


def test_failed_report_set_publish_rolls_back_every_candidate(tmp_path, monkeypatch) -> None:
    report_paths = (tmp_path / "one.json", tmp_path / "two.json")
    for path in report_paths:
        path.write_text(f"old {path.stem} json", encoding="utf-8")
        path.with_suffix(".md").write_text(f"old {path.stem} markdown", encoding="utf-8")
    report: dict[str, object] = {
        "candidate": {
            "candidate_id": "test-candidate",
            "provider": "fake",
            "model": "fake-model",
            "configuration": {},
        },
        "dataset_version": "s2-golden-v1",
        "dataset_sha256": "abc123",
        "scoring_method": "deterministic-v1",
        "summary": {
            "input_count": 0,
            "passed_count": 0,
            "failed_count": 0,
            "published_list_cost_usd": "0",
        },
        "slices": {},
    }
    original_replace = evaluation.os.replace
    failed = False

    def fail_last_replace(source, target):
        nonlocal failed
        if Path(target) == report_paths[1].with_suffix(".md") and not failed:
            failed = True
            raise OSError("synthetic final-pair failure")
        original_replace(source, target)

    monkeypatch.setattr(evaluation.os, "replace", fail_last_replace)
    with pytest.raises(OSError, match="synthetic final-pair failure"):
        evaluation.publish_report_set(((report, report_paths[0]), (report, report_paths[1])))

    for path in report_paths:
        assert path.read_text(encoding="utf-8") == f"old {path.stem} json"
        assert path.with_suffix(".md").read_text(encoding="utf-8") == (f"old {path.stem} markdown")


def test_progress_report_emits_heartbeat_for_long_stage(capsys) -> None:
    reporter = ProgressReporter(heartbeat_seconds=1)

    reporter.run_stage("synthetic_wait", 1, lambda: time.sleep(1.05))

    output = capsys.readouterr().out
    assert "status=started" in output
    assert "status=heartbeat" in output
    assert "status=completed" in output


def test_review_ledger_must_match_exact_dataset_hash(tmp_path) -> None:
    dataset = load_dataset()
    ledger = initial_review_ledger(dataset)
    ledger["dataset_sha256"] = "0" * 64
    path = tmp_path / "reviews.json"
    path.write_text(json.dumps(ledger), encoding="utf-8")

    with pytest.raises(EvaluationError, match="dataset_sha256"):
        evaluation.load_reviews(dataset, path)
