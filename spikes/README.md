# M0 — Codex App Server integration spike

M0 proves that Python can drive the existing Codex harness. Success requires a
real two-turn conversation in one thread, incremental response events, and a
reaped App Server process with no remaining process-group members. It does not
implement an agent loop or any later milestone.

## Run it

Prerequisites: macOS/Linux, Python 3.11+, `codex` on `PATH`, network access, and a
ChatGPT account with access to the configured Codex model. Codex needs its normal
user data/configuration directory to be writable, including for token refresh.

```bash
codex --version
codex login status
# If needed, run Codex's browser login yourself:
codex login
python3 spikes/m0_codex_app_server.py
```

Optional: `--codex /absolute/path/to/codex`, `--model MODEL`, `--timeout 120`.
The model defaults to the user's Codex configuration. This is a real interaction
and consumes account usage. Authentication does not imply access to every model
or unlimited usage.

The script emits newline-delimited JSON to stdout. Look for two
`turn/completed` events with status `completed`, then `spike/passed`, followed by
`process/stopped`. Failure exits 1, Ctrl-C exits 130, and SIGTERM exits 143.
The second response must recall the first turn's marker; it is not repeated in
the second prompt. Each turn must also produce streamed agent text.

## Engineering decisions

- **One file, standard library only.** `asyncio` handles subprocess pipes and
  deadlines. There is no SDK dependency or provider abstraction to maintain.
- **Stdio transport.** Python starts `codex app-server --listen stdio://` directly,
  without a shell, terminal parsing, port, daemon, or network listener.
- **Installed schema is the compatibility reference.** On the verified version,
  requests contain `id`, `method`, and `params`; responses echo `id`; notifications
  omit `id`. The wire omits the `jsonrpc` field and uses one JSON object per line,
  without LSP `Content-Length` framing. No experimental API opt-in is needed for
  these operations, although App Server itself is experimental.
- **Handshake before work.** Wait for `initialize` to succeed, send `initialized`,
  check `account/read`, then create a thread. A thread holds conversational state;
  a turn is one request plus the ensuing agent work; items are messages/tool work
  within that turn. Python owns transport and process lifetime, while Codex owns
  reasoning, tools, history, and model access.
- **One outstanding RPC.** This spike deliberately runs turns sequentially.
  Notifications are processed while waiting for RPC responses. A `turn/start`
  result acknowledges acceptance; only `turn/completed` determines final status.
  Completion is retained even if it arrives before the RPC reply. No background
  stdout reader, multiplexing framework, or polling model calls are necessary.
- **Reuse managed authentication.** `account/read` must report `chatgpt`, and
  `thread/start` explicitly selects provider `openai`. Python does not open token
  files, accept credentials, initiate login, or manage OAuth refresh. Codex does
  that through its supported login. The existing user's config/environment is
  inherited, so this is not an isolated Codex installation.
- **Small, non-repository conversation.** Use a temporary working directory,
  `sandbox: "read-only"`, `approvalPolicy: "never"`, and `ephemeral: true`. The
  thread is intentionally not resumable after shutdown. Prompts ask for no tools;
  that wording is not a security boundary. Native sandbox enforcement, MCP/plugin
  permissions, and canonical-repository protection remain M1 questions.
- **Selected output.** Stream text deltas and selected event IDs/types/statuses.
  Account details, arbitrary RPC error text, and raw tool/config/auth payloads are
  omitted. Stderr is continuously drained and counted, without forwarding its
  potentially private contents. This trades detailed diagnostics for a small,
  credential-conscious demo; it is not a general-purpose log redactor. Response
  text is visible, so do not use secret-bearing prompts for this demonstration.
- **Bounded cleanup.** There is no shutdown RPC in the generated schema. Close
  stdin, allow three seconds for normal exit, then signal the owned Unix process
  group with SIGTERM, followed by SIGKILL after two seconds if needed. Reap the
  direct child and kill any remaining group members. The same `finally` cleanup
  runs on errors, timeouts, Ctrl-C, and SIGTERM. SIGKILL of Python itself, machine
  failure, and descendants that create new sessions are outside this guarantee.

Unsupported server requests receive a method-not-found response and stop the
spike. There is deliberately no approval UI or automatic grant of permissions.
Full event persistence, reconnection, interactive tools, concurrent threads,
large-output backpressure, and thread resumption are deferred.

## Verification record

Verified on 2026-10-20 (America/Los_Angeles):

- Python `3.12.3`; Codex `0.162.0-alpha.17.2`.
- `codex app-server --help` confirms explicit/default stdio support.
- `codex app-server generate-json-schema --out /tmp/m0-codex-schema` generated
  the installed protocol without experimental fields. Checked initialization,
  account read, thread/turn inputs, text-delta/completion notifications, and the
  absence of a shutdown method. Generated schemas are not committed.
- `codex login status` reported ChatGPT login. The live RPC also reported
  `account.type = chatgpt`; no credential or account identity was printed.
- The workspace-sandbox run exited before initialization with code 1. This
  environment also reported read-only filesystem warnings from the CLI. Running
  with approved execution outside that sandbox completed both turns.
- Effective model/provider: `gpt-6.1-sol` / `openai`.
- Thread: `01a15876-dfd9-7973-860c-a84afba1e6ae`.
- Turns: `01a15876-e02a-7732-ba7f-eec25f56b0a3` and
  `01a15876-ec19-7b13-9e43-6b749387e76e`, both `completed`.
- Streamed answers: `ACK`, then `M0-ORBIT-731`. Script exited 0; App Server PID
  `5922` exited 0 through stdin EOF. A subsequent host process check found neither
  PID nor process group `5922`, with zero remaining group members.
- The live stream also included configuration and MCP startup notifications.
  Existing Codex configuration can activate integrations even for a tiny prompt.
  This spike does not change the user's configuration or validate those tools.
- The available ChatGPT login worked for these two inference requests. Plan
  details, quota ceilings, and other accounts' entitlements were not established.

Run deterministic protocol/lifecycle checks without model calls:

```bash
python3 -B -m unittest discover -s spikes -p 'test_*.py' -v
```

Four test methods cover early completion, stderr pipe backpressure, two-turn
continuity, missing auth, RPC errors, failed turns, incorrect recall, unsupported
server requests, abrupt exit, malformed JSON, deadlines with an unresponsive
process, Ctrl-C, and SIGTERM. Each subprocess case verifies that its process group
is gone. All passed. The live run separately proves actual model access.

## Official references and compatibility limits

[Official App Server documentation](https://learn.chatgpt.com/docs/app-server)
describes this integration surface. The local version's generated schema and
observed behavior determine the fields used here. Recheck these when upgrading
Codex, particularly because the installed binary is an alpha.

The official auth guidance permits the managed App Server path for local/open
source applications and directs broader integrations toward Sign in with ChatGPT;
it excludes commercial/hosted services from App Server authentication. Revisit
authentication before changing this project's local single-user distribution
scope. That future product decision is outside M0.

M1's follow-on evidence and limitations are in [`m1_README.md`](m1_README.md).
