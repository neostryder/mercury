"""Shadow log, calibration and failover gate for the second structured judge.

The second backend (LAYA_URL, see providers/structured_judge.py) answers the same
questions as the primary on every message, and decides nothing until this file
says it can. Its raw answers are not usable as they come back: on a plain
credential-phishing probe it said SPAM 0.87 where the primary said PHISH 1.0.
So every message the two both answered becomes a training pair, the primary's
answers are the soft targets, and a calibration is refitted from those pairs on
a schedule.

What is fitted, per question:

  Nouls (the five signals)       Platt scaling, p' = sigmoid(a * logit(p) + b).
  verdict, category, severity    Temperature plus a per-option bias, so
                                 p'_k is proportional to exp(a * log p_k + b_k).
                                 The bias can move the top choice, which is what
                                 a systematic confusion like PHISH read as SPAM
                                 needs. Temperature alone never can.
  matched_rule                   Temperature only. Its options are this
                                 recipient's rules, which differ per message,
                                 so there is no fixed option to bias.

Confidence is recomputed after calibration as one minus the normalized entropy
of the probabilities, which is how the primary itself reports it. The thresholds
in verdict_policy.py therefore mean the same thing whichever backend answered.

Readiness is judged on dispositions, not on questions, because a disposition is
what reaches SMTP. Metrics are out of fold across CV_FOLDS folds: every row is
scored by a fit that never saw it. The file stores typed answers only, never
message content, so it holds nothing the event log does not.
"""
import datetime as dt
import hashlib
import json
import math
import os
from pathlib import Path

import verdict_policy

SHADOW_PATH = Path(os.environ.get("LAYA_SHADOW_PATH", "/data/laya_shadow.jsonl"))
CALIBRATION_PATH = Path(os.environ.get("LAYA_CALIBRATION_PATH", "/data/laya_calibration.json"))
REFIT_SECONDS = float(os.environ.get("LAYA_REFIT_SECONDS", "21600"))
# The newest rows only. Older answers came from older model versions on both
# sides, and fitting time grows with the row count.
MAX_ROWS = int(os.environ.get("LAYA_MAX_ROWS", "3000"))
CV_FOLDS = 5

# A failover backend decides real mail, so the bar is set on what matters:
# the same disposition as the primary nearly always, and almost never a bounce
# of mail the primary would have let through, since a bounce cannot be undone.
READY_MIN_ROWS = int(os.environ.get("LAYA_READY_MIN_ROWS", "200"))
READY_MIN_AGREEMENT = float(os.environ.get("LAYA_READY_MIN_AGREEMENT", "0.95"))
READY_MAX_FALSE_BOUNCE = float(os.environ.get("LAYA_READY_MAX_FALSE_BOUNCE", "0.01"))
# Human labels are the recipient's own Telegram decisions. They are sparse, so
# they only gate once there are enough of them to mean something.
READY_MIN_HUMAN_LABELS = int(os.environ.get("LAYA_READY_MIN_HUMAN_LABELS", "10"))
READY_MAX_HUMAN_GAP = float(os.environ.get("LAYA_READY_MAX_HUMAN_GAP", "0.05"))

FIXED_OPTION_CHOICES = ("verdict", "category", "severity")
EPS = 1e-6
RIDGE = 0.01
ITERATIONS = 200
LEARNING_RATE = 0.05


# ------------------------------------------------------------------ recording

def _now() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat()


def shadow_id(message_key: str) -> str:
    """A short stable id for a message, derived from its dedup key, so the row
    can be found again when the recipient later acts on the message."""
    return hashlib.sha256(message_key.encode("utf-8", errors="replace")).hexdigest()[:24]


def _answers(structured: dict) -> dict:
    return {k: structured.get(k) for k in
            ("verdict", "category", "severity", "matched_rule", "signals", "rule_ids")}


def _append(row: dict) -> None:
    try:
        SHADOW_PATH.parent.mkdir(parents=True, exist_ok=True)
        with SHADOW_PATH.open("a", encoding="utf-8") as f:
            f.write(json.dumps(row) + "\n")
    except OSError:
        # Shadowing is instrumentation. A full disk must not fail mail.
        pass


def record_pair(sid: str | None, primary: dict, laya: dict) -> None:
    if not sid:
        return
    _append({"id": sid, "ts": _now(),
             "primary": _answers(primary), "laya": _answers(laya),
             "primary_model": primary.get("model"), "laya_model": laya.get("model")})


