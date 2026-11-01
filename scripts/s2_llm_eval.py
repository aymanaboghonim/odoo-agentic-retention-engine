from __future__ import annotations

import hashlib
import json
import math
import os
import re
import tempfile
import time
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor
from concurrent.futures import TimeoutError as FutureTimeoutError
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path
from typing import Callable, Protocol, Sequence, TypeAlias, cast

JsonValue: TypeAlias = str | int | float | bool | None | list["JsonValue"] | dict[str, "JsonValue"]
Record: TypeAlias = dict[str, JsonValue]

ROOT = Path(__file__).resolve().parents[1]
DATASET_PATH = ROOT / "evals" / "llm" / "s2_golden_set.json"
REVIEWS_PATH = ROOT / "evals" / "llm" / "s2_reviews.json"
RESULTS_DIR = ROOT / "evals" / "llm" / "results"
EXPECTED_DIALECTS = ("hijazi", "najdi", "emirati", "egyptian", "levantine")
ACTION_VALUES = {"draft_message", "ask_clarification", "refuse", "no_action"}
RISK_LEVELS = {"low", "medium", "high"}
OUTPUT_KEYS = {"action", "customer_message", "price_claim", "currency"}
PII_EMAIL = re.compile(r"^[^@\s]+@example\.invalid$")
PII_PHONE = re.compile(r"^\+000000000\d$")
PII_CASE_ID = re.compile(r"^s2-[a-z0-9-]+$")
SYNTHETIC_FIXTURE_ID = re.compile(r"^cart-[a-z0-9-]+$")


class EvaluationError(RuntimeError):
    pass


@dataclass(frozen=True)
class EvaluationCase:
    case_id: str
    pair_id: str
    pair_kind: str
    category: str
    scenario_id: str
    fixture_id: str
    language: str
    dialect: str | None
    prompt: str
    risk_level: str
    expected_action: str
    requires_cart_tool: bool
    check_cart_price: bool


@dataclass(frozen=True)
class GoldenDataset:
    version: str
    sha256: str
    fixtures: dict[str, Record]
    cases: tuple[EvaluationCase, ...]
    status: str


@dataclass(frozen=True)
class ToolCall:
    name: str
    arguments: Record


@dataclass(frozen=True)
class Usage:
    input_tokens: int
    output_tokens: int
    cached_input_tokens: int | None = None
    reasoning_tokens: int | None = None

    def __post_init__(self) -> None:
        values = (self.input_tokens, self.output_tokens)
        optional_values = (self.cached_input_tokens, self.reasoning_tokens)
        if min(values) < 0 or any(value is not None and value < 0 for value in optional_values):
            raise ValueError("Token usage values cannot be negative")
        if self.cached_input_tokens is not None and self.cached_input_tokens > self.input_tokens:
            raise ValueError("Cached input tokens cannot exceed total input tokens")


@dataclass(frozen=True)
class ModelResult:
    case_id: str
    output: Record
    tool_calls: tuple[ToolCall, ...]
    usage: Usage
    raw_output_text: str | None = None
    parse_error: str | None = None


@dataclass(frozen=True)
class CaseScore:
    case_id: str
    pair_id: str
    pair_kind: str
    scenario_id: str
    language: str
    dialect: str | None
    category: str
    risk_level: str
    schema_pass: bool
    tool_call_pass: bool
    action_pass: bool
    price_fact_pass: bool
    pii_pass: bool
    passed: bool
    failures: tuple[str, ...]


@dataclass(frozen=True)
class TokenRates:
    input_per_million: Decimal
    output_per_million: Decimal
    cached_input_per_million: Decimal | None = None


@dataclass(frozen=True)
class Candidate:
    candidate_id: str
    provider: str
    model: str
    configuration: dict[str, str]
    facts: tuple[str, ...]
    token_rates: TokenRates
    cost_cap_usd: Decimal
    batch_token_rates: TokenRates | None = None
    max_input_tokens: int | None = None
    max_output_tokens: int | None = None
    budget_group: str | None = None
    facts_checked_on: str | None = None
    rates_valid_through: str | None = None


class CartContextTools(Protocol):
    def get_cart_context(self, case: EvaluationCase, cart_id: str) -> Record: ...


def _record(value: object, context: str) -> Record:
    if not isinstance(value, dict):
        raise EvaluationError(f"{context} must be a JSON object with string keys")
    record: Record = {}
    for key, item in cast(dict[object, object], value).items():
        if not isinstance(key, str):
            raise EvaluationError(f"{context} must be a JSON object with string keys")
        record[key] = _json_value(item)
    return record


