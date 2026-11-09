import asyncio
import os
import unittest
from unittest.mock import patch

import httpx

from providers.classifier import HttpInjectionClassifier, get_classifier

ANSWER = {"label": "SAFE", "score": 0.01}


class Hosts:
    """Maps a host to a reply, or None for a refused connection. Records each
    request's host, path, and Authorization header."""

    def __init__(self, replies):
        self.replies = replies
        self.calls = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.calls.append((request.url.host, request.url.path, request.headers.get("authorization")))
        reply = self.replies[request.url.host]
        if reply is None:
            raise httpx.ConnectError("refused", request=request)
        if isinstance(reply, Exception):
            raise reply
        return reply if isinstance(reply, httpx.Response) else httpx.Response(200, json=reply)


class Clock:
    now = 0.0

    def __call__(self):
        return self.now


def gateway(host):
    return (f"http://{host}:8470/app/prompt-guard/classify", {"Authorization": "Bearer k"})


def direct(host):
    return (f"http://{host}:8009/classify", {})


def make(replies, endpoints, clock=None):
    hosts = Hosts(replies)
    classifier = HttpInjectionClassifier(
        endpoints, transport=httpx.MockTransport(hosts), clock=clock or Clock())
    return classifier, hosts


def run(classifier):
    return asyncio.run(classifier.check("hello"))


class FailoverTests(unittest.TestCase):
    def test_the_first_endpoint_answers_and_nothing_else_is_called(self):
        classifier, hosts = make({"bilbo": ANSWER, "eru": ANSWER}, [gateway("bilbo"), gateway("eru")])
        self.assertEqual(run(classifier), ANSWER)
        self.assertEqual([c[0] for c in hosts.calls], ["bilbo"])
        self.assertEqual(hosts.calls[0][2], "Bearer k")

    def test_a_refused_connection_moves_to_the_next_gateway(self):
        classifier, hosts = make({"bilbo": None, "eru": ANSWER}, [gateway("bilbo"), gateway("eru")])
        self.assertEqual(run(classifier), ANSWER)
        self.assertEqual([c[0] for c in hosts.calls], ["bilbo", "eru"])

    def test_a_timeout_moves_to_the_next_endpoint(self):
        timeout = httpx.ReadTimeout("slow")
        classifier, hosts = make({"bilbo": timeout, "eru": ANSWER}, [gateway("bilbo"), gateway("eru")])
        self.assertEqual(run(classifier), ANSWER)

    def test_a_5xx_moves_to_the_next_endpoint(self):
        classifier, hosts = make({"bilbo": httpx.Response(503), "eru": ANSWER}, [gateway("bilbo"), gateway("eru")])
        self.assertEqual(run(classifier), ANSWER)

    def test_a_4xx_from_a_direct_endpoint_is_the_callers_fault(self):
        classifier, hosts = make({"bilbo": httpx.Response(422), "eru": ANSWER}, [direct("bilbo"), direct("eru")])
        with self.assertRaises(httpx.HTTPStatusError):
            run(classifier)
        self.assertEqual([c[0] for c in hosts.calls], ["bilbo"])

    def test_a_gateway_that_lacks_the_service_or_the_key_falls_through_to_direct(self):
        for status in (401, 403, 404):
            classifier, hosts = make({"bilbo": httpx.Response(status), "eru": ANSWER}, [gateway("bilbo"), direct("eru")])
            self.assertEqual(run(classifier), ANSWER)
            self.assertEqual([c[0] for c in hosts.calls], ["bilbo", "eru"])

    def test_every_endpoint_failing_raises_the_last_error(self):
        classifier, hosts = make({"bilbo": None, "eru": None}, [gateway("bilbo"), direct("eru")])
        with self.assertRaises(httpx.ConnectError):
            run(classifier)
        self.assertEqual(len(hosts.calls), 2)

    def test_a_dead_endpoint_is_skipped_for_a_while_then_tried_again(self):
        clock = Clock()
        classifier, hosts = make({"bilbo": None, "eru": ANSWER}, [gateway("bilbo"), gateway("eru")], clock)
        run(classifier)
        run(classifier)
        self.assertEqual([c[0] for c in hosts.calls], ["bilbo", "eru", "eru"])
        clock.now = 31.0
        run(classifier)
        self.assertEqual([c[0] for c in hosts.calls][-2:], ["bilbo", "eru"])

    def test_when_every_endpoint_is_marked_down_they_are_all_tried_anyway(self):
        clock = Clock()
        classifier, hosts = make({"bilbo": None, "eru": None}, [gateway("bilbo"), gateway("eru")], clock)
        for _ in range(2):
            with self.assertRaises(httpx.ConnectError):
                run(classifier)
        self.assertEqual(len(hosts.calls), 4)


class ConfigurationTests(unittest.TestCase):
    def _classifier(self, **env):
        base = {"PROMPT_INJECTION_CLASSIFIER_URL": "http://bilbo:8009/classify"}
        with patch.dict(os.environ, {**base, **env}, clear=True):
            return get_classifier()

    def test_without_gateway_settings_only_the_direct_url_is_used(self):
        self.assertEqual(self._classifier()._endpoints, [("http://bilbo:8009/classify", {})])

    def test_gateways_come_first_in_order_and_the_direct_url_is_last(self):
        classifier = self._classifier(
            COGITATOR_GATEWAY_URLS="http://bilbo:8470, http://eru:8470/ ,http://bazzite:8470",
            COGITATOR_MERCURY_KEY="k")
        self.assertEqual([url for url, _ in classifier._endpoints], [
            "http://bilbo:8470/app/prompt-guard/classify",
            "http://eru:8470/app/prompt-guard/classify",
            "http://bazzite:8470/app/prompt-guard/classify",
            "http://bilbo:8009/classify",
        ])
        self.assertEqual(classifier._endpoints[0][1], {"Authorization": "Bearer k"})
        self.assertEqual(classifier._endpoints[-1][1], {})

    def test_gateways_are_left_out_when_the_key_is_missing(self):
        classifier = self._classifier(COGITATOR_GATEWAY_URLS="http://bilbo:8470")
        self.assertEqual(len(classifier._endpoints), 1)

    def test_the_direct_setting_may_list_several_urls(self):
        classifier = self._classifier(PROMPT_INJECTION_CLASSIFIER_URL="http://a:1/classify,http://b:1/classify")
        self.assertEqual([url for url, _ in classifier._endpoints], ["http://a:1/classify", "http://b:1/classify"])


if __name__ == "__main__":
    unittest.main()
