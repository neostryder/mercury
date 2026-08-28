"""Two-way approval loop over Telegram for proposed rules and mailbox actions.

This is deliberately not folded into providers/notifier.py: that provider is
a one-way "send an alert" contract, while this needs an actual back-and-forth
conversation (propose, read a reply, maybe revise, repeat) - a different
enough shape that forcing it through the same interface would only obscure
both. It talks to Telegram directly and independently of which Notifier is
configured, because a reply loop needs a channel that can receive messages,
not just send them.

Flow: propose() sends a message describing the rule (and any immediate
mailbox action) and remembers its message_id. poll_forever() long-polls for
replies; a reply to that message is read as "yes"/"no", or, for anything
else, as feedback to revise the proposal and send a new one. Approval
commits the rule and, if there was one, hands the action off for execution
and reports the outcome back to the same chat.
"""
import asyncio
import os

import httpx

from approvals import ApprovalStore

MAX_ROUNDS = 5
API_BASE = "https://api.telegram.org/bot{token}"


class TelegramApprovals:
    def __init__(self, store: ApprovalStore, interpret, revise, finalize, execute_action):
        self._store = store
        self._interpret = interpret
        self._revise = revise
        self._finalize = finalize
        self._execute_action = execute_action
        self._bot_token = os.environ["TELEGRAM_BOT_TOKEN"]
        self._chat_id = os.environ["TELEGRAM_CHAT_ID"]
        self._api = API_BASE.format(token=self._bot_token)
        self._message_to_proposal: dict[int, str] = {}
        self._offset = 0

    async def propose_new(self, instruction: str, message_context: str) -> tuple[str, str, str | None]:
        rule, action = await self._interpret(instruction, message_context)
        proposal_id = self._store.create(rule, action, message_context)
        await self._send_proposal(proposal_id, rule, action)
        return proposal_id, rule, action

    async def _send_proposal(self, proposal_id: str, rule: str, action: str | None) -> None:
        lines = ["Mercury: rule proposed", f"Rule: {rule}"]
        if action:
            lines.append(f"Also requested right now: {action}")
        lines.append('')
        lines.append('Reply to THIS message: "yes" to approve, "no" to discard, or anything else to revise it.')
        message_id = await self._send(proposal_id, "\n".join(lines))
        if message_id is not None:
            self._message_to_proposal[message_id] = proposal_id

    async def _send(self, proposal_id: str | None, text: str) -> int | None:
        async with httpx.AsyncClient(timeout=15) as client:
            try:
                resp = await client.post(
                    f"{self._api}/sendMessage",
                    json={"chat_id": self._chat_id, "text": text},
                )
                resp.raise_for_status()
                return resp.json()["result"]["message_id"]
            except Exception:
                return None

    async def poll_forever(self) -> None:
        async with httpx.AsyncClient(timeout=40) as client:
            while True:
                try:
                    resp = await client.get(
                        f"{self._api}/getUpdates",
                        params={"offset": self._offset, "timeout": 30},
                    )
                    resp.raise_for_status()
                    for update in resp.json().get("result", []):
                        self._offset = update["update_id"] + 1
                        await self._handle_update(update)
                except asyncio.CancelledError:
                    raise
                except Exception:
                    await asyncio.sleep(5)

    async def _handle_update(self, update: dict) -> None:
        message = update.get("message")
        if not message:
            return
        reply_to = message.get("reply_to_message")
        if not reply_to:
            return
        proposal_id = self._message_to_proposal.get(reply_to["message_id"])
        if not proposal_id:
            return
        proposal = self._store.get(proposal_id)
        if not proposal:
            return

        reply_text = (message.get("text") or "").strip()
        lowered = reply_text.lower()

        if lowered in ("yes", "y", "approve", "approved"):
            await self._approve(proposal_id, proposal)
        elif lowered in ("no", "n", "reject", "rejected", "discard"):
            self._store.discard(proposal_id)
            await self._send(None, f"Discarded: {proposal['rule']}")
        else:
            await self._revise_proposal(proposal_id, proposal, reply_text)

    async def _approve(self, proposal_id: str, proposal: dict) -> None:
        await self._finalize(proposal["rule"])
        result_lines = [f"Rule added: {proposal['rule']}"]
        if proposal.get("action"):
            outcome = await self._execute_action(proposal["action"], proposal["message_context"])
            result_lines.append(f"Mailbox action result: {outcome}")
        self._store.discard(proposal_id)
        await self._send(None, "\n".join(result_lines))

    async def _revise_proposal(self, proposal_id: str, proposal: dict, feedback: str) -> None:
        rounds = proposal.get("rounds", 0) + 1
        if rounds > MAX_ROUNDS:
            self._store.discard(proposal_id)
            await self._send(None, "Too many rounds of revision - discarded. Flag the message again to restart.")
            return

        rule, action = await self._revise(
            feedback, proposal["rule"], proposal.get("action"), proposal["message_context"]
        )
        self._store.update(proposal_id, rule=rule, action=action, rounds=rounds)
        await self._send_proposal(proposal_id, rule, action)
