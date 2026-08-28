"""Semantic-verdict provider.

This is the step that reads the (redacted) message and decides SPAM / PHISH /
LEGIT / UNSURE plus a recommended disposition. Mercury is deliberately
agnostic about what actually produces that judgment - the built-in
implementation calls out to an "agent gateway" over HTTP so any agent
framework can sit behind it (see gateway/agent_gateway.py, and
docs/ARCHITECTURE.md for why a gateway process exists at all).

Contract the gateway must implement:

    POST <url>  {"prompt": "..."}   header: X-Gateway-Secret: <secret>
    -> 200  {"response": "<full text reply>"}

To use a different judge entirely (call a hosted model API directly, run a
local model, etc.), implement the Judge protocol below and swap the
selection in get_judge().
"""
import os
from typing import Protocol

import httpx


class Judge(Protocol):
    async def ask(self, prompt: str) -> str:
        """Return the judge's full free-text reply to the prompt."""


class HttpAgentGatewayJudge:
    def __init__(self, url: str, secret: str, timeout: float = 180.0):
        self._url = url
        self._secret = secret
        self._timeout = timeout

    async def ask(self, prompt: str) -> str:
        async with httpx.AsyncClient(timeout=self._timeout) as client:
            r = await client.post(
                self._url,
                headers={"X-Gateway-Secret": self._secret},
                json={"prompt": prompt},
            )
            r.raise_for_status()
            return r.json()["response"]


def get_judge() -> Judge:
    return HttpAgentGatewayJudge(
        url=os.environ["AGENT_GATEWAY_URL"],
        secret=os.environ["AGENT_GATEWAY_SECRET"],
    )