def record_outcome(sid: str | None, disposition: str, source: str) -> None:
    """The disposition the recipient chose for a message, from a Telegram button."""
    if not sid or disposition not in ("250", "421", "550"):
        return
    _append({"outcome_for": sid, "ts": _now(), "disposition": disposition, "source": source})


def _read() -> tuple[list[dict], dict[str, str]]:
    pairs, outcomes = [], {}
    try:
        lines = SHADOW_PATH.read_text(encoding="utf-8").splitlines()
    except OSError:
        return [], {}
    for line in lines:
        try:
            row = json.loads(line)
        except ValueError:
            # A line still being written when the refit read the file.
            continue
        if "outcome_for" in row:
            outcomes[row["outcome_for"]] = row["disposition"]
        elif row.get("primary") and row.get("laya"):
            pairs.append(row)
    return pairs[-MAX_ROWS:], outcomes


# ------------------------------------------------------------------ math

def _log(p):
    return math.log(min(max(p, EPS), 1.0))


def _logit(p):
    p = min(max(p, EPS), 1 - EPS)
    return math.log(p / (1 - p))


def _sigmoid(x):
    return 1 / (1 + math.exp(-x)) if x >= 0 else math.exp(x) / (1 + math.exp(x))


def _softmax(z: dict) -> dict:
    top = max(z.values())
    e = {k: math.exp(v - top) for k, v in z.items()}
    total = sum(e.values())
    return {k: v / total for k, v in e.items()}


def entropy_confidence(probs: dict) -> float:
    """One minus normalized entropy: the primary's own definition of confidence."""
    k = len(probs)
    if k < 2:
        return 1.0
    h = -sum(p * math.log(p) for p in probs.values() if p > 0)
    return max(0.0, 1.0 - h / math.log(k))


class _Adam:
    def __init__(self, params: dict):
        self.p = params
        self.m = {k: 0.0 for k in params}
        self.v = {k: 0.0 for k in params}
        self.t = 0

    def step(self, grad: dict):
        self.t += 1
        for k, g in grad.items():
            self.m[k] = 0.9 * self.m[k] + 0.1 * g
            self.v[k] = 0.999 * self.v[k] + 0.001 * g * g
            mh = self.m[k] / (1 - 0.9 ** self.t)
            vh = self.v[k] / (1 - 0.999 ** self.t)
            self.p[k] -= LEARNING_RATE * mh / (math.sqrt(vh) + 1e-8)


def fit_choice(dists: list[dict], targets: list[dict], with_bias: bool) -> dict:
    """Minimise soft cross-entropy of softmax(a * log p + b) against the targets,
    with a ridge pulling (a, b) toward the identity (1, 0) so a small sample
    cannot fit wildly. Convex in (a, b), so plain Adam converges."""
    options = sorted({k for d in dists for k in d}) if with_bias else []
    params = {"a": 1.0, **{"b:" + k: 0.0 for k in options}}
    opt = _Adam(params)
    n = len(dists)
    for _ in range(ITERATIONS):
        grad = {"a": RIDGE * (params["a"] - 1.0),
                **{"b:" + k: RIDGE * params["b:" + k] for k in options}}
        for p, y in zip(dists, targets):
            x = {k: _log(v) for k, v in p.items()}
            q = _softmax({k: params["a"] * x[k] + params.get("b:" + k, 0.0) for k in x})
            for k in x:
                d = (q[k] - y.get(k, 0.0)) / n
                grad["a"] += d * x[k]
                if with_bias:
                    grad["b:" + k] += d
        opt.step(grad)
    return {"a": round(params["a"], 4),
            "b": {k: round(params["b:" + k], 4) for k in options}}


def fit_noul(ps: list[float], targets: list[float]) -> dict:
    params = {"a": 1.0, "b": 0.0}
    opt = _Adam(params)
    n = len(ps)
    xs = [_logit(p) for p in ps]
    for _ in range(ITERATIONS):
        grad = {"a": RIDGE * (params["a"] - 1.0), "b": RIDGE * params["b"]}
        for x, y in zip(xs, targets):
            d = (_sigmoid(params["a"] * x + params["b"]) - y) / n
            grad["a"] += d * x
            grad["b"] += d
        opt.step(grad)
    return {"a": round(params["a"], 4), "b": round(params["b"], 4)}


# ------------------------------------------------------------------ applying

