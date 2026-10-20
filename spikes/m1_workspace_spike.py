#!/usr/bin/env python3
"""M1: probe Codex's native sandbox, Git worktrees, resumption, and concurrency."""

import argparse
import asyncio
import hashlib
from pathlib import Path
import subprocess
import sys
import tempfile

sys.dont_write_bytecode = True

from m0_codex_app_server import AppServer, SpikeError, emit


MODEL_PROVIDER = "openai"
READ_ONLY = {"type": "readOnly", "networkAccess": False}


def git(repo, *args):
    result = subprocess.run(["git", "-C", str(repo), *args],
                           text=True, capture_output=True)
    if result.returncode:
        raise SpikeError(f"git {args[0]} failed (exit {result.returncode})")
    return result.stdout.strip()


def create_repo(root):
    repo = root / "canonical"
    repo.mkdir()
    subprocess.run(["git", "init", "-q", "-b", "main", str(repo)], check=True)
    git(repo, "config", "user.name", "Codex M1 Spike")
    git(repo, "config", "user.email", "codex-m1@example.invalid")
    (repo / "calculator.py").write_text(
        "def add(left, right):\n    return left - right\n")
    (repo / "test_calculator.py").write_text(
        "import unittest\nfrom calculator import add\n\n"
        "class CalculatorTests(unittest.TestCase):\n"
        "    def test_add(self):\n        self.assertEqual(add(2, 3), 5)\n\n"
        "if __name__ == '__main__':\n    unittest.main()\n")
    (repo / ".gitignore").write_text("__pycache__/\n")
    git(repo, "add", ".")
    git(repo, "commit", "-qm", "fixture: intentionally failing calculator test")
    result = subprocess.run([sys.executable, "-B", "-m", "unittest", "discover", "-s", "."],
                            cwd=repo, text=True, capture_output=True)
    if result.returncode == 0 or "AssertionError" not in result.stderr:
        raise SpikeError("Disposable fixture did not fail for its intended assertion")
    return repo


async def connect(args, cwd):
    # Disable the configured server/app/plugin surfaces. The live run still emitted
    # mcpToolCall items, so this is only a partial integration restriction.
    server = AppServer(args.codex, cwd,
        extra_args=["--disable", "apps", "--disable", "plugins",
                    "-c", "mcp_servers={}"],
        stream_text=False)
    try:
        await server.start()
        await server.request("initialize", {"clientInfo": {
            "name": "agent_development_m1", "title": "M1 workspace spike",
            "version": "0.1.0",
        }})
        await server.send({"method": "initialized", "params": {}})
        account = (await server.request("account/read", {
            "refreshToken": False,
        })).get("account")
        if not account or account.get("type") != "chatgpt":
            raise SpikeError("ChatGPT login required: run codex login first")
        emit("auth/ready", type="chatgpt")
        return server
    except BaseException:
        await server.close()
        raise


async def start_thread(server, cwd, sandbox, *, ephemeral=False, model=None):
    params = {"cwd": str(cwd), "modelProvider": MODEL_PROVIDER,
              "sandbox": sandbox, "approvalPolicy": "never",
              "ephemeral": ephemeral}
    if model:
        params["model"] = model
    result = await server.request("thread/start", params)
    thread_id = result["thread"]["id"]
    emit("thread/ready", threadId=thread_id, cwd=str(cwd),
         sandbox=sandbox, ephemeral=ephemeral, model=result.get("model"))
    return thread_id


async def start_turn(server, thread_id, prompt, sandbox_policy=None):
    params = {"threadId": thread_id,
              "input": [{"type": "text", "text": prompt}]}
    if sandbox_policy is not None:
        params["sandboxPolicy"] = sandbox_policy
    result = await server.request("turn/start", params)
    turn_id = result["turn"]["id"]
    emit("turn/accepted", threadId=thread_id, turnId=turn_id)
    return turn_id


async def wait_turn(server, thread_id, turn_id, timeout=120):
    key = (thread_id, turn_id)
    async with asyncio.timeout(timeout):
        while key not in server.completed:
            await server.receive()
    status = server.completed.pop(key)
    text = server.text.pop(key, "")
    emit("turn/result", threadId=thread_id, turnId=turn_id,
         status=status, streamedCharacters=len(text))
    if status != "completed":
        raise SpikeError(f"Turn {turn_id} ended with status {status}")
    return text


async def turn(server, thread_id, prompt, sandbox_policy=None):
    turn_id = await start_turn(server, thread_id, prompt, sandbox_policy)
    return turn_id, await wait_turn(server, thread_id, turn_id)


async def wait_for_command_start(server, thread_id, event_offset, timeout=90):
    """Wait for a real Codex shell item to start, or report a completed no-op."""
    async with asyncio.timeout(timeout):
        while True:
            for stamp, message in server.events[event_offset:]:
                params = message.get("params", {})
                item = params.get("item", {})
                if (message.get("method") == "item/started"
                        and params.get("threadId") == thread_id
                        and item.get("type") == "commandExecution"):
                    return stamp, item.get("id"), item.get("cwd"), item.get("command")
            if any(key[0] == thread_id for key in server.completed):
                raise SpikeError("Turn completed without starting a shell command")
            await server.receive()


