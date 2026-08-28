# Changelog

## Unreleased

- Initial scaffold: Cloudflare Worker gate, FastAPI backend, prompt-injection
  screening via a local classifier, redaction of the mailbox owner's own
  addresses, semantic verdict via model call, Telegram shadow reports.
- Shadow mode only - no message is ever blocked or bounced yet.
- Alert on pipeline failure instead of failing silently.
- Semantic verdict now goes through a configurable agent gateway instead of
  a bare model call, and includes a recommended disposition (250/421/550)
  alongside the verdict - not yet enforced.
- First rules-ledger entry: lewd/dating-site content recommends a hard
  bounce.
- Live mail now flows through the pipeline in shadow mode, not just a test
  alias.
- Refactored the backend around three explicit provider seams
  (`backend/providers/`: classifier, judge, notifier), each a small
  interface with one built-in implementation, so any of the three can be
  swapped independently. Renamed the environment variables accordingly and
  removed every hardcoded hostname/IP default.
- Genericized the agent gateway (`gateway/agent_gateway.py`, was
  `loremaster-gateway.py`): it now shells out to whatever command
  `AGENT_CHAT_COMMAND` names, instead of a hardcoded CLI path.
- Fixed the Worker gate always forwarding to a hardcoded `/ingest` path and
  discarding the backend's real response - it now forwards each request's
  own path and, outside the mail-webhook path, proxies synchronously and
  returns the backend's actual response.
- Added `/rules/propose` and a Thunderbird extension (`thunderbird/`): flag
  the open message, describe in plain language how it and similar messages
  should be handled, and the instruction is interpreted into a rules-ledger
  entry via the judge provider and appended immediately (reported via the
  notifier, not yet gated behind a confirmation step).
- `worker/wrangler.toml` is now gitignored (a `.example` template is
  committed instead) so a real deployment's hostnames are never committed.
