#!/usr/bin/env python3
"""M0: two turns through the installed Codex harness. Python 3.11+, Unix."""

import argparse
import asyncio
import json
import os
import signal
import sys
import tempfile


def emit(event, **fields):
    print(json.dumps({"event": event, **fields}), flush=True)


class SpikeError(Exception):
    pass


class AppServer:
    """One sequential RPC caller; notifications can arrive between any replies."""

    def __init__(self, executable, cwd):
        self.executable = executable
        self.cwd = cwd
        self.proc = None
        self.stderr_task = None
        self.stderr_bytes = 0
        self.next_id = 0
        self.completed = {}
        self.text = {}

    async def start(self):
        self.proc = await asyncio.create_subprocess_exec(
            self.executable, "app-server", "--listen", "stdio://",
            cwd=self.cwd,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            start_new_session=True,  # Own a Unix process group, including children.
            limit=4 * 1024 * 1024,
        )
        self.stderr_task = asyncio.create_task(self.drain_stderr())
        emit("process/started", pid=self.proc.pid)

    async def drain_stderr(self):
        # Drain continuously to prevent a full stderr pipe from blocking Codex.
        # Do not forward arbitrary logs: they can contain private configuration.
        while chunk := await self.proc.stderr.read(8192):
            self.stderr_bytes += len(chunk)

    async def send(self, message):
        self.proc.stdin.write((json.dumps(message) + "\n").encode())
        await self.proc.stdin.drain()

    async def receive(self):
        line = await self.proc.stdout.readline()
        if not line:
            raise SpikeError("App Server closed stdout unexpectedly")
        try:
            message = json.loads(line)
        except (ValueError, UnicodeError):
            raise SpikeError("App Server emitted invalid JSON") from None
        if not isinstance(message, dict):
            raise SpikeError("App Server emitted a non-object message")
        if "method" in message:
            if "id" in message:
                # M0 has no approval/input UI. Never leave a server request hanging.
                await self.send({"id": message["id"], "error": {
                    "code": -32601, "message": "Unsupported by the M0 client",
                }})
                raise SpikeError("Server requested client interaction; M0 stops here")
            self.notification(message)
        return message

    def notification(self, message):
        method = message["method"]
        params = message.get("params", {})
        # Deliberate allowlist: no raw auth/account/config/tool payloads.
        fields = {key: params[key] for key in ("threadId", "turnId", "itemId")
                  if key in params}
        if method == "item/agentMessage/delta":
            fields["delta"] = params["delta"]
            key = (params["threadId"], params["turnId"])
            self.text[key] = self.text.get(key, "") + params["delta"]
        for key in ("turn", "item", "thread"):
            value = params.get(key)
            if isinstance(value, dict):
                fields[key] = {k: value[k] for k in ("id", "type", "status")
                               if k in value}
        if method == "turn/completed":
            turn = params["turn"]
            self.completed[(params["threadId"], turn["id"])] = turn["status"]
        emit(method, **fields)

    async def request(self, method, params):
        self.next_id += 1
        request_id = self.next_id
        await self.send({"id": request_id, "method": method, "params": params})
        while True:
            reply = await self.receive()
            if "method" in reply:
                continue
            if reply.get("id") != request_id:
                raise SpikeError("Unexpected RPC response ID")
            if "error" in reply:
                # Raw error strings can echo request or environment contents.
                code = reply["error"].get("code")
                raise SpikeError(f"{method} rejected (RPC code {code})")
            return reply["result"]

    async def turn(self, thread_id, prompt):
        reply = await self.request("turn/start", {
            "threadId": thread_id, "input": [{"type": "text", "text": prompt}],
        })
        turn_id = reply["turn"]["id"]
        emit("turn/accepted", threadId=thread_id, turnId=turn_id)
        key = (thread_id, turn_id)
        # Completion can precede the RPC reply; the notification handler retains it.
        while key not in self.completed:
            await self.receive()
        status = self.completed.pop(key)
        if status != "completed":
            raise SpikeError(f"Turn ended with status {status}")
        text = self.text.pop(key, "")
        if not text:
            raise SpikeError("Turn completed without streamed agent text")
        return turn_id, text

    def signal_group(self, sig):
        try:
            os.killpg(self.proc.pid, sig)
        except ProcessLookupError:
            pass

    async def close(self):
        if self.proc is None:
            return
        try:
            self.proc.stdin.close()  # EOF is the normal stdio shutdown path.
            try:
                await asyncio.wait_for(self.proc.wait(), 3)
            except TimeoutError:
                self.signal_group(signal.SIGTERM)
                try:
                    await asyncio.wait_for(self.proc.wait(), 2)
                except TimeoutError:
                    self.signal_group(signal.SIGKILL)
                    await self.proc.wait()
        finally:
            # Also remove surviving group members if the leader exited first.
            self.signal_group(signal.SIGKILL)
            if self.stderr_task:
                self.stderr_task.cancel()
                await asyncio.gather(self.stderr_task, return_exceptions=True)
            emit("process/stopped", pid=self.proc.pid,
                 returncode=self.proc.returncode, stderrBytesSuppressed=self.stderr_bytes)


