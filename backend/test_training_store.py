import hashlib
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from training_store import TrainingStore, message_key


class TrainingStoreTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(dir=Path(__file__).parent)
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / "rows.json"
        self.now = 1000.0
        self.store = TrainingStore(self.path, clock=lambda: self.now)
        self.identity = ("2026-11-14T12:00:00+00:00", "example.com", "A subject")

    def _capture(self, **overrides):
        values = {
            "received_at": self.identity[0], "from_domain": self.identity[1],
            "subject": self.identity[2], "email": "Redacted text",
            "facts": {"sender_domain": "example.com", "detail": "recipient detail"},
            "structured": None,
            "decision": {"verdict": "SPAM", "disposition": "550", "category": "OTHER",
                         "alert": "NONE", "reasoning": "Unwanted message"},
        }
        values.update(overrides)
        self.store.capture(**values)

    def test_key_matches_the_worker_identity_fields(self):
        expected = hashlib.sha256("2026-11-14T12:00:00+00:00|example.com|A subject".encode()).hexdigest()
        self.assertEqual(message_key(*self.identity), expected)
        self.assertNotEqual(message_key(*self.identity), message_key(*self.identity[:2], "Other subject"))

    def test_capture_keeps_only_redacted_state_and_survives_reload(self):
        self._capture()
        row, = TrainingStore(self.path, clock=lambda: self.now).rows()
        self.assertEqual(row["key"], message_key(*self.identity))
        self.assertEqual(row["pilot"], "mercury_screen")
        self.assertEqual(row["state"], {"email": "Redacted text", "facts": {"sender_domain": "example.com"}})
        self.assertEqual(row["stored_at"], self.now)
        self.assertEqual(row["expires_at"], self.now + 30 * 86400)
        self.assertIsNone(row["label"])
        self.assertIsNone(row["structured"])
        self.assertNotIn("recipient detail", self.path.read_text())

    def test_read_prunes_at_thirty_days_and_removes_text_from_disk(self):
        self._capture()
        self.now += 30 * 86400 - 1
        self.assertEqual(len(self.store.rows()), 1)
        self.now += 1
        self.assertEqual(self.store.rows(), [])
        self.assertEqual(json.loads(self.path.read_text()), {})

    def test_capture_and_label_writes_prune_other_expired_rows(self):
        for operation in ("capture", "label"):
            with self.subTest(operation=operation):
                self._capture()
                self.now += 31 * 86400
                if operation == "capture":
                    self._capture(subject="New subject")
                else:
                    self.assertFalse(self.store.label(*self.identity, "right", None))
                self.assertNotIn(message_key(*self.identity), json.loads(self.path.read_text()))

    def test_labeled_rows_survive_and_recapture_preserves_the_label(self):
        self._capture()
        self.assertTrue(self.store.label(*self.identity, "wrong", "250"))
        self.now += 365 * 86400
        self._capture(email="Replacement text")
        row, = self.store.rows()
        self.assertEqual(row["label"]["verdict_choice"], "LEGIT")
        self.assertEqual(row["state"]["email"], "Redacted text")
        self.assertIsNone(self.store.status()["oldest_expiry"])

    def test_undo_restores_original_expiry_and_prunes_an_old_row(self):
        self._capture()
        self.store.label(*self.identity, "right", None)
        self.assertTrue(self.store.label(*self.identity, "undo", None))
        self.assertIsNone(self.store.rows()[0]["label"])
        self.store.label(*self.identity, "right", None)
        self.now += 31 * 86400
        self.assertTrue(self.store.label(*self.identity, "undo", None))
        self.assertEqual(self.store.rows(), [])
        self.assertNotIn("Redacted text", self.path.read_text())

    def test_retention_and_path_can_be_overridden(self):
        with patch.dict(os.environ, {"MERCURY_TRAINING_PATH": str(self.path),
                                     "MERCURY_TRAINING_RETENTION_DAYS": "2"}):
            self.store = TrainingStore(clock=lambda: self.now)
        self._capture()
        self.now += 2 * 86400
        self.assertEqual(self.store.rows(), [])

    def test_replace_is_atomic_and_a_failure_leaves_the_previous_file(self):
        self._capture()
        previous = self.path.read_bytes()

        def fail_replace(source, destination):
            self.assertEqual(Path(destination), self.path)
            self.assertEqual(Path(source).parent, self.path.parent)
            self.assertEqual(self.path.read_bytes(), previous)
            self.assertEqual(len(json.loads(Path(source).read_text())), 2)
            raise OSError("replace failed")

        with patch("training_store.os.replace", side_effect=fail_replace):
            with self.assertRaises(OSError):
                self._capture(subject="Another subject")
        self.assertEqual(self.path.read_bytes(), previous)
        self.assertEqual(list(self.path.parent.glob("*.tmp")), [])

    def test_corrupt_storage_is_not_silently_overwritten(self):
        self.path.write_bytes(b"invalid json")
        with self.assertRaises(json.JSONDecodeError):
            self._capture()
        self.assertEqual(self.path.read_bytes(), b"invalid json")

    def test_export_skips_unlabeled_rows_and_empty_text(self):
        self._capture(email=" \n ")
        self.store.label(*self.identity, "right", None)
        self._capture(subject="Unlabeled")
        self.assertEqual(self.store.export({}), "")


if __name__ == "__main__":
    unittest.main()
