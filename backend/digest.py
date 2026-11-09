"""Daily digest email: once a day, a standalone HTML email summarizing the
last 24 hours of Mercury's activity, sent to the mailbox owner.

This gathers its data over authenticated HTTPS calls to the Worker's
existing /dashboard/api/* routes (worker/src/index.js) rather than adding a
second path into D1. Those routes sit behind a Cloudflare Access
application; this makes the same request a browser would, but as a
non-interactive caller, so it authenticates with a Cloudflare Access
Service Token (CF-Access-Client-Id/CF-Access-Client-Secret headers) issued
to a Service Auth policy on that application rather than a human login. The
base URL for those calls is derived from MERCURY_WORKER_LOG_URL (the
Worker's own hostname, already configured for event_log.py) rather than
introducing a redundant env var for the same host.

The insights paragraph reuses the judge provider seam (providers/judge.py)
with the aggregate stats as context, rather than a separate LLM integration.

The /dashboard/api/messages, /rules, and /actions routes are paginated, so
each is read page by page until the rows are older than the 24-hour window.
Reading stops after MAX_PAGES pages; render_html() notes it inline when that
limit cut the window short. The standing rule count comes from the live
policy at /dashboard/api/filtering.
"""
import asyncio
import html
import logging
import os
import re
import smtplib
from collections import Counter
from datetime import datetime, timedelta, timezone
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from urllib.parse import urlsplit, urlunsplit

import httpx

from link_filter import blocked_hostname, defang, defang_hostnames
from providers.judge import Judge

logger = logging.getLogger(__name__)

# Fixed offset, not a DST-aware zoneinfo lookup - Arizona does not observe DST,
# so "next 7am Phoenix time" is always UTC-7 with no seasonal adjustment.
PHOENIX_TZ = timezone(timedelta(hours=-7), name="MST")

def _access_credential(name: str) -> str | None:
    """A service-token value, without the header label Cloudflare shows beside
    it on the token page ("CF-Access-Client-Id: ..."), which is easy to copy
    along with the value and makes Access reject the token."""
    value = os.environ.get(name)
    if value is None:
        return None
    return re.sub(r"^\s*CF-Access-Client-(Id|Secret)\s*:\s*", "", value, flags=re.IGNORECASE).strip()


WORKER_LOG_URL = os.environ.get("MERCURY_WORKER_LOG_URL")
CF_ACCESS_CLIENT_ID = _access_credential("MERCURY_DIGEST_ID")
CF_ACCESS_CLIENT_SECRET = _access_credential("MERCURY_DIGEST_SECRET")
SMTP_USER = os.environ.get("MERCURY_DIGEST_SMTP_USER")
SMTP_PASSWORD = os.environ.get("MERCURY_DIGEST_SMTP_PASSWORD")

SMTP_HOST = "smtp.forwardemail.net"
SMTP_PORT = 465
DIGEST_FROM = "gandalf@rpgm.tools"
DIGEST_TO = "aaron@rpgm.tools"
DASHBOARD_LINK = "https://mercury.rpgm.tools/"
PAGE_SIZE = 100
MAX_PAGES = 20
LOOKBACK = timedelta(hours=24)
MAX_DEFANGED_HOSTNAMES = 10


def _dashboard_base_url() -> str | None:
    if not WORKER_LOG_URL:
        return None
    parts = urlsplit(WORKER_LOG_URL)
    return urlunsplit((parts.scheme, parts.netloc, "", "", ""))


def _digest_enabled() -> tuple[bool, str]:
    missing = []
    if not WORKER_LOG_URL:
        missing.append("MERCURY_WORKER_LOG_URL")
    if not CF_ACCESS_CLIENT_ID:
        missing.append("MERCURY_DIGEST_ID")
    if not CF_ACCESS_CLIENT_SECRET:
        missing.append("MERCURY_DIGEST_SECRET")
    if not SMTP_USER:
        missing.append("MERCURY_DIGEST_SMTP_USER")
    if not SMTP_PASSWORD:
        missing.append("MERCURY_DIGEST_SMTP_PASSWORD")
    if missing:
        return False, "not set: " + ", ".join(missing)
    return True, ""


