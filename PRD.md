# Agent Development System — Product Requirements Document

**Status:** MVP product direction agreed; implementation details remain subject to technical validation.  
**Audience:** Developer, coding agents, and future contributors.  
**Scope:** Local, single-user desktop MVP. This document is the authoritative source for *what the product should do*.

## 1. Product thesis

Build a desktop environment in which one developer can work with existing frontier coding agents, beginning with Codex, without recreating their reasoning loops. The developer can discuss a project, optionally plan, preserve useful work as tasks, let agents modify code in isolated Git workspaces, supervise and review changes, and explicitly integrate accepted work into the canonical repository.

**Product philosophy:** Start with one agent. Add independent sessions when the developer chooses. Build reliable development primitives before attempting autonomous multi-agent orchestration.

**Primary user:** A developer who wants to build new software projects or work in existing repositories while retaining architectural judgment and final integration authority.

## 2. Core concepts

| Concept | Meaning |
| --- | --- |
| **Project** | A registered local Git repository. Its configured canonical branch (`main` by default) and accepted repository documents represent project truth. |
| **Application session** | A user-visible, persistent conversation/workstream with one primary coding agent. It can span multiple tasks and can exist without a writable workspace. |
| **Provider thread** | The native conversation/session maintained by Codex (and later other providers). Usually associated with an application session, but not assumed to be permanently one-to-one. |
| **Task** | A durable, agent-independent objective with a concise brief, constraints, and success criteria. It can remain unassigned and be executed later. |
| **Workspace** | A temporary Git branch + worktree holding proposed code changes for a coherent line of work. Several related tasks can use the same workspace. |
| **Execution / turn** | Native agent activity undertaken to address a request or task. The app tracks useful status but does not replace the provider's agent loop. |

**Separation of ownership:** Projects own tasks; sessions own conversational continuity; workspaces own proposed code state; the developer owns acceptance into canonical Git state.

## 3. Main user experience

### A. Create or open a project

The developer can initialize a new local Git-backed project or open an existing repository. The app identifies a canonical integration branch. Creating a project must not require writing a PRD for that *target* project.

### B. Start a session and talk naturally

The developer starts a Codex session and can ask questions, inspect the repository, or plan at any depth: no planning, task-level planning, or fuller project/feature planning. A discussion-only session need not create a writable worktree. Repository inspection should be selective rather than loading all files into model context.

**One primary agent per application session for the MVP.** The developer manually creates a second session when a separate line of work is useful. A session can contain multiple related tasks.

### C. Propose or create a task

For a message such as “Implement the Notebook model,” the app should offer a **user-approved task proposal** instead of silently converting conversation into tracked work or beginning modifications. The developer can edit the brief and choose:

- **Create & Run:** save the task and execute it in the current session (provisioning a workspace if necessary).
- **Create Only:** save the task as ready/unassigned for later execution in this or a new session.
- **Dismiss:** continue the conversation without creating tracked work.

The developer can also explicitly create a task (for example, through a UI action or `/task` command). **Task detection and proposal presentation are application features, not assumed native Codex UI features.** The app must enforce the approval boundary *before granting code-changing execution*, rather than relying on prompt wording to make Codex wait. The exact mechanism must be proven during implementation; if safe automatic detection is unreliable, explicit task creation remains a supported path.

### D. Execute work in an isolated workspace

For code-changing work, the app creates a Git branch/worktree derived from the current canonical project state, or reuses the session's active workspace for related tasks. Codex uses its native harness, tools, permissions, and local sandbox. It must not silently write into the canonical checkout.

The user can see useful progress, provide follow-up instructions or steering where the provider supports it, and inspect logs/diffs without unnecessary model calls.

### E. Review and integrate

The user inspects the proposed diff and available test/agent results; can ask the same session for revisions; and explicitly **accepts** or **abandons** the changes. No agent automatically merges into the canonical branch. If `main` has advanced, the app does not silently rebase an active workspace; it must surface the issue and validate integration against the current canonical state.

After integration/abandonment, the worktree can be removed while the application session and task history remain. A later implementation request in that same logical session may use a fresh workspace from current `main`.

### F. Resume saved work or add another session

A saved task contains a concise, portable brief (objective, constraints, acceptance criteria, relevant decisions/files, and originating context references when available). When executed later, the app provides the **current** canonical project state as well as relevant creation-time context. The user can assign it to an existing or newly created session. No autonomous session splitting or scheduling is required.

## 4. MVP requirements

### Projects and sessions

