"""This module copies Mercury's Telegram approval prompts and the daily digest to Cresset, the owner's notification hub, as a second delivery path alongside the existing Telegram and SMTP sends. It is active only when MERCURY_CRESSET_MODE is `both` and both CRESSET_URL and CRESSET_MERCURY_TOKEN are set. CRESSET_MERCURY_TOKEN is a send-scope source token and goes only in the Authorization header.

A proposal becomes a decision with Approve and Discard options and external_ref `mercury:<brief id>:p<N>`, where N is the brief's turn count when the proposal was sent. A flagged-message report becomes a decision with its five options and external_ref `mercury:<brief id>:report`. The digest is posted after the email as a `digest` event whose body is the plain-text email, capped at 8000 characters, with fingerprint `mercury-digest-<date>` so a retry merges into the same event.

Every send is best-effort. A failure is written to the admin log as a `cresset_mirror_failed` event and is never raised, because the Telegram message or digest email has already been delivered."""
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
CALLBACK_MAX_AGE_SECONDS = 600
DEFAULT_CALLBACK_URL = "https://mercury-ingest.rpgm.tools/cresset/callback"

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


def enabled() -> bool:
    # A test run that inherits a real Cresset address and key must never post to it, so
    # under a test runner the mirror stays off unless a test opts in on purpose.
    if _under_test() and os.environ.get("MERCURY_CRESSET_ALLOW_UNDER_TEST") != "1":
        return False
    return alert_settings.current_mode() == "both" and bool(_url() and _token())


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


async def _post(path: str, payload: dict) -> dict | None:
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            response = await client.post(
                f"{_url()}{path}",
                json=payload,
                headers={"Authorization": f"Bearer {_token()}", "User-Agent": USER_AGENT},
            )
            response.raise_for_status()
            return response.json()
    except httpx.HTTPStatusError as exc:
        _record_failure(path, f"HTTP {exc.response.status_code}")
    except Exception as exc:
        _record_failure(path, type(exc).__name__)
    return None


def proposal_ref(brief_id: str, turn_count: int) -> str:
    return f"mercury:{brief_id}:p{turn_count}"


def report_ref(brief_id: str) -> str:
    return f"mercury:{brief_id}:report"


async def ask_proposal(brief_id: str, turn_count: int, text: str) -> None:
    if not enabled():
        return
    await _post("/v1/decisions", {
        "project": PROJECT,
        "title": "Mercury proposal needs approval",
        "description": _clip(text, 1000),
        "options": PROPOSAL_OPTIONS,
        "external_ref": proposal_ref(brief_id, turn_count),
        "callback_url": _callback_url(),
    })


async def ask_report(brief_id: str, text: str, already_delivered: bool) -> None:
    if not enabled():
        return
    await _post("/v1/decisions", {
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
    if not enabled():
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
