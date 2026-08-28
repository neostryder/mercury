"""Prompt-injection classifier provider.

Mercury sends every message through a classifier before any content reaches
the semantic judge, because that judge is an agentic LLM loop and untrusted
senders can attempt prompt injection through the message body itself. This
module defines the interface that classifier lives behind so a different
classifier can be swapped in without touching app.py.

The one built-in implementation calls a small HTTP contract:

    POST <url>  {"text": "..."}
    -> 200  {"label": "SAFE" | "INJECTION", "score": <float 0..1>}

A reference server implementing that contract for the ProtectAI
deberta-v3-base-prompt-injection-v2 model lives in docs/injection-classifier.md.
Point PROMPT_INJECTION_CLASSIFIER_URL at any host implementing the same
contract - it does not have to be that specific model.
"""
import os
from typing import Protocol

import httpx


class InjectionClassifier(Protocol):
    async def check(self, text: str) -> dict:
        """Return {"label": "SAFE" | "INJECTION", "score": float}."""


class HttpInjectionClassifier:
    def __init__(self, url: str):
        self._url = url

    async def check(self, text: str) -> dict:
        async with httpx.AsyncClient(timeout=10) as client:
            r = await client.post(self._url, json={"text": text})
            r.raise_for_status()
            return r.json()


def get_classifier() -> InjectionClassifier:
    url = os.environ["PROMPT_INJECTION_CLASSIFIER_URL"]
    return HttpInjectionClassifier(url)
