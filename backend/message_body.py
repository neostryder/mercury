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
Input excerpts remove padding and shorten long URL tails before cutting prose.
"""
import re
from html.parser import HTMLParser
from urllib.parse import urlsplit, urlunsplit

# A short text part next to html with this many more words is a stub.
SHORT_TEXT_WORDS = 40
HTML_TO_TEXT_RATIO = 3

_PLACEHOLDER = re.compile(
    r"plain[- ]text (?:version|part|alternative)\s+(?:is\s+)?(?:not available|unavailable)"
    r"|(?:this|the) (?:e-?mail|message) (?:requires|contains) html"
    r"|(?:does not|doesn't|cannot|can't) (?:support|display|view) html"
    r"|(?:view|read) (?:this )?(?:e-?mail|message) in (?:your |a )?(?:web )?browser",
    re.I,
)

_SKIPPED = {"script", "style", "head", "title", "noscript"}
_VOID = {"area", "base", "br", "col", "embed", "hr", "img", "input", "link",
         "meta", "param", "source", "track", "wbr"}
_HIDDEN_STYLE = re.compile(
    r"(?:^|;)\s*(?:display\s*:\s*none|mso-hide\s*:\s*all)\s*(?:!important\s*)?(?:;|$)",
    re.I,
)
_INVISIBLE = str.maketrans("", "", "\u200c\u200b\u034f\u00ad\u2060\ufeff")
_URL = re.compile(r"""https?://[^\s<>"')\]]+""", re.I)
_BLOCK = {"p", "div", "br", "tr", "li", "h1", "h2", "h3", "h4", "h5", "h6",
          "table", "section", "article", "ul", "ol", "blockquote", "hr"}


class _TextExtractor(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self._skip: list[str] = []
        self._href: str | None = None
        self._label: list[str] = []

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        hidden = "hidden" in attrs or _HIDDEN_STYLE.search(attrs.get("style") or "")
        if self._skip or tag in _SKIPPED or hidden:
            if tag not in _VOID:
                self._skip.append(tag)
            return
        if tag in _BLOCK:
            self.parts.append("\n")
        if tag == "a" and not self._skip:
            self._href = attrs.get("href")
            self._label = []
        elif tag == "img" and not self._skip:
            alt = (attrs.get("alt") or "").strip()
            if alt:
                self.parts.append(alt + " ")

    def handle_endtag(self, tag):
        if self._skip:
            if tag in self._skip:
                del self._skip[len(self._skip) - 1 - self._skip[::-1].index(tag):]
            return
        if tag in _BLOCK:
            self.parts.append("\n")
        if tag == "a" and self._href is not None:
            href = self._href.strip()
            if href.lower().startswith(("http://", "https://", "mailto:")):
                self.parts.append(" ({}) ".format(href))
            self._href = None

    def handle_data(self, data):
        if not self._skip:
            self.parts.append(data)


def _normalize_text(text: str) -> str:
    text = text.translate(_INVISIBLE).replace("\xa0", " ")
    text = re.sub(r"[ \t\r\f\v]+", " ", text)
    text = re.sub(r" ?\n ?", "\n", text)
    return re.sub(r"\n{3,}", "\n\n", text).strip()


def _short_url(match: re.Match) -> str:
    url = match.group(0)
    if len(url) <= 160:
        return url
    try:
        parts = urlsplit(url)
    except ValueError:
        return url
    path = parts.path[:48] + "..." if len(parts.path) > 48 else parts.path
    return urlunsplit((parts.scheme, parts.netloc, path,
                       "..." if parts.query else "", "..." if parts.fragment else ""))


def judge_excerpt(content: str, limit: int) -> str:
    """Fit an input window, keeping URL hosts and marking any cut to prose."""
    if len(content) <= limit:
        return content
    content = _normalize_text(content)
    if len(content) > limit:
        content = _URL.sub(_short_url, content)
    if len(content) <= limit:
        return content
    marker = "\n[Content truncated]"
    prefix = content[:max(0, limit - len(marker))]
    boundary = max(prefix.rfind(" "), prefix.rfind("\n"))
    if boundary > 0:
        prefix = prefix[:boundary]
    return (prefix.rstrip() + marker)[:limit]


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
    return _normalize_text("".join(extractor.parts))


def judged_body(text: str | None, html: str | None) -> str:
    """The body text to show the judges: the text part unless it is a stub."""
    text_part = text if isinstance(text, str) else ""
    stripped = text_part.strip()
    rendered = html_to_text(html)
    if not rendered:
        return stripped
    if not stripped:
        return rendered
    placeholder = bool(_PLACEHOLDER.search(stripped)) and len(stripped) <= 200
    text_words = len(re.findall(r"\b\w+\b", _URL.sub("", stripped.translate(_INVISIBLE))))
    html_words = len(re.findall(r"\b\w+\b", _URL.sub("", rendered)))
    short = (text_words <= SHORT_TEXT_WORDS
             and html_words >= HTML_TO_TEXT_RATIO * max(1, text_words))
    return rendered if (placeholder or short) else text_part