def _apply_choice(answer: dict, fit: dict | None, is_score: bool) -> dict:
    probs = answer.get("probabilities")
    if not fit or not probs:
        return answer
    z = {k: fit["a"] * _log(v) + fit.get("b", {}).get(k, 0.0) for k, v in probs.items()}
    q = _softmax(z)
    out = dict(answer)
    out["probabilities"] = q
    out["confidence"] = entropy_confidence(q)
    if is_score:
        out["score"] = sum(int(k) * v for k, v in q.items())
    else:
        out["choice"] = max(q, key=q.get)
    return out


def apply(structured: dict, calibration: dict | None) -> dict:
    """The structured result with every calibrated question rescaled.

    Questions with no fit yet pass through unchanged, which is why readiness is
    judged on the output of this function rather than on any one question.
    """
    fits = (calibration or {}).get("fits") or {}
    out = dict(structured)
    for q in FIXED_OPTION_CHOICES + ("matched_rule",):
        if structured.get(q):
            out[q] = _apply_choice(structured[q], fits.get(q), q == "severity")
    signals = dict(structured.get("signals") or {})
    for name, p in signals.items():
        f = fits.get("signal:" + name)
        if f:
            signals[name] = _sigmoid(f["a"] * _logit(p) + f["b"])
    out["signals"] = signals
    out["calibrated"] = bool(fits)
    return out


def _rules_for(rule_ids: dict | None) -> dict[str, list[str]]:
    """Placeholder rule lists long enough for verdict_policy.decide() to recover
    a bucket from a rule id. Only the disposition matters for scoring."""
    rules = {"550": [], "421": [], "250": []}
    for rid, bucket in (rule_ids or {}).items():
        try:
            index = int(rid.rsplit("_", 1)[1])
        except (ValueError, IndexError):
            continue
        lst = rules.setdefault(bucket, [])
        while len(lst) <= index:
            lst.append(rid)
    return rules


def disposition_of(answers: dict) -> str:
    return verdict_policy.decide(answers, _rules_for(answers.get("rule_ids")))["disposition"]


# ------------------------------------------------------------------ fitting

def _fit_all(rows: list[dict]) -> dict:
    fits = {}
    for q in FIXED_OPTION_CHOICES + ("matched_rule",):
        dists, targets = [], []
        for r in rows:
            la, pa = (r["laya"].get(q) or {}), (r["primary"].get(q) or {})
            if la.get("probabilities") and pa.get("probabilities"):
                dists.append({str(k): v for k, v in la["probabilities"].items()})
                targets.append({str(k): v for k, v in pa["probabilities"].items()})
        if len(dists) >= 5:
            fits[q] = fit_choice(dists, targets, with_bias=q in FIXED_OPTION_CHOICES)
    names = {n for r in rows for n in (r["laya"].get("signals") or {})}
    for name in sorted(names):
        ps, ys = [], []
        for r in rows:
            lp = (r["laya"].get("signals") or {}).get(name)
            pp = (r["primary"].get("signals") or {}).get(name)
            if lp is not None and pp is not None:
                ps.append(lp)
                ys.append(pp)
        if len(ps) >= 5:
            fits["signal:" + name] = fit_noul(ps, ys)
    return fits


def _fold(row_id: str) -> int:
    return int(hashlib.sha1(row_id.encode()).hexdigest(), 16) % CV_FOLDS


def _agreement(pairs: list[tuple[dict, dict]], q: str) -> float | None:
    hits = total = 0
    for lay, pri in pairs:
        if q.startswith("signal:"):
            name = q.split(":", 1)[1]
            lp = (lay.get("signals") or {}).get(name)
            pp = (pri.get("signals") or {}).get(name)
            if lp is None or pp is None:
                continue
            hits += (lp >= 0.5) == (pp >= 0.5)
        else:
            la, pa = lay.get(q) or {}, pri.get(q) or {}
            key = "score" if q == "severity" else "choice"
            if la.get(key) is None or pa.get(key) is None:
                continue
            if q == "severity":
                hits += round(la["score"]) == round(pa["score"])
            else:
                hits += la["choice"] == pa["choice"]
        total += 1
    return round(hits / total, 4) if total else None


