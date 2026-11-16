"""Q12 minimal deterministic Odoo 19 ERP fixtures (spec PR-6, PR-7).

Creates six edge-case scenarios in the isolated Compose Odoo through the supported JSON-2
API only (never SQL). Re-runs are idempotent: records are found by fixed references and
reused; only the recent-cart timestamp is refreshed so that scenario stays recent.

Governance (docs/process.md): `--preflight-only` validates isolation, the read-only role,
the `website_sale_loyalty` module (F-042), and lookups, then exits with no write.
Writes also require `--confirmed-synthetic`.
"""

from __future__ import annotations

import argparse
import os
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Protocol

from scripts.gated_execution_review import _as_records, _review_product, _review_website

FIXTURE_PREFIX = "q12-fixture"
FIXTURE_DOMAIN = "example.test"


@dataclass(frozen=True)
class Scenario:
    key: str
    expected_skip: str | None  # ErpCartScanner skip reason, or None when eligible
    email: bool = True
    blacklisted: bool = False
    recent: bool = False
    carts: int = 1
    note: str = ""


# The six minimal edge cases. Consent is an Engine DB record, so `no_consent` and the
# consented scenarios differ only by the operator consent step printed at the end.
SCENARIOS: tuple[Scenario, ...] = (
    Scenario("eligible", None, note="opt-in granted; one run expected"),
    Scenario("missing-email", "missing_email", email=False),
    Scenario("blacklisted", "blacklisted", blacklisted=True, note="opt-in granted"),
    Scenario("recent", "recent", recent=True, note="opt-in granted"),
    Scenario("no-consent", "no_consent", note="no consent recorded"),
    Scenario(
        "duplicate",
        "active_run",
        carts=2,
        note="opt-in granted; two carts, second suppressed by active run/cooldown",
    ),
)
CONSENTED = frozenset({"eligible", "blacklisted", "recent", "duplicate"})


class Client(Protocol):
    def call(self, model: str, method: str, params: dict[str, object]) -> object: ...


class Reader(Protocol):
    def verify_readonly_role(self) -> None: ...

    def verify_loyalty_module_installed(self) -> None: ...


def _stage(name: str, started: float, **fields: object) -> None:
    extra = " ".join(f"{key}={value}" for key, value in fields.items())
    print(f"stage={name} elapsed_s={time.monotonic() - started:.0f} {extra}".rstrip(), flush=True)


def _one_id(value: object, label: str) -> int:
    if not isinstance(value, list) or len(value) != 1 or type(value[0]) is not int:
        raise ValueError(f"Odoo did not confirm {label}; inspect before retrying")
    return value[0]


def _find(client: Client, model: str, domain: list[object]) -> int | None:
    records = _as_records(
        client.call(
            model,
            "search_read",
            {"domain": domain, "fields": ["id"], "limit": 2, "context": {"active_test": False}},
        ),
        f"{model} fixture lookup",
    )
    if len(records) > 1:
        raise ValueError(f"fixture {model} lookup is ambiguous; inspect before retrying")
    found = records[0].get("id") if records else None
    if records and type(found) is not int:
        raise ValueError(f"fixture {model} record has no valid ID")
    return found if isinstance(found, int) else None


