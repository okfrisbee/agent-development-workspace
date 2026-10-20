# Agent Development System — Architecture

**Status:** Agreed MVP boundaries; implementation specifics to be validated by technical spikes.  
**Authority:** This document defines *how* the product is intended to work. Product behavior belongs in [`PRD.md`](PRD.md); build order belongs in [`PLAN.md`](PLAN.md).

## 1. Architecture at a glance

```text
Electron desktop application
  React + TypeScript renderer (views, chat, task approval, review)
          |
  Electron main process (app lifecycle, secure IPC bridge)
          |  newline-delimited structured messages over stdio
          v
Python runtime child process (owned by Electron main)
  - project / task / application-session metadata
  - Git worktrees, review and integration
  - SQLite persistence; event/log handling
  - Codex App Server lifecycle and protocol client
          |  native Codex App Server protocol (JSON-RPC over stdio)
          v
Codex App Server (initially one process, multiple logical threads)
  - native agent loop, model requests, tools, approvals
  - provider conversation history / context compaction
  - native local sandboxing and execution
          |
          v
Git worktree for an active implementation workstream
```

This is a **process topology proposal**, not a claim that every Codex thread is its own OS process. App Server multiplexing/concurrency must be validated in the first spikes. Agent tool commands may create additional OS subprocesses.

**React + TypeScript inside Electron** is the chosen UI; Next.js is not required. No persistent standalone daemon is required for the MVP.

## 2. Process ownership and IPC

- **Renderer:** Presentation only. Does not directly spawn Codex or own long-running execution. Communicates through a constrained Electron preload/main bridge; do not expose unrestricted Node APIs to web content.
- **Electron main:** Starts/owns the Python child process, bridges renderer commands and runtime events, handles intentional shutdown, and survives renderer reloads.
- **Python runtime:** Owns application operations, durable metadata, Git/worktree lifecycle, Codex App Server process and streaming, and review/integration operations. Do not block the event loop with synchronous long-running subprocess waits.
- **Codex App Server:** Provider-native agent harness. It owns Codex thread/turn/tool/approval/context semantics. We use its supported protocol, not terminal-screen scraping or a homegrown agent loop.

**Agent autonomy boundary:** The application manages projects, sessions, tasks, workspaces, persistence, and user-controlled integration. The provider owns its native reasoning loop, planning, tool selection, tool execution, context management, and supported approval mechanisms. Our runtime should expose and coordinate these capabilities through supported provider interfaces rather than reimplementing them. Application-level controls should enforce workspace boundaries and user intent without unnecessarily restricting agent autonomy.

### Electron main ↔ Python

Use **newline-delimited JSON over stdio** for MVP requests, responses, and asynchronous events. Example envelopes (illustrative; not a finalized schema):

```json
{"id":"req-42","method":"task.start","params":{"task_id":"task-17"}}
```

```json
{"id":"req-42","result":{"status":"started"}}
```

```json
{"type":"event","event":"session.status","session_id":"s-1","status":"working"}
```

Reserve stdout for machine-readable messages; diagnostics go to stderr/files. Include request IDs, structured errors, and a clear cancellation/shutdown path. Buffer and truncate or persist large output rather than flooding the renderer.

### Python ↔ Codex

Launch and communicate with **Codex App Server via its supported stdio JSON-RPC protocol**. Validate version/protocol capabilities and subscription-based authentication in the spike. Do not assume private or experimental APIs are stable. Protect credentials and never log tokens.

**M0 evidence:** Python completed a real two-turn conversation on Codex
`0.162.0-alpha.17.2` using the existing Codex-managed ChatGPT login. The stdio wire
uses newline-delimited JSON, omits the `jsonrpc` field, and requires
`initialize` → `initialized` before thread/turn requests. There is no shutdown RPC
in this version; the spike closes stdin and uses bounded process-group cleanup.
App Server remains experimental. See [`spikes/README.md`](spikes/README.md) for
reproduction and authentication limits, and [`spikes/m1_README.md`](spikes/m1_README.md)
for M1's sandbox/worktree evidence. No process topology change was required.

## 3. State ownership

