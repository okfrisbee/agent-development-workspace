# Agent Development System — Implementation Plan

**Status:** M0 complete; M1 ready to begin.

**Current milestone:** **M1 — Repository inspection, native sandbox, and worktree spike** (not started).
**Working rule:** Complete and verify one vertical slice at a time. This is a living execution plan, not another product specification.

**Milestone boundary:** M0–M6 deliver the functional MVP. M7 is real-world dogfooding, and M8 is an evidence-driven technical deepening phase, not a prerequisite for calling the MVP functional.

## How to use this plan

1. Read the **current milestone**, plus relevant sections of [`PRD.md`](PRD.md) and [`ARCHITECTURE.md`](ARCHITECTURE.md).
2. Implement **only** that milestone; keep experimental code isolated until it proves useful.
3. Verify the success criteria with a reproducible command, test, or observed behavior.
4. Record the outcome, surprises, and any architecture change needed.
5. Commit a working increment and move the current-milestone pointer forward.

Do not rewrite the whole plan merely because one tool behaves differently than expected. Update the smallest affected milestone and architecture assumption.

## M0 — Codex App Server integration spike

**Goal:** Prove that Python can control Codex App Server through its supported protocol while using an available ChatGPT/Codex authentication path.

**Build/prove:**
- Confirm installed Codex version and official App Server startup/protocol commands.
- Start App Server as a Python-managed subprocess.
- Authenticate through the supported flow without storing secrets in the repository or logs.
- Follow the **installed** App Server version’s supported initialization/handshake protocol; start a thread, send one turn, stream a response and structured events, and shut down cleanly.
- Continue the same thread with a second turn; observe actual thread/turn identifiers.
- Verify whether the available subscription entitlement works for this integration; record limitations instead of assuming unlimited free calls.

**Done when:** A tiny repeatable Python program performs a real two-turn interaction, streams events, and exits without orphaned processes. Document the actual protocol and known limitations.

**Not now:** Electron, SQLite, tasks, worktrees, Docker, provider abstraction layers.

## M1 — Repository inspection, native sandbox, and worktree spike

**Goal:** Verify our most important safety and workspace assumptions against a disposable Git repository.

**Build/prove:**
- Open a tiny repo with a failing test; inspect files through Codex.
- Test read-oriented discussion without modifying canonical source state; identify the supported permission/sandbox mechanism.
- Create a branch/worktree; point an agent thread at that directory; edit code and run tests there.
- Verify canonical checkout remains unchanged and inspect the resulting Git diff.
- Test a follow-up turn, thread resumption, and what happens if the worktree is removed and a new one created.
- Test two threads/worktrees using one App Server process, including whether they can actually run concurrently.

**Done when:** We have a reproducible demonstration of independent code state, known sandbox behavior, and evidence for how session/workspace reassociation works.

**Not now:** Polished review UI, automatic merging, advanced isolation, Docker executor.

## M2 — Minimal Python application runtime

**Goal:** Turn the working spikes into a small service owned by the eventual desktop app.

**Build/prove:**
- Minimal operations for project registration, logical sessions, native provider-thread references, and workspace lifecycle.
- SQLite migrations for only the metadata we genuinely need; WAL and short transactions.
- Structured request/response and event messages over stdin/stdout; errors and stderr diagnostics.
- Lifecycle handling: start Codex, stop it cleanly, preserve useful metadata, mark interrupted work on restart.
- Tests for Git commands, process cleanup, and persistence; no agent calls in unit tests.

**Done when:** A small terminal client can talk to the Python runtime, start a session, receive events, and recover saved metadata after restart.

**Not now:** Complex schema, a daemon that survives app exit, task orchestration.

## M3 — Minimal Electron desktop shell

**Goal:** Put a real UI on top of the working runtime without moving agent logic into Electron.

**Build/prove:**
- Electron main starts/stops Python and forwards messages safely to React/TypeScript.
- Open/create project, list sessions, start/resume one Codex conversation, display streamed responses.
- Renderer reload leaves Python and active local work alive while Electron main remains open.
- App quit shuts down active work deliberately and preserves durable state.

**Done when:** The desktop app can run a Codex conversation against a selected project and show live events.

**Not now:** Full IDE, polished styling, provider switcher, autonomous delegation.

## M4 — Task creation and portable handoff

**Goal:** Add the product's first distinct layer: durable, agent-independent tasks.

