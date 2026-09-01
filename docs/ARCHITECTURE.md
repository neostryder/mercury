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
   selector cannot exist on more than one list. Before honoring a match, the
   backend requires ForwardEmail's own `dmarc` verdict (already included in
   the webhook payload alongside `spf`/`dkim`/`arc`/`bimi`) to report a pass
   for the claimed `From:` domain. A clear authenticated match is a final
   disposition and skips both the prompt-injection classifier and semantic
   judge. A missing, failed, or malformed verdict falls through to step 5 as
   though no sender-list match existed.
5. Without an authenticated sender-list match, the backend redacts any of
   the recipient's own addresses appearing in the message (see below), then
   scores the redacted content with the prompt-injection classifier provider.
   A message classified as an injection attempt remains untrusted data for
   the rest of the pipeline.
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
and the `SENDER_LIST` category instead of leaving judge fields blank. When an
unauthenticated match is skipped, that reason is prepended to the normal
classifier/judge reasoning so it remains visible in the dashboard and digest.
A hard-bounce recommendation also saves the message's full content and
reasoning so it can be reviewed later.

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
`os.replace` for atomic replacement. The version 3 shape is:

```json
{
  "version": 3,
  "sender_lists": {
    "blacklist": [],
    "greylist": [],
    "whitelist": []
  },
  "blacklist_patterns": [],
  "semantic_rules": {
    "550": [],
    "421": [],
    "250": []
  },
  "custom_actions": [],
  "migration_warnings": []
}
```

A version 2 file on disk is upgraded to version 3 in place on first load
(`blacklist_patterns` defaults to an empty list), so no manual migration
step is needed on deploy.

Sender selectors are normalized lowercase domains or exact addresses. An
add operation first removes the same selector from the other sender lists.
At match time the exact address is checked before its domain. A malformed
file containing a cross-list duplicate is rejected as ambiguous, causing
the ingest path to fail open and send its ordinary pipeline-error alert.

`blacklist_patterns` holds regexes for the same rotating-domain spam
campaigns the exact-match blacklist otherwise needs one entry per domain
for (a numeric prefix plus a word before the TLD is a common shape). Each
pattern is compiled and validated when it is added, and matched with
`re.fullmatch` against the normalized domain only - never the full address
or message body - which keeps a pathological pattern's blast radius bounded
by the same 253-character domain length cap the exact-match path already
enforces. Exact sender-list matches (blacklist, greylist, or whitelist) are
always checked first; a pattern is only consulted once none of the three
exact lists match, so an exact whitelist or greylist entry always wins over
a broader pattern that would otherwise also catch that domain. Pattern
matches use the blacklist disposition (550) exclusively - unlike exact
selectors, a pattern cannot be added to the greylist or whitelist, since a
false-positive bounce is recoverable by the sender while a false-positive
pattern match against the whitelist would hand a spammer full bypass trust.

Sender authentication uses ForwardEmail's own `dmarc` webhook field
(`backend/filtering.py`'s `sender_domain_is_authenticated()`) rather than
parsing an `Authentication-Results` header out of the raw message. The raw
message can carry a forged header claiming to be from some OTHER mail
server's identity - ForwardEmail only strips assertions forged as its own
identity, not foreign ones - so trusting raw headers directly would reopen
the same spoofing gap this check exists to close. DMARC's own alignment
check already answers "does SPF or DKIM align with the visible `From:`
domain", evaluated by ForwardEmail itself against the same trust boundary
every other webhook field comes from. Only a `dmarc.status.result` of
exactly `"pass"` (case-insensitively) is accepted; anything else - missing,
`fail`, `none` (no published policy), `temperror`, or a malformed field -
fails closed. The skip reason is prepended to the normal judge reasoning in
the message event, making the fallback visible in the dashboard and daily
digest.

A domain with no published DMARC policy never takes the deterministic fast
path in either direction - it always falls through to full content
screening instead. This is deliberate: whether a message actually came from
that domain is exactly the ambiguous case worth failing closed on before
letting it skip content screening entirely.

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
honest `CAVEAT` about what can't be recovered. It then decides among seven
independent fields, only one of which forces a stop:

- `QUESTION` - genuinely unclear intent, or a real design choice that
  depends on the recipient's answer, is asked directly rather than guessed
  at. This is the ordinary way to handle a brief that isn't ready for a
  filtering change or action yet, not a rare fallback - a question is
  mutually exclusive with everything else that same turn, `REPLY` included.
- `REPLY` - a direct, honest answer when the recipient asked or said
  something that needs a real response and no other field already covers
  it (e.g. confirming whether an earlier action actually happened, or
  admitting a misread). Can stand alone, or introduce a fresh proposal
  below it.
- `SENDER_LIST` - a deterministic blacklist, greylist, or whitelist proposal
  with a domain or exact address. The judge defaults to an organization's
  own domain and uses an exact address for a shared/public provider where
  one account says nothing about the domain.
- `SEMANTIC_RULE` - a 550, 421, or 250 content/context condition. Its text is
  standalone but does not repeat the disposition supplied by its bucket.
- `CUSTOM_ACTION` - a per-sender standing instruction plus an optional real
  IMAP folder for native routing.
- `ACTION` - one of three kinds, described below, or `NONE`.
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
  blacklist follow-up is proposed separately after the final result and
  requires its own approval. An intermediate `NEEDS_SIGNIN` result opens the
  one-time credential path described below and never proposes a bounce by
  itself.
- `GANDALF: <note>` - hands the note and flagged message context to the
  separate Gandalf/Loremaster system as a plain-text email at
  `gandalf@rpgm.tools`. The relay uses SMTP over SSL with
  `MERCURY_MAILBOX_SMTP_HOST` and `MERCURY_MAILBOX_SMTP_PORT`, and authenticates
  with the mailbox's existing `MERCURY_MAILBOX_IMAP_USER` and
  `MERCURY_MAILBOX_IMAP_PASSWORD` credentials.

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
  the first one. There is no round limit that force-abandons an unresolved
  brief; a genuinely unclear one just keeps getting asked about.
- A reply to an already-resolved brief is re-run through the exact same
  `advance_brief()` used for the first message and every open-brief reply,
  never a separate conversational-only path - a resolved brief is only the
  last round's outcome, not a lock. It can answer a question about what
  happened (a new `REPLY` field in the response format, alongside the
  existing `QUESTION`/`SENDER_LIST`/`SEMANTIC_RULE`/`CUSTOM_ACTION`/`ACTION`/
  `CAVEAT` ones), and it can just as well propose a correction or a
  brand-new change or action when the reply calls for one - e.g. "do it"
  after being told an earlier request was never actually carried out
  proposes the action right then, rather than requiring the message be
  re-flagged from Thunderbird to start over. Approving or discarding a
  proposal always clears the brief's stored `changes`/`action`/`caveat`
  alongside resolving it, so a later unrelated "yes" can never land on a
  proposal that already committed or was discarded.

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
header, or a link in the body) and verify the link's domain before visiting
it. The domain, and every redirect domain, must have a clear relationship to
the sender or be a known mailing-list provider acting for it. An unrelated
domain, payment request, non-login credential request, phishing indicator,
or uncertain relationship is unsafe and is never visited.

