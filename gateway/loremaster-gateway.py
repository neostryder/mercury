#!/usr/bin/env python3
"""Minimal HTTP shim exposing the Loremaster persona (via `hermes chat -q`)
to processes that cannot invoke the macOS-native hermes CLI directly - a
Docker container on this host runs a different OS and cannot exec it. This
process runs natively on the host for that reason, not containerized.

Stdlib only, deliberately - it must not depend on anything the hermes-agent
venv doesn't already provide.
"""
import json
import subprocess
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

HERMES = str(Path.home() / ".hermes/hermes-agent/venv/bin/hermes")
PORT = 8721
SECRET_PROJECT_ID = "35b23a14-7732-4cbe-afce-b49900759ac1"  # Personal
BWS = str(Path.home() / ".hermes/bin/bws")


def _load_gateway_secret() -> str:
    env_path = Path.home() / ".hermes" / ".env"
    token = None
    for line in env_path.read_text().splitlines():
        if line.startswith("BWS_ACCESS_TOKEN="):
            token = line.split("=", 1)[1].strip()
            break
    if not token:
        raise SystemExit("BWS_ACCESS_TOKEN missing from ~/.hermes/.env")
    result = subprocess.run(
        [BWS, "secret", "list", SECRET_PROJECT_ID, "--output", "json"],
        env={"BWS_ACCESS_TOKEN": token, "PATH": "/usr/bin:/bin"},
        capture_output=True,
        text=True,
        check=True,
    )
    secrets = {s["key"]: s["value"] for s in json.loads(result.stdout)}
    return secrets["LOREMASTER_GATEWAY_SECRET"]


GATEWAY_SECRET = _load_gateway_secret()


class Handler(BaseHTTPRequestHandler):
    def do_POST(self):
        if self.path != "/loremaster":
            self.send_response(404)
            self.end_headers()
            return
        if self.headers.get("X-Gateway-Secret") != GATEWAY_SECRET:
            self.send_response(403)
            self.end_headers()
            return

        length = int(self.headers.get("Content-Length", 0))
        body = self.rfile.read(length)
        try:
            prompt = json.loads(body)["prompt"]
        except Exception:
            self.send_response(400)
            self.end_headers()
            return

        try:
            result = subprocess.run(
                [HERMES, "chat", "-Q", "--query-file", "-"],
                input=prompt,
                text=True,
                capture_output=True,
                timeout=60,
            )
            reply = result.stdout.strip()
        except subprocess.TimeoutExpired:
            self.send_response(504)
            self.end_headers()
            return

        payload = json.dumps({"response": reply}).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def log_message(self, format, *args):
        pass


if __name__ == "__main__":
    server = ThreadingHTTPServer(("0.0.0.0", PORT), Handler)
    print(f"loremaster-gateway listening on :{PORT}")
    server.serve_forever()
