from __future__ import annotations

import ipaddress
import json
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from typing import Any, cast
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlparse
from urllib.request import Request, urlopen

import psycopg
from psycopg.rows import dict_row

from engine.policy import GateApproval, OfferProposal

DEFAULT_ALLOWED_HOSTS = ("127.0.0.1", "localhost", "::1", "odoo")
DEFAULT_COUPON_DAYS = 7


class OdooConfigError(Exception):
    """Raised when the Odoo settings are missing or point outside the allowed hosts."""


class OdooApiError(Exception):
    """Raised when an Odoo JSON-2 call fails. Never contains the API key."""


@dataclass(frozen=True)
class OdooSettings:
    url: str
    database: str
    api_key: str
    readonly_database_url: str
    coupon_days: int = DEFAULT_COUPON_DAYS

    @classmethod
    def from_env(cls, env: Mapping[str, str]) -> OdooSettings:
        values = {
            name: env.get(name, "").strip()
            for name in (
                "ODOO_URL",
                "ODOO_DB_NAME",
                "ODOO_API_KEY",
                "ODOO_READONLY_DATABASE_URL",
            )
        }
        missing = [name for name, value in values.items() if not value]
        if missing:
            raise OdooConfigError(f"Missing Odoo settings: {', '.join(missing)}.")
        allowed = (
            tuple(
                host.strip()
                for host in env.get("ODOO_ALLOWED_HOSTS", "").split(",")
                if host.strip()
            )
            or DEFAULT_ALLOWED_HOSTS
        )
        host = urlparse(values["ODOO_URL"]).hostname or ""
        if not _host_allowed(host, allowed):
            raise OdooConfigError("ODOO_URL host is not loopback or in ODOO_ALLOWED_HOSTS.")
        raw_days = env.get("ODOO_COUPON_DAYS", "").strip()
        days = int(raw_days) if raw_days else DEFAULT_COUPON_DAYS
        if days < 1:
            raise OdooConfigError("ODOO_COUPON_DAYS must be at least 1.")
        return cls(
            url=values["ODOO_URL"].rstrip("/"),
            database=values["ODOO_DB_NAME"],
            api_key=values["ODOO_API_KEY"],
            readonly_database_url=values["ODOO_READONLY_DATABASE_URL"],
            coupon_days=days,
        )


def _host_allowed(host: str, allowed: tuple[str, ...]) -> bool:
    if host in allowed:
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


@dataclass(frozen=True)
class CartLine:
    product_id: int
    name: str
    sku: str | None
    category: str
    quantity: Decimal
    unit_price: Decimal
    unit_cost: Decimal | None


@dataclass(frozen=True)
class AbandonedCart:
    order_id: int
    reference: str
    partner_id: int
    partner_name: str
    partner_email: str
    language: str | None
    currency: str
    amount_total: Decimal
    date_order: datetime
    lines: tuple[CartLine, ...]

    def age_hours(self, now: datetime) -> float:
        return (now - self.date_order).total_seconds() / 3600


_CART_SELECT = """
SELECT so.id, so.name, so.date_order, so.amount_total, so.company_id,
       cur.name AS currency, p.id AS partner_id, p.name AS partner_name,
       p.email AS partner_email, p.lang AS language
FROM sale_order so
JOIN website w ON w.id = so.website_id
JOIN res_users wu ON wu.id = w.user_id
JOIN res_partner p ON p.id = so.partner_id
JOIN res_currency cur ON cur.id = so.currency_id
WHERE so.state = 'draft'
  AND so.partner_id <> wu.partner_id
  AND COALESCE(p.email, '') <> ''
  AND EXISTS (
      SELECT 1 FROM sale_order_line l
      WHERE l.order_id = so.id AND l.display_type IS NULL AND l.product_id IS NOT NULL
  )
"""

_SCAN_SQL = (
    _CART_SELECT
    + """  AND so.date_order <= %(now)s - w.cart_abandoned_delay * INTERVAL '1 hour'
ORDER BY so.date_order, so.id
LIMIT %(limit)s
"""
)

_ONE_SQL = _CART_SELECT + "  AND so.id = %(order_id)s\n"

