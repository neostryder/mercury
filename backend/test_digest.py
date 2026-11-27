import asyncio
import unittest
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, patch

import digest


def _iso(hours_ago: float) -> str:
    return (datetime.now(timezone.utc) - timedelta(hours=hours_ago)).isoformat()


class DigestGatherTests(unittest.TestCase):
    def _fake_get(self, pages: dict[str, list[dict]]):
        calls = []

        async def fake_get(client, base_url, path):
            calls.append(path)
            route, _, query = path.partition("?")
            if route == "/dashboard/api/summary":
                return {"last24h": {"total": 3}, "last7d": {"ruleChanges": 1}}
            if route == "/dashboard/api/filtering":
                return {
                    "sender_lists": {"blacklist": ["a.example"], "whitelist": ["b.example", "c.example"]},
                    "blacklist_patterns": ["spam.*"],
                    "semantic_rules": {"550": ["scam"], "421": []},
                    "custom_actions": [{"selector": "d.example"}],
                }
            params = dict(p.split("=") for p in query.split("&"))
            offset, limit = int(params["offset"]), int(params["limit"])
            rows = pages.get(route, [])
            return {"rows": rows[offset:offset + limit], "hasMore": offset + limit < len(rows)}

        return fake_get, calls

    def test_reads_paginated_rows_and_stops_past_the_window(self):
        messages = [
            {"received_at": _iso(h), "verdict": "CLEAN", "category": "OTHER", "enforced_disposition": "250"}
            for h in (1, 2, 3, 30, 31, 32)
        ]
        fake_get, calls = self._fake_get({"/dashboard/api/messages": messages})
        with (
            patch.object(digest, "_get", fake_get),
            patch.object(digest, "PAGE_SIZE", 3),
            patch.object(digest, "WORKER_LOG_URL", "https://mercury.example.com/log"),
            patch.object(digest, "CF_ACCESS_CLIENT_ID", "test-id"),
            patch.object(digest, "CF_ACCESS_CLIENT_SECRET", "test-value"),
        ):
            stats = asyncio.run(digest.gather_stats())

        self.assertEqual(len(stats["recent_messages"]), 3)
        self.assertFalse(stats["messages_capped"])
        self.assertEqual(stats["rule_count"], 6)
        message_calls = [c for c in calls if c.startswith("/dashboard/api/messages")]
        self.assertEqual(len(message_calls), 2)

    def test_marks_the_window_capped_when_pages_run_out(self):
        messages = [
            {"received_at": _iso(0.1), "verdict": "CLEAN", "category": "OTHER", "enforced_disposition": "250"}
            for _ in range(10)
        ]
        fake_get, _ = self._fake_get({"/dashboard/api/messages": messages})
        with (
            patch.object(digest, "_get", fake_get),
            patch.object(digest, "PAGE_SIZE", 2),
            patch.object(digest, "MAX_PAGES", 3),
            patch.object(digest, "WORKER_LOG_URL", "https://mercury.example.com/log"),
            patch.object(digest, "CF_ACCESS_CLIENT_ID", "test-id"),
            patch.object(digest, "CF_ACCESS_CLIENT_SECRET", "test-value"),
        ):
            stats = asyncio.run(digest.gather_stats())

        self.assertTrue(stats["messages_capped"])
        self.assertEqual(len(stats["recent_messages"]), 6)



class AccessCredentialTests(unittest.TestCase):
    def test_strips_the_header_label_copied_with_the_value(self):
        cases = {
            "CF-Access-Client-Id: abc.access": "abc.access",
            "cf-access-client-secret:xyz": "xyz",
            "  plain-value \t": "plain-value",
        }
        for raw, want in cases.items():
            with self.subTest(raw=raw), patch.dict("os.environ", {"MERCURY_DIGEST_TEST": raw}):
                self.assertEqual(digest._access_credential("MERCURY_DIGEST_TEST"), want)

    def test_missing_stays_missing(self):
        with patch.dict("os.environ", {}, clear=False):
            self.assertIsNone(digest._access_credential("MERCURY_DIGEST_UNSET_NAME"))


