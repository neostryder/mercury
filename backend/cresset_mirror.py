"""Mercury uses the Cresset sender registry when CRESSET_URL and CRESSET_MERCURY_TOKEN are set and no local choice is saved. CRESSET_MERCURY_TOKEN is a send-scope source token and goes only in the Authorization header.

A proposal becomes a decision with Approve and Discard options and external_ref `mercury:<brief id>:p<N>`, where N is the brief's turn count when the proposal was sent. A flagged-message report becomes a decision with its five options and external_ref `mercury:<brief id>:report`. The digest is posted after the email as a `digest` event whose body is the plain-text email, capped at 8000 characters, with fingerprint `mercury-digest-<date>` so a retry merges into the same event.

Approval prompts and the digest reach Telegram and SMTP first. Flagged-message reports try Cresset first in `cresset` mode and fall back to Telegram on failure. Failed posts are logged as `cresset_mirror_failed`."""
import asyncio
import hashlib
import hmac
import json
import logging
import os
import re
import sys
import time
from datetime import datetime, timezone
from html.parser import HTMLParser

import httpx

import alert_settings
import event_log

logger = logging.getLogger(__name__)

PROJECT = "mercury"
USER_AGENT = "mercury-backend (cresset mirror)"
SENDER_CONFIG_TTL = 600
SENDER_CONFIG_TIMEOUT = 1.5
CALLBACK_MAX_AGE_SECONDS = 600
DEFAULT_CALLBACK_URL = "https://mercury-ingest.rpgm.tools/cresset/callback"

_sender_config: tuple[tuple[str, bytes], float, str] | None = None

PROPOSAL_OPTIONS = [
    {"id": "approve", "label": "Approve"},
    {"id": "discard", "label": "Discard"},
]
REPORT_OPTIONS = [
    {"id": "unsubscribe", "label": "Unsubscribe"},
    {"id": "soft", "label": "Soft-bounce"},
    {"id": "hard", "label": "Hard-bounce"},
    {"id": "deliver", "label": "Deliver + whitelist"},
    {"id": "dismiss", "label": "Do nothing"},
]
REPORT_OPTIONS_DELIVERED = [
    option if option["id"] != "deliver" else {"id": "deliver", "label": "Whitelist"}
    for option in REPORT_OPTIONS
]


def _url() -> str:
    return os.environ.get("CRESSET_URL", "").strip().rstrip("/")


def _token() -> str:
    return os.environ.get("CRESSET_MERCURY_TOKEN", "").strip()


def _callback_url() -> str:
    return os.environ.get("MERCURY_CRESSET_CALLBACK_URL", DEFAULT_CALLBACK_URL).strip()


def _under_test() -> bool:
    argv0 = (sys.argv[0] if sys.argv else "").replace("\\", "/").lower()
    return bool(
        os.environ.get("PYTEST_CURRENT_TEST")
        or "pytest" in sys.modules
        or "/unittest" in argv0
        or argv0.endswith("unittest")
    )


async def delivery_mode() -> str:
    # A test run can inherit live Cresset credentials, so tests must opt in before using them.
    if _under_test() and os.environ.get("MERCURY_CRESSET_ALLOW_UNDER_TEST") != "1":
        return "off"
    url, token = _url(), _token()
    if not url or not token:
        return "off"
    saved = alert_settings.saved_mode()
    if saved is not None:
        return saved

    global _sender_config
    key = (url, hashlib.sha256(token.encode()).digest())
    cached = _sender_config
    stale = None
    if cached is not None and cached[0] == key:
        _, expires_at, stale = cached
        if time.monotonic() < expires_at:
            return stale

    try:
        async with asyncio.timeout(SENDER_CONFIG_TIMEOUT):
            async with httpx.AsyncClient(timeout=SENDER_CONFIG_TIMEOUT) as client:
                response = await client.get(
                    f"{url}/v1/sender-config",
                    params={"sender": PROJECT},
                    headers={"Authorization": f"Bearer {token}", "User-Agent": USER_AGENT},
                )
                response.raise_for_status()
                data = response.json()
    except (httpx.HTTPError, httpx.InvalidURL, OSError, ValueError):
        return stale or alert_settings.environment_mode()

    if isinstance(data, dict) and data.get("sender") == PROJECT:
        mode = data.get("mode")
        if mode in ("direct", "both", "cresset"):
            mode = "off" if mode == "direct" else mode
            _sender_config = (key, time.monotonic() + SENDER_CONFIG_TTL, mode)
            return mode
    return stale or alert_settings.environment_mode()


