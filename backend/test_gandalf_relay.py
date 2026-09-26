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

    def test_send_to_gandalf_retries_with_blocked_hostname_defanged(self):
        server = MagicMock()
        server.send_message.side_effect = [
            gandalf_relay.smtplib.SMTPDataError(
                554,
                b"5.6.0 Link hostname of blocked.example was detected by "
                b"Cloudflare's Family DNS to contain adult-related content, "
                b"phishing, and/or malware.",
            ),
            None,
        ]
        with (
            patch.object(gandalf_relay, "IMAP_USER", "mercury@example.com"),
            patch.object(gandalf_relay, "IMAP_PASSWORD", "mailbox-password"),
            patch.object(gandalf_relay.smtplib, "SMTP_SSL") as smtp_ssl,
        ):
            smtp_ssl.return_value.__enter__.return_value = server
            result = gandalf_relay.send_to_gandalf(
                "Check Blocked.example", "See https://blocked.example/ and mercury@example.com"
            )

        self.assertTrue(result)
        self.assertEqual(server.send_message.call_count, 2)
        retried = server.send_message.call_args_list[1].args[0]
        body = retried.get_payload(decode=True).decode("utf-8")
        self.assertEqual(retried["Subject"], "Check blocked[.]example")
        self.assertIn("https://blocked[.]example/", body)
        self.assertNotIn("blocked.example", body)
        self.assertIn("mercury@example.com", body)
        self.assertIn("defanged", body)

    def test_send_to_gandalf_gives_up_when_defanged_retry_is_rejected(self):
        server = MagicMock()
        server.send_message.side_effect = gandalf_relay.smtplib.SMTPDataError(
            554, b"5.6.0 Link hostname of blocked.example was detected"
        )
        with (
            patch.object(gandalf_relay, "IMAP_USER", "mercury@example.com"),
            patch.object(gandalf_relay, "IMAP_PASSWORD", "mailbox-password"),
            patch.object(gandalf_relay.smtplib, "SMTP_SSL") as smtp_ssl,
            self.assertLogs(gandalf_relay.logger, level="WARNING") as logs,
        ):
            smtp_ssl.return_value.__enter__.return_value = server
            result = gandalf_relay.send_to_gandalf("Subject", "blocked.example")

        self.assertFalse(result)
        self.assertEqual(server.send_message.call_count, 2)
        self.assertIn("554", logs.output[-1])

    def test_send_to_gandalf_logs_other_rejections_without_retry(self):
        server = MagicMock()
        server.send_message.side_effect = gandalf_relay.smtplib.SMTPDataError(
            550, b"5.7.1 Message rejected"
        )
        with (
            patch.object(gandalf_relay, "IMAP_USER", "mercury@example.com"),
            patch.object(gandalf_relay, "IMAP_PASSWORD", "mailbox-password"),
            patch.object(gandalf_relay.smtplib, "SMTP_SSL") as smtp_ssl,
            self.assertLogs(gandalf_relay.logger, level="WARNING") as logs,
        ):
            smtp_ssl.return_value.__enter__.return_value = server
            result = gandalf_relay.send_to_gandalf("Subject", "Body")

        self.assertFalse(result)
        self.assertEqual(server.send_message.call_count, 1)
        self.assertIn("550", logs.output[0])

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
