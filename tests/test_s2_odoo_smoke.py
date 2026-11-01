from __future__ import annotations

from typing import cast

import pytest

from scripts.s2_llm_eval import (
    EvaluationError,
    GoldenDataset,
    ModelResult,
    ToolCall,
    Usage,
    load_dataset,
    score_case,
)
from scripts.s2_odoo_smoke import (
    ACTIVITY_SUMMARY,
    SMOKE_CARTS,
    OdooSmokeTools,
    seed_smoke_inventory,
    validate_inventory_approval,
)
from scripts.spike_s1_odoo import JsonValue, OdooApiError, OdooClient, Record


class FakeOdooClient(OdooClient):
    def __init__(self) -> None:
        super().__init__("http://localhost:8069", "odoo", "not-used-test-value")
        self.websites: list[Record] = [
            {
                "id": 1,
                "cart_abandoned_delay": 10,
                "send_abandoned_cart_email": False,
                "currency_id": [1, "USD"],
            }
        ]
        self.products: dict[int, Record] = {}
        self.partners: list[Record] = []
        self.orders: list[Record] = []
        self.lines: list[Record] = []
        self.activities: dict[int, Record] = {}
        self.next_product_id = 101
        self.next_order_id = 301
        self.next_line_id = 401
        self.next_activity_id = 501
        self.create_models: list[str] = []
        self.missing_fields: dict[str, set[str]] = {}

    def call(self, model: str, method: str, params: dict[str, object]) -> JsonValue:
        if method == "fields_get":
            raw_fields = params.get("allfields")
            if not isinstance(raw_fields, list):
                raise AssertionError("fields_get expects allfields")
            missing = self.missing_fields.get(model, set())
            return {
                field: {"type": "char", "readonly": False}
                for field in raw_fields
                if isinstance(field, str) and field not in missing
            }
        if method == "create" and model in {"product.product", "res.partner", "sale.order"}:
            self.create_models.append(model)
            rows = params.get("vals_list")
            if not isinstance(rows, list):
                raise AssertionError("create expects vals_list")
            values = [cast(dict[str, object], row) for row in rows]
            if model == "product.product":
                return cast(JsonValue, self._create_products(values))
            if model == "res.partner":
                return cast(JsonValue, self._create_partners(values))
            return cast(JsonValue, self._create_orders(values))
        if model == "mail.activity" and method == "create":
            activity_rows = params.get("vals_list")
            if not isinstance(activity_rows, list) or len(activity_rows) != 1:
                raise AssertionError("activity create expects one value")
            activity_values = cast(dict[str, object], activity_rows[0])
            activity_id = self.next_activity_id
            self.next_activity_id += 1
            self.activities[activity_id] = {
                "id": activity_id,
                "activity_type_id": [
                    self._json_value(activity_values["activity_type_id"]),
                    "To Do",
                ],
                "res_model_id": [
                    self._json_value(activity_values["res_model_id"]),
                    "sale.order",
                ],
                "res_id": self._json_value(activity_values["res_id"]),
                "user_id": [self._json_value(activity_values["user_id"]), "Smoke User"],
                "summary": self._json_value(activity_values["summary"]),
                "note": self._json_value(activity_values["note"]),
                "date_deadline": self._json_value(activity_values["date_deadline"]),
            }
            return cast(JsonValue, [activity_id])
        if model == "mail.activity" and method == "unlink":
            ids = params.get("ids")
            if not isinstance(ids, list):
                raise AssertionError("unlink expects IDs")
            for activity_id in ids:
                if isinstance(activity_id, int):
                    self.activities.pop(activity_id, None)
            return True
        raise AssertionError(f"Unexpected Odoo call {model}.{method}")

    def search_read(
        self,
        model: str,
        domain: list[JsonValue],
        fields: list[str],
        limit: int = 100,
    ) -> list[Record]:
        del fields
        if model == "website":
            records = self.websites
        elif model == "sale.order":
            records = self.orders
        elif model == "sale.order.line":
            records = self.lines
        elif model == "product.product":
            records = list(self.products.values())
        elif model == "product.pricelist":
            records = [{"id": 9, "currency_id": [1, "USD"]}]
        elif model == "res.partner":
            records = self.partners
        elif model == "ir.model":
            records = [{"id": 50, "model": "sale.order"}]
        elif model == "mail.activity":
            records = list(self.activities.values())
        else:
            raise AssertionError(f"Unexpected Odoo model {model}")
        return [record.copy() for record in records if self._matches(record, domain)][:limit]

    def _create_products(self, values: list[dict[str, object]]) -> list[int]:
        ids = []
        for value in values:
            product_id = self.next_product_id
            self.next_product_id += 1
            self.products[product_id] = {
                "id": product_id,
                "name": self._json_value(value["name"]),
                "default_code": self._json_value(value["default_code"]),
                "description_sale": self._json_value(value["description_sale"]),
                "list_price": self._json_value(value["list_price"]),
                "sale_ok": self._json_value(value["sale_ok"]),
            }
            ids.append(product_id)
        return ids

    def _create_partners(self, values: list[dict[str, object]]) -> list[int]:
        ids = []
        for index, value in enumerate(values):
            partner_id = 200 + index
            self.partners.append(
                {
                    "id": partner_id,
                    "name": self._json_value(value["name"]),
                    "email": self._json_value(value["email"]),
                    "property_product_pricelist": self._json_value(
                        value["property_product_pricelist"]
                    ),
                }
            )
            ids.append(partner_id)
        return ids

    def _create_orders(self, values: list[dict[str, object]]) -> list[int]:
        ids = []
        for index, value in enumerate(values):
            order_id = self.next_order_id
            self.next_order_id += 1
            product_id = 101 + index
            product = self.products[product_id]
            product_name = product["name"]
            unit_price = product["list_price"]
            date_order = value["date_order"]
            if not isinstance(date_order, str):
                raise AssertionError("date_order must be a string")
            age = SMOKE_CARTS["cart-open-001" if index == 0 else "cart-recent-004"]["age_hours"]
            if not isinstance(age, int) or isinstance(age, bool):
                raise AssertionError("fixture age must be an integer")
            self.orders.append(
                {
                    "id": order_id,
                    "partner_id": self._json_value(value["partner_id"]),
                    "website_id": [1, "Main Website"],
                    "client_order_ref": self._json_value(value["client_order_ref"]),
                    "date_order": date_order,
                    "state": "draft",
                    "is_abandoned_cart": age > 10,
                    "cart_recovery_email_sent": False,
                    "order_line": [self.next_line_id],
                }
            )
            if not isinstance(unit_price, (int, float)):
                raise AssertionError("product price must be numeric")
            self.lines.append(
                {
                    "id": self.next_line_id,
                    "order_id": order_id,
                    "product_id": [product_id, product_name],
                    "product_uom_qty": 1.0,
                    "price_unit": unit_price,
                    "price_subtotal": unit_price,
                    "currency_id": [1, "USD"],
                }
            )
            self.next_line_id += 1
            ids.append(order_id)
        return ids

    @staticmethod
    def _json_value(value: object) -> JsonValue:
        if value is None or isinstance(value, (str, int, float, bool)):
            return value
        if isinstance(value, list):
            return [FakeOdooClient._json_value(item) for item in cast(list[object], value)]
        if isinstance(value, dict):
            record: Record = {}
            for key, item in cast(dict[object, object], value).items():
                if not isinstance(key, str):
                    raise AssertionError("JSON object keys must be strings")
                record[key] = FakeOdooClient._json_value(item)
            return record
        raise AssertionError("Odoo fake received a non-JSON value")

    @staticmethod
    def _matches(record: Record, domain: list[JsonValue]) -> bool:
        for clause in domain:
            if not isinstance(clause, list) or len(clause) != 3:
                continue
            field, operator, expected = clause
            if not isinstance(field, str) or not isinstance(operator, str):
                return False
            actual = record.get(field)
            if isinstance(actual, list) and actual:
                actual = actual[0]
            if operator == "=" and actual != expected:
                return False
            if operator == "!=" and actual == expected:
                return False
            if operator == "in" and (not isinstance(expected, list) or actual not in expected):
                return False
        return True


