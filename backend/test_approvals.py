import json
import os
import shutil
import stat
import tempfile
import time
import unittest
from pathlib import Path

from cryptography.fernet import Fernet

import approvals
from approvals import ApprovalStore

RAW = "From: a@example.com\r\nSubject: hello\r\n\r\nPrivate body text\r\n"


class RawMessageAtRestTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = Path(tempfile.mkdtemp())
        self.path = self.temp_dir / "approvals.json"
        self.key = Fernet.generate_key()
        self.store = ApprovalStore(self.path, self.key)

    def tearDown(self):
        shutil.rmtree(self.temp_dir, ignore_errors=True)

    def _file_text(self):
        return self.path.read_text(encoding="utf-8")

    def test_raw_message_is_encrypted_in_the_file(self):
        brief_id = self.store.create_brief("context", message_metadata={"raw_message": RAW, "verdict": "UNSURE"})
        text = self._file_text()
        self.assertNotIn("Private body text", text)
        self.assertNotIn('"raw_message":', text)
        self.assertIn("raw_message_sealed", text)
        brief = self.store.get_brief(brief_id)
        self.assertEqual(brief["message_metadata"]["raw_message"], RAW)
        self.assertEqual(brief["message_metadata"]["verdict"], "UNSURE")

    def test_later_writes_keep_it_encrypted(self):
        brief_id = self.store.create_brief("context", message_metadata={"raw_message": RAW})
        self.store.get_brief(brief_id)
        self.store.append_turn(brief_id, "recipient", "deliver it")
        self.store.update_brief(brief_id, rounds=1)
        self.assertNotIn("Private body text", self._file_text())
        self.assertEqual(self.store.get_brief(brief_id)["message_metadata"]["raw_message"], RAW)

    def test_the_callers_metadata_is_not_modified(self):
        metadata = {"raw_message": RAW}
        self.store.create_brief("context", message_metadata=metadata)
        self.assertEqual(metadata, {"raw_message": RAW})

    def test_deciding_or_resolving_drops_the_raw_copy(self):
        decided = self.store.create_brief("a", message_metadata={"raw_message": RAW})
        resolved = self.store.create_brief("b", message_metadata={"raw_message": RAW})
        self.store.forget_raw_message(decided)
        self.store.resolve_brief(resolved)
        self.assertNotIn("raw_message_sealed", self._file_text())
        self.assertNotIn("raw_message", self.store.get_brief(decided)["message_metadata"])
        self.assertNotIn("raw_message", self.store.get_brief(resolved)["message_metadata"])

    def test_a_raw_copy_older_than_the_ttl_is_dropped(self):
        brief_id = self.store.create_brief("context", message_metadata={"raw_message": RAW})
        data = json.loads(self._file_text())
        data["briefs"][brief_id]["created_at"] = time.time() - approvals.RAW_MESSAGE_TTL_SECONDS - 60
        self.path.write_text(json.dumps(data), encoding="utf-8")
        self.assertNotIn("raw_message", self.store.get_brief(brief_id)["message_metadata"])
        self.store.append_turn(brief_id, "recipient", "hello")
        self.assertNotIn("raw_message_sealed", self._file_text())

    def test_a_plaintext_file_from_before_encryption_is_sealed_on_next_write(self):
        legacy = ApprovalStore(self.path)
        brief_id = legacy.create_brief("context", message_metadata={"raw_message": RAW})
        self.assertIn("Private body text", self._file_text())
        self.store.append_turn(brief_id, "recipient", "hello")
        self.assertNotIn("Private body text", self._file_text())
        self.assertEqual(self.store.get_brief(brief_id)["message_metadata"]["raw_message"], RAW)

    def test_a_different_key_cannot_read_the_raw_copy(self):
        brief_id = self.store.create_brief("context", message_metadata={"raw_message": RAW})
        other = ApprovalStore(self.path, Fernet.generate_key())
        self.assertNotIn("raw_message", other.get_brief(brief_id)["message_metadata"])

    def test_empty_raw_message_is_not_stored(self):
        brief_id = self.store.create_brief("context", message_metadata={"raw_message": None})
        self.assertNotIn("raw_message", self._file_text())
        self.assertNotIn("raw_message", self.store.get_brief(brief_id)["message_metadata"])

    @unittest.skipIf(os.name == "nt", "POSIX file modes")
    def test_the_file_is_owner_only(self):
        self.store.create_brief("context")
        self.assertEqual(stat.S_IMODE(self.path.stat().st_mode), 0o600)


if __name__ == "__main__":
    unittest.main()