_LINES_SQL = """
SELECT l.product_id, l.product_uom_qty AS quantity, l.price_unit,
       pp.default_code AS sku,
       COALESCE(pt.name->>'en_US', pt.name->>(SELECT jsonb_object_keys(pt.name) LIMIT 1)) AS name,
       cat.complete_name AS category,
       pp.standard_price->>%(company)s AS unit_cost
FROM sale_order_line l
JOIN product_product pp ON pp.id = l.product_id
JOIN product_template pt ON pt.id = pp.product_tmpl_id
JOIN product_category cat ON cat.id = pt.categ_id
WHERE l.order_id = %(order_id)s
  AND l.display_type IS NULL
  AND COALESCE(l.is_delivery, FALSE) = FALSE
ORDER BY l.sequence, l.id
"""


class OdooReader:
    """Reads Odoo through a read-only database role. It has no write method by design."""

    def __init__(self, database_url: str) -> None:
        self._url = database_url

    def _connect(self) -> psycopg.Connection[dict[str, Any]]:
        conn = psycopg.connect(self._url, row_factory=dict_row)
        conn.read_only = True
        return conn

    def scan_abandoned_carts(self, *, now: datetime, limit: int = 50) -> list[AbandonedCart]:
        with self._connect() as conn:
            rows = conn.execute(_SCAN_SQL, {"now": _naive_utc(now), "limit": limit}).fetchall()
            return [self._cart(conn, row) for row in rows]

    def get_cart(self, order_id: int) -> AbandonedCart | None:
        with self._connect() as conn:
            row = conn.execute(_ONE_SQL, {"order_id": order_id}).fetchone()
            return self._cart(conn, row) if row else None

    def _cart(self, conn: psycopg.Connection[dict[str, Any]], row: dict[str, Any]) -> AbandonedCart:
        lines = conn.execute(
            _LINES_SQL, {"order_id": row["id"], "company": str(row["company_id"])}
        ).fetchall()
        return AbandonedCart(
            order_id=row["id"],
            reference=row["name"],
            partner_id=row["partner_id"],
            partner_name=row["partner_name"],
            partner_email=row["partner_email"],
            language=row["language"],
            currency=row["currency"],
            amount_total=Decimal(row["amount_total"]),
            date_order=row["date_order"].replace(tzinfo=UTC),
            lines=tuple(
                CartLine(
                    product_id=line["product_id"],
                    name=line["name"],
                    sku=line["sku"],
                    category=line["category"],
                    quantity=Decimal(line["quantity"]),
                    unit_price=Decimal(line["price_unit"]),
                    unit_cost=Decimal(line["unit_cost"]) if line["unit_cost"] is not None else None,
                )
                for line in lines
            ),
        )


def _naive_utc(moment: datetime) -> datetime:
    if moment.tzinfo is None:
        raise ValueError("now must be timezone-aware.")
    return moment.astimezone(UTC).replace(tzinfo=None)


def cart_to_proposal_inputs(cart: AbandonedCart) -> tuple[Decimal, Decimal | None]:
    """Return (eligible_total, eligible_unit_cost_total) for the gate. Cost is None if unknown."""
    total = sum((line.unit_price * line.quantity for line in cart.lines), Decimal(0))
    costs = [line.unit_cost for line in cart.lines]
    if any(cost is None for cost in costs):
        return total, None
    cost_total = sum(
        (cast(Decimal, line.unit_cost) * line.quantity for line in cart.lines), Decimal(0)
    )
    return total, cost_total


