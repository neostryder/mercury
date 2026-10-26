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

Two ways to call it. POST /agent holds the connection open until the command
finishes and answers 200 {"response": ...}, which suits a quick judgment.
For a task that can run for minutes (a browsing agent), POST /agent/jobs
answers 202 {"job": "<id>"} at once and runs the command in the background;
GET /agent/jobs/<id> then answers 200 with {"status": "running"},
{"status": "done", "response": ...}, {"status": "error", "error": ...}, or
{"status": "timeout"}, and 404 for an id it does not know (it forgets jobs
after an hour and on restart). Both need the X-Gateway-Secret header.
"""
import hmac
import json
import os
import secrets
import shlex
import subprocess
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

HOST = os.environ.get("AGENT_GATEWAY_HOST", "127.0.0.1")
PORT = int(os.environ.get("AGENT_GATEWAY_PORT", "8721"))
# A judge prompt is a few tens of KB; anything past this is not one.
MAX_BODY = int(os.environ.get("AGENT_GATEWAY_MAX_BODY", str(1024 * 1024)))
GATEWAY_SECRET = os.environ["AGENT_GATEWAY_SECRET"]
CHAT_COMMAND = shlex.split(os.environ["AGENT_CHAT_COMMAND"])
# A real browsing task (a multi-step unsubscribe: check the route, visit it,
# fill a form, confirm) can easily run past a minute. This must stay well
# under the backend's own HttpAgentGatewayJudge timeout (judge.py, 300s), so a
# genuinely slow agent call surfaces here as this 504 - which the backend
# can name as "the agent took too long" - rather than as the backend's own
# blunter connection-level timeout with no such context.
TIMEOUT = float(os.environ.get("AGENT_GATEWAY_TIMEOUT", "290"))
# A background job has no caller waiting on the connection, so it can run far
# longer than a synchronous call. The command is stopped when this runs out.
JOB_TIMEOUT = float(os.environ.get("AGENT_GATEWAY_JOB_TIMEOUT", "1500"))
# How long a finished or running job stays addressable by its id.
JOB_TTL = float(os.environ.get("AGENT_GATEWAY_JOB_TTL", "3600"))
MAX_RUNNING_JOBS = int(os.environ.get("AGENT_GATEWAY_MAX_JOBS", "4"))

_jobs: dict[str, dict] = {}
_jobs_lock = threading.Lock()


def run_agent(prompt: str, timeout: float) -> tuple[int, dict | None]:
    """Runs the chat command once and returns the status and body the
    synchronous endpoint would send."""
    try:
        result = subprocess.run(
            CHAT_COMMAND,
            input=prompt,
            text=True,
            capture_output=True,
            timeout=timeout,
        )
        reply = result.stdout.strip()
    except subprocess.TimeoutExpired:
        return 504, None
    except OSError as exc:
        return 502, {"error": f"agent command could not start: {type(exc).__name__}"}

    if result.returncode != 0:
        detail = result.stderr.strip().splitlines()[-1:] or [""]
        return 502, {"error": f"agent exited {result.returncode}", "detail": detail[0][:300]}
    if not reply:
        return 502, {"error": "agent printed no reply"}
    return 200, {"response": reply}


def _forget_old_jobs(now: float) -> None:
    for job_id in [j for j, v in _jobs.items() if now - v["started"] > JOB_TTL]:
        del _jobs[job_id]


def _run_job(job_id: str, prompt: str) -> None:
    status, body = run_agent(prompt, JOB_TIMEOUT)
    if status == 200:
        outcome = {"status": "done", **body}
    elif status == 504:
        outcome = {"status": "timeout"}
    else:
        outcome = {"status": "error", **body}
    with _jobs_lock:
        if job_id in _jobs:
            _jobs[job_id]["outcome"] = outcome


def start_job(prompt: str) -> str | None:
    """Starts a background job and returns its id, or None when too many are running."""
    now = time.time()
    with _jobs_lock:
        _forget_old_jobs(now)
        if sum(1 for v in _jobs.values() if "outcome" not in v) >= MAX_RUNNING_JOBS:
            return None
        job_id = secrets.token_urlsafe(12)
        _jobs[job_id] = {"started": now}
    threading.Thread(target=_run_job, args=(job_id, prompt), daemon=True).start()
    return job_id


class Handler(BaseHTTPRequestHandler):
    def _reply(self, status: int, data: dict | None = None) -> None:
        payload = json.dumps(data).encode() if data is not None else b""
        self.send_response(status)
        if data is not None:
            self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def _authorized(self) -> bool:
        supplied = self.headers.get("X-Gateway-Secret", "")
        return hmac.compare_digest(supplied.encode(), GATEWAY_SECRET.encode())

    def _read_prompt(self) -> str | None:
        """Reads and validates the request body. Replies with the error and
        returns None when it is not a usable prompt."""
        try:
            length = int(self.headers.get("Content-Length", ""))
        except ValueError:
            self._reply(411)
            return None
        if length < 0:
            self._reply(400)
            return None
        if length > MAX_BODY:
            self._reply(413)
            return None
        body = self.rfile.read(length)
        try:
            prompt = json.loads(body)["prompt"]
            if not isinstance(prompt, str):
                raise TypeError("prompt is not a string")
        except Exception:
            self._reply(400)
            return None
        return prompt

    def do_POST(self):
        if self.path not in ("/agent", "/agent/jobs"):
            return self._reply(404)
        if not self._authorized():
            return self._reply(403)
        prompt = self._read_prompt()
        if prompt is None:
            return

        if self.path == "/agent/jobs":
            job_id = start_job(prompt)
            if job_id is None:
                return self._reply(429, {"error": "too many agent jobs running"})
            return self._reply(202, {"job": job_id})

        status, body = run_agent(prompt, TIMEOUT)
        self._reply(status, body)

    def do_GET(self):
        if not self.path.startswith("/agent/jobs/"):
            return self._reply(404)
        if not self._authorized():
            return self._reply(403)
        job_id = self.path[len("/agent/jobs/"):]
        with _jobs_lock:
            _forget_old_jobs(time.time())
            job = _jobs.get(job_id)
            outcome = job.get("outcome") if job else None
        if job is None:
            return self._reply(404)
        self._reply(200, outcome or {"status": "running"})

    def log_message(self, format, *args):
        pass


if __name__ == "__main__":
    server = ThreadingHTTPServer((HOST, PORT), Handler)
    print(f"agent-gateway listening on {HOST}:{PORT}, path /agent")
    server.serve_forever()
