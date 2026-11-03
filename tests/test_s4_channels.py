from __future__ import annotations

import ast
import io
import json
from collections.abc import Mapping
from email.message import EmailMessage, Message
from pathlib import Path
from urllib.error import HTTPError
from urllib.request import Request

import pytest

from engine import channels
from engine.channels import (
    ChannelError,
    InboundEvent,
    MailpitEmailAdapter,
    TelegramBotClient,
    TelegramUpdatePoller,
)
from engine.worker import S4_LIVE_TEST_MESSAGE, WorkerChannelDispatcher
from scripts import s4_channels


class FakeTelegram:
    def __init__(
        self,
        webhook: Mapping[str, object] | None = None,
        updates: list[Mapping[str, object]] | None = None,
    ) -> None:
        self.webhook = webhook or {"url": ""}
        self.updates = updates or []
        self.offsets: list[int | None] = []

    def get_webhook_info(self) -> Mapping[str, object]:
        return self.webhook

    def get_updates(self, offset: int | None = None) -> list[Mapping[str, object]]:
        self.offsets.append(offset)
        return self.updates


def test_telegram_client_uses_https_and_keeps_token_out_of_errors(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    token = "123456:secret-token"
    captured: list[Request] = []

    def fake_urlopen(request: Request, timeout: float) -> io.BytesIO:
        del timeout
        captured.append(request)
        return io.BytesIO(b'{"ok":true,"result":{"message_id":7}}')

    monkeypatch.setattr(channels, "urlopen", fake_urlopen)
    client = TelegramBotClient(token)

    assert client.send_message("test-chat", "synthetic") == 7
    assert captured[0].full_url == f"https://api.telegram.org/bot{token}/sendMessage"
    body = captured[0].data
    assert isinstance(body, bytes)
    assert json.loads(body) == {
        "chat_id": "test-chat",
        "text": "synthetic",
    }

    def rejected_urlopen(request: Request, timeout: float) -> io.BytesIO:
        del timeout
        raise HTTPError(request.full_url, 403, "Forbidden", Message(), io.BytesIO())

    monkeypatch.setattr(channels, "urlopen", rejected_urlopen)
    with pytest.raises(ChannelError) as error:
        client.send_message("test-chat", "synthetic")
    assert token not in str(error.value)


def test_telegram_rejects_unsuccessful_or_malformed_responses(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    responses = [
        b'{"ok":false,"description":"failed"}',
        b'{"ok":true}',
        b"not-json",
    ]

    def fake_urlopen(request: Request, timeout: float) -> io.BytesIO:
        del request, timeout
        return io.BytesIO(responses.pop(0))

    monkeypatch.setattr(channels, "urlopen", fake_urlopen)
    client = TelegramBotClient("123456:token")

    with pytest.raises(ChannelError, match="unsuccessful"):
        client.get_webhook_info()
    with pytest.raises(ChannelError, match="did not include a result"):
        client.get_webhook_info()
    with pytest.raises(ChannelError, match="invalid JSON"):
        client.get_webhook_info()


def test_telegram_updates_use_bounded_long_polling(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: list[Request] = []

    def fake_urlopen(request: Request, timeout: float) -> io.BytesIO:
        del timeout
        captured.append(request)
        return io.BytesIO(b'{"ok":true,"result":[]}')

    monkeypatch.setattr(channels, "urlopen", fake_urlopen)
    client = TelegramBotClient("123456:token")

    assert client.get_updates(offset=19) == []
    request_body = captured[0].data
    assert isinstance(request_body, bytes)
    assert json.loads(request_body) == {"timeout": 5, "offset": 19}


def test_poller_forwards_normalized_events_and_advances_offset() -> None:
    client = FakeTelegram(
        updates=[
            {
                "update_id": 41,
                "message": {"message_id": 5, "chat": {"id": 123}, "text": "hello"},
            },
            {"update_id": 42, "callback_query": {"id": "ignored"}},
        ]
    )
    poller = TelegramUpdatePoller(client)
    received: list[InboundEvent] = []

    assert poller.poll_once(received.append) == 1
    assert received == [InboundEvent(update_id=41, message_id=5, chat_id="123", text="hello")]
    assert poller.poll_once(received.append) == 1
    assert client.offsets == [None, 43]


def test_poller_fails_closed_when_webhook_is_configured() -> None:
    client = FakeTelegram(webhook={"url": "https://example.invalid/hook"})
    poller = TelegramUpdatePoller(client)

    with pytest.raises(ChannelError, match="while a webhook is configured"):
        poller.poll_once(lambda event: None)
    assert client.offsets == []


def test_poller_does_not_acknowledge_an_event_the_ingress_rejects() -> None:
    client = FakeTelegram(
        updates=[
            {
                "update_id": 9,
                "message": {"message_id": 2, "chat": {"id": 123}, "text": "synthetic"},
            }
        ]
    )
    poller = TelegramUpdatePoller(client)

    def reject_ingress(event: InboundEvent) -> None:
        del event
        raise RuntimeError("ingress unavailable")

    with pytest.raises(RuntimeError, match="ingress unavailable"):
        poller.poll_once(reject_ingress)
    assert poller.poll_once(lambda event: None) == 1
    assert client.offsets == [None, None]


def test_worker_dispatch_requires_approval_and_uses_fixed_message() -> None:
    class SendOnlyTelegram:
        def __init__(self) -> None:
            self.sent: list[tuple[str, str]] = []

        def send_message(self, chat_id: str, text: str) -> int:
            self.sent.append((chat_id, text))
            return 12

    telegram = SendOnlyTelegram()
    dispatcher = WorkerChannelDispatcher(telegram)

    with pytest.raises(ChannelError, match="Explicit approval"):
        dispatcher.send_s4_live_test(chat_id="dedicated-chat", approved=False)
    assert telegram.sent == []

    assert dispatcher.send_s4_live_test(chat_id="dedicated-chat", approved=True) == 12
    assert telegram.sent == [("dedicated-chat", S4_LIVE_TEST_MESSAGE)]


def test_only_worker_dispatches_telegram_messages() -> None:
    root = Path(__file__).resolve().parents[1]
    source_files = [
        root / "src" / "engine" / "channels.py",
        root / "src" / "engine" / "worker.py",
        root / "scripts" / "s4_channels.py",
    ]
    dispatch_calls: list[Path] = []
    for path in source_files:
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Attribute)
                and node.func.attr == "send_message"
            ):
                if (
                    path.name == "channels.py"
                    and isinstance(node.func.value, ast.Name)
                    and node.func.value.id == "client"
                ):
                    continue
                dispatch_calls.append(path)

    assert dispatch_calls == [root / "src" / "engine" / "worker.py"]


def test_live_cli_refuses_without_confirmation(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    token = "123456:secret-token"
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", token)
    monkeypatch.setenv("TELEGRAM_TEST_CHAT_ID", "dedicated-chat")
    monkeypatch.setattr(
        s4_channels,
        "TelegramBotClient",
        lambda value: pytest.fail(f"client created without approval: {value}"),
    )

    assert s4_channels.main([]) == 2
    output = capsys.readouterr()
    assert token not in output.out + output.err


def test_mailpit_adapter_rejects_non_loopback_and_sends_email(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: list[tuple[str, int, str]] = []

    class FakeSMTP:
        def __init__(self, host: str, port: int, timeout: float) -> None:
            del timeout
            captured.append((host, port, "opened"))

        def __enter__(self) -> FakeSMTP:
            return self

        def __exit__(self, *args: object) -> None:
            del args

        def send_message(self, message: EmailMessage) -> dict[str, object]:
            captured.append(("message", 0, str(message["To"])))
            return {}

    monkeypatch.setattr(channels.smtplib, "SMTP", FakeSMTP)
    with pytest.raises(ChannelError, match="loopback"):
        MailpitEmailAdapter("mailpit")

    message = EmailMessage()
    message["To"] = "synthetic@example.invalid"
    message.set_content("synthetic")
    MailpitEmailAdapter().send(message)
    assert captured == [
        ("127.0.0.1", 1025, "opened"),
        ("message", 0, "synthetic@example.invalid"),
    ]


def test_mailpit_adapter_reports_failed_delivery(monkeypatch: pytest.MonkeyPatch) -> None:
    class FailedSMTP:
        def __init__(self, host: str, port: int, timeout: float) -> None:
            del host, port, timeout

        def __enter__(self) -> FailedSMTP:
            return self

        def __exit__(self, *args: object) -> None:
            del args

        def send_message(self, message: EmailMessage) -> dict[str, object]:
            del message
            raise OSError("connection refused")

    monkeypatch.setattr(channels.smtplib, "SMTP", FailedSMTP)
    with pytest.raises(ChannelError, match="Mailpit SMTP delivery failed"):
        MailpitEmailAdapter().send(EmailMessage())
