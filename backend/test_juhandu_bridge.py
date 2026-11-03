import asyncio
import json
import os
import tempfile
import time
import unittest
from unittest.mock import patch

import httpx

import juhandu_bridge as bridge

RAW = (
    "Message-ID: <issue-7@news.example.com>\r\n"
    "From: The Weekly <hello@news.example.com>\r\n"
    "Subject: Issue 7\r\n"
    "Date: Mon, 02 Nov 2026 12:00:00 +0000\r\n"
    "List-ID: <weekly.news.example.com>\r\n"
    "List-Unsubscribe: <https://news.example.com/u/1>, <mailto:u@news.example.com>, <http://insecure.example/u>\r\n"
    "MIME-Version: 1.0\r\n"
    "Content-Type: multipart/alternative; boundary=b\r\n"
    "\r\n"
    "--b\r\nContent-Type: text/plain; charset=utf-8\r\n\r\nHello text\r\n"
    "--b\r\nContent-Type: text/html; charset=utf-8\r\n\r\n<p>Hello html</p>\r\n"
    "--b--\r\n"
)


class BuildPayloadTests(unittest.TestCase):
    def test_a_newsletter_becomes_the_door_contract(self):
        payload = bridge.build_payload(RAW, "LEGIT", "NEWSLETTER")

        self.assertEqual(payload["messageId"], "<issue-7@news.example.com>")
        self.assertEqual(payload["from"], {"name": "The Weekly", "address": "hello@news.example.com"})
        self.assertEqual(payload["subject"], "Issue 7")
        self.assertEqual(payload["date"], "2026-11-02T12:00:00+00:00")
        self.assertEqual(payload["listId"], "<weekly.news.example.com>")
        self.assertEqual(
            payload["listUnsubscribe"],
            ["https://news.example.com/u/1", "mailto:u@news.example.com"],
        )
        self.assertIn("Hello text", payload["text"])
        self.assertIn("Hello html", payload["html"])
        self.assertEqual((payload["category"], payload["verdict"]), ("NEWSLETTER", "LEGIT"))

    def test_a_plain_text_message_has_no_html_key(self):
        raw = "Message-ID: <a@b.c>\r\nFrom: a@b.c\r\nSubject: s\r\n\r\nJust text"
        payload = bridge.build_payload(raw, "LEGIT", "NEWSLETTER")

        self.assertIn("Just text", payload["text"])
        self.assertNotIn("html", payload)
        self.assertNotIn("date", payload)
        self.assertNotIn("listId", payload)
        self.assertNotIn("listUnsubscribe", payload)

    def test_a_message_that_cannot_be_an_item_is_skipped(self):
        no_id = "From: a@b.c\r\nSubject: s\r\n\r\nbody"
        no_sender = "Message-ID: <a@b.c>\r\nSubject: s\r\n\r\nbody"
        no_body = "Message-ID: <a@b.c>\r\nFrom: a@b.c\r\nSubject: s\r\n\r\n"

        for raw in (None, "", no_id, no_sender, no_body):
            self.assertIsNone(bridge.build_payload(raw, "LEGIT", "NEWSLETTER"))

    def test_an_oversize_payload_is_cut_to_the_door_limit(self):
        big = "x" * (3 * 1024 * 1024)
        raw = (
            "Message-ID: <big@b.c>\r\nFrom: a@b.c\r\nSubject: s\r\n"
            "MIME-Version: 1.0\r\nContent-Type: multipart/alternative; boundary=b\r\n\r\n"
            f"--b\r\nContent-Type: text/plain\r\n\r\n{big}\r\n"
            f"--b\r\nContent-Type: text/html\r\n\r\n<p>{big}</p>\r\n--b--\r\n"
        )
        payload = bridge.build_payload(raw, "LEGIT", "NEWSLETTER")

        self.assertLessEqual(bridge._size(payload), bridge.MAX_BODY_BYTES)
        self.assertTrue(payload["text"])
        self.assertNotIn("html", payload)


class Door:
    """A stand-in for Juhandu's mail door that answers with queued statuses."""

    def __init__(self, *statuses):
        self.statuses = list(statuses)
        self.requests = []

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        status = self.statuses.pop(0) if len(self.statuses) > 1 else self.statuses[0]
        if isinstance(status, Exception):
            raise status
        return httpx.Response(status, json={})


