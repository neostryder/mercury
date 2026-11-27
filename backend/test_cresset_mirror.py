import asyncio
import hashlib
import hmac
import json
import os
import shutil
import sys
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

os.environ.setdefault("MERCURY_SHARED_SECRET", "test-secret")
os.environ.setdefault("TELEGRAM_BOT_TOKEN", "test-token")
os.environ.setdefault("TELEGRAM_CHAT_ID", "test-chat")

sys.path.insert(0, str(Path(__file__).parent))

import cresset_mirror as mirror
from approvals import ApprovalStore
from telegram_approvals import TelegramApprovals

SOURCE_KEY = "crs_fake-source-key"
ENV = {
    "MERCURY_CRESSET_MODE": "both",
    "CRESSET_URL": "https://cresset.example.com",
    "CRESSET_MERCURY_TOKEN": SOURCE_KEY,
    "MERCURY_CRESSET_ALLOW_UNDER_TEST": "1",
    "MERCURY_ALERT_SETTINGS_PATH": str(Path(__file__).parent / "no-such-alert-settings.json"),
}


def _sign(body: bytes, timestamp: str, key: str = SOURCE_KEY) -> str:
    secret = hashlib.sha256(key.encode()).hexdigest().encode()
    return "sha256=" + hmac.new(secret, f"{timestamp}.".encode() + body, hashlib.sha256).hexdigest()


def _mock_client(handler):
    client = mirror.httpx.AsyncClient
    transport = mirror.httpx.MockTransport(handler)
    return patch.object(mirror.httpx, "AsyncClient", side_effect=lambda **kwargs: client(transport=transport, **kwargs))


class RegistryTests(unittest.TestCase):
    def setUp(self):
        self.clock = SimpleNamespace(monotonic=lambda: self.now)
        self.now = 0
        for patcher in (
            patch.dict(os.environ, ENV, clear=True),
            patch.object(mirror, "_sender_config", None),
            patch.object(mirror, "time", self.clock),
        ):
            patcher.start()
            self.addCleanup(patcher.stop)

    def test_the_mode_is_cached_and_refreshed_after_ten_minutes(self):
        modes = iter(("cresset", "direct"))

        def config(request):
            self.assertEqual(request.method, "GET")
            self.assertEqual(str(request.url), f"{ENV['CRESSET_URL']}/v1/sender-config?sender=mercury")
            self.assertEqual(request.headers["authorization"], f"Bearer {SOURCE_KEY}")
            return mirror.httpx.Response(200, json={"sender": "mercury", "mode": next(modes)})

        with _mock_client(config) as client:
            self.assertEqual(asyncio.run(mirror.delivery_mode()), "cresset")
            self.now = 599
            self.assertEqual(asyncio.run(mirror.delivery_mode()), "cresset")
            client.assert_called_once_with(timeout=1.5)
            self.now = 600
            self.assertEqual(asyncio.run(mirror.delivery_mode()), "off")
            self.assertEqual(client.call_count, 2)

    def test_hub_failure_uses_stale_mode_then_local_mode_if_credentials_change(self):
        def config(request):
            if self.now:
                raise mirror.httpx.ConnectError("down")
            return mirror.httpx.Response(200, json={"sender": "mercury", "mode": "cresset"})

        with _mock_client(config):
            self.assertEqual(asyncio.run(mirror.delivery_mode()), "cresset")
            self.now = 601
            self.assertEqual(asyncio.run(mirror.delivery_mode()), "cresset")
            with patch.dict(os.environ, {"CRESSET_MERCURY_TOKEN": "other-fixture"}):
                self.assertEqual(asyncio.run(mirror.delivery_mode()), "both")

    def test_hub_failure_without_a_cache_uses_the_local_mode(self):
        with _mock_client(lambda request: mirror.httpx.Response(503)):
            self.assertEqual(asyncio.run(mirror.delivery_mode()), "both")

    def test_missing_credentials_skip_the_registry(self):
        for missing in ("CRESSET_URL", "CRESSET_MERCURY_TOKEN"):
            with self.subTest(missing=missing), patch.dict(os.environ, {missing: ""}), _mock_client(None) as client:
                self.assertEqual(asyncio.run(mirror.delivery_mode()), "off")
                client.assert_not_called()


