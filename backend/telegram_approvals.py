"""An open-ended, multi-turn brief over Telegram between the recipient and
Loremaster about a flagged message - not a rigid one-shot form.

This is deliberately not folded into providers/notifier.py: that provider is
a one-way "send an alert" contract, while this needs an actual back-and-forth
conversation - a different enough shape that forcing it through the same
interface would only obscure both. It talks to Telegram directly and
independently of which Notifier is configured, because a conversation needs a
channel that can receive messages, not just send them.

Flow: propose_new() opens a brief with the flagged message and the
recipient's instruction, and asks Loremaster to advance it - the result is
either a QUESTION (sent as plain text, brief stays open awaiting an answer)
or a proposed RULE and/or ACTION (sent with Approve/Discard buttons).
poll_forever() long-polls for button taps and replies. Every message Mercury
sends for a brief is tracked back to it - not just the first one - so a
reply to ANY message in that thread (a question, a proposal, an "approved,
working on it", the final outcome) continues the same conversation rather
than going unanswered. Approval or discard resolves the brief (it is never
deleted), so a later reply challenging the outcome still gets answered, via
Loremaster's own judgment over the full history rather than silently
dropped.

Reactions (thumbs-up) were tried first but Telegram's Bot API only delivers
message_reaction updates when the bot is an administrator in the chat - a
role that cannot exist in a private 1:1 chat, so it never fires there.
Inline keyboard buttons are the private-chat-compatible equivalent for the
approve/discard step; free-text reply is how the conversation itself works.
"""
import asyncio
import json
import logging
import os
import secrets
from datetime import datetime, timezone

import httpx

import event_log
from approvals import ApprovalStore

MAX_ROUNDS = 8
API_BASE = "https://api.telegram.org/bot{token}"
TELEGRAM_TEXT_LIMIT = 4000

logger = logging.getLogger(__name__)

BOUNCE_OPTIONS = {
    "hard": {"label": "Blacklist", "description": "Blacklist (hard bounce)"},
    "soft": {"label": "Greylist", "description": "Greylist (soft bounce)"},
    "none": {"label": "No bounce", "description": "No bounce"},
}


