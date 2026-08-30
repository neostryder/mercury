"""Daily digest email: once a day, a standalone HTML email summarizing the
last 24 hours of Mercury's activity, sent to the mailbox owner.

The backend has no Cloudflare credentials of its own (see event_log.py), so
this gathers its data the same way the browser dashboard does: authenticated
HTTPS calls to the Worker's existing /dashboard/api/* routes
(worker/src/index.js), reusing the same HTTP Basic Auth rather than adding a
second path into D1. The base URL for those calls is derived from
MERCURY_WORKER_LOG_URL (the Worker's own hostname, already configured for
event_log.py) rather than introducing a redundant env var for the same host.

The insights paragraph reuses the judge provider seam (providers/judge.py)
with the aggregate stats as context, rather than a separate LLM integration.

The /dashboard/api/messages, /rules, and /actions routes each cap out at
their own fixed row limit (100/50/50 respectively, most recent first) - the
same limit the dashboard UI itself is bound by. On a day with unusually high
volume the last-24h breakdown below may undercount; render_html() notes this
inline only when the cap was actually reached.
"""
import asyncio
import html
import logging
import os
import smtplib
from collections import Counter
from datetime import datetime, timedelta, timezone
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from urllib.parse import urlsplit, urlunsplit

import httpx

from providers.judge import Judge

logger = logging.getLogger(__name__)

# Fixed offset, not a DST-aware zoneinfo lookup - Arizona does not observe DST,
# so "next 7am Phoenix time" is always UTC-7 with no seasonal adjustment.
PHOENIX_TZ = timezone(timedelta(hours=-7), name="MST")

WORKER_LOG_URL = os.environ.get("MERCURY_WORKER_LOG_URL")
DASHBOARD_USER = os.environ.get("MERCURY_DASHBOARD_USER")
DASHBOARD_PASSWORD = os.environ.get("MERCURY_DASHBOARD_PASSWORD")
SMTP_USER = os.environ.get("MERCURY_DIGEST_SMTP_USER")
SMTP_PASSWORD = os.environ.get("MERCURY_DIGEST_SMTP_PASSWORD")

SMTP_HOST = "smtp.forwardemail.net"
SMTP_PORT = 465
DIGEST_FROM = "gandalf@rpgm.tools"
DIGEST_TO = "aaron@rpgm.tools"
DASHBOARD_LINK = "https://mercury.rpgm.tools/dashboard"
MESSAGES_ROW_CAP = 100
LOOKBACK = timedelta(hours=24)


def _dashboard_base_url() -> str | None:
    if not WORKER_LOG_URL:
        return None
    parts = urlsplit(WORKER_LOG_URL)
    return urlunsplit((parts.scheme, parts.netloc, "", "", ""))


def _digest_enabled() -> tuple[bool, str]:
    missing = []
    if not WORKER_LOG_URL:
        missing.append("MERCURY_WORKER_LOG_URL")
    if not DASHBOARD_USER:
        missing.append("MERCURY_DASHBOARD_USER")
    if not DASHBOARD_PASSWORD:
        missing.append("MERCURY_DASHBOARD_PASSWORD")
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


async def gather_stats() -> dict:
    base_url = _dashboard_base_url()
    async with httpx.AsyncClient(auth=(DASHBOARD_USER, DASHBOARD_PASSWORD), timeout=30) as client:
        summary, messages, rules, actions = await asyncio.gather(
            _get(client, base_url, "/dashboard/api/summary"),
            _get(client, base_url, "/dashboard/api/messages"),
            _get(client, base_url, "/dashboard/api/rules"),
            _get(client, base_url, "/dashboard/api/actions"),
        )

    cutoff = datetime.now(timezone.utc) - LOOKBACK
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
        "messages_capped": len(messages) >= MESSAGES_ROW_CAP,
        "verdict_counts": verdict_counts,
        "category_counts": category_counts,
        "hard_bounces": hard_bounces,
        "action_required": action_required,
        "ledger": ledger,
        "generated_at": datetime.now(PHOENIX_TZ),
    }


