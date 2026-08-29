# Architecture

## Why a Worker in front of a self-hosted backend

Most mail-hosting providers' incoming-mail webhooks are synchronous:
whatever HTTP status the webhook endpoint returns can determine whether the
message is accepted, soft-deferred, or hard-bounced at SMTP time. That
makes uptime of the webhook endpoint itself a direct input to whether
legitimate mail gets bounced - and providers commonly classify a
webhook-endpoint outage as a permanent failure rather than a retryable one,
regardless of the underlying cause. (I use ForwardEmail; its bounce
classification, in its own open-source code, does not treat a 502/503/504
from an unreachable origin as retryable.)

A self-hosted machine cannot offer the uptime this requires: any outage
during that synchronous webhook call would surface as a definite HTTP
error and get treated as permanent. Splitting the gate onto a Cloudflare
Worker (their edge network, not any one machine) removes the self-hosted
box from that path entirely - the address the webhook calls is never down
in the way a home server or a single VPS can be. Any always-on edge/serverless
platform your mail host's webhook can be pointed at would serve the same
purpose; Cloudflare Workers is what I already had DNS on.

## Pipeline

1. Your mail host POSTs the parsed message to the Worker's public hostname.
2. **If the path is `/ingest`** (the mail-webhook path): the Worker waits
   on the backend's response and returns its disposition (250/421/550) as
   the webhook's own HTTP status - this is what ForwardEmail's webhook
   contract expects, per the design goal above of an edge gate producing
   the actual bounce decision. But a backend that's unreachable, slow past
   a fixed timeout, or returns anything other than one of those three
   recognized codes must still resolve to "accept" (250) - infrastructure
   trouble is never itself a legitimate reason to bounce a message.
3. **Any other path** (e.g. `/rules/propose`, used by the Thunderbird
   extension) is proxied synchronously - the caller is a direct,
   interactive user action, not something under SMTP bounce-risk, so the
   Worker waits for the backend's real response and returns it as-is.
4. The backend redacts any of the recipient's own addresses appearing in
   the message (see below) before the content is used in any further step.
5. The redacted content is scored by the prompt-injection classifier
   provider. A message classified as an injection attempt is treated as
   untrusted data for the rest of the pipeline - its content is never
   treated as instructions by the judge.
6. The redacted content, the injection score, and the current rules ledger
   are given to the judge provider for a semantic verdict (SPAM / PHISH /
   LEGIT / UNSURE) plus a recommended disposition, a category, an alert
   level (none / standard / urgent - the judge's own call on whether this
   is worth a same-day ping), and reasoning.
7. Every verdict is recorded to the event log (see "Event log" below)
   regardless of alert level. The notifier provider only actually sends to
   Telegram when the alert level is standard or urgent - routine traffic,
   including most hard bounces, is left for the daily summary and dashboard
   instead of pinging on every message. The disposition enforced is what
   the Worker actually returned to the mail host in step 2 - unless
   `MERCURY_SHADOW_MODE=true` is set, which reverts to reporting only (the
   backend always returns 250/accept in that mode) without a code change or
   redeploy.

## Delivering an accepted message

