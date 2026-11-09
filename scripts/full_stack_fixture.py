from __future__ import annotations

import argparse
import uuid
from datetime import UTC, datetime, timedelta

from engine.full_stack_worker import WorkerSettings
from engine.odoo import JsonTwoClient, OdooReader, OdooSettings
from scripts.gated_execution_review import _as_records, _review_product, _review_website


def main() -> None:
    import os

    parser = argparse.ArgumentParser(description="Create one disposable synthetic review fixture")
    parser.add_argument("--preflight-only", action="store_true")
    parser.add_argument("--confirmed-synthetic", action="store_true")
    args = parser.parse_args()
    worker = WorkerSettings.from_env(os.environ)
    settings = OdooSettings.from_env(os.environ)
    if settings.url != "http://odoo:8069" or settings.database != "odoo":
        raise ValueError("fixture creation is restricted to the isolated Compose Odoo")
    reader = OdooReader(worker.odoo_url)
    reader.verify_synthetic_readonly_role()
    client = JsonTwoClient(settings)
    products = _as_records(
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
        ),
        "synthetic product lookup",
    )
    product, price, _cost, _category = _review_product(products)
    websites = _as_records(
        client.call(
            "website",
            "search_read",
            {"domain": [], "fields": ["id", "name"], "order": "id asc", "limit": 1},
        ),
        "website lookup",
    )
    website_id, _ = _review_website(websites)
    print("stage=fixture-preflight status=passed; no write has been made", flush=True)
    if args.preflight_only:
        return
    if not args.confirmed_synthetic:
        raise ValueError("--confirmed-synthetic is required to create a disposable fixture")
    tag = uuid.uuid4().hex[:12]
    reference = f"retention-review-{tag}"
    print(f"stage=fixture-create synthetic_reference={reference}", flush=True)
    partner = client.call(
        "res.partner",
        "create",
        {
            "vals_list": [
                {
                    "name": f"Synthetic retention review {tag}",
                    "email": f"{reference}@example.test",
                    "lang": "en_US",
                }
            ]
        },
    )
    if not isinstance(partner, list) or len(partner) != 1 or type(partner[0]) is not int:
        raise ValueError("Odoo did not confirm the synthetic partner; inspect before retrying")
    order = client.call(
        "sale.order",
        "create",
        {
            "vals_list": [
                {
                    "partner_id": partner[0],
                    "website_id": website_id,
                    "date_order": (datetime.now(UTC) - timedelta(days=7)).strftime(
                        "%Y-%m-%d %H:%M:%S"
                    ),
                    "client_order_ref": reference,
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
    )
    if not isinstance(order, list) or len(order) != 1 or type(order[0]) is not int:
        raise ValueError("Odoo did not confirm the synthetic order; inspect before retrying")
    cart = reader.get_synthetic_review_cart(order[0], now=datetime.now(UTC))
    if cart is None or cart.partner_id != partner[0]:
        raise ValueError("created cart did not pass the read-only synthetic eligibility check")
    print(f"stage=fixture-create status=complete cart_id={cart.order_id}", flush=True)


if __name__ == "__main__":
    main()
