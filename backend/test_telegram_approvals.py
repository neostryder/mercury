import asyncio
import os
import shutil
import sys
import unittest
from pathlib import Path
from unittest.mock import AsyncMock

os.environ.setdefault("TELEGRAM_BOT_TOKEN", "test-token")
os.environ.setdefault("TELEGRAM_CHAT_ID", "test-chat")

sys.path.insert(0, str(Path(__file__).parent))

from approvals import ApprovalStore
from telegram_approvals import TelegramApprovals

NO_OP_RESULT = {"question": None, "reply": None, "changes": [], "action": None, "caveat": None}


def _reply_update(message_id: int, text: str) -> dict:
    return {
        "message": {
            "text": text,
            "reply_to_message": {"message_id": message_id},
        }
    }


class ChangeTextTests(unittest.TestCase):
    def test_blacklist_pattern_renders_its_regex(self):
        text = TelegramApprovals._change_text({
            "kind": "blacklist_pattern",
            "pattern": r"^\d{6}[a-z]+\.com$",
        })
        self.assertEqual(text, r"blacklist pattern: ^\d{6}[a-z]+\.com$")


class ResolvedBriefReopensTests(unittest.TestCase):
    """A brief reaching an outcome must never become a dead end - a later
    reply has to be able to trigger a real correction or a brand-new
    proposal, not just a conversational answer that changes nothing."""

    def setUp(self):
        self.temp_dir = Path(__file__).parent / ".test-telegram-reopen-data"
        shutil.rmtree(self.temp_dir, ignore_errors=True)
        self.temp_dir.mkdir()
        self.store = ApprovalStore(self.temp_dir / "approvals.json")
        self.finalize = AsyncMock()
        self.advance = AsyncMock(return_value=dict(NO_OP_RESULT))
        self.telegram = TelegramApprovals(
            self.store,
            advance=self.advance,
            finalize=self.finalize,
            execute_action=AsyncMock(return_value=("Unsubscribed.", None)),
            execute_message_decision=AsyncMock(),
        )
        self.telegram._send = AsyncMock(side_effect=[101, 102, 103, 104, 105, 106])
        self.telegram._answer_callback = AsyncMock()

    def tearDown(self):
        shutil.rmtree(self.temp_dir, ignore_errors=True)

    def _resolved_brief_with_no_op_outcome(self) -> str:
        brief_id, summary, action = asyncio.run(
            self.telegram.propose_new("Flag this message", "From: shop@fanatical.example")
        )
        self.assertIsNone(summary)
        self.assertIsNone(action)
        brief = self.store.get_brief(brief_id)
        self.assertEqual(brief["status"], "resolved")
        return brief_id

    def test_explicit_instruction_after_a_no_op_outcome_proposes_a_new_action(self):
        brief_id = self._resolved_brief_with_no_op_outcome()

        self.advance.return_value = {
            "question": None,
            "reply": "You're right, that was never done - fixing it now:",
            "changes": [],
            "action": "UNSUBSCRIBE: fanatical.example",
            "caveat": None,
        }
        asyncio.run(self.telegram._handle_update(_reply_update(101, "Do it")))

        brief = self.store.get_brief(brief_id)
        self.assertEqual(brief["status"], "open")
        self.assertEqual(brief["action"], "UNSUBSCRIBE: fanatical.example")
        sent_text = self.telegram._send.await_args_list[-1].args[0]
        self.assertIn("fixing it now", sent_text)
        self.assertIn("UNSUBSCRIBE: fanatical.example", sent_text)

        asyncio.run(self.telegram._handle_callback({
            "id": "callback-1",
            "data": f"approve:{brief_id}",
        }))
        self.assertEqual(self.telegram._execute_action.await_count, 1)
        self.assertEqual(self.store.get_brief(brief_id)["status"], "resolved")

    def test_reply_field_answers_a_question_without_reproposing_anything(self):
        brief_id = self._resolved_brief_with_no_op_outcome()

        self.advance.return_value = {
            "question": None,
            "reply": "No. I did not unsubscribe you.",
            "changes": [],
            "action": None,
            "caveat": None,
        }
        asyncio.run(self.telegram._handle_update(_reply_update(101, "So did you?")))

        sent_text = self.telegram._send.await_args_list[-1].args[0]
        self.assertEqual(sent_text, "No. I did not unsubscribe you.")
        brief = self.store.get_brief(brief_id)
        self.assertEqual(brief["status"], "resolved")
        self.assertEqual(brief["changes"], [])
        self.assertIsNone(brief["action"])

    def test_generic_fallback_is_not_used_when_a_reply_is_available(self):
        brief_id = self._resolved_brief_with_no_op_outcome()
        self.advance.return_value = {
            "question": None,
            "reply": "That's correct, nothing needed to change there.",
            "changes": [],
            "action": None,
            "caveat": None,
        }
        asyncio.run(self.telegram._handle_update(_reply_update(101, "So that's it?")))
        sent_text = self.telegram._send.await_args_list[-1].args[0]
        self.assertNotEqual(sent_text, "Got it - nothing to add or do.")


