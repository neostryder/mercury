"""Prompt-injection classifier provider.

Mercury sends every message through a classifier before any content reaches
the semantic judge, because that judge is an agentic LLM loop and untrusted
senders can attempt prompt injection through the message body itself. This
module defines the interface that classifier lives behind so a different
classifier can be swapped in without touching app.py.

The one built-in implementation calls a small HTTP contract:

    POST <url>  {"text": "..."}
    -> 200  {"label": "SAFE" | "INJECTION", "score": <float 0..1>}

A reference server implementing that contract for Meta's
Llama-Prompt-Guard-2-86M model lives in docs/injection-classifier.md.
Point PROMPT_INJECTION_CLASSIFIER_URL at any host implementing the same
contract - it does not have to be that specific model.
"""
import logging
import os
import time
from typing import Protocol

import httpx

from providers.cogitator_gateway import gateway_endpoints

logger = logging.getLogger(__name__)

# One slow or dead address must not hold up a message for long: the connection
# is given a few seconds, the answer ten.
TIMEOUT = httpx.Timeout(10.0, connect=3.0)
# An address that refused a connection is not tried again for this long unless
# every address is in that state.
DOWN_SECONDS = 30.0


class InjectionClassifier(Protocol):
    async def check(self, text: str) -> dict:
        """Return {"label": "SAFE" | "INJECTION", "score": float}."""


class HttpInjectionClassifier:
    """Tries an ordered list of endpoints. A connection error, a timeout, or a
    5xx answer moves to the next one; a 4xx answer means the request itself is
    wrong, so it is raised without trying another. When every endpoint fails the
    last error is raised, as it was with a single URL."""

    def __init__(self, endpoints: list[tuple[str, dict[str, str]]] | str,
                 transport: httpx.AsyncBaseTransport | None = None, clock=time.monotonic):
        if isinstance(endpoints, str):
            endpoints = [(endpoints, {})]
        self._endpoints = endpoints
        self._transport = transport
        self._clock = clock
        self._down: dict[str, float] = {}

    def _candidates(self) -> list[tuple[str, dict[str, str]]]:
        now = self._clock()
        live = [e for e in self._endpoints if self._down.get(e[0], 0.0) <= now]
        return live or list(self._endpoints)

    async def check(self, text: str) -> dict:
        last: Exception | None = None
        async with httpx.AsyncClient(timeout=TIMEOUT, transport=self._transport) as client:
            for url, headers in self._candidates():
                try:
                    r = await client.post(url, json={"text": text}, headers=headers)
                except httpx.HTTPError as exc:
                    self._down[url] = self._clock() + DOWN_SECONDS
                    logger.warning("prompt-injection classifier at %s failed (%s); trying the next endpoint",
                                   url, type(exc).__name__)
                    last = exc
                    continue
                # A gateway that does not know the service yet (404) or does not accept the
                # key (401, 403) is a setup gap, not a bad request: the next endpoint,
                # ending at the direct URL, still gets its turn.
                setup_gap = bool(headers) and r.status_code in (401, 403, 404)
                if r.status_code >= 500 or setup_gap:
                    self._down[url] = self._clock() + DOWN_SECONDS
                    logger.warning("prompt-injection classifier at %s answered %s; trying the next endpoint",
                                   url, r.status_code)
                    last = httpx.HTTPStatusError(f"HTTP {r.status_code}", request=r.request, response=r)
                    continue
                r.raise_for_status()
                return r.json()
        assert last is not None
        raise last


def get_classifier() -> InjectionClassifier:
    """The gateway endpoints first (when COGITATOR_GATEWAY_URLS and
    COGITATOR_MERCURY_KEY are set), then the direct URL as the last fallback.
    PROMPT_INJECTION_CLASSIFIER_URL may itself list several URLs separated by commas."""
    direct = [(u.strip(), {}) for u in os.environ["PROMPT_INJECTION_CLASSIFIER_URL"].split(",") if u.strip()]
    return HttpInjectionClassifier(gateway_endpoints("prompt-guard", "classify") + direct)
