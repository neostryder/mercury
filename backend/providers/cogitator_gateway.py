"""Cogitator gateway endpoints for the services Mercury leans on.

An engine's own port cannot fail over, so a service the Cogitator gateway marks
passthrough is reached through the gateway instead:

    <base>/app/<service>/<path>

with a Cogitator key in the Authorization bearer header. When the node's own
copy cannot load or answer, that gateway relays to the backup nodes. COGITATOR_GATEWAY_URLS
lists the gateway base URLs in the order to try them (the primary node first,
then the others), and COGITATOR_MERCURY_KEY holds the key. With either unset
there are no gateway endpoints and callers use only their direct URLs.
"""
import os


def gateway_bases() -> list[str]:
    raw = os.environ.get("COGITATOR_GATEWAY_URLS", "")
    return [u.strip().rstrip("/") for u in raw.split(",") if u.strip()]


def gateway_headers() -> dict[str, str] | None:
    key = os.environ.get("COGITATOR_MERCURY_KEY", "").strip()
    return {"Authorization": "Bearer " + key} if key else None


def gateway_endpoints(service: str, path: str) -> list[tuple[str, dict[str, str]]]:
    """(url, headers) for each configured gateway, in order. Empty when the
    gateway list or the key is not configured."""
    headers = gateway_headers()
    if not headers:
        return []
    return [(f"{base}/app/{service}/{path.lstrip('/')}", dict(headers)) for base in gateway_bases()]
