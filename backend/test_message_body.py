import unittest

from message_body import html_to_text, judged_body

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

    def test_real_text_part_is_kept_untouched(self):
        text = "Your order has shipped. " * 10
        self.assertEqual(judged_body(text, NEWSLETTER_HTML), text)

    def test_short_genuine_text_is_kept_when_the_html_is_no_richer(self):
        self.assertEqual(judged_body("Your order has shipped.", "<p>Your order has shipped.</p>"),
                         "Your order has shipped.")

    def test_text_only_message_is_unchanged(self):
        self.assertEqual(judged_body("Hello there", None), "Hello there")
        self.assertEqual(judged_body(None, None), "")

    def test_html_with_no_visible_text_falls_back_to_what_arrived(self):
        self.assertEqual(judged_body("", "<img src='x.png'>"), "<img src='x.png'>")


if __name__ == "__main__":
    unittest.main()
