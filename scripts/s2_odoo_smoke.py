from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import cast

from scripts.s2_llm_eval import (
    CaseScore,
    EvaluationCase,
    EvaluationError,
    GoldenDataset,
    ModelResult,
    load_dataset,
)
from scripts.spike_s1_odoo import JsonValue, OdooApiError, OdooClient, Record

THRESHOLD_HOURS = 10
RECENT_AGE_HOURS = 1
ELIGIBLE_AGE_HOURS = 11
ACTIVITY_SUMMARY = "S2 internal evaluation smoke check"
RUN_ID_PATTERN = re.compile(r"^[a-z0-9][a-z0-9-]{0,31}$")

SMOKE_CARTS: dict[str, dict[str, object]] = {
    "cart-open-001": {
        "cart_id": "s2-cart-001",
        "age_hours": ELIGIBLE_AGE_HOURS,
    },
    "cart-recent-004": {
        "cart_id": "s2-cart-004",
        "age_hours": RECENT_AGE_HOURS,
    },
}
REQUIRED_ODOO_FIELDS = {
    "website": ("cart_abandoned_delay", "send_abandoned_cart_email", "currency_id"),
    "product.pricelist": ("currency_id",),
    "product.product": (
        "name",
        "default_code",
        "description_sale",
        "list_price",
        "sale_ok",
        "type",
    ),
    "res.partner": ("name", "email", "property_product_pricelist"),
    "sale.order": (
        "partner_id",
        "website_id",
        "pricelist_id",
        "client_order_ref",
        "date_order",
        "order_line",
        "state",
        "is_abandoned_cart",
        "cart_recovery_email_sent",
    ),
    "sale.order.line": (
        "order_id",
        "product_id",
        "product_uom_qty",
        "price_unit",
        "price_subtotal",
        "currency_id",
    ),
    "mail.activity": (
        "activity_type_id",
        "res_model_id",
        "res_id",
        "user_id",
        "summary",
        "note",
        "date_deadline",
    ),
}
WRITE_ODOO_FIELDS = {
    "product.product": (
        "name",
        "default_code",
        "description_sale",
        "list_price",
        "sale_ok",
        "type",
    ),
    "res.partner": ("name", "email", "property_product_pricelist"),
    "sale.order": (
        "partner_id",
        "website_id",
        "pricelist_id",
        "client_order_ref",
        "date_order",
        "order_line",
    ),
    "mail.activity": (
        "activity_type_id",
        "res_model_id",
        "res_id",
        "user_id",
        "summary",
        "note",
        "date_deadline",
    ),
}


def _record(value: object, context: str) -> Record:
    if not isinstance(value, dict):
        raise OdooApiError(f"{context} must be an object")
    record: Record = {}
    for key, item in cast(dict[object, object], value).items():
        if not isinstance(key, str):
            raise OdooApiError(f"{context} has a non-string key")
        record[key] = _json_value(item, context)
    return record


def _json_value(value: object, context: str) -> JsonValue:
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, list):
        return [_json_value(item, context) for item in cast(list[object], value)]
    if isinstance(value, dict):
        return _record(cast(object, value), context)
    raise OdooApiError(f"{context} contains a non-JSON value")


def _record_id(record: Record, context: str) -> int:
    value = record.get("id")
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise OdooApiError(f"{context} has no valid record ID")
    return value


def _many2one_id(value: JsonValue | None, context: str) -> int:
    if (
        not isinstance(value, list)
        or len(value) != 2
        or isinstance(value[0], bool)
        or not isinstance(value[0], int)
    ):
        raise OdooApiError(f"{context} is not a valid Odoo relation")
    return value[0]


def _many2one_name(value: JsonValue | None, context: str) -> str:
    if not isinstance(value, list) or len(value) != 2 or not isinstance(value[1], str):
        raise OdooApiError(f"{context} is not a valid Odoo relation")
    return value[1]


def _many2one_matches_name(value: JsonValue | None, expected_name: str) -> bool:
    return (
        isinstance(value, list)
        and len(value) == 2
        and isinstance(value[0], int)
        and not isinstance(value[0], bool)
        and value[1] == expected_name
    )


def _api_created_ids(value: JsonValue, expected: int, context: str) -> list[int]:
    if (
        not isinstance(value, list)
        or len(value) != expected
        or any(isinstance(item, bool) or not isinstance(item, int) or item < 1 for item in value)
    ):
        raise OdooApiError(f"Odoo did not return valid IDs for {context}")
    return cast(list[int], value)