def add_worktree(repo, branch, path):
    subprocess.run(["git", "-C", str(repo), "worktree", "add", "-b", branch,
                    str(path), "main"], check=True, capture_output=True, text=True)
    return path


async def run(args):
    with tempfile.TemporaryDirectory(prefix="codex-m1-") as temp:
        root = Path(temp)
        repo = create_repo(root)
        canonical_head = git(repo, "rev-parse", "HEAD")
        canonical_source = hashlib.sha256((repo / "calculator.py").read_bytes()).hexdigest()
        emit("fixture/ready", initialTest="failing", canonicalHead=canonical_head)

        server = None
        thread_id = None
        fix_tree = root / "work-fix"
        fresh_tree = root / "work-fresh"
        try:
            async with asyncio.timeout(args.timeout):
                server = await connect(args, root)

                # A read-only discussion in the canonical fixture.
                read_thread = await start_thread(server, repo, "read-only", ephemeral=True)
                await turn(server, read_thread,
                    "Read calculator.py and test_calculator.py. Explain the test failure "
                    "and point to the relevant lines. Do not run commands or edit files.")
                unchanged = (git(repo, "rev-parse", "HEAD") == canonical_head
                             and git(repo, "status", "--porcelain") == ""
                             and hashlib.sha256((repo / "calculator.py").read_bytes()).hexdigest()
                             == canonical_source)

                # Ask App Server's command executor to prove read-only write denial.
                probe = repo / "sandbox_probe.txt"
                command = [sys.executable, "-B", "-c",
                           "from pathlib import Path; Path('sandbox_probe.txt').write_text('probe')"]
                probe_result = await server.request("command/exec", {
                    "command": command, "cwd": str(repo),
                    "sandboxPolicy": READ_ONLY, "timeoutMs": 10000,
                })
                write_denied = probe_result["exitCode"] != 0 and not probe.exists()
                if probe.exists():
                    probe.unlink()  # Only a disposable file created by this spike.
                emit("sandbox/readOnly", writeDenied=write_denied,
                     exitCode=probe_result["exitCode"], canonicalUnchanged=unchanged)

                # Code-changing work gets a sibling worktree and an explicit writable root.
                add_worktree(repo, "m1/fix", fix_tree)
                thread_id = await start_thread(server, fix_tree, "workspace-write",
                                               model=args.model)
                write_policy = {"type": "workspaceWrite",
                                "writableRoots": [str(fix_tree)],
                                "networkAccess": False}
                await turn(server, thread_id,
                    "Fix calculator.add so it adds its arguments. Run `python3 -B -m "
                    "unittest discover -s .` "
                    "and make the smallest source change needed. Work only in the current "
                    "directory; do not change the test.", write_policy)
                fix_files = git(fix_tree, "diff", "--name-only").splitlines()
                canonical_after_fix = (git(repo, "rev-parse", "HEAD") == canonical_head
                                       and git(repo, "status", "--porcelain") == ""
                                       and hashlib.sha256((repo / "calculator.py").read_bytes()).hexdigest()
                                       == canonical_source)
                fix_tests = subprocess.run(
                    [sys.executable, "-B", "-m", "unittest", "discover", "-s", "."],
                    cwd=fix_tree, text=True, capture_output=True)
                emit("worktree/verified", changedFiles=fix_files,
                     canonicalUnchanged=canonical_after_fix,
                     testExitCode=fix_tests.returncode,
                     branch=git(fix_tree, "branch", "--show-current"))
                if fix_tests.returncode != 0 or "calculator.py" not in fix_files:
                    raise SpikeError("Agent did not fix the worktree test")
                if not canonical_after_fix:
                    raise SpikeError("Canonical fixture changed during worktree execution")

                await turn(server, thread_id,
                    "Follow up in this same thread: summarize the source change and confirm "
                    "the test result. Do not make additional edits.", write_policy)
        finally:
            if server is not None:
                await server.close()

        # Restart App Server, remove the old worktree, and resume the provider thread
        # against a clean worktree from canonical main.
        subprocess.run(["git", "-C", str(repo), "worktree", "remove", "--force",
                        str(fix_tree)], check=True, capture_output=True, text=True)
        add_worktree(repo, "m1/fresh", fresh_tree)
        server = None
        try:
            async with asyncio.timeout(args.timeout):
                server = await connect(args, root)
                resumed = await server.request("thread/resume", {
                    "threadId": thread_id, "excludeTurns": True,
                    "cwd": str(fresh_tree), "sandbox": "workspace-write",
                    "approvalPolicy": "never",
                })
                returned_cwd = resumed.get("cwd")
                emit("thread/resumed", threadId=thread_id, cwd=returned_cwd)
                before = len(server.events)
                await turn(server, thread_id,
                    "We removed the prior worktree and resumed this thread on a new "
                    "worktree based on canonical main. Use the shell to run "
                    "`python3 -B -m unittest discover -s .` and inspect calculator.py in the "
                    "current directory. Do not edit anything. Report whether the prior fix "
                    "exists in this new workspace.",
                    {"type": "workspaceWrite", "writableRoots": [str(fresh_tree)],
                     "networkAccess": False})
                current_command = next((message.get("params", {}).get("item", {})
                    for _, message in server.events[before:]
                    if message.get("method") == "item/started"
                    and message.get("params", {}).get("threadId") == thread_id
                    and message.get("params", {}).get("item", {}).get("type") == "commandExecution"), {})
                current_cwd_observed = current_command.get("cwd") == str(fresh_tree)
                fresh_is_baseline = hashlib.sha256(
                    (fresh_tree / "calculator.py").read_bytes()).hexdigest() == canonical_source
                emit("workspace/reassigned", cwdMatches=(returned_cwd == str(fresh_tree)),
                     cleanBaseline=fresh_is_baseline, shellObserved=current_cwd_observed,
                     testsFail=(subprocess.run(
                         [sys.executable, "-B", "-m", "unittest", "discover", "-s", "."],
                         cwd=fresh_tree, capture_output=True).returncode != 0))

                # Two independent worktrees and turns on the same App Server process.
                tree_a, tree_b = root / "work-a", root / "work-b"
                add_worktree(repo, "m1/concurrent-a", tree_a)
                add_worktree(repo, "m1/concurrent-b", tree_b)
                thread_a = await start_thread(server, tree_a, "workspace-write",
                                              ephemeral=True, model=args.model)
                thread_b = await start_thread(server, tree_b, "workspace-write",
                                              ephemeral=True, model=args.model)
                policy_a = {"type": "workspaceWrite", "writableRoots": [str(tree_a)],
                            "networkAccess": False}
                policy_b = {"type": "workspaceWrite", "writableRoots": [str(tree_b)],
                            "networkAccess": False}
                command_offset = len(server.events)
                turn_a = await start_turn(server, thread_a,
                    "Using the shell tool, run exactly `sleep 12` in the current "
                    "workspace, then reply only A-DONE. Do not edit files or access the "
                    "network.", policy_a)
                start_a, item_a, cwd_a, _ = await wait_for_command_start(
                    server, thread_a, command_offset)
                command_offset = len(server.events)
                turn_b = await start_turn(server, thread_b,
                    "Using the shell tool, run exactly `sleep 12` in the current "
                    "workspace, then reply only B-DONE. Do not edit files or access the "
                    "network.", policy_b)
                start_b, item_b, cwd_b, _ = await wait_for_command_start(
                    server, thread_b, command_offset)
                # One stdout reader demultiplexes both threads. While waiting for A,
                # it also records B's events; never read the pipe from two coroutines.
                await wait_turn(server, thread_a, turn_a)
                await wait_turn(server, thread_b, turn_b)
                completed_at = {}
                for stamp, message in server.events:
                    params = message.get("params", {})
                    item = params.get("item", {})
                    if (message.get("method") == "item/completed"
                            and item.get("id") in (item_a, item_b)):
                        completed_at[item["id"]] = stamp
                overlap = (start_b < completed_at.get(item_a, 0)
                           and start_a < completed_at.get(item_b, 0)
                           and cwd_a == str(tree_a) and cwd_b == str(tree_b))
                emit("concurrency/result", sameAppServer=True,
                     bothToolCommandsStarted=True, overlappingExecution=overlap,
                     threadIds=[thread_a, thread_b])
                if not overlap:
                    raise SpikeError("Two thread turns did not overlap in execution")
        finally:
            if server is not None:
                await server.close()

        emit("m1/result", readOnlyDiscussionPreservedCanonical=unchanged,
             readOnlyWriteDenied=write_denied,
             worktreeIsolated=canonical_after_fix,
             resumedThreadOnNewWorkspace=current_cwd_observed and fresh_is_baseline,
             concurrentThreadsOverlapped=overlap,
             persistentThreadId=thread_id)
        if not unchanged or not write_denied or not current_cwd_observed or not fresh_is_baseline:
            raise SpikeError("One or more M1 safety/workspace checks failed; inspect result events")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--codex", default="codex", help="Codex executable path")
    parser.add_argument("--model", help="Override the configured Codex model")
    parser.add_argument("--timeout", type=float, default=300,
                        help="Deadline for each App Server phase, in seconds")
    args = parser.parse_args()
    if args.timeout <= 0:
        parser.error("--timeout must be positive")
    try:
        asyncio.run(run(args))
    except KeyboardInterrupt:
        return 130
    except TimeoutError:
        print("M1 failed: deadline exceeded; check network and account access", file=sys.stderr)
        return 1
    except asyncio.CancelledError:
        return 143
    except (SpikeError, OSError, subprocess.SubprocessError, KeyError) as exc:
        message = str(exc) if isinstance(exc, SpikeError) else type(exc).__name__
        print(f"M1 failed: {message}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
