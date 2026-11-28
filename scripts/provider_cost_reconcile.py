"""Daily provider billing reconciliation (#130; ADR-0029 decision 8, ADR-0034; BR-1).

    python scripts/provider_cost_reconcile.py --out-dir DIR [--day YYYY-MM-DD]

Runs in `.github/workflows/billing-reconcile.yml` (environment `billing`). For each UTC day of
the last 7 days ending at `--day` (default: yesterday) it compares:

- provider: the OpenAI organization Costs API (`GET /v1/organization/costs`, admin key,
  daily buckets, grouped by project; F-036);
- engine: the engine's own estimate in Grafana, `increase(aorte_llm_cost_usd*_total[24h])`
  at the day's end, over every environment that exports to Grafana;
- evaluations: the `actual_usd` of live-evaluation reports merged to `main`
  (`evals/release/results/*/report.json`), by the UTC day of `completed_at`. Evaluation runs
  export to Langfuse only, so they are not in the Grafana estimate (owner, 2026-11-27).

drift = provider - (engine + evaluations). A day is `pass` when |drift| <= USD 0.10, `drift`
above that, and `unknown` when the provider has no bucket for it or a lookup failed: a missing
bucket is never read as 0 (BR-1). Grafana accepts samples at most 2 h old (F-102), so the day
cannot be backdated: the job pushes the target day's values as current gauges.

Output (public repo logs): one `billing day=... status=...` line, never an amount, key, URL,
or project ID (BR-1). The aggregate JSON report (no PII) goes to `--out-dir/<day>.json`; the
workflow copies it to `reports/billing/` in the backups bucket.
"""

from __future__ import annotations

import argparse
import base64
import json
import sys
from collections.abc import Callable, Iterable, Mapping, Sequence
from datetime import UTC, date, datetime, time, timedelta
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any, Literal
from urllib.parse import urlencode, urlsplit
from urllib.request import Request, urlopen

ROOT = Path(__file__).resolve().parents[1]
RESULTS = ROOT / "evals" / "release" / "results"
SCHEMA = 1
WINDOW_DAYS = 7
THRESHOLD_USD = Decimal("0.10")
TIMEOUT_SECONDS = 20
MAX_PAGES = 5
COSTS_URL = "https://api.openai.com/v1/organization/costs"
PROM_UID = "grafanacloud-prom"
ENGINE_EXPR = 'sum(increase({__name__=~"aorte_llm_cost_usd.*_total"}[24h]))'
RESOURCE = {"service.name": "aorte-billing", "deployment.environment.name": "aws-mvp"}
REQUIRED_ENV = (
    "OPENAI_ADMIN_KEY",
    "GRAFANA_STACK_URL",
    "GRAFANA_READ_TOKEN",
    "GRAFANA_OTLP_ENDPOINT",
    "GRAFANA_OTLP_TOKEN",
)

Status = Literal["pass", "drift", "unknown"]
Fetch = Callable[[str, Mapping[str, str]], Any]
Post = Callable[[str, Mapping[str, str], bytes], None]


class ConfigError(ValueError):
    """A missing or malformed setting; the message names the setting, never its value."""


class LookupFailed(Exception):
    """A provider or Grafana lookup failed; its days become `unknown` (BR-1)."""


# ------------------------------------------------------------------------------ HTTP


def _fetch_json(url: str, headers: Mapping[str, str]) -> Any:
    request = Request(url, headers=dict(headers))  # noqa: S310 - fixed https hosts
    with urlopen(request, timeout=TIMEOUT_SECONDS) as response:  # noqa: S310
        return json.loads(response.read(2_000_000), parse_float=Decimal)


def _post(url: str, headers: Mapping[str, str], body: bytes) -> None:
    request = Request(url, data=body, headers=dict(headers), method="POST")  # noqa: S310
    with urlopen(request, timeout=TIMEOUT_SECONDS) as response:  # noqa: S310
        if not 200 <= response.status < 300:
            raise LookupFailed("otlp push")


def _https(env: Mapping[str, str], key: str, *, suffix: str) -> str:
    value = env.get(key, "").strip().rstrip("/")
    parts = urlsplit(value)
    host = parts.hostname or ""
    if parts.scheme != "https" or not host.endswith(suffix) or parts.query:
        raise ConfigError(f"{key} must be an https URL on *{suffix}")
    return value


def _secret(env: Mapping[str, str], key: str) -> str:
    value = env.get(key, "").strip()
    if not value:
        raise ConfigError(f"{key} is required")
    return value


def _day_start(day: date) -> int:
    return int(datetime.combine(day, time(), tzinfo=UTC).timestamp())


def _amount(value: object) -> Decimal:
    try:
        amount = Decimal(str(value))
    except InvalidOperation as error:
        raise LookupFailed("amount") from error
    if not amount.is_finite():
        raise LookupFailed("amount")
    return amount


