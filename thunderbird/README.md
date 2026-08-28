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

## Installing (development / personal use)

Thunderbird only requires a signed XPI for install via the Add-ons Manager
proper. For personal use, load it unpacked instead:

1. In Thunderbird, open **Tools -> Developer Tools -> Debug Add-ons** (or
   navigate to `about:debugging#/runtime/this-firefox` equivalent for
   Thunderbird - **Add-ons -> gear icon -> Debug Add-ons**).
2. Click **Load Temporary Add-on** and select this directory's
   `manifest.json`.
3. Open the extension's options (from the Add-ons Manager) and set:
   - **Mercury URL** - your Worker gate's public hostname (the same one
     ForwardEmail's webhook calls), e.g. `https://mercury.example.com`.
   - **Shared secret** - the same value as your backend's
     `MERCURY_SHARED_SECRET`.

A temporary add-on is removed when Thunderbird restarts; reload it from the
same screen when needed. Packaging a persistent, signed build is future
work - see the repo's CHANGELOG.

## Using it

Open a message (or select several in a list view), click the **Flag for
Mercury** button in the message display toolbar, describe how it/they -
and similar messages - should be handled, and send. Every selected message
goes into the same proposal, so "these three are all the same phishing
campaign, bounce anything like them" works as one flag rather than three.
The popup shows the proposed rule text; check Telegram to approve it.

## Why a message-display action, not a context menu

A toolbar button next to an open message keeps the flow to one click plus
as much explanation as you want to give, and reads directly off the message
already on screen rather than requiring a folder-list selection first.