class LinkFilterTests(unittest.TestCase):
    def _stats(self):
        message = {
            "received_at": None,
            "alert_level": "STANDARD",
            "from_display": "Deals <promo@spam-shop.example>",
            "subject": "Win at https://modelconventions.example/offer",
            "reasoning": "Bulk mail linking to modelconventions.example, a known lure.",
        }
        return {
            "summary": {},
            "recent_messages": [message],
            "messages_capped": False,
            "rule_count": 0,
            "verdict_counts": digest.Counter(),
            "category_counts": digest.Counter(),
            "hard_bounces": [],
            "action_required": [message],
            "ledger": [{"at": None, "kind": "Rule add", "detail": "Block *@spam-shop.example"}],
            "generated_at": datetime.now(timezone.utc),
        }

    def test_quoted_hostnames_are_defanged_in_the_rendered_digest(self):
        body = digest.render_html(self._stats(), "Mail from modelconventions.example spiked.")
        self.assertNotIn("modelconventions.example", body)
        self.assertIn("modelconventions[.]example", body)
        self.assertNotIn("spam-shop.example", body)
        self.assertIn("https://mercury.rpgm.tools/", body)

    def test_a_blocked_hostname_is_defanged_and_the_send_retried(self):
        error = digest.smtplib.SMTPDataError(
            554, b"Link hostname of lure.example was detected by Cloudflare's Family DNS"
        )
        with (
            patch.object(digest.smtplib, "SMTP_SSL") as smtp_ssl,
            patch.object(digest, "SMTP_USER", "u"),
            patch.object(digest, "SMTP_PASSWORD", "p"),
        ):
            server = smtp_ssl.return_value.__enter__.return_value
            server.send_message.side_effect = [error, None]
            digest.send_email("Digest", "<p>see lure.example</p>")

        self.assertEqual(server.send_message.call_count, 2)
        retried = server.send_message.call_args_list[1].args[0].as_string()
        self.assertNotIn("lure.example", retried)

    def test_an_unrelated_rejection_is_raised(self):
        with (
            patch.object(digest.smtplib, "SMTP_SSL") as smtp_ssl,
            patch.object(digest, "SMTP_USER", "u"),
            patch.object(digest, "SMTP_PASSWORD", "p"),
        ):
            server = smtp_ssl.return_value.__enter__.return_value
            server.send_message.side_effect = digest.smtplib.SMTPDataError(554, b"Message rejected")
            with self.assertRaises(digest.smtplib.SMTPDataError):
                digest.send_email("Digest", "<p>hello</p>")
        self.assertEqual(server.send_message.call_count, 1)


class DigestDeliveryTests(unittest.TestCase):
    def test_cresset_mode_keeps_smtp_as_the_primary_digest_delivery(self):
        async def post(path, payload):
            smtp.assert_called_once_with("Mercury daily digest - November 19, 2026", "<p>All quiet.</p>")
            self.assertEqual(path, "/v1/events")
            self.assertEqual(payload["fingerprint"], "mercury-digest-2026-11-19")
            return True

        stats = {"generated_at": datetime(2026, 11, 19, tzinfo=timezone.utc)}
        with (
            patch.object(digest, "gather_stats", AsyncMock(return_value=stats)),
            patch.object(digest, "build_insights", AsyncMock(return_value="All quiet.")),
            patch.object(digest, "render_html", return_value="<p>All quiet.</p>"),
            patch.object(digest, "send_email") as smtp,
            patch.object(digest.cresset_mirror, "delivery_mode", AsyncMock(return_value="cresset")),
            patch.object(digest.cresset_mirror, "_post", AsyncMock(side_effect=post)) as send_copy,
        ):
            asyncio.run(digest.run_digest_once(None))
        send_copy.assert_awaited_once()