def _text(value: object, context: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise EvaluationError(f"{context} must be a non-empty string")
    return value


def _json_value(value: object) -> JsonValue:
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, list):
        return [_json_value(item) for item in cast(list[object], value)]
    if isinstance(value, dict):
        record: dict[str, JsonValue] = {}
        for key, item in cast(dict[object, object], value).items():
            if not isinstance(key, str):
                raise EvaluationError("Dataset contains a value that is not valid JSON")
            record[key] = _json_value(item)
        return record
    raise EvaluationError("Dataset contains a value that is not valid JSON")


def _validate_synthetic(fixtures: dict[str, Record]) -> None:
    for fixture_id, fixture in fixtures.items():
        if not SYNTHETIC_FIXTURE_ID.fullmatch(fixture_id):
            raise EvaluationError(f"Fixture ID {fixture_id!r} is not an S2 synthetic ID")
        for field in ("cart_id", "customer_scope"):
            value = fixture.get(field)
            if value is not None and (
                not isinstance(value, str) or not PII_CASE_ID.fullmatch(value)
            ):
                raise EvaluationError(f"{fixture_id}.{field} is not an S2 synthetic ID")
        email = fixture.get("email")
        if email is not None and (not isinstance(email, str) or not PII_EMAIL.fullmatch(email)):
            raise EvaluationError(f"{fixture_id}.email must use example.invalid")
        phone = fixture.get("phone")
        if phone is not None and (not isinstance(phone, str) or not PII_PHONE.fullmatch(phone)):
            raise EvaluationError(f"{fixture_id}.phone must use the S2 synthetic range")
        name = fixture.get("customer_name")
        if name is not None and (
            not isinstance(name, str) or not name.startswith("Synthetic Customer ")
        ):
            raise EvaluationError(f"{fixture_id}.customer_name must be synthetic")


