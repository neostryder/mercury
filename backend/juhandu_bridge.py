"""Hands accepted newsletter mail to Juhandu, the feed reader, as reader items.

After an accepted message is in the mailbox, the parts Juhandu needs (sender,
subject, body, list headers) are posted as JSON to its mail door. The push is
strictly a copy: it runs as a background task, never changes or delays a
disposition, and a failed push loses only the Juhandu copy because the mail is
already delivered. A failed push waits in a small file under /data and is
retried with growing gaps for 24 hours, then dropped. Juhandu answers 200 for a
message it already holds, so a retry is always safe.

Off unless MERCURY_JUHANDU_PUSH is true and a URL and token are set. Shadow
mode never reaches this module, since it delivers nothing.
"""
import asyncio
import json
import logging
import os
import re
import time
import uuid
from email import policy
from email.parser import Parser
from email.utils import parseaddr, parsedate_to_datetime

import httpx

logger = logging.getLogger(__name__)

PUSH_ENABLED = os.environ.get("MERCURY_JUHANDU_PUSH", "false").lower() == "true"
# The mail door, for example http://host.docker.internal:8795/inbound/mail from
# inside the backend container, or the public hostname behind Cloudflare Access.
JUHANDU_URL = os.environ.get("MERCURY_JUHANDU_URL", "")
JUHANDU_TOKEN = os.environ.get("MERCURY_JUHANDU_TOKEN", "")
# Only needed when the URL is the public hostname.
ACCESS_ID = os.environ.get("MERCURY_JUHANDU_ACCESS_ID", "")
ACCESS_SECRET = os.environ.get("MERCURY_JUHANDU_ACCESS_SECRET", "")
CATEGORIES = {
    name.strip().upper()
    for name in os.environ.get("MERCURY_JUHANDU_CATEGORIES", "NEWSLETTER").split(",")
    if name.strip()
}
OUTBOX_PATH = os.environ.get("MERCURY_JUHANDU_OUTBOX_PATH", "/data/juhandu_outbox.json")

REQUEST_TIMEOUT_SECONDS = 15.0
# Juhandu refuses a body over 2 MB, so the payload is cut down to fit.
MAX_BODY_BYTES = 2 * 1024 * 1024 - 8 * 1024
RETRY_WINDOW_SECONDS = 24 * 60 * 60
RETRY_GAPS_SECONDS = (60, 120, 300, 900, 1800, 3600, 7200)
SWEEP_SECONDS = 60
OUTBOX_LIMIT = 200

# Replaced in tests with an httpx.MockTransport.
_transport: httpx.AsyncBaseTransport | None = None
# Strong references to pushes in flight, so a task is not collected mid-send.
_tasks: set[asyncio.Task] = set()

_LIST_TARGET = re.compile(r"<([^<>\s]+)>")


def enabled() -> bool:
    return bool(PUSH_ENABLED and JUHANDU_URL and JUHANDU_TOKEN)


def wants(category: str | None) -> bool:
    return enabled() and (category or "").upper() in CATEGORIES


def _clean(value) -> str:
    return " ".join(str(value or "").split())


def _part_text(message, kind: str) -> str:
    part = message.get_body(preferencelist=(kind,))
    if part is None:
        return ""
    try:
        return part.get_content() or ""
    except Exception:
        return ""


def build_payload(raw_message: str | None, verdict: str, category: str) -> dict | None:
    """The JSON Juhandu's mail door takes, built from the raw message, or None
    when the message cannot be an item: no Message-Id, no sender address, or no
    body at all."""
    if not raw_message:
        return None
    try:
        message = Parser(policy=policy.default).parsestr(raw_message)
        message_id = _clean(message.get("Message-ID"))
        name, address = parseaddr(_clean(message.get("From")))
        text = _part_text(message, "plain")
        html = _part_text(message, "html")
        subject = _clean(message.get("Subject"))
        list_id = _clean(message.get("List-ID"))
        list_unsubscribe = _clean(message.get("List-Unsubscribe"))
        sent = message.get("Date")
    except Exception:
        return None
    if not message_id or not address or not (text.strip() or html.strip()):
        return None

    payload: dict = {
        "messageId": message_id,
        "from": {"name": name, "address": address},
        "subject": subject,
        "category": category,
        "verdict": verdict,
    }
    if sent:
        try:
            payload["date"] = parsedate_to_datetime(str(sent)).isoformat()
        except Exception:
            pass
    if list_id:
        payload["listId"] = list_id
    targets = [
        target for target in _LIST_TARGET.findall(list_unsubscribe)
        if target.lower().startswith(("https://", "mailto:"))
    ]
    if targets:
        payload["listUnsubscribe"] = targets
    if text.strip():
        payload["text"] = text
    if html.strip():
        payload["html"] = html
    return _fit(payload)


def _size(payload: dict) -> int:
    return len(json.dumps(payload, ensure_ascii=False).encode("utf-8"))


