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
listens on loopback only unless AGENT_GATEWAY_HOST says otherwise. A backend
in Docker on the same machine reaches it as host.docker.internal.

A command that exits non-zero, or prints nothing, answers 502 rather than an
empty reply, since the backend would otherwise judge an empty reply as mail
to accept.
"""
import hmac
import json
import os
import shlex
import subprocess
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

HOST = os.environ.get("AGENT_GATEWAY_HOST", "127.0.0.1")
PORT = int(os.environ.get("AGENT_GATEWAY_PORT", "8721"))
# A judge prompt is a few tens of KB; anything past this is not one.
MAX_BODY = int(os.environ.get("AGENT_GATEWAY_MAX_BODY", str(1024 * 1024)))
GATEWAY_SECRET = os.environ["AGENT_GATEWAY_SECRET"]
CHAT_COMMAND = shlex.split(os.environ["AGENT_CHAT_COMMAND"])
# A real browsing task (a multi-step unsubscribe: check the route, visit it,
# fill a form, confirm) can easily run past a minute. This must stay well
# under the backend's own HttpAgentGatewayJudge timeout (judge.py), so a
# genuinely slow agent call surfaces here as this 504 - which the backend
# can name as "the agent took too long" - rather than as the backend's own
# blunter connection-level timeout with no such context.
TIMEOUT = float(os.environ.get("AGENT_GATEWAY_TIMEOUT", "240"))


class Handler(BaseHTTPRequestHandler):
    def _reply(self, status: int, data: dict | None = None) -> None:
        payload = json.dumps(data).encode() if data is not None else b""
        self.send_response(status)
        if data is not None:
            self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def do_POST(self):
        if self.path != "/agent":
            return self._reply(404)
        supplied = self.headers.get("X-Gateway-Secret", "")
        if not hmac.compare_digest(supplied.encode(), GATEWAY_SECRET.encode()):
            return self._reply(403)

        try:
            length = int(self.headers.get("Content-Length", ""))
        except ValueError:
            return self._reply(411)
        if length < 0:
            return self._reply(400)
        if length > MAX_BODY:
            return self._reply(413)
        body = self.rfile.read(length)
        try:
            prompt = json.loads(body)["prompt"]
            if not isinstance(prompt, str):
                raise TypeError("prompt is not a string")
        except Exception:
            return self._reply(400)

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
            return self._reply(504)
        except OSError as exc:
            return self._reply(502, {"error": f"agent command could not start: {type(exc).__name__}"})

        if result.returncode != 0:
            detail = result.stderr.strip().splitlines()[-1:] or [""]
            return self._reply(502, {"error": f"agent exited {result.returncode}", "detail": detail[0][:300]})
        if not reply:
            return self._reply(502, {"error": "agent printed no reply"})
        self._reply(200, {"response": reply})

    def log_message(self, format, *args):
        pass


if __name__ == "__main__":
    server = ThreadingHTTPServer((HOST, PORT), Handler)
    print(f"agent-gateway listening on {HOST}:{PORT}, path /agent")
    server.serve_forever()