def _seconds_until_next_7am(now: datetime | None = None) -> float:
    now = now or datetime.now(PHOENIX_TZ)
    target = now.replace(hour=7, minute=0, second=0, microsecond=0)
    if target <= now:
        target += timedelta(days=1)
    return (target - now).total_seconds()


def _parse_ts(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed


def _since(rows: list[dict], field: str, cutoff: datetime) -> list[dict]:
    kept = []
    for row in rows:
        ts = _parse_ts(row.get(field))
        if ts and ts >= cutoff:
            kept.append(row)
    return kept


async def _get(client: httpx.AsyncClient, base_url: str, path: str) -> object:
    r = await client.get(f"{base_url}{path}")
    r.raise_for_status()
    return r.json()


async def _get_window(
    client: httpx.AsyncClient, base_url: str, path: str, field: str, cutoff: datetime,
) -> tuple[list[dict], bool]:
    """Pages through one of the paginated dashboard tables, newest first,
    until a row falls before cutoff. Returns the rows and whether MAX_PAGES
    ran out before the window was covered."""
    rows: list[dict] = []
    for page in range(MAX_PAGES):
        data = await _get(client, base_url, f"{path}?limit={PAGE_SIZE}&offset={page * PAGE_SIZE}")
        batch = data.get("rows", []) if isinstance(data, dict) else []
        rows.extend(batch)
        oldest = _parse_ts(batch[-1].get(field)) if batch else None
        if not data.get("hasMore") or (oldest and oldest < cutoff):
            return rows, False
    return rows, True


def _standing_rule_count(policy: object) -> int:
    if not isinstance(policy, dict):
        return 0
    sender_lists = policy.get("sender_lists") or {}
    semantic_rules = policy.get("semantic_rules") or {}
    return (
        sum(len(v) for v in sender_lists.values())
        + len(policy.get("blacklist_patterns") or [])
        + sum(len(v) for v in semantic_rules.values())
        + len(policy.get("custom_actions") or [])
    )


async def gather_stats() -> dict:
    base_url = _dashboard_base_url()
    headers = {
        "CF-Access-Client-Id": CF_ACCESS_CLIENT_ID,
        "CF-Access-Client-Secret": CF_ACCESS_CLIENT_SECRET,
    }
    cutoff = datetime.now(timezone.utc) - LOOKBACK
    async with httpx.AsyncClient(headers=headers, timeout=30) as client:
        summary, policy, (messages, messages_capped), (rules, _), (actions, _) = await asyncio.gather(
            _get(client, base_url, "/dashboard/api/summary"),
            _get(client, base_url, "/dashboard/api/filtering"),
            _get_window(client, base_url, "/dashboard/api/messages", "received_at", cutoff),
            _get_window(client, base_url, "/dashboard/api/rules", "changed_at", cutoff),
            _get_window(client, base_url, "/dashboard/api/actions", "executed_at", cutoff),
        )

    recent_messages = _since(messages, "received_at", cutoff)
    recent_rules = _since(rules, "changed_at", cutoff)
    recent_actions = _since(actions, "executed_at", cutoff)

    verdict_counts = Counter((m.get("verdict") or "UNKNOWN") for m in recent_messages)
    category_counts = Counter((m.get("category") or "OTHER") for m in recent_messages)
    hard_bounces = [m for m in recent_messages if m.get("enforced_disposition") == "550"]
    action_required = [
        m for m in recent_messages
        if (m.get("alert_level") or "NONE").upper() in ("STANDARD", "URGENT")
    ]

    ledger = []
    for r in recent_rules:
        ledger.append({
            "at": r.get("changed_at"),
            "kind": f"Rule {r.get('action') or 'change'}",
            "detail": r.get("rule_text") or "",
        })
    for a in recent_actions:
        ledger.append({
            "at": a.get("executed_at"),
            "kind": f"Action ({a.get('kind') or 'UNKNOWN'})",
            "detail": a.get("outcome_summary") or a.get("details") or "",
        })
    for m in hard_bounces:
        sender = m.get("from_display") or m.get("from_domain") or "unknown sender"
        ledger.append({
            "at": m.get("received_at"),
            "kind": "Hard bounce",
            "detail": f"{sender} - {m.get('subject') or '(no subject)'}",
        })
    ledger.sort(key=lambda item: item.get("at") or "", reverse=True)

    return {
        "summary": summary or {},
        "recent_messages": recent_messages,
        "messages_capped": messages_capped,
        "rule_count": _standing_rule_count(policy),
        "verdict_counts": verdict_counts,
        "category_counts": category_counts,
        "hard_bounces": hard_bounces,
        "action_required": action_required,
        "ledger": ledger,
        "generated_at": datetime.now(PHOENIX_TZ),
    }


# Terms the insights paragraph may not use to rank counters or to assert that
# one counter explains or matches another. Sentences containing them are dropped.
_RANK_CLAIM_TERMS = (
    "largest",
    "biggest",
    "highest",
    "lowest",
    "most",
    "least",
    "fewest",
    "top",
    "outnumber",
    "outnumbers",
    "outnumbered",
    "majority",
    "share",
    "exceeds",
    "exceeded",
    "dominant",
    "greater than",
    "more than",
    "fewer than",
    "compared to",
    "than any other",
)

_CORRESPONDENCE_CLAIM_TERMS = (
    "because",
    "due to",
    "driven by",
    "as a result of",
    "thanks to",
    "led to",
    "caused by",
    "in response to",
    "correlated with",
    "tracks",
    "tracked",
    "mirrored",
    "in step with",
    "in parallel with",
    "reflects",
    "explains",
    "corresponds to",
    "in line with",
)

# Inflected forms that a plain term list would miss. Each entry is (label, pattern).
_RANK_CLAIM_INFLECTIONS = (
    ("exceed", r"\bexceed(?:s|ed|ing)?\b"),
    ("outnumber", r"\boutnumber(?:s|ed|ing)?\b"),
    ("dominate", r"\bdominat(?:e|es|ed|ing)\b"),
    ("share", r"\bshares\b"),
)

_CORRESPONDENCE_CLAIM_INFLECTIONS = (
    ("track", r"\btrack(?:s|ed|ing)?\b"),
    ("mirror", r"\bmirror(?:s|ed|ing)?\b"),
    ("reflect", r"\breflect(?:s|ed|ing)?\b"),
    ("explain", r"\bexplain(?:s|ed|ing)?\b"),
    ("correspond", r"\bcorrespond(?:s|ed|ing)?\s+to\b"),
    ("lead to", r"\b(?:led|leads|lead|leading)\s+to\b"),
    ("cause", r"\bcaus(?:es|ed|ing)\b"),
    ("drive", r"\bdr(?:ives|iving|ove|iven)\b"),
    ("correlate", r"\bcorrelat(?:e|es|ed|ing)\b"),
)

# The prompt also forbids a trend, a cause, and maintenance advice, so each of
# those gets a matcher of its own. The window has no earlier window to compare
# with, so any change-over-time wording is unsupported.
_TREND_CLAIM_PATTERNS = (
    ("trend", r"\b(?:trend|trends|trending|trended)\b"),
    ("spike", r"\b(?:spike|spikes|spiked|spiking|surge|surges|surged|surging)\b"),
    ("drop", r"\b(?:drop|drops|dropped|dropping|decline|declines|declined|declining)\b"),
    ("rise", r"\b(?:rise|rises|rose|risen|rising|climb|climbs|climbed|climbing)\b"),
    ("increase", r"\b(?:increase|increases|increased|increasing|decrease|decreases|decreased|decreasing)\b"),
    ("change", r"\b(?:jump|jumps|jumped|grew|grow|grows|growth|fell|fall|falls|falling|uptick|downturn)\b"),
    ("earlier window", r"\b(?:yesterday|previous day|prior day|earlier window|last week|baseline)\b"),
    ("than usual", r"\b(?:than usual|than normal|than typical|than before|than earlier|up from|down from)\b"),
    ("usual", r"\b(?:usually|normally|typically|as usual)\b"),
)

_CAUSE_CLAIM_PATTERNS = (
    ("reason", r"\b(?:the reasons?|reasons? for|reasons? why|why)\b"),
    ("stems from", r"\b(?:stem|stems|stemmed|stemming)\s+from\b"),
    ("attributed to", r"\b(?:attributable|attributed|attributing)\s+to\b"),
    ("result of", r"\b(?:resulting from|results? from|result of|owing to|on account of)\b"),
    ("behind", r"\b(?:behind the|underlying)\b"),
)

_ADVICE_CLAIM_PATTERNS = (
    ("should", r"\b(?:should|ought to|must|needs? to|need to be)\b"),
    ("recommend", r"\b(?:recommend|recommends|recommended|recommendation|suggest|suggests|suggested|advise|advised|advisable)\b"),
    ("consider", r"\b(?:consider|considering|you may want|you might want|you could|you can|it would be (?:wise|worth|good|prudent))\b"),
    ("worth", r"\bworth\s+(?:reviewing|checking|pruning|auditing|looking|revisiting|investigating|a look|a review)\b"),
    ("cleanup", r"\b(?:prune|pruning|prunes|pruned|clean up|cleanup|clean-up|tidy|tidying|consolidate|consolidating|declutter)\b"),
)

# A sentence that names none of these is not making a claim about this
# window's counts, so no claim term in it is a violation.
_COUNTER_NOUNS = (
    "message",
    "mail",
    "bounce",
    "rule",
    "action",
    "alert",
    "verdict",
    "categor",
    "spam",
    "phish",
    "hostile",
    "legit",
    "clean",
    "urgent",
    "sender",
    "domain",
    "list",
    "filter",
    "polic",
    "count",
    "share",
    "window",
    "digest",
    "pattern",
)

# "at least 10" and "at most 5" are numeric bounds, not ranking claims.
_RANK_TERM_EXCEPTIONS = ("at least", "at most")


def _mentions_a_counter(sentence: str) -> bool:
    lowered = sentence.lower()
    return any(noun in lowered for noun in _COUNTER_NOUNS)


def _stat_numbers(stats: dict) -> frozenset[str]:
    """The figures the digest already shows, as digit strings. 0 and 1 are left
    out: they turn up in honest prose ("one sender", "0 urgent") too often to
    mean a restatement."""
    summary = stats.get("summary") or {}
    values: list[object] = []
    values.extend((summary.get("last24h") or {}).values())
    values.append((summary.get("last7d") or {}).get("ruleChanges"))
    values.append(stats.get("rule_count"))
    values.extend((stats.get("verdict_counts") or {}).values())
    values.extend((stats.get("category_counts") or {}).values())
    values.append(len(stats.get("action_required") or []))
    values.append(len(stats.get("recent_messages") or []))
    values.append(len(stats.get("hard_bounces") or []))
    return frozenset(
        str(v) for v in values if isinstance(v, int) and not isinstance(v, bool) and v >= 2
    )


def _claim_violations(sentence: str, stat_numbers: frozenset[str] = frozenset()) -> list[str]:
    """The claim classes this sentence makes, if any.

    A rank claim compares two counters against each other; a correspondence
    claim asserts a relationship between counters the statistics do not
    relate; a trend, cause, or advice claim goes beyond a single window; a
    restatement repeats two or more of the figures the tables already show.
    All are unreadable as prose, so they are matched on their connectives and
    figures rather than parsed out of the sentence.
    """
    if not _mentions_a_counter(sentence):
        return []
    lowered = sentence.lower()
    found: list[str] = []
    for kind, patterns in (
        ("trend claim", _TREND_CLAIM_PATTERNS),
        ("cause claim", _CAUSE_CLAIM_PATTERNS),
        ("advice", _ADVICE_CLAIM_PATTERNS),
    ):
        for label, pattern in patterns:
            if re.search(pattern, lowered):
                found.append(f"{kind} ({label!r})")
                break
    if stat_numbers:
        repeated = {n for n in re.findall(r"\b\d+\b", lowered) if n in stat_numbers}
        if len(repeated) >= 2:
            found.append("restatement of the tables")
    for term in _RANK_CLAIM_TERMS:
        if not re.search(r"\b" + re.escape(term) + r"\b", lowered):
            continue
        if any(exc in lowered for exc in _RANK_TERM_EXCEPTIONS):
            continue
        found.append(f"rank claim ({term!r})")
        break
    for label, pattern in _RANK_CLAIM_INFLECTIONS:
        if re.search(pattern, lowered):
            found.append(f"rank claim ({label!r})")
            break
    for term in _CORRESPONDENCE_CLAIM_TERMS:
        if re.search(r"\b" + re.escape(term) + r"\b", lowered):
            found.append(f"correspondence claim ({term!r})")
            break
    for label, pattern in _CORRESPONDENCE_CLAIM_INFLECTIONS:
        if re.search(pattern, lowered):
            found.append(f"correspondence claim ({label!r})")
            break
    return found


def _sentences(paragraph: str) -> list[str]:
    return [s.strip() for s in re.split(r"(?<=[.!?])\s+|\n+", paragraph) if s.strip()]


def _strip_claim_violations(
    paragraph: str, stat_numbers: frozenset[str] = frozenset()
) -> tuple[str, list[str]]:
    """Drop the sentences that make an unsupported claim; keep the rest."""
    kept: list[str] = []
    violations: list[str] = []
    for sentence in _sentences(paragraph):
        found = _claim_violations(sentence, stat_numbers)
        if found:
            violations.extend(found)
        else:
            kept.append(sentence)
    return " ".join(kept).strip(), violations


def _safe_insights(stats: dict) -> str:
    """A paragraph that cannot make an unsupported claim, built from the
    statistics themselves rather than from the model."""
    summary = stats.get("summary") or {}
    last24h = summary.get("last24h") or {}
    total = last24h.get("total", len(stats.get("recent_messages") or []))
    bounces = last24h.get("hardBounces", len(stats.get("hard_bounces") or []))
    actions = last24h.get("actions", 0)
    return (
        f"This window processed {total} messages, with {bounces} hard bounces "
        f"and {actions} actions taken. The breakdown tables below carry the "
        f"counts per category."
    )


async def build_insights(stats: dict, judge: Judge) -> str:
    summary = stats["summary"]
    last24h = summary.get("last24h", {})
    context_lines = [
        f"Messages in the last 24 hours: {last24h.get('total', len(stats['recent_messages']))}",
        f"Hard bounces: {last24h.get('hardBounces', len(stats['hard_bounces']))}",
        f"Urgent alerts: {last24h.get('urgent', 0)}",
        f"Actions taken: {last24h.get('actions', 0)}",
        f"Rule changes in the last 7 days: {summary.get('last7d', {}).get('ruleChanges', 0)}",
        f"Standing rule count: {stats['rule_count']}",
        "Verdict breakdown, last 24 hours: "
        + (", ".join(f"{k}={v}" for k, v in stats["verdict_counts"].items()) or "none"),
        "Category breakdown, last 24 hours: "
        + (", ".join(f"{k}={v}" for k, v in stats["category_counts"].items()) or "none"),
        f"Items currently flagged standard/urgent (need a look): {len(stats['action_required'])}",
    ]
    prompt = f"""You are writing the short insights paragraph for Mercury's daily digest
email, sent once a day to the mailbox owner summarizing the last 24 hours of
their self-hosted spam/phishing filtering pipeline.

The statistics below are counts for a single 24-hour window, plus one
7-day rule-change count. Nothing here is a comparison against an earlier
window: there is no prior day, no baseline, and no trend line. Write only
what this window shows.

Rules:
- Report what this window contains. Do not assert a trend, a spike, a drop, a
  rise, a decline, or a rate of change: no earlier window is supplied here, so
  no such claim is supported by the evidence.
- Do not infer a cause. The ratio of hard bounces to messages is what this
  window shows; why that ratio holds, and what the owner should do about it,
  are not in this data.
- Do not issue maintenance advice, recommend pruning or cleanup, or propose
  actions for the owner. This paragraph reports; it does not direct.
- Do not rank the counters against each other. Do not call any category or
  verdict the largest, the most, the majority, or say that one outnumbers
  another; the breakdown tables already show the counts, and a ranking
  asserts a comparison the single window cannot support.
- Do not assert a relationship between counters the statistics do not
  relate. The counts below are independent. Do not say one of them
  explains, tracks, mirrors, or was caused by another.
- Do not restate the numbers back verbatim; they are already shown elsewhere in
  the email. Prefer what the breakdown reveals that a reader would not see in
  the table.
- If nothing in this window stands out, say so plainly in one sentence instead
  of manufacturing significance.

Respond with only the paragraph itself, nothing else.

Statistics:
{chr(10).join(context_lines)}
"""
    try:
        paragraph = (await judge.ask(prompt)).strip()
    except Exception as exc:
        return f"Insights unavailable this run ({type(exc).__name__}: {exc})."

    cleaned, violations = _strip_claim_violations(paragraph, _stat_numbers(stats))
    if violations:
        logger.warning(
            "Daily digest: dropped %d claim sentence(s) from the insights "
            "paragraph (%s)",
            len(violations),
            "; ".join(violations),
        )
    return cleaned or _safe_insights(stats)


def _esc(value: object) -> str:
    return html.escape(str(value) if value is not None else "")


def _fmt_time(value: str | None) -> str:
    """Human-readable Phoenix-local time. Caller is responsible for escaping
    the result, same as any other value passed through _esc()."""
    parsed = _parse_ts(value)
    if not parsed:
        return str(value) if value is not None else ""
    return parsed.astimezone(PHOENIX_TZ).strftime("%b %-d, %-I:%M %p")


def _quoted(value: object) -> str:
    """Text that came from filtered mail or from the judge, escaped and with its
    hostnames defanged so ForwardEmail's outbound link filter cannot reject the
    digest over a spam domain it quotes."""
    return _esc(defang_hostnames(str(value) if value is not None else ""))


def _truncate(value: object, limit: int) -> str:
    text = str(value) if value is not None else ""
    if len(text) <= limit:
        return text
    return text[: limit - 3].rstrip() + "..."


def _stat_card(label: str, value: object) -> str:
    return f"""<td style="padding:16px 20px;background:#f7f8fa;border-radius:8px;text-align:center;">
<div style="font-size:26px;font-weight:700;color:#1a2733;">{_esc(value)}</div>
<div style="font-size:12px;color:#6b7684;text-transform:uppercase;letter-spacing:0.04em;margin-top:4px;">{_esc(label)}</div>
</td>"""


def _breakdown_table(title: str, counts: Counter) -> str:
    if not counts:
        rows = '<tr><td style="padding:6px 12px;color:#6b7684;">None in the last 24 hours.</td></tr>'
    else:
        rows = "".join(
            f'<tr><td style="padding:6px 12px;border-top:1px solid #e6e8eb;">{_esc(k)}</td>'
            f'<td style="padding:6px 12px;border-top:1px solid #e6e8eb;text-align:right;font-weight:600;">{_esc(v)}</td></tr>'
            for k, v in counts.most_common()
        )
    return f"""<table role="presentation" width="100%" cellpadding="0" cellspacing="0" style="margin-top:8px;font-size:14px;">
<tr><td style="padding:6px 12px;font-weight:700;color:#1a2733;" colspan="2">{_esc(title)}</td></tr>
{rows}
</table>"""


def _ledger_table(ledger: list[dict]) -> str:
    if not ledger:
        return '<p style="font-size:14px;color:#6b7684;">Nothing non-trivial in the last 24 hours.</p>'
    rows = "".join(
        "<tr>"
        f'<td style="padding:8px 12px;border-top:1px solid #e6e8eb;white-space:nowrap;color:#6b7684;font-size:12px;vertical-align:top;">{_esc(_fmt_time(item.get("at")))}</td>'
        f'<td style="padding:8px 12px;border-top:1px solid #e6e8eb;font-weight:600;vertical-align:top;white-space:nowrap;">{_esc(item.get("kind"))}</td>'
        f'<td style="padding:8px 12px;border-top:1px solid #e6e8eb;vertical-align:top;max-width:260px;">{_quoted(_truncate(item.get("detail"), 200))}</td>'
        "</tr>"
        for item in ledger
    )
    return f"""<table role="presentation" width="100%" cellpadding="0" cellspacing="0" style="font-size:14px;border-collapse:collapse;">
<tr>
<th style="text-align:left;padding:8px 12px;font-size:12px;color:#6b7684;">When</th>
<th style="text-align:left;padding:8px 12px;font-size:12px;color:#6b7684;">Kind</th>
<th style="text-align:left;padding:8px 12px;font-size:12px;color:#6b7684;">Detail</th>
</tr>
{rows}
</table>"""


def _action_required_table(items: list[dict]) -> str:
    if not items:
        return '<p style="font-size:14px;color:#6b7684;">Nothing currently needs a look.</p>'
    rows = "".join(
        "<tr>"
        f'<td style="padding:8px 12px;border-top:1px solid #e6e8eb;white-space:nowrap;color:#6b7684;font-size:12px;vertical-align:top;">{_esc(_fmt_time(m.get("received_at")))}</td>'
        f'<td style="padding:8px 12px;border-top:1px solid #e6e8eb;vertical-align:top;font-weight:600;white-space:nowrap;">{_esc(m.get("alert_level"))}</td>'
        f'<td style="padding:8px 12px;border-top:1px solid #e6e8eb;vertical-align:top;max-width:160px;">{_quoted(_truncate(m.get("from_display"), 40))}</td>'
        f'<td style="padding:8px 12px;border-top:1px solid #e6e8eb;vertical-align:top;max-width:220px;">{_quoted(_truncate(m.get("subject"), 60))}</td>'
        f'<td style="padding:8px 12px;border-top:1px solid #e6e8eb;vertical-align:top;max-width:280px;font-size:13px;color:#4b5563;">{_quoted(_truncate(m.get("reasoning"), 180))}</td>'
        "</tr>"
        for m in items
    )
    return f"""<table role="presentation" width="100%" cellpadding="0" cellspacing="0" style="font-size:14px;border-collapse:collapse;">
<tr>
<th style="text-align:left;padding:8px 12px;font-size:12px;color:#6b7684;">When</th>
<th style="text-align:left;padding:8px 12px;font-size:12px;color:#6b7684;">Alert</th>
<th style="text-align:left;padding:8px 12px;font-size:12px;color:#6b7684;">From</th>
<th style="text-align:left;padding:8px 12px;font-size:12px;color:#6b7684;">Subject</th>
<th style="text-align:left;padding:8px 12px;font-size:12px;color:#6b7684;">Why</th>
</tr>
{rows}
</table>"""


def render_html(stats: dict, insights: str) -> str:
    summary = stats["summary"]
    last24h = summary.get("last24h", {})
    generated = stats["generated_at"].strftime("%B %d, %Y %I:%M %p %Z")
    cap_note = (
        '<p style="font-size:12px;color:#9aa3ad;margin-top:4px;">'
        f"More than {MAX_PAGES * PAGE_SIZE} messages arrived today, and this breakdown covers the newest {MAX_PAGES * PAGE_SIZE}.</p>"
        if stats["messages_capped"]
        else ""
    )

    return f"""<div style="max-width:640px;margin:0 auto;font-family:-apple-system,Segoe UI,Roboto,Helvetica,Arial,sans-serif;color:#1a2733;">
<div style="background:#1a2733;color:#ffffff;padding:24px 28px;border-radius:12px 12px 0 0;">
<div style="font-size:20px;font-weight:700;">Mercury daily digest</div>
<div style="font-size:13px;color:#c3cbd4;margin-top:4px;">Generated {_esc(generated)} - last 24 hours</div>
</div>

<div style="border:1px solid #e6e8eb;border-top:none;padding:24px 28px;border-radius:0 0 12px 12px;">

<table role="presentation" width="100%" cellpadding="0" cellspacing="0" style="border-spacing:10px 0;">
<tr>
{_stat_card("Messages", last24h.get("total", len(stats["recent_messages"])))}
{_stat_card("Hard bounces", last24h.get("hardBounces", len(stats["hard_bounces"])))}
{_stat_card("Urgent", last24h.get("urgent", 0))}
{_stat_card("Actions", last24h.get("actions", 0))}
</tr>
</table>
{cap_note}

<div style="margin-top:24px;font-size:15px;line-height:1.5;background:#f0f4f8;border-radius:8px;padding:16px 20px;">
{_quoted(insights)}
</div>

<table role="presentation" width="100%" cellpadding="0" cellspacing="0" style="margin-top:24px;">
<tr>
<td width="50%" style="vertical-align:top;padding-right:12px;">{_breakdown_table("Verdicts", stats["verdict_counts"])}</td>
<td width="50%" style="vertical-align:top;padding-left:12px;">{_breakdown_table("Categories", stats["category_counts"])}</td>
</tr>
</table>

<h3 style="margin-top:28px;margin-bottom:8px;font-size:16px;">Needs a look</h3>
{_action_required_table(stats["action_required"])}

<h3 style="margin-top:28px;margin-bottom:8px;font-size:16px;">Ledger (rule changes, actions, hard bounces)</h3>
{_ledger_table(stats["ledger"])}

<div style="margin-top:28px;text-align:center;">
<a href="{DASHBOARD_LINK}" style="display:inline-block;background:#1a2733;color:#ffffff;text-decoration:none;padding:10px 20px;border-radius:6px;font-size:14px;font-weight:600;">Open the dashboard</a>
</div>

</div>
</div>"""


def _digest_message(subject: str, html_body: str) -> MIMEMultipart:
    msg = MIMEMultipart("alternative")
    msg["Subject"] = subject
    msg["From"] = DIGEST_FROM
    msg["To"] = DIGEST_TO
    msg.attach(MIMEText(f"Mercury daily digest. View it at {DASHBOARD_LINK}", "plain"))
    msg.attach(MIMEText(html_body, "html"))
    return msg


def send_email(subject: str, html_body: str) -> None:
    """Synchronous (smtplib has no async API) - call via asyncio.to_thread.

    The quoted text is already defanged, so a 554 naming a blocked hostname
    means one slipped through in a shape the pattern does not catch; defang
    that hostname in the whole body and retry, a few times, rather than lose
    the day's digest."""
    defanged: set[str] = set()
    while True:
        try:
            with smtplib.SMTP_SSL(SMTP_HOST, SMTP_PORT, timeout=30) as server:
                server.login(SMTP_USER, SMTP_PASSWORD)
                server.send_message(_digest_message(subject, html_body))
            return
        except smtplib.SMTPDataError as error:
            hostname = blocked_hostname(error)
            if (
                not hostname
                or hostname.lower() in defanged
                or len(defanged) >= MAX_DEFANGED_HOSTNAMES
            ):
                raise
            defanged.add(hostname.lower())
            subject = defang(subject, hostname)
            html_body = defang(html_body, hostname)
            logger.warning("Daily digest: link filter blocked %s, retrying defanged", hostname)


async def run_digest_once(judge: Judge) -> None:
    stats = await gather_stats()
    insights = await build_insights(stats, judge)
    html_body = render_html(stats, insights)
    subject = f"Mercury daily digest - {stats['generated_at'].strftime('%B %d, %Y')}"
    await asyncio.to_thread(send_email, subject, html_body)


async def run_forever(judge: Judge) -> None:
    enabled, reason = _digest_enabled()
    if not enabled:
        logger.warning("Daily digest disabled: environment variables %s", reason)
        return
    while True:
        try:
            await asyncio.sleep(_seconds_until_next_7am())
            await run_digest_once(judge)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("Daily digest run failed")
            await asyncio.sleep(60)
