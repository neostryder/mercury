"""The text of a message that the judges read.

The webhook payload carries a text/plain part and an html part. The text part
used to win whenever it was present, which is wrong for senders whose text part
is a placeholder: a marketing platform that sends only "Plain text version not
available" gives the judges nothing but the subject line, and a bare subject
such as a promo code with a 24-hour expiry reads as an account-security scare.
The real message sat unread in the html part.

So a placeholder or near-empty text part is replaced by a plain-text rendering
of the html. Links are kept as "label (url)" because a mismatched link is one of
the few concrete phishing signs the judges are told to look for.
"""
import re
from html.parser import HTMLParser

# A text part this short, next to an html part this much longer, is a stub.
SHORT_TEXT_CHARS = 80
HTML_TO_TEXT_RATIO = 3

_PLACEHOLDER = re.compile(
    r"plain[- ]text (?:version|part|alternative)\s+(?:is\s+)?(?:not available|unavailable)"
    r"|(?:this|the) (?:e-?mail|message) (?:requires|contains) html"
    r"|(?:does not|doesn't|cannot|can't) (?:support|display|view) html"
    r"|(?:view|read) (?:this )?(?:e-?mail|message) in (?:your |a )?(?:web )?browser",
    re.I,
)

_SKIPPED = {"script", "style", "head", "title", "noscript"}
_BLOCK = {"p", "div", "br", "tr", "li", "h1", "h2", "h3", "h4", "h5", "h6",
          "table", "section", "article", "ul", "ol", "blockquote", "hr"}


class _TextExtractor(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self._skip = 0
        self._href: str | None = None
        self._label: list[str] = []

    def handle_starttag(self, tag, attrs):
        if tag in _SKIPPED:
            self._skip += 1
        elif tag in _BLOCK:
            self.parts.append("\n")
        if tag == "a" and not self._skip:
            self._href = dict(attrs).get("href")
            self._label = []
        elif tag == "img" and not self._skip:
            alt = (dict(attrs).get("alt") or "").strip()
            if alt:
                self.parts.append(alt + " ")

    def handle_endtag(self, tag):
        if tag in _SKIPPED:
            self._skip = max(0, self._skip - 1)
        elif tag in _BLOCK:
            self.parts.append("\n")
        if tag == "a" and self._href is not None:
            href = self._href.strip()
            if href.lower().startswith(("http://", "https://", "mailto:")):
                self.parts.append(" ({}) ".format(href))
            self._href = None

    def handle_data(self, data):
        if not self._skip:
            self.parts.append(data)


def html_to_text(html: str | None) -> str:
    """A plain-text rendering of html, with blank runs collapsed."""
    if not isinstance(html, str) or not html.strip():
        return ""
    extractor = _TextExtractor()
    try:
        extractor.feed(html)
        extractor.close()
    except Exception:                                         # noqa: BLE001
        return ""
    text = "".join(extractor.parts).replace("\xa0", " ")
    text = re.sub(r"[ \t\r\f\v]+", " ", text)
    text = re.sub(r" ?\n ?", "\n", text)
    return re.sub(r"\n{3,}", "\n\n", text).strip()


def judged_body(text: str | None, html: str | None) -> str:
    """The body text to show the judges: the text part unless it is a stub."""
    text_part = text if isinstance(text, str) else ""
    stripped = text_part.strip()
    rendered = html_to_text(html)
    if not rendered:
        return stripped or (html if isinstance(html, str) else "")
    if not stripped:
        return rendered
    placeholder = bool(_PLACEHOLDER.search(stripped)) and len(stripped) <= 200
    short = (len(stripped) < SHORT_TEXT_CHARS
             and len(rendered) >= HTML_TO_TEXT_RATIO * len(stripped))
    return rendered if (placeholder or short) else text_part