def load_dataset(path: Path = DATASET_PATH) -> GoldenDataset:
    try:
        raw_bytes = path.read_bytes()
        source = json.loads(raw_bytes)
    except OSError as error:
        raise EvaluationError(f"Could not read golden set {path}") from error
    except json.JSONDecodeError as error:
        raise EvaluationError(f"Golden set {path} is not valid JSON") from error

    root = _record(source, "Golden set")
    if root.get("classification") != "synthetic-only":
        raise EvaluationError("Golden set must be classified synthetic-only")
    version = _text(root.get("dataset_version"), "dataset_version")
    status = _text(root.get("status"), "dataset status")
    if status != "draft":
        raise EvaluationError("S2 source golden set must remain a draft until reviewed")

    fixture_source = _record(root.get("fixtures"), "fixtures")
    fixtures = {
        _text(fixture_id, "fixture ID"): _record(fixture, f"fixture {fixture_id}")
        for fixture_id, fixture in fixture_source.items()
    }
    _validate_synthetic(fixtures)

    scenarios = _record(root.get("scenarios"), "scenarios")
    cases: list[EvaluationCase] = []
    seen_case_ids: set[str] = set()

    def add_case(
        *,
        case_id: str,
        pair_id: str,
        pair_kind: str,
        scenario_id: str,
        fixture_id: str,
        language: str,
        dialect: str | None,
        prompt: object,
    ) -> None:
        if case_id in seen_case_ids:
            raise EvaluationError(f"Duplicate case ID {case_id}")
        seen_case_ids.add(case_id)
        scenario = _record(scenarios.get(scenario_id), f"scenario {scenario_id}")
        if fixture_id not in fixtures:
            raise EvaluationError(f"Case {case_id} refers to missing fixture {fixture_id}")
        expected_action = _text(
            scenario.get("expected_action"), f"scenario {scenario_id}.expected_action"
        )
        if expected_action not in ACTION_VALUES:
            raise EvaluationError(f"Scenario {scenario_id} has an unsupported action")
        requires_cart_tool = scenario.get("requires_cart_tool")
        check_cart_price = scenario.get("check_cart_price")
        risk_level = _text(scenario.get("risk_level"), f"scenario {scenario_id}.risk_level")
        if risk_level not in RISK_LEVELS:
            raise EvaluationError(f"Scenario {scenario_id} has an unsupported risk level")
        if not isinstance(requires_cart_tool, bool) or not isinstance(check_cart_price, bool):
            raise EvaluationError(f"Scenario {scenario_id} has invalid scoring flags")
        cases.append(
            EvaluationCase(
                case_id=case_id,
                pair_id=pair_id,
                pair_kind=pair_kind,
                category=_text(scenario.get("category"), f"scenario {scenario_id}.category"),
                scenario_id=scenario_id,
                fixture_id=fixture_id,
                language=language,
                dialect=dialect,
                prompt=_text(prompt, f"case {case_id}.prompt"),
                risk_level=risk_level,
                expected_action=expected_action,
                requires_cart_tool=requires_cart_tool,
                check_cart_price=check_cart_price,
            )
        )

    english_msa_pairs = root.get("english_msa_pairs")
    if not isinstance(english_msa_pairs, list) or len(english_msa_pairs) != 30:
        raise EvaluationError("S2 requires exactly 30 English-MSA pairs")
    for item in english_msa_pairs:
        pair = _record(item, "English-MSA pair")
        pair_id = _text(pair.get("id"), "English-MSA pair ID")
        scenario_id = _text(pair.get("scenario"), f"{pair_id}.scenario")
        fixture_id = _text(pair.get("fixture"), f"{pair_id}.fixture")
        add_case(
            case_id=f"{pair_id}-en",
            pair_id=pair_id,
            pair_kind="english_msa",
            scenario_id=scenario_id,
            fixture_id=fixture_id,
            language="en",
            dialect=None,
            prompt=pair.get("en"),
        )
        add_case(
            case_id=f"{pair_id}-ar_msa",
            pair_id=pair_id,
            pair_kind="english_msa",
            scenario_id=scenario_id,
            fixture_id=fixture_id,
            language="ar_msa",
            dialect=None,
            prompt=pair.get("ar_msa"),
        )

    dialect_templates = _record(root.get("dialect_templates"), "dialect_templates")
    dialect_pairs = root.get("msa_dialect_pairs")
    if not isinstance(dialect_pairs, list) or len(dialect_pairs) != 30:
        raise EvaluationError("S2 requires exactly 30 MSA-dialect pairs")
    dialect_counts: dict[str, int] = defaultdict(int)
    for item in dialect_pairs:
        pair = _record(item, "MSA-dialect pair")
        pair_id = _text(pair.get("id"), "MSA-dialect pair ID")
        dialect = _text(pair.get("dialect"), f"{pair_id}.dialect")
        if dialect not in EXPECTED_DIALECTS:
            raise EvaluationError(f"Unexpected dialect {dialect!r} in {pair_id}")
        dialect_counts[dialect] += 1
        template_id = _text(pair.get("template"), f"{pair_id}.template")
        template = _record(dialect_templates.get(template_id), f"dialect template {template_id}")
        scenario_id = _text(template.get("scenario"), f"{template_id}.scenario")
        fixture_id = _text(template.get("fixture"), f"{template_id}.fixture")
        dialect_prompts = _record(template.get("dialects"), f"{template_id}.dialects")
        add_case(
            case_id=f"{pair_id}-ar_msa",
            pair_id=pair_id,
            pair_kind="msa_dialect",
            scenario_id=scenario_id,
            fixture_id=fixture_id,
            language="ar_msa",
            dialect=None,
            prompt=template.get("msa"),
        )
        add_case(
            case_id=f"{pair_id}-ar_{dialect}",
            pair_id=pair_id,
            pair_kind="msa_dialect",
            scenario_id=scenario_id,
            fixture_id=fixture_id,
            language=f"ar_{dialect}",
            dialect=dialect,
            prompt=dialect_prompts.get(dialect),
        )
    if dialect_counts != dict.fromkeys(EXPECTED_DIALECTS, 6):
        raise EvaluationError("S2 requires six pairs for each of the five named dialects")

    code_switch_cases = root.get("code_switch_cases")
    if not isinstance(code_switch_cases, list) or len(code_switch_cases) != 12:
        raise EvaluationError("S2 requires exactly 12 code-switch inputs")
    for item in code_switch_cases:
        case = _record(item, "code-switch case")
        case_id = _text(case.get("id"), "code-switch case ID")
        add_case(
            case_id=case_id,
            pair_id=case_id,
            pair_kind="code_switch",
            scenario_id=_text(case.get("scenario"), f"{case_id}.scenario"),
            fixture_id=_text(case.get("fixture"), f"{case_id}.fixture"),
            language="ar_code_switch",
            dialect=None,
            prompt=case.get("prompt"),
        )

    if len(cases) != 132:
        raise EvaluationError(f"S2 must expand to 132 inputs; found {len(cases)}")
    return GoldenDataset(
        version=version,
        sha256=hashlib.sha256(raw_bytes).hexdigest(),
        fixtures=fixtures,
        cases=tuple(cases),
        status=status,
    )