class JsonTwoClient:
    """Writes to Odoo only through the supported JSON-2 API."""

    def __init__(self, settings: OdooSettings, *, timeout: int = 20) -> None:
        self._settings = settings
        self._timeout = timeout

    def call(self, model: str, method: str, params: dict[str, object]) -> object:
        url = f"{self._settings.url}/json/2/{quote(model, safe='')}/{quote(method, safe='')}"
        request = Request(
            url,
            data=json.dumps(params).encode("utf-8"),
            headers={
                "Authorization": f"bearer {self._settings.api_key}",
                "Content-Type": "application/json; charset=utf-8",
                "X-Odoo-Database": self._settings.database,
                "User-Agent": "odoo-agentic-retention-engine",
            },
            method="POST",
        )
        try:
            with urlopen(request, timeout=self._timeout) as response:  # noqa: S310
                return json.loads(response.read())
        except HTTPError as error:
            raise OdooApiError(f"Odoo {model}.{method} failed with HTTP {error.code}") from error
        except (URLError, TimeoutError) as error:
            raise OdooApiError(f"Could not reach Odoo for {model}.{method}") from error
        except json.JSONDecodeError as error:
            raise OdooApiError(f"Odoo {model}.{method} returned invalid JSON") from error

    def _create(self, model: str, values: dict[str, object]) -> int:
        result = self.call(model, "create", {"vals_list": [values]})
        if (
            not isinstance(result, list)
            or len(cast(list[object], result)) != 1
            or not isinstance(cast(list[object], result)[0], int)
        ):
            raise OdooApiError(f"Odoo {model}.create returned an unexpected result")
        return cast(list[int], result)[0]

    def _search_ids(self, model: str, domain: list[object]) -> list[int]:
        result = self.call(model, "search", {"domain": domain, "context": {"active_test": False}})
        if not isinstance(result, list):
            raise OdooApiError(f"Odoo {model}.search returned an unexpected result")
        return cast(list[int], result)

    def create_activity(
        self, *, order_id: int, summary: str, note: str, deadline: date, user_id: int | None = None
    ) -> int:
        model_ids = self._search_ids("ir.model", [["model", "=", "sale.order"]])
        if len(model_ids) != 1:
            raise OdooApiError("Odoo has no unique sale.order model record")
        values: dict[str, object] = {
            "res_model_id": model_ids[0],
            "res_id": order_id,
            "summary": summary,
            "note": note,
            "date_deadline": deadline.isoformat(),
        }
        if user_id is not None:
            values["user_id"] = user_id
        return self._create("mail.activity", values)

    def delete_activity(self, activity_id: int) -> None:
        self.call("mail.activity", "unlink", {"ids": [activity_id]})

    def create_coupon(
        self, *, code: str, percent: Decimal, partner_id: int, expires: date
    ) -> CouponRef:
        existing = self.call(
            "loyalty.card",
            "search_read",
            {"domain": [["code", "=", code]], "fields": ["program_id"], "limit": 1},
        )
        if isinstance(existing, list) and existing:
            program = cast(list[dict[str, Any]], existing)[0]["program_id"]
            return CouponRef(program_id=int(program[0]), code=code, card_id=None)
        program_id = self._create(
            "loyalty.program",
            {
                "name": f"Retention offer {code}",
                "program_type": "coupons",
                "applies_on": "current",
                "trigger": "with_code",
                "date_to": expires.isoformat(),
                "reward_ids": [
                    [
                        0,
                        0,
                        {
                            "reward_type": "discount",
                            "discount_mode": "percent",
                            "discount": float(percent),
                            "discount_applicability": "order",
                            "required_points": 1,
                        },
                    ]
                ],
            },
        )
        card_id = self._create(
            "loyalty.card",
            {
                "program_id": program_id,
                "partner_id": partner_id,
                "code": code,
                "points": 1,
                "expiration_date": expires.isoformat(),
            },
        )
        return CouponRef(program_id=program_id, code=code, card_id=card_id)

    def archive_coupon(self, coupon: CouponRef) -> None:
        """Deactivate the offer. Odoo 19 JSON-2 refuses to delete a loyalty.card (F-032)."""
        self.call(
            "loyalty.program", "write", {"ids": [coupon.program_id], "vals": {"active": False}}
        )
        if coupon.card_id is not None:
            self.call("loyalty.card", "write", {"ids": [coupon.card_id], "vals": {"active": False}})


@dataclass(frozen=True)
class CouponRef:
    program_id: int
    code: str
    card_id: int | None


class OdooCouponCreator:
    """CouponCreator for the gated executor: one single-use Odoo coupon per approved offer."""

    def __init__(self, client: JsonTwoClient, settings: OdooSettings) -> None:
        self._client = client
        self._settings = settings

    def create(self, approval: GateApproval, proposal: OfferProposal) -> str:
        if proposal.proposed_discount_percent is None:
            raise OdooApiError("Cannot create a coupon without a discount percent.")
        code = f"RET-{approval.fingerprint[:12].upper()}"
        expires = datetime.now(UTC).date() + timedelta(days=self._settings.coupon_days)
        coupon = self._client.create_coupon(
            code=code,
            percent=proposal.proposed_discount_percent,
            partner_id=int(proposal.customer_id),
            expires=expires,
        )
        return coupon.code
