"""Live release-gate evaluation in GitHub Actions (#135, ADR-0031, spec EVW-1..EVW-7).

Runs only in the approval-gated `llm-eval` workflow, on a throwaway PostgreSQL service. It runs
the proposal step on fixture carts only: no Odoo, no execution, no channel, no customer. Per case
set (tuned, held-out) it runs a 1-case smoke checked by machine, then the full set. The owner's
approval of the workflow run replaces the smoke digest review that the local #87 script keeps
(owner decision 2026-11-26).

Preflight is read-only apart from the schema migration of the throwaway database and never calls
the provider. The report (one directory: summary `report.json` + `README.md`, and per set the
#87 smoke and evaluation reports) is written to a temporary directory and published only after
the whole run succeeds (run governance).
"""

from __future__ import annotations

import argparse
import base64
import json
import os
import re
import shutil
import tempfile
import time
import urllib.parse
import urllib.request
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any

import sqlalchemy as sa
from sqlalchemy import Engine
from sqlalchemy.engine import make_url

from engine import telemetry
from engine.config import EngineConfig
from engine.db import create_engine, upgrade_schema
from engine.full_stack_proposal import progress
from engine.gate_ledger import InMemoryGateLedger
from engine.policy import Gate, GateConfig
from engine.proposal import ProposalSettings
from engine.proposal_provider import MODEL, VERSION, OpenAIClient
from engine.retrieval import (
    MODEL_FILES,
    MODEL_ID,
    MODEL_REPOSITORY,
    MODEL_REVISION,
    LocalE5Embedder,
    PostgresRetriever,
    ingest,
)
from scripts.eval_fingerprint import (
    INPUT_TOKEN_LIMIT,
    OUTPUT_TOKEN_LIMIT,
    ROOT,
    fingerprint,
    fingerprint_inputs,
    unclassified,
)
from scripts.poc2_evaluation_live import contract_digest
from scripts.poc2_evaluation_runner import (
    EvaluationProfile,
    evaluate_case,
    validate_settings,
    validate_unattempted,
)
from scripts.release_gate_cases import (
    CaseInventory,
    CaseSet,
    load_heldout_cases,
    release_fixture_cart,
    select_release_cases,
)
from scripts.release_gate_eval import RetryOnTimeout, budget_totals, validate_release_gate_config
from scripts.release_gate_gates import evaluate_gates, publish_release_report
from scripts.s2_llm_eval import GoldenDataset, load_dataset, load_reviews

ACTOR = "release-gate-ci"
# Per evaluation the two review ceilings stay under the owner's USD 2.00 (#87, Q17). One worst-
# case call reserves about USD 0.0046, so each ceiling covers its set several times over; the
# #87 runs spent USD 0.0060 to 0.0082 per set.
REVIEWED_CI = CaseSet(
    "reviewed",
    EvaluationProfile(
        "issue 135 tuned", "ci-eval-reviewed", "ci-eval-reviewed", Decimal("1.00"), ACTOR
    ),
    "reviewed",
)
HELDOUT_CI = CaseSet(
    "heldout",
    EvaluationProfile(
        "issue 135 held-out", "ci-eval-heldout", "ci-eval-heldout", Decimal("0.90"), ACTOR
    ),
    "heldout",
)
CI_SETS = (REVIEWED_CI, HELDOUT_CI)
RUN_CEILING_USD = Decimal("1.90")
PER_RUN_BUDGET_USD = Decimal("0.05")
# The fixed synthetic gate configuration of #87 (validate_release_gate_config).
GATE_ENV = {
    "GATE_TIMEZONE": "Asia/Riyadh",
    "GATE_CURRENCY": "USD",
    "GATE_DAILY_BUDGET": "100000",
    "GATE_CATEGORY_MIN_MARGINS": '{"Furniture / Office":"0.2"}',
}
POLICY_SOURCE = ROOT / "policies" / "synthetic.json"
LANGFUSE_HOSTS = frozenset({"us.cloud.langfuse.com"})  # US region, owner decision 2026-11-19
READBACK_TIMEOUT_SECONDS = 600
READBACK_INTERVAL_SECONDS = 30
_SHA = re.compile(r"[0-9a-f]{40}")
_RUN_ID = re.compile(r"[0-9]{1,20}")