class DigestInsightOutputTests(unittest.TestCase):
    """The paragraph that comes back is checked, not just the prompt."""

    def _stats(self) -> dict:
        return {
            "summary": {
                "last24h": {"total": 18, "hardBounces": 7, "urgent": 0, "actions": 10},
                "last7d": {"ruleChanges": 0},
            },
            "recent_messages": [],
            "hard_bounces": [],
            "rule_count": 59,
            "verdict_counts": {"LEGIT": 11, "SPAM": 7},
            "category_counts": {"OTHER": 18},
            "action_required": [],
        }

    def _paragraph(self, text: str) -> str:
        class _Judge:
            async def ask(self, prompt: str) -> str:
                return text

        return asyncio.run(digest.build_insights(self._stats(), _Judge()))

    def test_a_false_ranking_is_removed(self):
        out = self._paragraph(
            "Hard bounces were the largest category this window, outnumbering "
            "legitimate mail. The mix is unremarkable otherwise."
        )
        self.assertNotIn("largest", out.lower())
        self.assertNotIn("outnumbering", out.lower())
        self.assertIn("unremarkable", out)

    def test_a_false_correspondence_is_removed(self):
        out = self._paragraph(
            "The action count tracks the hard-bounce rate, which reflects how "
            "the filtering rules are performing."
        )
        self.assertNotIn("tracks", out.lower())
        self.assertNotIn("reflects", out.lower())

    def test_a_paragraph_with_only_claim_sentences_falls_back_to_the_statistics(self):
        out = self._paragraph("Hard bounces were the largest share of all messages.")
        self.assertNotIn("largest", out.lower())
        self.assertIn("18", out)
        self.assertIn("7", out)

    def test_an_honest_paragraph_survives_untouched(self):
        text = "Two senders accounted for the new rules; the rest of the window was uneventful."
        out = self._paragraph(text)
        self.assertEqual(out, text)

    def test_a_numeric_bound_is_not_treated_as_a_ranking(self):
        text = "At least 10 rules applied during this window, and the day was uneventful."
        out = self._paragraph(text)
        self.assertEqual(out, text)

    def test_prose_without_a_counter_is_not_scoped_in(self):
        text = "Most tools in the pipeline are uneventful, which is worth noting."
        out = self._paragraph(text)
        self.assertEqual(out, text)

    def test_an_inflected_correspondence_is_removed(self):
        out = self._paragraph(
            "The action count reflect the hard-bounce rate, which track how "
            "the filtering rules are performing."
        )
        self.assertNotIn("reflect", out.lower())
        self.assertNotIn("track", out.lower())

    def test_an_inflected_ranking_is_removed(self):
        out = self._paragraph(
            "Hard bounces exceed the message count, and two senders shares "
            "the new rules. The mix is unremarkable otherwise."
        )
        self.assertNotIn("exceed", out.lower())
        self.assertNotIn("shares", out.lower())
        self.assertIn("unremarkable", out)

    def test_a_correspondence_word_without_a_counter_is_not_scoped_in(self):
        text = "The tool will explain the result and track each change."
        out = self._paragraph(text)
        self.assertEqual(out, text)

    def test_an_inflection_does_not_strip_honest_prose(self):
        text = "Two senders accounted for the new rules; the rest of the window was uneventful."
        out = self._paragraph(text)
        self.assertEqual(out, text)


