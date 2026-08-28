"""Notification provider - where shadow reports and failure alerts go.

The built-in implementation sends to a Telegram chat, because that is what
reaches the mailbox owner directly and privately without extra setup. Swap
in a different provider (Slack, a different chat platform, email-to-self)
by implementing Notifier and changing the selection in get_notifier().
"""
import os
from typing import Protocol

import httpx


class Notifier(Protocol):
    async def send(self, text: str) -> None:
        """Deliver a plain-text message. Best-effort - callers already treat
        notification failures as non-fatal, since the whole point is to
        surface a problem, not to become a second point of failure.
        """


class TelegramNotifier:
    def __init__(self, bot_token: str, chat_id: str):
        self._bot_token = bot_token
        self._chat_id = chat_id

    async def send(self, text: str) -> None:
        async with httpx.AsyncClient(timeout=15) as client:
            await client.post(
                f"https://api.telegram.org/bot{self._bot_token}/sendMessage",
                json={
                    "chat_id": self._chat_id,
                    "text": text,
                    "disable_web_page_preview": True,
                },
            )


def get_notifier() -> Notifier:
    return TelegramNotifier(
        bot_token=os.environ["TELEGRAM_BOT_TOKEN"],
        chat_id=os.environ["TELEGRAM_CHAT_ID"],
    )