def refit() -> dict:
    """Fit from the shadow file, score out of fold, write the calibration."""
    rows, outcomes = _read()
    result = {"generated": _now(), "rows": len(rows), "fits": {}, "metrics": {},
              "ready": False, "why_not": []}
    if len(rows) < 5:
        result["why_not"].append("fewer than 5 paired rows")
        _write(result)
        return result

    folds = [[r for r in rows if _fold(r["id"]) == k] for k in range(CV_FOLDS)]
    held = []  # (row, calibrated laya answers) scored by a fit that never saw the row
    for k in range(CV_FOLDS):
        train = [r for j, fold in enumerate(folds) if j != k for r in fold]
        if len(train) < 5 or not folds[k]:
            continue
        fits = _fit_all(train)
        held += [(r, apply(r["laya"], {"fits": fits})) for r in folds[k]]

    questions = list(FIXED_OPTION_CHOICES) + ["matched_rule"] + sorted(
        "signal:" + n for n in {n for r in rows for n in (r["laya"].get("signals") or {})})
    per_question = {}
    for q in questions:
        per_question[q] = {
            "agreement_before": _agreement([(r["laya"], r["primary"]) for r, _ in held], q),
            "agreement_after": _agreement([(cal, r["primary"]) for r, cal in held], q),
        }

    agree = false_bounce = 0
    human = {"n": 0, "primary_correct": 0, "laya_correct": 0}
    for r, cal in held:
        primary_disp = disposition_of(r["primary"])
        laya_disp = disposition_of(cal)
        agree += laya_disp == primary_disp
        false_bounce += laya_disp == "550" and primary_disp != "550"
        label = outcomes.get(r["id"])
        if label:
            human["n"] += 1
            human["primary_correct"] += primary_disp == label
            human["laya_correct"] += laya_disp == label
    n_held = len(held)
    metrics = {
        "held_out_rows": n_held,
        "disposition_agreement": round(agree / n_held, 4) if n_held else None,
        "false_bounce_rate": round(false_bounce / n_held, 4) if n_held else None,
        "human_labels": human["n"],
        "human_accuracy_primary": round(human["primary_correct"] / human["n"], 4) if human["n"] else None,
        "human_accuracy_laya": round(human["laya_correct"] / human["n"], 4) if human["n"] else None,
        "questions": per_question,
    }

    why_not = []
    if len(rows) < READY_MIN_ROWS:
        why_not.append("{} of {} paired rows".format(len(rows), READY_MIN_ROWS))
    if (metrics["disposition_agreement"] or 0) < READY_MIN_AGREEMENT:
        why_not.append("disposition agreement {} below {}".format(
            metrics["disposition_agreement"], READY_MIN_AGREEMENT))
    if metrics["false_bounce_rate"] is None or metrics["false_bounce_rate"] > READY_MAX_FALSE_BOUNCE:
        why_not.append("false-bounce rate {} above {}".format(
            metrics["false_bounce_rate"], READY_MAX_FALSE_BOUNCE))
    if human["n"] >= READY_MIN_HUMAN_LABELS:
        gap = metrics["human_accuracy_primary"] - metrics["human_accuracy_laya"]
        if gap > READY_MAX_HUMAN_GAP:
            why_not.append("human-label accuracy {:.2f} behind the primary".format(gap))

    result.update({"fits": _fit_all(rows), "metrics": metrics,
                   "ready": not why_not, "why_not": why_not})
    _write(result)
    return result


def _write(result: dict) -> None:
    global _cached
    try:
        CALIBRATION_PATH.parent.mkdir(parents=True, exist_ok=True)
        tmp = CALIBRATION_PATH.with_suffix(".tmp")
        tmp.write_text(json.dumps(result, indent=1), encoding="utf-8")
        tmp.replace(CALIBRATION_PATH)
    except OSError:
        pass
    _cached = result


_cached: dict | None = None


def load() -> dict | None:
    global _cached
    if _cached is None:
        try:
            _cached = json.loads(CALIBRATION_PATH.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None
    return _cached


def failover(laya_answers: dict | None) -> dict | None:
    """Calibrated answers fit to decide with, or None while not ready."""
    calibration = load()
    if laya_answers is None or not calibration or not calibration.get("ready"):
        return None
    return apply(laya_answers, calibration)


def _refit_quietly() -> None:
    refit()


async def run_forever() -> None:
    """Refit at startup and then every REFIT_SECONDS.

    A full refit is tens of seconds of pure-Python arithmetic (43 s measured on
    3,000 rows). In a thread it would hold the GIL that long and slow every
    webhook arriving meanwhile, so it runs in a separate spawned process and
    this one only rereads the file it wrote.
    """
    import asyncio
    import concurrent.futures
    import multiprocessing
    global _cached
    context = multiprocessing.get_context("spawn")
    while True:
        try:
            with concurrent.futures.ProcessPoolExecutor(1, mp_context=context) as pool:
                await asyncio.get_running_loop().run_in_executor(pool, _refit_quietly)
            _cached = None
        except Exception:                                     # noqa: BLE001
            pass
        await asyncio.sleep(REFIT_SECONDS)