class DigestInsightRuleCoverageTests(unittest.TestCase):
    """Every rule in the insights prompt has a matcher: trend, cause, advice,
    and restatement as well as ranking and correspondence."""

    _stats = DigestInsightOutputTests._stats
    _paragraph = DigestInsightOutputTests._paragraph

    def test_a_trend_claim_is_removed(self):
        for sentence in (
            "Hard bounces spiked in this window.",
            "The message count dropped compared with yesterday.",
            "Spam verdicts rose in this window.",
            "Actions taken were up from the baseline.",
            "There was an uptick in sender activity.",
            "Mail volume is trending toward fewer messages.",
        ):
            out = self._paragraph(sentence + " The rest of the window was uneventful.")
            self.assertEqual(out, "The rest of the window was uneventful.", sentence)

    def test_a_cause_claim_is_removed(self):
        for sentence in (
            "The reason for the hard bounces is a stale sender list.",
            "The bounce count stems from a few dead domains.",
            "Spam messages are attributable to one sender.",
            "That is why the filter acted on ten messages.",
        ):
            out = self._paragraph(sentence + " The rest of the window was uneventful.")
            self.assertEqual(out, "The rest of the window was uneventful.", sentence)

    def test_advice_is_removed(self):
        for sentence in (
            "You should review the standing rules.",
            "It is worth reviewing the sender list.",
            "Consider pruning the rule list.",
            "I recommend a cleanup of the filters.",
        ):
            out = self._paragraph(sentence + " The rest of the window was uneventful.")
            self.assertEqual(out, "The rest of the window was uneventful.", sentence)

    def test_a_restatement_of_the_tables_is_removed(self):
        out = self._paragraph(
            "The window held 18 messages, 7 hard bounces, and 10 actions. "
            "Two senders accounted for the new rules."
        )
        self.assertEqual(out, "Two senders accounted for the new rules.")

    def test_a_sentence_with_one_figure_is_kept(self):
        text = "Every one of the 18 messages was read; nothing else stood out."
        self.assertEqual(self._paragraph(text), text)

    def test_small_numbers_are_not_a_restatement(self):
        text = "One sender and zero urgent alerts made the window uneventful."
        self.assertEqual(self._paragraph(text), text)

    def test_an_honest_nothing_stands_out_sentence_survives(self):
        text = "Nothing unusual stands out in this window."
        self.assertEqual(self._paragraph(text), text)

    def test_a_trend_word_without_a_counter_is_not_scoped_in(self):
        text = "The weather was nice and the coffee dropped on the floor."
        self.assertEqual(self._paragraph(text), text)