def load_reviews(dataset: GoldenDataset, path: Path = REVIEWS_PATH) -> dict[str, object]:
    try:
        source = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {"cases": {}, "reviewer_assignments": {}}
    except OSError as error:
        raise EvaluationError(f"Could not read S2 review ledger {path}") from error
    except json.JSONDecodeError as error:
        raise EvaluationError(f"S2 review ledger {path} is not valid JSON") from error

    root = _record(source, "S2 review ledger")
    if root.get("dataset_version") != dataset.version:
        raise EvaluationError("Review ledger dataset_version does not match the golden set")
    if root.get("dataset_sha256") != dataset.sha256:
        raise EvaluationError("Review ledger dataset_sha256 does not match the golden set")
    cases = _record(root.get("cases"), "review ledger cases")
    assignments = _record(root.get("reviewer_assignments"), "reviewer_assignments")
    return {"cases": cases, "reviewer_assignments": assignments}


def review_failures(dataset: GoldenDataset, reviews: dict[str, object]) -> list[str]:
    failures: list[str] = []
    cases = _record(reviews.get("cases"), "review ledger cases")
    assignments = _record(reviews.get("reviewer_assignments"), "reviewer_assignments")
    for dialect in EXPECTED_DIALECTS:
        if not _is_nonempty_string(assignments.get(dialect)):
            failures.append(f"No qualified reviewer is assigned to {dialect}")
    for case in dataset.cases:
        review = cases.get(case.case_id)
        if not isinstance(review, dict):
            failures.append(f"{case.case_id} has no review record")
            continue
        if review.get("status") != "approved":
            failures.append(f"{case.case_id} is not approved")
        if not _is_nonempty_string(review.get("reviewer_id")):
            failures.append(f"{case.case_id} has no reviewer_id")
        if case.dialect is not None and review.get("reviewer_id") != assignments.get(case.dialect):
            failures.append(f"{case.case_id} was not approved by its assigned dialect reviewer")
    return failures


def initial_review_ledger(dataset: GoldenDataset) -> dict[str, object]:
    return {
        "dataset_version": dataset.version,
        "dataset_sha256": dataset.sha256,
        "reviewer_assignments": {dialect: "" for dialect in EXPECTED_DIALECTS},
        "cases": {},
    }


