"""Client for the loremaster-gateway shim running natively on the gamemaster
host (not in Docker - it shells out to the hermes CLI, which is a macOS
venv binary that cannot run inside a Linux container). See
~/.hermes/scripts/loremaster-gateway.py on gamemaster for the server side.
"""
import os

import httpx

LOREMASTER_GATEWAY_URL = os.environ.get(
    "LOREMASTER_GATEWAY_URL", "http://192.168.2.152:8721/loremaster"
)
LOREMASTER_GATEWAY_SECRET = os.environ["LOREMASTER_GATEWAY_SECRET"]


async def ask_loremaster(prompt: str, timeout: float = 45.0) -> str:
    async with httpx.AsyncClient(timeout=timeout) as client:
        r = await client.post(
            LOREMASTER_GATEWAY_URL,
            headers={"X-Gateway-Secret": LOREMASTER_GATEWAY_SECRET},
            json={"prompt": prompt},
        )
        r.raise_for_status()
        return r.json()["response"]