**Build/prove:**
- Explicit task creation (`/task` or GUI) and task list with ready/active/review/done states.
- Explore a supported, safe mechanism for proposing tasks from conversation; editable **Create & Run / Create Only / Dismiss** approval UI.
- Ensure task approval happens **before** code-changing execution through an enforceable app/runtime boundary, not a prompt alone. If safe automatic detection is unreliable, ship explicit creation first and document the limitation.
- Save bounded task context and creation revision; assign a saved task to an existing or new session later using current canonical state.

**Done when:** A task can be saved without running, reopened, assigned, and executed without copying the original chat transcript.

**Not now:** Auto decomposition, dependency scheduler, cross-provider routing.

## M5 — Review, revision, and safe integration

**Goal:** Complete the canonical Git workflow end-to-end.

**Build/prove:**
- Show branch/worktree status, changed files, diff, and available checks.
- Request a revision through the existing logical session.
- Explicit Accept / Abandon actions; no automatic merging.
- Handle `main` advancing, conflicts, dirty checkouts, and failed validation without silently discarding work.
- Integrate approved changes using a deliberate Git operation; clean up worktree only after safe completion; retain session/task records.

**Done when:** A real feature is implemented in a worktree, reviewed, revised, integrated into `main`, and cleaned up safely.

**Not now:** Semantic conflict detection, autonomous integration, sophisticated CI orchestration.

## M6 — Multiple independent sessions

**Goal:** Prove the parallel development primitive without a coordinator agent.

**Build/prove:**
- User manually starts a second Codex session and separate worktree.
- Run independent work without cross-worktree writes.
- Observe both sessions; verify correct event routing and safe shutdown.
- Validate that session/workspace records remain consistent across app restart.

**Done when:** Two independent sessions can work on separate changes and each be reviewed/integrated deliberately.

**Not now:** Agent-to-agent chat, automatic spawning, shared mutable workspace, cross-provider delegation.

## M7 — Dogfood on the student knowledge-workspace project

**Goal:** Use the agent development system to build real software, not synthetic demos.

**Work:** Create/open the separate student-workspace repository, discuss its scope, create real tasks, implement work in independent sessions, and review/integrate. Record concrete friction in setup, context usage, supervision, reliability, isolation, or integration.

**Done when:** The system has supported several real development cycles and produced evidence of what is most painful—not a vanity usage count.

## M8 — Choose one real deepening direction

**Goal:** Make one technically meaningful improvement justified by observed bottlenecks.

Possible directions include: faster workspace provisioning, reproducible dependency state, stronger external sandbox, session/context handoff efficiency, better recovery, integration correctness, or provider portability.

**Done when:** There is a reproducible before/after measurement or failure case, a justified implementation, and a technical explanation of tradeoffs. Avoid claiming improvements solely because the implementation sounds sophisticated.

## Future (not MVP): first-mate delegation

A lead session may later request creation of a **separate** application-managed worker session with a bounded task package, its own provider-native thread, and optionally an independent workspace. The worker returns a concise result; the lead does not automatically merge into canonical `main`. Cross-provider support and delegated workspace ancestry are later design problems, not current milestones.

## Current status / decision log

| Item | Status | Evidence / next action |
| --- | --- | --- |
| Product model | Agreed | See `PRD.md` |
| Initial architecture | Agreed, unvalidated | See `ARCHITECTURE.md` |
| M0 Codex integration | **Complete** | Real two-turn ChatGPT-authenticated run; streamed events; process group absent after shutdown. See `spikes/README.md` |
| M1+ | Not started | Do not pre-implement |

### Milestone completion notes

Add short entries here after each milestone:

**M0 — complete (2026-10-20):** `python3 spikes/m0_codex_app_server.py`
completed two real turns in one thread on Codex `0.162.0-alpha.17.2`, using the
existing ChatGPT login and configured `gpt-6.1-sol` model. Both turns completed,
text deltas streamed, and the second recalled the first turn's marker. App Server
exited 0 after stdin EOF; a host check found no remaining process-group members.
Four deterministic test methods also passed, covering protocol errors, event
ordering, pipe backpressure, timeout, and signal cleanup without model calls.
The initial workspace-sandbox launch failed before initialization; approved host
execution succeeded. No architecture change was required beyond documenting wire
framing and shutdown details. App Server is experimental, and account access is
verified only for the tested requests. Full evidence and limitations are in
[`spikes/README.md`](spikes/README.md). **Next: M1; no M1 implementation yet.**

```text
Milestone:
Verified by:
What changed:
Unexpected findings:
Architecture/docs updates:
Next step:
```