class StaleApprovalCannotBeReplayedTests(unittest.TestCase):
    """Once a proposal is approved or discarded, its changes/action must be
    cleared - otherwise a later, unrelated "yes" reply could silently
    re-trigger committing (or discarding) a proposal that already resolved
    one way or the other."""

    def setUp(self):
        self.temp_dir = Path(__file__).parent / ".test-telegram-stale-data"
        shutil.rmtree(self.temp_dir, ignore_errors=True)
        self.temp_dir.mkdir()
        self.store = ApprovalStore(self.temp_dir / "approvals.json")
        self.finalize = AsyncMock()
        self.advance = AsyncMock(return_value=dict(NO_OP_RESULT))
        self.telegram = TelegramApprovals(
            self.store,
            advance=self.advance,
            finalize=self.finalize,
            execute_action=AsyncMock(),
            execute_message_decision=AsyncMock(),
        )
        self.telegram._send = AsyncMock(side_effect=[201, 202, 203, 204])
        self.telegram._answer_callback = AsyncMock()

    def tearDown(self):
        shutil.rmtree(self.temp_dir, ignore_errors=True)

    def test_yes_after_approval_does_not_recommit_the_same_change(self):
        self.advance.return_value = {
            "question": None,
            "reply": None,
            "changes": [{"kind": "sender_list", "list": "blacklist", "selector": "spam.example"}],
            "action": None,
            "caveat": None,
        }
        brief_id, summary, _ = asyncio.run(
            self.telegram.propose_new("Block this sender", "From: bad@spam.example")
        )
        self.assertIsNotNone(summary)

        asyncio.run(self.telegram._handle_update(_reply_update(201, "yes")))
        self.assertEqual(self.finalize.await_count, 1)
        self.assertEqual(self.store.get_brief(brief_id)["status"], "resolved")

        self.advance.return_value = dict(NO_OP_RESULT)
        asyncio.run(self.telegram._handle_update(_reply_update(202, "yes")))
        self.assertEqual(self.finalize.await_count, 1)
        self.assertEqual(self.advance.await_count, 2)


class BriefNeverForceAbandonsTests(unittest.TestCase):
    """A brief that stays unresolved across many rounds must keep being
    worked, never auto-discarded on a round count - the recipient not being
    satisfied yet is exactly the case that must keep getting real answers."""

    def setUp(self):
        self.temp_dir = Path(__file__).parent / ".test-telegram-rounds-data"
        shutil.rmtree(self.temp_dir, ignore_errors=True)
        self.temp_dir.mkdir()
        self.store = ApprovalStore(self.temp_dir / "approvals.json")
        self.advance = AsyncMock(return_value={
            "question": "Which folder should this go to?",
            "reply": None,
            "changes": [],
            "action": None,
            "caveat": None,
        })
        self.telegram = TelegramApprovals(
            self.store,
            advance=self.advance,
            finalize=AsyncMock(),
            execute_action=AsyncMock(),
            execute_message_decision=AsyncMock(),
        )
        sent_ids = list(range(301, 301 + 40))
        self.telegram._send = AsyncMock(side_effect=sent_ids)
        self.telegram._answer_callback = AsyncMock()

    def tearDown(self):
        shutil.rmtree(self.temp_dir, ignore_errors=True)

    def test_a_dozen_unresolved_rounds_never_trigger_a_give_up_message(self):
        brief_id, _, _ = asyncio.run(
            self.telegram.propose_new("File my mail somewhere", "From: someone@example.com")
        )
        last_message_id = 301
        for round_number in range(12):
            asyncio.run(self.telegram._handle_update(
                _reply_update(last_message_id, f"reply number {round_number}")
            ))
            last_message_id = 301 + self.telegram._send.await_count - 1

        brief = self.store.get_brief(brief_id)
        self.assertEqual(brief["status"], "open")
        self.assertEqual(brief["rounds"], 12)
        for call in self.telegram._send.await_args_list:
            self.assertNotIn("gone on a while", call.args[0])


