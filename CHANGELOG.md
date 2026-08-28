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
- Fixed the Thunderbird popup silently doing nothing on click: it looked up
  the active message via `tabs.query({currentWindow: true})`, which does not
  reliably resolve to the 3-pane window's displayed message from inside a
  message-display-action popup, so the lookup failed before the button's
  click handler was ever attached. It now asks
  `messageDisplay.getDisplayedMessage()` for the active tab directly, and
  `init()` reports a visible error instead of failing silently if it can't
  read the open message.
- Redesigned the Thunderbird extension's popup and options page with a
  Mercury-themed look (a winged caduceus mark, a silver/sky-blue/gold
  palette) instead of unstyled default form controls.
- `/rules/propose` no longer commits a rule immediately. It now proposes a
  rule (and, if the instruction also calls for an action on mail that
  already exists, a separately scoped action) over Telegram and waits for a
  reply: "yes" commits the rule and carries out any action via the judge
  provider's own mailbox-action skill, "no" discards it, anything else is
  treated as feedback and the proposal is revised and re-sent (capped at a
  few rounds). New `backend/approvals.py` (persisted pending proposals) and
  `backend/telegram_approvals.py` (the reply loop itself, independent of
  whichever Notifier is configured).
- `gateway/README.md` now documents running the gateway under a real
  process supervisor (systemd/launchd/pm2/etc.) instead of a bare
  `nohup ... &`, and fetching `AGENT_GATEWAY_SECRET` at process start
  rather than embedding it in a supervisor unit file. The reference
  deployment now does this via a `launchd` job on macOS.
