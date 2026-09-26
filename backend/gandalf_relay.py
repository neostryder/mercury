import logging
import os
import re
import smtplib
from email.mime.text import MIMEText

GANDALF_ADDRESS = "gandalf@rpgm.tools"
SMTP_HOST = os.environ.get("MERCURY_MAILBOX_SMTP_HOST", "smtp.forwardemail.net")
SMTP_PORT = int(os.environ.get("MERCURY_MAILBOX_SMTP_PORT", "465"))
IMAP_USER = os.environ.get("MERCURY_MAILBOX_IMAP_USER")
IMAP_PASSWORD = os.environ.get("MERCURY_MAILBOX_IMAP_PASSWORD")

# ForwardEmail rejects a message at DATA with a 554 when any hostname in it,
# linked or bare, is on Cloudflare Family DNS's block list, and names that
# hostname in the reply. A handoff that quotes a flagged message can easily
# carry one.
BLOCKED_HOSTNAME = re.compile(rb"Link hostname of (\S+) was detected", re.IGNORECASE)
MAX_DEFANGED_HOSTNAMES = 3
DEFANG_NOTE = (
    "\n\n(Mercury note: a hostname written like example[.]com was defanged "
    "because the outbound link filter blocked it.)"
)

logger = logging.getLogger(__name__)


def _message(subject: str, body: str) -> MIMEText:
    message = MIMEText(body, "plain", "utf-8")
    message["Subject"] = subject
    message["From"] = IMAP_USER
    message["To"] = GANDALF_ADDRESS
    return message


def _defang(text: str, hostname: str) -> str:
    return re.sub(re.escape(hostname), hostname.replace(".", "[.]"), text, flags=re.IGNORECASE)


def _blocked_hostname(error: smtplib.SMTPDataError) -> str | None:
    reply = error.smtp_error if isinstance(error.smtp_error, bytes) else b""
    match = BLOCKED_HOSTNAME.search(reply)
    return match.group(1).decode("ascii", "replace").strip(".'\"") if match else None


def send_to_gandalf(subject: str, body: str) -> bool:
    if not IMAP_USER or not IMAP_PASSWORD:
        return False
    defanged: set[str] = set()
    while True:
        try:
            with smtplib.SMTP_SSL(SMTP_HOST, SMTP_PORT) as server:
                server.login(IMAP_USER, IMAP_PASSWORD)
                server.send_message(_message(subject, body))
            return True
        except smtplib.SMTPDataError as error:
            hostname = _blocked_hostname(error)
            if (
                not hostname
                or hostname.lower() in defanged
                or len(defanged) >= MAX_DEFANGED_HOSTNAMES
            ):
                logger.warning(
                    "Gandalf relay rejected: %s %r", error.smtp_code, error.smtp_error
                )
                return False
            if not defanged:
                body += DEFANG_NOTE
            defanged.add(hostname.lower())
            subject = _defang(subject, hostname)
            body = _defang(body, hostname)
            logger.warning(
                "Gandalf relay: link filter blocked %s, retrying defanged", hostname
            )
        except Exception as error:
            logger.warning("Gandalf relay failed: %s: %s", type(error).__name__, error)
            return False
