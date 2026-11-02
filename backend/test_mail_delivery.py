import unittest
from unittest.mock import patch

import mail_delivery


class FakeImap:
    def __init__(self, host, port, existing=None, folders=None, logout_error=None):
        self.appended_to = None
        self.existing = existing or {}
        self.folders = folders or ["INBOX", "Archive"]
        self.logout_error = logout_error
        self.selected = None
        self.searched = []

    def login(self, user, password):
        return "OK", []

    def list(self):
        return "OK", [f'(\\HasNoChildren) "/" "{name}"'.encode() for name in self.folders]

    def select(self, folder, readonly=False):
        self.selected = folder.strip('"')
        return "OK", [b"1"]

    def search(self, charset, *criteria):
        self.searched.append(self.selected)
        return "OK", [b"7" if self.selected in self.existing else b""]

    def append(self, folder, flags, date_time, message):
        self.appended_to = folder
        return "OK", []

    def logout(self):
        if self.logout_error:
            raise self.logout_error
        return "BYE", []


RAW = "Message-ID: <abc@example.com>\r\nSubject: Test\r\n\r\nBody"


def deliver(connection, **kwargs):
    with (
        patch.object(mail_delivery, "DELIVER_ACCEPTED_MAIL", True),
        patch.object(mail_delivery, "IMAP_USER", "user"),
        patch.object(mail_delivery, "IMAP_PASSWORD", "password"),
        patch.object(mail_delivery.imaplib, "IMAP4_SSL", return_value=connection) as factory,
    ):
        result = mail_delivery.deliver_accepted_message(RAW, "LEGIT", "OTHER", "250", **kwargs)
    return result, factory


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

    def test_existing_copy_in_the_target_folder_is_not_appended_again(self):
        connection = FakeImap("host", 993, existing={"INBOX"})
        result, _ = deliver(connection)

        self.assertEqual(result, "delivered to INBOX")
        self.assertIsNone(connection.appended_to)

    def test_a_copy_filed_elsewhere_is_found_only_when_every_folder_is_searched(self):
        first = FakeImap("host", 993, existing={"Archive"}, folders=["INBOX", "Archive", "Receipts"])
        result, _ = deliver(first)
        self.assertEqual(result, "delivered to INBOX")
        self.assertEqual(first.appended_to, "INBOX")

        retry = FakeImap("host", 993, existing={"Archive"}, folders=["INBOX", "Archive", "Receipts"])
        result, _ = deliver(retry, search_all_folders=True)
        self.assertEqual(result, "delivered to Archive")
        self.assertIsNone(retry.appended_to)

    def test_a_failed_logout_does_not_turn_a_delivered_message_into_a_failure(self):
        connection = FakeImap("host", 993, logout_error=OSError("connection reset"))
        result, _ = deliver(connection)

        self.assertEqual(result, "delivered to INBOX")
        self.assertEqual(connection.appended_to, "INBOX")

    def test_the_connection_gets_a_socket_timeout(self):
        _, factory = deliver(FakeImap("host", 993))

        self.assertEqual(factory.call_args.kwargs["timeout"], mail_delivery.IMAP_TIMEOUT_SECONDS)

    def test_a_message_without_a_message_id_is_appended_without_a_search(self):
        connection = FakeImap("host", 993)
        with (
            patch.object(mail_delivery, "DELIVER_ACCEPTED_MAIL", True),
            patch.object(mail_delivery, "IMAP_USER", "user"),
            patch.object(mail_delivery, "IMAP_PASSWORD", "password"),
            patch.object(mail_delivery.imaplib, "IMAP4_SSL", return_value=connection),
        ):
            result = mail_delivery.deliver_accepted_message("Subject: x\r\n\r\nBody", "LEGIT", "OTHER", "250")

        self.assertEqual(result, "delivered to INBOX")
        self.assertEqual(connection.searched, [])


if __name__ == "__main__":
    unittest.main()