def _approved_smoke() -> tuple[FakeOdooClient, GoldenDataset, dict[str, object]]:
    client = FakeOdooClient()
    dataset = load_dataset()
    inventory = seed_smoke_inventory(client, dataset, "smoke-test")
    inventory["approval"] = {"status": "approved", "reviewer_id": "owner-test"}
    return client, dataset, inventory


def test_s2_odoo_fixture_is_api_seeded_and_isolated() -> None:
    client = FakeOdooClient()
    inventory = seed_smoke_inventory(client, load_dataset(), "smoke-test")

    assert [
        row["age_hours_at_seed"] for row in cast(list[dict[str, object]], inventory["carts"])
    ] == [
        11,
        1,
    ]
    assert [
        row["eligible_at_seed"] for row in cast(list[dict[str, object]], inventory["carts"])
    ] == [
        True,
        False,
    ]
    assert client.create_models == ["product.product", "res.partner", "sale.order"]
    assert inventory["threshold_hours"] == 10
    assert inventory["reminder_enabled"] is False
    assert inventory["approval"] == {"status": "pending", "reviewer_id": ""}


def test_s2_odoo_seed_rejects_non_usd_and_existing_cart_references() -> None:
    client = FakeOdooClient()
    client.websites[0]["currency_id"] = [2, "EUR"]
    with pytest.raises(OdooApiError, match="USD currency"):
        seed_smoke_inventory(client, load_dataset(), "smoke-test")
    assert not client.create_models

    client = FakeOdooClient()
    client.orders.append({"id": 1, "client_order_ref": "s2-cart-001"})
    with pytest.raises(OdooApiError, match="already exist"):
        seed_smoke_inventory(client, load_dataset(), "smoke-test")
    assert not client.create_models