# ------------------------------------------------------------------------- provider


def provider_costs(
    days: Sequence[date], admin_key: str, fetch: Fetch = _fetch_json
) -> dict[date, dict[str, Decimal] | None]:
    """USD per project for each day; None for a day without a bucket (never 0, BR-1)."""
    headers = {"Authorization": f"Bearer {admin_key}", "Content-Type": "application/json"}
    params: dict[str, object] = {
        "start_time": _day_start(days[0]),
        "end_time": _day_start(days[-1] + timedelta(days=1)),
        "bucket_width": "1d",
        "group_by": "project_id",
        "limit": len(days),
    }
    buckets: dict[int, dict[str, Decimal]] = {}
    for _ in range(MAX_PAGES):
        page = fetch(f"{COSTS_URL}?{urlencode(params)}", headers)
        if not isinstance(page, dict) or not isinstance(page.get("data"), list):
            raise LookupFailed("costs page")
        for bucket in page["data"]:
            if not isinstance(bucket, dict) or type(bucket.get("start_time")) is not int:
                raise LookupFailed("costs bucket")
            results = bucket.get("results")
            if not isinstance(results, list):
                raise LookupFailed("costs results")
            per_project = buckets.setdefault(bucket["start_time"], {})
            for result in results:
                amount = result.get("amount") if isinstance(result, dict) else None
                if not isinstance(amount, dict) or amount.get("currency") != "usd":
                    raise LookupFailed("costs currency")
                project = str(result.get("project_id") or "none")
                per_project[project] = per_project.get(project, Decimal("0")) + _amount(
                    amount.get("value")
                )
        if page.get("has_more") is not True:
            break
        next_page = page.get("next_page")
        if not isinstance(next_page, str) or not next_page:
            raise LookupFailed("costs paging")
        params["page"] = next_page
    else:
        raise LookupFailed("costs paging")
    return {day: buckets.get(_day_start(day)) for day in days}


# --------------------------------------------------------------------------- engine


def engine_estimate(
    day: date, stack_url: str, read_token: str, fetch: Fetch = _fetch_json
) -> Decimal:
    """The engine's estimated USD for one UTC day from Grafana; no series means no spend."""
    query = urlencode({"query": ENGINE_EXPR, "time": _day_start(day + timedelta(days=1))})
    url = f"{stack_url}/api/datasources/proxy/uid/{PROM_UID}/api/v1/query?{query}"
    answer = fetch(url, {"Authorization": f"Bearer {read_token}"})
    if not isinstance(answer, dict) or answer.get("status") != "success":
        raise LookupFailed("grafana query")
    data = answer.get("data")
    if not isinstance(data, dict):
        raise LookupFailed("grafana query")
    result = data.get("result")
    if data.get("resultType") != "vector" or not isinstance(result, list) or len(result) > 1:
        raise LookupFailed("grafana result")
    if not result:
        return Decimal("0")
    value = result[0].get("value") if isinstance(result[0], dict) else None
    if not isinstance(value, list) or len(value) != 2:
        raise LookupFailed("grafana value")
    return _amount(value[1])


def evaluation_spend(days: Iterable[date], results: Path = RESULTS) -> dict[date, Decimal]:
    """Actual USD of merged live-evaluation reports by the UTC day they completed."""
    totals = {day: Decimal("0") for day in days}
    for path in sorted(results.glob("*/report.json")):
        report = json.loads(path.read_text(encoding="utf-8"), parse_float=Decimal)
        if not isinstance(report, dict) or report.get("kind") != "live-evaluation":
            continue
        completed = datetime.fromisoformat(str(report["completed_at"])).astimezone(UTC).date()
        if completed in totals:
            totals[completed] += _amount(report["actual_usd"])
    return totals


# --------------------------------------------------------------------------- report


def reconcile_day(
    provider: Mapping[str, Decimal] | None, engine: Decimal | None, evaluations: Decimal
) -> dict[str, Any]:
    row: dict[str, Any] = {
        "provider_usd": None,
        "provider_by_project": None,
        "engine_usd": None if engine is None else str(engine),
        "evaluation_usd": str(evaluations),
        "drift_usd": None,
        "status": "unknown",
    }
    if provider is not None:
        total = sum(provider.values(), Decimal("0"))
        row["provider_usd"] = str(total)
        row["provider_by_project"] = {key: str(value) for key, value in sorted(provider.items())}
        if engine is not None:
            drift = total - engine - evaluations
            row["drift_usd"] = str(drift)
            row["status"] = "pass" if abs(drift) <= THRESHOLD_USD else "drift"
    return row


