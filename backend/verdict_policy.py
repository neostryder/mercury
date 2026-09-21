"""Turn a structured judge's typed answers into a disposition and alert level.

This is the half of the judge prompt that was never really a language problem.
About twenty lines of that prompt teach calibration in prose: when not to use
421, when not to raise an alert, when an unfamiliar sender is still fine. Those
are thresholds. Here they are numbers with names, which means they can be
changed, tested, and disagreed with, and a change does not risk disturbing the
rest of the prompt.

Every threshold is overridable by environment variable so a bad cutoff can be
walked back without a redeploy, matching how MERCURY_SHADOW_MODE already works.

Pure and synchronous on purpose: no network, no clock, no I/O. The tests for
this file are the tests for Mercury's filtering policy.
"""
import os

# A standing rule only wins when the model is actually sure it matched. Below
# this the message falls through to general judgment rather than being routed by
# a rule the model half-recognized: a terse rule scored 0.48 against 0.51 for
# no-match on a message it plainly described, so a low-confidence rule match is
# not evidence.
RULE_CONFIDENCE = float(os.environ.get("MERCURY_RULE_CONFIDENCE", "0.70"))

# Bouncing mail is the irreversible direction, so both bounce paths are gated
# higher than the accept path.
#
# Spam is deliberately NOT gated on severity as well. Severity measures how much
# threat evidence a message carries, and unsolicited bulk mail carries almost
# none: a live run put a CBD marketing blast at SPAM 0.98 with severity 0.8, and
# an earlier version of this file soft-deferred it for failing a severity bar it
# was never going to clear. Unwanted and dangerous are two different axes, and
# only one of them decides whether mail is spam.
PHISH_CONFIDENCE = float(os.environ.get("MERCURY_PHISH_CONFIDENCE", "0.80"))
SPAM_CONFIDENCE = float(os.environ.get("MERCURY_SPAM_CONFIDENCE", "0.80"))

# Accepting is the safe direction and the overwhelming majority of real mail, so
# this is the one threshold where being slightly wrong is cheap.
LEGIT_CONFIDENCE = float(os.environ.get("MERCURY_LEGIT_CONFIDENCE", "0.80"))
LEGIT_SEVERITY_CEILING = float(os.environ.get("MERCURY_LEGIT_SEVERITY_CEILING", "1.0"))

# A signal counts as present above this. Used only for the alert level, never to
# decide a disposition on its own.
SIGNAL_PRESENT = float(os.environ.get("MERCURY_SIGNAL_PRESENT", "0.70"))

INJECTION_PRESENT = float(os.environ.get("MERCURY_INJECTION_PRESENT", "0.70"))

# Alert tuning for soft-defers only. Below this confidence the message is
# genuinely ambiguous and worth the recipient's own eyes today.
AMBIGUOUS_CONFIDENCE = float(os.environ.get("MERCURY_AMBIGUOUS_CONFIDENCE", "0.50"))
# A soft-defer carrying this much threat evidence is urgent even when the
# verdict itself was confident.
URGENT_SEVERITY = float(os.environ.get("MERCURY_URGENT_SEVERITY", "2.0"))


def _conf(answer):
    return (answer or {}).get("confidence", 0.0)


def decide(structured: dict, rules: dict[str, list[str]]) -> dict:
    """Return the same shape judge_email() returns, minus reasoning.

    `structured` is a providers/structured_judge.py result. `rules` is
    policy["semantic_rules"], used to recover a matched rule's text from its id.
    """
    verdict_answer = structured.get("verdict") or {}
    verdict = verdict_answer.get("choice", "UNSURE")
    verdict_conf = _conf(verdict_answer)

    severity = (structured.get("severity") or {}).get("score", 0.0)
    signals = structured.get("signals") or {}

    category_answer = structured.get("category") or {}
    category = category_answer.get("choice", "OTHER")

    triggered_rule = None
    rule_answer = structured.get("matched_rule") or {}
    rule_choice = rule_answer.get("choice", "none")
    disposition = None
    why = None

    if rule_choice and rule_choice != "none" and _conf(rule_answer) >= RULE_CONFIDENCE:
        bucket = (structured.get("rule_ids") or {}).get(rule_choice)
        if bucket:
            # Recover the rule's own text from the id, so what gets stored is the
            # exact policy string rather than anything the model produced.
            try:
                index = int(rule_choice.rsplit("_", 1)[1])
                triggered_rule = rules.get(bucket, [])[index]
                disposition = bucket
                why = "standing rule"
            except (ValueError, IndexError, KeyError):
                triggered_rule = None

    if disposition is None:
        if verdict == "PHISH" and verdict_conf >= PHISH_CONFIDENCE:
            disposition, why = "550", "confident phish"
        elif verdict == "SPAM" and verdict_conf >= SPAM_CONFIDENCE:
            disposition, why = "550", "confident spam"
        elif (verdict == "LEGIT" and verdict_conf >= LEGIT_CONFIDENCE
              and severity < LEGIT_SEVERITY_CEILING):
            disposition, why = "250", "confident legit"
        else:
            # Everything that is not confidently one thing is a soft-defer. This
            # is the case the prompt tried to describe in prose, and it is the
            # one a probability actually settles.
            disposition = "421"
            why = "verdict {} at confidence {:.2f}".format(verdict, verdict_conf)

    injection_signal = signals.get("attempts_injection", 0.0)
    alert = _alert_for(disposition, verdict, verdict_conf, severity,
                       signals, injection_signal)

    return {
        "verdict": verdict,
        "disposition": disposition,
        "category": category,
        "alert": alert,
        "triggered_rule": triggered_rule,
        "why": why,
        "confidence": round(verdict_conf, 4),
        "severity": round(severity, 4),
    }