| Store | Authority / contents | Not its responsibility |
| --- | --- | --- |
| **Git** | Canonical branch, accepted source/docs, commits, temporary workspace branches, diffs and merge history | Live agent status |
| **SQLite** | Registered projects, logical sessions, provider thread references, tasks, assignments, workspace metadata, statuses, base revisions, lifecycle records | Source-code truth or provider model context |
| **Filesystem** | Worktrees, large logs, event traces, artifacts, exported diffs, diagnostics | Canonical accepted decisions outside Git |
| **RAM** | Child process handles, live event streams, transient caches, in-flight requests | Durable state |
| **Provider harness** | Native conversation/turn state, tool semantics, context compaction, resumption behavior | Application-level tasks and integration authority |

SQLite is a good fit for local concurrent sessions and short transactional metadata updates. Prefer WAL mode, short transactions, migrations, and a clear owner for writes. Large tool outputs belong on disk rather than in database rows.

## 4. Domain model and relationships

```text
Project
  ├── Tasks (durable; may be unassigned)
  ├── Application Sessions (user-created workstreams)
  │      ├── provider-thread reference(s)
  │      ├── task assignments / activity
  │      └── optional active Workspace
  └── canonical Git branch

Workspace
  ├── worktree path
  ├── branch
  ├── base revision
  └── lifecycle state
```

- **Application Session:** User-visible continuity and navigation; one primary agent for MVP. It may span multiple tasks and survive worktree deletion.
- **Provider Thread:** Native Codex conversation. A session will *usually* map to one Codex thread initially, but the schema should not assume a permanent 1:1 mapping. M1 verified that a thread can resume with a new `cwd`; its conversation history continues, but uncommitted files from the old worktree do not move into the new one. Persist the worktree or save its changes separately before cleanup.
- **Task:** Durable project-owned objective, not a Codex turn. A task can be created without execution and later assigned to any compatible session. One task may involve multiple turns; multiple related tasks may use one workspace.
- **Workspace:** Temporary branch/worktree for a coherent set of proposed code changes. It is not identical to the session or task. One active workspace per session is a reasonable MVP simplification, not a universal future invariant.
- **Execution/attempt:** Useful internal lifecycle/telemetry concept when needed, but not a mandatory user-visible object or separate process.

**Do not build an elaborate provider-neutral adapter hierarchy in v0.** Localize Codex-specific protocol behavior so a second provider can be added later based on real requirements.

## 5. Git/workspace lifecycle

```text
User creates/resumes session (no worktree required)
        |
Discussion / read-oriented inspection / planning
        |
User approves code-changing task
        v
Create branch + Git worktree from configured canonical branch
        |
Codex executes in that workspace; related tasks may continue there
        |
User reviews diff / tests / proposed changes
        |
   revise | accept | abandon
        |       |
        |       +-- validate against current main, integrate explicitly
        |       +-- cleanup/archive worktree after safe completion
        +-- same session may continue after workspace is gone
```

Rules:

1. Never give an implementation agent writable access to the canonical checkout as its task workspace.
2. Do not automatically rebase an active worktree when `main` advances. Show staleness and validate integration against current `main`.
3. Integration into the canonical branch requires explicit user approval and a deliberate Git operation. The precise merge/rebase/apply strategy remains to be chosen after testing real Git edge cases.
4. Do not delete worktrees with unreviewed changes without clear user intent. Retain durable task/session records after cleanup.
5. Existing repositories may have uncommitted changes or non-`main` default branches. Detect these and handle safely; do not silently overwrite or reset user work.
6. A session can later receive a new worktree from the latest canonical revision without creating a new application session. Provider-thread reassociation must be verified rather than assumed.

## 6. Agent integration and sandboxing

**MVP choice:** Codex App Server on the host, using Codex's **native local sandbox and approval model**, with Git worktrees providing independent source-state isolation.

This supersedes the earlier mandatory **Git worktree + Docker** proposal. Worktrees are not a substitute for an OS sandbox, and native sandbox guarantees vary by platform/configuration. The MVP must verify that discussion/inspection cannot silently mutate canonical project state and that code-changing work is confined to the intended worktree under supported settings.

