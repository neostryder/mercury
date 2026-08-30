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
4. The backend extracts and normalizes the sender address, then checks the
   deterministic blacklist (550), greylist (421), and whitelist (250).
   Exact-address matches take precedence over domain matches. The same
   selector cannot exist on more than one list. Any match is a final
   disposition and skips both the prompt-injection classifier and semantic
   judge.
5. For an unmatched sender, the backend redacts any of the recipient's own
   addresses appearing in the message (see below), then scores the redacted
   content with the prompt-injection classifier provider. A message
   classified as an injection attempt remains untrusted data for the rest
   of the pipeline.
6. The redacted content, injection score, and three semantic rule buckets
   are given to the judge provider for a semantic verdict (SPAM / PHISH /
   LEGIT / UNSURE) plus a disposition, category, alert level, and reasoning.
   A matching rule's bucket supplies its disposition and is applied before
   general judgment. The baseline prompt defaults routine transactional mail
   and identifiable business newsletters to LEGIT/250 unless this specific
   message has concrete warning signs. Unfamiliarity alone is not grounds
   for 421.
7. Accepted mail is appended over IMAP. A matching standing custom action
   can change the native target folder or invoke the mailbox-action agent
   for a non-native instruction after delivery.
8. Every disposition is recorded to the event log (see "Event log" below)
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
`X-Mercury-Disposition` headers. The default target is `INBOX`; a native
folder-routing custom action can select another existing folder. A
soft-deferred (421) or hard-bounced (550) message is simply never appended.
Gated by
`MERCURY_DELIVER_ACCEPTED_MAIL` (default off) - turning it on must happen
in lockstep with removing the mailbox's own address from every affected
alias, never independently, or a message would be delivered twice.

## Event log

Every disposition, filtering-policy change, and mailbox/unsubscribe action
is recorded to a Cloudflare D1 database (`worker/schema.sql`) - the
foundation for a dashboard and daily summary digest. The backend has no Cloudflare
credentials of its own; it reaches D1 through an authenticated `/log` route
on the Worker gate (`worker/src/index.js`), which already holds the D1
binding, via `backend/event_log.py`. Logging is fire-and-forget: a failure
there never affects delivery of the message it was logging. Deterministic
matches use an explicit `SKIPPED_*` injection label plus list-match reasoning
and the `SENDER_LIST` category instead of leaving judge fields blank. A hard-bounce recommendation also
saves the message's full content and reasoning so it can be reviewed later.

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

## Persisted filtering policy

`backend/filtering.py` wraps one versioned JSON file at
`MERCURY_RULES_LEDGER_PATH` (normally `/data/rules_ledger.json`). The path is
unchanged from the flat ledger so deployment does not need a coordinated
file rename. Writes use a same-directory temporary file, `fsync`, and
`os.replace` for atomic replacement. The version 2 shape is:

```json
{
  "version": 2,
  "sender_lists": {
    "blacklist": [],
    "greylist": [],
    "whitelist": []
  },
  "semantic_rules": {
    "550": [],
    "421": [],
    "250": []
  },
  "custom_actions": [],
  "migration_warnings": []
}
```

Sender selectors are normalized lowercase domains or exact addresses. An
add operation first removes the same selector from the other sender lists.
At match time the exact address is checked before its domain. A malformed
file containing a cross-list duplicate is rejected as ambiguous, causing
the ingest path to fail open and send its ordinary pipeline-error alert.

Semantic rules describe content or context conditions. The `550`, `421`, or
`250` bucket is the disposition, and the three labeled blocks are supplied
to `judge_email()` before its general calibration instructions. The judge's
`RULE_MATCH` response must still equal a stored rule exactly before it is
trusted for dashboard reversal.

Custom actions contain a selector, free-text instruction, and an optional
native folder action. One entry is stored per selector, and exact-address
matching takes precedence over domain matching. A native folder action
changes the IMAP APPEND target. Any other instruction runs through the
scoped mailbox-action agent after successful delivery.

The first read of a legacy `{"rules": [...]}` object migrates the known
policy in place:

