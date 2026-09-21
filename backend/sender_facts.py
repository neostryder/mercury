"""Facts about a message that a string comparison answers, not a model.

Two of the recipient's standing semantic rules asked the classifier to decide
things that are not judgments at all: whether the message reached one of their
own addresses, and whether the sender's domain sits under a foreign
country-code TLD. A model asked either one is guessing at `endswith`, and it
guesses unevenly - the TLD rule matched at 0.61 to 0.79 across otherwise
identical probes.

So they are computed here and handed to the classifier as observed facts. The
model is then judging what the facts mean, which is the part it is for.

Deliberately NOT a disposition of their own. Both were 550 rules, and making
them deterministic bounces would have been the wrong kind of reliable:

  - "not addressed to one of my known addresses" is false for legitimate mail.
    A Bcc has no visible recipient by design, which is exactly why
    _classify_recipient() carries the separate `r` and `f` classes, and it
    returns None whenever the webhook payload lacks usable To/Cc data at all.
    Of 570 accepted messages in the log, 564 carry no recipient class.
  - "foreign country-code TLD" catches nintendo.co.jp answering a repair
    request as readily as it catches a spam campaign.

A bounce is the one unrecoverable disposition. These inform it; they do not
issue it.
"""

# Generic and domestic TLDs. Anything outside this set that is two letters is
# treated as a country code, which is what the standing rule describes.
_NON_CC_TLDS = frozenset({
    "com", "org", "net", "edu", "gov", "mil", "int", "info", "biz", "name",
    "pro", "aero", "coop", "museum", "jobs", "mobi", "tel", "travel", "xxx",
    "app", "dev", "io", "ai", "co", "me", "tv", "cc", "us",
})


def sender_tld(sender_domain: str | None) -> str | None:
    """The last label of the sender's domain, lowercased."""
    if not sender_domain or "." not in sender_domain:
        return None
    return sender_domain.rsplit(".", 1)[1].lower().strip()


def is_country_code_tld(sender_domain: str | None) -> bool | None:
    """True for a foreign country-code TLD, None when the domain is unusable.

    `io`, `ai`, `co`, `me`, `tv` and `cc` are country codes by origin and
    ordinary generic domains in practice, so they are not treated as foreign.
    `us` is domestic. Everything else of exactly two letters is.
    """
    tld = sender_tld(sender_domain)
    if not tld:
        return None
    if tld in _NON_CC_TLDS:
        return False
    return len(tld) == 2 and tld.isalpha()


def describe(sender_domain: str | None, recipient_class: str | None,
             recipient_detail: str | None) -> dict:
    """The fact block handed to the classifier alongside the message.

    `recipient_class` is _classify_recipient()'s letter: R and r mean an
    rpgm.tools address, F and f mean a personal address that forwards in, and
    None means the payload carried nothing usable rather than that the message
    was misaddressed.
    """
    cc = is_country_code_tld(sender_domain)
    known = None if recipient_class is None else True
    return {
        "sender_domain": sender_domain,
        "sender_tld": sender_tld(sender_domain),
        "sender_tld_is_foreign_country_code": cc,
        "reached_a_known_address_of_the_recipient": known,
        "how_the_recipient_was_addressed": {
            "R": "an rpgm.tools address, visible in To or Cc",
            "F": "a personal address that forwards into rpgm.tools, visible in To or Cc",
            "r": "Bcc straight to an rpgm.tools address",
            "f": "Bcc on a personal address before it was forwarded in",
        }.get(recipient_class or "", "not determinable from this message's headers"),
        "note": ("These are computed facts, not judgments. An undeterminable "
                 "recipient means the headers did not carry the answer, not that "
                 "the message was misaddressed."),
        "detail": recipient_detail,
    }
