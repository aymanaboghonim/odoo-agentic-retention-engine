from __future__ import annotations

from engine.channels import ChannelError, TelegramMessageSender

S4_LIVE_TEST_MESSAGE = "S4 channel verification: synthetic test only. No customer data."


class WorkerChannelDispatcher:
    def __init__(self, telegram: TelegramMessageSender) -> None:
        self._telegram = telegram

    def send_s4_live_test(self, *, chat_id: str, approved: bool) -> int:
        if not approved:
            raise ChannelError("Explicit approval of the dedicated Telegram test chat is required.")
        if not chat_id:
            raise ChannelError("TELEGRAM_TEST_CHAT_ID must be set.")
        return self._telegram.send_message(chat_id, S4_LIVE_TEST_MESSAGE)