class EvaluationStageError(RuntimeError):
    """A named stage failed; no report is published."""


@dataclass(frozen=True)
class PlannedSet:
    case_set: CaseSet
    inventory: CaseInventory
    settings: ProposalSettings


def proposal_settings(case_set: CaseSet) -> ProposalSettings:
    """Fixed in code, so the settings are part of the fingerprint (this file is covered)."""
    profile = case_set.profile
    return ProposalSettings.from_env(
        {
            "PROPOSAL_SYNTHETIC_ONLY": "YES",
            "PROPOSAL_BUDGET_USD": str(PER_RUN_BUDGET_USD),
            "PROPOSAL_INPUT_TOKEN_LIMIT": str(INPUT_TOKEN_LIMIT),
            "PROPOSAL_OUTPUT_TOKEN_LIMIT": str(OUTPUT_TOKEN_LIMIT),
            "PROPOSAL_REVIEW_ID": profile.review_id,
            "PROPOSAL_REVIEW_BUDGET_USD": str(profile.budget_limit),
        }
    )


def load_inventory(case_set: CaseSet, dataset: GoldenDataset) -> CaseInventory:
    """Only the fixed, checked-in synthetic datasets; there is no option to pass another."""
    if case_set.name == "heldout":
        return load_heldout_cases()
    return select_release_cases(dataset, load_reviews(dataset))


def plan_budget(planned: tuple[PlannedSet, ...]) -> Decimal:
    """EVW-5: every planned call (smoke + full set) must fit its ceiling before any paid call."""
    total = Decimal("0")
    for item in planned:
        validate_settings(item.settings, item.case_set.profile)
        calls = len(item.inventory.cases) + 1
        per_call = item.settings.rate.estimate(INPUT_TOKEN_LIMIT, OUTPUT_TOKEN_LIMIT)
        required = per_call * calls
        if required > item.case_set.profile.budget_limit:
            raise ValueError(f"the {item.case_set.name} ceiling cannot reserve all planned calls")
        total += item.case_set.profile.budget_limit
    if total > RUN_CEILING_USD:
        raise ValueError("the review ceilings together exceed the run ceiling")
    return total


def require_throwaway_database(engine: Engine, env: Mapping[str, str]) -> None:
    """Never run against a database someone keeps: loopback or socket, explicit flag, empty."""
    if env.get("EVAL_THROWAWAY_DATABASE") != "YES":
        raise ValueError("EVAL_THROWAWAY_DATABASE=YES is required (a disposable database)")
    url = make_url(engine.url)
    host = url.host or url.query.get("host")
    if not (host in {"127.0.0.1", "localhost", "::1"} or str(host or "").startswith("/")):
        raise ValueError("the evaluation database must be local (loopback or Unix socket)")
    with engine.connect() as conn:
        used = conn.execute(
            sa.text("SELECT EXISTS (SELECT 1 FROM runs) OR EXISTS (SELECT 1 FROM proposal_calls)")
        ).scalar_one()
    if used:
        raise ValueError("the evaluation database is not empty; use a fresh one")


def require_eval_telemetry(env: Mapping[str, str]) -> telemetry.TelemetrySettings:
    """Live runs export to Langfuse US only, as `eval`, with tokenized text (OBS-4)."""
    settings = telemetry.TelemetrySettings.from_env(env)
    if not settings.enabled or settings.environment != "eval":
        raise ValueError("TELEMETRY_ENABLED=YES and TELEMETRY_ENVIRONMENT=eval are required")
    if not settings.langfuse or not settings.eval_text or settings.grafana:
        raise ValueError("the evaluation exports to Langfuse only, with TELEMETRY_EVAL_TEXT=YES")
    if langfuse_base(settings.langfuse_endpoint or "") is None:
        raise ValueError("the Langfuse endpoint must be the US region host over HTTPS")
    if settings.relay_key:
        raise ValueError("the runner reaches Langfuse directly; do not set the relay key")
    return settings