def validate_odoo_schema(client: OdooClient) -> None:
    for model, required_fields in REQUIRED_ODOO_FIELDS.items():
        value = client.call(
            model,
            "fields_get",
            {"allfields": list(required_fields), "attributes": ["type", "readonly"]},
        )
        fields = _record(value, f"{model} field metadata")
        missing = sorted(set(required_fields) - set(fields))
        if missing:
            raise OdooApiError(f"Odoo {model} is missing required S2 fields: {', '.join(missing)}")
        for field in WRITE_ODOO_FIELDS.get(model, ()):
            metadata = _record(fields.get(field), f"{model}.{field} metadata")
            if metadata.get("readonly") is not False:
                raise OdooApiError(f"Odoo field {model}.{field} is not writable by the S2 API")


def seed_smoke_inventory(
    client: OdooClient, dataset: GoldenDataset, run_id: str
) -> dict[str, object]:
    if not RUN_ID_PATTERN.fullmatch(run_id):
        raise ValueError("run_id must use lowercase letters, digits, and hyphens")
    validate_odoo_schema(client)
    websites = client.search_read(
        "website",
        [],
        [
            "id",
            "cart_abandoned_delay",
            "send_abandoned_cart_email",
            "currency_id",
        ],
        limit=10,
    )
    website = next(
        (
            row
            for row in websites
            if row.get("cart_abandoned_delay") == THRESHOLD_HOURS
            and row.get("send_abandoned_cart_email") is False
            and _many2one_matches_name(row.get("currency_id"), "USD")
        ),
        None,
    )
    if website is None:
        raise OdooApiError(
            "The local website must have the 10-hour delay, reminders disabled, and USD currency"
        )
    if _many2one_name(website.get("currency_id"), "website currency") != "USD":
        raise OdooApiError("The S2 synthetic price fixtures require a USD website")
    website_id = _record_id(website, "website")
    pricelists = client.search_read(
        "product.pricelist",
        [],
        ["id", "currency_id"],
        limit=200,
    )
    usd_pricelist = next(
        (
            row
            for row in pricelists
            if _many2one_name(row.get("currency_id"), "pricelist currency") == "USD"
        ),
        None,
    )
    if usd_pricelist is None:
        raise OdooApiError("The local Odoo instance has no USD pricelist for S2 fixtures")
    usd_pricelist_id = _record_id(usd_pricelist, "USD pricelist")

    cart_refs = [str(smoke_case["cart_id"]) for smoke_case in SMOKE_CARTS.values()]
    existing = client.search_read(
        "sale.order",
        cast(list[JsonValue], [["client_order_ref", "in", cart_refs]]),
        ["id", "client_order_ref"],
        limit=10,
    )
    if existing:
        raise OdooApiError(
            "S2 smoke cart references already exist; use a new disposable Odoo instance"
        )

    product_values: list[dict[str, object]] = []
    product_by_fixture: dict[str, dict[str, object]] = {}
    for fixture_id, cart in SMOKE_CARTS.items():
        fixture = dataset.fixtures.get(fixture_id)
        if fixture is None or fixture.get("cart_id") != cart.get("cart_id"):
            raise EvaluationError(f"S2 smoke cart {fixture_id} does not match the dataset")
        age_value = cart.get("age_hours")
        if not isinstance(age_value, int) or isinstance(age_value, bool):
            raise EvaluationError(f"S2 smoke cart {fixture_id} has an invalid age")
        expected_state = "abandoned" if age_value > THRESHOLD_HOURS else "active"
        if fixture.get("state") != expected_state:
            raise EvaluationError(f"S2 smoke cart {fixture_id} has an invalid state label")
        items = fixture.get("items")
        if not isinstance(items, list) or len(items) != 1 or not isinstance(items[0], dict):
            raise EvaluationError(f"S2 smoke fixture {fixture_id} must have one product")
        fixture_line = cast(dict[str, JsonValue], items[0])
        product_id = fixture_line.get("product_id")
        product_name = fixture_line.get("name")
        description = fixture_line.get("description")
        unit_price = fixture_line.get("unit_price")
        currency = fixture_line.get("currency")
        if (
            not isinstance(product_id, str)
            or not isinstance(product_name, str)
            or not isinstance(description, str)
            or isinstance(unit_price, bool)
            or not isinstance(unit_price, (int, float))
            or currency != "USD"
        ):
            raise EvaluationError(f"S2 smoke fixture {fixture_id} has invalid product facts")
        product_value: dict[str, object] = {
            "name": product_name,
            "default_code": product_id,
            "description_sale": description,
            "list_price": unit_price,
            "sale_ok": True,
            "type": "consu",
        }
        product_values.append(product_value)
        product_by_fixture[fixture_id] = product_value
    product_codes = [value["default_code"] for value in product_values]
    existing_products = client.search_read(
        "product.product",
        cast(list[JsonValue], [["default_code", "in", product_codes]]),
        ["id", "default_code"],
        limit=10,
    )
    if existing_products:
        raise OdooApiError(
            "S2 synthetic product codes already exist; use a new disposable Odoo instance"
        )
    synthetic_emails = [f"s2-customer-{index:03d}@example.invalid" for index in (1, 2)]
    existing_partners = client.search_read(
        "res.partner",
        cast(list[JsonValue], [["email", "in", synthetic_emails]]),
        ["id", "email"],
        limit=10,
    )
    if existing_partners:
        raise OdooApiError(
            "S2 synthetic customer addresses already exist; use a new disposable Odoo instance"
        )
    product_result = client.call(
        "product.product",
        "create",
        {"vals_list": product_values},
    )
    product_ids = _api_created_ids(product_result, 2, "synthetic products")
    partner_result = client.call(
        "res.partner",
        "create",
        {
            "vals_list": [
                {
                    "name": f"Synthetic Customer S2 {index:03d}",
                    "email": synthetic_emails[index - 1],
                    "property_product_pricelist": usd_pricelist_id,
                }
                for index in (1, 2)
            ]
        },
    )
    partner_ids = _api_created_ids(partner_result, 2, "synthetic customers")

    now = datetime.now(timezone.utc)
    order_values: list[dict[str, object]] = []
    for index, (_, cart_manifest) in enumerate(SMOKE_CARTS.items()):
        age_value = cart_manifest.get("age_hours")
        cart_ref = cart_manifest.get("cart_id")
        if not isinstance(age_value, int) or isinstance(age_value, bool):
            raise EvaluationError("S2 cart manifest contains an invalid age")
        if not isinstance(cart_ref, str):
            raise EvaluationError("S2 cart manifest contains an invalid cart ID")
        order_date = (now - timedelta(hours=age_value)).replace(tzinfo=None)
        order_values.append(
            {
                "partner_id": partner_ids[index],
                "website_id": website_id,
                "pricelist_id": usd_pricelist_id,
                "client_order_ref": cart_ref,
                "date_order": order_date.strftime("%Y-%m-%d %H:%M:%S"),
                "order_line": [
                    [
                        0,
                        0,
                        {
                            "product_id": product_ids[index],
                            "product_uom_qty": 1.0,
                        },
                    ]
                ],
            }
        )
    order_result = client.call("sale.order", "create", {"vals_list": order_values})
    order_ids = _api_created_ids(order_result, 2, "synthetic carts")
    orders = client.search_read(
        "sale.order",
        cast(list[JsonValue], [["id", "in", order_ids]]),
        [
            "id",
            "client_order_ref",
            "date_order",
            "state",
            "website_id",
            "is_abandoned_cart",
            "cart_recovery_email_sent",
            "order_line",
        ],
        limit=2,
    )
    by_ref: dict[str, Record] = {}
    for returned_order in orders:
        returned_ref = returned_order.get("client_order_ref")
        if not isinstance(returned_ref, str):
            raise OdooApiError("Odoo cart is missing its synthetic reference")
        by_ref[returned_ref] = returned_order
    cart_rows: list[dict[str, object]] = []
    for fixture_id, expectation in SMOKE_CARTS.items():
        cart_ref = expectation.get("cart_id")
        if not isinstance(cart_ref, str):
            raise EvaluationError(f"Invalid S2 cart reference for {fixture_id}")
        matched_order = by_ref.get(cart_ref)
        if matched_order is None:
            raise OdooApiError(f"Odoo did not return seeded cart {cart_ref}")
        age_value = expectation.get("age_hours")
        if not isinstance(age_value, int) or isinstance(age_value, bool):
            raise EvaluationError(f"Invalid age label for {fixture_id}")
        is_eligible = age_value > THRESHOLD_HOURS
        if (
            matched_order.get("state") != "draft"
            or matched_order.get("is_abandoned_cart") is not is_eligible
            or matched_order.get("cart_recovery_email_sent") is not False
        ):
            raise OdooApiError(f"Odoo cart {cart_ref} failed its state/eligibility safety check")
        cart_rows.append(
            {
                "fixture_id": fixture_id,
                "cart_id": cart_ref,
                "odoo_order_id": _record_id(matched_order, f"cart {cart_ref}"),
                "age_hours_at_seed": age_value,
                "eligible_at_seed": is_eligible,
                "state": matched_order["state"],
                "product_name": product_by_fixture[fixture_id]["name"],
                "unit_price": product_by_fixture[fixture_id]["list_price"],
                "currency": "USD",
            }
        )
    return _inventory_record(run_id, website_id, cart_rows)


