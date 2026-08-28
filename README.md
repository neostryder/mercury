# Mercury

Semantic email triage for `rpgm.tools`, built on top of ForwardEmail.

Mercury sits between ForwardEmail's incoming-mail webhook and a mailbox. It
screens each message for prompt-injection attempts, checks it against a
growing set of natural-language handling rules, and produces a semantic
spam/phishing/legitimacy verdict with reasoning. During the trial period it
never blocks delivery - every message still arrives normally, and a shadow
report (verdict + reasoning) is sent by Telegram for review.

## Architecture

Two components, split so that gate uptime does not depend on any
self-hosted machine:

- **`worker/`** - a Cloudflare Worker bound to `mercury.rpgm.tools`. This is
  the address ForwardEmail's webhook actually calls. It runs on Cloudflare's
  edge network rather than any self-hosted box, so a self-hosted outage
  cannot turn into a bounce of legitimate mail. It accepts the webhook
  payload and hands it off to the backend in the background.
- **`backend/`** - a FastAPI service deployed on a self-hosted machine. It
  redacts personal addresses, calls a local prompt-injection classifier,
  applies the standing rules ledger, gets a semantic verdict from a model,
  and sends the shadow report.

See [`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md) for the full design and
the reasoning behind the two-component split.

## Status

Shadow mode: every message is reported, none are ever blocked or bounced.
