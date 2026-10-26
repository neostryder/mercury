import importlib.util
import json
import os
import sys
import threading
import time
import unittest
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer
from pathlib import Path
from unittest.mock import patch

GATEWAY_PATH = Path(__file__).resolve().parent.parent / "gateway" / "agent_gateway.py"
PYTHON = sys.executable.replace("\\", "/")
# One-line commands for the gateway to run, written so shlex keeps each
# python -c program as a single argument.
ECHO = PYTHON + ' -c "import sys; sys.stdout.write(\'got:\' + sys.stdin.read())"'
FAIL = PYTHON + ' -c "import sys; sys.stderr.write(\'boom\'); sys.exit(3)"'


def sleeper(seconds):
    return PYTHON + f' -c "import time; time.sleep({seconds})"'


def load_gateway(command):
    os.environ["AGENT_GATEWAY_SECRET"] = "s3cret"
    os.environ["AGENT_CHAT_COMMAND"] = command
    spec = importlib.util.spec_from_file_location("agent_gateway_under_test", GATEWAY_PATH)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class GatewayJobTests(unittest.TestCase):
    def serve(self, command):
        gw = load_gateway(command)
        server = ThreadingHTTPServer(("127.0.0.1", 0), gw.Handler)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        self.base = f"http://127.0.0.1:{server.server_address[1]}"
        return gw

    def call(self, method, path, body=None, secret="s3cret"):
        data = json.dumps(body).encode() if body is not None else None
        request = urllib.request.Request(self.base + path, data=data, method=method)
        request.add_header("X-Gateway-Secret", secret)
        try:
            with urllib.request.urlopen(request, timeout=10) as r:
                return r.status, json.loads(r.read() or b"null")
        except urllib.error.HTTPError as e:
            with e:
                return e.code, json.loads(e.read() or b"null")

    def wait_for(self, job_id, timeout=10):
        deadline = time.time() + timeout
        while time.time() < deadline:
            status, body = self.call("GET", f"/agent/jobs/{job_id}")
            if body and body.get("status") != "running":
                return status, body
            time.sleep(0.05)
        self.fail("job never finished")

    def test_the_blocking_endpoint_still_answers(self):
        self.serve(ECHO)

        self.assertEqual(self.call("POST", "/agent", {"prompt": "hi"}), (200, {"response": "got:hi"}))

    def test_a_job_is_accepted_at_once_and_its_answer_is_polled(self):
        self.serve(ECHO)

        status, body = self.call("POST", "/agent/jobs", {"prompt": "hi"})

        self.assertEqual(status, 202)
        self.assertEqual(
            self.wait_for(body["job"]),
            (200, {"status": "done", "response": "got:hi"}),
        )

    def test_a_failing_command_is_an_error_job(self):
        self.serve(FAIL)

        _, body = self.call("POST", "/agent/jobs", {"prompt": "hi"})
        status, result = self.wait_for(body["job"])

        self.assertEqual(status, 200)
        self.assertEqual(result["status"], "error")
        self.assertEqual(result["error"], "agent exited 3")
        self.assertEqual(result["detail"], "boom")

    def test_a_job_past_its_limit_is_a_timeout_job(self):
        gw = self.serve(sleeper(5))

        with patch.object(gw, "JOB_TIMEOUT", 0.3):
            _, body = self.call("POST", "/agent/jobs", {"prompt": "hi"})
            self.assertEqual(self.wait_for(body["job"]), (200, {"status": "timeout"}))

    def test_an_unknown_job_is_404(self):
        self.serve(ECHO)

        self.assertEqual(self.call("GET", "/agent/jobs/nope")[0], 404)

    def test_the_secret_is_required_for_both_calls(self):
        self.serve(ECHO)

        self.assertEqual(self.call("POST", "/agent/jobs", {"prompt": "hi"}, secret="wrong")[0], 403)
        self.assertEqual(self.call("GET", "/agent/jobs/anything", secret="wrong")[0], 403)

    def test_too_many_running_jobs_is_429(self):
        gw = self.serve(sleeper(3))

        with patch.object(gw, "MAX_RUNNING_JOBS", 1):
            self.assertEqual(self.call("POST", "/agent/jobs", {"prompt": "a"})[0], 202)
            self.assertEqual(self.call("POST", "/agent/jobs", {"prompt": "b"})[0], 429)

    def test_a_job_is_forgotten_after_its_ttl(self):
        gw = self.serve(ECHO)

        _, body = self.call("POST", "/agent/jobs", {"prompt": "hi"})
        self.wait_for(body["job"])
        with patch.object(gw, "JOB_TTL", -1):
            self.assertEqual(self.call("GET", f"/agent/jobs/{body['job']}")[0], 404)


if __name__ == "__main__":
    unittest.main()