If safe: tracking query parameters are stripped from the URL before
visiting it, and only a single confirm click or form submit is attempted -
anything more involved stops rather than treating it as `FAILED` instead of
improvising further.
If unsafe: the link is never visited at all, and the result is
`SKIPPED_UNSAFE`.

A normal account login wall is a separate result. If and only if its domain
passes the same sender-relationship check, the judge stops without entering
anything and returns `SAFE: yes` with `RESULT: NEEDS_SIGNIN`. A login wall on
an unrelated, suspicious, or lookalike domain remains `SAFE: no` with
`RESULT: SKIPPED_UNSAFE`, so it can never trigger a credential request.

For `NEEDS_SIGNIN`, the backend creates a 256-bit random token in
`backend/credential_prompts.py` and posts a link to
`https://mercury.rpgm.tools/credential/{token}` into the same brief. The
message is added to the brief history and its Telegram message ID is indexed,
so a reply continues the normal conversation. The Worker serves a
self-contained username/password form and proxies it to the backend's
`/credential-prompt/{token}` endpoints. The token itself is the authorization
for these two endpoints; the dashboard shared secret is not exposed to the
phone. Both Worker and backend reject bodies over 4 KB.

The pending entry is memory-only and expires 10 minutes after creation. It is
never part of `ApprovalStore` or any file. A successful POST is single-use,
and the waiting unsubscribe task removes the submitted username and password
from the entry immediately after its one retrieval. Expired entries are also
discarded opportunistically. A restart intentionally loses an in-flight
entry.

If no submission arrives in time, the brief reports
`NEEDS_SIGNIN_TIMED_OUT`, writes that credential-free outcome to the event
log, and does not propose a bounce. After a successful submission, a second
judge call receives the credential only for this one approved login on the
verified domain. It must not repeat either value and remains limited to a
normal unsubscribe confirmation. Any MFA or 2FA challenge stops immediately
as `FAILED` with a manual-completion summary. The backend records only this
resolved second result and scrubs a submitted value from the summary even if
the judge repeats it contrary to instructions. This capability is not wired
into `MAILBOX` or standing `CUSTOM_ACTION` execution.

The backend parses a structured `SAFE / DOMAIN / RESULT / SUMMARY` reply
(not free text, for the same reason the typed proposal fields are parsed
rather than inferred) and accepts `UNSUBSCRIBED`, `FAILED`,
`SKIPPED_UNSAFE`, or the intermediate `NEEDS_SIGNIN` result. No sender entry
is committed at this point. After a final result, a blacklist entry is
presented as a normal Approve/Discard proposal, so the unsubscribe result
never decides standing disposition on the recipient's behalf.

## Prompt injection: why it shapes this design

The judge step is an LLM (potentially an agentic one) reading content that
came from an untrusted, attacker-controlled sender. That is the textbook
setup for a prompt-injection attempt: a message crafted to look like
instructions to whatever is reading it, rather than content to be judged.
For senders without an authenticated deterministic list match, the classifier
step exists specifically to surface that risk before the judge sees the
message. The judge's own prompt is written to treat the message body as data,
never as instructions, regardless of what the classifier found. Any agent
given broader access to a mailbox (for search, for the Thunderbird flagging
flow, or anything else) should carry the same discipline: mail content is
never a source of commands. Authenticated deterministic sender decisions
intentionally bypass both content scans. A whitelisted sender whose account
or signing infrastructure is later compromised can still pass until the
whitelist entry is removed; that remains the explicit reliability and cost
tradeoff of a hard whitelist. A bare `From:` spoof without a DMARC pass
reported by ForwardEmail falls through to both content scans.
