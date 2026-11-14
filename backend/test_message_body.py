import unittest

from message_body import html_to_text, judge_excerpt, judged_body

NEWSLETTER_HTML = (
    "<html><head><style>.x{color:red}</style><title>t</title></head><body>"
    "<p>Octopath Traveler is 60% off for 24 hours.</p>"
    '<p><a href="https://www.gog.com/promo">Get the deal</a></p>'
    "<script>track()</script></body></html>"
)


class HtmlToTextTests(unittest.TestCase):
    def test_keeps_visible_text_and_link_targets_and_drops_markup(self):
        text = html_to_text(NEWSLETTER_HTML)
        self.assertIn("Octopath Traveler is 60% off for 24 hours.", text)
        self.assertIn("Get the deal (https://www.gog.com/promo)", text)
        self.assertNotIn("color:red", text)
        self.assertNotIn("track()", text)

    def test_empty_or_non_string_input_is_empty(self):
        self.assertEqual(html_to_text(None), "")
        self.assertEqual(html_to_text("   "), "")
        self.assertEqual(html_to_text(123), "")

    def test_invisible_preheader_padding_does_not_fill_the_input_windows(self):
        padding = "\u200c\u200b\u034f\u00ad\u2060\ufeff&nbsp;&zwnj;" * 1000
        html = "<div>Preview" + padding + "</div><p>Your discount code is SAVE.</p>"
        body = judged_body("", html)
        for limit in (4000, 6000):
            with self.subTest(limit=limit):
                content = "From: news@example.com\nSubject: Offer\n\n" + body
                self.assertIn("Your discount code is SAVE.", content[:limit])
        self.assertEqual(body, "Preview\n\nYour discount code is SAVE.")

    def test_hidden_subtrees_do_not_hide_the_following_visible_body(self):
        for attrs in ('style="display: none"', 'style="MSO-HIDE: all"',
                      'style="color:red; DISPLAY : none !important;"',
                      'hidden', 'hidden="hidden"'):
            with self.subTest(attrs=attrs):
                html = (
                    "<div " + attrs + "><div>Hidden <span>preview</span></div>"
                    '<a href="https://hidden.example/">Hidden link</a>'
                    '<img alt="Hidden image" src="x.png"></div>'
                    "<p>Your discount code is SAVE.</p>"
                )
                self.assertEqual(html_to_text(html), "Your discount code is SAVE.")

    def test_hidden_void_and_self_closing_elements_do_not_swallow_the_body(self):
        html = (
            '<img hidden alt="Hidden image"><input style="display:none">'
            '<span hidden/><br hidden/><p>Your order has shipped.</p>'
        )
        self.assertEqual(html_to_text(html), "Your order has shipped.")

    def test_image_table_keeps_alt_text_and_link_hosts(self):
        html = (
            '<table><tr><td><a href="https://shop.example/sale">'
            '<img src="banner.png" alt="Your discount code is SAVE.">'
            '</a></td></tr></table>'
        )
        text = html_to_text(html)
        self.assertIn("Your discount code is SAVE.", text)
        self.assertIn("https://shop.example/sale", text)


class JudgeExcerptTests(unittest.TestCase):
    def test_long_urls_leave_room_for_the_body_and_keep_the_host(self):
        url = "https://tracker.example:8443/click/" + "x" * 1000 + "?id=" + "y" * 1000
        html = ('<p><a href="' + url + '">Shop</a></p>') * 4
        content = html_to_text(html + "<p>Your discount code is SAVE.</p>")
        for limit in (4000, 6000):
            with self.subTest(limit=limit):
                excerpt = judge_excerpt(content, limit)
                self.assertIn("Your discount code is SAVE.", excerpt)
                self.assertIn("https://tracker.example:8443/click/", excerpt)
                self.assertIn("...?...)", excerpt)
                self.assertNotIn("[Content truncated]", excerpt)
                self.assertLessEqual(len(excerpt), limit)

    def test_plain_text_padding_and_urls_are_compacted_before_the_cut(self):
        content = (
            "Preview" + "\u200c\u200b\u034f\u00ad\u2060\ufeff\xa0 \n" * 1000
            + "https://tracker.example/click?id=" + "x" * 7000
            + "\nYour order has shipped."
        )
        excerpt = judge_excerpt(content, 4000)
        self.assertIn("Your order has shipped.", excerpt)
        self.assertIn("https://tracker.example/click?...", excerpt)
        self.assertNotIn("[Content truncated]", excerpt)
        self.assertLess(len(excerpt), 200)

    def test_real_content_is_cut_at_a_word_boundary_with_an_explicit_marker(self):
        content = "Your order has shipped. " + "Further delivery details. " * 400
        for limit in (4000, 6000):
            with self.subTest(limit=limit):
                excerpt = judge_excerpt(content, limit)
                self.assertTrue(excerpt.startswith("Your order has shipped."))
                self.assertTrue(excerpt.endswith("\n[Content truncated]"))
                prefix = excerpt.removesuffix("\n[Content truncated]")
                self.assertTrue(content.startswith(prefix + " "))
                self.assertLessEqual(len(excerpt), limit)

    def test_short_content_and_link_targets_are_unchanged(self):
        content = "Your order has shipped.\n\nTrack: https://shop.example/orders?id=7\n"
        self.assertEqual(judge_excerpt(content, 4000), content)


