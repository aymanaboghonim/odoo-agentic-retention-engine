# Spec: Odoo adapter (read-only scan, JSON-2 writes)

- Issue: #63
- ADRs: ADR-0001 (read-only Odoo access), ADR-0018 (gate inputs)
- Facts: F-028, F-042
- Status: agreed

## Purpose

Let the engine see abandoned carts and customer, product and cost context in Odoo,
and create an activity and a coupon in Odoo, without ever writing to the Odoo database.

## Decisions (owner, 2026-11-05)

| Decision | Why |
| :-- | :-- |
| Scan with SQL on a read-only role (ADR-0001) | One query joins carts, lines, partner, currency and cost; the DB role makes a write impossible. |
| Eligible cart = draft website order, not the website public partner, has an email, has a real line, older than that website's `cart_abandoned_delay` | Matches Odoo's own abandoned-cart idea; no customer to contact otherwise. |
| Unit cost comes from Odoo `standard_price` (per company) and goes to the gate | The margin rule (ADR-0018) needs a cost; a second cost table would drift. Unknown cost reaches the gate as `None`, which it rejects. |
| Coupon = Odoo `loyalty.program` (`coupons`, percent reward) + one `loyalty.card` for the customer | Verified in F-042. The code is derived from the gate approval fingerprint, so a retry does not create a second coupon. |
| Writes only through JSON-2 with a bearer API key | Business rules and taxes stay in Odoo. |
| Live test on the disposable `s2-llm-smoke` instance | Isolated, synthetic data, no shared Odoo. |
| `ODOO_URL` must be loopback or in `ODOO_ALLOWED_HOSTS` | Prevents pointing the engine at a real instance by mistake. |

## Invariants

| ID | Must always be true | Check |
| :-- | :-- | :-- |
| OA-1 | The adapter has no SQL write path; the reader exposes only scan and get. | `test_no_sql_write_path_in_adapter` |
| OA-2 | The database role cannot write. | `test_read_only_role_cannot_write` (live) |
| OA-3 | Only a cart older than the delay, with a customer email and lines, is returned. | `test_scan_finds_eligible_cart_and_skips_recent` (live) |
| OA-4 | Cost is passed to the gate as unknown when any line has no cost. | `test_gate_inputs_cost_unknown_when_any_line_has_no_cost` |
| OA-5 | Activity and coupon are created through JSON-2 with the bearer key; errors never contain the key. | `test_activity_uses_json2_with_bearer_key`, `test_http_error_does_not_leak_key` (live: `test_activity_and_coupon_created_through_json2`) |
| OA-6 | The same approval creates the same coupon code once. | `test_coupon_creation_is_idempotent_by_code` |
| OA-7 | A non-local Odoo URL is refused. | `test_settings_reject_non_local_hosts` |

## Key behaviors

- `OdooReader.scan_abandoned_carts(now, limit)` and `get_cart(order_id)` return
  `AbandonedCart` with `CartLine` items; money is `Decimal`.
- `cart_to_proposal_inputs` gives the gate `eligible_total` and the unit cost total.
- `JsonTwoClient.create_activity` and `create_coupon` write to Odoo; `OdooCouponCreator`
  plugs into `GatedExecutor` (#56), which re-checks the gate first.
- Odoo 19 cannot delete a `loyalty.card` through JSON-2, so cleanup archives the
  program and card (F-042).

## Risks and limits

- `loyalty`, `sale_loyalty` and `website_sale_loyalty` are not installed by default; the
  runbook installs them on the local instance.
- Odoo evaluates the age with `date_order`; so does the scan. A cart edited later keeps its age.
- One company is assumed for cost lookup (the order's company).
- Coupon redemption in the shop is not tested here (later issues).
- Activity assignee defaults to the API user.