def _record_failure(what: str, detail: str) -> None:
    logger.warning("Cresset mirror %s failed: %s", what, detail)
    event_log.log_event("admin_log", {
        "at": datetime.now(timezone.utc).isoformat(),
        "event": "cresset_mirror_failed",
        "detail": f"{what}: {detail}"[:300],
    })


_CONTROL = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")


def _clip(text: str, limit: int) -> str:
    text = _CONTROL.sub("", str(text))
    text = " ".join(text.split()) if limit <= 200 else text.strip()
    return text if len(text) <= limit else text[: limit - 1].rstrip() + "."


async def _post(path: str, payload: dict) -> bool:
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            response = await client.post(
                f"{_url()}{path}",
                json=payload,
                headers={"Authorization": f"Bearer {_token()}", "User-Agent": USER_AGENT},
            )
            response.raise_for_status()
            return True
    except httpx.HTTPStatusError as exc:
        _record_failure(path, f"HTTP {exc.response.status_code}")
    except Exception as exc:
        _record_failure(path, type(exc).__name__)
    return False


def proposal_ref(brief_id: str, turn_count: int) -> str:
    return f"mercury:{brief_id}:p{turn_count}"


def report_ref(brief_id: str) -> str:
    return f"mercury:{brief_id}:report"


async def ask_proposal(brief_id: str, turn_count: int, text: str) -> None:
    if await delivery_mode() == "off":
        return
    await _post("/v1/decisions", {
        "project": PROJECT,
        "title": "Mercury proposal needs approval",
        "description": _clip(text, 1000),
        "options": PROPOSAL_OPTIONS,
        "external_ref": proposal_ref(brief_id, turn_count),
        "callback_url": _callback_url(),
    })


async def ask_report(brief_id: str, text: str, already_delivered: bool, *, mode: str) -> bool:
    if mode == "off":
        return False
    return await _post("/v1/decisions", {
        "project": PROJECT,
        "title": "Mercury flagged a message",
        "description": _clip(text, 1000),
        "options": REPORT_OPTIONS_DELIVERED if already_delivered else REPORT_OPTIONS,
        "external_ref": report_ref(brief_id),
        "callback_url": _callback_url(),
    })


class _Text(HTMLParser):
    BREAKS = {"p", "div", "br", "tr", "li", "h1", "h2", "h3", "h4", "table", "ul", "ol"}

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self._skip = 0

    def handle_starttag(self, tag, attrs):
        if tag in ("style", "script", "head"):
            self._skip += 1
        elif tag in self.BREAKS:
            self.parts.append("\n")

    def handle_endtag(self, tag):
        if tag in ("style", "script", "head"):
            self._skip = max(0, self._skip - 1)
        elif tag in self.BREAKS:
            self.parts.append("\n")
        elif tag in ("td", "th"):
            self.parts.append(" ")

    def handle_data(self, data):
        if not self._skip:
            self.parts.append(data)


def html_to_text(html: str) -> str:
    parser = _Text()
    parser.feed(html)
    parser.close()
    lines = [" ".join(line.split()) for line in "".join(parser.parts).splitlines()]
    return "\n".join(line for line in lines if line)


async def send_digest(subject: str, html_body: str, day: str) -> None:
    if await delivery_mode() == "off":
        return
    text = html_to_text(html_body)
    await _post("/v1/events", {
        "host": "mercury",
        "component": "digest",
        "class": "daily",
        "severity": "info",
        "kind": "digest",
        "category": "digest",
        "project": PROJECT,
        "title": _clip(subject, 120),
        "description": _clip(text, 400) or _clip(subject, 400),
        "body": _clip(text, 8000) or subject,
        "fingerprint": f"mercury-digest-{day}",
    })


def verify_callback(body: bytes, timestamp: str | None, signature: str | None, now: float | None = None) -> bool:
    token = _token()
    if not token or not timestamp or not signature:
        return False
    try:
        age = abs((time.time() if now is None else now) - int(timestamp))
    except ValueError:
        return False
    if age > CALLBACK_MAX_AGE_SECONDS:
        return False
    key = hashlib.sha256(token.encode()).hexdigest().encode()
    expected = "sha256=" + hmac.new(
        key, f"{timestamp}.".encode() + body, hashlib.sha256
    ).hexdigest()
    return hmac.compare_digest(expected, signature)


def parse_ref(ref: str | None) -> tuple[str, str] | None:
    parts = (ref or "").split(":")
    if len(parts) != 3 or parts[0] != "mercury" or not parts[1] or not parts[2]:
        return None
    return parts[1], parts[2]


def parse_callback_body(body: bytes) -> dict | None:
    try:
        data = json.loads(body)
    except ValueError:
        return None
    return data if isinstance(data, dict) else None
