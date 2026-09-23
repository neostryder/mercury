import unittest

import verdict_policy
from providers.structured_judge import rule_ids


RULES = {
    "550": ["The message asks the reader to give money to a political campaign.",
            "Unsolicited commercial outreach from a vendor with no prior relationship."],
    "421": ["The message concerns an invoice the recipient did not expect."],
    "250": ["The message comes from a school or university about a class the "
            "recipient teaches."],
}


def structured(verdict="LEGIT", confidence=0.9, severity=0.1, category="TRANSACTIONAL",
               rule=None, rule_confidence=0.9, **signals):
    out = {
        "verdict": {"choice": verdict, "confidence": confidence},
        "category": {"choice": category, "confidence": 0.9},
        "severity": {"score": severity, "confidence": 0.9},
        "signals": {k: 0.0 for k in ("impersonates_known_party",
                                     "requests_credentials_or_payment",
                                     "manufactured_urgency",
                                     "attempts_injection",
                                     "solicits_money")},
        "rule_ids": {rid: disp for rid, (disp, _) in rule_ids(RULES).items()},
    }
    out["signals"].update(signals)
    out["matched_rule"] = {"choice": rule or "none", "confidence": rule_confidence}
    return out


class RuleMatchingTests(unittest.TestCase):
    def test_confident_rule_match_sets_its_bucket_as_the_disposition(self):
        d = verdict_policy.decide(structured(rule="r550_0", rule_confidence=0.95), RULES)
        self.assertEqual(d["disposition"], "550")
        self.assertEqual(d["triggered_rule"], RULES["550"][0])
        self.assertEqual(d["why"], "standing rule")

    def test_rule_text_comes_from_the_policy_not_the_model(self):
        """The id is the only thing the model chooses, so the stored text is
        always the exact policy string and cannot be a paraphrase."""
        d = verdict_policy.decide(structured(rule="r250_0", rule_confidence=0.9), RULES)
        self.assertIs(d["triggered_rule"], RULES["250"][0])

    def test_low_confidence_rule_match_falls_through_to_general_judgment(self):
        d = verdict_policy.decide(
            structured(verdict="LEGIT", confidence=0.95,
                       rule="r550_0", rule_confidence=0.51), RULES)
        self.assertEqual(d["disposition"], "250")
        self.assertIsNone(d["triggered_rule"])

    def test_unknown_rule_id_does_not_route_anything(self):
        s = structured(rule="r550_99", rule_confidence=0.99)
        d = verdict_policy.decide(s, RULES)
        self.assertIsNone(d["triggered_rule"])
        self.assertEqual(d["disposition"], "250")

    def test_missing_matched_rule_answer_is_survivable(self):
        s = structured()
        del s["matched_rule"]
        self.assertEqual(verdict_policy.decide(s, RULES)["disposition"], "250")