def langfuse_base(endpoint: str) -> str | None:
    parsed = urllib.parse.urlsplit(endpoint)
    if parsed.scheme != "https" or parsed.hostname not in LANGFUSE_HOSTS:
        return None
    return f"https://{parsed.hostname}"


def report_name(now: datetime, digest: str) -> str:
    return f"eval-{now.strftime('%Y%m%dT%H%MZ')}-{digest[:12]}"


def preflight(
    engine: Engine,
    env: Mapping[str, str],
    output_dir: Path,
    *,
    live: bool,
    now: datetime,
) -> tuple[tuple[PlannedSet, ...], str, Path]:
    missing = unclassified()
    if missing:
        raise ValueError(f"unclassified behavior files: {missing}")
    if live:
        if not env.get("OPENAI_API_KEY", "").strip():
            raise ValueError("OPENAI_API_KEY must be set (llm-eval environment secret)")
        require_eval_telemetry(env)
    if not output_dir.is_dir() or not os.access(output_dir, os.W_OK):
        raise ValueError("report directory must exist and be writable")
    digest = fingerprint()
    destination = output_dir / report_name(now, digest)
    if destination.exists():
        raise FileExistsError("this report already exists; inspect it first")
    gate = GateConfig.from_env(GATE_ENV)
    validate_release_gate_config(gate)
    dataset = load_dataset()
    planned = tuple(
        PlannedSet(case_set, load_inventory(case_set, dataset), proposal_settings(case_set))
        for case_set in CI_SETS
    )
    plan_budget(planned)
    upgrade_schema(engine)
    require_throwaway_database(engine, env)
    for item in planned:
        for phase, cases in (
            ("smoke", item.inventory.cases[:1]),
            ("evaluation", item.inventory.cases),
        ):
            validate_unattempted(engine, cases, phase=phase, profile=item.case_set.profile)
    from huggingface_hub import snapshot_download  # type: ignore[import-not-found]

    snapshot_download(
        repo_id=MODEL_REPOSITORY,
        revision=MODEL_REVISION,
        token=False,
        local_files_only=True,
        allow_patterns=MODEL_FILES,
    )
    return planned, digest, destination


def check_smoke(name: str, result: Mapping[str, Any]) -> None:
    """Machine check that replaces the owner's smoke review in the workflow."""
    usage = result.get("usage")
    if (
        result.get("contract_pass") is not True
        or result.get("error") is not None
        or not isinstance(usage, dict)
        or usage.get("actual_usd") in {None, "None"}
    ):
        raise EvaluationStageError(f"stage=smoke-{name} failed: {result.get('error') or 'invalid'}")


def set_report(
    item: PlannedSet,
    dataset: GoldenDataset,
    phase: str,
    cases: tuple[Any, ...],
    results: list[dict[str, Any]],
    engine: Engine,
) -> dict[str, Any]:
    reserved, actual = budget_totals(engine, item.case_set.profile.review_id)
    report: dict[str, Any] = {
        "phase": phase,
        "completed_at": datetime.now(UTC).isoformat(),
        "dataset_version": dataset.version,
        "dataset_sha256": dataset.sha256,
        "model": MODEL,
        "prompt_version": VERSION,
        "contract_sha256": contract_digest(item.inventory.cases),
        "review_id": item.case_set.profile.review_id,
        "case_set": item.case_set.name,
        "budget_ceiling_usd": str(item.case_set.profile.budget_limit),
        "input_token_limit": item.settings.input_tokens,
        "output_token_limit": item.settings.output_tokens,
        "rate_provenance": item.settings.rate.provenance,
        "reserved_usd": str(reserved),
        "actual_usd": str(actual),
        "inventory": item.inventory.summary(),
        "expected_case_ids": [case.case_id for case in cases],
        "cases": results,
    }
    if phase == "evaluation":
        report["gates"] = evaluate_gates(cases, results)
    return report


