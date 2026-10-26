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

A gateway may also offer background jobs for work that takes minutes, which
ask_long() uses when present:

    POST <url>/jobs  {"prompt": "..."}  -> 202 {"job": "<id>"}
    GET  <url>/jobs/<id>  -> 200 {"status": "running"}
                           | {"status": "done", "response": "..."}
                           | {"status": "error", "error": "..."}
                           | {"status": "timeout"}
                           | 404 when it no longer knows the id

To use a different judge entirely (call a hosted model API directly, run a
local model, etc.), implement the Judge protocol below and swap the
selection in get_judge().
"""
import asyncio
import os
from typing import Protocol

import httpx


class JudgeNoAnswer(Exception):
    """The judge took the work but never sent an answer back: the wait ran out,
    the job timed out, or the gateway forgot it. Nothing reported a failure, so
    the work may well have been done."""


class JudgeReportedError(Exception):
    """The judge ran the work and said it failed."""


class Judge(Protocol):
    async def ask(self, prompt: str) -> str:
        """Return the judge's full free-text reply to the prompt."""

    async def ask_long(self, prompt: str) -> str:
        """Like ask(), for work that can run for minutes. Optional: callers
        use getattr and fall back to ask(). Raises JudgeNoAnswer when no answer
        ever arrives and JudgeReportedError when the judge says it failed."""


class HttpAgentGatewayJudge:
    # Kept comfortably above the reference gateway's own AGENT_GATEWAY_TIMEOUT
    # (agent_gateway.py, default 290s) so a slow-but-legitimate agent call
    # gets that gateway's own 504 - reported with useful context - rather
    # than this client giving up first with a bare connection timeout.
    #
    # ask_long() waits for a background job instead, so it is bounded by
    # max_wait: a little over the gateway's own job limit (1500s), so the
    # gateway's "timeout" status arrives before this gives up.
    def __init__(
        self,
        url: str,
        secret: str,
        timeout: float = 300.0,
        max_wait: float = 1620.0,
        poll_interval: float = 5.0,
        max_poll_failures: int = 60,
    ):
        self._url = url
        self._secret = secret
        self._timeout = timeout
        self._max_wait = max_wait
        self._poll_interval = poll_interval
        self._max_poll_failures = max_poll_failures

    async def ask(self, prompt: str) -> str:
        async with httpx.AsyncClient(timeout=self._timeout) as client:
            r = await client.post(
                self._url,
                headers={"X-Gateway-Secret": self._secret},
                json={"prompt": prompt},
            )
            r.raise_for_status()
            return r.json()["response"]

    async def ask_long(self, prompt: str) -> str:
        headers = {"X-Gateway-Secret": self._secret}
        jobs_url = self._url.rstrip("/") + "/jobs"
        async with httpx.AsyncClient(timeout=30.0) as client:
            r = await client.post(jobs_url, headers=headers, json={"prompt": prompt})
            if r.status_code in (404, 405, 501):
                # A gateway without background jobs: the old blocking call.
                return await self.ask(prompt)
            # Anything else that is not an accepted job means nothing started,
            # which is a real failure, so it raises.
            r.raise_for_status()
            job_url = f"{jobs_url}/{r.json()['job']}"

            loop = asyncio.get_running_loop()
            deadline = loop.time() + self._max_wait
            failures = 0
            while loop.time() < deadline:
                await asyncio.sleep(self._poll_interval)
                try:
                    r = await client.get(job_url, headers=headers)
                except httpx.TransportError:
                    # The gateway may be restarting; keep trying for a while.
                    failures += 1
                    if failures >= self._max_poll_failures:
                        raise JudgeNoAnswer("the agent gateway stopped answering")
                    continue
                failures = 0
                if r.status_code == 404:
                    raise JudgeNoAnswer("the agent gateway no longer knows this job")
                r.raise_for_status()
                body = r.json()
                status = body.get("status")
                if status == "running":
                    continue
                if status == "done":
                    return body["response"]
                if status == "timeout":
                    raise JudgeNoAnswer("the agent did not finish within the gateway's job limit")
                detail = body.get("detail") or body.get("error") or status
                raise JudgeReportedError(str(detail))
        raise JudgeNoAnswer("the agent did not report back in time")


def get_judge() -> Judge:
    return HttpAgentGatewayJudge(
        url=os.environ["AGENT_GATEWAY_URL"],
        secret=os.environ["AGENT_GATEWAY_SECRET"],
    )
