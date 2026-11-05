from __future__ import annotations

import os
import uuid
from collections.abc import Iterator
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from typing import Any, cast

import pytest

from engine.gate_ledger import InMemoryGateLedger
from engine.odoo import (
    JsonTwoClient,
    OdooCouponCreator,
    OdooReader,
    OdooSettings,
    cart_to_proposal_inputs,
)
from engine.policy import Gate, GateConfig, OfferProposal

REQUIRED = ("ODOO_URL", "ODOO_DB_NAME", "ODOO_API_KEY", "ODOO_READONLY_DATABASE_URL")
ENABLED = os.environ.get("ODOO_INTEGRATION") == "YES" and all(os.environ.get(k) for k in REQUIRED)

pytestmark = pytest.mark.skipif(
    not ENABLED,
    reason="Set ODOO_INTEGRATION=YES and the Odoo settings for a disposable local instance.",
)


@pytest.fixture
def world() -> Iterator[dict[str, Any]]:
    settings = OdooSettings.from_env(dict(os.environ))
    client = JsonTwoClient(settings)
    tag = uuid.uuid4().hex[:8]
    created: dict[str, Any] = {"tag": tag, "settings": settings, "client": client}
    product = cast(
        list[dict[str, Any]],
        client.call(
            "product.product",
            "search_read",
            {"domain": [["sale_ok", "=", True]], "fields": ["id", "list_price"], "limit": 1},
        ),
    )[0]
    partner = cast(
        list[int],
        client.call(
            "res.partner",
            "create",
            {"vals_list": [{"name": f"RET-IT {tag}", "email": f"ret-it-{tag}@example.com"}]},
        ),
    )[0]
    created["partner_id"] = partner
    old = (datetime.now(UTC) - timedelta(hours=30)).strftime("%Y-%m-%d %H:%M:%S")
    order = cast(
        list[int],
        client.call(
            "sale.order",
            "create",
            {
                "vals_list": [
                    {
                        "partner_id": partner,
                        "website_id": 1,
                        "date_order": old,
                        "client_order_ref": f"ret-it-{tag}",
                        "order_line": [
                            [
                                0,
                                0,
                                {
                                    "product_id": product["id"],
                                    "product_uom_qty": 2,
                                    "price_unit": 80.0,
                                },
                            ]
                        ],
                    }
                ]
            },
        ),
    )[0]
    created["order_id"] = order
    try:
        yield created
    finally:
        for activity in created.get("activities", []):
            client.delete_activity(activity)
        client.call("sale.order", "unlink", {"ids": [order]})
        client.call("res.partner", "unlink", {"ids": [partner]})
        if "coupon" in created:
            client.archive_coupon(created["coupon"])


def test_scan_finds_eligible_cart_and_skips_recent(world: dict[str, Any]) -> None:
    reader = OdooReader(world["settings"].readonly_database_url)
    now = datetime.now(UTC)
    carts = {c.order_id: c for c in reader.scan_abandoned_carts(now=now, limit=500)}
    cart = carts[world["order_id"]]
    assert cart.partner_id == world["partner_id"]
    assert cart.partner_email.startswith("ret-it-")
    assert cart.lines[0].unit_price == Decimal("80.00")
    assert cart.lines[0].quantity == Decimal("2.00")
    assert cart.age_hours(now) > 29
    total, _cost = cart_to_proposal_inputs(cart)
    assert total == Decimal("160.00")
    early = datetime.now(UTC) - timedelta(hours=48)
    assert world["order_id"] not in {
        c.order_id for c in reader.scan_abandoned_carts(now=early, limit=500)
    }


def test_read_only_role_cannot_write(world: dict[str, Any]) -> None:
    import psycopg

    with (
        psycopg.connect(world["settings"].readonly_database_url) as conn,
        pytest.raises(psycopg.errors.InsufficientPrivilege),
    ):
        conn.execute("UPDATE res_partner SET name = 'x' WHERE id = %s", (world["partner_id"],))


def test_activity_and_coupon_created_through_json2(world: dict[str, Any]) -> None:
    client: JsonTwoClient = world["client"]
    activity = client.create_activity(
        order_id=world["order_id"],
        summary="Retention offer for review",
        note="Integration test",
        deadline=date.today() + timedelta(days=2),
    )
    world["activities"] = [activity]
    rows = cast(
        list[dict[str, Any]],
        client.call(
            "mail.activity",
            "search_read",
            {"domain": [["id", "=", activity]], "fields": ["res_id", "summary"]},
        ),
    )
    assert rows[0]["res_id"] == world["order_id"]

    cart = OdooReader(world["settings"].readonly_database_url).get_cart(world["order_id"])
    assert cart is not None
    total, cost = cart_to_proposal_inputs(cart)
    proposal = OfferProposal(
        run_id=f"it-{world['tag']}",
        customer_id=str(cart.partner_id),
        cart_id=str(cart.order_id),
        cart_owner_id=str(cart.partner_id),
        category="decor",
        currency=cart.currency,
        eligible_total=total,
        eligible_unit_cost_total=cost if cost is not None else Decimal(0),
        proposed_discount_percent=Decimal("5"),
    )
    gate = Gate(
        GateConfig.from_env(
            {
                "GATE_TIMEZONE": "Asia/Riyadh",
                "GATE_CURRENCY": cart.currency,
                "GATE_DAILY_BUDGET": "1000",
                "GATE_CATEGORY_MIN_MARGINS": '{"decor": "0"}',
            }
        ),
        InMemoryGateLedger(),
    )
    approval = gate.issue_approval(proposal, now=datetime.now(UTC), human_approved=True)
    code = OdooCouponCreator(client, world["settings"]).create(approval, proposal)
    cards = cast(
        list[dict[str, Any]],
        client.call(
            "loyalty.card",
            "search_read",
            {
                "domain": [["code", "=", code]],
                "fields": ["partner_id", "program_id", "points"],
            },
        ),
    )
    assert cards[0]["partner_id"][0] == world["partner_id"]
    assert cards[0]["points"] == 1
    from engine.odoo import CouponRef

    world["coupon"] = CouponRef(
        program_id=cards[0]["program_id"][0], code=code, card_id=cards[0]["id"]
    )
