"""Forward taps on Mercury's Telegram buttons to Mercury's relay endpoint.

The gateway holds the shared bot's only getUpdates connection, so Mercury
cannot see taps on its own approval cards. This plugin adds an observer
handler to every python-telegram-bot Application the gateway initializes and
POSTs the few fields Mercury reads to https://mercury.rpgm.tools/telegram/relay.
Text replies are not forwarded: Loremaster answers them, and proposes rule
changes through Mercury itself, so forwarding them would handle each reply twice.

The handler is attached in Application.initialize rather than on the Hermes
Telegram adapter class: Hermes imports the adapter under a hermes_plugins.*
module name, so importing plugins.platforms.telegram.adapter here would patch
a second copy of the class that the gateway never uses.

Needs MERCURY_RELAY_SECRET in the environment (it comes from Bitwarden at
gateway start). The Worker checks it and adds the backend secret itself.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import urllib.request
from typing import Any

logger = logging.getLogger(__name__)
_RELAY_URL = "https://mercury.rpgm.tools/telegram/relay"
# callback_data prefixes on Mercury's own cards (backend/telegram_approvals.py).
_MERCURY_CALLBACK_KINDS = ("approve", "discard", "decision")
# PTB runs only the first matching handler in a group, and the Overseer relay registers a
# catch-all TypeHandler in group 100, so Mercury needs a group of its own.
_HANDLER_GROUP = 101
_PATCHED = False
_warned_missing_secret = False
# Heartbeat to Mercury so a silent break (a missing plugin, a missing secret, or another
# plugin's handler running first in our group) raises an alert instead of going unnoticed.
_HEARTBEAT_SECONDS = 300
_current_app: Any = None
_heartbeat_task: Any = None
_updates_seen = 0


def _chat_id(update: Any) -> str | None:
    chat = getattr(update, "effective_chat", None)
    chat_id = getattr(chat, "id", None) if chat is not None else None
    return str(chat_id) if chat_id is not None else None


def _payload(update: Any) -> dict[str, Any] | None:
    chat_id = _chat_id(update)
    if chat_id is None:
        return None

    callback = getattr(update, "callback_query", None)
    if callback is None:
        return None
    callback_id = getattr(callback, "id", None)
    data = getattr(callback, "data", None)
    if callback_id is None or not isinstance(data, str):
        return None
    if data.partition(":")[0] not in _MERCURY_CALLBACK_KINDS:
        return None
    return {"chat_id": chat_id, "callback_query": {"id": callback_id, "data": data}}


def _post(payload: dict[str, Any]) -> int | None:
    global _warned_missing_secret
    relay_secret = os.environ.get("MERCURY_RELAY_SECRET")
    if not relay_secret:
        if not _warned_missing_secret:
            logger.warning("Mercury Telegram relay skipped: MERCURY_RELAY_SECRET is not set in the gateway environment")
            _warned_missing_secret = True
        return None
    request = urllib.request.Request(
        os.environ.get("MERCURY_TELEGRAM_RELAY_URL", _RELAY_URL),
        data=json.dumps(payload, separators=(",", ":")).encode(),
        method="POST",
        headers={
            "Content-Type": "application/json",
            "User-Agent": "Mozilla/5.0 (loremaster-mercury-telegram-relay)",
            "X-Mercury-Relay-Secret": relay_secret,
        },
    )
    with urllib.request.urlopen(request, timeout=30) as response:
        response.read()
        return response.status


async def _forward(update: Any, _context: Any) -> None:
    global _updates_seen
    _updates_seen += 1
    callback = getattr(update, "callback_query", None)
    if callback is not None:
        data = getattr(callback, "data", None)
        kind_seen = data.partition(":")[0] if isinstance(data, str) else "?"
        logger.info("Mercury Telegram relay saw a callback query (kind %s)", kind_seen[:24])
    payload = _payload(update)
    if payload is None:
        return
    kind = "callback"
    try:
        status = await asyncio.to_thread(_post, payload)
        if status is not None:
            logger.info("Mercury Telegram relay forwarded a %s (HTTP %s)", kind, status)
    except Exception as exc:
        # A relay failure must not delay or break normal agent handling.
        logger.warning("Mercury Telegram relay failed for a %s: %s", kind, type(exc).__name__)


def _handler_first(app: Any) -> bool:
    """True when our handler is first in its group, so PTB runs it on every update."""
    handlers = app.handlers.get(_HANDLER_GROUP, ())
    if not handlers:
        return False
    callback = getattr(handlers[0], "callback", None)
    return callback is _forward or getattr(callback, "__wrapped__", None) is _forward


def _health_report() -> dict[str, Any]:
    app = _current_app
    return {
        "handler_first": bool(app is not None and _handler_first(app)),
        "secret_set": bool(os.environ.get("MERCURY_RELAY_SECRET")),
        "updates_seen": _updates_seen,
    }


async def _heartbeat_loop() -> None:
    await asyncio.sleep(30)
    while True:
        try:
            await asyncio.to_thread(_post, {"relay_health": _health_report()})
        except Exception as exc:
            logger.warning("Mercury Telegram relay heartbeat failed: %s", type(exc).__name__)
        await asyncio.sleep(_HEARTBEAT_SECONDS)


def _start_heartbeat(app: Any) -> None:
    global _current_app, _heartbeat_task
    _current_app = app
    if _heartbeat_task is None or _heartbeat_task.done():
        _heartbeat_task = asyncio.get_running_loop().create_task(_heartbeat_loop())


def _attach(app: Any) -> None:
    # python-telegram-bot Applications use __slots__, so a flag cannot be set on the
    # object. An already attached relay shows up as our callback in the handler group.
    from telegram import Update
    from telegram.ext import TypeHandler

    for handler in app.handlers.get(_HANDLER_GROUP, ()):
        callback = getattr(handler, "callback", None)
        # Hermes wraps every handler callback (functools.wraps), so look through __wrapped__.
        if callback is _forward or getattr(callback, "__wrapped__", None) is _forward:
            _start_heartbeat(app)
            return
    app.add_handler(TypeHandler(Update, _forward), group=_HANDLER_GROUP)
    _start_heartbeat(app)
    logger.info("Mercury Telegram relay handler attached")


def register(_ctx: Any) -> None:
    global _PATCHED
    if _PATCHED:
        return
    try:
        from telegram.ext import Application

        original = Application.initialize
        if getattr(original, "_mercury_relay_wrapped", False):
            _PATCHED = True
            return

        async def initialize(self: Any, *args: Any, **kwargs: Any) -> Any:
            try:
                _attach(self)
            except Exception:
                logger.warning("Mercury Telegram relay handler could not be attached", exc_info=True)
            return await original(self, *args, **kwargs)

        initialize._mercury_relay_wrapped = True
        Application.initialize = initialize
        _PATCHED = True
        logger.info("Mercury Telegram relay hook installed on telegram.ext.Application.initialize")
    except Exception:
        logger.warning("Mercury Telegram relay hook could not be installed", exc_info=True)
