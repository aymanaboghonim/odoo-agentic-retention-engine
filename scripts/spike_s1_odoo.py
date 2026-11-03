from __future__ import annotations

import argparse
import json
import os
import sys
import threading
import time
from collections.abc import Callable, Iterator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from ipaddress import ip_address
from typing import TextIO, TypeAlias
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlsplit, urlunsplit
from urllib.request import Request, urlopen

JsonValue: TypeAlias = str | int | float | bool | None | list["JsonValue"] | dict[str, "JsonValue"]
Record: TypeAlias = dict[str, JsonValue]


class OdooApiError(RuntimeError):
    pass


class PreflightError(RuntimeError):
    pass


HEARTBEAT_SECONDS = 30.0
READY_POLL_SECONDS = 5.0
READY_CHECK_TIMEOUT_SECONDS = 5.0


def _json_value(value: object) -> JsonValue:
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, list):
        return [_json_value(item) for item in value]
    if isinstance(value, dict):
        if not all(isinstance(key, str) for key in value):
            raise OdooApiError("Odoo returned a JSON object with a non-string key")
        return {key: _json_value(item) for key, item in value.items()}
    raise OdooApiError("Odoo returned a value that is not valid JSON")


def validate_local_url(url: str) -> str:
    parts = urlsplit(url)
    try:
        parts.port
    except ValueError as error:
        raise ValueError("ODOO_URL contains an invalid port") from error
    if (
        parts.scheme not in {"http", "https"}
        or not parts.hostname
        or parts.username
        or parts.password
        or parts.path not in {"", "/"}
        or parts.query
        or parts.fragment
    ):
        raise ValueError("ODOO_URL must be a plain HTTP(S) loopback URL")

    host = parts.hostname.lower()
    if host != "localhost":
        try:
            if not ip_address(host).is_loopback:
                raise ValueError("ODOO_URL must point to a loopback address")
        except ValueError as error:
            raise ValueError("ODOO_URL must point to localhost or a loopback IP") from error

    return urlunsplit((parts.scheme, parts.netloc, "", "", "")).rstrip("/")


@dataclass(frozen=True)
class OdooClient:
    base_url: str
    database: str
    api_key: str
    timeout: int = 20

    def __post_init__(self) -> None:
        object.__setattr__(self, "base_url", validate_local_url(self.base_url))
        if not self.database:
            raise ValueError("ODOO_DB_NAME must not be empty")
        if not self.api_key:
            raise ValueError("Set ODOO_API_KEY to a local Odoo API key")

    def call(self, model: str, method: str, params: dict[str, object]) -> JsonValue:
        url = f"{self.base_url}/json/2/{quote(model, safe='')}/{quote(method, safe='')}"
        request = Request(
            url,
            data=json.dumps(params).encode("utf-8"),
            headers={
                "Authorization": f"bearer {self.api_key}",
                "Content-Type": "application/json; charset=utf-8",
                "X-Odoo-Database": self.database,
                "User-Agent": "odoo-agentic-retention-engine-s1",
            },
            method="POST",
        )
        try:
            with urlopen(request, timeout=self.timeout) as response:
                return _json_value(json.loads(response.read()))
        except HTTPError as error:
            raise OdooApiError(
                f"Odoo API call {model}.{method} failed with HTTP {error.code}"
            ) from error
        except URLError as error:
            raise OdooApiError(f"Could not connect to local Odoo: {error.reason}") from error
        except TimeoutError as error:
            raise OdooApiError("Timed out connecting to local Odoo") from error
        except json.JSONDecodeError as error:
            raise OdooApiError("Odoo returned an invalid JSON response") from error

    def search_count(self, model: str, domain: list[JsonValue]) -> int:
        result = self.call(model, "search_count", {"domain": domain})
        if not isinstance(result, int):
            raise OdooApiError(f"Odoo returned an invalid count for {model}")
        return result

    def search_read(
        self, model: str, domain: list[JsonValue], fields: list[str], limit: int = 100
    ) -> list[dict[str, JsonValue]]:
        result = self.call(
            model,
            "search_read",
            {"domain": domain, "fields": fields, "limit": limit},
        )
        if not isinstance(result, list):
            raise OdooApiError(f"Odoo returned invalid records for {model}")
        records: list[dict[str, JsonValue]] = []
        for row in result:
            if not isinstance(row, dict):
                raise OdooApiError(f"Odoo returned invalid records for {model}")
            records.append(row)
        return records


def _record_id(record: dict[str, JsonValue]) -> int:
    value = record.get("id")
    if not isinstance(value, int):
        raise OdooApiError("Odoo returned a record without a numeric ID")
    return value