class TestRunGuardTests(unittest.TestCase):
    """A test run that inherits a live Cresset address and key must never reach it."""

    LIVE = {k: v for k, v in ENV.items() if k != "MERCURY_CRESSET_ALLOW_UNDER_TEST"}

    def test_a_test_run_stays_off_without_the_opt_in(self):
        with patch.dict(os.environ, self.LIVE, clear=True):
            self.assertEqual(asyncio.run(mirror.delivery_mode()), "off")

    def test_the_opt_in_turns_it_back_on(self):
        with patch.dict(os.environ, ENV, clear=True), patch.object(mirror.alert_settings, "saved_mode", return_value="both"):
            self.assertEqual(asyncio.run(mirror.delivery_mode()), "both")

    def test_nothing_is_posted_to_a_live_address_from_a_test_run(self):
        with patch.dict(os.environ, self.LIVE, clear=True),                 patch.object(mirror.httpx, "AsyncClient", side_effect=AssertionError("network used")) as client:
            asyncio.run(mirror.ask_proposal("ab12", 1, "Approve the new rule?"))
            asyncio.run(mirror.send_digest("Daily digest", "<p>hi</p>", "2026-11-22"))
            client.assert_not_called()


class VerifyCallbackTests(unittest.TestCase):
    def setUp(self):
        patcher = patch.dict(os.environ, ENV, clear=True)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.body = b'{"external_ref":"mercury:ab12:p3","outcome":"answered","option_id":"approve"}'

    def test_a_good_signature_passes(self):
        stamp = "1000"
        self.assertTrue(mirror.verify_callback(self.body, stamp, _sign(self.body, stamp), now=1005))

    def test_a_wrong_key_a_changed_body_and_a_missing_header_fail(self):
        stamp = "1000"
        self.assertFalse(mirror.verify_callback(self.body, stamp, _sign(self.body, stamp, "other"), now=1005))
        self.assertFalse(mirror.verify_callback(self.body + b" ", stamp, _sign(self.body, stamp), now=1005))
        self.assertFalse(mirror.verify_callback(self.body, None, _sign(self.body, stamp), now=1005))
        self.assertFalse(mirror.verify_callback(self.body, stamp, None, now=1005))
        self.assertFalse(mirror.verify_callback(self.body, "not-a-number", _sign(self.body, stamp), now=1005))

    def test_a_stale_timestamp_fails(self):
        stamp = "1000"
        self.assertFalse(mirror.verify_callback(self.body, stamp, _sign(self.body, stamp), now=1000 + 601))

    def test_no_key_set_fails(self):
        with patch.dict(os.environ, {}, clear=True):
            self.assertFalse(mirror.verify_callback(self.body, "1000", _sign(self.body, "1000"), now=1000))


class ParseTests(unittest.TestCase):
    def test_refs_round_trip(self):
        self.assertEqual(mirror.parse_ref(mirror.proposal_ref("ab12", 3)), ("ab12", "p3"))
        self.assertEqual(mirror.parse_ref(mirror.report_ref("ab12")), ("ab12", "report"))

    def test_foreign_refs_are_ignored(self):
        for ref in (None, "", "scout:ab:p1", "mercury:ab", "mercury::p1", "mercury:a:b:c"):
            self.assertIsNone(mirror.parse_ref(ref))

    def test_callback_body_must_be_a_json_object(self):
        self.assertIsNone(mirror.parse_callback_body(b"nope"))
        self.assertIsNone(mirror.parse_callback_body(b"[1]"))
        self.assertEqual(mirror.parse_callback_body(b'{"a":1}'), {"a": 1})


