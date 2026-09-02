# Mercury Rule Flagger (Thunderbird extension)

Lets you flag the message you're currently viewing and describe, in plain
language, how it - and similar messages - should be handled going forward.
The instruction and the flagged message are sent to your Mercury backend's
`/rules/propose` endpoint, which asks your configured judge provider to turn
the instruction into a single rule and appends it to the rules ledger.

Nothing is committed on send. The backend interprets the instruction into a
standalone rule - and, if the instruction also asked for something to be
done to mail that already exists (delete, move, and so on), a separately
scoped action - and sends both to you over Telegram for approval. Reply
"yes" to commit the rule and carry out any action, "no" to discard the
whole proposal, or anything else and it's treated as feedback: the
proposal gets revised and sent again. See the "Rules ledger" and "Approval
loop" sections of the main [README](../README.md) for how that works.

## Installing

Thunderbird only auto-updates and persists across restarts for an
installed (non-temporary) add-on, which normally means a signed XPI. This
extension isn't signed, so persisting it means allowing unsigned installs
for your profile:

1. In Thunderbird, open **Settings**, search for "config editor" in the
   search box, and open it (accept the warning).
2. Search for `xpinstall.signatures.required` and set it to `false`.
3. Download the latest `.xpi` from
   [GitHub Releases](https://github.com/neostryder/mercury/releases)
   (`mercury-rule-flagger.xpi`).
4. Add-ons Manager -> gear icon -> **Install Add-on From File** -> select
   the downloaded `.xpi`.
5. Open the extension's options (from the Add-ons Manager) and set:
   - **Mercury URL** - your Worker gate's public hostname (the same one
     ForwardEmail's webhook calls), e.g. `https://mercury.example.com`.
   - **Shared secret** - the same value as your backend's
     `MERCURY_SHARED_SECRET`.

From then on it checks `thunderbird/updates.json` in this repo for new
versions on Thunderbird's own schedule (or via Add-ons Manager -> gear
icon -> **Check for Updates**) and updates itself - no more manual
reinstalls, as long as `xpinstall.signatures.required` stays `false`.

For development instead of a real install, **Debug Add-ons -> Load
Temporary Add-on** and select this directory's `manifest.json` works as
before, but a temporary add-on doesn't persist or auto-update.

Releasing a new version (for anyone maintaining a fork): bump the
`version` in `manifest.json`, then push a `thunderbird-vX.Y.Z` tag
matching it - `.github/workflows/release-thunderbird.yml` builds the
`.xpi`, attaches it to a GitHub Release, and points `updates.json` at it.

## Using it

Either open a message (or select several in a list view) and click the
**Flag for Mercury** button in the message display toolbar, or right-click
a message (or a multi-selection) in the list and choose **Flag for
Mercury** from the context menu - both open the same popup. Describe how
it/they - and similar messages - should be handled, and send. Every
selected message goes into the same proposal, so "these three are all the
same phishing campaign, bounce anything like them" works as one flag
rather than three. The popup shows the proposed rule text; check Telegram
to approve it (a reply of "yes", or a thumbs-up reaction on the proposal
message, both approve it).

Four quick-action buttons above the instruction box fill in a common
starting instruction from the flagged message's own sender, which you can
edit before sending: **Unsubscribe** and **Bounce Domain** fill in a
ready-to-send instruction; **Bounce Pattern** fills in a template with a
bracketed placeholder describing the shared domain shape (a rotating spam
campaign, not one sender) for you to fill in; **Custom** clears the box for
an instruction with no starting point at all. All four still go through the
same instruction box and the same approval flow - they're a starting point,
not a separate submission path.

The **&times;** next to the flagged-message text removes it from the request
entirely, for an instruction that isn't about any particular email (a
standing policy change like a blacklist pattern). Once removed it can't be
re-attached from the same popup - close and reopen it against a message if
you need that context back. Unsubscribe and Bounce Domain need a real
sender and are disabled once the message is removed; Bounce Pattern and
Custom still work, and Bounce Pattern still shows the removed message's
domain as a starting example even though it won't be sent.

Two special requests get their own handling beyond a plain rule:

- **Deleting existing mail** ("delete similar messages already in my Spam
  folder") is carried out as a separately-scoped action once approved, not
  folded into the standing rule.
- **Unsubscribing** ("unsubscribe me if it's safe, otherwise hard bounce
  the domain") has its safety evaluated first - a malicious-looking
  unsubscribe link is never visited. See the "Executing an approved
  unsubscribe" section of [docs/ARCHITECTURE.md](../docs/ARCHITECTURE.md)
  for exactly how that evaluation works.

## Category tags

Every new message that lands with an `X-Mercury-Category` header (added by
`backend/mail_delivery.py` when it delivers an accepted message via IMAP -
see the main [README](../README.md)) gets a matching native Thunderbird tag
applied automatically, with its own color per category. The tag shows up
as a colored chip in both the message list and the message view, same as
any other Thunderbird tag. Tags already on a message are left in place -
the category tag is added alongside them, not instead of them.

The full set of category tags (`Mercury: Newsletter`, `Mercury: Phishing`,
and so on) is created once, the first time the extension's background
script runs, and left alone on every later restart if already present.

## Why it asks for access to all sites

The manifest declares `host_permissions: ["*://*/*"]` so the popup's
request to your Mercury URL isn't blocked by CORS - Firefox/Thunderbird
extension pages need an explicit host permission to bypass CORS for a
cross-origin fetch, and since your Mercury URL is something you type into
the options page rather than a fixed address baked into the extension,
there's no narrower pattern to declare it against ahead of time. It's
still used for exactly one thing: the POST to your own configured URL.

## Why both a message-display action and a context menu

The toolbar button keeps the flow to one click when a message is already
open - `messageDisplayAction` only exists at all while something is
displayed, so it has no equivalent for a bare list selection with nothing
open in the reading pane. The `message_list` context-menu item
(`background.js`) is what covers that case: flagging any number of selected
messages straight from the list without opening one first. Both end up
calling the same `messageDisplayAction.openPopup()` - there is no separate
popup for the context-menu path - but `openPopup()` has no way to tell the
popup which messages to use, so the context-menu handler stashes the actual
right-clicked selection (`info.selectedMessages`) in `storage.local` first,
and `popup.js`'s `init()` uses it instead of the (irrelevant, for this
path) displayed message the moment it starts up.
