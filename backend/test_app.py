import asyncio
import json
import os
import shutil
import sys
import types
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

os.environ.setdefault("MERCURY_SHARED_SECRET", "test-secret")
os.environ.setdefault("TELEGRAM_BOT_TOKEN", "test-token")
os.environ.setdefault("TELEGRAM_CHAT_ID", "test-chat")
os.environ.setdefault("PROMPT_INJECTION_CLASSIFIER_URL", "http://classifier.test")
os.environ.setdefault("AGENT_GATEWAY_URL", "http://judge.test")
os.environ.setdefault("AGENT_GATEWAY_SECRET", "judge-secret")

try:
    import fastapi  # noqa: F401
except ModuleNotFoundError:
    fastapi_stub = types.ModuleType("fastapi")
    responses_stub = types.ModuleType("fastapi.responses")

    class FastAPI:
        def __init__(self, **kwargs):
            pass

        def get(self, path):
            return lambda function: function

        def post(self, path):
            return lambda function: function

    class HTTPException(Exception):
        def __init__(self, status_code, detail):
            super().__init__(detail)
            self.status_code = status_code
            self.detail = detail

    class Request:
        pass

    class JSONResponse:
        def __init__(self, status_code, content):
            self.status_code = status_code
            self.content = content

    fastapi_stub.FastAPI = FastAPI
    fastapi_stub.Header = lambda default=None: default
    fastapi_stub.HTTPException = HTTPException
    fastapi_stub.Request = Request
    responses_stub.JSONResponse = JSONResponse
    sys.modules["fastapi"] = fastapi_stub
    sys.modules["fastapi.responses"] = responses_stub

try:
    import httpx  # noqa: F401
except ModuleNotFoundError:
    httpx_stub = types.ModuleType("httpx")

    class AsyncClient:
        pass

    class Client:
        pass

    class BasicAuth:
        def __init__(self, username, password):
            self.username = username
            self.password = password

    httpx_stub.AsyncClient = AsyncClient
    httpx_stub.Client = Client
    httpx_stub.BasicAuth = BasicAuth
    sys.modules["httpx"] = httpx_stub

import app
from approvals import ApprovalStore
from filtering import FilteringPolicyStore
from telegram_approvals import TelegramApprovals


class FakeRequest:
    def __init__(self, payload):
        self.payload = payload

    async def json(self):
        return self.payload


class AppTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = Path(__file__).parent / ".test-app-data"
        shutil.rmtree(self.temp_dir, ignore_errors=True)
        self.temp_dir.mkdir()
        self.store = FilteringPolicyStore(self.temp_dir / "rules_ledger.json")
        self.original_store = app.policy_store
        self.original_classifier = app.classifier
        self.original_judge = app.judge
        self.original_delivery_enabled = app.mail_delivery.DELIVER_ACCEPTED_MAIL
        app.policy_store = self.store
        app.mail_delivery.DELIVER_ACCEPTED_MAIL = False

    def tearDown(self):
        app.policy_store = self.original_store
        app.classifier = self.original_classifier
        app.judge = self.original_judge
        app.mail_delivery.DELIVER_ACCEPTED_MAIL = self.original_delivery_enabled
        shutil.rmtree(self.temp_dir, ignore_errors=True)

    def _payload(self, address="news@example.com"):
        return {
            "from": {
                "text": f"Example News <{address}>",
                "value": [{"name": "Example News", "address": address}],
            },
            "subject": "A routine update",
            "text": "Your order has shipped.",
            "raw": "From: Example News <news@example.com>\r\nSubject: A routine update\r\n\r\nBody",
        }

    def test_deterministic_sender_match_skips_classifier_and_judge(self):
        self.store.put_sender("blacklist", "example.com")
        app.classifier = SimpleNamespace(
            check=AsyncMock(side_effect=AssertionError("classifier must be skipped"))
        )
        app.judge = SimpleNamespace(
            ask=AsyncMock(side_effect=AssertionError("judge must be skipped"))
        )
        events = []

        with patch.object(app.event_log, "log_event", side_effect=lambda table, fields: events.append((table, fields))):
            response = asyncio.run(app.ingest(FakeRequest(self._payload()), "test-secret"))

        self.assertEqual(response.status_code, 550)
        self.assertEqual(app.classifier.check.await_count, 0)
        self.assertEqual(app.judge.ask.await_count, 0)
        message = next(fields for table, fields in events if table == "messages")
        self.assertEqual(message["injection_label"], "SKIPPED_BLACKLIST")
        self.assertEqual(message["verdict"], "SPAM")
        self.assertIn("deterministic blacklist", message["reasoning"])

    def test_judge_prompt_uses_buckets_and_legitimate_mail_calibration(self):
        captured = []

        async def ask(prompt):
            captured.append(prompt)
            return """VERDICT: SPAM
DISPOSITION: 550
CATEGORY: SCAM
ALERT: NONE
REASONING: Matched the stored condition.
RULE_MATCH: A concrete scam condition"""

        app.judge = SimpleNamespace(ask=ask)
        result = asyncio.run(app.judge_email(
            "From: sender@example.com\n\nMessage",
            {"label": "SAFE", "score": 0.01},
            {
                "550": ["A concrete scam condition"],
                "421": ["An ambiguous condition"],
                "250": ["A trusted content condition"],
            },
        ))

        prompt = captured[0]
        self.assertIn("Rules that mean HARD BOUNCE (550)", prompt)
        self.assertIn("Rules that mean SOFT-DEFER (421)", prompt)
        self.assertIn("Rules that mean ACCEPT (250)", prompt)
        self.assertIn("Do not use 421 merely because the sender is unfamiliar", prompt)
        self.assertEqual(result["triggered_rule"], "A concrete scam condition")

    def test_native_custom_action_routes_delivery_to_folder(self):
        self.store.put_sender("whitelist", "example.com")
        self.store.put_custom_action("example.com", "File in Archive", "Archive")
        app.classifier = SimpleNamespace(
            check=AsyncMock(side_effect=AssertionError("classifier must be skipped"))
        )
        app.judge = SimpleNamespace(
            ask=AsyncMock(side_effect=AssertionError("judge must be skipped"))
        )
        app.mail_delivery.DELIVER_ACCEPTED_MAIL = True
        deliveries = []

        def deliver(*args):
            deliveries.append(args)
            return f"delivered to {args[4]}"

        with (
            patch.object(app.mail_delivery, "deliver_accepted_message", side_effect=deliver),
            patch.object(app.event_log, "log_event"),
        ):
            response = asyncio.run(app.ingest(FakeRequest(self._payload()), "test-secret"))

        self.assertEqual(response.status_code, 250)
        self.assertEqual(deliveries[0][4], "Archive")

    def test_brief_parser_returns_typed_changes(self):
        parsed = app._parse_brief_response("""QUESTION: NONE
SENDER_LIST: BLACKLIST | example.com
SEMANTIC_RULE: 421 | The sender identity is unclear and links are mismatched
CUSTOM_ACTION: news@example.com | File this message in Archive | FOLDER:Archive
ACTION: NONE
CAVEAT: NONE""")

        self.assertEqual([change["kind"] for change in parsed["changes"]], [
            "sender_list", "semantic_rule", "custom_action",
        ])
        self.assertEqual(parsed["changes"][0]["list"], "blacklist")
        self.assertEqual(parsed["changes"][1]["disposition"], "421")
        self.assertEqual(parsed["changes"][2]["native_folder"], "Archive")


class TelegramDecisionTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = Path(__file__).parent / ".test-telegram-data"
        shutil.rmtree(self.temp_dir, ignore_errors=True)
        self.temp_dir.mkdir()
        self.store = ApprovalStore(self.temp_dir / "approvals.json")
        self.finalize = AsyncMock()
        self.execute_decision = AsyncMock(return_value=(
            "The message remains deferred.",
            {"kind": "sender_list", "list": "greylist", "selector": "example.com"},
        ))
        self.telegram = TelegramApprovals(
            self.store,
            advance=AsyncMock(),
            discuss=AsyncMock(),
            finalize=self.finalize,
            execute_action=AsyncMock(),
            execute_message_decision=self.execute_decision,
        )
        self.telegram._send = AsyncMock(return_value=101)
        self.telegram._answer_callback = AsyncMock()

    def tearDown(self):
        shutil.rmtree(self.temp_dir, ignore_errors=True)

    def test_report_has_four_decision_buttons_and_followup_needs_approval(self):
        asyncio.run(self.telegram.send_trackable_report(
            "Mercury report",
            "From: news@example.com",
            {"sender_address": "news@example.com", "sender_domain": "example.com"},
        ))
        keyboard = self.telegram._send.await_args.kwargs["keyboard"]
        labels = [button["text"] for row in keyboard["inline_keyboard"] for button in row]
        self.assertEqual(labels, [
            "Unsubscribe", "Soft-bounce", "Hard-bounce", "Deliver + whitelist",
        ])

        brief_id = self.store.brief_for_message(101)
        asyncio.run(self.telegram._handle_callback({
            "id": "callback-1",
            "data": f"decision:soft:{brief_id}",
        }))

        brief = self.store.get_brief(brief_id)
        self.assertEqual(brief["changes"][0]["list"], "greylist")
        self.assertEqual(brief["status"], "open")
        self.assertEqual(self.finalize.await_count, 0)

        asyncio.run(self.telegram._handle_callback({
            "id": "callback-2",
            "data": f"approve:{brief_id}",
        }))
        self.assertEqual(self.finalize.await_count, 1)
        self.assertEqual(self.store.get_brief(brief_id)["status"], "resolved")


if __name__ == "__main__":
    unittest.main()
