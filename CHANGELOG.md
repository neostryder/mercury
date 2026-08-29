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
- Fixed "NetworkError when attempting to fetch resource" on send: the
  popup's cross-origin POST to the configured Mercury URL was being
  blocked by CORS, since Firefox/Thunderbird extension pages need an
  explicit host permission to bypass it. Added `host_permissions:
  ["*://*/*"]` - can't be scoped narrower since the Mercury URL is
  user-configured rather than fixed at build time.
- Fixed the popup always reporting "No message is currently displayed"
  even with a message open. Two mistakes at once, both confirmed against
  Thunderbird's own official messageDisplay example: `getDisplayedMessages`
  needs an explicit tab id (querying with no tabId does not reliably find
  the displayed message from inside this popup, contrary to my previous
  fix's assumption), and it resolves to a `MessageList` object
  (`{messages: [...], ...}`), not a bare array - the code was checking
  `.length` on the wrapper object, which is always `undefined`.
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
- Added a `message_list` right-click context menu entry ("Flag for
  Mercury") to the Thunderbird extension, alongside the existing toolbar
  button - it opens the same popup via `messageDisplayAction.openPopup()`,
  so flagging no longer requires opening the message first.
- A thumbs-up reaction on a proposal message in Telegram now approves it,
  same as replying "yes".
- Added an unsubscribe action, alongside the existing mailbox action, for
  instructions that ask to be unsubscribed from a sender. Its safety is
  evaluated first (a link with no clear relationship to the sender's own
  domain, or a page asking for credentials, is treated as unsafe and never
  visited); if safe, tracking query parameters are stripped and the
  unsubscribe is completed, then the sending domain is set to soft-bounce;
  if unsafe, unsubscribing is skipped and the domain is set to hard-bounce
  instead. See "Executing an approved unsubscribe" in
  `docs/ARCHITECTURE.md`.
- **Enforcement is now live.** The backend's `/ingest` response status is
  the judge's actual disposition (250/421/550) rather than always 200, and
  the Worker gate now waits for it and returns it to the mail host - but
  fails open (accept) on anything short of a clean, recognized disposition
  from the backend (unreachable, slow past a fixed timeout, or an
  unrecognized status), so infrastructure trouble can never itself cause a
  bounce. `MERCURY_SHADOW_MODE=true` reverts to report-only without a code
  change or redeploy.
- Fixed the "Flag for Mercury" context-menu entry never appearing: it was
  only registered inside `runtime.onInstalled`, which Thunderbird's own
  official `quickfilter` example warns against for exactly this reason -
  `menus.create()` now runs unconditionally at background-script top level
  (guarded against the resulting "already exists" rejection on a normal
  restart), matching the pattern Thunderbird's own examples use.
- Replaced thumbs-up-reaction approval with inline keyboard buttons
  (Approve/Discard) on each proposal message. Telegram's Bot API only
  delivers `message_reaction` updates when the bot is an administrator in
  the chat - a role that cannot exist in a private one-on-one chat, so a
  reaction there was silently never received. Plain "yes"/"no" text replies
  still work as before; anything else is still read as revision feedback.
- Unsubscribe no longer adds a bounce rule automatically. It reports its own
  outcome back to Telegram first (`UNSUBSCRIBED` / `FAILED` /
  `SKIPPED_UNSAFE`, plus a summary), then separately asks - as its own
  inline-button question - whether to add the sending domain to the
  blacklist (hard bounce), the greylist (soft bounce), or leave it alone. An
  unsubscribe request is not itself a request for a standing rule.
- The verdict step now also assigns a category (from a fixed label set:
  newsletter, promotional, transactional, account security, personal,
  social, financial, phishing, scam, malware, other) and its own alert
  level (none / standard / urgent) - a judgment call on whether the
  recipient should be pinged in Telegram right now versus having it show up
  in the daily summary and dashboard instead. Only standard and urgent
  alerts reach Telegram individually now; routine traffic, including most
  hard bounces, no longer pings on every message.
- Added a structured event log backing a forthcoming dashboard and daily
  summary: every verdict, rule change, and mailbox/unsubscribe action is
  now recorded to a Cloudflare D1 database (`worker/schema.sql`), written
  through a new authenticated `/log` route on the Worker gate rather than
  giving the backend its own Cloudflare credentials
  (`backend/event_log.py`). A hard-bounce recommendation also saves the
  full message and reasoning, so it can be reviewed later without having
  had to catch it live. Logging is fire-and-forget and never affects
  delivery if it fails.
- Approving an action (mailbox or unsubscribe) now sends an immediate
  "working on it" message before the agent call, which can take a while -
  the button-tap toast alone was too easy to miss, leaving it unclear
  whether a tap or reply had actually registered. The agent is also now
  asked to post its own brief progress updates as it works (e.g. "Examining
  the unsubscribe link...") rather than going quiet until the final report.
- Removed the parenthetical disposition explanation from the
  Blacklist/Greylist/No bounce button labels themselves (it stays in the
  question text above them).
- The rule-proposal interpreter can now be told an instruction arrived via
  speech-to-text dictation (a `via_dictation` flag, plumbed through from
  `/rules/propose` but not yet set by any real input method) - it's asked to
  read past likely transcription errors and infer intent rather than take
  garbled phrasing literally, still deferring to the normal revise-feedback
  loop when genuinely unclear.
- Gave the extension a real icon (a winged-helmet shield mark) instead of
  Thunderbird's default puzzle-piece placeholder - used in `icons/`, the
  toolbar button, and the context-menu entry. The source image had a
  checkerboard baked into its pixels as fake transparency rather than a real
  alpha channel; rebuilt with a genuine one by flood-filling only the
  background region connected to the image border (so similarly-toned
  pixels inside the artwork itself are left alone), then cropped and padded
  to a square at the sizes Thunderbird actually uses (16/32/48/64/96/128).
- Added the categorized taxonomy's two missing common cases -
  shipping/delivery notices and political/fundraising asks - as their own
  labels rather than folding into transactional/promotional.
- Added a first dashboard at `/dashboard` on the Worker's own domain: 24h/7d
  summary cards, a category breakdown, a filterable (accepted/deferred/
  bounced) recent-activity table with color-coded rows, and recent rule
  changes and actions - all reading live from the D1 event log
  (`worker/src/dashboard.js`, new `/dashboard/api/*` routes in
  `worker/src/index.js`). Gated by HTTP Basic Auth against a
  `DASHBOARD_PASSWORD` Worker secret - the browser prompts once and
  remembers it; Cloudflare Access (restricting by the actual owner email)
  is a stronger option to layer on later if wanted, but needs Zero Trust
  account configuration this Worker can't set up on its own.
- Added trend charts (message volume by disposition, category volume) over
  the last 30 days to the dashboard, rendered as inline SVG built from D1
  query results on the Worker itself - no client-side charting library or
  canvas (`renderStackedBarSVG` in `worker/src/dashboard.js`).
- Added a hard-bounce detail view: an aggregate list of every hard-bounced
  message, expandable per row to the full saved message text and the
  judge's full reasoning, plus - when a specific standing rule decided the
  disposition - that rule's text and a control to reverse it. The judge now
  reports which rule (if any) applied, verbatim, alongside its verdict
  (`RULE_MATCH` in `backend/app.py`'s prompt), stored in a new
  `messages.triggered_rule` column. Reversal calls a new backend endpoint,
  `POST /rules/reverse`, authenticated the same way the backend's own calls
  into the Worker's `/log` route are (`X-Mercury-Secret` against the shared
  secret both sides hold) - the rules ledger lives only on the backend's
  filesystem, not in D1, so the Worker cannot remove a rule from it
  directly.
- Added an action-item checklist to the dashboard: open `action_items` rows
  with a checkbox that marks one complete
  (`POST /dashboard/api/action-items/{id}/complete`, setting the existing
  `completed_at` column rather than adding a redundant boolean one).
- Both the rule reversal and action-item completion are recorded to the
  `admin_log` table.
- Fixed the dashboard loading over plain HTTP with a "Not Secure" warning:
  the Worker now redirects any `http://` request to `https://` itself
  (301) rather than relying on a zone-level setting, and dashboard
  responses send `Strict-Transport-Security` so the browser remembers to
  use HTTPS for this domain going forward.
- Fixed real mail never reaching the pipeline at all: the ForwardEmail
  catch-all and test aliases were configured against `/webhook` and
  `/webhook-test`, but the Worker and backend only ever implemented
  `/ingest` - every real incoming message had been 404ing since the alias
  was created. Confirmed by checking the backend's own request log rather
  than assuming the configured URL matched the code. Fixed by treating
  `/webhook` and `/webhook-test` identically to `/ingest`.
- Discovered (via the ForwardEmail API, checking the actual alias
  configuration rather than assuming) that the catch-all alias - and five
  other aliases - deliver directly to `aaron@rpgm.tools`'s real mailbox in
  parallel with, or instead of, the Mercury webhook. Since ForwardEmail
  delivers to multiple recipients on one alias independently with no
  cross-talk, this meant Mercury's disposition had never actually been
  able to block anything from reaching the inbox.
- Added the other half of enforcement: an accepted (250) message is now
  delivered into the real mailbox by Mercury itself via IMAP APPEND
  (`backend/mail_delivery.py`), tagged with `X-Mercury-Verdict`,
  `X-Mercury-Category`, and `X-Mercury-Disposition` headers, using the
  original raw message ForwardEmail's webhook payload includes rather than
  a reconstruction. Soft-deferred and hard-bounced messages are simply
  never appended - that's what makes enforcement actually binding, instead
  of advisory alongside an independent parallel delivery. Gated by
  `MERCURY_DELIVER_ACCEPTED_MAIL` (default off) since flipping it on must
  happen together with removing the mailbox's own address from each
  affected alias's recipient list, never one without the other.
- Validated the IMAP delivery path end to end (a test message landed in
  the real mailbox tagged with the right headers), then cut over all six
  affected ForwardEmail aliases (the catch-all, `dallenb4`, the
  `aaron*/strider*/aragorn*/neostryder*` regex alias, `verification`,
  `/^dallen(.*)$/`, and `/^finance(.*)$/`) to route through Mercury instead
  of delivering to `aaron@rpgm.tools` directly, preserving any other
  co-recipient on the same alias (e.g. `dallenb4@rpgm.tools` stays on the
  `/^dallen(.*)$/` alias, unfiltered, exactly as before). Enforcement is
  now actually binding for the first time - every path that reaches the
  real inbox goes through Mercury's classifier and rules ledger first.
- Fixed the backend Dockerfile's explicit per-file COPY list (the same
  pattern that caused a real outage earlier this session) by copying every
  `.py` file in the directory instead, so a future new module can't be
  left out of the image by omission again.
- The Thunderbird extension now applies a color-coded native tag to every
  new message, one per judge category, based on its `X-Mercury-Category`
  header. `messenger.messages.tags.create()` runs once per category at
  background-script startup, checked against `tags.list()` first so it
  stays idempotent across restarts; `onNewMailReceived` then reads each new
  message's full headers, matches the category to its tag, and applies it
  via `messenger.messages.update()`, merging with any tags already on the
  message rather than replacing them. Added the `accountsRead`,
  `messagesUpdate`, `messagesTags`, and `messagesTagsList` permissions this
  requires. Bumped to 0.3.3.
- Added a daily digest email (`backend/digest.py`): once a day at 7am
  America/Phoenix time (a fixed UTC-7 offset - Arizona does not observe
  DST), the backend gathers the last 24 hours of activity from the Worker's
  existing `/dashboard/api/*` routes over authenticated HTTPS, and sends a
  standalone HTML email (inline CSS only) to `aaron@rpgm.tools` covering
  message volume, verdict and category breakdowns, a ledger of rule
  changes/actions/hard bounces, the current standard/urgent alert list, and
  a short insights paragraph from the judge provider. Sent from
  `gandalf@rpgm.tools` via ForwardEmail's SMTP. Runs as a plain `asyncio`
  background task inside the existing FastAPI service - no new dependency
  and no separate cron container. Gated by `MERCURY_DASHBOARD_USER`/
  `MERCURY_DASHBOARD_PASSWORD` (the same Basic Auth the browser dashboard
  already uses) and `MERCURY_DIGEST_SMTP_USER`/`MERCURY_DIGEST_SMTP_PASSWORD`;
  any missing, and it logs a skip reason at startup instead of running.
- Fixed the context menu entry and icon vanishing after the category-chip
  update: `ensureCategoryTagsExist()` ran unguarded at background-script
  top level, after the context menu and its click listener were already
  registered - an uncaught rejection there could take the whole background
  page down with it. Wrapped it and the per-message tagging call in their
  own try/catch, logging instead of throwing, so a tagging failure can't
  affect the menu, popup, or icon. Bumped to 0.3.4.
- Fixed the release workflow's `.xpi` never containing `background.js` or
  `icons/` - only `manifest.json`, the popup, and the options page were
  ever zipped, even though the manifest declares both as required
  resources. Every installed release built by this pipeline has been
  missing its own background script and icons since the workflow was
  created; a temporary/unpacked load reads the whole folder directly and
  was never affected, which is why this went unnoticed.
- Fixed a silent failure in the Telegram approval loop's final report:
  `_send()` swallowed every exception with no logging, and never truncated
  its outgoing text - a mailbox action's own report of what it did (e.g.
  every message it deleted) can easily exceed Telegram's ~4096 character
  limit, and Telegram rejects an oversized message outright. Now the text
  is truncated before sending, a send failure is logged to `admin_log`
  instead of disappearing, and a short fallback notice ("check the
  dashboard's Recent Actions") is sent so the recipient is never left in
  total silence about whether an approved action actually ran.
- Fixed the dashboard's trend charts bucketing messages by UTC calendar
  date instead of Phoenix time: a message received in the evening (Phoenix
  is a fixed UTC-7 offset) already falls on the next UTC day, so it showed
  up under tomorrow's date. Both the D1 query's `date(received_at)` and the
  chart's day-axis generation now shift by the same fixed 7 hours before
  taking the calendar date.
- Fixed flagged-message requests always getting framed as a rule proposal,
  even a purely one-time mailbox action with no standing preference
  intended. `interpret_instruction()`'s prompt treated rule extraction as
  the primary task and action detection as an afterthought; it now asks
  whether the instruction expresses a standing preference and/or an action
  on existing mail as two independent, equally optional questions. The
  Telegram proposal message now reads "rule proposed", "action proposed",
  or "rule + action proposed" depending on what's actually there, instead
  of always showing a rule line (previously "Rule: None" when there wasn't
  one).

