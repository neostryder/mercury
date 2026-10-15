import asyncio
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).parent))

from providers import structured_judge as sj  # noqa: E402

GATEWAY = "http://gateway.test/v1/systemone"
DIRECT = "https://direct.test/v1/systemone"
OK = {"model": "jev-1.14.0", "answers": {}}


class FakeResponse:
    def __init__(self, status, body=None, headers=None):
        self.status_code = status
        self._body = body if body is not None else {}
        self.headers = headers or {}

    def json(self):
        return self._body

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(f"status {self.status_code}")


class FakeClient:
    """Stands in for httpx.AsyncClient; answers each URL from a script and records the calls."""

    calls = []
    script = {}

    def __init__(self, *args, **kwargs):
        pass

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def post(self, url, headers=None, json=None):
        FakeClient.calls.append((url, dict(headers or {})))
        reply = FakeClient.script[url]
        if isinstance(reply, Exception):
            raise reply
        return reply


def run(judge, payload=None):
    return asyncio.run(judge._post(payload or {"model": "m", "state": {}, "questions": {}}))


class GatewayJudgeTests(unittest.TestCase):
    def setUp(self):
        FakeClient.calls = []
        FakeClient.script = {}
        patcher = patch.object(sj.httpx, "AsyncClient", FakeClient)
        patcher.start()
        self.addCleanup(patcher.stop)

    def judge(self, key="direct-key"):
        return sj.SystemOneStructuredJudge(key, endpoint=DIRECT, gateway_url=GATEWAY, gateway_token="gw-token")

    def test_gateway_answer_is_used_with_token_consumer_and_tier(self):
        FakeClient.script = {GATEWAY: FakeResponse(200, OK)}
        self.assertEqual(run(self.judge()), OK)
        url, headers = FakeClient.calls[0]
        self.assertEqual(url, GATEWAY)
        self.assertEqual(headers["Authorization"], "Bearer gw-token")
        self.assertEqual(headers["X-Augur-Consumer"], "mercury")
        self.assertEqual(headers["X-Augur-Data"], "internal")
        self.assertEqual(len(FakeClient.calls), 1)

    def test_unreachable_gateway_goes_direct(self):
        FakeClient.script = {GATEWAY: OSError("refused"), DIRECT: FakeResponse(200, OK)}
        self.assertEqual(run(self.judge()), OK)
        self.assertEqual([c[0] for c in FakeClient.calls], [GATEWAY, DIRECT])
        self.assertEqual(FakeClient.calls[1][1]["Authorization"], "Bearer direct-key")

    def test_gateway_own_503_goes_direct(self):
        FakeClient.script = {GATEWAY: FakeResponse(503, headers={"x-augur-gateway": "1"}), DIRECT: FakeResponse(200, OK)}
        self.assertEqual(run(self.judge()), OK)
        self.assertEqual([c[0] for c in FakeClient.calls], [GATEWAY, DIRECT])

    def test_refusal_never_goes_direct(self):
        FakeClient.script = {GATEWAY: FakeResponse(503, headers={"x-augur-gateway": "1", "x-augur-refused": "1"})}
        self.assertIsNone(run(self.judge()))
        self.assertEqual([c[0] for c in FakeClient.calls], [GATEWAY])

    def test_upstream_error_passes_through_without_direct_retry(self):
        FakeClient.script = {GATEWAY: FakeResponse(422)}
        self.assertIsNone(run(self.judge()))
        self.assertEqual([c[0] for c in FakeClient.calls], [GATEWAY])

    def test_no_direct_key_means_no_direct_fallback(self):
        FakeClient.script = {GATEWAY: OSError("refused")}
        self.assertIsNone(run(self.judge(key=None)))
        self.assertEqual([c[0] for c in FakeClient.calls], [GATEWAY])

    def test_without_gateway_settings_the_direct_path_is_unchanged(self):
        FakeClient.script = {DIRECT: FakeResponse(200, OK)}
        judge = sj.SystemOneStructuredJudge("direct-key", endpoint=DIRECT)
        self.assertEqual(run(judge), OK)
        self.assertEqual([c[0] for c in FakeClient.calls], [DIRECT])

    def test_factory_accepts_gateway_settings_alone(self):
        env = {"STRUCTURED_JUDGE_ENABLED": "true", "STRUCTURED_JUDGE_GATEWAY_URL": GATEWAY,
               "STRUCTURED_JUDGE_GATEWAY_TOKEN": "gw-token"}
        with patch.dict("os.environ", env, clear=True):
            self.assertIsNotNone(sj.get_structured_judge())
        with patch.dict("os.environ", {"STRUCTURED_JUDGE_ENABLED": "true"}, clear=True):
            self.assertIsNone(sj.get_structured_judge())


if __name__ == "__main__":
    unittest.main()
