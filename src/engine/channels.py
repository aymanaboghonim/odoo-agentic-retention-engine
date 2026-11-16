from __future__ import annotations

import ipaddress
import json
import smtplib
import socket
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from email.message import EmailMessage
from typing import Protocol
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

GET_UPDATES_LONG_POLL_SECONDS = 5


class ChannelError(RuntimeError):
    pass


class TelegramUpdateSource(Protocol):
    def get_webhook_info(self) -> Mapping[str, object]: ...

    def get_updates(self, offset: int | None = None) -> list[Mapping[str, object]]: ...


class TelegramMessageSender(Protocol):
    def send_message(self, chat_id: str, text: str) -> int: ...


class ChannelPort(TelegramUpdateSource, TelegramMessageSender, Protocol):
    pass


class EmailPort(Protocol):
    def send(self, message: EmailMessage) -> None: ...


@dataclass(frozen=True)
class InboundEvent:
    update_id: int
    message_id: int
    chat_id: str
    text: str


class TelegramBotClient:
    def __init__(self, token: str, timeout: float = 10.0) -> None:
        if not token or token.strip() != token:
            raise ChannelError("TELEGRAM_BOT_TOKEN must be set without surrounding whitespace.")
        if timeout <= 0:
            raise ChannelError("Telegram request timeout must be positive.")
        self._token = token
        self._timeout = timeout

    def _call(self, method: str, payload: Mapping[str, object] | None = None) -> object:
        url = f"https://api.telegram.org/bot{self._token}/{method}"
        body = json.dumps(payload or {}).encode("utf-8")
        request = Request(
            url,
            data=body,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        try:
            with urlopen(request, timeout=self._timeout) as response:
                response_data = json.loads(response.read().decode("utf-8"))
        except HTTPError as exc:
            status = exc.code
            exc.close()
            raise ChannelError(f"Telegram {method} failed with HTTP {status}.") from None
        except (URLError, TimeoutError, OSError) as exc:
            raise ChannelError(
                f"Telegram {method} request failed ({type(exc).__name__})."
            ) from None
        except UnicodeDecodeError, json.JSONDecodeError:
            raise ChannelError(f"Telegram {method} returned invalid JSON.") from None

        if not isinstance(response_data, dict) or response_data.get("ok") is not True:
            raise ChannelError(f"Telegram {method} returned an unsuccessful response.")
        if "result" not in response_data:
            raise ChannelError(f"Telegram {method} response did not include a result.")
        return response_data["result"]

    def get_webhook_info(self) -> Mapping[str, object]:
        result = self._call("getWebhookInfo")
        if not isinstance(result, dict):
            raise ChannelError("Telegram returned an invalid webhook status.")
        return result

    def get_updates(self, offset: int | None = None) -> list[Mapping[str, object]]:
        if self._timeout <= GET_UPDATES_LONG_POLL_SECONDS:
            raise ChannelError("Telegram HTTP timeout must exceed the long-poll timeout.")
        payload: dict[str, object] = {"timeout": GET_UPDATES_LONG_POLL_SECONDS}
        if offset is not None:
            payload["offset"] = offset
        result = self._call("getUpdates", payload)
        if not isinstance(result, list) or not all(isinstance(item, dict) for item in result):
            raise ChannelError("Telegram returned an invalid updates list.")
        return result

    def send_message(self, chat_id: str, text: str) -> int:
        if not chat_id:
            raise ChannelError("Telegram chat ID must be set.")
        if not text:
            raise ChannelError("Telegram message text must not be empty.")
        result = self._call("sendMessage", {"chat_id": chat_id, "text": text})
        if not isinstance(result, dict) or type(result.get("message_id")) is not int:
            raise ChannelError("Telegram returned an invalid sent-message result.")
        return result["message_id"]


class TelegramUpdatePoller:
    def __init__(self, client: TelegramUpdateSource) -> None:
        self._client = client
        self._next_offset: int | None = None

    def poll_once(self, event_sink: Callable[[InboundEvent], None]) -> int:
        webhook = self._client.get_webhook_info()
        webhook_url = webhook.get("url")
        if not isinstance(webhook_url, str):
            raise ChannelError("Telegram returned an invalid webhook URL.")
        if webhook_url:
            raise ChannelError("Telegram polling is unavailable while a webhook is configured.")

        updates = self._client.get_updates(self._next_offset)
        delivered = 0
        for update in updates:
            update_id = update.get("update_id")
            if type(update_id) is not int:
                raise ChannelError("Telegram update did not include a valid update ID.")

            message = update.get("message")
            if isinstance(message, dict):
                chat = message.get("chat")
                text = message.get("text")
                message_id = message.get("message_id")
                if (
                    isinstance(chat, dict)
                    and type(chat.get("id")) in (int, str)
                    and type(message_id) is int
                    and isinstance(text, str)
                ):
                    event_sink(
                        InboundEvent(
                            update_id=update_id,
                            message_id=message_id,
                            chat_id=str(chat["id"]),
                            text=text,
                        )
                    )
                    delivered += 1
            self._next_offset = update_id + 1
        return delivered


def _private_service(host: str) -> bool:
    """A Compose service name that resolves only to private, non-public addresses."""
    if not host or "." in host:
        return False
    try:
        infos = socket.getaddrinfo(host, None)
    except OSError:
        return False
    addresses = {ipaddress.ip_address(info[4][0]) for info in infos}
    return bool(addresses) and all(
        address.is_private and not address.is_loopback for address in addresses
    )


class MailpitEmailAdapter:
    def __init__(
        self,
        host: str = "127.0.0.1",
        port: int = 1025,
        timeout: float = 10.0,
        *,
        allow_private_service: bool = False,
    ) -> None:
        try:
            is_loopback = ipaddress.ip_address(host).is_loopback
        except ValueError:
            is_loopback = False
        if not is_loopback and not (allow_private_service and _private_service(host)):
            raise ChannelError("Mailpit SMTP host must be a loopback IP address.")
        if not 1 <= port <= 65535:
            raise ChannelError("Mailpit SMTP port must be between 1 and 65535.")
        if timeout <= 0:
            raise ChannelError("Mailpit SMTP timeout must be positive.")
        self._host = host
        self._port = port
        self._timeout = timeout

    def send(self, message: EmailMessage) -> None:
        try:
            with smtplib.SMTP(self._host, self._port, timeout=self._timeout) as client:
                refused = client.send_message(message)
        except OSError, smtplib.SMTPException:
            raise ChannelError("Mailpit SMTP delivery failed.") from None
        if refused:
            raise ChannelError("Mailpit refused one or more recipients.")
