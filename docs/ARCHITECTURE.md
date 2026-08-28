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
2. **If the path is `/ingest`** (the mail-webhook path): the Worker hands
   the payload to the backend in the background and returns 200
   immediately, without waiting on the backend at all. In shadow mode there
   is nothing for the backend's result to change about this response, and
   once enforcement is added, a backend that cannot be reached in time must
   still resolve to "accept" rather than a bounce caused merely by backend
   downtime.
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
   LEGIT / UNSURE) plus a recommended disposition, with reasoning.
7. The notifier provider sends the verdict and reasoning. The message is
   delivered normally regardless of the verdict - shadow mode does not
   enforce anything yet.

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
hand in `rules_ledger.json`, or added through the Thunderbird extension's
`/rules/propose` endpoint, which interprets a flagged message plus a
free-text instruction into one rule via the judge provider and appends it
immediately - there is no confirmation step yet, so every automatic
addition is also reported through the notifier.

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