class DispositionTests(unittest.TestCase):
    def test_confident_phish_hard_bounces(self):
        d = verdict_policy.decide(structured("PHISH", 0.97, severity=3.0), RULES)
        self.assertEqual(d["disposition"], "550")

    def test_unconfident_phish_soft_defers_rather_than_bouncing(self):
        d = verdict_policy.decide(structured("PHISH", 0.62, severity=2.0), RULES)
        self.assertEqual(d["disposition"], "421")

    def test_confident_spam_bounces_on_confidence_alone(self):
        """Unwanted and dangerous are different axes. Bulk marketing carries
        almost no threat evidence and is still spam: a live run put a real one
        at SPAM 0.98 with severity 0.8."""
        for severity in (0.4, 0.8, 2.0):
            self.assertEqual(
                verdict_policy.decide(
                    structured("SPAM", 0.95, severity=severity), RULES)["disposition"],
                "550", "severity {}".format(severity))

    def test_unconfident_spam_still_soft_defers(self):
        self.assertEqual(
            verdict_policy.decide(structured("SPAM", 0.64, severity=0.8), RULES)["disposition"],
            "421")

    def test_unsure_with_no_threat_evidence_is_accepted(self):
        """22 of 45 messages this mailbox actually accepted came back UNSURE.
        Deferring on the verdict alone would hold half the inbox."""
        d = verdict_policy.decide(structured("UNSURE", 0.27), RULES)
        self.assertEqual(d["disposition"], "250")

    def test_unconfident_verdicts_do_not_hold_mail(self):
        """Ordinary mail is frequently not confidently anything: a bank notice,
        a credit alert and a newsletter measured 0.50 to 0.60, because phishing
        imitates exactly those. Confidence is not a legitimacy score."""
        for verdict, conf in (("LEGIT", 0.60), ("LEGIT", 0.50), ("UNSURE", 0.31)):
            d = verdict_policy.decide(structured(verdict, conf, severity=0.6), RULES)
            self.assertEqual(d["disposition"], "250", "{} at {}".format(verdict, conf))

    def test_real_threat_evidence_defers_without_a_confident_verdict(self):
        """Severity 2.0 is above everything 45 real accepted messages produced;
        the highest was 1.81."""
        self.assertEqual(
            verdict_policy.decide(structured("UNSURE", 0.3, severity=1.8), RULES)["disposition"],
            "250")
        self.assertEqual(
            verdict_policy.decide(structured("UNSURE", 0.3, severity=2.2), RULES)["disposition"],
            "421")

    def test_leaning_bad_defers_even_below_the_bounce_bar(self):
        d = verdict_policy.decide(structured("PHISH", 0.55, severity=0.9), RULES)
        self.assertEqual(d["disposition"], "421")

    def test_an_unfamiliar_sender_is_not_by_itself_a_reason_to_defer(self):
        """The prompt spent four lines saying this. Here it is just the absence
        of a term for it."""
        d = verdict_policy.decide(structured("LEGIT", 0.86, severity=0.3), RULES)
        self.assertEqual(d["disposition"], "250")

    def test_a_scam_categorized_spam_bounces_below_the_general_bar(self):
        """2026-09-23 421: a health-insurance lead-gen message wearing
        HealthCare.com's branding from an unrelated domain, categorized SCAM.
        Deceptive and confident is enough; it does not need to clear the
        blanket 0.80 SPAM bar too."""
        d = verdict_policy.decide(structured("SPAM", 0.65, severity=1.0, category="SCAM"), RULES)
        self.assertEqual(d["disposition"], "550")
        self.assertEqual(d["why"], "confident deceptive spam (SCAM)")

    def test_impersonation_promotes_a_promotional_spam_to_bounce(self):
        """2026-09-23 421: a HELOC lead-gen message wearing AmeriSave's
        branding from an unrelated domain, categorized PROMOTIONAL rather than
        SCAM. The impersonation signal, not the category, is what makes it
        deceptive."""
        d = verdict_policy.decide(
            structured("SPAM", 0.65, severity=1.0, category="PROMOTIONAL",
                       impersonates_known_party=0.9),
            RULES)
        self.assertEqual(d["disposition"], "550")

    def test_a_low_confidence_scam_verdict_still_defers(self):
        """Narrowing 421, not removing it: a deceptive category or signal
        does not bypass the confidence floor, only the higher general one."""
        d = verdict_policy.decide(structured("SPAM", 0.52, severity=1.0, category="SCAM"), RULES)
        self.assertEqual(d["disposition"], "421")

    def test_a_generic_promotional_spam_without_deception_still_defers(self):
        """No scam/phishing category and no impersonation signal: this is the
        case 421 stays reserved for."""
        d = verdict_policy.decide(
            structured("SPAM", 0.65, severity=1.0, category="PROMOTIONAL"), RULES)
        self.assertEqual(d["disposition"], "421")


