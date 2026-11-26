"""EVW-1, EVW-5, EVW-6: the live evaluation workflow and its CI entry (#135, ADR-0031).

Hermetic: reads the workflow files as text and runs the report and budget code on synthetic
inputs; no provider, no Langfuse, no GitHub call.
"""

from __future__ import annotations

import io
import json
import os
import re
import urllib.parse
import uuid
from collections.abc import Iterator
from dataclasses import replace
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

import pytest
import sqlalchemy as sa

from engine.config import normalize_database_url
from engine.db import RunRepository, create_engine, upgrade_schema
from scripts import release_gate_ci as ci
from scripts.eval_fingerprint import ROOT
from scripts.poc2_evaluation_runner import EvaluationProfile
from scripts.release_gate_cases import HELDOUT_PATH, CaseSet
from scripts.s2_llm_eval import DATASET_PATH, load_dataset

WORKFLOWS = ROOT / ".github" / "workflows"
TEXT = (WORKFLOWS / "llm-eval.yml").read_text(encoding="utf-8")
EVALUATE = TEXT.split("\n  evaluate:\n", 1)[1].split("\n  report-pr:\n", 1)[0]
REPORT_PR = TEXT.split("\n  report-pr:\n", 1)[1]
URL = os.environ.get("TEST_DATABASE_URL", "")
if not URL and os.environ.get("REQUIRE_DB_TESTS") == "1":
    raise RuntimeError("REQUIRE_DB_TESTS=1 but TEST_DATABASE_URL is not set")

SECRETS = {
    "OPENAI_API_KEY": "sk-proj-" + "7" * 20,
    "LANGFUSE_PUBLIC_KEY": "pk-lf-" + "1" * 12,
    "LANGFUSE_SECRET_KEY": "sk-lf-" + "2" * 12,
}


def step(name: str) -> str:
    return EVALUATE.split(f"name: {name}", 1)[1].split("\n      - ", 1)[0]


# --- EVW-1 ------------------------------------------------------------------------------------


def test_llm_eval_workflow_is_separate_and_approval_gated() -> None:
    triggers = TEXT.split("\non:\n", 1)[1].split("\npermissions:", 1)[0]
    assert "workflow_dispatch:" in triggers and "push:" in triggers
    assert "pull_request" not in triggers and "schedule" not in triggers
    assert re.search(r"^permissions: \{\}$", TEXT, re.M)
    # Only the approval-gated job sees the secrets, and only on main.
    assert "    environment: llm-eval\n" in EVALUATE
    assert "if: github.ref == 'refs/heads/main'" in EVALUATE
    assert "secrets." not in REPORT_PR and "environment:" not in REPORT_PR
    assert set(re.findall(r"secrets\.([A-Z_]+)", TEXT)) == set(SECRETS)
    # Every action is pinned to a full commit SHA.
    uses = re.findall(r"uses: (\S+)", TEXT)
    assert uses and all(re.fullmatch(r"[\w./-]+@[0-9a-f]{40}", u) for u in uses)
    # Never part of make ci or the required CI workflow.
    for other in ("ci.yml", "deploy-aws.yml"):
        body = (WORKFLOWS / other).read_text(encoding="utf-8")
        assert "llm-eval" not in body and "release_gate_ci" not in body
    makefile = (ROOT / "Makefile").read_text(encoding="utf-8")
    assert "release_gate_ci" not in makefile and "llm-eval" not in makefile
    # The report PR carries the traceability lines the CI check needs.
    assert 'echo "Closes #${issue}"' in REPORT_PR
    assert '-m "Refs #${issue}"' in REPORT_PR
    assert 'branch="${issue}-eval-report-${short}"' in REPORT_PR


# --- EVW-5 ------------------------------------------------------------------------------------