def test_s2_odoo_seed_checks_model_fields_before_creating_records() -> None:
    client = FakeOdooClient()
    client.missing_fields["res.partner"] = {"property_product_pricelist"}

    with pytest.raises(OdooApiError, match="property_product_pricelist"):
        seed_smoke_inventory(client, load_dataset(), "smoke-test")

    assert not client.create_models


def test_s2_odoo_inventory_requires_unchanged_reviewed_labels() -> None:
    client = FakeOdooClient()
    pending = seed_smoke_inventory(client, load_dataset(), "smoke-test")
    with pytest.raises(EvaluationError, match="has not been approved"):
        validate_inventory_approval(pending)

    _, _, inventory = _approved_smoke()
    validate_inventory_approval(inventory)

    inventory["threshold_hours"] = 12
    with pytest.raises(EvaluationError, match="hash does not match"):
        validate_inventory_approval(inventory)


def test_s2_odoo_smoke_reads_live_cart_facts_matching_fixture() -> None:
    client, dataset, inventory = _approved_smoke()
    tools = OdooSmokeTools(client, dataset, inventory)
    case = next(
        item
        for item in dataset.cases
        if item.fixture_id == "cart-open-001" and item.requires_cart_tool and item.language == "en"
    )

    context = tools.get_cart_context(case, "s2-cart-001")

    assert context["state"] == "abandoned"
    assert context["subtotal"] == 80.0
    assert context["currency"] == "USD"
    assert context["items"] == dataset.fixtures["cart-open-001"]["items"]


def test_s2_activity_write_requires_deterministic_gate_and_is_idempotent() -> None:
    client, dataset, inventory = _approved_smoke()
    tools = OdooSmokeTools(client, dataset, inventory)
    case = next(
        item
        for item in dataset.cases
        if item.fixture_id == "cart-open-001"
        and item.scenario_id == "recovery"
        and item.requires_cart_tool
        and item.language == "en"
    )
    result = ModelResult(
        case_id=case.case_id,
        output={
            "action": "draft_message",
            "customer_message": "Your cart is waiting.",
            "price_claim": None,
            "currency": None,
        },
        tool_calls=(ToolCall("get_cart_context", {"cart_id": "s2-cart-001"}),),
        usage=Usage(100, 30),
    )
    score = score_case(dataset, case, result)
    assert score.passed

    first = tools.create_activity_if_safe(
        case, result, score, activity_user_id=7, activity_type_id=8
    )
    second = tools.create_activity_if_safe(
        case, result, score, activity_user_id=7, activity_type_id=8
    )
    assert first is not None and second is not None
    first_id, created = first
    second_id, created_again = second
    assert first_id == second_id
    assert created is True
    assert created_again is False
    assert len(client.activities) == 1
    assert next(iter(client.activities.values()))["summary"] == ACTIVITY_SUMMARY

    assert first_id is not None
    tools.cleanup_activity(first_id, "s2-cart-001")
    assert not client.activities


def test_s2_activity_cleanup_refuses_an_unrelated_activity() -> None:
    client, dataset, inventory = _approved_smoke()
    tools = OdooSmokeTools(client, dataset, inventory)
    case = next(
        item
        for item in dataset.cases
        if item.fixture_id == "cart-open-001"
        and item.scenario_id == "recovery"
        and item.requires_cart_tool
        and item.language == "en"
    )
    result = ModelResult(
        case_id=case.case_id,
        output={
            "action": "draft_message",
            "customer_message": "Your cart is waiting.",
            "price_claim": None,
            "currency": None,
        },
        tool_calls=(ToolCall("get_cart_context", {"cart_id": "s2-cart-001"}),),
        usage=Usage(100, 30),
    )
    score = score_case(dataset, case, result)
    outcome = tools.create_activity_if_safe(
        case, result, score, activity_user_id=7, activity_type_id=8
    )
    assert outcome is not None
    activity_id, created = outcome
    assert created is True
    client.activities[activity_id]["summary"] = "unrelated activity"

    with pytest.raises(OdooApiError, match="outside the S2 cleanup scope"):
        tools.cleanup_activity(activity_id, "s2-cart-001")

    assert activity_id in client.activities
