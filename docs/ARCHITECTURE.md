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

## Approval loop

`/rules/propose` never writes to the ledger directly. It asks the judge
provider to interpret the flagged instruction into a standalone rule for
the ledger, and, separately, whether the instruction also calls for an
immediate action - one of two kinds:

- `MAILBOX: <details>` - something done to mail that already exists (e.g.
  deleting messages already sitting in a folder), narrowly scoped to a
  folder, message set, and action rather than left for the executing step
  to interpret further.
- `UNSUBSCRIBE: <details>` - see "Executing an approved unsubscribe" below.
  An unsubscribe request is not itself a request for a standing rule, so
  the rule half of the proposal is always `NONE` for this kind - whether to
  add one is asked separately, afterward, once the outcome is known.

Both are sent to Telegram as one proposal (`backend/telegram_approvals.py`),
independent of whichever `Notifier` provider is configured for one-way
alerts, since this needs a channel that can receive a reply, not just
deliver a message. The proposal carries inline Approve/Discard buttons
(`callback_query` updates) rather than relying on a reaction - Telegram's
Bot API only delivers `message_reaction` updates when the bot is an
administrator in the chat, a role that cannot exist in a private one-on-one
chat, so a thumbs-up there is never actually received:

- Tapping Approve, or replying "yes", commits the rule to the ledger (if
  there was one) and, if there was an action, hands it to the judge
  provider to carry out (see below).
- Tapping Discard, or replying "no", discards the whole proposal.
- Any other text reply is treated as feedback: the judge revises the
  proposal and a new one is sent, capped at a few rounds so a persistently
  misunderstood proposal can't loop forever.

Proposals are persisted (`backend/approvals.py`,
`pending_approvals.json`) so a backend restart doesn't strand one
mid-conversation.

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
