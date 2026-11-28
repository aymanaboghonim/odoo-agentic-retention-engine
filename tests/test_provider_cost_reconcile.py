"""#130, BR-1: the daily billing reconciliation (scripts/provider_cost_reconcile.py).

Hermetic: the OpenAI Costs API, the Grafana query API, and the OTLP push are fakes; the
workflow and Terraform files are read as text or with python-hcl2.
"""

from __future__ import annotations

import base64
import json
import re
from collections.abc import Mapping
from datetime import UTC, date, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlsplit

import hcl2
import pytest

from scripts import provider_cost_reconcile as billing

ROOT = Path(__file__).resolve().parents[1]
NOW = datetime(2026, 11, 27, 6, 23, tzinfo=UTC)
TARGET = date(2026, 11, 26)
# Synthetic values, built at run time so no key-shaped literal is in the repo (secret scan).
ADMIN_KEY = "-".join(("synthetic", "admin", "value"))
READ_TOKEN = "-".join(("synthetic", "read", "value"))
OTLP_TOKEN = base64.b64encode(b"123:synthetic-otlp-value").decode()
STACK = "https://synthetic-stack.grafana.net"
ENV = {
    "OPENAI_ADMIN_KEY": ADMIN_KEY,
    "GRAFANA_STACK_URL": STACK,
    "GRAFANA_READ_TOKEN": READ_TOKEN,
    "GRAFANA_OTLP_ENDPOINT": "https://otlp-gateway-prod-us-east-2.grafana.net/otlp",
    "GRAFANA_OTLP_TOKEN": OTLP_TOKEN,
}
OPENAI_AMOUNT = "0.123456"  # distinctive, so a leak into output is easy to find
ENGINE_AMOUNT = "0.0234567"


def day_start(day: date) -> int:
    return int(datetime(day.year, day.month, day.day, tzinfo=UTC).timestamp())


def window() -> list[date]:
    return [date(2026, 11, d) for d in range(20, 27)]


class Fakes:
    def __init__(
        self,
        *,
        missing: set[date] = frozenset(),  # type: ignore[assignment]
        provider_error: bool = False,
        grafana_error: bool = False,
        pages: int = 1,
    ) -> None:
        self.missing = missing
        self.provider_error = provider_error
        self.grafana_error = grafana_error
        self.pages = pages
        self.calls: list[tuple[str, dict[str, str]]] = []
        self.posts: list[tuple[str, dict[str, str], bytes]] = []

    def fetch(self, url: str, headers: Mapping[str, str]) -> Any:
        self.calls.append((url, dict(headers)))
        query = parse_qs(urlsplit(url).query)
        if url.startswith(billing.COSTS_URL):
            if self.provider_error:
                raise OSError("synthetic provider failure")
            page = int(query.get("page", ["0"])[0])
            days = [d for d in window() if d not in self.missing]
            chunk = days[page :: self.pages]
            data = [
                {
                    "start_time": day_start(d),
                    "end_time": day_start(d) + 86400,
                    "results": [
                        {
                            "amount": {"value": Decimal(OPENAI_AMOUNT), "currency": "usd"},
                            "project_id": "proj_synthetic_a",
                        },
                        {
                            "amount": {"value": Decimal("0.01"), "currency": "usd"},
                            "project_id": "proj_synthetic_b",
                        },
                    ],
                }
                for d in chunk
            ]
            more = page + 1 < self.pages
            return {
                "object": "page",
                "data": data,
                "has_more": more,
                "next_page": str(page + 1) if more else None,
            }
        assert url.startswith(f"{STACK}/api/datasources/proxy/uid/grafanacloud-prom/api/v1/query")
        if self.grafana_error:
            raise OSError("synthetic grafana failure")
        return {
            "status": "success",
            "data": {"resultType": "vector", "result": [{"value": [1, ENGINE_AMOUNT]}]},
        }

    def post(self, url: str, headers: Mapping[str, str], body: bytes) -> None:
        self.posts.append((url, dict(headers), body))