def langfuse_generations(
    base: str,
    public_key: str,
    secret_key: str,
    since: datetime,
    until: datetime,
    *,
    opener: Callable[[urllib.request.Request], Any] | None = None,
) -> dict[str, Any]:
    """Count `eval` generations in the window through the v2 observations API (F-090)."""
    open_url = opener or (lambda request: urllib.request.urlopen(request, timeout=30))  # noqa: S310
    token = base64.b64encode(f"{public_key}:{secret_key}".encode()).decode("ascii")
    traces: set[str] = set()
    generations = 0
    cost = Decimal("0")
    cursor: str | None = None
    for _ in range(100):
        query = {
            "type": "GENERATION",
            "environment": "eval",
            "fromStartTime": since.isoformat().replace("+00:00", "Z"),
            "toStartTime": until.isoformat().replace("+00:00", "Z"),
            "fields": "core,basic,usage",
            "limit": "1000",
        }
        if cursor:
            query["cursor"] = cursor
        request = urllib.request.Request(
            f"{base}/api/public/v2/observations?{urllib.parse.urlencode(query)}",
            headers={"Authorization": f"Basic {token}", "Accept": "application/json"},
        )
        with open_url(request) as response:
            body = json.loads(response.read())
        rows = body.get("data") if isinstance(body, dict) else None
        if not isinstance(rows, list):
            raise ValueError("unexpected Langfuse observations response")
        for row in rows:
            if isinstance(row, dict) and row.get("type") == "GENERATION":
                generations += 1
                if isinstance(row.get("traceId"), str):
                    traces.add(row["traceId"])
                if isinstance(row.get("totalCost"), int | float):
                    cost += Decimal(str(row["totalCost"]))
        meta = body.get("meta")
        cursor = meta.get("cursor") if isinstance(meta, dict) else None
        if not cursor:
            break
    return {"generations": generations, "traces": len(traces), "total_cost_usd": str(cost)}


def read_back(
    settings: telemetry.TelemetrySettings, since: datetime, expected: int
) -> dict[str, Any]:
    """Recorded, not required: a Langfuse outage must not hide the safety result."""
    base = langfuse_base(settings.langfuse_endpoint or "")
    if base is None or not settings.langfuse_public_key or not settings.langfuse_secret_key:
        return {"status": "not_configured", "expected_generations": expected}
    deadline = time.monotonic() + READBACK_TIMEOUT_SECONDS
    last: dict[str, Any] = {}
    error: str | None = None
    while True:
        until = datetime.now(UTC) + timedelta(minutes=1)
        try:
            last = langfuse_generations(
                base, settings.langfuse_public_key, settings.langfuse_secret_key, since, until
            )
            error = None
        except (OSError, ValueError) as exc:  # urllib errors are OSError
            error = type(exc).__name__
        listed = int(last.get("generations", 0))
        print(f"stage=langfuse-readback listed={listed} expected={expected}", flush=True)
        if listed >= expected or time.monotonic() >= deadline:
            break
        time.sleep(READBACK_INTERVAL_SECONDS)
    status = "complete" if int(last.get("generations", 0)) >= expected else "incomplete"
    return {
        "status": status,
        "expected_generations": expected,
        **last,
        **({"last_error": error} if error else {}),
    }


def ci_provenance(env: Mapping[str, str]) -> dict[str, str | None]:
    sha = env.get("GITHUB_SHA", "")
    run_id = env.get("GITHUB_RUN_ID", "")
    return {
        "commit": sha if _SHA.fullmatch(sha) else None,
        "workflow_run_id": run_id if _RUN_ID.fullmatch(run_id) else None,
    }