def build_report(
    target: date,
    env: Mapping[str, str],
    *,
    fetch: Fetch = _fetch_json,
    results: Path = RESULTS,
    now: datetime | None = None,
) -> dict[str, Any]:
    days = [target - timedelta(days=offset) for offset in range(WINDOW_DAYS - 1, -1, -1)]
    stack_url = _https(env, "GRAFANA_STACK_URL", suffix=".grafana.net")
    admin_key = _secret(env, "OPENAI_ADMIN_KEY")
    read_token = _secret(env, "GRAFANA_READ_TOKEN")
    try:
        provider: Mapping[date, Mapping[str, Decimal] | None] = provider_costs(
            days, admin_key, fetch
        )
    except Exception:  # noqa: BLE001 - BR-1: any provider failure is unknown, never 0
        provider = dict.fromkeys(days)
    evaluations = evaluation_spend(days, results)
    rows = {}
    for day in days:
        try:
            engine: Decimal | None = engine_estimate(day, stack_url, read_token, fetch)
        except Exception:  # noqa: BLE001 - BR-1
            engine = None
        rows[day.isoformat()] = reconcile_day(provider[day], engine, evaluations[day])
    return {
        "kind": "billing-reconcile",
        "schema": SCHEMA,
        "generated_at": (now or datetime.now(UTC)).isoformat(),
        "target_day": target.isoformat(),
        "threshold_usd": str(THRESHOLD_USD),
        "currency": "usd",
        "days": rows,
    }


def otlp_body(row: Mapping[str, Any], now: datetime) -> bytes:
    """Gauges for the target day; amounts only when known (BR-1: unknown is not 0)."""
    stamp = str(int(now.timestamp() * 1_000_000_000))
    values: dict[str, Decimal] = {"aorte.billing.known": Decimal(row["status"] != "unknown")}
    for name, key in (
        ("aorte.provider.cost_usd", "provider_usd"),
        ("aorte.billing.engine_cost_usd", "engine_usd"),
        ("aorte.billing.drift_usd", "drift_usd"),
    ):
        if row[key] is not None:
            values[name] = Decimal(row[key])
    metrics = [
        {"name": name, "gauge": {"dataPoints": [{"asDouble": float(value), "timeUnixNano": stamp}]}}
        for name, value in values.items()
    ]
    resource = [{"key": k, "value": {"stringValue": v}} for k, v in RESOURCE.items()]
    payload = {
        "resourceMetrics": [
            {
                "resource": {"attributes": resource},
                "scopeMetrics": [{"scope": {"name": "aorte.billing"}, "metrics": metrics}],
            }
        ]
    }
    return json.dumps(payload).encode()


def push(row: Mapping[str, Any], env: Mapping[str, str], now: datetime, post: Post = _post) -> None:
    endpoint = _https(env, "GRAFANA_OTLP_ENDPOINT", suffix=".grafana.net")
    token = _secret(env, "GRAFANA_OTLP_TOKEN")
    try:
        base64.b64decode(token, validate=True)
    except ValueError as error:
        raise ConfigError(
            "GRAFANA_OTLP_TOKEN must be the base64 of <instance-id>:<token>"
        ) from error
    headers = {"Authorization": f"Basic {token}", "Content-Type": "application/json"}
    post(f"{endpoint}/v1/metrics", headers, otlp_body(row, now))


# ------------------------------------------------------------------------------ CLI


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="scripts/provider_cost_reconcile.py")
    parser.add_argument("--out-dir", type=Path, required=True)
    parser.add_argument("--day", type=date.fromisoformat, help="UTC day; default yesterday")
    return parser


def main(
    argv: Sequence[str] | None = None,
    env: Mapping[str, str] | None = None,
    *,
    fetch: Fetch = _fetch_json,
    post: Post = _post,
    results: Path = RESULTS,
    now: datetime | None = None,
) -> int:
    import os

    args = _parser().parse_args(argv)
    env = os.environ if env is None else env
    now = now or datetime.now(UTC)
    target = args.day or (now.date() - timedelta(days=1))
    try:
        # Preflight: every setting is checked before any lookup (fail closed).
        for key in REQUIRED_ENV:
            _secret(env, key)
        _https(env, "GRAFANA_OTLP_ENDPOINT", suffix=".grafana.net")
        if target >= now.date():
            raise ConfigError("--day must be a finished UTC day")
        report = build_report(target, env, fetch=fetch, results=results, now=now)
    except ConfigError as error:
        print(f"billing status=config-error reason={error}", file=sys.stderr)
        return 2
    row = report["days"][target.isoformat()]
    args.out_dir.mkdir(parents=True, exist_ok=True)
    (args.out_dir / f"{target.isoformat()}.json").write_text(
        json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    status: Status = row["status"]
    try:
        push(row, env, now, post)
    except Exception as error:  # noqa: BLE001 - print the class only, never a URL or value
        print(f"billing day={target} status={status} push=failed ({type(error).__name__})")
        return 1
    print(f"billing day={target} status={status} push=ok")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
