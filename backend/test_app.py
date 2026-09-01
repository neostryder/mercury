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
from credential_prompts import CredentialPromptStore
from filtering import FilteringPolicyStore, empty_policy
from telegram_approvals import TelegramApprovals


class FakeRequest:
    def __init__(self, payload):
        self.payload = payload

    async def json(self):
        return self.payload


class FakeBodyRequest:
    def __init__(self, body: bytes, declared_length: int | None = None):
        self.body = body
        self.headers = {}
        if declared_length is not None:
            self.headers["content-length"] = str(declared_length)

    async def stream(self):
        yield self.body


class MutableClock:
    def __init__(self):
        self.value = 1000.0

    def __call__(self):
        return self.value


class AppTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = Path(__file__).parent / ".test-app-data"
        shutil.rmtree(self.temp_dir, ignore_errors=True)
        self.temp_dir.mkdir()
        self.store = FilteringPolicyStore(self.temp_dir / "rules_ledger.json")
        self.original_store = app.policy_store
        self.original_classifier = app.classifier
        self.original_judge = app.judge
        self.original_notifier = app.notifier
        self.original_delivery_enabled = app.mail_delivery.DELIVER_ACCEPTED_MAIL
        app.policy_store = self.store
        app.mail_delivery.DELIVER_ACCEPTED_MAIL = False

    def tearDown(self):
        app.policy_store = self.original_store
        app.classifier = self.original_classifier
        app.judge = self.original_judge
        app.notifier = self.original_notifier
        app.mail_delivery.DELIVER_ACCEPTED_MAIL = self.original_delivery_enabled
        shutil.rmtree(self.temp_dir, ignore_errors=True)

    def _payload(self, address="news@example.com", authenticated=True):
        payload = {
            "from": {
                "text": f"Example News <{address}>",
                "value": [{"name": "Example News", "address": address}],
            },
            "subject": "A routine update",
            "text": "Your order has shipped.",
            "raw": (
                f"From: Example News <{address}>\r\n"
                "Subject: A routine update\r\n\r\nBody"
            ),
        }
        if authenticated:
            payload["dmarc"] = {"status": {"result": "pass"}}
        return payload

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
        self.assertEqual(message["category"], "SENDER_LIST")
        self.assertIn("deterministic blacklist", message["reasoning"])

    def test_blacklist_pattern_match_surfaces_the_pattern_as_the_triggered_rule(self):
        self.store.add_blacklist_pattern(r"^\d{2,}[a-z0-9.-]*\.[a-z]{2,}$")
        app.classifier = SimpleNamespace(
            check=AsyncMock(side_effect=AssertionError("classifier must be skipped"))
        )
        app.judge = SimpleNamespace(
            ask=AsyncMock(side_effect=AssertionError("judge must be skipped"))
        )
        events = []

        with patch.object(app.event_log, "log_event", side_effect=lambda table, fields: events.append((table, fields))):
            response = asyncio.run(app.ingest(
                FakeRequest(self._payload(address="spam@473245firmwarespro.com")), "test-secret"
            ))

        self.assertEqual(response.status_code, 550)
        message = next(fields for table, fields in events if table == "messages")
        self.assertEqual(message["triggered_rule"], r"^\d{2,}[a-z0-9.-]*\.[a-z]{2,}$")
        self.assertIn("blacklist pattern", message["reasoning"])
        self.assertIn(r"^\d{2,}[a-z0-9.-]*\.[a-z]{2,}$", message["reasoning"])

    def test_reversing_a_blacklist_pattern_removes_it(self):
        self.store.add_blacklist_pattern(r"^\d{2,}[a-z0-9.-]*\.[a-z]{2,}$")

        removed = asyncio.run(app._reverse_rule(r"^\d{2,}[a-z0-9.-]*\.[a-z]{2,}$"))

        self.assertTrue(removed)
        self.assertEqual(self.store.load()["blacklist_patterns"], [])

    def test_unauthenticated_sender_match_uses_normal_path_for_every_list(self):
        for list_name in ("blacklist", "greylist", "whitelist"):
            with self.subTest(list_name=list_name):
                self.store.put_sender(list_name, "example.com")
                app.classifier = SimpleNamespace(
                    check=AsyncMock(return_value={"label": "SAFE", "score": 0.01})
                )
                app.judge = SimpleNamespace(ask=AsyncMock(return_value="""VERDICT: LEGIT
DISPOSITION: 250
CATEGORY: TRANSACTIONAL
ALERT: NONE
REASONING: Normal content judgment accepted the message.
RULE_MATCH: NONE"""))
                events = []

                with patch.object(
                    app.event_log,
                    "log_event",
                    side_effect=lambda table, fields: events.append((table, fields)),
                ):
                    response = asyncio.run(app.ingest(
                        FakeRequest(self._payload(authenticated=False)), "test-secret"
                    ))

                self.assertEqual(response.status_code, 250)
                self.assertEqual(app.classifier.check.await_count, 1)
                self.assertEqual(app.judge.ask.await_count, 1)
                message = next(
                    fields for table, fields in events if table == "messages"
                )
                self.assertEqual(message["injection_label"], "SAFE")
                self.assertEqual(message["category"], "TRANSACTIONAL")
                self.assertIn(
                    f"Skipped unauthenticated deterministic {list_name} match",
                    message["reasoning"],
                )
                self.assertIn(
                    "ForwardEmail's own DMARC verdict did not report a pass "
                    "aligned with claimed From domain",
                    message["reasoning"],
                )

    def test_ambiguous_policy_fails_open_and_alerts(self):
        policy = empty_policy()
        policy["sender_lists"]["blacklist"] = ["example.com"]
        policy["sender_lists"]["whitelist"] = ["example.com"]
        self.store.path.write_text(json.dumps(policy), encoding="utf-8")
        app.notifier = SimpleNamespace(send=AsyncMock())

        response = asyncio.run(app.ingest(FakeRequest(self._payload()), "test-secret"))

        self.assertEqual(response.status_code, 250)
        body = (
            response.content
            if hasattr(response, "content")
            else json.loads(response.body.decode("utf-8"))
        )
        self.assertFalse(body["ok"])
        self.assertEqual(app.notifier.send.await_count, 1)

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
        self.assertIsNone(parsed["reply"])

    def test_brief_parser_reads_a_reply_alongside_a_proposal(self):
        parsed = app._parse_brief_response("""QUESTION: NONE
REPLY: You're right, that was never done - fixing it now:
SENDER_LIST: NONE
SEMANTIC_RULE: NONE
CUSTOM_ACTION: NONE
ACTION: UNSUBSCRIBE: fanatical.example
CAVEAT: NONE""")

        self.assertEqual(
            parsed["reply"], "You're right, that was never done - fixing it now:"
        )
        self.assertEqual(parsed["action"], "UNSUBSCRIBE: fanatical.example")
        self.assertEqual(parsed["changes"], [])

    def test_brief_parser_recognizes_blacklist_pattern(self):
        parsed = app._parse_brief_response("""QUESTION: NONE
SENDER_LIST: NONE
BLACKLIST_PATTERN: ^\\d{6}[a-z]+\\.com$
SEMANTIC_RULE: NONE
CUSTOM_ACTION: NONE
ACTION: NONE
CAVEAT: NONE""")

        self.assertEqual([change["kind"] for change in parsed["changes"]], ["blacklist_pattern"])
        self.assertEqual(parsed["changes"][0]["pattern"], r"^\d{6}[a-z]+\.com$")

    def test_brief_parser_rejects_invalid_blacklist_pattern(self):
        with self.assertRaises(ValueError):
            app._parse_brief_response("""QUESTION: NONE
SENDER_LIST: NONE
BLACKLIST_PATTERN: [unclosed
SEMANTIC_RULE: NONE
CUSTOM_ACTION: NONE
ACTION: NONE
CAVEAT: NONE""")

    def test_action_field_is_not_confused_with_custom_action(self):
        parsed = app._parse_brief_response("""QUESTION: NONE
REPLY: NONE
SENDER_LIST: NONE
SEMANTIC_RULE: NONE
CUSTOM_ACTION: news@example.com | File this message in Archive | FOLDER:Archive
ACTION: UNSUBSCRIBE: fanatical.example
CAVEAT: NONE""")

        self.assertEqual(parsed["changes"][0]["kind"], "custom_action")
        self.assertEqual(parsed["changes"][0]["instruction"], "File this message in Archive")
        self.assertEqual(parsed["action"], "UNSUBSCRIBE: fanatical.example")

    def test_brief_parser_recognizes_gandalf_action(self):
        parsed = app._parse_brief_response("""QUESTION: NONE
REPLY: NONE
SENDER_LIST: NONE
SEMANTIC_RULE: NONE
CUSTOM_ACTION: NONE
ACTION: GANDALF: Log this as a competitor idea and research it
CAVEAT: NONE""")

        self.assertEqual(
            parsed["action"],
            "GANDALF: Log this as a competitor idea and research it",
        )
        self.assertEqual(parsed["changes"], [])

    def test_dispatch_action_routes_gandalf_without_followup(self):
        execute = AsyncMock(return_value="Sent to Gandalf.")
        with patch.object(app, "execute_gandalf_handoff", new=execute):
            outcome, followup = asyncio.run(app.dispatch_action(
                "GANDALF: Research Acme as a competitor", "Flagged message"
            ))

        self.assertEqual(outcome, "Sent to Gandalf.")
        self.assertIsNone(followup)
        execute.assert_awaited_once_with(
            "Research Acme as a competitor", "Flagged message"
        )

    def test_execute_gandalf_handoff_sends_and_logs_success(self):
        events = []
        with (
            patch.object(app.gandalf_relay, "send_to_gandalf", return_value=True) as send,
            patch.object(
                app.event_log,
                "log_event",
                side_effect=lambda table, fields: events.append((table, fields)),
            ),
        ):
            outcome = asyncio.run(app.execute_gandalf_handoff(
                "Research Acme as a competitor", "From: news@acme.example\n\nMessage body"
            ))

        self.assertEqual(outcome, "Sent to Gandalf.")
        subject, body = send.call_args.args
        self.assertEqual(subject, "Mercury handoff: Research Acme as a competitor")
        self.assertIn("Research Acme as a competitor", body)
        self.assertIn("From: news@acme.example\n\nMessage body", body)
        self.assertEqual(events[0][0], "actions")
        self.assertEqual(events[0][1]["kind"], "GANDALF_HANDOFF")
        self.assertEqual(events[0][1]["result"], "SENT")
        self.assertEqual(events[0][1]["outcome_summary"], outcome)
        self.assertIsNone(events[0][1]["domain"])

    def test_execute_gandalf_handoff_logs_failed_send(self):
        events = []
        with (
            patch.object(app.gandalf_relay, "send_to_gandalf", return_value=False),
            patch.object(
                app.event_log,
                "log_event",
                side_effect=lambda table, fields: events.append((table, fields)),
            ),
        ):
            outcome = asyncio.run(app.execute_gandalf_handoff(
                "Research Acme as a competitor", "Flagged message"
            ))

        self.assertEqual(outcome, "Could not reach Gandalf - reply to try again.")
        self.assertEqual(events[0][0], "actions")
        self.assertEqual(events[0][1]["kind"], "GANDALF_HANDOFF")
        self.assertEqual(events[0][1]["result"], "FAILED")
        self.assertEqual(events[0][1]["outcome_summary"], outcome)
        self.assertIsNone(events[0][1]["domain"])

    def test_filtering_management_endpoint_mutates_all_entry_types(self):
        with patch.object(app.event_log, "log_event"):
            asyncio.run(app.change_filtering_policy(FakeRequest({
                "operation": "put",
                "kind": "sender_list",
                "list": "blacklist",
                "selector": "example.com",
            }), "test-secret"))
            asyncio.run(app.change_filtering_policy(FakeRequest({
                "operation": "put",
                "kind": "sender_list",
                "list": "whitelist",
                "selector": "example.com",
            }), "test-secret"))
            asyncio.run(app.change_filtering_policy(FakeRequest({
                "operation": "put",
                "kind": "semantic_rule",
                "disposition": "421",
                "rule": "A genuinely ambiguous message condition",
            }), "test-secret"))
            with patch.object(app.mail_delivery, "list_folders", return_value=["INBOX", "Archive"]):
                asyncio.run(app.change_filtering_policy(FakeRequest({
                    "operation": "put",
                    "kind": "custom_action",
                    "selector": "example.com",
                    "instruction": "File accepted mail in Archive",
                    "native_folder": "Archive",
                }), "test-secret"))
            asyncio.run(app.change_filtering_policy(FakeRequest({
                "operation": "put",
                "kind": "blacklist_pattern",
                "pattern": r"^\d{6}[a-z]+\.com$",
            }), "test-secret"))

        policy = asyncio.run(app.get_filtering_policy("test-secret"))
        self.assertEqual(policy["sender_lists"]["blacklist"], [])
        self.assertEqual(policy["sender_lists"]["whitelist"], ["example.com"])
        self.assertEqual(
            policy["semantic_rules"]["421"], ["A genuinely ambiguous message condition"]
        )
        self.assertEqual(policy["custom_actions"][0]["native"]["folder"], "Archive")
        self.assertEqual(policy["blacklist_patterns"], [r"^\d{6}[a-z]+\.com$"])

    def test_filtering_management_endpoint_rejects_invalid_pattern(self):
        with patch.object(app.event_log, "log_event"):
            with self.assertRaises(app.HTTPException) as ctx:
                asyncio.run(app.change_filtering_policy(FakeRequest({
                    "operation": "put",
                    "kind": "blacklist_pattern",
                    "pattern": "[unclosed",
                }), "test-secret"))
        self.assertEqual(ctx.exception.status_code, 400)

    def test_propose_rule_with_no_attached_message_sends_a_general_instruction_marker(self):
        fake_telegram = SimpleNamespace(
            propose_new=AsyncMock(return_value=("brief-1", "blacklist pattern: test", None))
        )
        with patch.object(app, "telegram_approvals", fake_telegram):
            response = asyncio.run(app.propose_rule(
                FakeRequest({"instruction": "bounce this pattern", "messages": []}),
                "test-secret",
            ))

        self.assertTrue(response["ok"])
        instruction_arg, message_context_arg, _ = fake_telegram.propose_new.call_args[0]
        self.assertEqual(instruction_arg, "bounce this pattern")
        self.assertIn("no message attached", message_context_arg)