class AlertTests(unittest.TestCase):
    def test_an_accepted_message_never_pages_whatever_else_is_true(self):
        """Issue #49: the judge's own ALERT level paged for routine accepted
        mail. This is now structural rather than an instruction."""
        for signals in ({}, {"manufactured_urgency": 0.99},
                        {"impersonates_known_party": 0.99,
                         "requests_credentials_or_payment": 0.99}):
            d = verdict_policy.decide(
                structured("LEGIT", 0.95, severity=0.1, **signals), RULES)
            self.assertEqual(d["disposition"], "250")
            self.assertEqual(d["alert"], "NONE")

    def test_a_genuinely_ambiguous_soft_defer_pages_urgently(self):
        d = verdict_policy.decide(structured("UNSURE", 0.3, severity=2.4), RULES)
        self.assertEqual(d["disposition"], "421")
        self.assertEqual(d["alert"], "URGENT")

    def test_a_confident_soft_defer_with_no_threat_is_only_standard(self):
        """Every 421 paging URGENT rebuilds #49's over-paging somewhere new."""
        d = verdict_policy.decide(structured("SPAM", 0.70, severity=0.5), RULES)
        self.assertEqual(d["disposition"], "421")
        self.assertEqual(d["alert"], "STANDARD")

    def test_a_soft_defer_carrying_real_threat_evidence_is_urgent(self):
        d = verdict_policy.decide(structured("PHISH", 0.70, severity=2.6), RULES)
        self.assertEqual(d["disposition"], "421")
        self.assertEqual(d["alert"], "URGENT")

    def test_a_routine_hard_bounce_stays_quiet_for_the_daily_summary(self):
        d = verdict_policy.decide(structured("SPAM", 0.99, severity=2.5), RULES)
        self.assertEqual(d["disposition"], "550")
        self.assertEqual(d["alert"], "NONE")

    def test_a_phishing_bounce_is_worth_same_day_attention(self):
        d = verdict_policy.decide(structured("PHISH", 0.95, severity=3.0), RULES)
        self.assertEqual(d["alert"], "STANDARD")

    def test_credential_harvesting_behind_impersonation_is_urgent(self):
        d = verdict_policy.decide(
            structured("PHISH", 0.95, severity=3.0,
                       impersonates_known_party=0.95,
                       requests_credentials_or_payment=0.93), RULES)
        self.assertEqual(d["alert"], "URGENT")

    def test_an_injection_attempt_is_urgent_even_behind_a_bounce(self):
        d = verdict_policy.decide(
            structured("SPAM", 0.99, severity=2.5, attempts_injection=0.98), RULES)
        self.assertEqual(d["alert"], "URGENT")


class DisagreementTests(unittest.TestCase):
    def test_agreement_produces_nothing_to_log(self):
        llm = {"verdict": "LEGIT", "disposition": "250", "category": "TRANSACTIONAL",
               "alert": "NONE", "triggered_rule": None}
        d = verdict_policy.decide(structured("LEGIT", 0.95), RULES)
        self.assertIsNone(verdict_policy.disagreement(llm, d))

    def test_each_differing_field_is_named_with_both_answers(self):
        llm = {"verdict": "LEGIT", "disposition": "250", "category": "NEWSLETTER",
               "alert": "NONE", "triggered_rule": None}
        d = verdict_policy.decide(structured("PHISH", 0.97, severity=3.0,
                                             category="PHISHING"), RULES)
        diffs = verdict_policy.disagreement(llm, d)
        self.assertEqual(set(diffs), {"verdict", "disposition", "category", "alert"})
        self.assertEqual(diffs["disposition"], {"judge": "250", "structured": "550"})

    def test_empty_string_and_none_are_the_same_absent_rule(self):
        llm = {"verdict": "LEGIT", "disposition": "250", "category": "TRANSACTIONAL",
               "alert": "NONE", "triggered_rule": ""}
        d = verdict_policy.decide(structured("LEGIT", 0.95), RULES)
        self.assertIsNone(verdict_policy.disagreement(llm, d))


class DescribeTests(unittest.TestCase):
    def test_the_fallback_sentence_names_the_rule_and_the_signals(self):
        s = structured("PHISH", 0.97, severity=3.0, rule="r550_0",
                       rule_confidence=0.95, requests_credentials_or_payment=0.9)
        line = verdict_policy.describe(verdict_policy.decide(s, RULES), s)
        self.assertIn("PHISH at 97% confidence", line)
        self.assertIn("political campaign", line)
        self.assertIn("requests credentials or payment", line)
        self.assertIn("3.0 of 3", line)

    def test_it_says_so_when_no_rule_matched(self):
        s = structured("LEGIT", 0.9)
        self.assertIn("no standing rule matched",
                      verdict_policy.describe(verdict_policy.decide(s, RULES), s))


class RuleIdTests(unittest.TestCase):
    def test_ids_carry_their_bucket_and_position(self):
        ids = rule_ids(RULES)
        self.assertEqual(ids["r550_1"], ("550", RULES["550"][1]))
        self.assertEqual(ids["r421_0"], ("421", RULES["421"][0]))
        self.assertEqual(len(ids), 4)

    def test_an_empty_policy_produces_no_ids(self):
        self.assertEqual(rule_ids({"550": [], "421": [], "250": []}), {})


if __name__ == "__main__":
    unittest.main()