def test_eval_workflow_preflight_and_ceiling() -> None:
    # Preflight runs first, without any secret; the paid step adds --execute-live.
    preflight = step("Preflight (no provider call)")
    live = step("Run the evaluation (paid, hard ceiling in code)")
    assert EVALUATE.index("Preflight (no provider call)") < EVALUATE.index("Run the evaluation")
    assert "secrets." not in preflight and "--execute-live" not in preflight
    assert "--execute-live" in live and "scripts.release_gate_ci" in live

    # The ceilings: per set and in total, under the owner's USD 2.00 per evaluation.
    dataset = load_dataset()
    planned = tuple(
        ci.PlannedSet(s, ci.load_inventory(s, dataset), ci.proposal_settings(s)) for s in ci.CI_SETS
    )
    assert ci.plan_budget(planned) == ci.RUN_CEILING_USD <= Decimal("2.00")
    for item in planned:
        assert item.settings.synthetic_only is True
        assert item.settings.review_budget is not None
        assert item.settings.review_budget.limit_usd == item.case_set.profile.budget_limit

    # A ceiling that cannot reserve every planned call fails before any paid call.
    tiny = CaseSet(
        "reviewed",
        EvaluationProfile("tiny", "ci-eval-tiny", "ci-eval-tiny", Decimal("0.10"), ci.ACTOR),
        "reviewed",
    )
    small = (ci.PlannedSet(tiny, planned[0].inventory, ci.proposal_settings(tiny)),)
    with pytest.raises(ValueError, match="cannot reserve"):
        ci.plan_budget(small)
    # Settings that drift from the fixed profile are refused.
    drifted = replace(planned[0], settings=replace(planned[0].settings, output_tokens=100))
    with pytest.raises(ValueError):
        ci.plan_budget((drifted,))


def test_live_mode_refuses_wrong_telemetry() -> None:
    base = {
        "TELEMETRY_ENABLED": "YES",
        "TELEMETRY_ENVIRONMENT": "eval",
        "TELEMETRY_EVAL_TEXT": "YES",
        "LANGFUSE_OTLP_ENDPOINT": "https://us.cloud.langfuse.com/api/public/otel",
        "TELEMETRY_DAILY_LANGFUSE_UNIT_CAP": "2000",
        **SECRETS,
    }
    assert ci.require_eval_telemetry(base).eval_text is True
    for change in (
        {"TELEMETRY_ENVIRONMENT": "aws-mvp", "TELEMETRY_RELAY_KEY": "relay-SYNTHETIC-000000"},
        {"TELEMETRY_ENVIRONMENT": "local"},
        {"TELEMETRY_EVAL_TEXT": "NO"},
        {"LANGFUSE_OTLP_ENDPOINT": "https://cloud.langfuse.com/api/public/otel"},
        {"TELEMETRY_RELAY_KEY": "relay-SYNTHETIC-000000"},
    ):
        with pytest.raises(ValueError):
            ci.require_eval_telemetry({**base, **change})


def test_throwaway_database_guard_refuses_kept_databases() -> None:
    remote = create_engine("postgresql+psycopg://engine:x@db.example.test:5432/engine")
    try:
        with pytest.raises(ValueError, match="EVAL_THROWAWAY_DATABASE"):
            ci.require_throwaway_database(remote, {})
        with pytest.raises(ValueError, match="local"):
            ci.require_throwaway_database(remote, {"EVAL_THROWAWAY_DATABASE": "YES"})
    finally:
        remote.dispose()


@pytest.fixture
def db() -> Iterator[sa.Engine]:
    if not URL:
        pytest.skip("TEST_DATABASE_URL not set")
    engine = create_engine(normalize_database_url(URL))
    upgrade_schema(engine)
    yield engine
    engine.dispose()


def test_throwaway_database_guard_refuses_a_used_database(db: sa.Engine) -> None:
    run, _ = RunRepository(db).create_run(
        trigger_type="synthetic",
        trigger_ref="1:1",
        idempotency_key=f"evw5-guard:{uuid.uuid4()}",
        actor="evw5-test",
    )
    run_id = run.id
    try:
        with pytest.raises(ValueError, match="not empty"):
            ci.require_throwaway_database(db, {"EVAL_THROWAWAY_DATABASE": "YES"})
    finally:
        with db.begin() as conn:
            conn.execute(sa.text("DELETE FROM run_events WHERE run_id=:id"), {"id": run_id})
            conn.execute(sa.text("DELETE FROM runs WHERE id=:id"), {"id": run_id})


# --- EVW-6 ------------------------------------------------------------------------------------


def set_summary(name: str) -> dict[str, Any]:
    return {
        "review_id": f"ci-eval-{name}",
        "cases": 3,
        "budget_ceiling_usd": "1.00",
        "reserved_usd": "0.0138",
        "actual_usd": "0.0004",
        "gates": {"G1": True, "G2": True, "G3": True, "G4_G5": name == "reviewed"},
        "safety_pass": True,
        "quality_pass": name == "reviewed",
        "report": f"{name}-evaluation/README.md",
        "smoke_report": f"{name}-smoke/README.md",
    }