class DigestInsightNegationTests(unittest.TestCase):
    _stats = DigestInsightOutputTests._stats
    _paragraph = DigestInsightOutputTests._paragraph

    def test_without_explaining_keeps_the_whole_paragraph(self):
        text = (
            "The window was uneventful. Two senders accounted for the new rules. "
            "The digest reports message counts WITHOUT EXPLAINING them. "
            "The tables carry the details. Nothing unusual stands out."
        )
        self.assertEqual(self._paragraph(text), text)

    def test_negated_claims_are_kept(self):
        for sentence in (
            "The rule count does not explain the action count.",
            "These rule counts do not explain the action count.",
            "The filter doesn't explain the verdicts.",
            "The filters don't explain the verdicts.",
            "The filter did not explain the verdicts.",
            "The filter didn't explain the verdicts.",
            "No cause is implied by these message counts.",
            "Nothing here suggests a cause for these message counts.",
            "The message count was not caused by the new rule.",
            "The message count cannot be attributed to the new rule.",
            "The message count can't be attributed to the new rule.",
            "The filter never explains the verdicts.",
            "None of these counts explains the verdicts.",
            "Neither count explains the message verdicts.",
            "The digest neither tracks nor explains the message counts.",
            "There is no way to explain the message counts.",
            "The digest is unable to explain the message counts.",
            "The digest offers no advice to prune the sender list.",
            "The message count is not rising.",
            "The message counts are not rising.",
            "The message count isn't rising.",
            "The message counts aren't rising.",
            "The message count was not rising.",
            "The message count wasn't rising.",
            "The action count does not stem from the rule count.",
            "The action count is not the result of the rule count.",
            "The digest does not recommend pruning the sender list.",
            "The sender list is not worth reviewing.",
            "The digest does not consider pruning the sender list.",
            "No action should prune the sender list.",
            "These rules do not dominate the sender counts.",
            "The message count is not the largest.",
            "The digest does not correlate sender counts.",
            "The digest does not in any way explain the message counts.",
        ):
            with self.subTest(sentence=sentence):
                self.assertEqual(digest._claim_violations(sentence), [])
                self.assertEqual(self._paragraph(sentence), sentence)

    def test_asserted_claims_are_removed(self):
        for sentence in (
            "This explains the rise in message counts.",
            "The messages were caused by the new rule.",
            "You should prune the sender list.",
            "The rule count is the largest.",
            "The rules dominate the sender counts.",
            "The rule count stems from sender activity.",
            "The rule count is attributed to sender activity.",
            "The message count is rising.",
            "I recommend reviewing the sender list.",
            "No outage occurred and the recorded rule activity explains the count.",
            "No outage occurred, and this explains the message count.",
            "No outage occurred; this explains the message count.",
            "No outage occurred: this explains the message count.",
            "No outage occurred but this explains the message count.",
            "No outage occurred however this explains the message count.",
            "The digest not only explains the message count.",
            "The digest not only records mail but also explains the count.",
            "The digest never explains mail, yet explains the action count.",
            "The digest never recommends pruning, yet recommends a rule audit.",
            "The count never rose, but rose in the message window.",
            "The rule count is not the largest, but the action count is the largest.",
            "The count does not stem from mail, but stems from sender activity.",
            "The filter explains the message count without further details.",
            "Without explaining the counts, you should prune the list.",
        ):
            with self.subTest(sentence=sentence):
                self.assertTrue(digest._claim_violations(sentence))
                kept = "The rest of the window was uneventful."
                self.assertEqual(self._paragraph(sentence + " " + kept), kept)

    def test_negation_does_not_exempt_restated_figures(self):
        sentence = "The digest does not explain the 18 messages and 7 hard bounces."
        self.assertEqual(
            digest._claim_violations(sentence, frozenset({"18", "7"})),
            ["restatement of the tables"],
        )
        kept = "The rest of the window was uneventful."
        self.assertEqual(self._paragraph(sentence + " " + kept), kept)


class DigestInsightPromptTests(unittest.TestCase):
    """The insights prompt must not invite a trend, a cause, or a directive."""

    def _stats(self) -> dict:
        return {
            "summary": {
                "last24h": {"total": 31, "hardBounces": 15, "urgent": 0, "actions": 17},
                "last7d": {"ruleChanges": 0},
            },
            "recent_messages": [],
            "hard_bounces": [],
            "rule_count": 59,
            "verdict_counts": {"LEGIT": 15, "SPAM": 13},
            "category_counts": {"OTHER": 31},
            "action_required": [],
        }

    def _prompt(self) -> str:
        captured: dict[str, str] = {}

        class _Judge:
            async def ask(self, prompt: str) -> str:
                captured["prompt"] = prompt
                return "A paragraph."

        asyncio.run(digest.build_insights(self._stats(), _Judge()))
        return captured["prompt"]

    def test_prompt_forbids_trend_cause_and_directive(self):
        prompt = self._prompt()
        lowered = prompt.lower()
        self.assertIn("do not assert a trend", lowered)
        self.assertIn("do not infer a cause", lowered)
        self.assertIn("do not issue maintenance advice", lowered)
        self.assertIn("no prior day, no baseline", lowered)

    def test_prompt_does_not_ask_for_a_spike_or_drop(self):
        lowered = self._prompt().lower()
        self.assertNotIn("a spike or drop in", lowered)

    def test_prompt_still_supplies_the_statistics(self):
        prompt = self._prompt()
        self.assertIn("Messages in the last 24 hours: 31", prompt)
        self.assertIn("Hard bounces: 15", prompt)


if __name__ == "__main__":
    unittest.main()