def summary_report(
    *,
    digest: str,
    sets: Mapping[str, Mapping[str, Any]],
    langfuse: Mapping[str, Any],
    env: Mapping[str, str],
    completed_at: datetime,
) -> dict[str, Any]:
    """Built from allowlisted values only, so no secret can reach it (EVW-6)."""
    reserved = sum((Decimal(s["reserved_usd"]) for s in sets.values()), Decimal("0"))
    actual = sum((Decimal(s["actual_usd"]) for s in sets.values()), Decimal("0"))
    return {
        "kind": "live-evaluation",
        "schema": 1,
        "fingerprint": digest,
        "fingerprint_inputs": fingerprint_inputs(),
        "completed_at": completed_at.isoformat(),
        **ci_provenance(env),
        "model": MODEL,
        "prompt_version": VERSION,
        "policy_model_id": MODEL_ID,
        "run_ceiling_usd": str(RUN_CEILING_USD),
        "reserved_usd": str(reserved),
        "actual_usd": str(actual),
        "safety_pass": all(s["safety_pass"] for s in sets.values()),
        "quality_pass": all(s["quality_pass"] for s in sets.values()),
        "sets": {name: dict(value) for name, value in sets.items()},
        "langfuse": dict(langfuse),
    }


def set_summary(report: Mapping[str, Any]) -> dict[str, Any]:
    gates = report["gates"]["gates"]
    return {
        "review_id": report["review_id"],
        "cases": len(report["cases"]),
        "budget_ceiling_usd": report["budget_ceiling_usd"],
        "reserved_usd": report["reserved_usd"],
        "actual_usd": report["actual_usd"],
        "gates": {name: bool(gates[name]["pass"]) for name in ("G1", "G2", "G3", "G4_G5")},
        "safety_pass": all(bool(gates[name]["pass"]) for name in ("G1", "G2", "G3")),
        "quality_pass": bool(gates["G4_G5"]["pass"]),
        "report": f"{report['case_set']}-evaluation/README.md",
        "smoke_report": f"{report['case_set']}-smoke/README.md",
    }


def render_summary(report: Mapping[str, Any]) -> str:
    safety = "PASS" if report["safety_pass"] else "FAIL (deploys blocked)"
    quality = "PASS" if report["quality_pass"] else "WARN (deploy needs an override reason)"
    lines = [
        "# Live evaluation report (#135)",
        "",
        f"- Fingerprint: `{report['fingerprint']}`",
        f"- Commit: `{report.get('commit')}`; workflow run: `{report.get('workflow_run_id')}`",
        f"- Model: `{report['model']}`; prompt: `{report['prompt_version']}`",
        f"- Safety gates G1..G3: **{safety}**; quality gates G4/G5: **{quality}**",
        f"- Spend: USD {report['actual_usd']} estimated; USD {report['reserved_usd']} reserved "
        f"(run ceiling USD {report['run_ceiling_usd']}).",
        "- Raw data: [report.json](report.json).",
        "",
        "| Set | Cases | G1 | G2 | G3 | G4/G5 | Estimated USD | Report |",
        "| :-- | :-- | :-- | :-- | :-- | :-- | :-- | :-- |",
    ]
    for name, item in report["sets"].items():
        g = item["gates"]
        marks = [("pass" if g[key] else "FAIL") for key in ("G1", "G2", "G3", "G4_G5")]
        lines.append(
            f"| {name} | {item['cases']} | {' | '.join(marks)} | {item['actual_usd']} | "
            f"[evaluation]({item['report']}), [smoke]({item['smoke_report']}) |"
        )
    langfuse = report["langfuse"]
    lines += [
        "",
        "## Langfuse",
        "",
        f"Read back through `GET /api/public/v2/observations` (environment `eval`): status "
        f"`{langfuse.get('status')}`, {langfuse.get('generations', 0)} of "
        f"{langfuse.get('expected_generations')} expected generations listed in "
        f"{langfuse.get('traces', 0)} traces.",
        "",
        "## How to read this",
        "",
        "Each set ran a 1-case smoke checked by machine, then every case, through the real "
        "proposal step on fixture carts. Nothing was executed or sent. The owner's approval of "
        "the workflow run replaced the smoke digest review (owner decision 2026-11-26). The "
        "deploy gate (#157) accepts this report only for a commit with the same fingerprint. "
        "Limits of each set are in its evaluation report.",
        "",
    ]
    return "\n".join(lines)


