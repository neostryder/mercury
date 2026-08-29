"""Persisted state for briefs - open-ended collaborations between the
recipient and Loremaster over a flagged message, see telegram_approvals.py
for the conversation loop itself.

A brief holds the full turn history (not just the latest instruction),
because every turn is re-interpreted with that whole history rather than in
isolation - Loremaster needs to see the earlier back-and-forth to reason
about a follow-up the same way a person would. A brief starts open and
either resolves (a rule and/or action got committed, or nothing needed to
be) or stays open awaiting the recipient's answer to a clarifying question.
It is never deleted outright once resolved: a later reply asking about it
(e.g. "why did you do that") should still find it, so `message_index` maps
every message Mercury has ever sent for a brief - not just its first one -
back to that brief's id, and a resolved brief's own follow-up questions are
answered from its full history rather than silently dropped.

Persisted to a small JSON file so a backend restart doesn't strand an
in-flight brief or lose the message-to-brief index a reply depends on.
"""
import json
import secrets
from pathlib import Path


class ApprovalStore:
    def __init__(self, path: Path):
        self.path = path

    def _load(self) -> dict:
        if not self.path.exists():
            return {"briefs": {}, "message_index": {}}
        try:
            data = json.loads(self.path.read_text())
        except (json.JSONDecodeError, OSError):
            return {"briefs": {}, "message_index": {}}
        data.setdefault("briefs", {})
        data.setdefault("message_index", {})
        return data

    def _save(self, data: dict) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(json.dumps(data, indent=2))

    def create_brief(self, message_context: str, via_dictation: bool = False) -> str:
        brief_id = secrets.token_hex(4)
        data = self._load()
        data["briefs"][brief_id] = {
            "status": "open",
            "message_context": message_context,
            "via_dictation": via_dictation,
            "history": [],
            "rule": None,
            "action": None,
            "caveat": None,
            "rounds": 0,
        }
        self._save(data)
        return brief_id

    def get_brief(self, brief_id: str) -> dict | None:
        return self._load()["briefs"].get(brief_id)

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
        self.update_brief(brief_id, status="resolved")

    def track_message(self, message_id: int, brief_id: str) -> None:
        data = self._load()
        data["message_index"][str(message_id)] = brief_id
        self._save(data)

    def brief_for_message(self, message_id: int) -> str | None:
        return self._load()["message_index"].get(str(message_id))