class UnsubscribeFollowupAndRecipientEmailTests(unittest.TestCase):
    """execute_action's bounce_decision followup used to be offered for any
    outcome that named a domain, including a plain FAILED - which meant a
    benign failure (an unsubscribe form asking for an email address that
    was never supplied) still prompted to blacklist a legitimate sender.
    It should only be offered when the outcome actually recommends it."""

    def setUp(self):
        self.temp_dir = Path(__file__).parent / ".test-telegram-unsubscribe-followup-data"
        shutil.rmtree(self.temp_dir, ignore_errors=True)
        self.temp_dir.mkdir()
        self.store = ApprovalStore(self.temp_dir / "approvals.json")
        self.execute_action = AsyncMock()
        self.telegram = TelegramApprovals(
            self.store,
            advance=AsyncMock(),
            finalize=AsyncMock(),
            execute_action=self.execute_action,
            execute_message_decision=AsyncMock(),
        )
        self.telegram._send = AsyncMock(side_effect=range(401, 420))
        self.telegram._answer_callback = AsyncMock()

    def tearDown(self):
        shutil.rmtree(self.temp_dir, ignore_errors=True)

    def _brief_with_action(self, message_metadata=None) -> str:
        brief_id = self.store.create_brief(
            "From: news@prcmarketresearch.com\n\nUnsubscribe link",
            message_metadata=message_metadata,
        )
        self.store.update_brief(
            brief_id, action="UNSUBSCRIBE: prcmarketresearch.com", status="open"
        )
        return brief_id

    def test_none_recommendation_does_not_propose_a_blacklist_followup(self):
        self.execute_action.return_value = (
            "Unsubscribe: FAILED. The form needs an email address.",
            {"kind": "bounce_decision", "domain": "prcmarketresearch.com", "recommendation": "none"},
        )
        brief_id = self._brief_with_action()

        asyncio.run(self.telegram._approve(brief_id, self.store.get_brief(brief_id)))

        sent_texts = [call.args[0] for call in self.telegram._send.await_args_list]
        self.assertFalse(any("blacklist" in text.lower() for text in sent_texts))
        self.assertEqual(self.store.get_brief(brief_id)["status"], "resolved")

    def test_hard_recommendation_still_proposes_a_blacklist_followup(self):
        self.execute_action.return_value = (
            "Unsubscribe: SKIPPED_UNSAFE. The route looked like a phishing page.",
            {"kind": "bounce_decision", "domain": "prcmarketresearch.com", "recommendation": "hard"},
        )
        brief_id = self._brief_with_action()

        asyncio.run(self.telegram._approve(brief_id, self.store.get_brief(brief_id)))

        sent_texts = [call.args[0] for call in self.telegram._send.await_args_list]
        self.assertTrue(any("blacklist" in text.lower() for text in sent_texts))
        self.assertEqual(self.store.get_brief(brief_id)["status"], "open")

    def test_recipient_email_from_message_metadata_reaches_execute_action(self):
        self.execute_action.return_value = ("Unsubscribe: UNSUBSCRIBED.", None)
        brief_id = self._brief_with_action(
            message_metadata={"recipient_email": "subscriber@example.com"}
        )

        asyncio.run(self.telegram._approve(brief_id, self.store.get_brief(brief_id)))

        self.execute_action.assert_awaited_once_with(
            "UNSUBSCRIBE: prcmarketresearch.com",
            "From: news@prcmarketresearch.com\n\nUnsubscribe link",
            brief_id,
            "subscriber@example.com",
        )


class MostRecentOpenBriefTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = Path(__file__).parent / ".test-most-recent-open-brief-data"
        shutil.rmtree(self.temp_dir, ignore_errors=True)
        self.temp_dir.mkdir()
        self.store = ApprovalStore(self.temp_dir / "approvals.json")

    def tearDown(self):
        shutil.rmtree(self.temp_dir, ignore_errors=True)

    def test_returns_none_when_nothing_is_open(self):
        self.assertIsNone(self.store.most_recent_open_brief())

    def test_returns_the_newest_of_several_open_briefs(self):
        older = self.store.create_brief("From: a@example.com")
        newer = self.store.create_brief("From: b@example.com")
        self.assertEqual(self.store.most_recent_open_brief(), newer)
        self.assertNotEqual(self.store.most_recent_open_brief(), older)

    def test_ignores_a_resolved_brief_even_if_newer(self):
        open_brief = self.store.create_brief("From: a@example.com")
        resolved_brief = self.store.create_brief("From: b@example.com")
        self.store.resolve_brief(resolved_brief)
        self.assertEqual(self.store.most_recent_open_brief(), open_brief)