ForwardEmail's webhook is a terminal, parallel delivery target - its
response is read only for logging and never used to add headers or
otherwise influence a separately-configured mailbox recipient on the same
alias (confirmed against ForwardEmail's own source, `helpers/on-data-mx.js`).
That means the webhook's disposition can only actually gate delivery if the
mailbox's own address is removed from an alias's recipient list entirely,
leaving the webhook as the sole recipient - at which point something has to
take over delivering an accepted message onward.

That something is Mercury itself: on a 250 (accept) outside shadow mode,
`backend/mail_delivery.py` delivers the original raw message (included in
the webhook payload, not a reconstruction) into the real mailbox via IMAP
APPEND, tagged with `X-Mercury-Verdict`, `X-Mercury-Category`, and
`X-Mercury-Disposition` headers. A soft-deferred (421) or hard-bounced
(550) message is simply never appended. Gated by
`MERCURY_DELIVER_ACCEPTED_MAIL` (default off) - turning it on must happen
in lockstep with removing the mailbox's own address from every affected
alias, never independently, or a message would be delivered twice.

## Event log

Every verdict, rule change, and mailbox/unsubscribe action is recorded to a
Cloudflare D1 database (`worker/schema.sql`) - the foundation for a
dashboard and daily summary digest. The backend has no Cloudflare
credentials of its own; it reaches D1 through an authenticated `/log` route
on the Worker gate (`worker/src/index.js`), which already holds the D1
binding, via `backend/event_log.py`. Logging is fire-and-forget: a failure
there never affects delivery of the message it was logging. A hard-bounce
recommendation also saves the message's full content and the judge's
reasoning, so it can be reviewed - and a rule reversed - without having had
to catch it live.

## Providers

`backend/providers/` holds three seams, each a small `Protocol` plus one
built-in implementation, selected by a `get_*()` factory function reading
an environment variable:

- `classifier.py` - `InjectionClassifier`. Built-in: a generic HTTP POST to
  any server implementing `{"text"} -> {"label", "score"}`.
- `judge.py` - `Judge`. Built-in: HTTP POST to an agent gateway (see
  `gateway/README.md`) implementing `{"prompt"} -> {"response"}`.
- `notifier.py` - `Notifier`. Built-in: a Telegram bot message.

Swapping any one of these to a different backing service means implementing
the same protocol and changing the corresponding `get_*()` function - the
rest of `app.py` only calls the interface.

## Redaction

Before any content leaves the backend process for the classifier or judge,
addresses belonging to the mailbox owner are masked to `first three
characters + "*"`, domain intact (e.g. `someone@example.com` becomes
`som*@example.com`). Which addresses count as "the mailbox owner" is
configured at deploy time (`backend/identities.json`, gitignored - see
`identities.json.example` for the format), not committed to source. This
applies uniformly regardless of which provider is being called for a given
step. It does not apply to the message as actually delivered or
quarantined - only to the copy used for classification.

## Rules ledger

A standing set of natural-language handling rules, supplied over time
(for example: "messages from this TLD should soft-bounce except when sent
to this specific address"). The ledger is given to the judge alongside
each new message rather than compiled into fixed logic, so a new rule takes
effect on the next message without a code change. Rules can be edited by
hand in `rules_ledger.json`, or proposed through the Thunderbird extension's
`/rules/propose` endpoint - see "Approval loop" below for how a proposal
there actually becomes a committed rule.

## Approval loop: briefs, not forms

`/rules/propose` never writes to the ledger directly, and it does not treat
a flagged instruction as a rigid form to fill in. It opens a **brief** - an
open-ended collaboration with the judge provider (Loremaster) about a
flagged message, re-interpreted as a whole conversation rather than parsed
atomically. Each turn, `advance_brief()` (`backend/app.py`) is handed the
full history plus the latest message and decides among four independent
things, only one of which forces a stop:

- `QUESTION` - genuinely unclear intent, or a real design choice that
  depends on the recipient's answer, is asked directly rather than guessed
  at. This is the ordinary way to handle a brief that isn't ready for a
  rule or action yet, not a rare fallback - a question is mutually
  exclusive with proposing anything that same turn.
- `RULE` - a standing preference, self-contained enough to stand alone in
  the ledger with no access to this conversation once added. `NONE` for a
  one-time request about existing mail with no lasting preference implied.
- `ACTION` - one of two kinds, described below, or `NONE`.
- `CAVEAT` - only alongside a `RULE`: whether the rule actually adds
  distinguishing criteria beyond what the baseline verdict step (SPAM,
  PHISH, LEGIT, UNSURE, and the disposition that follows from it) would
  already do on its own. A rule that just restates "obviously bad mail
  should be blocked" is likely to never be the deciding factor, and the
  recipient sees that heads-up before approving, not after.

An `ACTION`, when present, is one of:

- `MAILBOX: <details>` - something done to mail that already exists (e.g.
  deleting messages already sitting in a folder), narrowly scoped to a
  folder, message set, and action rather than left for the executing step
  to interpret further.
- `UNSUBSCRIBE: <details>` - see "Executing an approved unsubscribe" below.
  An unsubscribe request is not itself a request for a standing rule, so
  the rule half is always `NONE` for this kind - whether to add one is
  asked separately, afterward, once the outcome is known.

Sent to Telegram (`backend/telegram_approvals.py`), independent of whichever
`Notifier` provider is configured for one-way alerts, since this needs a
channel that can receive a reply, not just deliver a message. A question is
plain text; a rule and/or action proposal carries inline Approve/Discard
buttons (`callback_query` updates) rather than relying on a reaction -
Telegram's Bot API only delivers `message_reaction` updates when the bot is
an administrator in the chat, a role that cannot exist in a private
one-on-one chat, so a thumbs-up there is never actually received:

- Tapping Approve, or replying "yes" to an active proposal, commits the
  rule to the ledger (if there was one) and, if there was an action, hands
  it to the judge provider to carry out (see below).
- Tapping Discard, or replying "no", ends the brief without committing
  anything.
- Any other reply - to a question, to a proposal, to the "approved,
  working on it" notice, or to the final outcome - continues the same
  brief: every message Mercury sends is tracked back to its brief, not just
  the first one, capped at a few rounds so a persistently unresolved brief
  can't loop forever.
- A reply to an already-resolved brief (challenging or asking about a
  decision already made) is answered from the full history by
  `discuss_resolved_brief()` - purely conversational, since it never
  reopens the ledger or takes an action itself; that requires a new brief
  or the dashboard's own reverse-rule control.

Briefs are persisted (`backend/approvals.py`, `pending_approvals.json`) -
full turn history and a message-id-to-brief index, not just the latest
proposal - so a backend restart doesn't strand one mid-conversation or
orphan a reply to an older message in the thread.

## Executing an approved mailbox action

An approved `MAILBOX` action is not carried out by the backend itself - it
is handed to the same judge provider (the agent behind the gateway) as a
further prompt, on the understanding that it will use its own scoped
mailbox-action skill (folder-and-action-limited, e.g. delete-within-Spam-only)
to do it and report back what happened. This keeps the backend from ever
needing standing mailbox-write credentials of its own: the only thing that
can actually touch the mailbox is the already-vetted agent, and only after
explicit, per-action human approval. See "Prompt injection: why it shapes
this design" below for why that boundary matters here specifically.

## Executing an approved unsubscribe

An approved `UNSUBSCRIBE` action is also handed to the judge provider, using
its browsing skill, but with an evaluation step first: it is prompted to
find the flagged message's unsubscribe mechanism (a `List-Unsubscribe`
header, or a link in the body) and judge whether the route is safe -
unsafe if the link's domain has no clear relationship to the sender or a
known mailing-list provider acting for it, if the page asks for credentials
or payment details, or if anything about it looks like a phishing attempt
rather than a standard opt-out; uncertain is treated as unsafe.

If safe: tracking query parameters are stripped from the URL before
visiting it, and only a single confirm click or form submit is attempted -
anything more involved stops rather than treating it as `FAILED` instead of
improvising further.
If unsafe: the link is never visited at all, and the result is
`SKIPPED_UNSAFE`.

The backend parses a structured `SAFE / DOMAIN / RESULT / SUMMARY` reply
(not free text - the same reason the rule/action split above is parsed
rather than inferred) and reports the result back to Telegram immediately -
`UNSUBSCRIBED`, `FAILED`, or `SKIPPED_UNSAFE`, plus the summary. No rule is
committed at this point. Separately, the recipient is then asked (again via
inline buttons) whether to add the sending domain to the blacklist (hard
bounce), the greylist (soft bounce), or leave it alone - an unsubscribe
request is not itself a request for a standing rule, so that decision is
always its own explicit step rather than something the safety judgment
decides on the recipient's behalf.

## Prompt injection: why it shapes this design

The judge step is an LLM (potentially an agentic one) reading content that
came from an untrusted, attacker-controlled sender. That is the textbook
setup for a prompt-injection attempt: a message crafted to look like
instructions to whatever is reading it, rather than content to be judged.
The classifier step exists specifically to surface that risk as its own
signal *before* the judge ever sees the message, and the judge's own prompt
is written to treat the message body as data, never as instructions,
regardless of what the classifier found. Any agent given broader access to
a mailbox (for search, for the Thunderbird flagging flow, or anything
else) should carry the same discipline: mail content is never a source of
commands.
