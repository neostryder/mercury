import json
import shutil
import unittest
from pathlib import Path

from filtering import (
    FilteringPolicyStore,
    MIGRATED_LEWD_RULE,
    MIGRATED_UNSOLICITED_SPAM_RULE,
    PolicyConfigError,
    empty_policy,
    sender_domain_is_authenticated,
)


class SenderAuthenticationTests(unittest.TestCase):
    def test_accepts_aligned_pass_across_multiple_result_headers(self):
        raw = (
            "Authentication-Results: first.test;\r\n"
            " spf=softfail smtp.mailfrom=mailer@news.example.com\r\n"
            "Authentication-Results: second.test;\r\n"
            ' dkim=pass reason="signed; stable" header.d=EXAMPLE.COM.\r\n'
            "From: News <news@news.example.com>\r\n\r\nBody"
        )

        self.assertTrue(
            sender_domain_is_authenticated(raw, "news.example.com")
        )

    def test_accepts_aligned_spf_mailfrom(self):
        raw = (
            "Authentication-Results: mx.test; "
            "spf=pass smtp.mailfrom=mailer@news.example.com\r\n"
            "From: News <news@news.example.com>\r\n\r\nBody"
        )

        self.assertTrue(
            sender_domain_is_authenticated(raw.encode(), "news.example.com")
        )

    def test_rejects_missing_failed_misaligned_or_ambiguous_results(self):
        cases = {
            "missing": "From: News <news@example.com>\r\n\r\nBody",
            "softfail": (
                "Authentication-Results: mx.test; "
                "spf=softfail smtp.mailfrom=news@example.com\r\n\r\nBody"
            ),
            "misaligned": (
                "Authentication-Results: mx.test; "
                "dkim=pass header.d=attacker.test\r\n\r\nBody"
            ),
            "missing identity": (
                "Authentication-Results: mx.test; dkim=pass\r\n\r\nBody"
            ),
            "ambiguous identities": (
                "Authentication-Results: mx.test; dkim=pass "
                "header.d=example.com header.d=attacker.test\r\n\r\nBody"
            ),
            "pass only in comment": (
                "Authentication-Results: mx.test; dkim=fail "
                "(dkim=pass header.d=example.com) header.d=example.com\r\n\r\nBody"
            ),
            "pass only in body": (
                "From: News <news@example.com>\r\n\r\n"
                "Authentication-Results: mx.test; dkim=pass header.d=example.com"
            ),
            "authenticated child": (
                "Authentication-Results: mx.test; "
                "dkim=pass header.d=child.example.com\r\n\r\nBody"
            ),
        }

        for label, raw in cases.items():
            with self.subTest(label=label):
                self.assertFalse(sender_domain_is_authenticated(raw, "example.com"))


class FilteringPolicyStoreTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = Path(__file__).parent / ".test-filtering-data"
        shutil.rmtree(self.temp_dir, ignore_errors=True)
        self.temp_dir.mkdir()
        self.path = self.temp_dir / "rules_ledger.json"
        self.store = FilteringPolicyStore(self.path)

    def tearDown(self):
        shutil.rmtree(self.temp_dir, ignore_errors=True)

    def test_migrates_known_flat_ledger(self):
        self.path.write_text(json.dumps({"rules": [
            "Hard bounce everything from kickstarnow.com (550)",
            "Hard bounce everything from kickstartrack.com (550)",
            "Hard bounce everything from mail.beehiiv.com (550)",
            "Always allow immail.fanatical.com and unsubscribe from the current message",
            "If the message proposes lewd activities, dating sites, or lewd images (550)",
            "Hard bounce messages that are completely unsolicited and clearly spam (550)",
            "Never block or defer legitimate messages from PayPal or GOG",
            "Archive emails from Nellis Auction instead of Inbox",
        ]}), encoding="utf-8")

        policy = self.store.load()

        self.assertEqual(
            policy["sender_lists"]["blacklist"],
            ["kickstarnow.com", "kickstartrack.com", "mail.beehiiv.com"],
        )
        self.assertEqual(policy["sender_lists"]["greylist"], [])
        self.assertEqual(policy["sender_lists"]["whitelist"], ["immail.fanatical.com"])
        self.assertEqual(
            policy["semantic_rules"]["550"],
            [MIGRATED_LEWD_RULE, MIGRATED_UNSOLICITED_SPAM_RULE],
        )
        self.assertEqual(policy["custom_actions"], [])
        self.assertEqual(policy["migration_warnings"], [])
        self.assertNotIn("rules", json.loads(self.path.read_text(encoding="utf-8")))

    def test_retains_unexpected_legacy_text_as_warning(self):
        self.path.write_text(json.dumps({"rules": ["A rule outside the known migration"]}))
        self.assertEqual(
            self.store.load()["migration_warnings"],
            ["A rule outside the known migration"],
        )

    def test_exact_address_match_overrides_domain(self):
        self.store.put_sender("blacklist", "example.com")
        self.store.put_sender("whitelist", "person@example.com")

        exact = self.store.match_sender("person@example.com")
        other = self.store.match_sender("other@example.com")

        self.assertEqual((exact.list_name, exact.disposition), ("whitelist", "250"))
        self.assertEqual((other.list_name, other.disposition), ("blacklist", "550"))

    def test_adding_selector_removes_it_from_other_lists(self):
        self.store.put_sender("blacklist", "example.com")
        result = self.store.put_sender("greylist", "EXAMPLE.COM.")
        policy = self.store.load()

        self.assertEqual(result["removed_from"], ["blacklist"])
        self.assertNotIn("example.com", policy["sender_lists"]["blacklist"])
        self.assertIn("example.com", policy["sender_lists"]["greylist"])

    def test_duplicate_selector_in_file_is_rejected(self):
        policy = empty_policy()
        policy["sender_lists"]["blacklist"] = ["example.com"]
        policy["sender_lists"]["whitelist"] = ["example.com"]
        self.path.write_text(json.dumps(policy))

        with self.assertRaises(PolicyConfigError):
            self.store.load()

    def test_semantic_rules_are_bucketed(self):
        self.assertTrue(self.store.add_semantic_rule("421", "Genuinely ambiguous content"))
        self.assertFalse(self.store.add_semantic_rule("421", "Genuinely ambiguous content"))
        self.assertEqual(
            self.store.load()["semantic_rules"]["421"], ["Genuinely ambiguous content"]
        )
        self.assertTrue(self.store.remove_semantic_rule("421", "Genuinely ambiguous content"))

    def test_moving_semantic_rule_removes_old_bucket_copy(self):
        self.store.add_semantic_rule("421", "One condition")
        self.assertTrue(self.store.add_semantic_rule("250", "One condition"))
        policy = self.store.load()
        self.assertEqual(policy["semantic_rules"]["421"], [])
        self.assertEqual(policy["semantic_rules"]["250"], ["One condition"])

    def test_custom_action_uses_most_specific_selector(self):
        self.store.put_custom_action("example.com", "File in Archive", "Archive")
        self.store.put_custom_action("person@example.com", "File in Receipts", "Receipts")

        exact = self.store.match_custom_action("person@example.com")
        other = self.store.match_custom_action("other@example.com")

        self.assertEqual(exact["native"]["folder"], "Receipts")
        self.assertEqual(other["native"]["folder"], "Archive")


if __name__ == "__main__":
    unittest.main()