class HtmlToTextTests(unittest.TestCase):
    def test_keeps_the_words_and_drops_markup_and_styles(self):
        html = "<html><head><style>p{color:red}</style></head><body><h1>Digest</h1><p>Held <b>3</b> messages.</p><ul><li>one</li><li>two</li></ul></body></html>"
        text = mirror.html_to_text(html)
        self.assertEqual(text.splitlines(), ["Digest", "Held 3 messages.", "one", "two"])


class SendTests(unittest.TestCase):
    def setUp(self):
        patcher = patch.dict(os.environ, ENV, clear=True)
        patcher.start()
        self.addCleanup(patcher.stop)
        pin = patch.object(mirror.alert_settings, "saved_mode", return_value="both")
        pin.start()
        self.addCleanup(pin.stop)

    def test_a_proposal_becomes_a_decision_with_a_callback(self):
        with patch.object(mirror, "_post", AsyncMock(return_value={"id": "d1"})) as post:
            asyncio.run(mirror.ask_proposal("ab12", 3, "Mercury: standing change proposed"))
        path, payload = post.await_args.args
        self.assertEqual(path, "/v1/decisions")
        self.assertEqual(payload["external_ref"], "mercury:ab12:p3")
        self.assertEqual([o["id"] for o in payload["options"]], ["approve", "discard"])
        self.assertEqual(payload["callback_url"], mirror.DEFAULT_CALLBACK_URL)
        self.assertEqual(payload["project"], "mercury")

    def test_a_report_offers_five_buttons_and_relabels_deliver(self):
        with patch.object(mirror, "_post", AsyncMock(return_value={})) as post:
            asyncio.run(mirror.ask_report("ab12", "report", False, mode="both"))
            asyncio.run(mirror.ask_report("ab12", "report", True, mode="both"))
        first, second = (call.args[1] for call in post.await_args_list)
        self.assertEqual([o["id"] for o in first["options"]], ["unsubscribe", "soft", "hard", "deliver", "dismiss"])
        self.assertEqual(first["options"][3]["label"], "Deliver + whitelist")
        self.assertEqual(second["options"][3]["label"], "Whitelist")
        self.assertEqual(first["external_ref"], "mercury:ab12:report")

    def test_the_digest_becomes_a_digest_event_with_a_dated_fingerprint(self):
        with patch.object(mirror, "_post", AsyncMock(return_value={})) as post:
            asyncio.run(mirror.send_digest("Mercury daily digest - November 19, 2026", "<p>All quiet.</p>", "2026-11-19"))
        path, payload = post.await_args.args
        self.assertEqual(path, "/v1/events")
        self.assertEqual(payload["kind"], "digest")
        self.assertEqual(payload["fingerprint"], "mercury-digest-2026-11-19")
        self.assertEqual(payload["body"], "All quiet.")
        self.assertLessEqual(len(payload["title"]), 120)

    def test_nothing_is_sent_when_off(self):
        with patch.dict(os.environ, {}, clear=True), patch.object(mirror, "_post", AsyncMock()) as post:
            asyncio.run(mirror.ask_proposal("ab12", 1, "x"))
            asyncio.run(mirror.ask_report("ab12", "x", False, mode="off"))
            asyncio.run(mirror.send_digest("s", "<p>x</p>", "2026-11-19"))
        post.assert_not_awaited()

    def test_a_failed_post_is_logged_and_never_raised(self):
        class Boom:
            def __init__(self, *a, **k):
                pass

            async def __aenter__(self):
                return self

            async def __aexit__(self, *a):
                return False

            async def post(self, *a, **k):
                raise OSError("down")

        with patch.object(mirror.httpx, "AsyncClient", Boom), patch.object(mirror.event_log, "log_event") as log:
            self.assertFalse(asyncio.run(mirror._post("/v1/events", {})))
        self.assertEqual(log.call_args.args[1]["event"], "cresset_mirror_failed")

    def test_the_key_goes_only_in_the_authorization_header(self):
        seen = {}

        class Fake:
            def __init__(self, *a, **k):
                pass

            async def __aenter__(self):
                return self

            async def __aexit__(self, *a):
                return False

            async def post(self, url, json=None, headers=None):
                seen.update(url=url, json=json, headers=headers)
                return SimpleNamespace(raise_for_status=lambda: None, json=lambda: {"ok": True})

        with patch.object(mirror.httpx, "AsyncClient", Fake):
            asyncio.run(mirror._post("/v1/events", {"title": "t"}))
        self.assertEqual(seen["url"], "https://cresset.example.com/v1/events")
        self.assertEqual(seen["headers"]["Authorization"], f"Bearer {SOURCE_KEY}")
        self.assertNotIn(SOURCE_KEY, json.dumps(seen["json"]))