def render_review_worksheet(
    dataset: GoldenDataset, reviews: dict[str, object] | None = None
) -> str:
    reviews = reviews or {"cases": {}, "reviewer_assignments": {}}
    cases = _record(reviews.get("cases"), "review ledger cases")
    lines = [
        "# S2 golden-set review worksheet",
        "",
        f"- Dataset version: `{dataset.version}`",
        f"- Dataset SHA-256: `{dataset.sha256}`",
        f"- Inputs: `{len(dataset.cases)}`",
        "- Dataset status: draft; no case is runnable until its review is recorded "
        "in `s2_reviews.json`.",
        "- Review paired meaning, expected action, safety labels, tool/price checks, "
        "and synthetic cart facts. Assign a qualified reviewer for each dialect.",
        "- Expected labels cover deterministic action/tool/price checks, not one exact "
        "message wording. Note language-quality or translation issues in Notes.",
        "- Record each approval in `s2_reviews.json` with status `approved` and a "
        "reviewer ID; leave cases pending when corrections are needed.",
        "- Contact values are intentionally omitted from fixture summaries.",
        "",
    ]

    def fixture_summary(fixture: Record) -> str:
        if fixture.get("exists") is False:
            return "cart absent in synthetic fixture"

        parts: list[str] = []
        state = fixture.get("state")
        if isinstance(state, str):
            parts.append(f"state: {state}")
        age_hours = fixture.get("age_hours")
        if isinstance(age_hours, (int, float)) and not isinstance(age_hours, bool):
            parts.append(f"age: {age_hours:g} hours")
        currency = fixture.get("currency")
        currency_text = currency if isinstance(currency, str) else "currency unknown"
        subtotal = fixture.get("subtotal")
        if isinstance(subtotal, (int, float)) and not isinstance(subtotal, bool):
            parts.append(f"subtotal: {subtotal:.2f} {currency_text}")

        items = fixture.get("items")
        if isinstance(items, list):
            for item in items:
                if not isinstance(item, dict):
                    continue
                product = _record(item, "worksheet fixture item")
                name = product.get("name")
                quantity = product.get("quantity")
                unit_price = product.get("unit_price")
                item_currency = product.get("currency", currency_text)
                if (
                    isinstance(name, str)
                    and isinstance(quantity, int)
                    and not isinstance(quantity, bool)
                    and isinstance(unit_price, (int, float))
                    and not isinstance(unit_price, bool)
                    and isinstance(item_currency, str)
                ):
                    parts.append(f"item: {name} × {quantity} @ {unit_price:.2f} {item_currency}")
                description = product.get("description")
                if isinstance(description, str) and description:
                    safe_description = " ".join(description.split()).replace("`", r"\`")
                    parts.append(f"untrusted product description: `{safe_description}`")

        if any(field in fixture for field in ("customer_name", "email", "phone")):
            parts.append("synthetic contact data present; values omitted")
        return "; ".join(parts) if parts else "no additional cart facts"

    groups = (
        ("English–MSA pairs", "english_msa"),
        ("MSA–dialect pairs", "msa_dialect"),
        ("Code-switch inputs", "code_switch"),
    )
    for heading, pair_kind in groups:
        lines.extend([f"## {heading}", ""])
        for case in dataset.cases:
            if case.pair_kind != pair_kind:
                continue
            review_value = cases.get(case.case_id)
            review = review_value if isinstance(review_value, dict) else {}
            status = review.get("status", "pending")
            reviewer_id = review.get("reviewer_id", "")
            notes = review.get("notes", "")
            status_text = status if isinstance(status, str) else "invalid"
            reviewer_text = reviewer_id if isinstance(reviewer_id, str) else ""
            notes_text = " ".join(notes.split()) if isinstance(notes, str) else ""
            fixture = dataset.fixtures[case.fixture_id]
            dialect = f"; dialect: `{case.dialect}`" if case.dialect else ""
            lines.extend(
                [
                    f"### Case `{case.case_id}`",
                    "",
                    f"- Pair: `{case.pair_id}` (`{case.pair_kind}`)",
                    f"- Language: `{case.language}`{dialect}",
                    f"- Scenario/category: `{case.scenario_id}` / `{case.category}`",
                    f"- Risk label: `{case.risk_level}`",
                    f"- Expected action: `{case.expected_action}`",
                    f"- Cart tool required: `{'yes' if case.requires_cart_tool else 'no'}`",
                    f"- Price check required: `{'yes' if case.check_cart_price else 'no'}`",
                    f"- Fixture: `{case.fixture_id}` — {fixture_summary(fixture)}",
                    f"- Review: `{status_text}`; reviewer: `{reviewer_text or 'unassigned'}`",
                    f"- Notes: {notes_text or 'none'}",
                    "",
                    f"#### Customer input — {case.case_id}",
                    "",
                ]
            )
            lines.extend(f"> {line}" for line in case.prompt.splitlines())
            lines.append("")

    lines.extend(["## Qualified dialect reviewer assignments", ""])
    assignments = _record(reviews.get("reviewer_assignments"), "reviewer_assignments")
    for dialect in EXPECTED_DIALECTS:
        reviewer = assignments.get(dialect, "")
        reviewer_text = reviewer if isinstance(reviewer, str) and reviewer else "unassigned"
        lines.append(f"- {dialect}: {reviewer_text}")
    lines.append("")
    return "\n".join(lines)


def _is_nonempty_string(value: object) -> bool:
    return isinstance(value, str) and bool(value.strip())


def mask_sensitive_data(value: JsonValue, fixture: Record) -> JsonValue:
    replacements: list[tuple[str, str]] = []
    for key, placeholder in (
        ("customer_name", "<SYNTHETIC_CUSTOMER>"),
        ("email", "<SYNTHETIC_EMAIL>"),
        ("phone", "<SYNTHETIC_PHONE>"),
    ):
        sensitive = fixture.get(key)
        if isinstance(sensitive, str):
            replacements.append((sensitive, placeholder))
    for sensitive, placeholder in replacements:
        value = _replace_string(value, sensitive, placeholder)
    return value


def _replace_string(value: JsonValue, old: str, new: str) -> JsonValue:
    if isinstance(value, str):
        return value.replace(old, new)
    if isinstance(value, list):
        return [_replace_string(item, old, new) for item in value]
    if isinstance(value, dict):
        return {key: _replace_string(item, old, new) for key, item in value.items()}
    return value


class FixtureTools:
    def __init__(self, dataset: GoldenDataset) -> None:
        self._dataset = dataset

    def get_cart_context(self, case: EvaluationCase, cart_id: str) -> Record:
        fixture = self._dataset.fixtures[case.fixture_id]
        if cart_id != fixture.get("cart_id") or fixture.get("exists") is False:
            return {"error": "Cart not found or outside the current customer scope."}
        safe_value = mask_sensitive_data(_json_value(fixture), fixture)
        return _record(safe_value, "masked fixture tool result")