async def build_insights(stats: dict, judge: Judge) -> str:
    summary = stats["summary"]
    last24h = summary.get("last24h", {})
    context_lines = [
        f"Messages in the last 24 hours: {last24h.get('total', len(stats['recent_messages']))}",
        f"Hard bounces: {last24h.get('hardBounces', len(stats['hard_bounces']))}",
        f"Urgent alerts: {last24h.get('urgent', 0)}",
        f"Actions taken: {last24h.get('actions', 0)}",
        f"Rule changes in the last 7 days: {summary.get('last7d', {}).get('ruleChanges', 0)}",
        f"Standing rule count: {summary.get('ruleCount', 0)}",
        "Verdict breakdown, last 24 hours: "
        + (", ".join(f"{k}={v}" for k, v in stats["verdict_counts"].items()) or "none"),
        "Category breakdown, last 24 hours: "
        + (", ".join(f"{k}={v}" for k, v in stats["category_counts"].items()) or "none"),
        f"Items currently flagged standard/urgent (need a look): {len(stats['action_required'])}",
    ]
    prompt = f"""You are writing the short insights paragraph for Mercury's daily digest
email, sent once a day to the mailbox owner summarizing the last 24 hours of
their self-hosted spam/phishing filtering pipeline.

Given the aggregate statistics below, write a short paragraph (2 to 4
sentences) calling out anything actually worth noticing - a spike or drop in
volume, a pattern in what came in, something that needs attention. If
nothing stands out, say so plainly instead of manufacturing significance.
Do not just repeat the numbers back verbatim; they are already shown
elsewhere in the email. Respond with only the paragraph itself, nothing
else.

Statistics:
{chr(10).join(context_lines)}
"""
    try:
        return (await judge.ask(prompt)).strip()
    except Exception as exc:
        return f"Insights unavailable this run ({type(exc).__name__}: {exc})."


def _esc(value: object) -> str:
    return html.escape(str(value) if value is not None else "")


def _fmt_time(value: str | None) -> str:
    """Human-readable Phoenix-local time. Caller is responsible for escaping
    the result, same as any other value passed through _esc()."""
    parsed = _parse_ts(value)
    if not parsed:
        return str(value) if value is not None else ""
    return parsed.astimezone(PHOENIX_TZ).strftime("%b %-d, %-I:%M %p")


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
        f'<td style="padding:8px 12px;border-top:1px solid #e6e8eb;vertical-align:top;max-width:260px;">{_esc(_truncate(item.get("detail"), 200))}</td>'
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
        f'<td style="padding:8px 12px;border-top:1px solid #e6e8eb;vertical-align:top;max-width:160px;">{_esc(_truncate(m.get("from_display"), 40))}</td>'
        f'<td style="padding:8px 12px;border-top:1px solid #e6e8eb;vertical-align:top;max-width:220px;">{_esc(_truncate(m.get("subject"), 60))}</td>'
        f'<td style="padding:8px 12px;border-top:1px solid #e6e8eb;vertical-align:top;max-width:280px;font-size:13px;color:#4b5563;">{_esc(_truncate(m.get("reasoning"), 180))}</td>'
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
        f"The dashboard API returns at most {MESSAGES_ROW_CAP} recent messages - "
        "today's breakdown may undercount if more than that arrived.</p>"
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
{_esc(insights)}
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


def send_email(subject: str, html_body: str) -> None:
    """Synchronous (smtplib has no async API) - call via asyncio.to_thread."""
    msg = MIMEMultipart("alternative")
    msg["Subject"] = subject
    msg["From"] = DIGEST_FROM
    msg["To"] = DIGEST_TO
    msg.attach(MIMEText(f"Mercury daily digest. View it at {DASHBOARD_LINK}", "plain"))
    msg.attach(MIMEText(html_body, "html"))
    with smtplib.SMTP_SSL(SMTP_HOST, SMTP_PORT, timeout=30) as server:
        server.login(SMTP_USER, SMTP_PASSWORD)
        server.send_message(msg)


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