def _inventory_record(
    run_id: str, website_id: int, carts: list[dict[str, object]]
) -> dict[str, object]:
    manifest: dict[str, object] = {
        "inventory_version": "s2-odoo-inventory-v1",
        "run_id": run_id,
        "threshold_hours": THRESHOLD_HOURS,
        "threshold_source_fact": "F-003",
        "website_id": website_id,
        "carts": carts,
        "reminder_enabled": False,
    }
    manifest["manifest_sha256"] = _manifest_hash(manifest)
    manifest["approval"] = {"status": "pending", "reviewer_id": ""}
    return manifest


def _manifest_hash(inventory: dict[str, object]) -> str:
    manifest = {
        key: value for key, value in inventory.items() if key not in {"manifest_sha256", "approval"}
    }
    return hashlib.sha256(
        json.dumps(manifest, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode(
            "utf-8"
        )
    ).hexdigest()


def validate_inventory_approval(inventory: dict[str, object]) -> None:
    if inventory.get("manifest_sha256") != _manifest_hash(inventory):
        raise EvaluationError("Odoo inventory approval hash does not match its contents")
    approval = inventory.get("approval")
    if not isinstance(approval, dict):
        raise EvaluationError("Odoo inventory is missing its approval record")
    approval_record = _record(cast(object, approval), "Odoo inventory approval")
    if (
        inventory.get("inventory_version") != "s2-odoo-inventory-v1"
        or inventory.get("threshold_hours") != THRESHOLD_HOURS
        or inventory.get("threshold_source_fact") != "F-003"
        or inventory.get("reminder_enabled") is not False
    ):
        raise EvaluationError("Odoo inventory does not match the recorded S2 contract")
    run_id = inventory.get("run_id")
    website_id = inventory.get("website_id")
    if (
        not isinstance(run_id, str)
        or not RUN_ID_PATTERN.fullmatch(run_id)
        or isinstance(website_id, bool)
        or not isinstance(website_id, int)
        or website_id < 1
    ):
        raise EvaluationError("Odoo inventory has invalid run or website identifiers")
    carts = inventory.get("carts")
    if not isinstance(carts, list) or len(carts) != len(SMOKE_CARTS):
        raise EvaluationError("S2 Odoo inventory must contain exactly two carts")
    expected_inventory = {
        "cart-open-001": ("s2-cart-001", ELIGIBLE_AGE_HOURS, True),
        "cart-recent-004": ("s2-cart-004", RECENT_AGE_HOURS, False),
    }
    found_inventory: dict[str, tuple[str, int, bool]] = {}
    for item in cast(list[object], carts):
        cart = _record(item, "Odoo inventory cart")
        fixture_id = cart.get("fixture_id")
        cart_id = cart.get("cart_id")
        age = cart.get("age_hours_at_seed")
        eligible = cart.get("eligible_at_seed")
        if (
            not isinstance(fixture_id, str)
            or not isinstance(cart_id, str)
            or isinstance(age, bool)
            or not isinstance(age, int)
            or not isinstance(eligible, bool)
            or cart.get("state") != "draft"
        ):
            raise EvaluationError("Odoo inventory contains invalid cart labels")
        found_inventory[fixture_id] = (cart_id, age, eligible)
    if found_inventory != expected_inventory:
        raise EvaluationError("Odoo inventory does not match the two-cart S2 age controls")
    if approval_record.get("status") != "approved":
        raise EvaluationError("Odoo inventory has not been approved by its owner")
    reviewer_id = approval_record.get("reviewer_id")
    if not isinstance(reviewer_id, str) or not reviewer_id.strip():
        raise EvaluationError("Odoo inventory approval has no reviewer_id")


def load_approved_inventory(path: Path) -> dict[str, object]:
    try:
        inventory = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as error:
        raise EvaluationError(f"Odoo inventory file {path} does not exist") from error
    except OSError as error:
        raise EvaluationError(f"Could not read Odoo inventory {path}") from error
    except json.JSONDecodeError as error:
        raise EvaluationError(f"Odoo inventory {path} is not valid JSON") from error
    if not isinstance(inventory, dict):
        raise EvaluationError("Odoo inventory root must be a JSON object")
    typed_inventory = cast(dict[str, object], inventory)
    validate_inventory_approval(typed_inventory)
    return typed_inventory


class OdooSmokeTools:
    def __init__(
        self,
        client: OdooClient,
        dataset: GoldenDataset,
        inventory: dict[str, object],
    ) -> None:
        validate_inventory_approval(inventory)
        self.client = client
        self.dataset = dataset
        carts_value = inventory.get("carts")
        if not isinstance(carts_value, list):
            raise EvaluationError("Odoo inventory has no cart list")
        self.carts: dict[str, dict[str, object]] = {}
        for item in cast(list[object], carts_value):
            if not isinstance(item, dict):
                raise EvaluationError("Odoo inventory contains an invalid cart")
            cart = cast(dict[str, object], item)
            fixture_id = cart.get("fixture_id")
            if not isinstance(fixture_id, str):
                raise EvaluationError("Odoo inventory cart has no fixture ID")
            if fixture_id not in SMOKE_CARTS or fixture_id in self.carts:
                raise EvaluationError("Odoo inventory has an unknown or duplicate fixture ID")
            fixture = dataset.fixtures[fixture_id]
            fixture_items = fixture.get("items")
            if not isinstance(fixture_items, list) or len(fixture_items) != 1:
                raise EvaluationError(f"Dataset fixture {fixture_id} has invalid product facts")
            fixture_item = _record(fixture_items[0], f"dataset fixture {fixture_id} product")
            if (
                cart.get("cart_id") != fixture.get("cart_id")
                or cart.get("product_name") != fixture_item.get("name")
                or cart.get("unit_price") != fixture_item.get("unit_price")
                or cart.get("currency") != fixture_item.get("currency")
            ):
                raise EvaluationError(
                    f"Odoo inventory product facts differ from reviewed fixture {fixture_id}"
                )
            self.carts[fixture_id] = cart
        if set(self.carts) != set(SMOKE_CARTS):
            raise EvaluationError("Odoo inventory is missing an S2 smoke fixture")

    def get_cart_context(self, case: EvaluationCase, cart_id: str) -> Record:
        expected = self.carts.get(case.fixture_id)
        fixture = self.dataset.fixtures[case.fixture_id]
        if (
            expected is None
            or cart_id != expected.get("cart_id")
            or cart_id != fixture.get("cart_id")
        ):
            return {"error": "Cart not found or outside the current customer scope."}
        orders = self.client.search_read(
            "sale.order",
            [
                ["client_order_ref", "=", cart_id],
                ["website_id", "!=", False],
                ["state", "=", "draft"],
            ],
            ["id", "client_order_ref", "date_order", "is_abandoned_cart", "order_line"],
            limit=2,
        )
        if len(orders) != 1:
            raise OdooApiError(f"Odoo did not return exactly one draft cart for {cart_id}")
        order = orders[0]
        order_id = _record_id(order, f"cart {cart_id}")
        age_hours = _age_hours(order.get("date_order"))
        eligible = age_hours > THRESHOLD_HOURS
        expected_eligible = expected.get("eligible_at_seed")
        if eligible is not expected_eligible or order.get("is_abandoned_cart") is not eligible:
            raise OdooApiError(
                f"Odoo abandonment state does not match the S2 threshold for {cart_id}"
            )
        lines = self.client.search_read(
            "sale.order.line",
            cast(list[JsonValue], [["order_id", "=", order_id]]),
            ["product_id", "product_uom_qty", "price_unit", "price_subtotal", "currency_id"],
            limit=20,
        )
        expected_items = fixture.get("items")
        if not isinstance(expected_items, list) or len(lines) != len(expected_items):
            raise OdooApiError(f"Odoo cart line count differs from reviewed fixture {cart_id}")
        items: list[JsonValue] = []
        subtotal = 0.0
        currency: str | None = None
        for line in lines:
            product_value = line.get("product_id")
            product_id = _many2one_id(product_value, "sale order product")
            product_name = _many2one_name(product_value, "sale order product")
            product_rows = self.client.search_read(
                "product.product",
                cast(list[JsonValue], [["id", "=", product_id]]),
                ["id", "name", "default_code", "description_sale"],
                limit=1,
            )
            if len(product_rows) != 1:
                raise OdooApiError(f"Odoo product facts are missing for {cart_id}")
            product = product_rows[0]
            product_code = product.get("default_code")
            product_description = product.get("description_sale")
            if (
                product.get("name") != product_name
                or not isinstance(product_code, str)
                or not isinstance(product_description, str)
            ):
                raise OdooApiError(f"Odoo product facts are invalid for {cart_id}")
            quantity = line.get("product_uom_qty")
            unit_price = line.get("price_unit")
            line_subtotal = line.get("price_subtotal")
            line_currency = _many2one_name(line.get("currency_id"), "sale order currency")
            if (
                isinstance(quantity, bool)
                or not isinstance(quantity, (int, float))
                or isinstance(unit_price, bool)
                or not isinstance(unit_price, (int, float))
                or isinstance(line_subtotal, bool)
                or not isinstance(line_subtotal, (int, float))
            ):
                raise OdooApiError(f"Odoo returned invalid product facts for {cart_id}")
            subtotal += float(line_subtotal)
            currency = line_currency
            items.append(
                cast(
                    Record,
                    {
                        "product_id": product_code,
                        "name": product_name,
                        "quantity": quantity,
                        "unit_price": unit_price,
                        "currency": line_currency,
                        "description": product_description,
                    },
                )
            )
        expected_subtotal = fixture.get("subtotal")
        expected_currency = fixture.get("currency")
        if (
            items != expected_items
            or round(subtotal, 2) != expected_subtotal
            or currency != expected_currency
        ):
            raise OdooApiError(f"Odoo product/price facts differ from reviewed fixture {cart_id}")
        return {
            "cart_id": cart_id,
            "state": "abandoned" if eligible else "active",
            "age_hours": round(age_hours, 2),
            "items": items,
            "subtotal": round(subtotal, 2),
            "currency": currency,
        }

    def create_activity_if_safe(
        self,
        case: EvaluationCase,
        result: ModelResult,
        score: CaseScore,
        *,
        activity_user_id: int,
        activity_type_id: int,
    ) -> tuple[int, bool] | None:
        if case.scenario_id != "recovery" or case.fixture_id != "cart-open-001":
            return None
        if not score.passed or result.output.get("action") != "draft_message":
            raise EvaluationError("Deterministic activity gate rejected the model result")
        cart = self.carts.get(case.fixture_id)
        if cart is None or cart.get("eligible_at_seed") is not True:
            raise EvaluationError("Deterministic activity gate rejected the cart eligibility")
        cart_id = cart.get("cart_id")
        if not isinstance(cart_id, str):
            raise EvaluationError("Deterministic activity gate has no cart ID")
        orders = self.client.search_read(
            "sale.order",
            [["client_order_ref", "=", cart_id], ["state", "=", "draft"]],
            ["id", "is_abandoned_cart", "cart_recovery_email_sent", "date_order"],
            limit=2,
        )
        if len(orders) != 1:
            raise OdooApiError("Activity gate requires exactly one eligible draft cart")
        order = orders[0]
        if (
            order.get("is_abandoned_cart") is not True
            or order.get("cart_recovery_email_sent") is not False
            or _age_hours(order.get("date_order")) <= THRESHOLD_HOURS
        ):
            raise EvaluationError("Deterministic activity gate rejected stale cart state")
        order_id = _record_id(order, f"cart {cart_id}")
        model_rows = self.client.search_read(
            "ir.model", [["model", "=", "sale.order"]], ["id"], limit=1
        )
        if len(model_rows) != 1:
            raise OdooApiError("Could not resolve the sale.order model for the activity")
        model_id = _record_id(model_rows[0], "sale.order model")
        existing = self.client.search_read(
            "mail.activity",
            [
                ["res_model_id", "=", model_id],
                ["res_id", "=", order_id],
                ["summary", "=", ACTIVITY_SUMMARY],
            ],
            ["id", "res_model_id", "res_id", "summary", "user_id", "activity_type_id"],
            limit=2,
        )
        if existing:
            if len(existing) != 1:
                raise OdooApiError("Duplicate S2 smoke activities were found")
            activity = existing[0]
            if (
                activity.get("summary") != ACTIVITY_SUMMARY
                or _many2one_id(activity.get("res_model_id"), "activity model") != model_id
                or activity.get("res_id") != order_id
                or _many2one_id(activity.get("user_id"), "activity user") != activity_user_id
                or _many2one_id(activity.get("activity_type_id"), "activity type")
                != activity_type_id
            ):
                raise OdooApiError("Existing S2 activity does not match the idempotency key")
            return _record_id(activity, "existing S2 activity"), False
        result_ids = self.client.call(
            "mail.activity",
            "create",
            {
                "vals_list": [
                    {
                        "activity_type_id": activity_type_id,
                        "res_model_id": model_id,
                        "res_id": order_id,
                        "user_id": activity_user_id,
                        "summary": ACTIVITY_SUMMARY,
                        "note": "Synthetic S2 smoke check. No email or offer was sent.",
                        "date_deadline": datetime.now(timezone.utc).date().isoformat(),
                    }
                ]
            },
        )
        activity_id = _api_created_ids(result_ids, 1, "internal smoke activity")[0]
        verified = self.client.search_read(
            "mail.activity",
            [["id", "=", activity_id]],
            ["id", "res_model_id", "res_id", "summary", "user_id", "activity_type_id"],
            limit=1,
        )
        if (
            len(verified) != 1
            or verified[0].get("summary") != ACTIVITY_SUMMARY
            or verified[0].get("res_id") != order_id
            or _many2one_id(verified[0].get("res_model_id"), "activity model") != model_id
            or _many2one_id(verified[0].get("user_id"), "activity user") != activity_user_id
            or _many2one_id(verified[0].get("activity_type_id"), "activity type")
            != activity_type_id
        ):
            raise OdooApiError("Odoo did not verify the deterministic smoke activity")
        return activity_id, True

    def cleanup_activity(self, activity_id: int, cart_id: str) -> None:
        orders = self.client.search_read(
            "sale.order",
            [["client_order_ref", "=", cart_id]],
            ["id"],
            limit=2,
        )
        if len(orders) != 1:
            raise OdooApiError("Cannot verify the exact cart for activity cleanup")
        model_rows = self.client.search_read(
            "ir.model", [["model", "=", "sale.order"]], ["id"], limit=1
        )
        if len(model_rows) != 1:
            raise OdooApiError("Cannot resolve sale.order model for activity cleanup")
        order_id = _record_id(orders[0], f"cart {cart_id}")
        model_id = _record_id(model_rows[0], "sale.order model")
        activities = self.client.search_read(
            "mail.activity",
            [["id", "=", activity_id]],
            ["id", "res_model_id", "res_id", "summary"],
            limit=2,
        )
        if len(activities) != 1:
            raise OdooApiError("Cannot verify the exact S2 smoke activity for cleanup")
        activity = activities[0]
        if (
            activity.get("summary") != ACTIVITY_SUMMARY
            or activity.get("res_id") != order_id
            or _many2one_id(activity.get("res_model_id"), "activity model") != model_id
        ):
            raise OdooApiError("Refusing to remove an activity outside the S2 cleanup scope")
        self.client.call("mail.activity", "unlink", {"ids": [activity_id]})
        remaining = self.client.search_read(
            "mail.activity",
            cast(list[JsonValue], [["id", "=", activity_id]]),
            ["id"],
            limit=1,
        )
        if remaining:
            raise OdooApiError("Odoo did not remove the exact S2 smoke activity")


def _age_hours(value: JsonValue | None) -> float:
    if not isinstance(value, str):
        raise OdooApiError("Odoo cart has no valid date_order")
    order_date = datetime.strptime(value, "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc)
    return (datetime.now(timezone.utc) - order_date).total_seconds() / 3600


def write_inventory_pair(
    inventory: dict[str, object], json_path: Path, markdown_path: Path
) -> tuple[Path, Path]:
    json_path.parent.mkdir(parents=True, exist_ok=True)
    markdown_path.parent.mkdir(parents=True, exist_ok=True)
    json_content = json.dumps(inventory, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    carts = inventory.get("carts")
    rows = carts if isinstance(carts, list) else []
    lines = [
        "# S2 disposable Odoo smoke inventory",
        "",
        f"- Run ID: `{inventory.get('run_id')}`",
        f"- Threshold: {inventory.get('threshold_hours')} hours (S2-only; source F-003)",
        f"- Inventory SHA-256: `{inventory.get('manifest_sha256')}`",
        "- Reminder email: disabled",
        "- Approval: pending owner inspection",
        "",
        "| Cart ID | Fixture | Age at seed | Eligible | Product | Price | Currency |",
        "|---|---|---:|---|---|---:|---|",
    ]
    for item in rows:
        if not isinstance(item, dict):
            raise EvaluationError("Cannot render an invalid Odoo cart inventory")
        cart = cast(dict[str, object], item)
        lines.append(
            f"| `{cart.get('cart_id')}` | `{cart.get('fixture_id')}` | "
            f"{cart.get('age_hours_at_seed')}h | {cart.get('eligible_at_seed')} | "
            f"{cart.get('product_name')} | {cart.get('unit_price')} | {cart.get('currency')} |"
        )
    lines.extend(
        [
            "",
            "Set approval.status to `approved` and enter a reviewer_id only after inspection.",
            "",
        ]
    )
    markdown_content = "\n".join(lines)
    _publish_text_pair(
        json_path,
        json_content.encode("utf-8"),
        markdown_path,
        markdown_content.encode("utf-8"),
    )
    return json_path, markdown_path


def _publish_text_pair(
    first_path: Path,
    first_content: bytes,
    second_path: Path,
    second_content: bytes,
) -> None:
    import tempfile

    previous = [path.read_bytes() if path.exists() else None for path in (first_path, second_path)]
    staged: list[tuple[Path, Path]] = []
    try:
        for target, content in (
            (first_path, first_content),
            (second_path, second_content),
        ):
            with tempfile.NamedTemporaryFile(
                dir=target.parent, prefix=f".{target.name}.", delete=False
            ) as handle:
                temporary = Path(handle.name)
                handle.write(content)
            staged.append((temporary, target))
        for temporary, target in staged:
            os.replace(temporary, target)
    except OSError:
        for path, old_content in zip((first_path, second_path), previous, strict=True):
            if old_content is None:
                path.unlink(missing_ok=True)
            else:
                path.write_bytes(old_content)
        raise
    finally:
        for temporary, _ in staged:
            temporary.unlink(missing_ok=True)


def _main_seed(args: argparse.Namespace) -> int:
    if not args.execute_live:
        raise EvaluationError("Pass --execute-live to seed the explicitly approved local fixture")
    if os.environ.get("S2_DISPOSABLE_INSTANCE") != "YES":
        raise EvaluationError("Set S2_DISPOSABLE_INSTANCE=YES for a fresh local Odoo instance")
    client = OdooClient(
        base_url=os.environ.get("ODOO_URL", "http://localhost:8069"),
        database=os.environ.get("ODOO_DB_NAME", "odoo"),
        api_key=os.environ.get("ODOO_API_KEY", ""),
    )
    dataset = load_dataset()
    inventory = seed_smoke_inventory(client, dataset, args.run_id)
    write_inventory_pair(inventory, args.inventory_json, args.inventory_markdown)
    print(
        f"Created the S2 synthetic inventory; review "
        f"{args.inventory_markdown} and approve {args.inventory_json} before any model call."
    )
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="Seed the isolated S2 Odoo smoke fixture.")
    commands = parser.add_subparsers(dest="command", required=True)
    seed_parser = commands.add_parser("seed")
    seed_parser.add_argument("--execute-live", action="store_true")
    seed_parser.add_argument("--run-id", required=True)
    seed_parser.add_argument(
        "--inventory-json", type=Path, default=Path("evals/llm/s2_odoo_inventory.json")
    )
    seed_parser.add_argument(
        "--inventory-markdown",
        type=Path,
        default=Path("evals/llm/s2_odoo_inventory.md"),
    )
    args = parser.parse_args()
    try:
        if args.command == "seed":
            return _main_seed(args)
    except (OdooApiError, EvaluationError, ValueError) as error:
        print(f"S2 Odoo smoke failed: {error}", file=sys.stderr)
        return 1
    raise EvaluationError(f"Unsupported command {args.command!r}")


if __name__ == "__main__":
    raise SystemExit(main())
