from email.message import Message
from io import BytesIO
from unittest.mock import patch
from urllib.error import HTTPError

import pytest

from scripts.spike_s1_odoo import OdooApiError, OdooClient, validate_local_url


@pytest.mark.parametrize(
    "url",
    [
        "http://localhost:8069",
        "http://127.0.0.1:8069/",
        "https://[::1]:8069",
    ],
)
def test_local_odoo_urls_are_accepted(url: str) -> None:
    assert validate_local_url(url).startswith(("http://", "https://"))


@pytest.mark.parametrize(
    "url",
    [
        "https://odoo.example.com",
        "http://192.0.2.1:8069",
        "http://user:password@localhost:8069",
        "http://localhost:bad-port",
    ],
)
def test_non_local_or_malformed_odoo_urls_are_rejected(url: str) -> None:
    with pytest.raises(ValueError):
        validate_local_url(url)


def test_api_error_is_reported_without_exposing_the_api_key() -> None:
    api_key = "local-test-api-key"
    client = OdooClient("http://localhost:8069", "odoo", api_key)
    http_error = HTTPError(
        "http://localhost:8069/json/2/website/search_read",
        401,
        "Unauthorized",
        Message(),
        BytesIO(),
    )

    with patch("scripts.spike_s1_odoo.urlopen", side_effect=http_error):
        with pytest.raises(OdooApiError, match="HTTP 401") as error:
            client.call("website", "search_read", {"domain": []})

    assert api_key not in str(error.value)
