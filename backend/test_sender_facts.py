import unittest

import sender_facts


class TldTests(unittest.TestCase):
    def test_foreign_country_codes(self):
        for domain in ("shopping-deals.co.jp", "nintendo.co.jp", "example.ru",
                       "mail.example.de", "x.cn"):
            self.assertIs(sender_facts.is_country_code_tld(domain), True, domain)

    def test_generic_tlds_are_not_country_codes(self):
        for domain in ("ups.com", "github.com", "example.org", "news.indiegala.com",
                       "foo.info", "bar.biz", "baz.app", "site.dev"):
            self.assertIs(sender_facts.is_country_code_tld(domain), False, domain)

    def test_country_codes_used_as_generics_are_not_treated_as_foreign(self):
        """io, ai, co, me, tv and cc are country codes by origin and ordinary
        generic domains in practice. Treating them as foreign would sweep in a
        large share of perfectly normal senders."""
        for domain in ("example.io", "tool.ai", "startup.co", "about.me",
                       "stream.tv", "link.cc"):
            self.assertIs(sender_facts.is_country_code_tld(domain), False, domain)

    def test_us_is_domestic(self):
        self.assertIs(sender_facts.is_country_code_tld("agency.us"), False)

    def test_unusable_domains_are_unknown_rather_than_false(self):
        for domain in (None, "", "localhost", "no-dot"):
            self.assertIsNone(sender_facts.is_country_code_tld(domain), repr(domain))

    def test_tld_extraction(self):
        self.assertEqual(sender_facts.sender_tld("a.b.example.CO.JP"), "jp")
        self.assertIsNone(sender_facts.sender_tld("nodot"))


class DescribeTests(unittest.TestCase):
    def test_an_undeterminable_recipient_is_unknown_not_a_miss(self):
        """564 of 570 accepted messages in the log carry no recipient class.
        Reading that as 'not addressed to the recipient' would condemn almost
        every message."""
        facts = sender_facts.describe("ups.com", None, None)
        self.assertIsNone(facts["reached_a_known_address_of_the_recipient"])
        self.assertIn("not determinable", facts["how_the_recipient_was_addressed"])

    def test_a_bcc_still_counts_as_reaching_a_known_address(self):
        for letter in ("r", "f"):
            facts = sender_facts.describe("ups.com", letter, "Bcc: x@rpgm.tools")
            self.assertIs(facts["reached_a_known_address_of_the_recipient"], True)
            self.assertIn("Bcc", facts["how_the_recipient_was_addressed"])

    def test_visible_addressing_is_described_by_class(self):
        self.assertIn("rpgm.tools address, visible",
                      sender_facts.describe("x.com", "R", "To: a@rpgm.tools")
                      ["how_the_recipient_was_addressed"])
        self.assertIn("forwards into rpgm.tools",
                      sender_facts.describe("x.com", "F", "To: a@example.com")
                      ["how_the_recipient_was_addressed"])

    def test_the_block_carries_the_tld_facts(self):
        facts = sender_facts.describe("shopping-deals.co.jp", None, None)
        self.assertEqual(facts["sender_tld"], "jp")
        self.assertIs(facts["sender_tld_is_foreign_country_code"], True)

    def test_an_unknown_sender_domain_does_not_assert_anything(self):
        facts = sender_facts.describe(None, None, None)
        self.assertIsNone(facts["sender_tld"])
        self.assertIsNone(facts["sender_tld_is_foreign_country_code"])

    def test_the_alias_names_a_sender_domain_label(self):
        for domain, letter, detail in (
            ("walmart.com", "R", "To: aaron-walmart@rpgm.tools"),
            ("em2.turbotax.intuit.com", "R", "Cc: aaron-intuit@rpgm.tools"),
            ("email3.gog.com", "r", "Bcc: strider-gog@rpgm.tools"),
            ("mail.walmartmail.com", "R", "To: aaron-walmart@rpgm.tools"),
            ("EMAIL3.GOG.COM", "r", "Bcc: STRIDER-GOG@RPGM.TOOLS"),
            ("merchant-store.com", "R", "To: aaron-merchant-store@rpgm.tools"),
        ):
            with self.subTest(domain=domain, detail=detail):
                facts = sender_facts.describe(domain, letter, detail)
                self.assertIs(facts["recipient_alias_names_sender"], True)
                self.assertIn("is a computed alias/domain match", facts["note"])

    def test_a_known_alias_that_does_not_name_a_sender_label_is_false(self):
        for domain in ("unrelated.com", "wal.mart.com"):
            with self.subTest(domain=domain):
                facts = sender_facts.describe(domain, "R", "To: aaron-walmart@rpgm.tools")
                self.assertIs(facts["recipient_alias_names_sender"], False)

    def test_an_unknown_alias_or_sender_is_none(self):
        for domain, letter, detail in (
            ("gog.com", None, None),
            ("gog.com", "F", "To: strider-gog@example.com"),
            ("gog.com", "f", "Bcc via personal-address forward (original address not visible)"),
            ("gog.com", "R", None),
            ("gog.com", "R", "unknown"),
            ("gog.com", "r", "Bcc: strider-gog@example.com"),
            ("gog.com", "R", "To: strider@rpgm.tools"),
            ("gog.com", "R", "To: strider-@rpgm.tools"),
            ("go.com", "R", "To: strider-go@rpgm.tools"),
            (None, "R", "To: strider-gog@rpgm.tools"),
            ("", "R", "To: strider-gog@rpgm.tools"),
            ("localhost", "R", "To: strider-gog@rpgm.tools"),
        ):
            with self.subTest(domain=domain, letter=letter, detail=detail):
                facts = sender_facts.describe(domain, letter, detail)
                self.assertIsNone(facts["recipient_alias_names_sender"])


if __name__ == "__main__":
    unittest.main()