def _age_hours(date_value: JsonValue | None) -> float | None:
    if not isinstance(date_value, str):
        return None
    order_date = datetime.strptime(date_value, "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc)
    return (datetime.now(timezone.utc) - order_date).total_seconds() / 3600


def inspect_odoo(
    client: OdooClient, on_step: Callable[[int, int], None] | None = None
) -> tuple[list[Record], list[Record]]:
    total_steps = 5

    def step(completed: int) -> None:
        if on_step:
            on_step(completed, total_steps)

    websites = client.search_read(
        "website",
        [],
        [
            "id",
            "name",
            "cart_abandoned_delay",
            "send_abandoned_cart_email",
            "send_abandoned_cart_email_activation_time",
            "cart_recovery_mail_template_id",
        ],
        limit=20,
    )
    step(1)
    carts = client.search_read(
        "sale.order",
        [["website_id", "!=", False], ["state", "=", "draft"]],
        ["id", "date_order", "website_id", "is_abandoned_cart", "cart_recovery_email_sent"],
        limit=200,
    )
    step(2)
    crons = client.search_read(
        "ir.cron",
        [["name", "ilike", "abandoned cart"]],
        ["id", "name", "active", "interval_number", "interval_type"],
        limit=10,
    )
    step(3)
    product_count = client.search_count("product.template", [])
    step(4)
    customer_count = client.search_count("res.partner", [["customer_rank", ">", 0]])
    step(5)

    print(f"Products: {product_count}")
    print(f"Customer partners: {customer_count}")
    for website in websites:
        print(
            "Website "
            f"{_record_id(website)}: "
            f"abandoned_delay_hours={website.get('cart_abandoned_delay')}, "
            f"reminder_enabled={website.get('send_abandoned_cart_email')}, "
            f"recovery_template={website.get('cart_recovery_mail_template_id')}"
        )
    for cron in crons:
        print(
            f"Reminder cron {_record_id(cron)}: "
            f"active={cron.get('active')}, "
            f"interval={cron.get('interval_number')} {cron.get('interval_type')}"
        )
    for cart in carts:
        age = _age_hours(cart.get("date_order"))
        age_summary = "unknown" if age is None else f"{age:.1f}"
        print(
            f"Website draft cart {_record_id(cart)}: age_hours={age_summary}, "
            f"eligible={cart.get('is_abandoned_cart')}, "
            f"reminder_sent={cart.get('cart_recovery_email_sent')}"
        )
    if not any(cart.get("is_abandoned_cart") is True for cart in carts):
        print("No eligible aged abandoned cart found; use --seed-aged-cart if needed.")
    return websites, carts


def seed_aged_cart(client: OdooClient, websites: list[dict[str, JsonValue]]) -> int:
    if not websites:
        raise OdooApiError("Cannot seed a cart because Odoo has no website")

    website = websites[0]
    website_id = _record_id(website)
    delay = website.get("cart_abandoned_delay")
    delay_hours = delay if isinstance(delay, (int, float)) else 10.0
    age_hours = max(12, int(delay_hours) + 2)
    order_date = (
        datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(hours=age_hours)
    ).strftime("%Y-%m-%d %H:%M:%S")

    products = client.search_read(
        "product.product",
        [["sale_ok", "=", True]],
        ["id"],
        limit=1,
    )
    customers = client.search_read(
        "res.partner",
        [["customer_rank", ">", 0], ["email", "!=", False]],
        ["id"],
        limit=1,
    )
    if not products or not customers:
        raise OdooApiError("Demo data needs at least one saleable product and emailed customer")
    product_id = _record_id(products[0])
    customer_id = _record_id(customers[0])

    reference = "S1-SYNTHETIC-CART-001"
    existing = client.search_read(
        "sale.order",
        [["client_order_ref", "=", reference]],
        ["id", "state"],
        limit=1,
    )
    if existing:
        order = existing[0]
        if order.get("state") != "draft":
            raise OdooApiError(f"Fixture {reference} exists but is not a draft cart")
        order_id = _record_id(order)
        client.call(
            "sale.order",
            "write",
            {"ids": [order_id], "vals": {"date_order": order_date}},
        )
    else:
        result = client.call(
            "sale.order",
            "create",
            {
                "vals_list": [
                    {
                        "partner_id": customer_id,
                        "website_id": website_id,
                        "client_order_ref": reference,
                        "date_order": order_date,
                        "order_line": [[0, 0, {"product_id": product_id, "product_uom_qty": 1.0}]],
                    }
                ]
            },
        )
        if not isinstance(result, list) or not result or not isinstance(result[0], int):
            raise OdooApiError("Odoo did not return the created synthetic cart ID")
        order_id = result[0]

    verified = client.search_read(
        "sale.order",
        [["id", "=", order_id]],
        ["id", "state", "is_abandoned_cart", "client_order_ref"],
        limit=1,
    )
    if (
        not verified
        or verified[0].get("state") != "draft"
        or verified[0].get("is_abandoned_cart") is not True
        or verified[0].get("client_order_ref") != reference
    ):
        raise OdooApiError("The synthetic cart was not verified as an eligible abandoned cart")
    print(f"Verified synthetic aged abandoned cart ID {order_id}; age_hours={age_hours}.")
    return order_id


class Progress:
    def __init__(
        self,
        stream: TextIO | None = None,
        heartbeat_seconds: float = HEARTBEAT_SECONDS,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._stream = stream
        self._heartbeat_seconds = heartbeat_seconds
        self._clock = clock
        self.failed_stage: str | None = None

    def emit(self, stage: str, status: str, started: float, extra: str = "") -> None:
        elapsed = int(self._clock() - started)
        suffix = f" {extra}" if extra else ""
        print(
            f"progress stage={stage} status={status}{suffix} elapsed_seconds={elapsed}",
            file=self._stream or sys.stderr,
            flush=True,
        )

    @contextmanager
    def stage(self, name: str) -> Iterator[Callable[[int, int], None]]:
        started = self._clock()
        stop = threading.Event()
        self.emit(name, "started", started)

        def beat() -> None:
            while not stop.wait(self._heartbeat_seconds):
                self.emit(name, "heartbeat", started)

        def step(completed: int, total: int) -> None:
            self.emit(name, "progress", started, f"completed={completed} total={total}")

        thread = threading.Thread(target=beat, daemon=True)
        thread.start()
        try:
            yield step
        except BaseException:
            stop.set()
            thread.join()
            if self.failed_stage is None:
                self.failed_stage = name
            self.emit(name, "failed", started)
            raise
        stop.set()
        thread.join()
        self.emit(name, "completed", started)


def build_client(env: Mapping[str, str]) -> OdooClient:
    return OdooClient(
        base_url=env.get("ODOO_URL", "http://localhost:8069"),
        database=env.get("ODOO_DB_NAME", "odoo"),
        api_key=env.get("ODOO_API_KEY", ""),
    )


def check_login_page(base_url: str, timeout: float = READY_CHECK_TIMEOUT_SECONDS) -> None:
    request = Request(
        f"{base_url}/web/login", headers={"User-Agent": "odoo-agentic-retention-engine-s1"}
    )
    try:
        with urlopen(request, timeout=timeout):
            return
    except HTTPError as error:
        raise PreflightError(f"Odoo login page returned HTTP {error.code}") from error
    except OSError as error:
        raise PreflightError(
            f"Odoo did not answer at {base_url}; is the local stack running? ({error})"
        ) from error


def wait_until_ready(
    base_url: str,
    wait_seconds: float,
    check: Callable[[str], None] | None = None,
    sleep: Callable[[float], None] = time.sleep,
    clock: Callable[[], float] = time.monotonic,
) -> None:
    check = check or check_login_page
    deadline = clock() + wait_seconds
    while True:
        try:
            check(base_url)
            return
        except PreflightError as error:
            if clock() >= deadline:
                bound = f" within {wait_seconds:g} seconds" if wait_seconds > 0 else ""
                raise PreflightError(f"Odoo was not ready{bound}: {error}") from error
            sleep(READY_POLL_SECONDS)


def run_preflight(client: OdooClient, wait_seconds: float, progress: Progress) -> None:
    with progress.stage("preflight-readiness"):
        wait_until_ready(client.base_url, wait_seconds)
    with progress.stage("preflight-api"):
        client.search_count("website", [])


PREFLIGHT_LIMITS = (
    "Preflight passed: configuration is valid, the login page answers, and the API key "
    "can read the named database once. It does not verify that module installation is "
    "complete, that demo data exists, that a reminder is enabled, or that the seed will "
    "succeed. The probe and seed write no files."
)


def main(
    argv: list[str] | None = None,
    env: Mapping[str, str] | None = None,
    progress: Progress | None = None,
) -> int:
    parser = argparse.ArgumentParser(description="Inspect the isolated local Odoo S1 baseline.")
    parser.add_argument(
        "--seed-aged-cart",
        action="store_true",
        help="Create or re-age the deterministic synthetic cart through the local Odoo API.",
    )
    parser.add_argument(
        "--preflight-only",
        action="store_true",
        help="Check configuration, readiness, and API access, then stop without probing.",
    )
    parser.add_argument(
        "--wait-ready",
        type=float,
        default=0.0,
        metavar="SECONDS",
        help="Wait up to this long for Odoo to answer before failing (default: 0).",
    )
    args = parser.parse_args(argv)
    if args.wait_ready < 0:
        parser.error("--wait-ready must not be negative")
    if args.preflight_only and args.seed_aged_cart:
        parser.error("--preflight-only cannot be combined with --seed-aged-cart")

    progress = progress or Progress()
    try:
        with progress.stage("preflight-configuration"):
            client = build_client(os.environ if env is None else env)
        run_preflight(client, args.wait_ready, progress)
        if args.preflight_only:
            print(PREFLIGHT_LIMITS)
            return 0
        with progress.stage("probe") as step:
            websites, _ = inspect_odoo(client, step)
        if args.seed_aged_cart:
            with progress.stage("seed"):
                seed_aged_cart(client, websites)
    except (OdooApiError, PreflightError, ValueError) as error:
        stage = progress.failed_stage or "unknown"
        print(f"S1 Odoo check failed at stage {stage}: {error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
