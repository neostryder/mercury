"""Watches the Telegram relay that carries Approve and Discard taps to Mercury.

Mercury cannot hold its bot's getUpdates connection (the Hermes gateway does),
so a plugin in the gateway forwards each tap to /telegram/relay. When that plugin
stops forwarding, nothing at Mercury's end fails: taps just never arrive. The
plugin sends a heartbeat every few minutes, and this module raises a Telegram
alert when the heartbeat goes missing or reports a problem, then a second
message when it recovers.

A heartbeat is a JSON object under the key "relay_health". Fields Mercury reads:
`handler_first` (the plugin's handler is first in its python-telegram-bot handler
group, so it runs on every update), `secret_set` (the relay secret is present in
the gateway), and `updates_seen` (updates the handler has run for since the
gateway started). Anything other than true for the first two is unhealthy.
"""
import asyncio
import logging
import time

logger = logging.getLogger(__name__)

# The plugin beats every 5 minutes; 20 minutes of silence is four missed beats.
STALE_AFTER_SECONDS = 20 * 60
CHECK_EVERY_SECONDS = 5 * 60
# After a backend restart the next beat may be minutes away, so staleness is not
# judged until this long after startup.
STARTUP_GRACE_SECONDS = 30 * 60

_started_at = time.monotonic()
_last_beat: float | None = None
_last_report: dict = {}
_alerting = False


def record_heartbeat(report: dict, now: float | None = None) -> None:
    global _last_beat, _last_report
    _last_beat = time.monotonic() if now is None else now
    _last_report = report if isinstance(report, dict) else {}


def problem(now: float | None = None) -> str | None:
    """Return a plain-language problem, or None when the relay looks healthy."""
    now = time.monotonic() if now is None else now
    if _last_beat is None:
        if now - _started_at < STARTUP_GRACE_SECONDS:
            return None
        return "no relay heartbeat has arrived since Mercury started"
    age = now - _last_beat
    if age > STALE_AFTER_SECONDS:
        return f"the last relay heartbeat was {int(age // 60)} minutes ago"
    if _last_report.get("handler_first") is not True:
        return ("the relay handler is not first in its Telegram handler group, "
                "so another plugin's handler can swallow taps")
    if _last_report.get("secret_set") is not True:
        return "the Hermes gateway has no relay secret, so the relay plugin cannot forward taps"
    return None


async def check_once(notifier, now: float | None = None) -> None:
    """Send one alert when a problem begins and one message when it ends."""
    global _alerting
    found = problem(now)
    if found and not _alerting:
        _alerting = True
        await notifier.send(
            f"Mercury relay problem: {found}. Approve and Discard taps may not "
            "reach Mercury until it is fixed. Check the relay plugin in your Hermes gateway."
        )
    elif not found and _alerting:
        _alerting = False
        await notifier.send("Mercury relay is working again. Approve and Discard taps reach Mercury.")


async def run_forever(notifier) -> None:
    while True:
        try:
            await check_once(notifier)
        except Exception:
            logger.exception("relay health check failed")
        await asyncio.sleep(CHECK_EVERY_SECONDS)
