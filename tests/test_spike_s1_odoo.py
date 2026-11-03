import time
from collections.abc import Callable
from email.message import Message
from io import BytesIO, StringIO
from unittest.mock import patch
from urllib.error import HTTPError

import pytest

from scripts.spike_s1_odoo import (
    READY_POLL_SECONDS,
    OdooApiError,
    OdooClient,
    PreflightError,
    Progress,
    main,
    validate_local_url,
    wait_until_ready,
)


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


API_KEY = "local-test-api-key"
ENV = {"ODOO_URL": "http://localhost:8069", "ODOO_DB_NAME": "odoo", "ODOO_API_KEY": API_KEY}


def _progress() -> tuple[Progress, StringIO]:
    stream = StringIO()
    return Progress(stream=stream, heartbeat_seconds=30.0), stream


def test_stage_reports_start_and_completion() -> None:
    progress, stream = _progress()

    with progress.stage("probe") as step:
        step(2, 5)

    lines = stream.getvalue().splitlines()
    assert lines[0].startswith("progress stage=probe status=started")
    assert "status=progress completed=2 total=5" in lines[1]
    assert lines[-1].startswith("progress stage=probe status=completed")
    assert "elapsed_seconds=" in lines[-1]


def test_slow_stage_emits_heartbeat() -> None:
    stream = StringIO()
    progress = Progress(stream=stream, heartbeat_seconds=0.01)

    with progress.stage("slow"):
        time.sleep(0.1)

    assert "status=heartbeat" in stream.getvalue()


def test_failed_stage_names_stage_and_exits_nonzero(capsys: pytest.CaptureFixture[str]) -> None:
    progress, stream = _progress()

    with patch(
        "scripts.spike_s1_odoo.check_login_page", side_effect=PreflightError("Odoo did not answer")
    ):
        assert main([], ENV, progress) == 1

    assert "stage preflight-readiness" in capsys.readouterr().err
    assert "status=failed" in stream.getvalue()


def test_preflight_reports_missing_api_key(capsys: pytest.CaptureFixture[str]) -> None:
    progress, _ = _progress()

    with patch("scripts.spike_s1_odoo.urlopen") as opened:
        assert main(["--preflight-only"], {"ODOO_API_KEY": ""}, progress) == 1

    opened.assert_not_called()
    error = capsys.readouterr().err
    assert "stage preflight-configuration" in error
    assert "ODOO_API_KEY" in error


def test_preflight_failure_stops_before_probe_and_seed() -> None:
    progress, _ = _progress()

    with (
        patch("scripts.spike_s1_odoo.check_login_page"),
        patch.object(OdooClient, "call", side_effect=OdooApiError("HTTP 401")) as call,
        patch("scripts.spike_s1_odoo.inspect_odoo") as inspect,
        patch("scripts.spike_s1_odoo.seed_aged_cart") as seed,
    ):
        assert main(["--seed-aged-cart"], ENV, progress) == 1

    assert call.call_count == 1
    inspect.assert_not_called()
    seed.assert_not_called()


def test_preflight_makes_read_only_calls(capsys: pytest.CaptureFixture[str]) -> None:
    progress, _ = _progress()

    with (
        patch("scripts.spike_s1_odoo.check_login_page"),
        patch.object(OdooClient, "call", return_value=2) as call,
    ):
        assert main(["--preflight-only"], ENV, progress) == 0

    assert [item.args[:2] for item in call.call_args_list] == [("website", "search_count")]
    assert "does not verify" in capsys.readouterr().out


def test_probe_and_seed_run_in_order_after_preflight() -> None:
    progress, _ = _progress()
    order: list[str] = []

    def record(name: str, result: object) -> Callable[..., object]:
        def run(*_: object) -> object:
            order.append(name)
            return result

        return run

    with (
        patch("scripts.spike_s1_odoo.check_login_page", side_effect=record("ready", None)),
        patch.object(OdooClient, "call", side_effect=record("api", 1)),
        patch("scripts.spike_s1_odoo.inspect_odoo", side_effect=record("probe", ([], []))),
        patch("scripts.spike_s1_odoo.seed_aged_cart", side_effect=record("seed", 1)),
    ):
        assert main(["--seed-aged-cart"], ENV, progress) == 0

    assert order == ["ready", "api", "probe", "seed"]


def test_progress_output_hides_api_key(capsys: pytest.CaptureFixture[str]) -> None:
    progress, stream = _progress()

    with (
        patch("scripts.spike_s1_odoo.check_login_page"),
        patch.object(OdooClient, "call", side_effect=OdooApiError("HTTP 401")),
    ):
        main([], ENV, progress)

    captured = capsys.readouterr()
    assert API_KEY not in stream.getvalue() + captured.out + captured.err


def test_wait_ready_succeeds_after_retry() -> None:
    attempts: list[str] = []

    def check(_: str) -> None:
        attempts.append("try")
        if len(attempts) < 3:
            raise PreflightError("not yet")

    sleeps: list[float] = []
    wait_until_ready("http://localhost:8069", 60, check, sleeps.append, lambda: 0.0)

    assert len(attempts) == 3
    assert sleeps == [READY_POLL_SECONDS] * 2


def test_wait_ready_times_out_with_stage(capsys: pytest.CaptureFixture[str]) -> None:
    progress, _ = _progress()
    now = {"value": 0.0}

    def sleep(seconds: float) -> None:
        now["value"] += seconds

    with patch(
        "scripts.spike_s1_odoo.check_login_page", side_effect=PreflightError("connection refused")
    ):
        with pytest.raises(PreflightError, match="not ready within 10 seconds"):
            wait_until_ready(
                "http://localhost:8069",
                10,
                lambda url: (_ for _ in ()).throw(PreflightError("connection refused")),
                sleep,
                lambda: now["value"],
            )
        assert main(["--preflight-only"], ENV, progress) == 1

    assert "stage preflight-readiness" in capsys.readouterr().err


def test_preflight_cannot_be_combined_with_seed() -> None:
    with pytest.raises(SystemExit):
        main(["--preflight-only", "--seed-aged-cart"], ENV)
