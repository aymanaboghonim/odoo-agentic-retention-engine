from __future__ import annotations

import argparse
from decimal import Decimal
from inspect import signature

import pytest

from scripts.gated_execution_review import (
    FIXED_MESSAGES,
    _channel,
    _odoo,
    _print_review,
    _require_local_host,
    _review_product,
    _review_website,
    parser,
)


def test_review_cli_requires_channel_language_and_mode_for_proposals() -> None:
    args = parser().parse_args(
        [
            "prepare",
            "--cart-id",
            "17",
            "--channel",
            "mailpit",
            "--language",
            "ar",
            "--mode",
            "fixed",
        ]
    )
    assert args.cart_id == 17
    assert args.channel == "mailpit"
    assert args.language == "ar"
    assert args.mode == "fixed"


def test_review_cli_has_separate_operator_steps() -> None:
    review_parser = parser()
    parsed = [
        review_parser.parse_args(["create-cart"]),
        review_parser.parse_args(["preflight", "--channel", "mailpit"]),
        review_parser.parse_args(
            [
                "prepare",
                "--cart-id",
                "17",
                "--channel",
                "mailpit",
                "--language",
                "en",
                "--mode",
                "fixed",
            ]
        ),
        review_parser.parse_args(["show", "--run-id", "00000000-0000-0000-0000-000000000001"]),
        review_parser.parse_args(["approve", "--run-id", "00000000-0000-0000-0000-000000000001"]),
        review_parser.parse_args(
            [
                "execute",
                "--run-id",
                "00000000-0000-0000-0000-000000000001",
                "--channel",
                "mailpit",
            ]
        ),
    ]
    assert [args.command for args in parsed] == [
        "create-cart",
        "preflight",
        "prepare",
        "show",
        "approve",
        "execute",
    ]
    signature(parsed[0].function).bind(parsed[0])


def test_review_preview_shows_localized_mailpit_subject_before_message(
    capsys: pytest.CaptureFixture[str],
) -> None:
    _print_review(
        "evaluating_policy",
        {
            "execution_channel": "mailpit",
            "language": "ar",
            "proposed_discount_percent": "0",
            "customer_message": "سلتك التجريبية بانتظارك.",
        },
        {},
    )
    output = capsys.readouterr().out
    assert "approved_subject=سلتك بانتظارك" in output
    assert output.index("approved_subject=") < output.index("approved_message_begin")
    assert "سلتك التجريبية بانتظارك." in output


def test_review_channels_and_fixed_messages_are_explicit() -> None:
    assert _channel("telegram") == "telegram"
    assert _channel("mailpit") == "mailpit"
    assert set(FIXED_MESSAGES) == {"en", "ar"}
    with pytest.raises(argparse.ArgumentTypeError, match="telegram or mailpit"):
        _channel("email")


def test_review_product_requires_a_costed_product_with_positive_margin() -> None:
    product_id, price, cost, category = _review_product(
        [
            {
                "id": 13,
                "list_price": 750.0,
                "standard_price": 500.0,
                "categ_id": [5, "Furniture / Office"],
            }
        ]
    )
    assert (product_id, price, cost, category) == (
        13,
        Decimal("750.0"),
        Decimal("500.0"),
        "Furniture / Office",
    )

    with pytest.raises(ValueError, match="known unit cost"):
        _review_product(
            [
                {
                    "id": 15,
                    "list_price": 750.0,
                    "standard_price": False,
                    "categ_id": [5, "Furniture / Office"],
                }
            ]
        )


def test_review_website_selects_the_first_ordered_local_site() -> None:
    assert _review_website(
        [
            {"id": 1, "name": "My Website"},
            {"id": 2, "name": "My Website 2"},
        ]
    ) == (1, "My Website")


def test_review_targets_must_be_loopback() -> None:
    _require_local_host("127.0.0.1", "database")
    _require_local_host("localhost", "database")
    with pytest.raises(ValueError, match="loopback"):
        _require_local_host("example.com", "database")


@pytest.mark.parametrize(
    ("readonly_url", "expected_error"),
    [
        ("postgresql://ro@db.example.com:5432/odoo", "container IP"),
        ("postgresql://ro@172.25.0.3:5432/other", "selected Odoo database"),
    ],
)
def test_review_odoo_config_rejects_nonlocal_or_mismatched_db(
    monkeypatch: pytest.MonkeyPatch, readonly_url: str, expected_error: str
) -> None:
    monkeypatch.setenv("ODOO_URL", "http://127.0.0.1:8070")
    monkeypatch.setenv("ODOO_DB_NAME", "odoo")
    monkeypatch.setenv("ODOO_API_KEY", "synthetic-test-key")
    monkeypatch.setenv("ODOO_READONLY_DATABASE_URL", readonly_url)
    with pytest.raises(ValueError, match=expected_error):
        _odoo()


def test_review_odoo_requires_the_documented_disposable_api_port(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("ODOO_URL", "http://127.0.0.1:8069")
    monkeypatch.setenv("ODOO_DB_NAME", "odoo")
    monkeypatch.setenv("ODOO_API_KEY", "synthetic-test-key")
    monkeypatch.setenv("ODOO_READONLY_DATABASE_URL", "postgresql://ro@172.25.0.3:5432/odoo")
    with pytest.raises(ValueError, match="port 8070"):
        _odoo()
