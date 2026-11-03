from __future__ import annotations

import argparse
import os
import sys

from engine.channels import ChannelError, TelegramBotClient
from engine.worker import WorkerChannelDispatcher


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Run the bounded S4 Telegram test.")
    parser.add_argument(
        "--confirm-dedicated-test-chat",
        action="store_true",
        help="approve one fixed synthetic message to TELEGRAM_TEST_CHAT_ID",
    )
    args = parser.parse_args(argv)
    if not args.confirm_dedicated_test_chat:
        print(
            "Refusing live send: pass --confirm-dedicated-test-chat only after verifying "
            "the configured chat is dedicated to testing.",
            file=sys.stderr,
        )
        return 2

    token = os.environ.get("TELEGRAM_BOT_TOKEN", "")
    chat_id = os.environ.get("TELEGRAM_TEST_CHAT_ID", "")
    if not token:
        print("Preflight failed: TELEGRAM_BOT_TOKEN is not set.", file=sys.stderr)
        return 2
    if not chat_id:
        print("Preflight failed: TELEGRAM_TEST_CHAT_ID is not set.", file=sys.stderr)
        return 2

    print("Preflight passed; sending one fixed synthetic message to the approved test chat.")
    try:
        dispatcher = WorkerChannelDispatcher(TelegramBotClient(token))
        message_id = dispatcher.send_s4_live_test(chat_id=chat_id, approved=True)
    except ChannelError as exc:
        print(f"Telegram live check failed: {exc}", file=sys.stderr)
        return 1
    print(f"Telegram accepted the fixed synthetic test message (message ID {message_id}).")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
