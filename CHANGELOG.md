# Changelog

## Unreleased

### Added

- [Visible] [Filtering] Blacklist entries can now be regex patterns, in
  addition to exact addresses and domains. A pattern is matched full-string
  against the sender domain only, is validated (compiled) when it is added,
  and is checked only after all three exact sender lists find no match, so
  an exact whitelist or greylist entry always takes precedence over a
  pattern that would otherwise also catch that domain. Covers rotating spam
  campaigns that share a domain-naming shape (a random numeric prefix plus
  a word before the TLD) without needing a separate blacklist entry per
  domain. A pattern can only bounce (550), never greylist or whitelist.

- [Visible] [Filtering] The open-ended brief judge can now propose a
  BLACKLIST_PATTERN change alongside SENDER_LIST, for a recipient describing
  a rotating spam domain shape rather than one sender. Same validation and
  precedence rules as a dashboard-entered pattern.

- [Visible] [Thunderbird] The flagging popup gained four quick-action
  buttons above the instruction box - Unsubscribe, Bounce Domain, Bounce
  Pattern, and Custom - each filling in a starting instruction from the
  flagged message's own sender that the recipient can still edit before
  sending. All four go through the same instruction box, submission, and
  Telegram approval flow as typing one by hand (v0.3.6).

- [Visible] [Thunderbird] The flagging popup's flagged-message text gained
  an (x) to remove it from the request, for an instruction that is a
  standing policy change rather than about that particular email. Unsubscribe
  and Bounce Domain are disabled once removed, since both need a real
  sender; Bounce Pattern and Custom keep working (v0.3.7).

