"""Helpers for ForwardEmail's outbound link filter.

ForwardEmail rejects a message at DATA with a 554 when any hostname in it,
linked or bare, is on Cloudflare Family DNS's block list, and names that
hostname in the reply. Mercury quotes mail it filtered (subjects, senders,
the judge's reasoning), so its own outbound mail can easily carry one. A
hostname written like example[.]com is no longer a hostname to the filter.
"""
import re
import smtplib

BLOCKED_HOSTNAME = re.compile(rb"Link hostname of (\S+) was detected", re.IGNORECASE)
_HOSTNAME = re.compile(r"(?<![\w-])((?:[a-z0-9](?:[a-z0-9-]*[a-z0-9])?\.)+[a-z]{2,})(?![\w-])", re.IGNORECASE)


def defang(text: str, hostname: str) -> str:
    return re.sub(re.escape(hostname), hostname.replace(".", "[.]"), text, flags=re.IGNORECASE)


def defang_hostnames(text: str) -> str:
    """Defang every hostname-shaped token in text quoted from outside Mercury."""
    return _HOSTNAME.sub(lambda match: match.group(1).replace(".", "[.]"), text)


def blocked_hostname(error: smtplib.SMTPDataError) -> str | None:
    reply = error.smtp_error if isinstance(error.smtp_error, bytes) else b""
    match = BLOCKED_HOSTNAME.search(reply)
    return match.group(1).decode("ascii", "replace").strip(".'\"") if match else None
