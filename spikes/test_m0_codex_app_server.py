"""Protocol/lifecycle checks using a local fake process; no model calls or login."""

import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import tempfile
import unittest


SPIKE = Path(__file__).with_name("m0_codex_app_server.py")
FAKE = r'''
import json, os, signal, sys, time
mode = os.environ.get("M0_FAKE_MODE", "success")
def send(value):
    print(json.dumps(value), flush=True)
def event(method, params):
    send({"method": method, "params": params})
turn = 0
for line in sys.stdin:
    msg = json.loads(line)
    method = msg.get("method")
    if "id" not in msg:
        continue
    if mode == "hang":
        signal.signal(signal.SIGTERM, signal.SIG_IGN)
        while True: time.sleep(1)
    if mode == "exit": sys.exit(7)
    if mode == "invalid":
        print("not JSON", flush=True)
        continue
    if method == "initialize":
        # More than pipe capacity: the client must drain stderr concurrently.
        sys.stderr.write("PRIVATE_LOG" * 20000)
        sys.stderr.flush()
        result = {}
    elif method == "account/read":
        result = {"account": {"type": "chatgpt", "email": "PRIVATE_EMAIL"}}
        if mode == "no-auth": result["account"] = None
    elif method == "thread/start":
        assert msg["params"]["sandbox"] == "read-only"
        assert msg["params"]["approvalPolicy"] == "never"
        result = {"thread": {"id": "thread-test"}, "model": "fake", "modelProvider": "openai"}
    elif method == "turn/start":
        assert msg["params"]["threadId"] == "thread-test"
        turn += 1
        if mode == "server-request":
            send({"id": "server-1", "method": "item/commandExecution/requestApproval", "params": {}})
            answer = json.loads(sys.stdin.readline())
            assert answer["id"] == "server-1" and "error" in answer
            continue
        if mode == "rpc-error":
            send({"id": msg["id"], "error": {"code": -32602, "message": "PRIVATE_ERROR"}})
            continue
        item = {"id": str(turn), "status": "failed" if mode == "failed" else "completed"}
        params = {"threadId": "thread-test", "turnId": str(turn), "itemId": "message"}
        text = "ACK" if turn == 1 else "M0-ORBIT-731"
        if mode == "wrong-memory" and turn == 2: text = "wrong"
        for delta in [text[:2], text[2:]]:
            event("item/agentMessage/delta", {**params, "delta": delta})
        # Deliberately send completion BEFORE acknowledging the request.
        event("turn/completed", {"threadId": "thread-test", "turn": item})
        result = {"turn": item}
    else:
        raise AssertionError(method)
    send({"id": msg["id"], "result": result})
'''


class LifecycleTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="m0-test-")
        self.addCleanup(self.temp.cleanup)
        self.fake = Path(self.temp.name) / "fake-codex"
        self.fake.write_text(f"#!{sys.executable}\n" + FAKE)
        self.fake.chmod(0o755)

    def command(self, timeout=10):
        return [sys.executable, str(SPIKE), "--codex", str(self.fake),
                "--timeout", str(timeout)]

    def check_stopped(self, stdout):
        events = [json.loads(line) for line in stdout.splitlines()]
        started = next(e for e in events if e["event"] == "process/started")
        self.assertEqual(events[-1]["event"], "process/stopped")
        # Reaped leader and no remaining process group.
        with self.assertRaises(ProcessLookupError):
            os.killpg(started["pid"], 0)
        return events

    def invoke(self, mode, timeout=10):
        result = subprocess.run(self.command(timeout), capture_output=True, text=True,
                                env={**os.environ, "M0_FAKE_MODE": mode}, timeout=20)
        self.check_stopped(result.stdout)
        self.assertNotIn("PRIVATE_", result.stdout + result.stderr)
        return result

    def test_two_turns_early_completion_and_stderr_backpressure(self):
        result = self.invoke("success")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn('"event": "spike/passed"', result.stdout)
        self.assertIn('"turnIds": ["1", "2"]', result.stdout)

    def test_failures_reap_process(self):
        for mode in ("no-auth", "rpc-error", "failed", "wrong-memory",
                     "server-request", "exit", "invalid"):
            with self.subTest(mode=mode):
                result = self.invoke(mode)
                self.assertEqual(result.returncode, 1, result.stdout)
                self.assertNotIn('"event": "spike/passed"', result.stdout)

    def test_deadline_kills_unresponsive_process(self):
        result = self.invoke("hang", timeout=0.2)
        self.assertEqual(result.returncode, 1)
        self.assertIn("deadline exceeded", result.stderr)

    def test_interrupts_reap_process(self):
        for sig, code in ((signal.SIGINT, 130), (signal.SIGTERM, 143)):
            with self.subTest(signal=sig):
                proc = subprocess.Popen(self.command(), stdout=subprocess.PIPE,
                                        stderr=subprocess.PIPE, text=True,
                                        env={**os.environ, "M0_FAKE_MODE": "hang"})
                try:
                    first = proc.stdout.readline()
                    proc.send_signal(sig)
                    rest, stderr = proc.communicate(timeout=15)
                    self.assertEqual(proc.returncode, code, stderr)
                    self.check_stopped(first + rest)
                finally:
                    if proc.poll() is None:
                        proc.kill()
                        proc.wait()


if __name__ == "__main__":
    unittest.main()
