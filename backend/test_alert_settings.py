import asyncio
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

os.environ.setdefault("MERCURY_SHARED_SECRET", "test-secret")
os.environ.setdefault("TELEGRAM_BOT_TOKEN", "test-token")
os.environ.setdefault("TELEGRAM_CHAT_ID", "test-chat")
os.environ.setdefault("PROMPT_INJECTION_CLASSIFIER_URL", "http://classifier.test")
os.environ.setdefault("AGENT_GATEWAY_URL", "http://judge.test")
os.environ.setdefault("AGENT_GATEWAY_SECRET", "judge-secret")

sys.path.insert(0, str(Path(__file__).parent))

import alert_settings
import app
import cresset_mirror

KEY = "crs_fake-source-key"


class StoreTests(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.dir.cleanup)
        self.path = Path(self.dir.name) / "alert_settings.json"
        patcher = patch.dict(os.environ, {"MERCURY_ALERT_SETTINGS_PATH": str(self.path)}, clear=True)
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_nothing_saved_falls_back_to_the_environment_then_off(self):
        self.assertEqual(alert_settings.current_mode(), "off")
        os.environ["MERCURY_CRESSET_MODE"] = "both"
        self.assertEqual(alert_settings.current_mode(), "both")
        os.environ["MERCURY_CRESSET_MODE"] = "cresset"
        self.assertEqual(alert_settings.current_mode(), "off")

    def test_a_saved_choice_wins_over_the_environment(self):
        os.environ["MERCURY_CRESSET_MODE"] = "both"
        alert_settings.save_mode("off")
        self.assertEqual(alert_settings.current_mode(), "off")
        alert_settings.save_mode("both")
        self.assertEqual(json.loads(self.path.read_text()), {"cresset": "both"})
        self.assertEqual(alert_settings.saved_mode(), "both")

    def test_an_unknown_mode_is_refused_and_nothing_is_written(self):
        with self.assertRaises(ValueError):
            alert_settings.save_mode("cresset")
        self.assertFalse(self.path.exists())

    def test_a_damaged_file_is_ignored(self):
        self.path.write_text("not json")
        self.assertIsNone(alert_settings.saved_mode())
        self.path.write_text('{"cresset": "loud"}')
        self.assertEqual(alert_settings.current_mode(), "off")


class MirrorUsesTheSettingTests(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.dir.cleanup)
        env = {
            "MERCURY_ALERT_SETTINGS_PATH": str(Path(self.dir.name) / "alert_settings.json"),
            "MERCURY_CRESSET_ALLOW_UNDER_TEST": "1",
            "CRESSET_URL": "https://cresset.example.com",
            "CRESSET_MERCURY_TOKEN": KEY,
        }
        patcher = patch.dict(os.environ, env, clear=True)
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_the_saved_setting_turns_the_mirror_on_and_off(self):
        self.assertFalse(cresset_mirror.enabled())
        alert_settings.save_mode("both")
        self.assertTrue(cresset_mirror.enabled())
        alert_settings.save_mode("off")
        self.assertFalse(cresset_mirror.enabled())


class EndpointTests(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.dir.cleanup)
        env = {"MERCURY_ALERT_SETTINGS_PATH": str(Path(self.dir.name) / "alert_settings.json")}
        patcher = patch.dict(os.environ, env)
        patcher.start()
        self.addCleanup(patcher.stop)
        log = patch.object(app.event_log, "log_event")
        log.start()
        self.addCleanup(log.stop)

    def _post(self, payload, secret=app.SHARED_SECRET):
        request = SimpleNamespace(json=AsyncMock(return_value=payload))
        return asyncio.run(app.change_alert_settings(request, secret))

    def test_the_shared_secret_is_required(self):
        with self.assertRaises(app.HTTPException) as caught:
            asyncio.run(app.get_alert_settings("wrong"))
        self.assertEqual(caught.exception.status_code, 403)
        with self.assertRaises(app.HTTPException) as caught:
            self._post({"cresset": "both"}, secret="wrong")
        self.assertEqual(caught.exception.status_code, 403)
        self.assertIsNone(alert_settings.saved_mode())

    def test_a_change_is_saved_and_read_back(self):
        self.assertEqual(asyncio.run(app.get_alert_settings(app.SHARED_SECRET))["cresset"], "off")
        self.assertEqual(self._post({"cresset": "both"}), {"ok": True, "cresset": "both"})
        view = asyncio.run(app.get_alert_settings(app.SHARED_SECRET))
        self.assertEqual((view["cresset"], view["saved"], view["modes"]), ("both", True, ["off", "both"]))

    def test_a_bad_mode_is_a_400(self):
        for payload in ({"cresset": "cresset"}, {}, [], "both"):
            with self.assertRaises(app.HTTPException) as caught:
                self._post(payload)
            self.assertEqual(caught.exception.status_code, 400)
        self.assertIsNone(alert_settings.saved_mode())


if __name__ == "__main__":
    unittest.main()
