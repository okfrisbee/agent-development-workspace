# M1 — Native sandbox and Git worktree spike

M1 tests a deliberately tiny Git repository created under the operating system's
temporary directory. It does not touch the project checkout. Run it with:

```bash
python3 -B spikes/m1_workspace_spike.py
```

It uses the existing Codex login and configured model for six short model turns,
plus one sandboxed write probe. The run spends account usage. Prerequisites are
Python 3.11+, Git, Codex on `PATH`, and a ChatGPT login with model access. Allow
several minutes for completion. `--model` and `--timeout` can override defaults.

## Protocol and policy choices

- The installed CLI (`0.162.0-alpha.17.2`) generated the protocol schema used by
  the spike. Its legacy `thread/start.sandbox` field expects `workspace-write`,
  while the current [App Server documentation](https://learn.chatgpt.com/docs/app-server)
  example uses `workspaceWrite`. The newer `turn/start.sandboxPolicy` shape is a
  tagged object, such as `{"type":"workspaceWrite","writableRoots":[...]}`.
  The spike uses each shape supported by the installed version; the mismatch does
  not require an architecture change.
- A read-only thread inspects the canonical fixture. App Server's `command/exec`
  write attempt uses `sandboxPolicy: {"type":"readOnly","networkAccess":false}`.
  It must return a nonzero exit code and leave the target absent.
- A code-changing thread uses `sandboxPolicy: {"type":"workspaceWrite",
  "writableRoots":[worktree],"networkAccess":false}`. Git creates a sibling
  worktree from the canonical `main`; the Codex turn edits and tests there.
- `approvalPolicy: "never"` avoids interactive approval dialogs. It does not
  bypass the native sandbox; the read-only probe was still blocked.
- The test runs use a single App Server process for both concurrency threads. One
  Python coroutine reads stdout and dispatches notifications into per-thread
  completion state. Do not have multiple coroutines read the same pipe. The test
  starts both shell commands and compares their actual command item start and
  completion events to prove overlap.
- One persistent provider thread is resumed after App Server restarts. After the
  original worktree is removed, `thread/resume` accepts the new `cwd` and continues
  the same conversation. Its new workspace begins at canonical `main`; the prior
  uncommitted fix is absent. Provider history and filesystem state must be managed
  separately. Preserve a worktree or save its changes before removing it.
- The Codex process inherits the user's normal installation and auth. The run also
  passes `--disable apps`, `--disable plugins`, and `mcp_servers={}` to reduce
  inherited integrations. The server still emitted `mcpToolCall` items in the
  test turn. Their destinations are not exposed in this spike's filtered event
  output, so these overrides are **not proven to disable all MCP activity**. The
  fixture and prompts contain only synthetic code. Do not treat this result as a
  guarantee about integrations, network side channels, or hostile repositories.
- The Python event buffer keeps only item IDs, types, working directories, and
  statuses needed for the concurrency checks. It does not retain MCP arguments,
  tool output, or streamed response text when M1 is run.

## Verification record

Verified 2026-10-20 with Codex `0.162.0-alpha.17.2`, Python `3.12.3`, Git
`2.55.0`, on Linux:

- The fixture starts with one failing `unittest` assertion. A read-only Codex
  thread explained it. A direct sandboxed file write exited 1, created no file,
  and left the canonical `HEAD`, tracked file hash, and worktree status unchanged.
- In branch `m1/fix`, Codex changed only `calculator.py`; the test passed. The
  canonical `main` commit, file hash, and clean status remained unchanged.
- A follow-up turn in the same provider thread completed. After an App Server
  restart, `thread/resume` continued that thread with the original worktree.
- The script removed the disposable worktree and made a fresh sibling worktree
  from `main`. Resuming the thread with the new `cwd` returned that path; a shell
  item reported that exact working directory. The test failed again, proving the
  previous uncommitted edit was not transferred with thread history.
- Two additional threads used separate worktrees on one App Server process. Both
  started their 12-second shell commands before either completed, and both
  commands completed. This confirms overlapping execution in this environment.
- The App Server exited with status 0. Host checks found no remaining process
  groups for the two live App Server instances. Temporary repositories/worktrees
  were removed by the script.
- The work thread remains in the user's Codex local history for inspection:
  `01a15897-40b7-7d32-a10e-964ab17eba55`. Its temporary working directory is gone.
  The read-only and concurrency threads were ephemeral.
- An initial concurrency implementation waited for both threads with
  `asyncio.gather`; both coroutines then attempted to read one `StreamReader` and
  Python raised `RuntimeError`. The spike now has one stream consumer and waits
  for turns sequentially while recording interleaved events. Codex server-side
  concurrency itself succeeded.
- Codex emitted MCP tool items during the read-only discussion despite the
  integration overrides. The test repository was synthetic; no project source
  or secrets were supplied. M1 does not establish that configured integrations
  are fully disabled.

## What M1 establishes

On this Linux installation, Codex's native read-only sandbox denied writes to
the canonical fixture; workspace-write with an explicit writable root allowed a
worktree edit; provider-thread history resumed across process restart and could
be associated with a fresh `cwd`; and two threads executed concurrently in one
App Server process. These results do not establish equivalent macOS behavior,
hostile-code containment, full MCP isolation, or safe cleanup of unreviewed
changes. M2 can build on this topology while keeping workspace data durable and
showing the effective permission scope.
