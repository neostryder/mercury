"""Delivers an accepted message into the real mailbox via IMAP APPEND.

This exists because ForwardEmail's webhook is a terminal, parallel delivery
target - its own docs and source (helpers/on-data-mx.js) confirm the
webhook response is read only for logging, never used to add headers or
otherwise influence what a separately-configured mailbox recipient on the
same alias receives. There is no way to make Mercury's verdict binding by
changing only the webhook response; the only way is for whatever currently
delivers straight to the mailbox to be replaced with the webhook, and for
Mercury itself to deliver an accepted message onward.

Gated by MERCURY_DELIVER_ACCEPTED_MAIL (default off): flipping it on and
removing the mailbox's own address from an alias's recipient list must
happen together, or every accepted message would be delivered twice (once
here, once by the still-configured parallel recipient) - see the alias
audit in the chat history that led to this file existing.
"""
import email
import imaplib
import logging
import os
import re
import threading
import time

logger = logging.getLogger(__name__)

DELIVER_ACCEPTED_MAIL = os.environ.get("MERCURY_DELIVER_ACCEPTED_MAIL", "false").lower() == "true"
IMAP_HOST = os.environ.get("MERCURY_MAILBOX_IMAP_HOST", "imap.forwardemail.net")
IMAP_PORT = int(os.environ.get("MERCURY_MAILBOX_IMAP_PORT", "993"))
IMAP_USER = os.environ.get("MERCURY_MAILBOX_IMAP_USER")
IMAP_PASSWORD = os.environ.get("MERCURY_MAILBOX_IMAP_PASSWORD")
# Without a socket timeout a stalled mail host holds the ingest call open
# indefinitely. A timeout is only a failed attempt: the host may still commit
# the APPEND later, which is why every delivery looks for an existing copy first.
IMAP_TIMEOUT_SECONDS = float(os.environ.get("MERCURY_IMAP_TIMEOUT_SECONDS", "45"))
# The mail host's IMAP server sometimes drops a new connection, refuses it, or
# stalls it, then answers normally seconds later. Everything before the APPEND
# can safely be tried again, so a delivery makes this many attempts at
# connecting, logging in and looking for an existing copy before it gives up.
# The APPEND itself is never repeated in the same call.
IMAP_PREPARE_ATTEMPTS = int(os.environ.get("MERCURY_IMAP_PREPARE_ATTEMPTS", "3"))
IMAP_RETRY_PAUSE_SECONDS = float(os.environ.get("MERCURY_IMAP_RETRY_PAUSE_SECONDS", "3"))
# A burst of accepted mail opened several connections at once, and the host
# refused some of them. Deliveries take turns instead.
_delivery_lock = threading.Lock()


def _add_headers(raw_message: str, headers: dict[str, str]) -> bytes:
    """Prepends headers to a raw RFC822 message. Prepending (rather than
    parsing and re-serializing the whole MIME structure) is deliberate -
    headers may appear in any order before the blank line that separates
    them from the body, so this can't corrupt the MIME structure the way a
    naive re-parse/re-emit of a message with attachments could."""
    prefix = "".join(f"{name}: {value}\r\n" for name, value in headers.items())
    return (prefix + raw_message).encode("utf-8", errors="replace")


def _folder_names(conn) -> list[str]:
    typ, data = conn.list()
    if typ != "OK":
        return []
    folders = []
    for entry in data:
        if not entry:
            continue
        text = entry.decode("utf-8", errors="replace") if isinstance(entry, bytes) else str(entry)
        match = re.search(r'"([^"]*)"\s*$', text)
        folders.append(match.group(1) if match else text.split()[-1])
    return folders


def list_folders() -> list[str]:
    """Real IMAP folder names in the mailbox, best-effort. Used to ground the
    brief judge's proposals so it never invents a folder that doesn't exist
    (e.g. a "Deferred mail" folder for a 421 disposition, which was never
    delivered anywhere in the first place - see advance_brief()'s prompt).
    Returns an empty list on any failure rather than raising; a missing
    folder list just means the judge proceeds without that grounding."""
    if not IMAP_USER or not IMAP_PASSWORD:
        return []
    try:
        conn = imaplib.IMAP4_SSL(IMAP_HOST, IMAP_PORT, timeout=IMAP_TIMEOUT_SECONDS)
        try:
            conn.login(IMAP_USER, IMAP_PASSWORD)
            return _folder_names(conn)
        finally:
            _close(conn)
    except Exception:
        return []


def _close(conn) -> None:
    """Ends the session without letting a failed logout change the outcome: a
    message already appended must not be reported as undelivered because the
    host dropped the connection afterwards."""
    try:
        conn.logout()
    except Exception:
        pass


