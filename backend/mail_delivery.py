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
import os

DELIVER_ACCEPTED_MAIL = os.environ.get("MERCURY_DELIVER_ACCEPTED_MAIL", "false").lower() == "true"
IMAP_HOST = os.environ.get("MERCURY_MAILBOX_IMAP_HOST", "imap.forwardemail.net")
IMAP_PORT = int(os.environ.get("MERCURY_MAILBOX_IMAP_PORT", "993"))
IMAP_USER = os.environ.get("MERCURY_MAILBOX_IMAP_USER")
IMAP_PASSWORD = os.environ.get("MERCURY_MAILBOX_IMAP_PASSWORD")


def _add_headers(raw_message: str, headers: dict[str, str]) -> bytes:
    """Prepends headers to a raw RFC822 message. Prepending (rather than
    parsing and re-serializing the whole MIME structure) is deliberate -
    headers may appear in any order before the blank line that separates
    them from the body, so this can't corrupt the MIME structure the way a
    naive re-parse/re-emit of a message with attachments could."""
    prefix = "".join(f"{name}: {value}\r\n" for name, value in headers.items())
    return (prefix + raw_message).encode("utf-8", errors="replace")


def deliver_accepted_message(raw_message: str, verdict: str, category: str, disposition: str) -> str:
    """Synchronous (imaplib has no async API) - call via asyncio.to_thread.
    Returns a short status string for logging; raises nothing outward, since
    a delivery failure here must not be treated as a pipeline error that
    could somehow affect the disposition already decided."""
    if not DELIVER_ACCEPTED_MAIL:
        return "skipped (MERCURY_DELIVER_ACCEPTED_MAIL is off)"
    if not IMAP_USER or not IMAP_PASSWORD:
        return "skipped (no mailbox IMAP credentials configured)"

    message_bytes = _add_headers(raw_message, {
        "X-Mercury-Verdict": verdict,
        "X-Mercury-Category": category,
        "X-Mercury-Disposition": disposition,
    })
    try:
        conn = imaplib.IMAP4_SSL(IMAP_HOST, IMAP_PORT)
        try:
            conn.login(IMAP_USER, IMAP_PASSWORD)
            typ, data = conn.append("INBOX", None, None, message_bytes)
            if typ != "OK":
                return f"failed: APPEND returned {typ} {data}"
            return "delivered"
        finally:
            conn.logout()
    except Exception as exc:
        return f"failed: {type(exc).__name__}: {exc}"
