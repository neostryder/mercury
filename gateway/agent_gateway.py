#!/usr/bin/env python3
"""Generic HTTP gateway exposing a CLI-based agent to Mercury's backend.

Mercury's semantic-judge step can be backed by any agent that can be driven
from a command line: an agent framework's own CLI, a wrapper around a
hosted model, a local model runner, whatever you already use. Rather than
have the backend (which may run in a container, on a different OS, or
without your agent's own credentials and configuration) invoke that CLI
directly, this gateway runs natively wherever your agent already works and
exposes it over a small authenticated HTTP endpoint the backend calls across
the network. See README.md in this directory for why this indirection
exists at all.

Configure the command that receives the prompt on stdin and must print the
full reply on stdout, via AGENT_CHAT_COMMAND (shell-parsed with shlex). For
example, for a CLI that takes a "read the query from stdin" flag:

    AGENT_CHAT_COMMAND="my-agent chat --quiet --query-file -"

Set AGENT_GATEWAY_SECRET to a random shared secret matching the backend's
own AGENT_GATEWAY_SECRET. This endpoint has no other authentication, so it
is meant to run on a private network, not exposed to the internet.
"""
import json
import os
import shlex
import subprocess
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

PORT = int(os.environ.get("AGENT_GATEWAY_PORT", "8721"))
GATEWAY_SECRET = os.environ["AGENT_GATEWAY_SECRET"]
CHAT_COMMAND = shlex.split(os.environ["AGENT_CHAT_COMMAND"])
TIMEOUT = float(os.environ.get("AGENT_GATEWAY_TIMEOUT", "60"))


class Handler(BaseHTTPRequestHandler):
    def do_POST(self):
        if self.path != "/agent":
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
                CHAT_COMMAND,
                input=prompt,
                text=True,
                capture_output=True,
                timeout=TIMEOUT,
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
    print(f"agent-gateway listening on :{PORT}, path /agent")
    server.serve_forever()
