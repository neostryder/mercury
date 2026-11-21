"""Regression tests for the 2026-11-20 digest fixes.

Three defects, all measured against the live guard before the fix:

  1. The guard could not see an UNQUALIFIED EVALUATIVE ("Quiet day.").  The
     cause was structural, not lexical: _claim_violations() returned early
     when the sentence named no counter noun, so no matcher was ever
     consulted.  Adding vocabulary to a matcher alone would NOT have caught
     the shipped sentence.
  2. The judge brief restricted the OUTPUT but never the TOOL SURFACE, so
     the judge set its own tool budget (29-255 tool calls on the four
     overruns against at most 1 on the compliant population).
  3. A judge that exceeded its budget shipped a placeholder with nothing
     logged, so a compliant-but-undelivered paragraph looked like a clean
     run.

Every negative control here is a case that ALREADY SHIPPED and must keep
shipping.  A guard that over-fires is as broken as one that under-fires.
"""
import asyncio
import logging
import unittest
from unittest.mock import patch

import digest


class DigestUnqualifiedEvaluativeTests(unittest.TestCase):
    """The evaluative form the live guard could not see."""

    def test_an_unqualified_evaluative_is_a_violation(self):
        # The exact sentence that shipped on 2026-11-19.
        self.assertTrue(digest._claim_violations("Quiet day."))

    def test_the_evaluative_is_caught_with_a_counter_noun_too(self):
        # The lexical gap is SEPARATE from the structural one: even once a
        # counter noun is present the evaluative was missed.
        self.assertTrue(
            digest._claim_violations("A quiet window overall, with 41 messages processed.")
        )
        self.assertTrue(
            digest._claim_violations("A slow day, with 41 messages processed.")
        )

    def test_a_negated_evaluative_is_not_a_violation(self):
        for sentence in (
            "Nothing unusual stands out in this window.",
            "The window was not quiet.",
            "Nothing unusual, with 41 messages processed.",
        ):
            with self.subTest(sentence=sentence):
                self.assertEqual(digest._claim_violations(sentence), [])

    def test_honest_prose_that_survived_before_still_survives(self):
        """Shipped controls. These must not regress."""
        for sentence in (
            "Two senders accounted for the new rules; the rest of the window was uneventful.",
            "Most tools in the pipeline are uneventful, which is worth noting.",
            "The weather was nice and the coffee dropped on the floor.",
            "The tool will explain the result and track each change.",
            "Nothing unusual stands out.",
            "One sender and zero urgent alerts made the window uneventful.",
            "At least 10 rules applied during this window, and the day was uneventful.",
        ):
            with self.subTest(sentence=sentence):
                self.assertEqual(digest._claim_violations(sentence), [], sentence)

    def test_the_mix_is_unremarkable_still_survives(self):
        # A shipped control that uses an evaluative ABOUT THE MIX, which is
        # a different claim from an evaluative ABOUT THE WINDOW.
        sentence = "The mix is unremarkable otherwise."
        self.assertEqual(digest._claim_violations(sentence), [])

    def test_the_evaluative_is_dropped_from_the_paragraph(self):
        stats = {
            "summary": {"last24h": {"total": 18, "hardBounces": 7, "urgent": 0, "actions": 10},
                         "last7d": {"ruleChanges": 0}},
            "recent_messages": [], "hard_bounces": [], "rule_count": 59,
            "verdict_counts": {"LEGIT": 11, "SPAM": 7},
            "category_counts": {"OTHER": 18}, "action_required": [],
        }

        class _Judge:
            async def ask(self, prompt):
                return "Quiet day. Two senders accounted for the new rules."

        out = asyncio.run(digest.build_insights(stats, _Judge()))
        self.assertNotIn("Quiet day", out)
        self.assertIn("Two senders accounted for the new rules.", out)


class DigestJudgeToolBudgetTests(unittest.TestCase):
    """The brief restricted the output but not the tool surface."""

    def _prompt(self) -> str:
        captured: dict[str, str] = {}
        stats = {
            "summary": {"last24h": {"total": 31, "hardBounces": 15, "urgent": 0, "actions": 17},
                         "last7d": {"ruleChanges": 0}},
            "recent_messages": [], "hard_bounces": [], "rule_count": 59,
            "verdict_counts": {"LEGIT": 15, "SPAM": 13},
            "category_counts": {"OTHER": 31}, "action_required": [],
        }

        class _Judge:
            async def ask(self, prompt):
                captured["prompt"] = prompt
                return "A paragraph."

        asyncio.run(digest.build_insights(stats, _Judge()))
        return captured["prompt"]

    def test_the_brief_forbids_tool_use(self):
        lowered = self._prompt().lower()
        self.assertIn("you have no tools", lowered)
        self.assertIn("do not read files", lowered)
        self.assertIn("do not run commands", lowered)

    def test_the_brief_still_forbids_the_original_claims(self):
        lowered = self._prompt().lower()
        self.assertIn("do not assert a trend", lowered)
        self.assertIn("do not infer a cause", lowered)
        self.assertIn("do not issue maintenance advice", lowered)

    def test_the_brief_still_supplies_the_statistics(self):
        self.assertIn("Messages in the last 24 hours: 31", self._prompt())


class DigestFallbackVisibilityTests(unittest.TestCase):
    """A placeholder must not ship silently."""

    def test_an_overrun_logs_a_warning(self):
        stats = {
            "summary": {"last24h": {"total": 18, "hardBounces": 7, "urgent": 0, "actions": 10},
                         "last7d": {"ruleChanges": 0}},
            "recent_messages": [], "hard_bounces": [], "rule_count": 59,
            "verdict_counts": {"LEGIT": 11, "SPAM": 7},
            "category_counts": {"OTHER": 18}, "action_required": [],
        }

        class _Judge:
            async def ask(self, prompt):
                raise TimeoutError("the judge did not answer within its budget")

        with self.assertLogs("digest", level="WARNING") as captured:
            out = asyncio.run(digest.build_insights(stats, _Judge()))
        self.assertIn("Insights unavailable this run", out)
        self.assertTrue(
            any("budget" in line for line in captured.output),
            captured.output,
        )


if __name__ == "__main__":
    unittest.main()
