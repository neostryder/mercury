import unittest
from unittest.mock import patch

import mail_delivery


class FakeImap:
    def __init__(self, host, port):
        self.appended_to = None

    def login(self, user, password):
        return "OK", []

    def append(self, folder, flags, date_time, message):
        self.appended_to = folder
        return "OK", []

    def logout(self):
        return "BYE", []


class MailDeliveryTests(unittest.TestCase):
    def test_delivery_uses_requested_folder(self):
        connection = FakeImap("host", 993)
        with (
            patch.object(mail_delivery, "DELIVER_ACCEPTED_MAIL", True),
            patch.object(mail_delivery, "IMAP_USER", "user"),
            patch.object(mail_delivery, "IMAP_PASSWORD", "password"),
            patch.object(mail_delivery.imaplib, "IMAP4_SSL", return_value=connection),
        ):
            result = mail_delivery.deliver_accepted_message(
                "Subject: Test\r\n\r\nBody", "LEGIT", "OTHER", "250", "Archive"
            )

        self.assertEqual(result, "delivered to Archive")
        self.assertEqual(connection.appended_to, "Archive")

    def test_delivery_rejects_control_characters_in_folder(self):
        with (
            patch.object(mail_delivery, "DELIVER_ACCEPTED_MAIL", True),
            patch.object(mail_delivery, "IMAP_USER", "user"),
            patch.object(mail_delivery, "IMAP_PASSWORD", "password"),
        ):
            result = mail_delivery.deliver_accepted_message(
                "Subject: Test\r\n\r\nBody", "LEGIT", "OTHER", "250", "Archive\r\nBAD"
            )
        self.assertEqual(result, "failed: invalid target folder")


if __name__ == "__main__":
    unittest.main()
