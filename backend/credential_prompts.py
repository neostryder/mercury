"""Ephemeral, single-use credentials for approved unsubscribe attempts.

This module deliberately has no persistence path. A credential exists only
between one successful submission and one waiter retrieval, and the entire
entry is discarded after retrieval, timeout, or expiry.
"""

import asyncio
import math
import secrets
import time
from dataclasses import dataclass, field
from typing import Callable


PROMPT_LIFETIME_SECONDS = 600.0


@dataclass
class _PendingCredential:
    domain: str
    event: asyncio.Event = field(default_factory=asyncio.Event)
    expires_at: float = 0.0
    used: bool = False
    username: str | None = None
    password: str | None = None


class CredentialPromptStore:
    def __init__(self, clock: Callable[[], float] = time.monotonic):
        self._clock = clock
        self._entries: dict[str, _PendingCredential] = {}

    def _drop_expired(self, now: float | None = None) -> None:
        if now is None:
            now = self._clock()
        for token, entry in list(self._entries.items()):
            # Once a valid POST has claimed an entry, its waiter owns the
            # cleanup. Expiry gates submission time, not event-loop scheduling
            # after an already accepted submission.
            if not entry.used and entry.expires_at <= now:
                entry.username = None
                entry.password = None
                self._entries.pop(token, None)

    def create(self, domain: str) -> str:
        now = self._clock()
        self._drop_expired(now)
        token = secrets.token_urlsafe(32)
        self._entries[token] = _PendingCredential(
            domain=domain,
            expires_at=now + PROMPT_LIFETIME_SECONDS,
        )
        return token

    def get_status(self, token: str) -> dict | None:
        now = self._clock()
        self._drop_expired(now)
        entry = self._entries.get(token)
        if entry is None or entry.used:
            return None
        return {
            "domain": entry.domain,
            "expires_in_seconds": math.ceil(entry.expires_at - now),
        }

    def submit(self, token: str, username: str, password: str) -> bool:
        self._drop_expired()
        entry = self._entries.get(token)
        if entry is None or entry.used:
            return False
        entry.username = username
        entry.password = password
        entry.used = True
        entry.event.set()
        return True

    async def wait_for(
        self, token: str, timeout_seconds: float
    ) -> tuple[str, str] | None:
        self._drop_expired()
        entry = self._entries.get(token)
        if entry is None:
            return None

        try:
            if not entry.event.is_set():
                remaining = entry.expires_at - self._clock()
                if remaining <= 0:
                    return None
                await asyncio.wait_for(
                    entry.event.wait(),
                    timeout=min(max(timeout_seconds, 0.0), remaining),
                )
            if entry.username is None or entry.password is None:
                return None
            return entry.username, entry.password
        except asyncio.TimeoutError:
            return None
        finally:
            entry.username = None
            entry.password = None
            if self._entries.get(token) is entry:
                self._entries.pop(token, None)


_store = CredentialPromptStore()


def create(domain: str) -> str:
    return _store.create(domain)


def get_status(token: str) -> dict | None:
    return _store.get_status(token)


def submit(token: str, username: str, password: str) -> bool:
    return _store.submit(token, username, password)


async def wait_for(token: str, timeout_seconds: float) -> tuple[str, str] | None:
    return await _store.wait_for(token, timeout_seconds)