class BridgeTestCase(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        for name, value in (
            ("PUSH_ENABLED", True),
            ("JUHANDU_URL", "http://juhandu.test/inbound/mail"),
            ("JUHANDU_TOKEN", "secret-token"),
            ("ACCESS_ID", ""),
            ("ACCESS_SECRET", ""),
            ("OUTBOX_PATH", os.path.join(self.directory.name, "outbox.json")),
        ):
            patcher = patch.object(bridge, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)

    def door(self, *statuses):
        door = Door(*statuses)
        patcher = patch.object(bridge, "_transport", httpx.MockTransport(door.handler))
        patcher.start()
        self.addCleanup(patcher.stop)
        return door

    async def settle(self):
        while bridge._tasks:
            await asyncio.gather(*list(bridge._tasks))


class SubmitTests(BridgeTestCase):
    async def test_a_newsletter_is_posted_with_the_bearer_token(self):
        door = self.door(201)

        self.assertTrue(bridge.submit(RAW, "LEGIT", "NEWSLETTER"))
        await self.settle()

        request = door.requests[0]
        self.assertEqual(request.headers["authorization"], "Bearer secret-token")
        self.assertNotIn("cf-access-client-id", request.headers)
        self.assertEqual(json.loads(request.content)["messageId"], "<issue-7@news.example.com>")
        self.assertEqual(bridge._load_outbox(), [])

    async def test_access_headers_are_sent_when_configured(self):
        door = self.door(200)

        with patch.object(bridge, "ACCESS_ID", "id.access"), patch.object(bridge, "ACCESS_SECRET", "s3"):
            bridge.submit(RAW, "LEGIT", "NEWSLETTER")
            await self.settle()

        self.assertEqual(door.requests[0].headers["cf-access-client-id"], "id.access")
        self.assertEqual(door.requests[0].headers["cf-access-client-secret"], "s3")

    async def test_the_host_header_is_overridden_only_when_configured(self):
        door = self.door(201)

        bridge.submit(RAW, "LEGIT", "NEWSLETTER")
        await self.settle()
        with patch.object(bridge, "HOST_HEADER", "127.0.0.1:8795"):
            bridge.submit(RAW, "LEGIT", "NEWSLETTER")
            await self.settle()

        self.assertEqual(door.requests[0].headers["host"], "juhandu.test")
        self.assertEqual(door.requests[1].headers["host"], "127.0.0.1:8795")

    async def test_other_categories_and_a_disabled_push_send_nothing(self):
        door = self.door(201)

        self.assertFalse(bridge.submit(RAW, "LEGIT", "PROMOTIONAL"))
        with patch.object(bridge, "PUSH_ENABLED", False):
            self.assertFalse(bridge.submit(RAW, "LEGIT", "NEWSLETTER"))
        with patch.object(bridge, "JUHANDU_TOKEN", ""):
            self.assertFalse(bridge.submit(RAW, "LEGIT", "NEWSLETTER"))
        await self.settle()

        self.assertEqual(door.requests, [])

    async def test_the_category_set_is_configurable(self):
        door = self.door(201)

        with patch.object(bridge, "CATEGORIES", {"NEWSLETTER", "PROMOTIONAL"}):
            self.assertTrue(bridge.submit(RAW, "LEGIT", "promotional"))
            await self.settle()

        self.assertEqual(len(door.requests), 1)

    async def test_a_failed_push_is_queued_and_never_raises(self):
        for failure in (503, 401, 404, 429, httpx.ConnectError("down")):
            with self.subTest(failure=repr(failure)):
                self.door(failure)
                bridge._save_outbox([])

                self.assertTrue(bridge.submit(RAW, "LEGIT", "NEWSLETTER"))
                await self.settle()

                queued = bridge._load_outbox()
                self.assertEqual(len(queued), 1)
                self.assertEqual(queued[0]["attempts"], 1)
                self.assertGreater(queued[0]["next_at"], time.time())

    async def test_a_refused_body_is_not_queued(self):
        for status in (400, 413):
            with self.subTest(status=status):
                self.door(status)
                bridge._save_outbox([])

                bridge.submit(RAW, "LEGIT", "NEWSLETTER")
                await self.settle()

                self.assertEqual(bridge._load_outbox(), [])

    async def test_a_message_that_cannot_be_built_starts_no_push(self):
        door = self.door(201)

        self.assertFalse(bridge.submit("not a message", "LEGIT", "NEWSLETTER"))
        self.assertFalse(bridge.submit(None, "LEGIT", "NEWSLETTER"))
        await self.settle()

        self.assertEqual(door.requests, [])

    async def test_an_already_held_message_counts_as_sent(self):
        self.door(200)

        bridge.submit(RAW, "LEGIT", "NEWSLETTER")
        await self.settle()

        self.assertEqual(bridge._load_outbox(), [])


class RetryTests(BridgeTestCase):
    def queue(self, **overrides):
        entry = {
            "id": "one",
            "payload": bridge.build_payload(RAW, "LEGIT", "NEWSLETTER"),
            "first_at": time.time() - 100,
            "attempts": 1,
            "next_at": time.time() - 1,
        }
        entry.update(overrides)
        bridge._save_outbox([entry])

    async def test_a_due_entry_is_sent_and_removed(self):
        door = self.door(201)
        self.queue()

        await bridge.retry_due()

        self.assertEqual(len(door.requests), 1)
        self.assertEqual(bridge._load_outbox(), [])

    async def test_an_entry_that_is_not_due_waits(self):
        door = self.door(201)
        self.queue(next_at=time.time() + 600)

        await bridge.retry_due()

        self.assertEqual(door.requests, [])
        self.assertEqual(len(bridge._load_outbox()), 1)

    async def test_a_failed_retry_backs_off(self):
        self.door(503)
        self.queue()

        await bridge.retry_due()

        entry = bridge._load_outbox()[0]
        self.assertEqual(entry["attempts"], 2)
        self.assertGreater(entry["next_at"], time.time() + 60)

    async def test_the_backoff_gap_stops_growing(self):
        self.door(503)
        self.queue(attempts=50)

        await bridge.retry_due()

        entry = bridge._load_outbox()[0]
        self.assertLessEqual(entry["next_at"], time.time() + bridge.RETRY_GAPS_SECONDS[-1] + 1)

    async def test_an_entry_older_than_the_window_is_dropped(self):
        door = self.door(503)
        self.queue(first_at=time.time() - bridge.RETRY_WINDOW_SECONDS - 10)

        await bridge.retry_due()

        self.assertEqual(len(door.requests), 1)
        self.assertEqual(bridge._load_outbox(), [])

    async def test_a_refused_body_is_dropped_on_retry(self):
        self.door(400)
        self.queue()

        await bridge.retry_due()

        self.assertEqual(bridge._load_outbox(), [])

    async def test_an_entry_queued_during_a_send_is_kept(self):
        self.queue()

        async def slow_send(payload):
            bridge._queue({"messageId": "<other@b.c>"}, time.time(), 1)
            return "sent", "inserted"

        with patch.object(bridge, "send", slow_send):
            await bridge.retry_due()

        remaining = bridge._load_outbox()
        self.assertEqual([entry["payload"]["messageId"] for entry in remaining], ["<other@b.c>"])

    async def test_the_outbox_is_capped(self):
        bridge._save_outbox([
            {"id": str(i), "payload": {}, "first_at": 0, "attempts": 1, "next_at": 0}
            for i in range(bridge.OUTBOX_LIMIT)
        ])

        bridge._queue({"messageId": "<new@b.c>"}, time.time(), 1)

        entries = bridge._load_outbox()
        self.assertEqual(len(entries), bridge.OUTBOX_LIMIT)
        self.assertEqual(entries[-1]["payload"]["messageId"], "<new@b.c>")

    async def test_a_missing_or_damaged_outbox_reads_as_empty(self):
        self.assertEqual(bridge._load_outbox(), [])
        with open(bridge.OUTBOX_PATH, "w") as handle:
            handle.write("{broken")
        self.assertEqual(bridge._load_outbox(), [])


if __name__ == "__main__":
    unittest.main()