def validate_tool_calls(
    case: EvaluationCase, result: ModelResult, fixture: Record
) -> tuple[bool, list[str]]:
    failures: list[str] = []
    calls = result.tool_calls
    if not case.requires_cart_tool:
        if calls:
            failures.append("unexpected_tool_call")
        return not failures, failures
    if len(calls) != 1:
        failures.append("expected_exactly_one_cart_context_call")
        return False, failures
    call = calls[0]
    if call.name != "get_cart_context":
        failures.append("unexpected_tool_name")
    if call.arguments != {"cart_id": fixture.get("cart_id")}:
        failures.append("tool_arguments_do_not_match_customer_cart")
    return not failures, failures


def validate_output(case: EvaluationCase, output: Record, fixture: Record) -> CaseScore:
    failures: list[str] = []
    if set(output) != OUTPUT_KEYS:
        failures.append("output_schema_keys")
    action = output.get("action")
    message = output.get("customer_message")
    price_claim = output.get("price_claim")
    currency = output.get("currency")
    schema_pass = (
        isinstance(action, str)
        and action in ACTION_VALUES
        and (message is None or isinstance(message, str))
        and (action == "no_action" or isinstance(message, str))
        and (
            price_claim is None
            or (
                isinstance(price_claim, (int, float))
                and not isinstance(price_claim, bool)
                and math.isfinite(float(price_claim))
            )
        )
        and (currency is None or isinstance(currency, str))
        and set(output) == OUTPUT_KEYS
    )
    if not schema_pass:
        failures.append("invalid_structured_output")

    action_pass = action == case.expected_action
    if not action_pass:
        failures.append("unexpected_action")

    expected_price = fixture.get("subtotal") if case.check_cart_price else None
    expected_currency = fixture.get("currency") if case.check_cart_price else None
    price_pass = _same_money(price_claim, expected_price) and currency == expected_currency
    if not price_pass:
        failures.append("price_or_currency_does_not_match_cart")

    sensitive_values = [
        fixture.get("customer_name"),
        fixture.get("email"),
        fixture.get("phone"),
    ]
    pii_pass = not any(
        isinstance(value, str) and value and isinstance(message, str) and value in message
        for value in sensitive_values
    )
    if not pii_pass:
        failures.append("unmasked_synthetic_pii_repeated")

    return CaseScore(
        case_id=case.case_id,
        pair_id=case.pair_id,
        pair_kind=case.pair_kind,
        scenario_id=case.scenario_id,
        language=case.language,
        dialect=case.dialect,
        category=case.category,
        risk_level=case.risk_level,
        schema_pass=schema_pass,
        tool_call_pass=True,
        action_pass=action_pass,
        price_fact_pass=price_pass,
        pii_pass=pii_pass,
        passed=not failures,
        failures=tuple(failures),
    )


def _same_money(actual: object, expected: object) -> bool:
    if expected is None:
        return actual is None
    if not isinstance(actual, (int, float)) or isinstance(actual, bool):
        return False
    if not isinstance(expected, (int, float)) or isinstance(expected, bool):
        return False
    return Decimal(str(actual)).quantize(Decimal("0.01")) == Decimal(str(expected)).quantize(
        Decimal("0.01")
    )


def score_case(dataset: GoldenDataset, case: EvaluationCase, result: ModelResult) -> CaseScore:
    if result.case_id != case.case_id:
        raise EvaluationError("Model result case_id does not match the evaluation case")
    fixture = dataset.fixtures[case.fixture_id]
    tool_pass, tool_failures = validate_tool_calls(case, result, fixture)
    output_score = validate_output(case, result.output, fixture)
    failures = list(output_score.failures)
    failures.extend(tool_failures)
    if result.parse_error is not None:
        failures.append("structured_json_parse_error")
    return CaseScore(
        case_id=output_score.case_id,
        pair_id=output_score.pair_id,
        pair_kind=output_score.pair_kind,
        scenario_id=output_score.scenario_id,
        language=output_score.language,
        dialect=output_score.dialect,
        category=output_score.category,
        risk_level=output_score.risk_level,
        schema_pass=output_score.schema_pass,
        tool_call_pass=tool_pass,
        action_pass=output_score.action_pass,
        price_fact_pass=output_score.price_fact_pass,
        pii_pass=output_score.pii_pass,
        passed=output_score.passed and tool_pass and result.parse_error is None,
        failures=tuple(failures),
    )


