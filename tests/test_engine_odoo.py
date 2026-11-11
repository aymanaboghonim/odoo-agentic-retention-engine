from __future__ import annotations

import json
import re
import threading
from datetime import UTC, date, datetime
from decimal import Decimal
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from typing import Any, cast
from unittest.mock import MagicMock, patch

import pytest

from engine import odoo
from engine.coupon_code import coupon_code_for_run
from engine.odoo import (
    AbandonedCart,
    CartLine,
    JsonTwoClient,
    OdooApiError,
    OdooConfigError,
    OdooSettings,
    cart_to_proposal_inputs,
)

ENV = {
    "ODOO_URL": "http://127.0.0.1:8070",
    "ODOO_DB_NAME": "odoo",
    "ODOO_API_KEY": "test-key-not-real",
    "ODOO_READONLY_DATABASE_URL": "postgresql://ro@127.0.0.1/odoo",
}


def test_settings_require_all_values() -> None:
    with pytest.raises(OdooConfigError, match="ODOO_API_KEY"):
        OdooSettings.from_env({k: v for k, v in ENV.items() if k != "ODOO_API_KEY"})


@pytest.mark.parametrize("url", ["https://odoo.example.com", "http://10.0.0.5:8069"])
def test_settings_reject_non_local_hosts(url: str) -> None:
    with pytest.raises(OdooConfigError, match="loopback"):
        OdooSettings.from_env({**ENV, "ODOO_URL": url})


def test_settings_allow_listed_host_and_default_expiry() -> None:
    settings = OdooSettings.from_env(
        {**ENV, "ODOO_URL": "http://odoo:8069/", "ODOO_ALLOWED_HOSTS": "odoo"}
    )
    assert settings.url == "http://odoo:8069"
    assert settings.coupon_days == 7


def _line(price: str, qty: str, cost: str | None) -> CartLine:
    return CartLine(
        1,
        "Lamp",
        "L1",
        "decor",
        Decimal(qty),
        Decimal(price),
        Decimal(cost) if cost is not None else None,
    )


def _cart(*lines: CartLine) -> AbandonedCart:
    return AbandonedCart(
        1,
        "S1",
        9,
        "Test",
        "t@example.com",
        "ar_001",
        "USD",
        Decimal(0),
        datetime(2026, 11, 1, tzinfo=UTC),
        lines,
    )


def test_gate_inputs_sum_price_and_cost() -> None:
    total, cost = cart_to_proposal_inputs(_cart(_line("80", "2", "50"), _line("12", "1", "5")))
    assert (total, cost) == (Decimal("172"), Decimal("105"))


def test_gate_inputs_cost_unknown_when_any_line_has_no_cost() -> None:
    total, cost = cart_to_proposal_inputs(_cart(_line("80", "1", "50"), _line("12", "1", None)))
    assert total == Decimal("92")
    assert cost is None


def test_age_hours() -> None:
    cart = _cart(_line("1", "1", "1"))
    assert cart.age_hours(datetime(2026, 11, 1, 11, tzinfo=UTC)) == 11


def test_execution_lookup_uses_current_abandoned_delay() -> None:
    reader = odoo.OdooReader(ENV["ODOO_READONLY_DATABASE_URL"])
    connection = MagicMock()
    connection.__enter__.return_value = connection
    connection.execute.return_value.fetchone.return_value = None
    now = datetime(2026, 11, 5, 12, 0, tzinfo=UTC)
    with patch.object(reader, "_connect", return_value=connection):
        assert reader.get_abandoned_cart(42, now=now) is None
    query, parameters = connection.execute.call_args.args
    assert "so.id = %(order_id)s" in query
    assert "so.date_order <= %(now)s - w.cart_abandoned_delay" in query
    assert parameters == {"order_id": 42, "now": now.replace(tzinfo=None)}