def eval_reports(tmp_path: Path, *items: tuple[str, str, str]) -> Path:
    for name, completed, usd in items:
        folder = tmp_path / name
        folder.mkdir()
        (folder / "report.json").write_text(
            json.dumps({"kind": "live-evaluation", "completed_at": completed, "actual_usd": usd})
        )
    (tmp_path / "old-format").mkdir()
    (tmp_path / "old-format" / "raw.json").write_text("{}")
    return tmp_path


def run(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], fakes: Fakes, results: Path | None = None
) -> tuple[int, str, dict[str, Any]]:
    out = tmp_path / "out"
    code = billing.main(
        ["--out-dir", str(out)],
        ENV,
        fetch=fakes.fetch,
        post=fakes.post,
        results=results or tmp_path,
        now=NOW,
    )
    captured = capsys.readouterr()
    path = out / f"{TARGET.isoformat()}.json"
    report = json.loads(path.read_text()) if path.exists() else {}
    return code, captured.out + captured.err, report


def assert_no_leak(text: str) -> None:
    for secret in (
        ADMIN_KEY,
        READ_TOKEN,
        OTLP_TOKEN,
        STACK,
        OPENAI_AMOUNT,
        ENGINE_AMOUNT,
        "proj_synthetic",
        "0.1",
    ):
        assert secret not in text


