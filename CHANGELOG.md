# Changelog

## Unreleased

- Initial scaffold: Cloudflare Worker gate, FastAPI backend, prompt-injection
  screening via a local classifier, redaction of the mailbox owner's own
  addresses, semantic verdict via model call, Telegram shadow reports.
- Shadow mode only - no message is ever blocked or bounced yet.
- Alert on pipeline failure instead of failing silently.
- Semantic verdict now goes through Loremaster (via a small gateway shim on
  the host) instead of a bare model call, and includes a recommended
  disposition (250/421/550) alongside the verdict - not yet enforced.
- First rules-ledger entry: lewd/dating-site content recommends a hard
  bounce.