def _alert_for(disposition, verdict, verdict_conf, severity, signals, injection_signal):
    """Whether this pages Telegram now or waits for the daily summary.

    #49 was the judge's own ALERT level paging for routine accepted mail, fixed
    by adding prompt text telling it not to. Here the rule is structural: an
    accepted message never pages, full stop, and nothing the model says can
    change that.
    """
    if disposition == "250":
        return "NONE"

    if injection_signal >= INJECTION_PRESENT:
        return "URGENT"

    if disposition == "421":
        # A soft-defer always earns a look, because deferred mail is mail the
        # recipient may not get. But not every one is urgent, and treating them
        # all as urgent rebuilds #49's over-paging in a new place: a live run had
        # confidently-classified bulk spam landing at 421 and paging URGENT.
        # Urgent is for the genuinely ambiguous, or for a real threat this was
        # not confident enough to bounce outright.
        if verdict == "UNSURE" or verdict_conf < AMBIGUOUS_CONFIDENCE:
            return "URGENT"
        if severity >= URGENT_SEVERITY:
            return "URGENT"
        return "STANDARD"

    # 550 from here. Most hard bounces are routine and the daily summary covers
    # them; the exception is an active attempt on the recipient's accounts.
    compromise = (signals.get("requests_credentials_or_payment", 0.0) >= SIGNAL_PRESENT
                  and signals.get("impersonates_known_party", 0.0) >= SIGNAL_PRESENT)
    if compromise:
        return "URGENT"
    if verdict == "PHISH":
        return "STANDARD"
    return "NONE"


def describe(decision: dict, structured: dict) -> str:
    """A reasoning line built from the numbers, for when the judge has none.

    The judge writes the reasoning that normally reaches Telegram and the daily
    digest. This exists so that a judge outage degrades to a terse but honest
    sentence rather than to an empty field.
    """
    bits = ["{} at {:.0%} confidence".format(decision["verdict"], decision["confidence"])]
    if decision["triggered_rule"]:
        bits.append('matched the standing rule "{}"'.format(decision["triggered_rule"]))
    else:
        bits.append("no standing rule matched")
    present = sorted(k for k, v in (structured.get("signals") or {}).items()
                     if v >= SIGNAL_PRESENT)
    if present:
        bits.append("signals: " + ", ".join(p.replace("_", " ") for p in present))
    bits.append("threat evidence {:.1f} of 3".format(decision["severity"]))
    return "Structured verdict: " + "; ".join(bits) + "."


def disagreement(llm_verdict: dict, decision: dict) -> dict | None:
    """What the two judges disagreed about, or None when they did not.

    Shadow mode exists to collect exactly these. Agreement is not informative
    and is not worth a row.
    """
    diffs = {}
    for field in ("verdict", "disposition", "category", "alert"):
        if llm_verdict.get(field) != decision.get(field):
            diffs[field] = {"judge": llm_verdict.get(field),
                            "structured": decision.get(field)}
    if (llm_verdict.get("triggered_rule") or None) != (decision.get("triggered_rule") or None):
        diffs["triggered_rule"] = {"judge": llm_verdict.get("triggered_rule"),
                                   "structured": decision.get("triggered_rule")}
    return diffs or None