NO_OP_PROPOSAL = {
    "question": None, "reply": None, "caveat": None, "action": None,
    "changes": [{"kind": "sender_list", "list": "whitelist", "selector": "a@b.example"}],
}


class ApplyCressetAnswerTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = Path(__file__).parent / ".test-cresset-answer-data"
        shutil.rmtree(self.temp_dir, ignore_errors=True)
        self.temp_dir.mkdir()
        self.store = ApprovalStore(self.temp_dir / "approvals.json")
        self.finalize = AsyncMock()
        self.decide = AsyncMock(return_value=("Done.", None))
        self.telegram = TelegramApprovals(
            self.store,
            advance=AsyncMock(return_value=dict(NO_OP_PROPOSAL)),
            finalize=self.finalize,
            execute_action=AsyncMock(return_value=("ok", None)),
            execute_message_decision=self.decide,
        )
        self.telegram._send = AsyncMock(return_value=1)
        self.telegram._answer_callback = AsyncMock()

    def tearDown(self):
        shutil.rmtree(self.temp_dir, ignore_errors=True)

    def _proposal(self):
        with patch.object(mirror, "ask_proposal", AsyncMock()) as ask:
            brief_id, _, _ = asyncio.run(self.telegram.propose_new("Whitelist them", "From: a@b.example"))
        return brief_id, ask.await_args.args

    def test_a_proposal_mirrors_with_its_history_length(self):
        brief_id, args = self._proposal()
        self.assertEqual(args[0], brief_id)
        self.assertEqual(args[1], len(self.store.get_brief(brief_id)["history"]))
        self.assertIn("standing change", args[2])

    def test_cresset_mode_keeps_the_telegram_approval_buttons(self):
        async def post(path, payload):
            self.telegram._send.assert_awaited_once()
            return True

        with (
            patch.object(mirror, "delivery_mode", AsyncMock(return_value="cresset")),
            patch.object(mirror, "_post", AsyncMock(side_effect=post)) as send_copy,
        ):
            brief_id, _, _ = asyncio.run(self.telegram.propose_new("Whitelist them", "From: a@b.example"))

        buttons = self.telegram._send.await_args.kwargs["keyboard"]["inline_keyboard"][0]
        self.assertEqual([button["callback_data"] for button in buttons], [f"approve:{brief_id}", f"discard:{brief_id}"])
        send_copy.assert_awaited_once()

    def _send_report_with_hub(self, mode, outcome=204):
        calls = []

        def handle(request):
            if request.method == "GET":
                return mirror.httpx.Response(200, json={"sender": "mercury", "mode": mode})
            self.assertEqual(request.url.path, "/v1/decisions")
            calls.append("cresset")
            if isinstance(outcome, Exception):
                raise outcome
            return mirror.httpx.Response(outcome)

        async def send(text, keyboard):
            calls.append("telegram")
            self.assertEqual(len(keyboard["inline_keyboard"]), 3)
            return 1

        self.telegram._send.side_effect = send
        with (
            patch.dict(os.environ, ENV, clear=True),
            patch.object(mirror, "_sender_config", None),
            patch.object(mirror.event_log, "log_event"),
            _mock_client(handle),
        ):
            asyncio.run(self.telegram.send_trackable_report("Mercury report", "ctx", {}))
        return calls

    def test_the_registry_selects_report_delivery_order(self):
        for mode, expected in (
            ("direct", ["telegram"]),
            ("both", ["telegram", "cresset"]),
            ("cresset", ["cresset"]),
        ):
            with self.subTest(mode=mode):
                self.assertEqual(self._send_report_with_hub(mode), expected)

    def test_cresset_first_reports_fall_back_to_telegram_on_failure(self):
        for outcome in (401, 403, 429, 503, mirror.httpx.ConnectError("down"), mirror.httpx.ReadTimeout("timeout")):
            with self.subTest(outcome=outcome):
                self.assertEqual(self._send_report_with_hub("cresset", outcome), ["cresset", "telegram"])

    def test_approve_in_cresset_runs_the_same_approval(self):
        brief_id, args = self._proposal()
        result = asyncio.run(self.telegram.apply_cresset_answer(brief_id, f"p{args[1]}", "approve"))
        self.assertEqual(result, "applied")
        self.finalize.assert_awaited_once()
        self.assertEqual(self.store.get_brief(brief_id)["status"], "resolved")

    def test_discard_in_cresset_resolves_without_finalizing(self):
        brief_id, args = self._proposal()
        result = asyncio.run(self.telegram.apply_cresset_answer(brief_id, f"p{args[1]}", "discard"))
        self.assertEqual(result, "applied")
        self.finalize.assert_not_awaited()

    def test_an_answer_to_an_older_proposal_is_stale(self):
        brief_id, args = self._proposal()
        self.store.append_turn(brief_id, "user", "actually make it a blacklist")
        result = asyncio.run(self.telegram.apply_cresset_answer(brief_id, f"p{args[1]}", "approve"))
        self.assertEqual(result, "stale")
        self.finalize.assert_not_awaited()

    def test_a_second_answer_after_telegram_already_handled_it_does_nothing(self):
        brief_id, args = self._proposal()
        asyncio.run(self.telegram.resolve_proposal(brief_id, "approve"))
        self.finalize.reset_mock()
        result = asyncio.run(self.telegram.apply_cresset_answer(brief_id, f"p{args[1]}", "approve"))
        self.assertEqual(result, "unavailable")
        self.finalize.assert_not_awaited()

    def test_a_report_answer_runs_the_message_decision_once(self):
        with patch.object(mirror, "ask_report", AsyncMock()):
            asyncio.run(self.telegram.send_trackable_report("Mercury report", "ctx", {"already_delivered": False}))
        brief_id = next(iter(self.store._load()["briefs"]))
        first = asyncio.run(self.telegram.apply_cresset_answer(brief_id, "report", "soft"))
        second = asyncio.run(self.telegram.apply_cresset_answer(brief_id, "report", "hard"))
        self.assertEqual((first, second), ("applied", "unavailable"))
        self.decide.assert_awaited_once()
        self.assertEqual(self.decide.await_args.args[0], "soft")

    def test_an_unknown_option_or_tag_is_unavailable(self):
        brief_id, args = self._proposal()
        self.assertEqual(asyncio.run(self.telegram.apply_cresset_answer(brief_id, "report", "nuke")), "unavailable")
        self.assertEqual(asyncio.run(self.telegram.apply_cresset_answer(brief_id, f"p{args[1]}", "maybe")), "unavailable")
        self.assertEqual(asyncio.run(self.telegram.apply_cresset_answer("nope", "p1", "approve")), "unavailable")


if __name__ == "__main__":
    unittest.main()
