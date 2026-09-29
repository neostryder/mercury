import asyncio
import os
import unittest
from unittest.mock import patch

import httpx

from providers.structured_judge import EndpointPool, get_laya_judge

PC = "http://pc:8010/v1/systemone"
BILBO = "http://bilbo:8010/v1/systemone"
ANSWER = {"answers": {"verdict": {"choice": "LEGIT"}}}


class FakeServers:
    """Each host maps to (load reply, post reply); None means unreachable."""

    def __init__(self, hosts):
        self.hosts = hosts
        self.posts = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        host = request.url.host
        load, post = self.hosts[host]
        if request.url.path == "/load":
            if load is None:
                raise httpx.ConnectError("refused", request=request)
            return load if isinstance(load, httpx.Response) else httpx.Response(200, json=load)
        self.posts.append(host)
        if post is None:
            raise httpx.ConnectError("refused", request=request)
        return post if isinstance(post, httpx.Response) else httpx.Response(200, json=post)


class Clock:
    def __init__(self):
        self.now = 0.0

    def __call__(self):
        return self.now


def pool(servers, clock=None):
    return EndpointPool([PC, BILBO], clock=clock or Clock(),
                        transport=httpx.MockTransport(servers))


def ask(p):
    return asyncio.run(p.post({"model": "laya"}, {}, 5.0))


READY = {"ready": True, "busy": False}


class EndpointPoolTests(unittest.TestCase):
    def test_the_first_healthy_server_answers(self):
        servers = FakeServers({"pc": (READY, ANSWER), "bilbo": (READY, ANSWER)})
        self.assertEqual(ask(pool(servers)), ANSWER)
        self.assertEqual(servers.posts, ["pc"])

    def test_an_unreachable_server_falls_through_and_is_skipped_for_a_while(self):
        servers = FakeServers({"pc": (None, None), "bilbo": (READY, ANSWER)})
        clock = Clock()
        p = pool(servers, clock)
        self.assertEqual(ask(p), ANSWER)
        servers.hosts["pc"] = (READY, ANSWER)
        clock.now = 29.0
        ask(p)
        self.assertEqual(servers.posts, ["bilbo", "bilbo"])
        clock.now = 31.0
        ask(p)
        self.assertEqual(servers.posts[-1], "pc")

    def test_a_busy_server_is_skipped_briefly(self):
        servers = FakeServers({"pc": ({"ready": True, "busy": True}, ANSWER),
                               "bilbo": (READY, ANSWER)})
        clock = Clock()
        p = pool(servers, clock)
        ask(p)
        servers.hosts["pc"] = (READY, ANSWER)
        clock.now = 6.0
        ask(p)
        self.assertEqual(servers.posts, ["bilbo", "pc"])

    def test_not_ready_and_503_fall_through(self):
        for pc in (({"ready": False}, ANSWER), (READY, httpx.Response(503))):
            with self.subTest(pc=pc):
                servers = FakeServers({"pc": pc, "bilbo": (READY, ANSWER)})
                self.assertEqual(ask(pool(servers)), ANSWER)

    def test_a_4xx_is_not_retried_elsewhere(self):
        servers = FakeServers({"pc": (READY, httpx.Response(422)), "bilbo": (READY, ANSWER)})
        self.assertIsNone(ask(pool(servers)))
        self.assertEqual(servers.posts, ["pc"])

    def test_a_server_without_load_is_used(self):
        servers = FakeServers({"pc": (httpx.Response(404), ANSWER), "bilbo": (READY, ANSWER)})
        self.assertEqual(ask(pool(servers)), ANSWER)
        self.assertEqual(servers.posts, ["pc"])

    def test_every_server_down_returns_none(self):
        servers = FakeServers({"pc": (None, None), "bilbo": (None, None)})
        self.assertIsNone(ask(pool(servers)))


class LayaUrlTests(unittest.TestCase):
    def test_a_comma_separated_list_keeps_its_order(self):
        with patch.dict(os.environ, {"LAYA_URL": f"{PC}, {BILBO}"}):
            judge = get_laya_judge()
        self.assertEqual(judge._pool.endpoints, [PC, BILBO])

    def test_a_single_url_still_works(self):
        with patch.dict(os.environ, {"LAYA_URL": BILBO}):
            self.assertEqual(get_laya_judge()._pool.endpoints, [BILBO])


if __name__ == "__main__":
    unittest.main()