def run(
    client: Client,
    reader: Reader,
    *,
    preflight_only: bool,
    confirmed_synthetic: bool,
    now: Callable[[], datetime] = lambda: datetime.now(UTC),
) -> dict[str, list[int]]:
    started = time.monotonic()
    _stage("preflight", started, status="started")
    reader.verify_readonly_role()
    reader.verify_loyalty_module_installed()
    product, price, _cost, _category = _review_product(
        client.call(
            "product.product",
            "search_read",
            {
                "domain": [
                    ["name", "=", "Customizable Desk"],
                    ["sale_ok", "=", True],
                    ["list_price", ">", 0],
                    ["standard_price", ">", 0],
                ],
                "fields": ["id", "list_price", "standard_price", "categ_id"],
                "limit": 1,
            },
        )
    )
    website_id, _ = _review_website(
        client.call(
            "website",
            "search_read",
            {"domain": [], "fields": ["id", "name"], "order": "id asc", "limit": 1},
        )
    )
    _stage(
        "preflight",
        started,
        status="passed",
        checks="read-only-role,website_sale_loyalty,product,website",
        not_checked="engine-consent-state",
        writes=0,
    )
    if preflight_only:
        return {}
    if not confirmed_synthetic:
        raise ValueError("--confirmed-synthetic is required to create fixtures")
    created: dict[str, list[int]] = {}
    for index, scenario in enumerate(SCENARIOS, start=1):
        _stage("fixture", started, scenario=scenario.key, progress=f"{index}/{len(SCENARIOS)}")
        ref = f"{FIXTURE_PREFIX}-{scenario.key}"
        email = f"{ref}@{FIXTURE_DOMAIN}"
        partner = _find(client, "res.partner", [["ref", "=", ref]])
        if partner is None:
            partner = _one_id(
                client.call(
                    "res.partner",
                    "create",
                    {
                        "vals_list": [
                            {
                                "name": f"Q12 fixture {scenario.key}",
                                "ref": ref,
                                "email": email if scenario.email else False,
                                "lang": "en_US",
                            }
                        ]
                    },
                ),
                f"fixture partner {scenario.key}",
            )
        if (
            scenario.blacklisted
            and _find(client, "mail.blacklist", [["email", "=", email]]) is None
        ):
            _one_id(
                client.call("mail.blacklist", "create", {"vals_list": [{"email": email}]}),
                "fixture blacklist entry",
            )
        moment = now() if scenario.recent else now() - timedelta(days=7)
        stamp = moment.strftime("%Y-%m-%d %H:%M:%S")
        orders: list[int] = []
        for number in range(1, scenario.carts + 1):
            order_ref = f"{ref}-{number}"
            order = _find(client, "sale.order", [["client_order_ref", "=", order_ref]])
            if order is None:
                order = _one_id(
                    client.call(
                        "sale.order",
                        "create",
                        {
                            "vals_list": [
                                {
                                    "partner_id": partner,
                                    "website_id": website_id,
                                    "date_order": stamp,
                                    "client_order_ref": order_ref,
                                    "order_line": [
                                        [
                                            0,
                                            0,
                                            {
                                                "product_id": product,
                                                "product_uom_qty": 1,
                                                "price_unit": float(price),
                                            },
                                        ]
                                    ],
                                }
                            ]
                        },
                    ),
                    f"fixture order {order_ref}",
                )
            elif scenario.recent:
                client.call("sale.order", "write", {"ids": [order], "vals": {"date_order": stamp}})
            orders.append(order)
        created[scenario.key] = [partner, *orders]
    _stage("fixture", started, status="complete", scenarios=len(created))
    for key in sorted(CONSENTED & created.keys()):
        print(
            f"next: python -m engine.privacy_cli consent grant --customer-id {created[key][0]} "
            f"--channel email --source {FIXTURE_PREFIX}-{key}",
            flush=True,
        )
    return created


def main() -> None:
    from engine.full_stack_worker import WorkerSettings
    from engine.odoo import JsonTwoClient, OdooReader, OdooSettings

    parser = argparse.ArgumentParser(description="Create Q12 minimal ERP edge-case fixtures")
    parser.add_argument("--preflight-only", action="store_true")
    parser.add_argument("--confirmed-synthetic", action="store_true")
    args = parser.parse_args()
    worker = WorkerSettings.from_env(os.environ)
    settings = OdooSettings.from_env(os.environ)
    if settings.url != "http://odoo:8069" or settings.database != "odoo":
        raise ValueError("fixture creation is restricted to the isolated Compose Odoo")
    run(
        JsonTwoClient(settings),
        OdooReader(worker.odoo_url),
        preflight_only=args.preflight_only,
        confirmed_synthetic=args.confirmed_synthetic,
    )


if __name__ == "__main__":
    main()
