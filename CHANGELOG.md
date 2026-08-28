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
- The Thunderbird extension now has a real release process: a
  `thunderbird-vX.Y.Z` tag triggers `.github/workflows/release-thunderbird.yml`,
  which builds the `.xpi`, attaches it to a GitHub Release, and points
  `thunderbird/updates.json` at it. The extension's `manifest.json` now
  declares `browser_specific_settings.gecko.update_url` pointing at that
  file, so an installed (non-temporary) copy checks for and applies new
  versions on its own - no more manual reinstall per release, as long as
  unsigned installs are still allowed for the profile. Bumped to 0.2.0.
- Flagging now supports selecting multiple messages at once (Thunderbird's
  `getDisplayedMessages` already returned an array - the popup only used
  to send the first one). All selected messages go into the same
  proposal, labeled `Message N of M`, capped at 10 messages / 2000 chars
  each. `/rules/propose`'s payload shape changed from a single `message`
  object to a `messages` array; no backward-compat kept since both ends
  ship together.
- Fixed the popup failing with "getDisplayedMessage is not a function":
  the real Thunderbird API is `messageDisplay.getDisplayedMessages`
  (plural, returns an array) - there is no singular `getDisplayedMessage`.
  My earlier fix for the dead submit button had guessed the wrong method
  name.
- `gateway/README.md` now documents running the gateway under a real
  process supervisor (systemd/launchd/pm2/etc.) instead of a bare
  `nohup ... &`, and fetching `AGENT_GATEWAY_SECRET` at process start
  rather than embedding it in a supervisor unit file. The reference
  deployment now does this via a `launchd` job on macOS.
- Documented that a VM-based Docker runtime (Colima, Lima, etc.) needs its
  own periodic health check independent of the container's own restart
  policy, since `restart: unless-stopped` doesn't help if the VM
  underneath it wedges. The reference deployment now runs a watchdog that
  checks Docker responsiveness every 5 minutes and escalates through
  restart strategies if not, also verifying the Mercury container itself
  comes back up.
