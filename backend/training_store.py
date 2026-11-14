"""Local redacted training rows, retained until expiry or recipient review."""
import hashlib
import json
import os
import tempfile
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable


def message_key(received_at: str, from_domain: str | None, subject: str) -> str:
    return hashlib.sha256(f"{received_at}|{from_domain}|{subject}".encode("utf-8")).hexdigest()


def verdict_choice(row: dict, verdict: str, correct_disposition: str | None) -> str:
    if verdict not in ("right", "wrong", "undo"):
        raise ValueError("invalid verdict")
    if correct_disposition not in (None, "250", "421", "550"):
        raise ValueError("invalid correct_disposition")
    if verdict == "wrong" and correct_disposition is None:
        raise ValueError("wrong requires correct_disposition")
    disposition = correct_disposition if verdict == "wrong" else row["decision"]["disposition"]
    if disposition == "250":
        return "LEGIT"
    structured_verdict = ((row.get("structured") or {}).get("verdict") or {}).get("choice")
    if disposition == "421":
        # A hold the recipient confirmed keeps the classifier's block verdict.
        if verdict == "right" and structured_verdict in ("PHISH", "SPAM"):
            return structured_verdict
        return "UNSURE"
    if verdict == "wrong":
        recorded = structured_verdict
    else:
        recorded = row["decision"]["verdict"]
    return "PHISH" if recorded == "PHISH" else "SPAM"


class TrainingStore:
    def __init__(self, path: Path | None = None, clock: Callable[[], float] = time.time):
        self.path = path if path is not None else Path(
            os.environ.get("MERCURY_TRAINING_PATH", "/data/training_rows.json"))
        self.retention_days = float(os.environ.get("MERCURY_TRAINING_RETENTION_DAYS", "30"))
        if not 0 < self.retention_days < float("inf"):
            raise ValueError("training retention must be positive and finite")
        self._clock = clock
        self._lock = threading.RLock()

    def _load(self) -> dict:
        if not self.path.exists():
            return {}
        return json.loads(self.path.read_text(encoding="utf-8"))

    def _save(self, data: dict) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        fd, name = tempfile.mkstemp(prefix=self.path.name + ".", suffix=".tmp", dir=self.path.parent)
        tmp = Path(name)
        try:
            with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as fh:
                json.dump(data, fh, indent=2)
                fh.flush()
                os.fsync(fh.fileno())
            os.replace(tmp, self.path)
        finally:
            tmp.unlink(missing_ok=True)

    def _prune(self, data: dict) -> bool:
        expired = [key for key, row in data.items()
                   if row["label"] is None and row["expires_at"] <= self._clock()]
        for key in expired:
            del data[key]
        return bool(expired)

    def rows(self) -> list[dict]:
        with self._lock:
            data = self._load()
            if self._prune(data):
                self._save(data)
            return list(data.values())

    def capture(self, received_at: str, from_domain: str | None, subject: str,
                email: str, facts: dict, structured: dict | None, decision: dict) -> None:
        with self._lock:
            data = self._load()
            self._prune(data)
            key = message_key(received_at, from_domain, subject)
            now = self._clock()
            if key not in data:
                data[key] = {
                    "key": key,
                    "received_at": received_at,
                    "from_domain": from_domain,
                    "subject": subject,
                    "pilot": "mercury_screen",
                    "state": {"email": email, "facts": {k: v for k, v in facts.items() if k != "detail"}},
                    "structured": structured,
                    "decision": {k: decision[k] for k in
                                 ("verdict", "disposition", "category", "alert", "reasoning")},
                    "stored_at": now,
                    "expires_at": now + self.retention_days * 24 * 60 * 60,
                    "label": None,
                }
            self._save(data)

    def label(self, received_at: str, from_domain: str | None, subject: str,
              verdict: str, correct_disposition: str | None) -> bool:
        with self._lock:
            data = self._load()
            self._prune(data)
            row = data.get(message_key(received_at, from_domain, subject))
            if row is None:
                self._save(data)
                return False
            choice = verdict_choice(row, verdict, correct_disposition)
            row["label"] = None if verdict == "undo" else {
                "verdict": verdict,
                "correct_disposition": correct_disposition,
                "verdict_choice": choice,
            }
            self._prune(data)
            self._save(data)
            return True

    def status(self) -> dict:
        rows = self.rows()
        labeled = [row for row in rows if row["label"] is not None]
        expiries = [row["expires_at"] for row in rows if row["label"] is None]
        return {
            "held_rows_kept": len(rows),
            "labeled_total": len(labeled),
            "labeled_by_class": {choice: sum(row["label"]["verdict_choice"] == choice for row in labeled)
                                 for choice in ("LEGIT", "SPAM", "PHISH", "UNSURE")},
            "oldest_expiry": min(expiries, default=None),
        }

    def export(self, questions: dict) -> str:
        lines = []
        for row in self.rows():
            if row["label"] is None or not row["state"].get("email", "").strip():
                continue
            ts = datetime.fromisoformat(row["received_at"].replace("Z", "+00:00"))
            item = {
                "id": "mercury-" + row["key"][:16],
                "pilot": row["pilot"],
                "ts": ts.astimezone(timezone.utc).isoformat().replace("+00:00", "Z"),
                "state": row["state"],
                "questions": questions,
                "human": {"verdict": row["label"]["verdict_choice"]},
            }
            structured = row.get("structured")
            if structured is not None:
                item["jev"] = {"model": structured.get("model") or "unknown"}
                if structured.get("answers") is not None:
                    item["jev"]["answers"] = structured["answers"]
            lines.append(json.dumps(item))
        return "".join(line + "\n" for line in lines)
