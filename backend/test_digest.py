import asyncio
import unittest
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

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


if __name__ == "__main__":
    unittest.main()
