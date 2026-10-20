"""One-click unsubscribe (RFC 8058), done in code instead of by the browsing agent.

A sender that publishes `List-Unsubscribe-Post: List-Unsubscribe=One-Click`
promises that an HTTPS POST of that exact body to its `List-Unsubscribe` URL
unsubscribes the recipient, with no page to read and no button to find. The
browsing agent used to open that URL as an ordinary page instead, and judged
the result from whatever rendered: a blank JavaScript preference center, or an
SMS sign-up page shown after the opt-out had already been recorded, both
failed an unsubscribe that the sender had already honored.

Mercury only does this for a URL on the sender's own registrable domain, so
the request goes where the sender itself said to send it. Any other route
(a mailing-list provider's domain, a link in the body) stays with the agent,
which judges those. A failed attempt returns a note for the agent's prompt
rather than an error, and the agent then tries the remaining routes.
"""
import asyncio
import ipaddress
import re
import socket
from dataclasses import dataclass
from html import unescape
from urllib.parse import urljoin, urlparse

import httpx

# Second-level labels under which the registrable domain takes one more label,
# so "shop.example.co.uk" is "example.co.uk" and not "co.uk". A short list of
# the common ones; an unlisted suffix errs toward treating hosts as unrelated,
# which only hands the route to the agent.
_TWO_PART_SUFFIXES = frozenset({
    "co.uk", "org.uk", "ac.uk", "gov.uk", "com.au", "net.au", "org.au",
    "co.nz", "co.jp", "co.in", "co.za", "com.br", "com.mx", "com.sg",
    "com.hk", "com.tr", "com.cn", "com.ar", "co.kr",
})
_ROUTES_HEADING = "Unsubscribe routes extracted from this message:"
_ANGLE_HTTPS_URL = re.compile(r"<(https://[^>\s]+)>", re.I)
_ONE_CLICK_BODY = {"List-Unsubscribe": "One-Click"}
_CONFIRMATION = re.compile(
    r"unsubscribed|opted[ -]?out|no longer (?:receive|be sent)|"
    r"removed from|successfully (?:removed|unsubscribed)|request submitted",
    re.I,
)
_MAX_HOPS = 4
_MAX_PAGE_BYTES = 200_000
_USER_AGENT = "Mercury-unsubscribe/1.0 (RFC 8058 one-click)"


@dataclass(frozen=True)
class OneClickResult:
    accepted: bool
    summary: str


def registrable_domain(host: str) -> str:
    labels = [label for label in (host or "").lower().strip(".").split(".") if label]
    if len(labels) <= 2:
        return ".".join(labels)
    if ".".join(labels[-2:]) in _TWO_PART_SUFFIXES:
        return ".".join(labels[-3:])
    return ".".join(labels[-2:])


def sender_domain_of(message_context: str) -> str | None:
    """The domain on the From line, read from the header block only so a body
    cannot supply it."""
    head = (message_context or "").split("\n\n", 1)[0]
    match = re.search(r"^From:.*?@([A-Za-z0-9.-]+\.[A-Za-z]{2,})", head, re.M)
    return match.group(1).lower() if match else None


def _routes_block(message_context: str) -> str:
    """The extracted-routes block alone. Mercury writes it from the message's
    headers and link targets, so a body that quotes the same words cannot add
    a route or a one-click declaration to it."""
    _, found, rest = (message_context or "").partition(_ROUTES_HEADING)
    return rest.split("\n\n", 1)[0] if found else ""


def one_click_urls(message_context: str, sender_domain: str | None) -> list[str]:
    """HTTPS List-Unsubscribe URLs on the sender's own registrable domain, when
    the message also declares one-click support."""
    block = _routes_block(message_context)
    declared = re.search(
        r"^List-Unsubscribe-Post:.*List-Unsubscribe=One-Click", block, re.I | re.M
    )
    header = re.search(r"^List-Unsubscribe:(.*)$", block, re.I | re.M)
    if not (declared and header and sender_domain):
        return []
    family = registrable_domain(sender_domain)
    urls = []
    for url in _ANGLE_HTTPS_URL.findall(header.group(1)):
        parts = urlparse(url)
        if (
            parts.hostname
            and parts.username is None
            and parts.port in (None, 443)
            and registrable_domain(parts.hostname) == family
        ):
            urls.append(url)
    return urls


async def _resolves_publicly(host: str) -> bool:
    """False for a host that is, or resolves to, anything but a public address,
    so a crafted List-Unsubscribe cannot aim Mercury at its own network."""
    try:
        infos = await asyncio.get_running_loop().getaddrinfo(host, 443, type=socket.SOCK_STREAM)
    except OSError:
        return False
    return bool(infos) and all(
        ipaddress.ip_address(info[4][0]).is_global for info in infos
    )


def _visible_text(markup: str) -> str:
    markup = re.sub(r"(?is)<(script|style)\b.*?</\1>", " ", markup)
    return " ".join(unescape(re.sub(r"(?s)<[^>]+>", " ", markup)).split())


async def _confirmation_text(client: httpx.AsyncClient, response: httpx.Response, family: str) -> str | None:
    """Follows the redirect chain the POST answered with, by GET and only within
    the sender's own domain, and returns the visible text of the page it ends
    on. This is evidence for the report; a failure to read it never undoes an
    accepted unsubscribe."""
    hops = 0
    while response.is_redirect and hops < _MAX_HOPS:
        target = urljoin(str(response.url), response.headers.get("location", ""))
        parts = urlparse(target)
        if (
            parts.scheme != "https"
            or not parts.hostname
            or registrable_domain(parts.hostname) != family
            or not await _resolves_publicly(parts.hostname)
        ):
            return None
        response = await client.get(target)
        hops += 1
    if response.is_redirect or response.status_code != 200:
        return None
    return _visible_text(response.text[:_MAX_PAGE_BYTES]) or None


async def attempt(message_context: str, sender_domain: str | None = None) -> OneClickResult | None:
    """Sends the declared one-click POST. None when the message offers no route
    this can use; otherwise an accepted result, or a refusal whose summary is
    meant for the agent's prompt."""
    sender_domain = sender_domain or sender_domain_of(message_context)
    urls = one_click_urls(message_context, sender_domain)
    if not urls:
        return None
    url = urls[0]
    host = urlparse(url).hostname
    family = registrable_domain(host)
    if not await _resolves_publicly(host):
        return OneClickResult(False, f"The one-click route on {host} was not tried: the host does not resolve to a public address.")

    headers = {"User-Agent": _USER_AGENT}
    try:
        async with httpx.AsyncClient(timeout=15, follow_redirects=False, headers=headers) as client:
            response = await client.post(url, data=_ONE_CLICK_BODY)
            status = response.status_code
            if not (200 <= status < 400):
                return OneClickResult(
                    False,
                    f"A one-click POST to {host} returned HTTP {status}, so the unsubscribe was not confirmed that way.",
                )
            try:
                page_text = await _confirmation_text(client, response, family)
            except (httpx.HTTPError, OSError):
                page_text = None
    except (httpx.HTTPError, OSError) as exc:
        return OneClickResult(
            False,
            f"A one-click POST to {host} failed ({type(exc).__name__}), so the unsubscribe was not confirmed that way.",
        )

    summary = f"Sent the one-click POST that the message declares to {host}, and the provider accepted it (HTTP {status})."
    if page_text and _CONFIRMATION.search(page_text):
        summary += f" Its confirmation page read: \"{page_text[:160]}\""
    return OneClickResult(True, summary)