class CredentialPromptEndpointTests(unittest.TestCase):
    def setUp(self):
        self.clock = MutableClock()
        self.store = CredentialPromptStore(clock=self.clock)
        self.original_store = app.credential_prompts._store
        app.credential_prompts._store = self.store

    def tearDown(self):
        app.credential_prompts._store = self.original_store

    def _request(self, username="aaron", password="one-time-password"):
        return FakeBodyRequest(json.dumps({
            "username": username,
            "password": password,
        }).encode("utf-8"))

    def test_valid_token_get_and_submit(self):
        token = self.store.create("fanatical.com")

        status = asyncio.run(app.get_credential_prompt(token))
        self.assertEqual(status, {
            "valid": True,
            "domain": "fanatical.com",
            "expires_in_seconds": 600,
        })
        self.assertEqual(
            asyncio.run(app.submit_credential_prompt(token, self._request())),
            {"ok": True},
        )

    def test_unknown_and_expired_tokens_are_invalid(self):
        self.assertEqual(
            asyncio.run(app.get_credential_prompt("unknown")), {"valid": False}
        )
        self.assertEqual(
            asyncio.run(app.submit_credential_prompt("unknown", self._request())),
            {"ok": False, "error": "expired or already used"},
        )

        token = self.store.create("fanatical.com")
        self.clock.value += 601
        self.assertEqual(
            asyncio.run(app.get_credential_prompt(token)), {"valid": False}
        )
        self.assertEqual(
            asyncio.run(app.submit_credential_prompt(token, self._request())),
            {"ok": False, "error": "expired or already used"},
        )

    def test_already_used_token_rejects_a_second_post(self):
        token = self.store.create("fanatical.com")

        self.assertEqual(
            asyncio.run(app.submit_credential_prompt(token, self._request())),
            {"ok": True},
        )
        self.assertEqual(
            asyncio.run(app.submit_credential_prompt(token, self._request(password="again"))),
            {"ok": False, "error": "expired or already used"},
        )
        self.assertEqual(
            asyncio.run(app.get_credential_prompt(token)), {"valid": False}
        )

    def test_oversized_body_is_rejected(self):
        token = self.store.create("fanatical.com")
        request = FakeBodyRequest(b"x" * 4097)

        with self.assertRaises(app.HTTPException) as raised:
            asyncio.run(app.submit_credential_prompt(token, request))

        self.assertEqual(raised.exception.status_code, 413)
        self.assertIsNotNone(self.store.get_status(token))


class UnsubscribeCredentialFlowTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = Path(__file__).parent / ".test-unsubscribe-credential-data"
        shutil.rmtree(self.temp_dir, ignore_errors=True)
        self.temp_dir.mkdir()
        self.approvals_path = self.temp_dir / "approvals.json"
        self.store = ApprovalStore(self.approvals_path)
        self.original_judge = app.judge
        self.original_telegram = app.telegram_approvals

        self.telegram = TelegramApprovals(
            self.store,
            advance=AsyncMock(),
            finalize=AsyncMock(),
            execute_action=app.dispatch_action,
            execute_message_decision=AsyncMock(),
        )
        self.telegram._send = AsyncMock(side_effect=range(1001, 1020))
        app.telegram_approvals = self.telegram

    def tearDown(self):
        app.judge = self.original_judge
        app.telegram_approvals = self.original_telegram
        shutil.rmtree(self.temp_dir, ignore_errors=True)

    def _create_unsubscribe_brief(self) -> str:
        brief_id = self.store.create_brief(
            "From: Fanatical <news@fanatical.com>\n\nUnsubscribe link"
        )
        self.store.update_brief(
            brief_id,
            action="UNSUBSCRIBE: fanatical.com",
            status="open",
        )
        return brief_id

    def test_needs_signin_timeout_is_reported_without_bounce_followup(self):
        app.judge = SimpleNamespace(ask=AsyncMock(return_value="""SAFE: yes
DOMAIN: fanatical.com
RESULT: NEEDS_SIGNIN
SUMMARY: The verified preference page requires an account sign-in."""))
        wait_for = AsyncMock(return_value=None)
        events = []
        brief_id = self._create_unsubscribe_brief()

        with (
            patch.object(app.credential_prompts, "create", return_value="one-time-token"),
            patch.object(app.credential_prompts, "wait_for", new=wait_for),
            patch.object(
                app.event_log,
                "log_event",
                side_effect=lambda table, fields: events.append((table, fields)),
            ),
        ):
            asyncio.run(self.telegram._approve(brief_id, self.store.get_brief(brief_id)))

        self.assertEqual(app.judge.ask.await_count, 1)
        wait_for.assert_awaited_once_with("one-time-token", timeout_seconds=600)
        self.assertEqual(events[0][1]["result"], "NEEDS_SIGNIN_TIMED_OUT")
        self.assertEqual(self.store.get_brief(brief_id)["status"], "resolved")
        self.assertEqual(self.store.get_brief(brief_id)["changes"], [])
        history_text = self.approvals_path.read_text(encoding="utf-8")
        self.assertIn("mercury.rpgm.tools/credential/one-time-token", history_text)
        self.assertIn("Didn't hear back in time", history_text)

    def test_submitted_credential_is_used_once_and_never_persisted_or_logged(self):
        fake_username = "credential-user@example.com"
        fake_password = "FAKE-ONE-TIME-PASSWORD-9f6e"
        app.judge = SimpleNamespace(ask=AsyncMock(side_effect=[
            """SAFE: yes
DOMAIN: fanatical.com
RESULT: NEEDS_SIGNIN
SUMMARY: The verified preference page requires an account sign-in.""",
            f"""SAFE: yes
DOMAIN: fanatical.com
RESULT: UNSUBSCRIBED
SUMMARY: Signed in as {fake_username} using {fake_password} and completed the unsubscribe.""",
        ]))
        wait_for = AsyncMock(return_value=(fake_username, fake_password))
        events = []
        brief_id = self._create_unsubscribe_brief()

        with (
            patch.object(app.credential_prompts, "create", return_value="one-time-token"),
            patch.object(app.credential_prompts, "wait_for", new=wait_for),
            patch.object(
                app.event_log,
                "log_event",
                side_effect=lambda table, fields: events.append((table, fields)),
            ),
        ):
            asyncio.run(self.telegram._approve(brief_id, self.store.get_brief(brief_id)))

        self.assertEqual(app.judge.ask.await_count, 2)
        second_prompt = app.judge.ask.await_args_list[1].args[0]
        self.assertIn(fake_username, second_prompt)
        self.assertIn(fake_password, second_prompt)
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0][1]["result"], "UNSUBSCRIBED")

        serialized_events = json.dumps(events)
        persisted_brief = self.approvals_path.read_text(encoding="utf-8")
        self.assertNotIn(fake_password, serialized_events)
        self.assertNotIn(fake_password, persisted_brief)
        self.assertNotIn(fake_username, serialized_events)
        self.assertNotIn(fake_username, persisted_brief)
        self.assertIn("[redacted credential]", persisted_brief)

    def test_unsafe_needs_signin_response_never_creates_a_prompt(self):
        app.judge = SimpleNamespace(ask=AsyncMock(return_value="""SAFE: no
DOMAIN: unrelated-login.example
RESULT: NEEDS_SIGNIN
SUMMARY: The redirect requests a password on an unrelated domain."""))
        events = []

        with (
            patch.object(
                app.credential_prompts,
                "create",
                side_effect=AssertionError("unsafe response must not create a prompt"),
            ),
            patch.object(
                app.event_log,
                "log_event",
                side_effect=lambda table, fields: events.append((table, fields)),
            ),
        ):
            outcome, followup = asyncio.run(app.execute_unsubscribe_action(
                "Unsubscribe from example.com", "From: news@example.com"
            ))

        self.assertIn("SKIPPED_UNSAFE", outcome)
        self.assertEqual(events[0][1]["result"], "SKIPPED_UNSAFE")
        self.assertEqual(followup["recommendation"], "hard")


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
            {
                "sender_address": "news@example.com",
                "sender_domain": "example.com",
                "raw_message": "Subject: Test\r\n\r\nBody",
            },
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
        self.assertNotIn("raw_message", brief["message_metadata"])
        self.assertEqual(self.finalize.await_count, 0)

        asyncio.run(self.telegram._handle_callback({
            "id": "callback-2",
            "data": f"approve:{brief_id}",
        }))
        self.assertEqual(self.finalize.await_count, 1)
        self.assertEqual(self.store.get_brief(brief_id)["status"], "resolved")


if __name__ == "__main__":
    unittest.main()