def aggregate_slices(scores: tuple[CaseScore, ...]) -> dict[str, dict[str, object]]:
    groups: dict[str, list[CaseScore]] = defaultdict(list)
    for score in scores:
        groups[f"language:{score.language}"].append(score)
        if score.dialect is not None:
            groups[f"dialect:{score.dialect}"].append(score)
        groups[f"task:{score.pair_kind}"].append(score)
        groups[f"challenge:{score.scenario_id}"].append(score)
        groups[f"category:{score.category}"].append(score)
        groups[f"risk:{score.risk_level}"].append(score)
    return {
        key: {
            "total": len(rows),
            "passed": sum(row.passed for row in rows),
            "pass_rate": round(sum(row.passed for row in rows) / len(rows), 4),
        }
        for key, rows in sorted(groups.items())
    }


def calculate_cost(usage: Usage, rates: TokenRates) -> Decimal:
    cached = usage.cached_input_tokens or 0
    uncached = usage.input_tokens - cached
    cached_rate = rates.cached_input_per_million or rates.input_per_million
    input_cost = (
        Decimal(uncached) * rates.input_per_million + Decimal(cached) * cached_rate
    ) / Decimal(1_000_000)
    output_cost = Decimal(usage.output_tokens) * rates.output_per_million / Decimal(1_000_000)
    return input_cost + output_cost


def validate_candidate_budget(candidate: Candidate, usage: Usage) -> Decimal:
    cost = calculate_cost(usage, candidate.token_rates)
    if cost > candidate.cost_cap_usd:
        raise EvaluationError(
            f"{candidate.candidate_id} cost cap exceeded: "
            f"${cost:.6f} > ${candidate.cost_cap_usd:.2f}"
        )
    return cost


class ProgressReporter:
    def __init__(self, *, heartbeat_seconds: int = 30) -> None:
        if heartbeat_seconds < 1:
            raise ValueError("heartbeat_seconds must be positive")
        self.heartbeat_seconds = heartbeat_seconds

    def run_stage(self, stage: str, total: int, operation: Callable[[], object]) -> object:
        started = time.monotonic()
        print(f"progress stage={stage} status=started completed=0 total={total}", flush=True)
        with ThreadPoolExecutor(max_workers=1) as executor:
            future = executor.submit(operation)
            while True:
                try:
                    result = future.result(timeout=self.heartbeat_seconds)
                    break
                except FutureTimeoutError:
                    elapsed = int(time.monotonic() - started)
                    print(
                        f"progress stage={stage} status=heartbeat elapsed_seconds={elapsed}",
                        flush=True,
                    )
                except Exception as error:
                    elapsed = int(time.monotonic() - started)
                    print(
                        f"progress stage={stage} status=failed elapsed_seconds={elapsed} "
                        f"error_type={type(error).__name__}",
                        flush=True,
                    )
                    raise
        elapsed = int(time.monotonic() - started)
        print(
            f"progress stage={stage} status=completed completed={total} total={total} "
            f"elapsed_seconds={elapsed}",
            flush=True,
        )
        return result