def test_eval_report_has_no_secrets_and_synthetic_only() -> None:
    env = {**SECRETS, "GITHUB_SHA": "not-a-sha " + SECRETS["OPENAI_API_KEY"], "GITHUB_RUN_ID": "1"}
    report = ci.summary_report(
        digest="a" * 64,
        sets={"reviewed": set_summary("reviewed"), "heldout": set_summary("heldout")},
        langfuse={"status": "complete", "expected_generations": 6, "generations": 6},
        env=env,
        completed_at=datetime(2026, 11, 26, tzinfo=UTC),
    )
    text = json.dumps(report) + ci.render_summary(report)
    for value in SECRETS.values():
        assert value not in text
    assert "sk-" not in text and "pk-lf" not in text
    assert report["commit"] is None and report["workflow_run_id"] == "1"
    assert report["safety_pass"] is True and report["quality_pass"] is False
    assert "WARN" in ci.render_summary(report)

    # Only the fixed, checked-in synthetic datasets: there is no option to pass another one.
    assert DATASET_PATH.is_relative_to(ROOT / "evals") and HELDOUT_PATH.is_relative_to(ROOT)
    assert ci.POLICY_SOURCE == ROOT / "policies" / "synthetic.json"
    source = (ROOT / "scripts" / "release_gate_ci.py").read_text(encoding="utf-8")
    options = re.findall(r'add_argument\("(--[\w-]+)"', source)
    assert options == ["--output-dir", "--execute-live"]
    for item in ci.CI_SETS:
        assert ci.proposal_settings(item).synthetic_only is True


def test_langfuse_readback_counts_generations_across_pages() -> None:
    pages = [
        {
            "data": [{"type": "GENERATION", "traceId": "t1", "totalCost": 0.0001}] * 2,
            "meta": {"cursor": "next"},
        },
        {
            "data": [{"type": "GENERATION", "traceId": "t2", "totalCost": 0.0002}],
            "meta": {"cursor": None},
        },
    ]
    seen: list[str] = []

    class Response(io.BytesIO):
        def __enter__(self) -> Response:
            return self

        def __exit__(self, *args: object) -> None:
            self.close()

    def opener(request: Any) -> Response:
        seen.append(request.full_url)
        assert request.get_header("Authorization").startswith("Basic ")
        return Response(json.dumps(pages[len(seen) - 1]).encode())

    result = ci.langfuse_generations(
        "https://us.cloud.langfuse.com",
        SECRETS["LANGFUSE_PUBLIC_KEY"],
        SECRETS["LANGFUSE_SECRET_KEY"],
        datetime(2026, 11, 26, tzinfo=UTC),
        datetime(2026, 11, 26, 1, tzinfo=UTC),
        opener=opener,
    )
    assert result == {"generations": 3, "traces": 2, "total_cost_usd": "0.0004"}
    first = urllib.parse.parse_qs(urllib.parse.urlsplit(seen[0]).query)
    assert first["type"] == ["GENERATION"] and first["environment"] == ["eval"]
    assert first["fromStartTime"] == ["2026-11-26T00:00:00Z"]
    assert "cursor" not in first
    assert urllib.parse.parse_qs(urllib.parse.urlsplit(seen[1]).query)["cursor"] == ["next"]
    assert seen[0].startswith("https://us.cloud.langfuse.com/api/public/v2/observations?")
    assert ci.langfuse_base("http://us.cloud.langfuse.com/x") is None
    assert ci.langfuse_base("https://evil.example.test/api/public/otel") is None


def test_report_name_and_smoke_check() -> None:
    name = ci.report_name(datetime(2026, 11, 26, 9, 5, tzinfo=UTC), "b" * 64)
    assert name == "eval-20261126T0905Z-bbbbbbbbbbbb"
    ci.check_smoke("reviewed", {"contract_pass": True, "error": None, "usage": {"actual_usd": "1"}})
    with pytest.raises(ci.EvaluationStageError, match="smoke-reviewed"):
        ci.check_smoke("reviewed", {"contract_pass": False, "error": "invalid proposal schema"})