class _Recorder(BaseHTTPRequestHandler):
    calls: list[dict[str, Any]] = []
    replies: dict[str, object] = {}
    reply_queues: dict[str, list[object]] = {}

    def do_POST(self) -> None:
        body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        type(self).calls.append(
            {"path": self.path, "body": body, "auth": self.headers["Authorization"]}
        )
        queue = type(self).reply_queues.get(self.path, [])
        reply = queue.pop(0) if queue else type(self).replies.get(self.path, 404)
        status = reply if isinstance(reply, int) else 200
        self.send_response(status)
        self.end_headers()
        if not isinstance(reply, int):
            self.wfile.write(json.dumps(reply).encode())

    def log_message(self, *args: object) -> None:
        return


@pytest.fixture
def server() -> Any:
    _Recorder.calls = []
    _Recorder.replies = {}
    _Recorder.reply_queues = {}
    httpd = HTTPServer(("127.0.0.1", 0), _Recorder)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    yield httpd
    httpd.shutdown()
    thread.join()


def _client(server: HTTPServer) -> JsonTwoClient:
    url = f"http://127.0.0.1:{server.server_address[1]}"
    return JsonTwoClient(OdooSettings.from_env({**ENV, "ODOO_URL": url}))


def test_activity_uses_json2_with_bearer_key(server: HTTPServer) -> None:
    _Recorder.replies = {
        "/json/2/ir.model/search": [77],
        "/json/2/mail.activity/create": [501],
    }
    activity = _client(server).create_activity(
        order_id=12, summary="Retention offer", note="note", deadline=date(2026, 11, 8)
    )
    assert activity == 501
    created = _Recorder.calls[-1]
    assert created["auth"] == "bearer test-key-not-real"
    assert created["body"]["vals_list"][0]["res_model_id"] == 77
    assert created["body"]["vals_list"][0]["res_id"] == 12


def test_coupon_is_created_as_program_and_card(server: HTTPServer) -> None:
    card = {
        "id": 32,
        "code": "RET-ABC",
        "partner_id": [9, "Synthetic"],
        "program_id": [31, "Retention offer RET-ABC"],
        "points": 1,
        "expiration_date": "2026-11-12",
        "active": True,
    }
    program = {
        "id": 31,
        "program_type": "coupons",
        "trigger": "with_code",
        "date_to": "2026-11-12",
        "active": True,
        "reward_ids": [41],
    }
    reward = {
        "reward_type": "discount",
        "discount_mode": "percent",
        "discount": 10.0,
        "discount_applicability": "order",
        "required_points": 1.0,
    }
    _Recorder.reply_queues = {
        "/json/2/loyalty.card/search_read": [[], [card]],
        "/json/2/loyalty.program/search_read": [[], [program]],
        "/json/2/loyalty.program/create": [[31]],
        "/json/2/loyalty.card/create": [[32]],
        "/json/2/loyalty.reward/search_read": [[reward], [reward]],
    }
    coupon = _client(server).create_coupon(
        code="RET-ABC", percent=Decimal("10"), partner_id=9, expires=date(2026, 11, 12)
    )
    assert (coupon.program_id, coupon.card_id, coupon.code) == (31, 32, "RET-ABC")
    program = next(c for c in _Recorder.calls if c["path"].endswith("loyalty.program/create"))
    program_body = cast(dict[str, Any], program["body"])
    reward = program_body["vals_list"][0]["reward_ids"][0][2]
    assert reward["discount"] == 10.0
    assert reward["discount_mode"] == "percent"
    card = next(c for c in _Recorder.calls if c["path"].endswith("loyalty.card/create"))
    card_body = cast(dict[str, Any], card["body"])
    assert card_body["vals_list"][0]["partner_id"] == 9


