import os
import smtplib
from email.mime.text import MIMEText

GANDALF_ADDRESS = "gandalf@rpgm.tools"
SMTP_HOST = os.environ.get("MERCURY_MAILBOX_SMTP_HOST", "smtp.forwardemail.net")
SMTP_PORT = int(os.environ.get("MERCURY_MAILBOX_SMTP_PORT", "465"))
IMAP_USER = os.environ.get("MERCURY_MAILBOX_IMAP_USER")
IMAP_PASSWORD = os.environ.get("MERCURY_MAILBOX_IMAP_PASSWORD")


def send_to_gandalf(subject: str, body: str) -> bool:
    if not IMAP_USER or not IMAP_PASSWORD:
        return False
    try:
        message = MIMEText(body, "plain", "utf-8")
        message["Subject"] = subject
        message["From"] = IMAP_USER
        message["To"] = GANDALF_ADDRESS
        with smtplib.SMTP_SSL(SMTP_HOST, SMTP_PORT) as server:
            server.login(IMAP_USER, IMAP_PASSWORD)
            server.send_message(message)
        return True
    except Exception:
        return False
