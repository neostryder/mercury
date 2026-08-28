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

Open a message (or select several in a list view), click the **Flag for
Mercury** button in the message display toolbar, describe how it/they -
and similar messages - should be handled, and send. Every selected message
goes into the same proposal, so "these three are all the same phishing
campaign, bounce anything like them" works as one flag rather than three.
The popup shows the proposed rule text; check Telegram to approve it.

## Why it asks for access to all sites

The manifest declares `host_permissions: ["*://*/*"]` so the popup's
request to your Mercury URL isn't blocked by CORS - Firefox/Thunderbird
extension pages need an explicit host permission to bypass CORS for a
cross-origin fetch, and since your Mercury URL is something you type into
the options page rather than a fixed address baked into the extension,
there's no narrower pattern to declare it against ahead of time. It's
still used for exactly one thing: the POST to your own configured URL.

## Why a message-display action, not a context menu

A toolbar button next to an open message keeps the flow to one click plus
as much explanation as you want to give, and reads directly off the message
already on screen rather than requiring a folder-list selection first.
