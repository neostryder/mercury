import asyncio
import unittest

from credential_prompts import CredentialPromptStore


class MutableClock:
    def __init__(self):
        self.value = 1000.0

    def __call__(self) -> float:
        return self.value


class CredentialPromptStoreTests(unittest.TestCase):
    def setUp(self):
        self.clock = MutableClock()
        self.store = CredentialPromptStore(clock=self.clock)

    def test_create_and_valid_lookup(self):
        token = self.store.create("fanatical.com")

        self.assertGreaterEqual(len(token), 40)
        self.assertEqual(self.store.get_status(token), {
            "domain": "fanatical.com",
            "expires_in_seconds": 600,
        })

    def test_expired_prompt_is_removed(self):
        token = self.store.create("fanatical.com")
        self.clock.value += 601

        self.assertIsNone(self.store.get_status(token))
        self.assertFalse(self.store.submit(token, "aaron", "secret"))

    def test_submit_is_single_use_and_wait_clears_the_entry(self):
        token = self.store.create("fanatical.com")

        self.assertTrue(self.store.submit(token, "aaron", "secret"))
        self.assertFalse(self.store.submit(token, "aaron", "second"))
        self.assertIsNone(self.store.get_status(token))
        self.assertEqual(
            asyncio.run(self.store.wait_for(token, timeout_seconds=1)),
            ("aaron", "secret"),
        )
        self.assertIsNone(asyncio.run(self.store.wait_for(token, timeout_seconds=0)))

    def test_submission_accepted_before_expiry_survives_waiter_scheduling(self):
        token = self.store.create("fanatical.com")
        self.clock.value += 599

        self.assertTrue(self.store.submit(token, "aaron", "secret"))
        self.clock.value += 2
        self.assertEqual(
            asyncio.run(self.store.wait_for(token, timeout_seconds=1)),
            ("aaron", "secret"),
        )

    def test_wait_receives_a_later_submission(self):
        async def scenario():
            token = self.store.create("fanatical.com")
            waiter = asyncio.create_task(
                self.store.wait_for(token, timeout_seconds=1)
            )
            await asyncio.sleep(0)
            self.assertTrue(self.store.submit(token, "aaron", "secret"))
            return await waiter

        self.assertEqual(asyncio.run(scenario()), ("aaron", "secret"))

    def test_wait_times_out_and_invalidates_the_prompt(self):
        token = self.store.create("fanatical.com")

        self.assertIsNone(asyncio.run(self.store.wait_for(token, timeout_seconds=0.01)))
        self.assertIsNone(self.store.get_status(token))
        self.assertFalse(self.store.submit(token, "aaron", "too-late"))


if __name__ == "__main__":
    unittest.main()