def _fit(payload: dict) -> dict:
    """Cuts a payload to Juhandu's body limit: the HTML goes first (the text
    carries the summary), then the text is shortened."""
    if _size(payload) <= MAX_BODY_BYTES:
        return payload
    if "text" in payload:
        payload.pop("html", None)
    while _size(payload) > MAX_BODY_BYTES and payload.get("text"):
        payload["text"] = payload["text"][: len(payload["text"]) // 2]
    return payload


def _headers() -> dict[str, str]:
    headers = {"Authorization": f"Bearer {JUHANDU_TOKEN}"}
    if ACCESS_ID and ACCESS_SECRET:
        headers["CF-Access-Client-Id"] = ACCESS_ID
        headers["CF-Access-Client-Secret"] = ACCESS_SECRET
    return headers


async def send(payload: dict) -> tuple[str, str]:
    """One attempt. Returns ("sent" | "drop" | "retry", a short note). A 400 or
    413 will not improve on retry; anything else (a down host, a refused token,
    a closed door, a rate limit) might."""
    try:
        async with httpx.AsyncClient(transport=_transport, timeout=REQUEST_TIMEOUT_SECONDS) as client:
            response = await client.post(JUHANDU_URL, json=payload, headers=_headers())
    except Exception as exc:
        return "retry", f"{type(exc).__name__}"
    if response.status_code in (200, 201):
        return "sent", "inserted" if response.status_code == 201 else "already held"
    if response.status_code in (400, 413):
        return "drop", f"HTTP {response.status_code}"
    return "retry", f"HTTP {response.status_code}"


def _load_outbox() -> list[dict]:
    try:
        with open(OUTBOX_PATH, encoding="utf-8") as handle:
            entries = json.load(handle)
        return entries if isinstance(entries, list) else []
    except (OSError, ValueError):
        return []


def _save_outbox(entries: list[dict]) -> None:
    temporary = OUTBOX_PATH + ".tmp"
    try:
        with open(temporary, "w", encoding="utf-8") as handle:
            json.dump(entries, handle)
        os.replace(temporary, OUTBOX_PATH)
    except OSError as exc:
        logger.warning("Juhandu outbox could not be saved: %s", exc)


def _queue(payload: dict, first_at: float, attempts: int) -> None:
    gap = RETRY_GAPS_SECONDS[min(attempts - 1, len(RETRY_GAPS_SECONDS) - 1)]
    entries = _load_outbox()
    entries.append({
        "id": uuid.uuid4().hex,
        "payload": payload,
        "first_at": first_at,
        "attempts": attempts,
        "next_at": time.time() + gap,
    })
    _save_outbox(entries[-OUTBOX_LIMIT:])


async def _push(payload: dict) -> None:
    started = time.time()
    outcome, note = await send(payload)
    message_id = payload.get("messageId")
    if outcome == "retry":
        _queue(payload, started, 1)
        logger.warning("Juhandu push failed (%s), queued for retry: %s", note, message_id)
    elif outcome == "drop":
        logger.warning("Juhandu refused a push (%s), not retried: %s", note, message_id)
    else:
        logger.info("Juhandu push %s: %s", note, message_id)


def submit(raw_message: str | None, verdict: str, category: str | None) -> bool:
    """Starts a push for an accepted, delivered message and returns at once.
    Returns whether a push was started. Never raises: nothing here may change
    the disposition the caller already decided."""
    try:
        if not wants(category):
            return False
        payload = build_payload(raw_message, verdict, category or "")
        if payload is None:
            return False
        task = asyncio.get_running_loop().create_task(_push(payload))
        _tasks.add(task)
        task.add_done_callback(_tasks.discard)
        return True
    except Exception as exc:
        logger.warning("Juhandu push not started: %s", exc)
        return False


async def retry_due() -> None:
    """Retries every queued push that is due and drops those older than the
    retry window. The outbox is reread after each send, so a push queued
    meanwhile is never overwritten."""
    now = time.time()
    due = [entry for entry in _load_outbox() if entry.get("next_at", 0) <= now]
    for entry in due:
        outcome, note = await send(entry["payload"])
        entries = _load_outbox()
        current = next((item for item in entries if item.get("id") == entry["id"]), None)
        if current is None:
            continue
        message_id = entry["payload"].get("messageId")
        if outcome == "sent":
            entries.remove(current)
            logger.info("Juhandu retry %s: %s", note, message_id)
        elif outcome == "drop" or time.time() - current["first_at"] > RETRY_WINDOW_SECONDS:
            entries.remove(current)
            logger.warning("Juhandu push given up (%s): %s", note, message_id)
        else:
            current["attempts"] += 1
            gap = RETRY_GAPS_SECONDS[min(current["attempts"] - 1, len(RETRY_GAPS_SECONDS) - 1)]
            current["next_at"] = time.time() + gap
        _save_outbox(entries)


async def run_forever() -> None:
    while True:
        await asyncio.sleep(SWEEP_SECONDS)
        try:
            await retry_due()
        except Exception as exc:
            logger.warning("Juhandu retry sweep failed: %s", exc)
