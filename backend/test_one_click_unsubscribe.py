import asyncio
import unittest
from unittest.mock import patch

import httpx

import one_click_unsubscribe as oc

FANDANGO_CONTEXT = (
    "Message\n"
    "From: Fandango Stream <fandangoathome@movies.fandango.com>\n"
    "Subject: Stream Free Movies\n"
    "\n"
    "Unsubscribe routes extracted from this message:\n"
    "List-Unsubscribe: <https://link.athome.fandango.com/oc/abc/123>,"
    "<mailto:unsubscribe_1@mx.sailthru.com>\n"
    "List-Unsubscribe-Post: List-Unsubscribe=One-Click\n"
    "Links that look like an unsubscribe or preference route:\n"
    "- Unsubscribe -> https://link.vudu.com/click/1/aHR0cHM\n"
    "\n"
    "<html>body</html>"
)


def run(coro):
    return asyncio.run(coro)


def patched_client(handler):
    real = httpx.AsyncClient

    def factory(*args, **kwargs):
        kwargs["transport"] = httpx.MockTransport(handler)
        return real(*args, **kwargs)

    return patch.object(oc.httpx, "AsyncClient", factory)


def public(value=True):
    async def fake(host):
        return value

    return patch.object(oc, "_resolves_publicly", fake)


class RegistrableDomainTests(unittest.TestCase):
    def test_takes_the_last_two_labels(self):
        self.assertEqual(oc.registrable_domain("link.athome.fandango.com"), "fandango.com")
        self.assertEqual(oc.registrable_domain("example.com"), "example.com")

    def test_keeps_one_more_label_under_a_two_part_suffix(self):
        self.assertEqual(oc.registrable_domain("mail.shop.example.co.uk"), "example.co.uk")


class RouteSelectionTests(unittest.TestCase):
    def test_sender_domain_comes_from_the_header_block(self):
        self.assertEqual(oc.sender_domain_of(FANDANGO_CONTEXT), "movies.fandango.com")

    def test_a_body_cannot_supply_the_sender(self):
        context = "Message\nSubject: x\n\nFrom: a@evil.example\n"
        self.assertIsNone(oc.sender_domain_of(context))

    def test_selects_a_https_route_on_the_sender_domain_family(self):
        urls = oc.one_click_urls(FANDANGO_CONTEXT, "movies.fandango.com")
        self.assertEqual(urls, ["https://link.athome.fandango.com/oc/abc/123"])

    def test_a_route_on_another_domain_is_left_to_the_agent(self):
        context = FANDANGO_CONTEXT.replace("link.athome.fandango.com", "track.mailer.example")
        self.assertEqual(oc.one_click_urls(context, "movies.fandango.com"), [])

    def test_no_one_click_declaration_means_no_route(self):
        context = FANDANGO_CONTEXT.replace("List-Unsubscribe-Post: List-Unsubscribe=One-Click\n", "")
        self.assertEqual(oc.one_click_urls(context, "movies.fandango.com"), [])

    def test_a_declaration_quoted_in_the_body_does_not_count(self):
        context = (
            "Message\nFrom: a@example.com\n\n"
            "Unsubscribe routes extracted from this message:\n"
            "List-Unsubscribe: <https://example.com/u>\n\n"
            "List-Unsubscribe-Post: List-Unsubscribe=One-Click\n"
        )
        self.assertEqual(oc.one_click_urls(context, "example.com"), [])

    def test_plain_http_and_credentialed_urls_are_ignored(self):
        context = (
            "Message\nFrom: a@example.com\n\n"
            "Unsubscribe routes extracted from this message:\n"
            "List-Unsubscribe: <http://example.com/u>,<https://user:pw@example.com/u>,"
            "<https://example.com:8443/u>\n"
            "List-Unsubscribe-Post: List-Unsubscribe=One-Click\n"
        )
        self.assertEqual(oc.one_click_urls(context, "example.com"), [])


class AttemptTests(unittest.TestCase):
    def test_posts_the_one_click_body_and_reports_the_confirmation(self):
        seen = []

        def handler(request: httpx.Request) -> httpx.Response:
            seen.append(request)
            if request.method == "POST":
                return httpx.Response(302, headers={"location": "https://link.athome.fandango.com/manage/x"})
            return httpx.Response(200, text="<html><body>You are currently unsubscribed from marketing emails.</body></html>")

        with public(), patched_client(handler):
            result = run(oc.attempt(FANDANGO_CONTEXT))

        self.assertTrue(result.accepted)
        self.assertEqual(seen[0].method, "POST")
        self.assertEqual(seen[0].content, b"List-Unsubscribe=One-Click")
        self.assertIn("HTTP 302", result.summary)
        self.assertIn("currently unsubscribed", result.summary)

    def test_a_2xx_answer_is_accepted_without_a_page(self):
        with public(), patched_client(lambda request: httpx.Response(200, text="OK")):
            result = run(oc.attempt(FANDANGO_CONTEXT))
        self.assertTrue(result.accepted)
        self.assertIn("HTTP 200", result.summary)

    def test_an_error_status_is_refused_with_a_note_for_the_agent(self):
        with public(), patched_client(lambda request: httpx.Response(404)):
            result = run(oc.attempt(FANDANGO_CONTEXT))
        self.assertFalse(result.accepted)
        self.assertIn("HTTP 404", result.summary)

    def test_a_connection_error_is_refused_not_raised(self):
        def handler(request):
            raise httpx.ConnectError("down")

        with public(), patched_client(handler):
            result = run(oc.attempt(FANDANGO_CONTEXT))
        self.assertFalse(result.accepted)
        self.assertIn("ConnectError", result.summary)

    def test_a_private_address_is_never_contacted(self):
        def handler(request):
            raise AssertionError("must not send a request")

        with public(False), patched_client(handler):
            result = run(oc.attempt(FANDANGO_CONTEXT))
        self.assertFalse(result.accepted)
        self.assertIn("public address", result.summary)

    def test_a_redirect_off_the_sender_domain_is_not_followed(self):
        calls = []

        def handler(request):
            calls.append(request.method)
            return httpx.Response(302, headers={"location": "https://elsewhere.example/page"})

        with public(), patched_client(handler):
            result = run(oc.attempt(FANDANGO_CONTEXT))
        self.assertTrue(result.accepted)
        self.assertEqual(calls, ["POST"])

    def test_returns_none_when_the_message_offers_no_usable_route(self):
        self.assertIsNone(run(oc.attempt("Message\nFrom: news@example.com")))


if __name__ == "__main__":
    unittest.main()
