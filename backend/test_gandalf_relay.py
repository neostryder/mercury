import unittest
from unittest.mock import MagicMock, patch

import gandalf_relay


class GandalfRelayTests(unittest.TestCase):
    def test_send_to_gandalf_uses_mailbox_credentials_and_recipient(self):
        server = MagicMock()
        with (
            patch.object(gandalf_relay, "SMTP_HOST", "smtp.example.com"),
            patch.object(gandalf_relay, "SMTP_PORT", 465),
            patch.object(gandalf_relay, "IMAP_USER", "mercury@example.com"),
            patch.object(gandalf_relay, "IMAP_PASSWORD", "mailbox-password"),
            patch.object(gandalf_relay.smtplib, "SMTP_SSL") as smtp_ssl,
        ):
            smtp_ssl.return_value.__enter__.return_value = server
            result = gandalf_relay.send_to_gandalf("Research Acme", "Flagged message")

        self.assertTrue(result)
        smtp_ssl.assert_called_once_with("smtp.example.com", 465)
        server.login.assert_called_once_with("mercury@example.com", "mailbox-password")
        message = server.send_message.call_args.args[0]
        self.assertEqual(message["From"], "mercury@example.com")
        self.assertEqual(message["To"], gandalf_relay.GANDALF_ADDRESS)
        self.assertEqual(message["Subject"], "Research Acme")

    def test_send_to_gandalf_catches_connection_failure(self):
        with (
            patch.object(gandalf_relay, "IMAP_USER", "mercury@example.com"),
            patch.object(gandalf_relay, "IMAP_PASSWORD", "mailbox-password"),
            patch.object(
                gandalf_relay.smtplib,
                "SMTP_SSL",
                side_effect=ConnectionError("SMTP unavailable"),
            ),
        ):
            result = gandalf_relay.send_to_gandalf("Research Acme", "Flagged message")

        self.assertFalse(result)

    def test_send_to_gandalf_skips_connection_without_credentials(self):
        with (
            patch.object(gandalf_relay, "IMAP_USER", None),
            patch.object(gandalf_relay, "IMAP_PASSWORD", None),
            patch.object(gandalf_relay.smtplib, "SMTP_SSL") as smtp_ssl,
        ):
            result = gandalf_relay.send_to_gandalf("Research Acme", "Flagged message")

        self.assertFalse(result)
        smtp_ssl.assert_not_called()


if __name__ == "__main__":
    unittest.main()
