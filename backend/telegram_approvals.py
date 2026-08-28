"""Two-way approval loop over Telegram for proposed rules and mailbox actions.

This is deliberately not folded into providers/notifier.py: that provider is
a one-way "send an alert" contract, while this needs an actual back-and-forth
conversation (propose, read a reply, maybe revise, repeat) - a different
enough shape that forcing it through the same interface would only obscure
both. It talks to Telegram directly and independently of which Notifier is
configured, because a reply loop needs a channel that can receive messages,
not just send them.

Flow: propose_new() sends a message describing the rule (and any immediate
action) with inline Approve/Discard buttons, and remembers its message_id.
poll_forever() long-polls for button taps (callback_query updates) and plain
replies; a button tap or a "yes"/"no" text reply is read as approve/discard,
anything else is read as feedback to revise the proposal and send a new one.
Approval commits the rule (unless the proposal has none - some actions, like
an outcome-dependent unsubscribe attempt, decide their own rule afterward)
and, if there was an action, hands it off for execution and reports the
outcome back to the same chat. An unsubscribe outcome may itself trigger a
follow-up Blacklist/Greylist/None question, handled the same way.

Reactions (thumbs-up) were tried first but Telegram's Bot API only delivers
message_reaction updates when the bot is an administrator in the chat - a
role that cannot exist in a private 1:1 chat, so it never fires there.
Inline keyboard buttons are the private-chat-compatible equivalent.
"""
import asyncio
import json
import os
import secrets

import httpx

from approvals import ApprovalStore

MAX_ROUNDS = 5
API_BASE = "https://api.telegram.org/bot{token}"

