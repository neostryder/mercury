"""Fire-and-forget logging into Mercury's D1-backed event log.

The backend has no Cloudflare credentials of its own - it reaches D1 through
an authenticated /log route on the Worker gate (worker/src/index.js), which
already holds the binding. Logging must never affect the request it's
logging: log_event() schedules the write and returns immediately rather than
being awaited, and any failure (network, Worker down, bad data) is swallowed
here rather than surfaced to the caller.
"""
import os

import httpx

WORKER_LOG_URL = os.environ.get("MERCURY_WORKER_LOG_URL")
SHARED_SECRET = os.environ["MERCURY_SHARED_SECRET"]


def log_event(table: str, fields: dict) -> None:
    """Schedule a best-effort insert. Must be called from within a running
    event loop (every call site in this codebase is inside an async request
    handler)."""
    import asyncio

    if not WORKER_LOG_URL:
        return
    asyncio.create_task(_post(table, fields))


async def _post(table: str, fields: dict) -> None:
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            await client.post(
                WORKER_LOG_URL,
                json={"table": table, "fields": fields},
                headers={"X-Mercury-Secret": SHARED_SECRET},
            )
    except Exception:
        pass