- **PR-01:** Create a local Git-backed project and open an existing Git repository.
- **PR-02:** Identify/configure the canonical integration branch (`main` by default).
- **PR-03:** Start, navigate, and resume Codex conversations where supported by its native protocol.
- **PR-04:** Preserve a logical application session across multiple turns and related tasks; user controls new-session creation.
- **PR-05:** Support conversation and read-oriented project inspection without first provisioning a writable workspace, while protecting canonical source state.

### Tasks and context

- **PR-06:** Explicitly create a task; offer editable, approval-gated proposals for actionable requests when technically feasible.
- **PR-07:** Support **Create & Run**, **Create Only**, and **Dismiss**; never silently create/run a proposed task.
- **PR-08:** Keep unassigned tasks durable and portable between sessions/providers in the application model.
- **PR-09:** Preserve a bounded task brief and references, not an entire copied conversation; reconcile with current repository state when later executed.

### Workspace and execution

- **PR-10:** Create separate Git branch/worktree state for independently active code-changing work.
- **PR-11:** Reuse an active workspace for related tasks in the same workstream; allow a new workspace after prior work is integrated/abandoned.
- **PR-12:** Use Codex App Server and provider-native agent/sandbox behavior; stream supported progress, tool, approval, and completion events.
- **PR-13:** Support native follow-up/steering as available; make operational status/diffs accessible without requiring a model call when possible.

### Review and durability

- **PR-14:** Show changed files, Git diff, and available test results/agent summaries before acceptance.
- **PR-15:** Support revisions, explicit acceptance/integration, and abandonment; never automatically merge agent changes into `main`.
- **PR-16:** Detect when canonical `main` has advanced and do not silently rewrite an active workspace.
- **PR-17:** Preserve projects, sessions, tasks, and workspace metadata across app restarts. Keep enough state to identify interrupted work.
- **PR-18:** Allow the developer to manually start a second independent Codex session/workspace and supervise both.

## 5. Quality and safety constraints

- **Canonical-state safety:** No agent changes enter `main` without explicit approval. Read-only discussion must not accidentally mutate canonical files.
- **Workspace separation:** Independent worktrees must not overwrite one another's tracked source state. Git worktrees alone are **not** a hostile-code security boundary.
- **Native-agent fidelity:** Do not build a custom model loop, context compactor, or universal tool interception layer in the MVP.
- **Context/usage efficiency:** Avoid bulk prompt injection, redundant model calls, and unnecessary delegation. A fresh context is not automatically cheaper overall; measure quality and usage when optimizing.
- **Responsiveness:** Long-running agent work must not block the desktop renderer. Renderer reloads should not stop the runtime while the app remains open.
- **Lifecycle:** Intentionally quitting the desktop app may stop local agent work; metadata and worktrees must remain inspectable on reopening.
- **Portability:** Unix-first (macOS/Linux); Windows and cloud deployment are outside MVP scope.
- **Cost:** Prefer subscription-authenticated Codex where officially supported; do not assume unrestricted usage or a paid API requirement without validating the actual integration path.

## 6. Explicitly out of MVP

Claude Code integration; cross-provider coordination; agent-created application sessions; autonomous task decomposition/scheduling; group chat or `@agent` routing; automatic rebasing/merging; mandatory task dependency DAGs; Docker/external executor; cloud agents; multi-user collaboration; custom model inference; a full IDE/editor; elaborate context caching or snapshots.

Native Codex subagents, if available, remain **provider behavior**, not an application-managed orchestration feature.

## 7. Future direction (not a commitment)

A lead agent could request a **new independent application-managed session** for a delegated task, potentially using a different provider and a fresh, bounded context package. A delegated code-changing session could receive its own worktree, possibly derived from a specific lead-workspace revision. Results return to the lead; final canonical integration still requires developer approval.

This is deliberately **not** a plan to place several primary agents inside one shared application session. Independent sessions can improve context relevance, but total token usage and integration overhead must be measured rather than assumed to improve.

Other candidate deepening areas: stronger execution sandboxing, dependency/workspace reuse, context efficiency, observability, recovery, and integration correctness. Choose based on real dogfooding bottlenecks.

## 8. MVP acceptance scenario

1. Create/open a repository and start a Codex session.
2. Discuss the project and inspect relevant files without modifying canonical `main`.
3. Propose or explicitly create a task; approve **Create & Run**.
4. Provision a separate Git worktree; execute the task with native Codex behavior.
5. Observe progress and inspect changes; request a revision in the same logical session.
6. Review the final diff and available checks; explicitly integrate into `main`.
7. Remove the temporary workspace without losing session/task history.
8. Save another task for later, then assign it to a new or existing session.
9. Start a second independent session/worktree manually and verify the code states remain separate.

**Success means this end-to-end workflow actually runs on a real repository**, not merely that the UI can display it.
