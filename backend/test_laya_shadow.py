import json
import math
import os
import random
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

os.environ.setdefault("MERCURY_SHARED_SECRET", "test-secret")

import laya_shadow


def _choice(probs):
    choice = max(probs, key=probs.get)
    return {"choice": choice, "probabilities": probs,
            "confidence": laya_shadow.entropy_confidence(probs)}


def _answers(verdict_probs, severity=0.2, impersonates=0.05):
    sev = {"0": 1 - severity, "1": severity * 0.5, "2": severity * 0.3, "3": severity * 0.2}
    return {
        "verdict": _choice(verdict_probs),
        "category": _choice({"PHISHING": verdict_probs.get("PHISH", 0) + 0.01,
                             "PROMOTIONAL": verdict_probs.get("SPAM", 0) + 0.01,
                             "TRANSACTIONAL": verdict_probs.get("LEGIT", 0) + 0.01}),
        "severity": {**_choice(sev), "score": sum(int(k) * v for k, v in sev.items())},
        "matched_rule": None,
        "signals": {"impersonates_known_party": impersonates},
        "rule_ids": {},
    }


def _norm(d):
    total = sum(d.values())
    return {k: v / total for k, v in d.items()}


class ShadowTestCase(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        base = Path(self.dir.name)
        self.patches = [
            patch.object(laya_shadow, "SHADOW_PATH", base / "shadow.jsonl"),
            patch.object(laya_shadow, "CALIBRATION_PATH", base / "calibration.json"),
            patch.object(laya_shadow, "_cached", None),
            patch.object(laya_shadow, "ITERATIONS", 120),
        ]
        for p in self.patches:
            p.start()

    def tearDown(self):
        for p in self.patches:
            p.stop()
        self.dir.cleanup()

    def _pairs(self, n, confuse_phish=True, seed=7):
        """A shadow whose answers are the primary's put through a fixed
        distortion plus a little noise: softer everywhere, and biased toward
        SPAM strongly enough that it reads PHISH as SPAM, the confusion a live
        probe showed. A systematic distortion is what calibration can undo;
        noise is not, which is why the gate is judged out of fold."""
        rng = random.Random(seed)
        bias = {"SPAM": 2.0, "PHISH": -1.0} if confuse_phish else {}

        def distort(probs, bias_for=None):
            z = {k: 0.6 * math.log(max(v, 1e-6)) + (bias_for or {}).get(k, 0.0)
                 + rng.gauss(0, 0.05) for k, v in probs.items()}
            top = max(z.values())
            e = {k: math.exp(v - top) for k, v in z.items()}
            return _norm(e)

        def soften(p):
            x = math.log(p / (1 - p))
            return 1 / (1 + math.exp(-(0.5 * x + 0.3 + rng.gauss(0, 0.05))))

        for i in range(n):
            truth = rng.choice(["LEGIT", "LEGIT", "LEGIT", "SPAM", "PHISH"])
            bad = truth == "PHISH"
            primary = _answers(_norm({k: (0.97 if k == truth else 0.01)
                                      for k in ("LEGIT", "SPAM", "PHISH", "UNSURE")}),
                               0.9 if bad else 0.05, 0.95 if bad else 0.03)
            shadow = {
                "verdict": _choice(distort(primary["verdict"]["probabilities"], bias)),
                "category": _choice(distort(primary["category"]["probabilities"])),
                "matched_rule": None,
                "rule_ids": {},
                "signals": {k: soften(v) for k, v in primary["signals"].items()},
            }
            sev = distort(primary["severity"]["probabilities"])
            shadow["severity"] = {**_choice(sev), "score": sum(int(k) * v for k, v in sev.items())}
            laya_shadow.record_pair("m{}".format(i), {**primary, "model": "jev"},
                                    {**shadow, "model": "laya"})


class ConfidenceTests(unittest.TestCase):
    def test_matches_the_primarys_reported_confidence(self):
        # A live primary answer: these probabilities came back with confidence 0.13.
        conf = laya_shadow.entropy_confidence({"0": 0.06, "1": 0.22, "2": 0.25, "3": 0.47})
        self.assertAlmostEqual(conf, 0.13, places=2)

    def test_certainty_is_one(self):
        self.assertEqual(laya_shadow.entropy_confidence({"A": 1.0, "B": 0.0}), 1.0)


class RecordingTests(ShadowTestCase):
    def test_no_message_content_is_stored(self):
        answers = _answers({"LEGIT": 0.9, "SPAM": 0.1})
        laya_shadow.record_pair("m1", {**answers, "email": "secret body"}, answers)
        text = laya_shadow.SHADOW_PATH.read_text(encoding="utf-8")
        self.assertNotIn("secret body", text)

    def test_a_torn_last_line_is_skipped(self):
        answers = _answers({"LEGIT": 0.9, "SPAM": 0.1})
        laya_shadow.record_pair("m1", answers, answers)
        with laya_shadow.SHADOW_PATH.open("a", encoding="utf-8") as f:
            f.write('{"id": "m2", "prim')
        pairs, _ = laya_shadow._read()
        self.assertEqual([r["id"] for r in pairs], ["m1"])

    def test_outcomes_are_read_back_by_id(self):
        laya_shadow.record_outcome("m1", "250", "telegram")
        laya_shadow.record_outcome("m2", "bogus", "telegram")
        _, outcomes = laya_shadow._read()
        self.assertEqual(outcomes, {"m1": "250"})

    def test_missing_id_records_nothing(self):
        laya_shadow.record_pair(None, {}, {})
        laya_shadow.record_outcome(None, "250", "telegram")
        self.assertFalse(laya_shadow.SHADOW_PATH.exists())


class CalibrationTests(ShadowTestCase):
    def test_a_per_option_bias_corrects_a_systematic_confusion(self):
        self._pairs(120)
        rows, _ = laya_shadow._read()
        fits = laya_shadow._fit_all(rows)
        phish_rows = [r for r in rows if r["primary"]["verdict"]["choice"] == "PHISH"]
        before = sum(r["laya"]["verdict"]["choice"] == "PHISH" for r in phish_rows)
        after = sum(laya_shadow.apply(r["laya"], {"fits": fits})["verdict"]["choice"] == "PHISH"
                    for r in phish_rows)
        self.assertEqual(before, 0)
        self.assertGreater(after, len(phish_rows) * 0.8)

    def test_too_few_rows_is_never_ready(self):
        self._pairs(40, confuse_phish=False)
        result = laya_shadow.refit()
        self.assertFalse(result["ready"])
        self.assertTrue(any("paired rows" in w for w in result["why_not"]))
        self.assertIsNone(laya_shadow.failover(_answers({"LEGIT": 1.0})))

    def test_enough_agreeing_rows_become_ready(self):
        self._pairs(220)
        with patch.object(laya_shadow, "READY_MIN_ROWS", 200):
            result = laya_shadow.refit()
        self.assertGreaterEqual(result["metrics"]["disposition_agreement"], 0.95,
                                result["metrics"])
        self.assertTrue(result["ready"], result["why_not"])
        on_disk = json.loads(laya_shadow.CALIBRATION_PATH.read_text(encoding="utf-8"))
        self.assertTrue(on_disk["ready"])
        calibrated = laya_shadow.failover(_answers({"LEGIT": 0.9, "SPAM": 0.1}))
        self.assertTrue(calibrated["calibrated"])

    def test_human_labels_gate_once_there_are_enough(self):
        self._pairs(220)
        # The recipient delivered every message the primary called PHISH: the
        # shadow cannot be judged against the primary alone once that exists.
        rows, _ = laya_shadow._read()
        for r in rows[:30]:
            laya_shadow.record_outcome(r["id"], "250", "telegram")
        with patch.object(laya_shadow, "READY_MIN_ROWS", 200):
            result = laya_shadow.refit()
        self.assertEqual(result["metrics"]["human_labels"], 30)
        self.assertIsNotNone(result["metrics"]["human_accuracy_laya"])


if __name__ == "__main__":
    unittest.main()