BOUNCE_OPTIONS = {
    "hard": {"label": "Blacklist", "description": "Blacklist (hard bounce)"},
    "soft": {"label": "Greylist", "description": "Greylist (soft bounce)"},
    "none": {"label": "No bounce", "description": "No bounce"},
}


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
        self._bounce_decisions: dict[str, str] = {}  # decision_id -> domain
        self._offset = 0

    async def propose_new(
        self, instruction: str, message_context: str, via_dictation: bool = False
    ) -> tuple[str, str, str | None]:
        rule, action = await self._interpret(instruction, message_context, via_dictation)
        proposal_id = self._store.create(rule, action, message_context)
        await self._send_proposal(proposal_id, rule, action)
        return proposal_id, rule, action

    async def _send_proposal(self, proposal_id: str, rule: str, action: str | None) -> None:
        lines = ["Mercury: rule proposed", f"Rule: {rule}"]
        if action:
            lines.append(f"Also requested right now: {action}")
        lines.append('')
        lines.append('Tap a button, or reply to THIS message with feedback to revise it.')
        keyboard = {
            "inline_keyboard": [[
                {"text": "✅ Approve", "callback_data": f"approve:{proposal_id}"},
                {"text": "❌ Discard", "callback_data": f"discard:{proposal_id}"},
            ]]
        }
        message_id = await self._send(None, "\n".join(lines), keyboard=keyboard)
        if message_id is not None:
            self._message_to_proposal[message_id] = proposal_id

    async def _send(self, proposal_id: str | None, text: str, keyboard: dict | None = None) -> int | None:
        payload = {"chat_id": self._chat_id, "text": text}
        if keyboard:
            payload["reply_markup"] = json.dumps(keyboard)
        async with httpx.AsyncClient(timeout=15) as client:
            try:
                resp = await client.post(f"{self._api}/sendMessage", json=payload)
                resp.raise_for_status()
                return resp.json()["result"]["message_id"]
            except Exception:
                return None

    async def _answer_callback(self, callback_query_id: str, text: str = "") -> None:
        async with httpx.AsyncClient(timeout=15) as client:
            try:
                await client.post(
                    f"{self._api}/answerCallbackQuery",
                    json={"callback_query_id": callback_query_id, "text": text},
                )
            except Exception:
                pass

    async def poll_forever(self) -> None:
        async with httpx.AsyncClient(timeout=40) as client:
            while True:
                try:
                    resp = await client.get(
                        f"{self._api}/getUpdates",
                        params={
                            "offset": self._offset,
                            "timeout": 30,
                            "allowed_updates": json.dumps(["message", "callback_query"]),
                        },
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
        callback = update.get("callback_query")
        if callback:
            await self._handle_callback(callback)
            return

        message = update.get("message")
        if not message:
            return
        reply_to = message.get("reply_to_message")
        if not reply_to:
            return
        reply_text = (message.get("text") or "").strip()
        lowered = reply_text.lower()

        proposal_id = self._message_to_proposal.get(reply_to["message_id"])
        if proposal_id:
            proposal = self._store.get(proposal_id)
            if not proposal:
                return
            if lowered in ("yes", "y", "approve", "approved"):
                await self._approve(proposal_id, proposal)
            elif lowered in ("no", "n", "reject", "rejected", "discard"):
                self._store.discard(proposal_id)
                await self._send(None, f"Discarded: {proposal['rule']}")
            else:
                await self._revise_proposal(proposal_id, proposal, reply_text)
            return

    async def _handle_callback(self, callback: dict) -> None:
        data = callback.get("data", "")
        callback_id = callback["id"]
        kind, _, ref = data.partition(":")

        if kind in ("approve", "discard"):
            proposal_id = ref
            proposal = self._store.get(proposal_id)
            if not proposal:
                await self._answer_callback(callback_id, "Already handled.")
                return
            if kind == "approve":
                await self._answer_callback(callback_id, "Approved")
                await self._approve(proposal_id, proposal)
            else:
                await self._answer_callback(callback_id, "Discarded")
                self._store.discard(proposal_id)
                await self._send(None, f"Discarded: {proposal['rule']}")
            return

        if kind == "bounce":
            disposition, _, decision_id = ref.partition(":")
            domain = self._bounce_decisions.pop(decision_id, None)
            if not domain:
                await self._answer_callback(callback_id, "Already handled.")
                return
            await self._answer_callback(callback_id, "Got it")
            if disposition == "none":
                await self._send(None, f"No bounce rule added for {domain}.")
                return
            label = "hard" if disposition == "hard" else "soft"
            rule = f"Treat all future email from the domain {domain} as a {label} bounce."
            await self._finalize(rule, "bounce_decision")
            await self._send(None, f"Rule added: {rule}")
            return

        await self._answer_callback(callback_id)

    async def ask_bounce_decision(self, domain: str, recommendation: str | None) -> None:
        decision_id = secrets.token_hex(4)
        self._bounce_decisions[decision_id] = domain
        rec = BOUNCE_OPTIONS.get(recommendation, {}).get("description") if recommendation else None
        rec_note = f"\nMy recommendation: {rec}." if rec else ""
        text = f"Add {domain} to a bounce list?{rec_note}"
        keyboard = {
            "inline_keyboard": [[
                {"text": opt["label"], "callback_data": f"bounce:{disposition}:{decision_id}"}
                for disposition, opt in BOUNCE_OPTIONS.items()
            ]]
        }
        await self._send(None, text, keyboard=keyboard)

    async def _approve(self, proposal_id: str, proposal: dict) -> None:
        result_lines = []
        followup = None
        if proposal.get("rule"):
            await self._finalize(proposal["rule"], "rule_proposal")
            result_lines.append(f"Rule added: {proposal['rule']}")
        if proposal.get("action"):
            # The action itself (a real agent call - browsing, mailbox work)
            # can take a while; an instant chat message here matters more
            # than the answerCallbackQuery toast, which is easy to miss.
            await self._send(None, "Approved - working on it now...")
            outcome, followup = await self._execute_action(proposal["action"], proposal["message_context"])
            result_lines.append(outcome)
        if not result_lines:
            result_lines.append("Approved, but there was nothing to add or do.")
        self._store.discard(proposal_id)
        await self._send(None, "\n".join(result_lines))
        if followup and followup.get("kind") == "bounce_decision":
            await self.ask_bounce_decision(followup["domain"], followup.get("recommendation"))

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
