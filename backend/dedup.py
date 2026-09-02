"""Deduplicates repeated /ingest calls for the same underlying message.

ForwardEmail's webhook, or the Worker gate in front of it, can call /ingest
more than once for what is really one message - a retry after a slow
response, or two independent deliveries of the same message. Nothing
upstream of this store keys anything off the message's own identity, so a
repeat call re-ran the classifier, judge, and delivery steps from scratch:
on accept, mail_delivery.deliver_accepted_message appended the raw message
into the mailbox a second time, and because the judge is a live model call,
a repeat was not guaranteed to reach the same disposition as the first call
- a message already delivered could come back recorded as bounced.

Persisted to a small JSON file, same shape as approvals.py, so a backend
restart doesn't reopen the dedup window. A key passes through two states:
"pending" while the first call is still being processed (so a concurrent
duplicate arriving before the first call finishes is caught too, not just a
later retry), then "done" once a disposition is recorded. Entries past
DEDUP_RETENTION_SECONDS are dropped on the next load so the file does not
grow without bound; a mail host retrying long after that window is treated
as a new message rather than matched against stale state.
"""
import json
import time
from pathlib import Path
from typing import Callable

DEDUP_RETENTION_SECONDS = 6 * 60 * 60


class IngestDedupStore:
    def __init__(self, path: Path, clock: Callable[[], float] = time.time):
        self.path = path
        self._clock = clock

    def _load(self) -> dict:
        if not self.path.exists():
            return {"seen": {}}
        try:
            data = json.loads(self.path.read_text())
        except (json.JSONDecodeError, OSError):
            return {"seen": {}}
        data.setdefault("seen", {})
        return data

    def _save(self, data: dict) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(json.dumps(data, indent=2))

    def _prune(self, data: dict, now: float) -> None:
        cutoff = now - DEDUP_RETENTION_SECONDS
        data["seen"] = {
            key: entry for key, entry in data["seen"].items()
            if entry.get("at", 0) >= cutoff
        }

    def claim(self, key: str) -> dict | None:
        """None means this is the first call seen for key - the key is now
        marked pending and the caller should run the pipeline. A non-None
        return is an existing entry (status "pending" or "done") that the
        caller must not re-run the pipeline for."""
        now = self._clock()
        data = self._load()
        self._prune(data, now)
        entry = data["seen"].get(key)
        if entry is not None:
            self._save(data)
            return entry
        data["seen"][key] = {"status": "pending", "at": now}
        self._save(data)
        return None

    def record(self, key: str, disposition: int, content: dict) -> None:
        now = self._clock()
        data = self._load()
        self._prune(data, now)
        data["seen"][key] = {
            "status": "done",
            "at": now,
            "disposition": disposition,
            "content": content,
        }
        self._save(data)

    def release(self, key: str) -> None:
        """Drops a pending claim without recording an outcome - used when the
        pipeline fails before reaching a disposition, so a genuine retry
        isn't stuck matching a pending marker for the rest of the retention
        window."""
        data = self._load()
        data["seen"].pop(key, None)
        self._save(data)