def run_live(
    engine: Engine,
    planned: tuple[PlannedSet, ...],
    digest: str,
    destination: Path,
    env: Mapping[str, str],
) -> Path:
    settings = require_eval_telemetry(env)
    handle = telemetry.configure_from_env(env, service_name="aorte-eval")
    started = datetime.now(UTC) - timedelta(minutes=1)
    staging = Path(tempfile.mkdtemp(prefix=".eval-partial-", dir=destination.parent))
    try:
        with progress("eval-model-load-and-policy-index"):
            embedder = LocalE5Embedder()
            count = ingest(engine, embedder, POLICY_SOURCE)
        print(f"stage=policy-index passages={count}", flush=True)
        retriever = PostgresRetriever(engine, embedder)
        provider = RetryOnTimeout(OpenAIClient(env.get("OPENAI_API_KEY", "")))
        gate_config = GateConfig.from_env(GATE_ENV)
        dataset = load_dataset()
        summaries: dict[str, dict[str, Any]] = {}
        calls = 0
        for item in planned:
            name = item.case_set.name
            for phase in ("smoke", "evaluation"):
                cases = item.inventory.cases[:1] if phase == "smoke" else item.inventory.cases
                gate = Gate(gate_config, InMemoryGateLedger())
                results: list[dict[str, Any]] = []
                for index, case in enumerate(cases, start=1):
                    with progress(f"eval-{name}-{phase}-case-{index}-of-{len(cases)}"):
                        result = evaluate_case(
                            engine,
                            dataset,
                            case,
                            phase=phase,
                            provider=provider,
                            retriever=retriever,
                            gate=gate,
                            settings=item.settings,
                            profile=item.case_set.profile,
                            build_cart=release_fixture_cart,
                        )
                    result["provider_timeout_retries"] = provider.retries
                    calls += 1 + provider.retries
                    provider.retries = 0
                    results.append(result)
                if phase == "smoke":
                    check_smoke(name, results[0])
                report = set_report(item, dataset, phase, cases, results, engine)
                publish_release_report(
                    staging,
                    phase,
                    report,
                    report_prefix=name,
                    review_id=item.case_set.profile.review_id,
                )
                print(f"stage={name}-{phase} status=complete cases={len(cases)}", flush=True)
            summaries[name] = set_summary(report)
        handle.force_flush()
        langfuse = read_back(settings, started, calls)
        summary = summary_report(
            digest=digest,
            sets=summaries,
            langfuse=langfuse,
            env=env,
            completed_at=datetime.now(UTC),
        )
        (staging / "report.json").write_text(
            json.dumps(summary, indent=2, sort_keys=True, ensure_ascii=False) + "\n",
            encoding="utf-8",
        )
        (staging / "README.md").write_text(render_summary(summary), encoding="utf-8")
        os.rename(staging, destination)
    except BaseException:
        shutil.rmtree(staging, ignore_errors=True)
        raise
    finally:
        handle.shutdown()
    return destination


def main() -> None:
    parser = argparse.ArgumentParser(description="Live release-gate evaluation for CI (#135)")
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--execute-live", action="store_true")
    args = parser.parse_args()
    env = dict(os.environ)
    print("stage=preflight status=started", flush=True)
    engine = create_engine(EngineConfig.from_env(env).database_url)
    try:
        planned, digest, destination = preflight(
            engine, env, args.output_dir, live=args.execute_live, now=datetime.now(UTC)
        )
        sizes = {item.case_set.name: len(item.inventory.cases) for item in planned}
        print(
            f"stage=preflight status=passed fingerprint={digest} cases={sizes}; provider access, "
            "Langfuse reach, inference, and output quality were not checked",
            flush=True,
        )
        if not args.execute_live:
            return
        report = run_live(engine, planned, digest, destination, env)
        print(f"stage=complete report={report}", flush=True)
        output = env.get("GITHUB_OUTPUT")
        if output:
            with open(output, "a", encoding="utf-8") as handle:
                handle.write(f"report_dir={report.name}\nfingerprint={digest}\n")
    finally:
        engine.dispose()


if __name__ == "__main__":
    main()
