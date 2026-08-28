# Architecture

## Why a Worker in front of a self-hosted backend

ForwardEmail's incoming-mail webhook is synchronous: whatever HTTP status
the webhook endpoint returns can determine whether the message is accepted,
soft-deferred, or hard-bounced at SMTP time. That makes uptime of the
webhook endpoint itself a direct input to whether legitimate mail gets
bounced.

A self-hosted machine cannot offer the uptime this requires: any outage
during the (synchronous) webhook call would otherwise surface to
ForwardEmail as a definite HTTP error, which their bounce classification
treats as permanent rather than retryable regardless of the underlying
cause. Splitting the gate onto Cloudflare's own edge network (a Worker)
removes the self-hosted machine from that path entirely - the address
ForwardEmail's webhook calls is never down in the way a home server can be.

The Worker hands the payload to the self-hosted backend in the background
(it does not wait on the backend's response) and always accepts the message
at the SMTP layer while in shadow mode. Once shadow mode ends, the accept
path will need to become synchronous for the small set of messages the
system disposes deliberately, while a fallback default (never a bounce
caused merely by backend downtime) covers the case where the backend
cannot be reached in time.

## Pipeline

1. ForwardEmail POSTs the parsed message to `mercury.rpgm.tools` (the
   Worker).
2. The Worker forwards the payload to the backend and returns immediately.
3. The backend redacts any of the recipient's own addresses appearing in
   the message (see below) before the content is used in any further step.
4. The redacted content is scored by a local prompt-injection classifier.
   A message classified as an injection attempt is treated as untrusted
   data for the rest of the pipeline - its content is never treated as
   instructions.
5. The redacted content, the injection score, and the current rules
   ledger are given to a model for a semantic verdict (SPAM / PHISH /
   LEGIT / UNSURE) with reasoning.
6. A shadow report (verdict + reasoning) is sent by Telegram. The message
   is delivered normally regardless of the verdict.

## Redaction

Before any content leaves the backend process for an external model call,
addresses belonging to the mailbox owner are masked to `first three
characters + "*"`, domain intact (e.g. `someone@example.com` becomes
`som*@example.com`). Which addresses count as "the mailbox owner" is
configured at deploy time (`backend/identities.json`, gitignored - see
`identities.json.example` for the format), not committed to source. This
applies uniformly to every downstream consumer, regardless of which
provider is being called for a given step. It does not apply to the
message as actually delivered or quarantined - only to the copy used for
classification.

## Rules ledger

A standing set of natural-language handling rules, supplied over time
(for example: "messages from this TLD should soft-bounce except when sent
to an rpgm.tools address"). The ledger is given to the semantic-verdict
step alongside each new message rather than compiled into fixed logic, so
a new rule takes effect on the next message without a code change.
