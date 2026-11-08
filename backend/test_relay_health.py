import asyncio
import unittest

import relay_health as rh


class FakeNotifier:
    def __init__(self):
        self.sent = []

    async def send(self, text):
        self.sent.append(text)


def reset():
    rh._started_at = 0.0
    rh._last_beat = None
    rh._last_report = {}
    rh._alerting = False


GOOD = {"handler_first": True, "secret_set": True, "updates_seen": 4}


class ProblemTests(unittest.TestCase):
    def setUp(self):
        reset()

    def test_quiet_during_startup_grace(self):
        self.assertIsNone(rh.problem(now=60))

    def test_no_heartbeat_after_grace_is_a_problem(self):
        self.assertIn("since Mercury started", rh.problem(now=rh.STARTUP_GRACE_SECONDS + 1))

    def test_fresh_good_heartbeat_is_healthy(self):
        rh.record_heartbeat(GOOD, now=1000)
        self.assertIsNone(rh.problem(now=1100))

    def test_stale_heartbeat_is_a_problem(self):
        rh.record_heartbeat(GOOD, now=1000)
        self.assertIn("minutes ago", rh.problem(now=1000 + rh.STALE_AFTER_SECONDS + 60))

    def test_shadowed_handler_is_a_problem(self):
        rh.record_heartbeat({**GOOD, "handler_first": False}, now=1000)
        self.assertIn("not first", rh.problem(now=1010))

    def test_missing_secret_is_a_problem(self):
        rh.record_heartbeat({**GOOD, "secret_set": False}, now=1000)
        self.assertIn("no relay secret", rh.problem(now=1010))

    def test_missing_fields_are_unhealthy(self):
        rh.record_heartbeat({}, now=1000)
        self.assertIsNotNone(rh.problem(now=1010))

    def test_non_object_report_is_unhealthy(self):
        rh.record_heartbeat("ok", now=1000)
        self.assertIsNotNone(rh.problem(now=1010))


class CheckOnceTests(unittest.TestCase):
    def setUp(self):
        reset()

    def test_alerts_once_then_announces_recovery(self):
        notifier = FakeNotifier()
        rh.record_heartbeat({**GOOD, "handler_first": False}, now=1000)

        async def run():
            await rh.check_once(notifier, now=1010)
            await rh.check_once(notifier, now=1020)
            rh.record_heartbeat(GOOD, now=1030)
            await rh.check_once(notifier, now=1040)
            await rh.check_once(notifier, now=1050)

        asyncio.run(run())
        self.assertEqual(len(notifier.sent), 2)
        self.assertIn("problem", notifier.sent[0])
        self.assertIn("working again", notifier.sent[1])

    def test_healthy_relay_sends_nothing(self):
        notifier = FakeNotifier()
        rh.record_heartbeat(GOOD, now=1000)
        asyncio.run(rh.check_once(notifier, now=1010))
        self.assertEqual(notifier.sent, [])


if __name__ == "__main__":
    unittest.main()
