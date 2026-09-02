import unittest
from pathlib import Path

from dedup import DEDUP_RETENTION_SECONDS, IngestDedupStore


class MutableClock:
    def __init__(self):
        self.value = 1000.0

    def __call__(self) -> float:
        return self.value


class IngestDedupStoreTests(unittest.TestCase):
    def setUp(self):
        self.clock = MutableClock()
        self.path = Path(__file__).parent / ".test-dedup-store.json"
        self.path.unlink(missing_ok=True)
        self.store = IngestDedupStore(self.path, clock=self.clock)

    def tearDown(self):
        self.path.unlink(missing_ok=True)

    def test_first_claim_returns_none_and_marks_pending(self):
        self.assertIsNone(self.store.claim("key-1"))
        self.assertEqual(self.store.claim("key-1"), {"status": "pending", "at": 1000.0})

    def test_recorded_outcome_is_replayed_on_a_later_claim(self):
        self.store.claim("key-1")
        self.store.record("key-1", 250, {"ok": True})

        self.assertEqual(self.store.claim("key-1"), {
            "status": "done", "at": 1000.0, "disposition": 250, "content": {"ok": True},
        })

    def test_release_clears_a_pending_claim_so_a_retry_runs_the_pipeline(self):
        self.store.claim("key-1")
        self.store.release("key-1")

        self.assertIsNone(self.store.claim("key-1"))

    def test_entries_past_retention_are_pruned_on_the_next_claim(self):
        self.store.claim("key-1")
        self.store.record("key-1", 250, {"ok": True})
        self.clock.value += DEDUP_RETENTION_SECONDS + 1

        self.assertIsNone(self.store.claim("key-1"))

    def test_different_keys_do_not_interfere(self):
        self.store.claim("key-1")
        self.store.record("key-1", 550, {"ok": False})

        self.assertIsNone(self.store.claim("key-2"))


if __name__ == "__main__":
    unittest.main()