class TelegramApprovals:
    def __init__(self, store: ApprovalStore, advance, discuss, finalize, execute_action):
        self._store = store
        self._advance = advance
        self._discuss = discuss
        self._finalize = finalize
        self._execute_action = execute_action
        self._bot_token = os.environ["TELEGRAM_BOT_TOKEN"]
        self._chat_id = os.environ["TELEGRAM_CHAT_ID"]
        self._api = API_BASE.format(token=self._bot_token)
        self._bounce_decisions: dict[str, tuple[str, str]] = {}  # decision_id -> (domain, brief_id)
        self._offset = 0

    async def send_trackable_report(self, text: str, message_context: str) -> None:
        """A per-message verdict report (unlike a pipeline-failure alert) is
        about a specific email the recipient might reasonably want to react
        to - "whitelist this sender", "why was this UNSURE" - so it opens a
        brief the same way a proposal does, rather than firing through the
        one-way Notifier with no way to ever pick up a reply to it."""
        brief_id = self._store.create_brief(message_context)
        self._store.append_turn(brief_id, "loremaster", text)
        message_id = await self._send(text)
        if message_id is not None:
            self._store.track_message(message_id, brief_id)

    async def propose_new(
        self, instruction: str, message_context: str, via_dictation: bool = False
    ) -> tuple[str, str | None, str | None]:
        brief_id = self._store.create_brief(message_context, via_dictation=via_dictation)
        self._store.append_turn(brief_id, "user", instruction)
        result = await self._advance([], message_context, instruction, via_dictation)
        await self._apply_brief_result(brief_id, result)
        brief = self._store.get_brief(brief_id)
        return brief_id, brief["rule"], brief["action"]

    async def _apply_brief_result(self, brief_id: str, result: dict) -> None:
        """Send Loremaster's turn and update brief state - shared by the
        first message in a brief and every reply after it."""
        question, rule, action, caveat = result["question"], result["rule"], result["action"], result["caveat"]
        if question:
            self._store.update_brief(brief_id, rule=None, action=None, caveat=None)
            text = f"❓ {question}"
            self._store.append_turn(brief_id, "loremaster", text)
            message_id = await self._send(text)
        elif rule or action:
            self._store.update_brief(brief_id, rule=rule, action=action, caveat=caveat)
            text = self._proposal_text(rule, action, caveat)
            self._store.append_turn(brief_id, "loremaster", text)
            keyboard = {
                "inline_keyboard": [[
                    {"text": "✅ Approve", "callback_data": f"approve:{brief_id}"},
                    {"text": "❌ Discard", "callback_data": f"discard:{brief_id}"},
                ]]
            }
            message_id = await self._send(
                text + "\n\nTap a button, or reply to THIS message with feedback.", keyboard=keyboard
            )
        else:
            text = "Got it - nothing to add or do."
            self._store.append_turn(brief_id, "loremaster", text)
            self._store.resolve_brief(brief_id)
            message_id = await self._send(text)
        if message_id is not None:
            self._store.track_message(message_id, brief_id)

    def _proposal_text(self, rule: str | None, action: str | None, caveat: str | None) -> str:
        if rule and action:
            lines = ["Mercury: rule + action proposed", f"Rule: {rule}", f"Action: {action}"]
        elif rule:
            lines = ["Mercury: rule proposed", f"Rule: {rule}"]
        else:
            lines = ["Mercury: action proposed", f"Action: {action}"]
        if caveat:
            lines.append(f"\n⚠️ {caveat}")
        return "\n".join(lines)

    async def _send(self, text: str, keyboard: dict | None = None) -> int | None:
        # Telegram rejects a message over ~4096 characters outright - an
        # agent's own report of what it did (e.g. every message it deleted)
        # can easily run longer than that, and an unhandled 400 here used to
        # mean the recipient never found out the action even ran.
        truncated = len(text) > TELEGRAM_TEXT_LIMIT
        payload = {
            "chat_id": self._chat_id,
            "text": text[:TELEGRAM_TEXT_LIMIT] + ("\n... (truncated)" if truncated else ""),
        }
        if keyboard:
            payload["reply_markup"] = json.dumps(keyboard)
        try:
            async with httpx.AsyncClient(timeout=15) as client:
                resp = await client.post(f"{self._api}/sendMessage", json=payload)
                resp.raise_for_status()
                return resp.json()["result"]["message_id"]
        except Exception as exc:
            logger.error("Telegram send failed: %s", exc)
            event_log.log_event("admin_log", {
                "at": datetime.now(timezone.utc).isoformat(),
                "event": "telegram_send_failed",
                "detail": f"{exc}",
            })
            await self._send_fallback_notice()
            return None

    async def _send_fallback_notice(self) -> None:
        # A separate, minimal request rather than a recursive _send() call -
        # it must not depend on the same payload that just failed.
        try:
            async with httpx.AsyncClient(timeout=15) as client:
                await client.post(f"{self._api}/sendMessage", json={
                    "chat_id": self._chat_id,
                    "text": "Mercury could not report back on its last action - check the dashboard's Recent Actions for what happened.",
                })
        except Exception as exc:
            logger.error("Telegram fallback notice also failed: %s", exc)

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
        if not reply_text:
            return

        brief_id = self._store.brief_for_message(reply_to["message_id"])
        if not brief_id:
            return
        brief = self._store.get_brief(brief_id)
        if not brief:
            return

        if brief["status"] == "resolved":
            await self._continue_resolved_brief(brief_id, brief, reply_text)
            return

        lowered = reply_text.lower()
        if (brief.get("rule") or brief.get("action")) and lowered in ("yes", "y", "approve", "approved"):
            await self._approve(brief_id, brief)
            return
        if (brief.get("rule") or brief.get("action")) and lowered in ("no", "n", "reject", "rejected", "discard"):
            await self._discard(brief_id, brief)
            return
        await self._continue_open_brief(brief_id, brief, reply_text)

    async def _continue_open_brief(self, brief_id: str, brief: dict, reply_text: str) -> None:
        rounds = brief.get("rounds", 0) + 1
        if rounds > MAX_ROUNDS:
            self._store.resolve_brief(brief_id)
            await self._send("This brief has gone on a while - discarded for now. Flag the message again to restart it.")
            return
        self._store.append_turn(brief_id, "user", reply_text)
        self._store.update_brief(brief_id, rounds=rounds)
        result = await self._advance(brief["history"], brief["message_context"], reply_text, brief.get("via_dictation", False))
        await self._apply_brief_result(brief_id, result)

    async def _continue_resolved_brief(self, brief_id: str, brief: dict, reply_text: str) -> None:
        outcome = self._proposal_text(brief.get("rule"), brief.get("action"), None) if (brief.get("rule") or brief.get("action")) else "Nothing was added or done."
        self._store.append_turn(brief_id, "user", reply_text)
        answer = await self._discuss(brief["history"], brief["message_context"], outcome, reply_text)
        self._store.append_turn(brief_id, "loremaster", answer)
        message_id = await self._send(answer)
        if message_id is not None:
            self._store.track_message(message_id, brief_id)

    async def _handle_callback(self, callback: dict) -> None:
        data = callback.get("data", "")
        callback_id = callback["id"]
        kind, _, ref = data.partition(":")

        if kind in ("approve", "discard"):
            brief_id = ref
            brief = self._store.get_brief(brief_id)
            if not brief or brief["status"] == "resolved":
                await self._answer_callback(callback_id, "Already handled.")
                return
            if kind == "approve":
                await self._answer_callback(callback_id, "Approved")
                await self._approve(brief_id, brief)
            else:
                await self._answer_callback(callback_id, "Discarded")
                await self._discard(brief_id, brief)
            return

        if kind == "bounce":
            disposition, _, decision_id = ref.partition(":")
            entry = self._bounce_decisions.pop(decision_id, None)
            if not entry:
                await self._answer_callback(callback_id, "Already handled.")
                return
            domain, brief_id = entry
            await self._answer_callback(callback_id, "Got it")
            if disposition == "none":
                text = f"No bounce rule added for {domain}."
                self._store.append_turn(brief_id, "loremaster", text)
                await self._send(text)
                return
            label = "hard" if disposition == "hard" else "soft"
            rule = f"Treat all future email from the domain {domain} as a {label} bounce."
            await self._finalize(rule, "bounce_decision")
            self._store.update_brief(brief_id, rule=rule)
            text = f"Rule added: {rule}"
            self._store.append_turn(brief_id, "loremaster", text)
            await self._send(text)
            return

        await self._answer_callback(callback_id)

    async def ask_bounce_decision(self, brief_id: str, domain: str, recommendation: str | None) -> None:
        decision_id = secrets.token_hex(4)
        self._bounce_decisions[decision_id] = (domain, brief_id)
        rec = BOUNCE_OPTIONS.get(recommendation, {}).get("description") if recommendation else None
        rec_note = f"\nMy recommendation: {rec}." if rec else ""
        text = f"Add {domain} to a bounce list?{rec_note}"
        self._store.append_turn(brief_id, "loremaster", text)
        keyboard = {
            "inline_keyboard": [[
                {"text": opt["label"], "callback_data": f"bounce:{disposition}:{decision_id}"}
                for disposition, opt in BOUNCE_OPTIONS.items()
            ]]
        }
        message_id = await self._send(text, keyboard=keyboard)
        if message_id is not None:
            self._store.track_message(message_id, brief_id)

    async def _approve(self, brief_id: str, brief: dict) -> None:
        result_lines = []
        followup = None
        if brief.get("rule"):
            await self._finalize(brief["rule"], "rule_proposal")
            result_lines.append(f"Rule added: {brief['rule']}")
        if brief.get("action"):
            # The action itself (a real agent call - browsing, mailbox work)
            # can take a while; an instant chat message here matters more
            # than the answerCallbackQuery toast, which is easy to miss.
            await self._send("Approved - working on it now...")
            outcome, followup = await self._execute_action(brief["action"], brief["message_context"])
            result_lines.append(outcome)
        if not result_lines:
            result_lines.append("Approved, but there was nothing to add or do.")
        text = "\n".join(result_lines)
        self._store.append_turn(brief_id, "loremaster", text)
        if not (followup and followup.get("kind") == "bounce_decision"):
            self._store.resolve_brief(brief_id)
        message_id = await self._send(text)
        if message_id is not None:
            self._store.track_message(message_id, brief_id)
        if followup and followup.get("kind") == "bounce_decision":
            await self.ask_bounce_decision(brief_id, followup["domain"], followup.get("recommendation"))

    async def _discard(self, brief_id: str, brief: dict) -> None:
        self._store.resolve_brief(brief_id)
        text = f"Discarded: {brief.get('rule') or brief.get('action')}"
        self._store.append_turn(brief_id, "loremaster", text)
        message_id = await self._send(text)
        if message_id is not None:
            self._store.track_message(message_id, brief_id)
