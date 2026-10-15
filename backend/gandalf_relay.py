import logging
import os
import smtplib
from email.mime.text import MIMEText

from link_filter import blocked_hostname, defang

GANDALF_ADDRESS = "gandalf@rpgm.tools"
SMTP_HOST = os.environ.get("MERCURY_MAILBOX_SMTP_HOST", "smtp.forwardemail.net")
SMTP_PORT = int(os.environ.get("MERCURY_MAILBOX_SMTP_PORT", "465"))
IMAP_USER = os.environ.get("MERCURY_MAILBOX_IMAP_USER")
IMAP_PASSWORD = os.environ.get("MERCURY_MAILBOX_IMAP_PASSWORD")

# A handoff that quotes a flagged message can carry a hostname ForwardEmail's
# link filter rejects; see link_filter.py.
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
            hostname = blocked_hostname(error)
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
            subject = defang(subject, hostname)
            body = defang(body, hostname)
            logger.warning(
                "Gandalf relay: link filter blocked %s, retrying defanged", hostname
            )
        except Exception as error:
            logger.warning("Gandalf relay failed: %s: %s", type(error).__name__, error)
            return False