class JudgedBodyTests(unittest.TestCase):
    def test_placeholder_text_part_is_replaced_by_the_html(self):
        body = judged_body("Plain text version not available", NEWSLETTER_HTML)
        self.assertIn("Octopath Traveler", body)
        self.assertNotIn("not available", body)

    def test_empty_text_part_uses_the_rendered_html_not_raw_markup(self):
        body = judged_body("", NEWSLETTER_HTML)
        self.assertIn("Octopath Traveler", body)
        self.assertNotIn("<p>", body)

    def test_short_text_part_next_to_much_longer_html_is_replaced(self):
        body = judged_body("View online", NEWSLETTER_HTML)
        self.assertIn("Octopath Traveler", body)

    def test_legal_stub_over_the_character_threshold_uses_richer_html(self):
        stub = (
            "This notice was sent to a registered subscriber. "
            "Manage your email preferences or unsubscribe from future notices."
        )
        message = "The notice explains the settlement terms and the deadline for a claim. " * 8
        self.assertEqual(judged_body(stub, "<p>" + message + "</p>"), message.strip())

    def test_browser_header_with_a_long_url_uses_richer_html(self):
        stub = (
            "View this email in your browser | Unsubscribe | "
            "https://prefs.example/" + "x" * 220
        )
        message = "Your discount code is SAVE and the sale ends on Friday. " * 8
        self.assertEqual(judged_body(stub, "<p>" + message + "</p>"), message.strip())

    def test_padding_in_the_text_part_does_not_make_it_a_real_body(self):
        stub = "\u200c\u200b\u034f\u00ad\u2060\ufeff" * 1000
        self.assertEqual(judged_body(stub, "<p>Your order has shipped.</p>"),
                         "Your order has shipped.")

    def test_tracking_url_length_does_not_make_html_richer(self):
        text = "Your order has shipped."
        html = ('<a href="https://tracker.example/' + "x" * 1000 + '">Track delivery</a>')
        self.assertEqual(judged_body(text, html), text)

    def test_real_text_part_is_kept_untouched(self):
        text = "Your order has shipped. " * 10
        self.assertEqual(judged_body(text, NEWSLETTER_HTML), text)

    def test_short_genuine_text_is_kept_when_the_html_is_no_richer(self):
        self.assertEqual(judged_body("Your order has shipped.", "<p>Your order has shipped.</p>"),
                         "Your order has shipped.")

    def test_text_only_message_is_unchanged(self):
        self.assertEqual(judged_body("Hello there", None), "Hello there")
        self.assertEqual(judged_body(None, None), "")

    def test_html_without_alt_text_does_not_return_raw_markup(self):
        self.assertEqual(judged_body("", "<img src='x.png'>"), "")

    def test_html_with_only_hidden_text_does_not_return_the_hidden_markup(self):
        for html in ('<div hidden>Hidden preview</div>',
                     '<div style="display:none">Hidden preview</div>',
                     '<div style="mso-hide:all">Hidden preview</div>'):
            with self.subTest(html=html):
                self.assertEqual(judged_body("", html), "")
                self.assertEqual(judged_body("Your order has shipped.", html),
                                 "Your order has shipped.")


if __name__ == "__main__":
    unittest.main()