- [Visible] [Thunderbird] Composing a new message, or forwarding one, now
  pops up a small prompt asking which username to send as (default: the
  account's own username), combined with the domain to form the From
  address. Replying instead sets the From address automatically, with no
  prompt, to whichever domain alias the original message was actually sent
  to, falling back to the same prompt when no such alias can be found in the
  original's headers. The account's own bare inbox address is never used as
  an outgoing From (v0.3.9).

- [Visible] [Dashboard] A message bounced by a blacklist pattern now names
  the pattern in its reasoning and saves it as the message's triggered
  rule, so the existing "Reverse this rule" button on a hard-bounce's
  detail view removes the pattern too, the same way it already removes a
  bad semantic rule.

### Changed

- [Visible] [Dashboard] The browser dashboard no longer authenticates with a
  shared HTTP Basic Auth password. A Cloudflare Access application on
  mercury.rpgm.tools/dashboard* now gates entry by email identity instead,
  reusing the same reusable Access policy already used elsewhere. The daily
  digest email's own automated fetch of the same routes (backend/digest.py)
  switched from that shared password to a Cloudflare Access Service Token
  (CF-Access-Client-Id/CF-Access-Client-Secret headers), authorized through a
  Service Auth policy on the same application rather than a second human
  credential. The Worker no longer holds or checks a DASHBOARD_PASSWORD
  secret at all.

- [Visible] [Filtering] A domain sender-list or custom-action entry now
  also covers its own subdomains, at any depth - whitelisting paypal.com
  now also covers a sender at billing.paypal.com. When entries in
  different lists both cover a sender's domain, the longer, more specific
  entry wins, the same way an exact address already outranks any domain
  entry. Address selectors are unaffected; they only ever match themselves.

### Fixed

- [Visible] [Thunderbird] Replying still never triggered the sending-address
  prompt or an auto-matched From, even after v0.3.10 fixed it for new
  messages and forwards. Replying from an already-open message tab converts
  that same tab into the compose editor in place instead of creating a new
  one, so tabs.onCreated - which only fires for a genuinely new tab - never
  saw it. tabs.onUpdated, watching for a tab's type changing to
  "messageCompose", now catches this case too, alongside onCreated for the
  new-tab case (v0.3.11).

- [Visible] [Thunderbird] The sending-address prompt introduced in v0.3.9 had
  four problems: it never appeared at all when replying or forwarding, since
  the compose.onComposeStateChanged event it relied on only fires on a later
  edit and never fired for a composer that was already fully populated the
  moment its tab existed; the popup window was too short for its own
  content; submitting it failed with "Could not establish connection" because
  it round-tripped the chosen address through background.js's runtime
  messaging instead of setting it directly; and it could not be dismissed
  with Escape. The trigger is now tabs.onCreated (fires once, reliably, for
  every new compose tab of any type), the popup sets the From address itself,
  the window is taller, and Escape closes it (v0.3.10).

- [Visible] [Pipeline] A repeated /ingest call for the same message - a
  retried webhook after a slow response, or two independent deliveries of
  it - was reprocessed from scratch every time: on an accepted message this
  appended a second copy into the mailbox via IMAP, and since the semantic
  judge is a live model call, a repeat was not guaranteed to reach the same
  verdict as the first call, so an already-delivered message could come
  back recorded as bounced. /ingest now keys a persisted dedup store off
  the message's own Message-Id (falling back to a content hash when one
  isn't present) and replays the first call's outcome for any repeat within
  a bounded retention window instead of re-running the classifier, judge,
  or delivery steps.

- [Visible] [Filtering] An open-ended brief instruction whitelisting or
  blacklisting several domains in one turn ("whitelist paypal.com and
  gog.com") only ever produced one sender-list entry - the SENDER_LIST
  response format had room for a single selector, so the judge silently
  dropped the rest rather than proposing all of them. It now accepts a
  comma-separated list of selectors under one disposition and turns each
  into its own separate policy entry.

- [Visible] [Telegram] A verdict report's fourth decision button always
  read "Deliver + whitelist" even when the message had already been
  delivered (250), where a tap could only ever do the whitelist half. It
  now reads "Whitelist" in that case, and a new fifth "Do nothing" button
  resolves a report that turned out not to need any standing change or
  action.

- [Visible] [Thunderbird] Right-clicking a message-list selection with
  nothing open in the reading pane and choosing "Flag for Mercury" did not
  open a usable request - the popup was looking at the displayed message,
  not the actual right-clicked selection, so it found nothing when the two
  differed. The context-menu click now hands its selection to the popup
  directly (v0.3.8).

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
- Added a caveat check to rule proposals: `interpret_instruction()` and
  `revise_instruction()` now also judge whether a proposed rule actually
  adds distinguishing criteria beyond the baseline verdict step's own
  SPAM/PHISH/LEGIT/UNSURE reasoning, or whether it just restates "obviously
  bad mail should be blocked" and would therefore never be the deciding
  factor. The rules ledger is semantic by design - a broad rule is meant to
  be supported - but the recipient now sees a direct heads-up before
  approving one that's unlikely to ever do anything, instead of finding out
  after the fact.
- Replaced the single-shot rule/action parse with an open-ended brief.
  `interpret_instruction()`/`revise_instruction()` are gone; `advance_brief()`
  decides among QUESTION/RULE/ACTION/CAVEAT each turn against the brief's
  full history, so an unclear request gets asked about over Telegram
  instead of forced into a rule. Every message Mercury sends for a brief -
  not just its first proposal - is tracked back to it, so any reply
  continues the same conversation rather than being silently dropped; a
  resolved brief stays reachable too, and a later challenge to its outcome
  gets a real answer from `discuss_resolved_brief()` rather than nothing.
  `backend/approvals.py`'s store now holds full turn history and a
  message-id-to-brief index rather than one flat proposal dict.
- Fixed per-message verdict reports (the "Mercury report" / URGENT alerts
  sent for a STANDARD or URGENT verdict) being unreplyable - they went out
  through the one-way `Notifier` interface, which never captured a message
  id, so a reply asking "why was this UNSURE" or "whitelist this sender"
  had nothing to route back to. They now go out through
  `send_trackable_report()`, opening a brief the same way a proposal does,
  so they're a normal part of the same conversation. Pipeline-failure
  alerts are unaffected - those aren't about a specific email and stay on
  the plain one-way `Notifier` path.
- [Visible] Fixed the Thunderbird flag popup showing a literal "Proposed:
  null" when a flagging instruction produced only an action (for example an
  unsubscribe-plus-hard-bounce request) with no standing rule attached. The
  status line now omits the rule line entirely when there is none, and
  reads "Proposed: <action>" instead of "Also proposed: <action>" in that
  case.
- [Visible] Fixed the daily digest email showing raw ISO timestamps (with
  microseconds and a UTC offset) in the "Needs a look" and ledger tables,
  and let a single verbose piece of judge reasoning stretch a table row
  across many lines. Timestamps now render as short Phoenix-local times,
  and the reasoning, subject, and sender columns are length-capped; full
  detail is still available on the dashboard.
- [Visible] Fixed a brief proposing an action that could never actually
  happen: asked to "un-defer" mail that had been given a 421 disposition,
  Mercury proposed a `MAILBOX` action against a "Deferred mail" folder that
  doesn't exist, because a 421 or 550 disposition rejects the message at
  SMTP time and it was never delivered anywhere - there is no folder to act
  on. `advance_brief()` (`backend/app.py`) is now grounded with the
  mailbox's real IMAP folder list (`mail_delivery.list_folders()`) and with
  the fact that a 421/550 disposition means the message was never stored,
  so it never invents a folder and knows to explain via `CAVEAT` instead of
  proposing an action that can't do anything. `CAVEAT` itself is broadened
  to cover this case (not just a rule that doesn't add anything), and a
  caveat with no rule or action attached now reaches Telegram as its own
  message instead of being silently replaced with "nothing to add or do."
  Also strengthened the brief prompt to decompose a multi-part request into
  whatever combination of a rule and an action actually satisfies it,
  rather than transcribing it close to verbatim.
- [Internal] Replaced the flat semantic rules ledger with one versioned,
  atomically written filtering policy containing deterministic blacklist,
  greylist, and whitelist sender entries; 550, 421, and 250 semantic rule
  buckets; and standing custom actions. Exact sender addresses override
  domains, and adding a selector to one deterministic list removes it from
  the other two. Legacy policy migrates in place with the known sender and
  semantic entries, omitting the superseded PayPal/GOG exception and Nellis
  Auction test.
- [Visible] Deterministic sender matches now provide the final disposition
  before content processing and skip both the injection classifier and
  semantic judge. Their message events contain an explicit skipped-list
  injection label, verdict, category, and list-match reasoning. Whitelist
  matches also skip content scanning by design. Accepted deterministic
  matches use a `SENDER_LIST` Thunderbird category tag.
- [Visible] Split the semantic judge context into labeled 550, 421, and 250
  rule blocks, retaining exact `RULE_MATCH` reversal. Recalibrated the
  general prompt toward LEGIT/250 for ordinary transactional mail and
  identifiable business newsletters, reserving 421 for concrete ambiguity
  and 550 for clearly malicious, lewd, or unsolicited spam content.
- [Visible] Added Unsubscribe, Soft-bounce, Hard-bounce, and Deliver +
  whitelist buttons to STANDARD and URGENT Telegram reports. The one-message
  action executes first; any matching sender-list entry is presented
  afterward as a separate Approve/Discard proposal. A retained raw message
  supports manual delivery after defer and is removed from approval state
  when the decision runs or the brief resolves.
- [Visible] Added standing custom actions. Native folder routing changes the
  IMAP APPEND target for accepted mail; non-native instructions run through
  the scoped mailbox-action agent after successful delivery. The migrated
  list starts empty.
- [Visible] Added authenticated dashboard management for all deterministic
  sender lists, semantic rule buckets, and standing custom actions,
  including visible warnings for any unrecognized text encountered during
  legacy migration.
- [Visible] Deterministic blacklist, greylist, and whitelist matches now
  require ForwardEmail's own `dmarc` webhook verdict to report a pass for
  the claimed `From:` domain. A missing, failed, or malformed verdict sends
  the message through the normal classifier and semantic judge instead, and
  the message event records why the unauthenticated match was skipped. A
  domain with no published DMARC policy never takes the deterministic path
  in either direction - this was deliberately changed from an initial
  approach that re-parsed the raw message's own `Authentication-Results`
  headers, since those can carry a forged assertion claiming to be from a
  server other than ForwardEmail itself (ForwardEmail only strips forgeries
  of its own identity), which would have reopened the exact spoofing gap
  this check exists to close.
- [Visible] Fixed a resolved brief being a dead end: any reply after a
  brief settled - even one asking whether an earlier request actually got
  carried out - used to get a purely conversational answer that could never
  itself change anything, no matter how explicit the recipient's follow-up
  was. A brief no longer distinguishes "still open" from "already resolved"
  for the purpose of what a reply can do - every reply is re-read against
  the full conversation, and a corrected understanding now proposes the
  actual change or action right then rather than requiring the message be
  re-flagged from scratch. A new `REPLY` field lets a turn answer a direct
  question (e.g. "no, that was never done") without having to invent a
  filtering change or action just to say something. Also removed the fixed
  round limit that used to auto-discard a brief that had gone back and
  forth for a while - an unresolved brief now stays reachable for as long
  as it takes, rather than getting silently abandoned mid-conversation.
- [Internal] Fixed `_parse_brief_response`'s field regexes matching a field
  name anywhere in the text rather than only at the start of a line, which
  meant `ACTION:` could match inside `CUSTOM_ACTION:` and silently return
  the wrong value (or the custom action's own text) whenever both fields
  were present in the same response. Field patterns are now anchored to an
  actual line start.
- [Visible] Approved unsubscribes can now pause with `NEEDS_SIGNIN` when a
  verified sender-related preferences page requires an account login.
  Mercury posts a 10-minute, single-use form link into the same Telegram
  brief so the credential is entered on Mercury's own TLS-protected domain,
  never in bot chat. Unanswered prompts end without a bounce suggestion, and
  any MFA or 2FA challenge stops with a clear manual-completion result.
- [Internal] Added a memory-only, single-use credential prompt store, public
  token-authorized backend GET/POST endpoints with 4 KB request limits, and
  matching Cloudflare Worker form routes with HSTS and no-store headers.
  The unsubscribe executor supplies the submitted value to one scoped judge
  retry, immediately removes it from pending memory, and prevents it from
  reaching event logs or persisted brief history.
- [Visible] The dashboard's messages, hard-bounces, rule-change, and actions
  tables now have pagination: a 20/50/100 page-size selector plus Prev/Next,
  instead of a single fixed-size page with no way to see older rows.
- [Visible] The event log now purges automatically. A daily Worker cron
  deletes messages, actions, rule-change, and admin-log rows older than a
  configurable window (`LOG_RETENTION_DAYS`, defaulting to 365 days), and
  completed action items past the same window. An open action item is never
  purged by age alone.
- [Visible] Added a `GANDALF` brief action for handing a flagged message to
  Gandalf/Loremaster for separate competitor, canon, research, or planning
  work. Once approved, Mercury emails the summarized instruction and flagged
  context to `gandalf@rpgm.tools` and reports whether the handoff was sent.
- [Internal] Added `backend/gandalf_relay.py`, an SMTP-over-SSL relay that
  reuses the mailbox's existing IMAP username and password. Its SMTP endpoint
  is configured by `MERCURY_MAILBOX_SMTP_HOST` (default
  `smtp.forwardemail.net`) and `MERCURY_MAILBOX_SMTP_PORT` (default `465`).