async def run(args):
    # asyncio.run already turns Ctrl-C into cancellation; handle app-quit SIGTERM too.
    task = asyncio.current_task()
    asyncio.get_running_loop().add_signal_handler(signal.SIGTERM, task.cancel)
    # Keep this conversation away from the canonical repository and its instructions.
    with tempfile.TemporaryDirectory(prefix="codex-m0-") as cwd:
        server = AppServer(args.codex, cwd)
        try:
            await server.start()
            async with asyncio.timeout(args.timeout):
                await server.request("initialize", {"clientInfo": {
                    "name": "agent_development_m0", "title": "M0 Python spike",
                    "version": "0.1.0",
                }})
                await server.send({"method": "initialized", "params": {}})
                account = (await server.request("account/read", {
                    "refreshToken": False,
                })).get("account")
                if not account or account.get("type") != "chatgpt":
                    raise SpikeError("ChatGPT login required: run codex login first")
                emit("auth/ready", type=account["type"])
                params = {"cwd": cwd, "modelProvider": "openai",
                          "sandbox": "read-only", "approvalPolicy": "never",
                          "ephemeral": True}
                if args.model:
                    params["model"] = args.model
                thread = await server.request("thread/start", params)
                thread_id = thread["thread"]["id"]
                emit("thread/ready", threadId=thread_id, model=thread["model"],
                     modelProvider=thread["modelProvider"])
                first_id, _ = await server.turn(thread_id,
                    "Remember this marker for our conversation: M0-ORBIT-731. "
                    "Reply only ACK. Do not use tools.")
                second_id, text = await server.turn(thread_id,
                    "What marker did I ask you to remember? Reply only with the marker. "
                    "Do not use tools.")
                if first_id == second_id or text.strip() != "M0-ORBIT-731":
                    raise SpikeError("Two-turn continuity check failed")
                emit("spike/passed", threadId=thread_id, turnIds=[first_id, second_id])
        finally:
            await server.close()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--codex", default="codex", help="Codex executable path")
    parser.add_argument("--model", help="Override the configured Codex model")
    parser.add_argument("--timeout", type=float, default=120,
                        help="Overall protocol/inference deadline in seconds")
    args = parser.parse_args()
    if args.timeout <= 0:
        parser.error("--timeout must be positive")
    try:
        asyncio.run(run(args))
    except KeyboardInterrupt:
        return 130
    except asyncio.CancelledError:
        return 143
    except TimeoutError:
        print("M0 failed: deadline exceeded; check network and account access", file=sys.stderr)
        return 1
    except (SpikeError, OSError) as exc:
        # OS error text can contain paths; RPC errors above contain only safe metadata.
        message = str(exc) if isinstance(exc, SpikeError) else type(exc).__name__
        print(f"M0 failed: {message}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