**Explicitly deferred:** Putting the App Server and arbitrary project code in one all-purpose Docker container, or requiring a common external Docker executor. If stronger execution isolation/reproducibility is justified later, investigate a trusted harness outside an external Docker/VM executor, provided the provider's supported interfaces allow it.

**Security baseline:** Treat repository contents, tool output, and generated code as untrusted; avoid granting unnecessary filesystem/network permissions; keep authentication secrets out of worktrees and execution logs. Do not promise hostile-code containment without testing the actual sandbox boundary.

**M1 evidence (Linux, Codex `0.162.0-alpha.17.2`):** `read-only` blocked a
direct App Server write probe; `workspaceWrite` with one explicit writable root
allowed a test fix in a Git worktree while the canonical fixture remained clean.
Two shell commands on separate threads overlapped in one App Server process. The
Python client must use one stdout reader and dispatch events, even when multiple
turns are active. App Server resumed a persistent thread against a new `cwd`, but
the new worktree did not contain the old uncommitted edit. Configured apps/plugins
and empty `mcp_servers` overrides did not prevent all `mcpToolCall` items, so this
spike does not establish MCP isolation or hostile-code containment. See
[`spikes/m1_README.md`](spikes/m1_README.md) for reproducible evidence and limits.

## 7. Task proposals and context

Task proposals are an **application-level approval gate**. We need a supported way to identify actionable requests and show an editable proposal *before* code-changing execution. The implementation must prove that Codex cannot race ahead and mutate canonical state while a proposal is pending. **The app must enforce a non-mutating pre-approval stage or use a separate explicit task-creation interaction; a prompt such as "ask before coding" is not an enforcement boundary.** If native controls do not support safe intent detection, implement explicit task creation first and revisit proposals later.

Task packages should contain concise objective, constraints, acceptance criteria, relevant references, creation revision, and provenance back to the originating discussion when applicable. When run later, retrieve **current** canonical project information and inspect relevant files on demand. Avoid injecting entire conversation histories or repositories into each new session.

The native provider owns its own context management. We can observe available usage/latency information, but should not claim precise token savings without measurement.

## 8. Lifecycle, failure, and recovery

- **Renderer reload:** Electron main/Python/Codex can remain running; UI reconstructs state from runtime events and durable metadata.
- **Intentional app quit:** Python and Codex are shut down deliberately. Active work is marked interrupted/stopped; Git worktrees and task/session metadata remain for inspection on reopen.
- **Unexpected crash:** Reconcile SQLite records against actual Git worktrees and provider processes on next launch; do not assume in-memory handles or a previously active model turn survived.
- **Session resume:** Use native Codex resumption when supported; verify how resumption behaves after worktree cleanup and app restart.
- **Observability:** Persist relevant events/diagnostics and show task status, changed files, diffs, and available checks without excessive model calls.

## 9. Deferred architecture and future delegation

Future app-managed delegation should create **a separate application session** for the delegated agent, with a bounded task package and optionally an independent worktree. The lead session receives results/summaries rather than the subordinate's entire context. Cross-provider adapters and parent/delegated relationships belong to later iterations.

A delegated workspace might need to start from an approved lead-workspace commit instead of current `main`; integration and consistency are real future design problems. Fresh contexts can reduce per-session clutter but do **not** automatically reduce total model usage.

## 10. Technical unknowns to validate before locking implementation

1. Codex App Server startup, supported ChatGPT authentication, and protocol/event schema.
2. Multiple native threads in one App Server process; actual concurrency and cancellation semantics.
3. Read-only discussion mode and task-approval gating without canonical-repository writes.
4. How working-directory changes and worktree deletion affect native thread continuation/resumption.
5. Native sandbox permissions, network/credential exposure, and Git worktree access.
6. Safe integration with dirty checkouts, stale bases, conflicts, and failed tests.
7. Python process shutdown and event streaming over stdio under backpressure.

Use [`PLAN.md`](PLAN.md) to test these incrementally. **Change this document when evidence disproves an assumption**, rather than building around an unverified capability.