- `kickstarnow.com`, `kickstartrack.com`, and `mail.beehiiv.com` become
  deterministic blacklist domains.
- `immail.fanatical.com` becomes a deterministic whitelist domain. Its old
  one-time unsubscribe text is not retained as a standing action.
- The lewd/dating/images condition and the clearly unsolicited spam
  condition become semantic 550 rules.
- The PayPal/GOG exception is omitted because the general judge calibration
  now handles ordinary legitimate business mail.
- The Nellis Auction archive test is omitted and custom actions start empty.

Any other legacy string is preserved in `migration_warnings`, visible in the
dashboard, rather than assigned a guessed disposition.

The browser dashboard reads and mutates the live policy through
`/dashboard/api/filtering`. The authenticated Worker proxies that route to
the backend's `GET /filtering` and `POST /filtering` endpoints with the
shared secret, so the policy file and secret never reach browser code.
Mutation bodies are capped at 32 KB at the Worker. Dashboard changes are
logged to the existing `rule_changes` event table.

## Approval loop: briefs, not forms

`/rules/propose` never writes to the policy directly, and it does not treat
a flagged instruction as a rigid form to fill in. It opens a **brief** - an
open-ended collaboration with the judge provider (Loremaster) about a
flagged message, re-interpreted as a whole conversation rather than parsed
atomically: a message expressing several wishes at once is decomposed into
whatever combination of filtering changes and an action actually
accomplishes the intent, not transcribed verbatim. Each turn, `advance_brief()`
(`backend/app.py`) is grounded with two pipeline facts before deciding -
the mailbox's real IMAP folder list (`mail_delivery.list_folders()`), so it
never invents a folder that doesn't exist, and the fact that a 421
(soft-defer) or 550 (hard-bounce) disposition means the message was never
delivered anywhere at all (Mercury keeps no usable copy of a 421; a
hard-bounce's content is retained only for review, not redelivery) - so
"un-defer" or "restore" requests about already-rejected mail can never
become a real `MAILBOX` action, only a future filtering change plus an
honest `CAVEAT` about what can't be recovered. It then decides among six
independent fields, only one of which forces a stop:

- `QUESTION` - genuinely unclear intent, or a real design choice that
  depends on the recipient's answer, is asked directly rather than guessed
  at. This is the ordinary way to handle a brief that isn't ready for a
  filtering change or action yet, not a rare fallback - a question is
  mutually exclusive with proposing anything that same turn.
- `SENDER_LIST` - a deterministic blacklist, greylist, or whitelist proposal
  with a domain or exact address. The judge defaults to an organization's
  own domain and uses an exact address for a shared/public provider where
  one account says nothing about the domain.
- `SEMANTIC_RULE` - a 550, 421, or 250 content/context condition. Its text is
  standalone but does not repeat the disposition supplied by its bucket.
- `CUSTOM_ACTION` - a per-sender standing instruction plus an optional real
  IMAP folder for native routing.
- `ACTION` - one of two kinds, described below, or `NONE`.
- `CAVEAT` - a direct heads-up, independent of the proposal fields. Two
  things it checks: whether a proposed semantic rule actually adds
  distinguishing criteria beyond what the baseline verdict step (SPAM,
  PHISH, LEGIT, UNSURE, and the disposition that follows from it) would
  already do on its own - a rule that just restates "obviously bad mail
  should be blocked" is likely to never be the deciding factor - and
  whether part of what was asked isn't actually achievable given the
  pipeline facts above. Either way the recipient sees the heads-up before
  approving, not after. A caveat with no change and no action still reaches
  Telegram as its own message rather than being silently dropped as
  "nothing to add or do."

An `ACTION`, when present, is one of:

- `MAILBOX: <details>` - something done to mail that already exists (e.g.
  deleting messages already sitting in a folder), narrowly scoped to a
  folder, message set, and action rather than left for the executing step
  to interpret further.
- `UNSUBSCRIBE: <details>` - see "Executing an approved unsubscribe" below.
  An unsubscribe request is not itself a standing sender decision. A
  blacklist follow-up is proposed separately after the result and requires
  its own approval.

Sent to Telegram (`backend/telegram_approvals.py`), independent of whichever
`Notifier` provider is configured for one-way alerts, since this needs a
channel that can receive a reply, not just deliver a message. A question is
plain text; a filtering-change and/or action proposal carries inline
Approve/Discard buttons (`callback_query` updates) rather than relying on a reaction -
Telegram's Bot API only delivers `message_reaction` updates when the bot is
an administrator in the chat, a role that cannot exist in a private
one-on-one chat, so a thumbs-up there is never actually received:

- Tapping Approve, or replying "yes" to an active proposal, commits the
  filtering changes (if present) and, if there was an action, hands
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
  reopens the policy or takes an action itself; that requires a new brief
  or the dashboard's own reverse-rule control.

Briefs are persisted (`backend/approvals.py`, `pending_approvals.json`) -
full turn history and a message-id-to-brief index, not just the latest
proposal - so a backend restart doesn't strand one mid-conversation or
orphan a reply to an older message in the thread.

## Telegram decisions for questionable messages

Every STANDARD or URGENT verdict report has four inline buttons:
Unsubscribe, Soft-bounce, Hard-bounce, and Deliver + whitelist. The report
brief temporarily retains the original raw message so a later Deliver tap
can append a message that was initially deferred. The raw copy is removed
from persisted approval state as soon as a decision runs or the brief
resolves.

The SMTP webhook response has already completed by the time Telegram sends a
callback. A Soft-bounce or Hard-bounce tap therefore cannot rewrite the
original response retroactively. It does not manually deliver the message;
a message originally given 421 or 550 remains undelivered, while a message
already accepted cannot be retracted. The matching greylist or blacklist
entry is then proposed and governs a sender retry or future message only
after a separate Approve tap. Deliver appends
the retained raw message immediately unless it was already delivered, then
proposes a whitelist entry. The duplicate-delivery guard is important for a
STANDARD or URGENT report whose original disposition was already 250.
Unsubscribe uses the existing safe unsubscribe executor and then proposes a
blacklist entry.

The first decision tap approves only the one-message action. Every standing
sender change is shown afterward as an ordinary Approve/Discard proposal.
Free-text replies remain available on the original report and continue the
same brief.

## Executing an approved mailbox action

An approved `MAILBOX` action is not carried out by the backend itself - it
is handed to the same judge provider (the agent behind the gateway) as a
further prompt, on the understanding that it will use its own scoped
mailbox-action skill (folder-and-action-limited, e.g. delete-within-Spam-only)
to do it and report back what happened. The backend's IMAP credentials are
limited to listing folders and appending accepted messages, including native
folder routing. Broader one-time mailbox work and non-native standing custom
actions remain behind the scoped agent prompt. A one-time action requires
explicit approval; a standing custom action runs on matching future messages
under the previously approved stored instruction.

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
(not free text, for the same reason the typed proposal fields are parsed
rather than inferred) and reports `UNSUBSCRIBED`, `FAILED`, or
`SKIPPED_UNSAFE` plus the summary. No sender entry is committed at this
point. A blacklist entry is then presented as a normal Approve/Discard
proposal, so the unsubscribe result never decides standing disposition on
the recipient's behalf.

## Prompt injection: why it shapes this design

The judge step is an LLM (potentially an agentic one) reading content that
came from an untrusted, attacker-controlled sender. That is the textbook
setup for a prompt-injection attempt: a message crafted to look like
instructions to whatever is reading it, rather than content to be judged.
For senders without a deterministic list match, the classifier step exists
specifically to surface that risk before the judge sees the message. The
judge's own prompt is written to treat the message body as data, never as instructions,
regardless of what the classifier found. Any agent given broader access to
a mailbox (for search, for the Thunderbird flagging flow, or anything
else) should carry the same discipline: mail content is never a source of
commands. Deterministic sender decisions intentionally bypass both content
scans. In particular, a compromised or spoofed whitelisted sender can pass
until the whitelist entry is removed; that is the explicit reliability and
cost tradeoff of a hard whitelist.