def message_id_of(raw_message: str) -> str | None:
    try:
        value = email.message_from_string(raw_message).get("Message-Id")
    except Exception:
        return None
    value = " ".join(str(value).split()) if value else ""
    return value or None


def _imap_quote(value: str) -> str:
    return '"' + value.replace("\\", "\\\\").replace('"', '\\"') + '"'


def _find_copy(conn, message_id: str, folders: list[str]) -> str | None:
    """The first folder that already holds a message with this Message-Id.
    The mail host can commit an APPEND after the client gave up on it, so a
    delivery that was reported as failed may be in the mailbox already."""
    if not message_id.isascii():
        return None
    wanted = _imap_quote(message_id)
    for folder in folders:
        typ, _ = conn.select(_imap_quote(folder), readonly=True)
        if typ != "OK":
            continue
        typ, data = conn.search(None, "HEADER", "Message-ID", wanted)
        if typ == "OK" and data and data[0] and data[0].strip():
            return folder
    return None


def _open_and_find_copy(message_id: str | None, target_folder: str, search_all_folders: bool):
    """Connects, logs in and looks for a copy already in the mailbox, trying
    again after a dropped, refused or stalled connection. Returns the open
    connection and the folder holding a copy (or None). Nothing here writes to
    the mailbox, so a repeat can never store a second copy. A refused login is
    not retried."""
    attempts = max(1, IMAP_PREPARE_ATTEMPTS)
    last_error: Exception | None = None
    for attempt in range(attempts):
        if attempt:
            time.sleep(IMAP_RETRY_PAUSE_SECONDS * attempt)
        conn = None
        try:
            conn = imaplib.IMAP4_SSL(IMAP_HOST, IMAP_PORT, timeout=IMAP_TIMEOUT_SECONDS)
            conn.login(IMAP_USER, IMAP_PASSWORD)
            existing = None
            if message_id:
                folders = [target_folder]
                if search_all_folders:
                    folders += [name for name in _folder_names(conn) if name != target_folder]
                elif target_folder != "INBOX":
                    folders.append("INBOX")
                existing = _find_copy(conn, message_id, folders)
            return conn, existing
        except (OSError, imaplib.IMAP4.abort) as exc:
            last_error = exc
            if conn is not None:
                _close(conn)
            logger.warning(
                "mail host connection attempt %d of %d failed: %s: %s",
                attempt + 1, attempts, type(exc).__name__, exc,
            )
        except Exception:
            if conn is not None:
                _close(conn)
            raise
    raise last_error


def deliver_accepted_message(
    raw_message: str,
    verdict: str,
    category: str,
    disposition: str,
    target_folder: str = "INBOX",
    search_all_folders: bool = False,
) -> str:
    """Synchronous (imaplib has no async API) - call via asyncio.to_thread.
    Returns a short status string for logging; raises nothing outward, since
    a delivery failure here must not be treated as a pipeline error that
    could somehow affect the disposition already decided.

    Appends nothing when a copy with the same Message-Id is already in the
    mailbox: the target folder and INBOX are searched, or every folder when
    search_all_folders is set (a retry after a failed attempt, by which time a
    copy may have been filed elsewhere)."""
    if not DELIVER_ACCEPTED_MAIL:
        return "skipped (MERCURY_DELIVER_ACCEPTED_MAIL is off)"
    if not IMAP_USER or not IMAP_PASSWORD:
        return "skipped (no mailbox IMAP credentials configured)"
    if not target_folder or any(character in target_folder for character in "\r\n\x00"):
        return "failed: invalid target folder"

    message_bytes = _add_headers(raw_message, {
        "X-Mercury-Verdict": verdict,
        "X-Mercury-Category": category,
        "X-Mercury-Disposition": disposition,
    })
    message_id = message_id_of(raw_message)
    started = time.monotonic()
    result = "failed: no result"
    try:
        with _delivery_lock:
            conn, existing = _open_and_find_copy(message_id, target_folder, search_all_folders)
            try:
                if existing:
                    result = f"delivered to {existing}"
                    logger.warning("mail delivery skipped, copy already in %s: %s", existing, message_id)
                    return result
                typ, data = conn.append(target_folder, None, None, message_bytes)
                if typ != "OK":
                    result = f"failed: APPEND returned {typ} {data}"
                    return result
                result = f"delivered to {target_folder}"
                return result
            finally:
                _close(conn)
    except Exception as exc:
        result = f"failed: {type(exc).__name__}: {exc}"
        return result
    finally:
        logger.warning(
            "mail delivery attempt %s folder=%s message_id=%s took=%.1fs",
            result[:120], target_folder, message_id, time.monotonic() - started,
        )