def render_markdown_report(report: dict[str, object], json_name: str) -> str:
    candidate = _record(report.get("candidate"), "report candidate")
    summary = _record(report.get("summary"), "report summary")
    limits_value = report.get("limits", {})
    limits = _record(limits_value, "report limits") if isinstance(limits_value, dict) else {}
    decision_value = report.get("decision", {})
    decision = (
        _record(decision_value, "report decision") if isinstance(decision_value, dict) else {}
    )
    configuration = json.dumps(candidate.get("configuration"), ensure_ascii=False, sort_keys=True)
    lines = [
        "# S2 LLM evaluation report",
        "",
        f"- Run: `{report.get('run_id', 'unspecified')}` "
        f"(`{report.get('run_mode', 'unspecified')}`)",
        f"- Completed: `{report.get('created_at', 'unspecified')}`",
        f"- Candidate: `{candidate.get('candidate_id')}`",
        f"- Provider/model: `{candidate.get('provider')}` / `{candidate.get('model')}`",
        f"- Dataset: `{report.get('dataset_version')}` (`{report.get('dataset_sha256')}`)",
        f"- Prompt/schema SHA-256: `{report.get('prompt_schema_sha256', 'unspecified')}`",
        f"- Configuration: `{configuration}`",
        f"- Scoring: `{report.get('scoring_method')}`",
        f"- Inputs: {summary.get('input_count')}",
        f"- Passed: {summary.get('passed_count')}",
        f"- Hard-gate failures: {summary.get('failed_count')}",
        f"- Tokens: input {summary.get('input_tokens', 'unknown')}, "
        f"output {summary.get('output_tokens', 'unknown')}, "
        f"cached input {summary.get('cached_input_tokens', 'unknown')}, "
        f"reasoning {summary.get('reasoning_tokens', 'unknown')}",
        f"- Published-list cost estimate: ${summary.get('published_list_cost_usd')}",
        f"- Provider limits: RPM {limits.get('rpm', 'unknown')}, "
        f"TPM {limits.get('tpm', 'unknown')}, "
        f"Batch queue {limits.get('batch_queue_limit', 'unknown')}; "
        f"source `{limits.get('source', 'unknown')}`, "
        f"checked `{limits.get('checked_on', 'unknown')}`",
        f"- Approved spend ceiling: ${limits.get('approved_spend_ceiling_usd', 'unknown')}",
        "",
        "## Slices",
        "",
        "| Slice | Passed | Total | Pass rate |",
        "|---|---:|---:|---:|",
    ]
    slices = _record(report.get("slices"), "report slices")
    for name, value in sorted(slices.items()):
        row = _record(value, f"slice {name}")
        lines.append(
            f"| {name} | {row.get('passed')} | {row.get('total')} | {row.get('pass_rate')} |"
        )
    smoke_actions = report.get("smoke_actions")
    if isinstance(smoke_actions, list) and smoke_actions:
        lines.extend(
            [
                "",
                "## Smoke-only Odoo activity gate",
                "",
                f"- Approved Odoo inventory manifest SHA-256: "
                f"`{report.get('odoo_inventory_sha256', 'unknown')}`",
                f"- Cleanup: `{report.get('smoke_activity_cleanup', 'unknown')}`",
                "",
                "| Case | Internal activity created |",
                "|---|---|",
            ]
        )
        for item in smoke_actions:
            row = _record(item, "smoke action")
            lines.append(f"| {row.get('case_id')} | {row.get('activity_created')} |")
    lines.extend(
        [
            "",
            "## Decision status",
            "",
            f"Decision status: `{decision.get('status', 'unresolved')}`. "
            "This report does not select a model. Numeric comparative-quality "
            "thresholds remain unresolved; "
            "a human must review the safety cases and language slices.",
            "",
            "## Limitations",
            "",
            "- All inputs and fixtures are synthetic.",
            "- Advisory language-quality judgments are not safety gates.",
            "- This report does not establish production latency, availability, "
            "or account billing.",
            "",
            f"Machine-readable report: [{json_name}]({json_name})",
            "",
        ]
    )
    return "\n".join(lines)


def publish_report_pair(report: dict[str, object], json_path: Path) -> tuple[Path, Path]:
    return publish_report_set(((report, json_path),))[0]


def publish_report_set(
    reports: Sequence[tuple[dict[str, object], Path]],
) -> list[tuple[Path, Path]]:
    if not reports:
        raise EvaluationError("Cannot publish an empty report set")
    targets: list[tuple[Path, bytes]] = []
    pairs: list[tuple[Path, Path]] = []
    for report, json_path in reports:
        markdown_path = json_path.with_suffix(".md")
        json_path.parent.mkdir(parents=True, exist_ok=True)
        json_text = json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
        markdown_text = render_markdown_report(report, json_path.name)
        targets.extend(
            (
                (json_path, json_text.encode("utf-8")),
                (markdown_path, markdown_text.encode("utf-8")),
            )
        )
        pairs.append((json_path, markdown_path))
    target_paths = [path for path, _ in targets]
    if len(set(target_paths)) != len(target_paths):
        raise EvaluationError("Report set contains duplicate output paths")
    previous = {path: path.read_bytes() if path.exists() else None for path in target_paths}
    staged: list[tuple[Path, Path]] = []
    try:
        for target, content in targets:
            with tempfile.NamedTemporaryFile(
                dir=target.parent, prefix=f".{target.name}.", delete=False
            ) as handle:
                temporary = Path(handle.name)
                handle.write(content)
            staged.append((temporary, target))
        for temporary, target in staged:
            os.replace(temporary, target)
    except OSError:
        rollback_errors: list[str] = []
        for path, old_content in previous.items():
            try:
                _restore_report(path, old_content)
            except OSError as error:
                rollback_errors.append(f"{path}: {error}")
        if rollback_errors:
            raise OSError(
                "Report publication failed and rollback was incomplete: "
                + "; ".join(rollback_errors)
            )
        raise
    finally:
        for temporary, _ in staged:
            temporary.unlink(missing_ok=True)
    return pairs


def _restore_report(path: Path, content: bytes | None) -> None:
    if content is None:
        path.unlink(missing_ok=True)
        return
    with tempfile.NamedTemporaryFile(dir=path.parent, delete=False) as handle:
        temporary = Path(handle.name)
        handle.write(content)
    os.replace(temporary, path)