def test_coupon_creation_is_idempotent_by_code(server: HTTPServer) -> None:
    card = {
        "id": 5,
        "code": "RET-ABC",
        "partner_id": [9, "Synthetic"],
        "program_id": [31, "Retention offer RET-ABC"],
        "points": 1,
        "expiration_date": "2026-11-12",
        "active": True,
    }
    program = {
        "id": 31,
        "program_type": "coupons",
        "trigger": "with_code",
        "date_to": "2026-11-12",
        "active": True,
        "reward_ids": [41],
    }
    reward = {
        "reward_type": "discount",
        "discount_mode": "percent",
        "discount": 10,
        "discount_applicability": "order",
        "required_points": 1,
    }
    _Recorder.reply_queues = {
        "/json/2/loyalty.card/search_read": [[card]],
        "/json/2/loyalty.program/search_read": [[program]],
        "/json/2/loyalty.reward/search_read": [[reward]],
    }
    coupon = _client(server).create_coupon(
        code="RET-ABC", percent=Decimal("10"), partner_id=9, expires=date(2026, 11, 12)
    )
    assert (coupon.program_id, coupon.card_id) == (31, 5)
    assert not any(c["path"].endswith("/create") for c in _Recorder.calls)


def test_existing_coupon_with_mismatched_terms_is_refused(server: HTTPServer) -> None:
    _Recorder.replies = {
        "/json/2/loyalty.card/search_read": [
            {
                "id": 5,
                "code": "RET-ABC",
                "partner_id": [10, "Other synthetic user"],
                "program_id": [31, "Retention offer RET-ABC"],
                "points": 1,
                "expiration_date": "2026-11-12",
                "active": True,
            }
        ]
    }
    with pytest.raises(OdooApiError, match="approved terms"):
        _client(server).create_coupon(
            code="RET-ABC", percent=Decimal("10"), partner_id=9, expires=date(2026, 11, 12)
        )
    assert len(_Recorder.calls) == 1


def test_partial_coupon_program_is_reused_only_when_terms_match(server: HTTPServer) -> None:
    program = {
        "id": 31,
        "program_type": "coupons",
        "trigger": "with_code",
        "date_to": "2026-11-12",
        "active": True,
        "reward_ids": [41],
    }
    card = {
        "id": 32,
        "code": "RET-ABC",
        "partner_id": [9, "Synthetic"],
        "program_id": [31, "Retention offer RET-ABC"],
        "points": 1,
        "expiration_date": "2026-11-12",
        "active": True,
    }
    reward = {
        "reward_type": "discount",
        "discount_mode": "percent",
        "discount": 10,
        "discount_applicability": "order",
        "required_points": 1,
    }
    _Recorder.reply_queues = {
        "/json/2/loyalty.card/search_read": [[], [card]],
        "/json/2/loyalty.program/search_read": [[program], [program]],
        "/json/2/loyalty.card/create": [[32]],
        "/json/2/loyalty.reward/search_read": [[reward], [reward]],
    }
    coupon = _client(server).create_coupon(
        code="RET-ABC", percent=Decimal("10"), partner_id=9, expires=date(2026, 11, 12)
    )
    assert (coupon.program_id, coupon.card_id) == (31, 32)
    assert not any(c["path"].endswith("loyalty.program/create") for c in _Recorder.calls)


def test_coupon_code_uses_stable_run_identity() -> None:
    assert coupon_code_for_run("run-1") == coupon_code_for_run("run-1")
    assert coupon_code_for_run("run-1") != coupon_code_for_run("run-2")
    assert re.fullmatch(r"RET-[0-9A-F]{32}", coupon_code_for_run("run-1"))
    with pytest.raises(ValueError):
        coupon_code_for_run("")


def test_http_error_does_not_leak_key(server: HTTPServer) -> None:
    with pytest.raises(OdooApiError) as caught:
        _client(server).call("res.partner", "search", {})
    assert "test-key-not-real" not in str(caught.value)


def test_no_sql_write_path_in_adapter() -> None:
    source = Path(odoo.__file__).read_text(encoding="utf-8")
    assert not re.search(
        r"\b(INSERT\s+INTO|UPDATE\s+\w+\s+SET|DELETE\s+FROM|TRUNCATE\s+(?:TABLE\s+)?\w+)\b",
        source,
        re.I,
    )
    reader_methods = {name for name in dir(odoo.OdooReader) if not name.startswith("__")}
    assert reader_methods == {
        "scan_abandoned_carts",
        "scan_synthetic_review_carts",
        "verify_synthetic_readonly_role",
        "get_synthetic_review_cart",
        "get_cart",
        "get_abandoned_cart",
        "_connect",
        "_cart",
    }
