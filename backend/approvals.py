"""Pending rule/action proposals awaiting the recipient's approval.

A proposal is created when a flagged instruction is interpreted into a rule
(and, possibly, an immediate mailbox action) and isn't committed until the
recipient approves it - see telegram_approvals.py for the approval flow
itself. Persisted to a small JSON file so a backend restart doesn't strand
an in-flight proposal.
"""
import json
import secrets
from pathlib import Path


class ApprovalStore:
    def __init__(self, path: Path):
        self.path = path

    def _load(self) -> dict:
        if not self.path.exists():
            return {}
        try:
            return json.loads(self.path.read_text())
        except (json.JSONDecodeError, OSError):
            return {}

    def _save(self, data: dict) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(json.dumps(data, indent=2))

    def create(self, rule: str, action: str | None, message_context: str) -> str:
        proposal_id = secrets.token_hex(4)
        data = self._load()
        data[proposal_id] = {
            "rule": rule,
            "action": action,
            "message_context": message_context,
            "rounds": 0,
        }
        self._save(data)
        return proposal_id

    def get(self, proposal_id: str) -> dict | None:
        return self._load().get(proposal_id)

    def update(self, proposal_id: str, **fields) -> None:
        data = self._load()
        if proposal_id in data:
            data[proposal_id].update(fields)
            self._save(data)

    def discard(self, proposal_id: str) -> None:
        data = self._load()
        data.pop(proposal_id, None)
        self._save(data)