def test_billing_reconcile_output_and_missing_bucket(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """BR-1: no amounts or keys in output; a missing provider day is unknown, never 0."""
    fakes = Fakes(missing={TARGET})
    code, output, report = run(tmp_path, capsys, fakes)
    assert code == 0
    assert output.strip() == f"billing day={TARGET} status=unknown push=ok"
    assert_no_leak(output)
    row = report["days"][TARGET.isoformat()]
    assert row["status"] == "unknown"
    assert row["provider_usd"] is None and row["drift_usd"] is None
    # The other days are still reconciled.
    assert report["days"]["2026-11-25"]["provider_usd"] == "0.133456"
    # Grafana gets known=0 and no amount for the unknown day: never a 0 that reads as no spend.
    (_, headers, body) = fakes.posts[0]
    sent = json.loads(body)["resourceMetrics"][0]["scopeMetrics"][0]["metrics"]
    names = {m["name"]: m["gauge"]["dataPoints"][0]["asDouble"] for m in sent}
    assert names == {"aorte.billing.known": 0.0, "aorte.billing.engine_cost_usd": 0.0234567}
    assert headers["Authorization"] == f"Basic {OTLP_TOKEN}"


def test_provider_or_grafana_failure_is_unknown(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    for fakes in (Fakes(provider_error=True), Fakes(grafana_error=True)):
        code, output, report = run(tmp_path, capsys, fakes)
        assert code == 0 and "status=unknown" in output
        assert_no_leak(output)
        assert {r["status"] for r in report["days"].values()} == {"unknown"}
        assert "synthetic" not in output  # never the exception text


def test_drift_includes_merged_evaluation_reports(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    (tmp_path / "results").mkdir()
    results = eval_reports(
        tmp_path / "results",
        ("eval-a", "2026-11-26T08:33:09+00:00", "0.0500"),
        ("eval-b", "2026-11-26T23:59:00+00:00", "0.0600"),
        ("eval-c", "2026-11-25T10:00:00+00:00", "0.9"),
    )
    code, output, report = run(tmp_path, capsys, Fakes(), results)
    row = report["days"][TARGET.isoformat()]
    # 0.133456 - 0.0234567 - 0.11 = -0.0000007: pass.
    assert row["evaluation_usd"] == "0.1100"
    assert Decimal(row["drift_usd"]) == Decimal("0.133456") - Decimal(ENGINE_AMOUNT) - Decimal(
        "0.11"
    )
    assert row["status"] == "pass" and output.strip().endswith("status=pass push=ok")
    assert row["provider_by_project"] == {
        "proj_synthetic_a": "0.123456",
        "proj_synthetic_b": "0.01",
    }
    # The day with the 0.9 evaluation is far off: drift, and still no amount in the output.
    assert report["days"]["2026-11-25"]["status"] == "drift"
    assert_no_leak(output)


def test_costs_query_and_pagination(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    fakes = Fakes(pages=3)
    code, _, report = run(tmp_path, capsys, fakes)
    assert code == 0
    assert all(r["provider_usd"] == "0.133456" for r in report["days"].values())
    cost_calls = [(u, h) for u, h in fakes.calls if u.startswith(billing.COSTS_URL)]
    assert len(cost_calls) == 3
    first = parse_qs(urlsplit(cost_calls[0][0]).query)
    assert first["start_time"] == [str(day_start(date(2026, 11, 20)))]
    assert first["end_time"] == [str(day_start(date(2026, 11, 27)))]  # exclusive: today
    assert first["bucket_width"] == ["1d"] and first["group_by"] == ["project_id"]
    assert cost_calls[0][1]["Authorization"] == f"Bearer {ADMIN_KEY}"
    grafana = [u for u, _ in fakes.calls if "api/v1/query" in u]
    assert len(grafana) == 7
    # The estimate for a day is read at the day's end.
    last = parse_qs(urlsplit(grafana[-1]).query)
    assert last["time"] == [str(day_start(date(2026, 11, 27)))]
    assert "aorte_llm_cost_usd" in last["query"][0]


def test_malformed_answers_are_unknown() -> None:
    days = window()
    with pytest.raises(billing.LookupFailed):
        billing.provider_costs(
            days,
            "k",
            lambda u, h: {
                "data": [
                    {"start_time": 1, "results": [{"amount": {"value": "1", "currency": "eur"}}]}
                ]
            },
        )
    with pytest.raises(billing.LookupFailed):
        billing.provider_costs(days, "k", lambda u, h: {"data": [], "has_more": True})
    with pytest.raises(billing.LookupFailed):
        billing.engine_estimate(TARGET, STACK, "t", lambda u, h: {"status": "error"})
    with pytest.raises(billing.LookupFailed):
        billing.engine_estimate(
            TARGET,
            STACK,
            "t",
            lambda u, h: {
                "status": "success",
                "data": {"resultType": "vector", "result": [{"value": [1, "NaN"]}]},
            },
        )
    # No series: the engine made no call that day.
    assert billing.engine_estimate(
        TARGET,
        STACK,
        "t",
        lambda u, h: {"status": "success", "data": {"resultType": "vector", "result": []}},
    ) == Decimal("0")


@pytest.mark.parametrize(
    ("key", "value"),
    [
        ("OPENAI_ADMIN_KEY", ""),
        ("GRAFANA_STACK_URL", "http://synthetic-stack.grafana.net"),
        ("GRAFANA_STACK_URL", "https://example.test"),
        ("GRAFANA_OTLP_ENDPOINT", "https://example.test/otlp"),
        ("GRAFANA_OTLP_TOKEN", ""),
    ],
)
def test_preflight_fails_closed_without_lookups(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], key: str, value: str
) -> None:
    fakes = Fakes()
    code = billing.main(
        ["--out-dir", str(tmp_path)],
        {**ENV, key: value},
        fetch=fakes.fetch,
        post=fakes.post,
        results=tmp_path,
        now=NOW,
    )
    output = capsys.readouterr()
    assert code == 2 and fakes.calls == [] and fakes.posts == []
    assert key in output.err and "status=config-error" in output.err
    assert_no_leak(output.out + output.err)


def test_unfinished_day_and_push_failure(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    fakes = Fakes()
    assert (
        billing.main(
            ["--out-dir", str(tmp_path), "--day", "2026-11-27"],
            ENV,
            fetch=fakes.fetch,
            post=fakes.post,
            results=tmp_path,
            now=NOW,
        )
        == 2
    )
    capsys.readouterr()

    def failing(url: str, headers: Mapping[str, str], body: bytes) -> None:
        raise OSError(f"cannot reach {url} with {headers}")

    code = billing.main(
        ["--out-dir", str(tmp_path / "out")],
        ENV,
        fetch=Fakes().fetch,
        post=failing,
        results=tmp_path,
        now=NOW,
    )
    output = capsys.readouterr().out
    assert (
        code == 1 and output.strip() == f"billing day={TARGET} status=drift push=failed (OSError)"
    )
    assert (tmp_path / "out" / f"{TARGET}.json").exists()  # the report is still stored
    assert_no_leak(output)


# ----------------------------------------------------------------- workflow and Terraform

WORKFLOW = (ROOT / ".github" / "workflows" / "billing-reconcile.yml").read_text(encoding="utf-8")


def test_billing_workflow_is_daily_unattended_and_least_privilege() -> None:
    assert re.search(r"schedule:\n\s+# .*\n\s+- cron: \"23 6 \* \* \*\"", WORKFLOW)
    assert "environment: billing" in WORKFLOW and "if: github.ref == 'refs/heads/main'" in WORKFLOW
    assert "permissions: {}" in WORKFLOW
    job_permissions = WORKFLOW.split("    permissions:\n", 1)[1].split("    steps:", 1)[0]
    assert job_permissions.split() == ["contents:", "read", "id-token:", "write"]
    for use in re.findall(r"uses: (\S+)", WORKFLOW):
        assert re.fullmatch(r"[\w.-]+/[\w.-]+@[0-9a-f]{40}", use), use
    steps = WORKFLOW.split("    steps:\n", 1)[1]
    assert steps.index("::add-mask::") < steps.index("- uses:")
    # AWS credentials only after the provider lookups, for the reports/ upload only.
    assert steps.index("provider_cost_reconcile.py") < steps.index("configure-aws-credentials")
    assert "--only-show-errors" in steps and "reports/billing/" in steps
    code = "\n".join(line for line in WORKFLOW.splitlines() if not line.lstrip().startswith("#"))
    assert "${{ vars." not in code and "set -x" not in code
    assert not re.search(r"(?<![\w.])\d{12}(?![\w.])", WORKFLOW)
    for secret in (
        "OPENAI_ADMIN_KEY",
        "GRAFANA_READ_TOKEN",
        "GRAFANA_OTLP_TOKEN",
        "GRAFANA_STACK_URL",
        "AWS_BILLING_ROLE_ARN",
        "AWS_BACKUPS_BUCKET",
    ):
        assert f"secrets.{secret}" in WORKFLOW
    assert "secrets.AWS_DEPLOY_ROLE_ARN" not in WORKFLOW


OPTIONS = hcl2.SerializationOptions(strip_string_quotes=True, with_comments=False)


def load(path: Path) -> dict[str, Any]:
    with path.open(encoding="utf-8") as handle:
        return hcl2.load(handle, serialization_options=OPTIONS)


def test_billing_role_writes_only_reports() -> None:
    bootstrap = load(ROOT / "deploy" / "terraform" / "bootstrap" / "main.tf")
    documents = {
        k: v for block in bootstrap["data"] for k, v in block["aws_iam_policy_document"].items()
    }
    (statement,) = documents["billing"]["statement"]
    assert statement["actions"] == ["s3:PutObject"]
    assert statement["resources"] == ["${local.arn_prefix}:s3:::${local.mvp}-backups-*/reports/*"]
    (trust,) = documents["billing_trust"]["statement"]
    subjects = [c for c in trust["condition"] if c["variable"].endswith(":sub")]
    assert subjects[0]["values"][0].endswith(":environment:${var.billing_environment}")
    variables = {
        k: v
        for block in load(ROOT / "deploy" / "terraform" / "bootstrap" / "variables.tf")["variable"]
        for k, v in block.items()
    }
    assert variables["billing_environment"]["default"] == "billing"
    assert variables["github_environment"]["default"] == "aws-mvp"


def test_reports_prefix_expires() -> None:
    backups = load(ROOT / "deploy" / "terraform" / "mvp" / "backups.tf")
    (lifecycle,) = [
        b["aws_s3_bucket_lifecycle_configuration"]["backups"]
        for b in backups["resource"]
        if "aws_s3_bucket_lifecycle_configuration" in b
    ]
    rules = {r["id"]: r for r in lifecycle["rule"]}
    assert rules["expire-reports"]["filter"][0]["prefix"] == "reports/"
    assert rules["expire-reports"]["expiration"][0]["days"] == 90
    assert rules["expire-reports"]["status"] == "Enabled"
    # models/ never expires (the pinned embedding model, F-072).
    assert all(r["filter"][0]["prefix"] != "models/" for r in rules.values())