class ReplyToUntrackedMessageFallsBackToOpenBriefTests(unittest.TestCase):
    """A reply to a message Mercury never tracked - too old to still be in
    the index, or a reply to something Loremaster said rather than Mercury -
    used to be silently dropped. It should fall back to whatever is
    genuinely still open rather than leaving the recipient's reply
    unanswered."""

    def setUp(self):
        self.temp_dir = Path(__file__).parent / ".test-untracked-reply-data"
        shutil.rmtree(self.temp_dir, ignore_errors=True)
        self.temp_dir.mkdir()
        self.store = ApprovalStore(self.temp_dir / "approvals.json")
        # A question (rather than NO_OP_RESULT) keeps the brief "open" -
        # the fallback only has something to find when a brief actually is.
        self.advance = AsyncMock(return_value={
            "question": "Which folder should this go to?",
            "reply": None,
            "changes": [],
            "action": None,
            "caveat": None,
        })
        self.telegram = TelegramApprovals(
            self.store,
            advance=self.advance,
            finalize=AsyncMock(),
            execute_action=AsyncMock(),
            execute_message_decision=AsyncMock(),
        )
        self.telegram._send = AsyncMock(side_effect=range(501, 520))
        self.telegram._answer_callback = AsyncMock()

    def tearDown(self):
        shutil.rmtree(self.temp_dir, ignore_errors=True)

    def test_reply_to_an_untracked_message_still_reaches_the_open_brief(self):
        brief_id, _, _ = asyncio.run(
            self.telegram.propose_new("Flag this", "From: someone@example.com")
        )

        # 999999 was never sent by Mercury - not in message_index at all,
        # e.g. a reply to a much older message or to Loremaster's own text.
        asyncio.run(self.telegram._handle_update(_reply_update(999999, "Do it")))

        self.assertEqual(self.advance.await_count, 2)
        brief = self.store.get_brief(brief_id)
        self.assertEqual(len(brief["history"]), 4)
        self.assertEqual(brief["history"][2], {"speaker": "user", "text": "Do it"})

    def test_reply_to_an_untracked_message_with_nothing_open_is_dropped(self):
        asyncio.run(self.telegram._handle_update(_reply_update(999999, "Do it")))
        self.assertEqual(self.advance.await_count, 0)


class ApproveActionFailureTests(unittest.TestCase):
    """A failed action used to propagate an exception up into
    poll_forever's blanket try/except, which logged nothing and told the
    recipient nothing - the action's own live progress updates would arrive
    and then just stop forever. It must now report back and leave the
    brief retryable."""

    def setUp(self):
        self.temp_dir = Path(__file__).parent / ".test-approve-failure-data"
        shutil.rmtree(self.temp_dir, ignore_errors=True)
        self.temp_dir.mkdir()
        self.store = ApprovalStore(self.temp_dir / "approvals.json")
        self.execute_action = AsyncMock(side_effect=RuntimeError("agent gateway timed out"))
        self.telegram = TelegramApprovals(
            self.store,
            advance=AsyncMock(),
            finalize=AsyncMock(),
            execute_action=self.execute_action,
            execute_message_decision=AsyncMock(),
        )
        self.telegram._send = AsyncMock(side_effect=range(601, 620))
        self.telegram._answer_callback = AsyncMock()

    def tearDown(self):
        shutil.rmtree(self.temp_dir, ignore_errors=True)

    def test_failed_action_reports_back_and_stays_open_for_retry(self):
        brief_id = self.store.create_brief("From: news@prcmarketresearch.com")
        self.store.update_brief(brief_id, action="UNSUBSCRIBE: prcmarketresearch.com", status="open")

        asyncio.run(self.telegram._approve(brief_id, self.store.get_brief(brief_id)))

        sent_texts = [call.args[0] for call in self.telegram._send.await_args_list]
        self.assertTrue(any("Action failed" in text and "RuntimeError" in text for text in sent_texts))
        brief = self.store.get_brief(brief_id)
        self.assertEqual(brief["status"], "open")
        self.assertEqual(brief["action"], "UNSUBSCRIBE: prcmarketresearch.com")

    def test_retry_after_failure_calls_execute_action_again(self):
        brief_id = self.store.create_brief("From: news@prcmarketresearch.com")
        self.store.update_brief(brief_id, action="UNSUBSCRIBE: prcmarketresearch.com", status="open")
        asyncio.run(self.telegram._approve(brief_id, self.store.get_brief(brief_id)))

        self.execute_action.side_effect = None
        self.execute_action.return_value = ("Unsubscribe: UNSUBSCRIBED.", None)
        asyncio.run(self.telegram._approve(brief_id, self.store.get_brief(brief_id)))

        self.assertEqual(self.execute_action.await_count, 2)
        self.assertEqual(self.store.get_brief(brief_id)["status"], "resolved")


if __name__ == "__main__":
    unittest.main()
