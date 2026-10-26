import asyncio
import sys
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

import httpx

sys.path.insert(0, str(Path(__file__).parent))

from providers import judge as judge_module  # noqa: E402
from providers.judge import HttpAgentGatewayJudge, JudgeNoAnswer, JudgeReportedError  # noqa: E402

URL = "http://gateway.test/agent"
JOBS = "http://gateway.test/agent/jobs"


def reply(status, body=None, url=JOBS):
    return httpx.Response(status, json=body, request=httpx.Request("GET", url))


class FakeClient:
    """Stands in for httpx.AsyncClient: answers from a script and records calls."""

    posts = []
    gets = []
    post_reply = None
    get_replies = []

    def __init__(self, *args, **kwargs):
        pass

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def post(self, url, headers=None, json=None):
        FakeClient.posts.append((url, dict(headers or {}), json))
        return FakeClient.post_reply

    async def get(self, url, headers=None):
        FakeClient.gets.append((url, dict(headers or {})))
        item = FakeClient.get_replies.pop(0)
        if isinstance(item, Exception):
            raise item
        return item


def judge(**kwargs):
    return HttpAgentGatewayJudge(URL, "s3cret", poll_interval=0, **kwargs)


class AskLongTests(unittest.TestCase):
    def setUp(self):
        FakeClient.posts = []
        FakeClient.gets = []
        FakeClient.post_reply = reply(202, {"job": "abc"})
        FakeClient.get_replies = []
        for patcher in (
            patch.object(judge_module.httpx, "AsyncClient", FakeClient),
            patch.object(judge_module.asyncio, "sleep", AsyncMock()),
        ):
            patcher.start()
            self.addCleanup(patcher.stop)

    def run_long(self, **kwargs):
        return asyncio.run(judge(**kwargs).ask_long("do the thing"))

    def test_polls_until_the_job_is_done(self):
        FakeClient.get_replies = [
            reply(200, {"status": "running"}),
            reply(200, {"status": "running"}),
            reply(200, {"status": "done", "response": "all good"}),
        ]

        self.assertEqual(self.run_long(), "all good")
        url, headers, body = FakeClient.posts[0]
        self.assertEqual(url, JOBS)
        self.assertEqual(headers["X-Gateway-Secret"], "s3cret")
        self.assertEqual(body, {"prompt": "do the thing"})
        self.assertEqual([g[0] for g in FakeClient.gets], [JOBS + "/abc"] * 3)

    def test_a_gateway_without_jobs_gets_the_blocking_call(self):
        FakeClient.post_reply = reply(404)
        blocking = AsyncMock(return_value="old style")

        with patch.object(HttpAgentGatewayJudge, "ask", blocking):
            self.assertEqual(self.run_long(), "old style")

        blocking.assert_awaited_once_with("do the thing")

    def test_a_refused_submit_is_a_real_failure(self):
        FakeClient.post_reply = reply(403)

        with self.assertRaises(httpx.HTTPStatusError):
            self.run_long()

    def test_a_reported_error_raises(self):
        FakeClient.get_replies = [
            reply(200, {"status": "error", "error": "agent exited 1", "detail": "boom"}),
        ]

        with self.assertRaisesRegex(JudgeReportedError, "boom"):
            self.run_long()

    def test_a_job_timeout_is_no_answer(self):
        FakeClient.get_replies = [reply(200, {"status": "timeout"})]

        with self.assertRaises(JudgeNoAnswer):
            self.run_long()

    def test_a_forgotten_job_is_no_answer(self):
        FakeClient.get_replies = [reply(200, {"status": "running"}), reply(404)]

        with self.assertRaises(JudgeNoAnswer):
            self.run_long()

    def test_running_out_of_wait_is_no_answer(self):
        with self.assertRaises(JudgeNoAnswer):
            self.run_long(max_wait=0)

    def test_a_gateway_that_goes_quiet_for_good_is_no_answer(self):
        FakeClient.get_replies = [httpx.ConnectError("down")] * 3

        with self.assertRaises(JudgeNoAnswer):
            self.run_long(max_poll_failures=3)

    def test_a_gateway_that_restarts_quickly_is_waited_out(self):
        FakeClient.get_replies = [
            httpx.ConnectError("restarting"),
            httpx.ConnectError("restarting"),
            reply(200, {"status": "done", "response": "back"}),
        ]

        self.assertEqual(self.run_long(max_poll_failures=3), "back")


if __name__ == "__main__":
    unittest.main()
