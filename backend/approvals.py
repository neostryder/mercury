"""Persisted state for briefs - open-ended collaborations between the
recipient and Loremaster over a flagged message, see telegram_approvals.py
for the conversation loop itself.

A brief holds the full turn history (not just the latest instruction),
because every turn is re-interpreted with that whole history rather than in
isolation - Loremaster needs to see the earlier back-and-forth to reason
about a follow-up the same way a person would. A brief starts open and
either resolves (filtering changes and/or an action got committed, or nothing needed to
be) or stays open awaiting the recipient's answer to a clarifying question.
It is never deleted outright once resolved: a later reply asking about it
(e.g. "why did you do that") should still find it, so `message_index` maps
every message Mercury has ever sent for a brief - not just its first one -
back to that brief's id, and a resolved brief's own follow-up questions are
answered from its full history rather than silently dropped.

Persisted to a small JSON file so a backend restart doesn't strand an
in-flight brief or lose the message-to-brief index a reply depends on.

A brief may carry the original raw message, so a Deliver decision can
append it to the mailbox. That copy is encrypted in the file when the store
has a key, readable only through `get_brief`, and dropped once the brief is
decided, resolved, or older than `RAW_MESSAGE_TTL_SECONDS`. The file itself
is written owner-only.
"""
import json
import os
import secrets
import time
from pathlib import Path

from cryptography.fernet import Fernet, InvalidToken

# A deferred message is retried by the sending side for about five days, so a
# raw copy older than a week can no longer be delivered usefully anyway.
RAW_MESSAGE_TTL_SECONDS = 7 * 24 * 3600


class ApprovalStore:
    def __init__(self, path: Path, key: bytes | None = None):
        """`key` is a Fernet key (see `Fernet.generate_key`). Without one, a
        raw message is kept in plaintext, which only tests should do."""
        self.path = path
        self._fernet = Fernet(key) if key else None

    def _load(self) -> dict:
        if not self.path.exists():
            return {"briefs": {}, "message_index": {}}
        try:
            data = json.loads(self.path.read_text())
        except (json.JSONDecodeError, OSError):
            return {"briefs": {}, "message_index": {}}
        data.setdefault("briefs", {})
        data.setdefault("message_index", {})
        now = time.time()
        for brief in data["briefs"].values():
            metadata = brief.setdefault("message_metadata", {})
            brief.setdefault("changes", [])
            brief.setdefault("message_decision", None)
            brief.setdefault("created_at", 0.0)
            if now - brief["created_at"] > RAW_MESSAGE_TTL_SECONDS:
                metadata.pop("raw_message", None)
                metadata.pop("raw_message_sealed", None)
            elif self._fernet and "raw_message" in metadata:
                # A file written before encryption existed.
                metadata["raw_message_sealed"] = self._seal(metadata.pop("raw_message"))
        return data

    def _save(self, data: dict) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(self.path.suffix + ".tmp")
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(json.dumps(data, indent=2))
        os.replace(tmp, self.path)

    def _seal(self, raw: str) -> str:
        return self._fernet.encrypt(raw.encode("utf-8")).decode("ascii")

    def _open(self, sealed: str) -> str | None:
        try:
            return self._fernet.decrypt(sealed.encode("ascii")).decode("utf-8")
        except (InvalidToken, ValueError):
            return None

    def create_brief(
        self,
        message_context: str,
        via_dictation: bool = False,
        message_metadata: dict | None = None,
    ) -> str:
        brief_id = secrets.token_hex(4)
        data = self._load()
        metadata = dict(message_metadata or {})
        if self._fernet and metadata.get("raw_message"):
            metadata["raw_message_sealed"] = self._seal(metadata.pop("raw_message"))
        elif not metadata.get("raw_message"):
            metadata.pop("raw_message", None)
        data["briefs"][brief_id] = {
            "status": "open",
            "message_context": message_context,
            "via_dictation": via_dictation,
            "message_metadata": metadata,
            "history": [],
            "changes": [],
            "action": None,
            "caveat": None,
            "message_decision": None,
            "rounds": 0,
            "created_at": time.time(),
        }
        self._save(data)
        return brief_id

    def get_brief(self, brief_id: str) -> dict | None:
        """The brief, with any sealed raw message opened as `raw_message`.
        The opened copy lives only in the returned dict."""
        brief = self._load()["briefs"].get(brief_id)
        if brief is None:
            return None
        metadata = brief["message_metadata"]
        sealed = metadata.pop("raw_message_sealed", None)
        if sealed and self._fernet:
            raw = self._open(sealed)
            if raw is not None:
                metadata["raw_message"] = raw
        return brief

    def update_brief(self, brief_id: str, **fields) -> None:
        data = self._load()
        if brief_id in data["briefs"]:
            data["briefs"][brief_id].update(fields)
            self._save(data)

    def append_turn(self, brief_id: str, speaker: str, text: str) -> None:
        data = self._load()
        brief = data["briefs"].get(brief_id)
        if brief is None:
            return
        brief["history"].append({"speaker": speaker, "text": text})
        self._save(data)

    def resolve_brief(self, brief_id: str) -> None:
        data = self._load()
        brief = data["briefs"].get(brief_id)
        if brief is None:
            return
        brief["status"] = "resolved"
        self._drop_raw(brief)
        self._save(data)

    def forget_raw_message(self, brief_id: str) -> None:
        data = self._load()
        brief = data["briefs"].get(brief_id)
        if brief is None:
            return
        self._drop_raw(brief)
        self._save(data)

    @staticmethod
    def _drop_raw(brief: dict) -> None:
        metadata = brief.get("message_metadata", {})
        metadata.pop("raw_message", None)
        metadata.pop("raw_message_sealed", None)

    def track_message(self, message_id: int, brief_id: str) -> None:
        data = self._load()
        data["message_index"][str(message_id)] = brief_id
        self._save(data)

    def brief_for_message(self, message_id: int) -> str | None:
        return self._load()["message_index"].get(str(message_id))

    def most_recent_open_brief(self) -> str | None:
        """The newest still-open brief - the fallback target for a reply that
        does not resolve to any tracked message, e.g. a reply to a much
        older message, or to something Loremaster said rather than Mercury.
        A reply should never just be dropped when there is exactly one open
        thing it could plausibly be about."""
        open_briefs = (
            (brief_id, brief)
            for brief_id, brief in self._load()["briefs"].items()
            if brief.get("status") == "open"
        )
        newest = max(open_briefs, key=lambda pair: pair[1].get("created_at", 0.0), default=None)
        return newest[0] if newest else None
