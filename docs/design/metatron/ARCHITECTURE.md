# Metatron CLI: architecture (for engineers and agents)

Status: design, 2026-11-17. Nothing here is built. Epic BOSSAPP-1367 in Plane (module "Metatron CLI").
Companion docs: [README.md](README.md) (plain overview), [GAP.md](GAP.md) (Codex feature gap).

How to read the evidence tags. "(seen)" means read in the Codex source at the pinned commit. "(assumed)" or "TODO" means not verified; verify before relying on it. Codex was never built or run for this study. Codex source: `github.com/openai/codex`, Rust workspace `codex-rs/`, pinned at commit **05ae1b0f** (record the full SHA in MT-0). All Codex paths below are relative to `codex-rs/` at that commit.

Operator decisions (2026-11-17), encoded as constraints:

1. Metatron is rewritten in **Rust** as a governed operator CLI (Codex/Kiro-class terminal).
2. Metatron **does not write code**. Code changes go to BOSS as a code task or workflow. There is no edit/patch tool.
3. It debugs, investigates and reports: ssh to hosts, `sar` metrics, packet capture, file transfer, on-the-fly Python analysis, quick dashboards, Grafana or any site via MCP or the user's own session cookie (cookie file, or Chrome cookie with per-domain permission).
4. Codex-like approval modes: read-only / ask / risky-only / act on my behalf. Tools come from shell + MCP + skills + search.
5. BOSS provides policy, vault credentials, audit, context and the model gateway. One agent path for every surface: Metatron is not a second decision engine and holds no model key.
6. Build starts after the current release work.

---

## 1. How Codex works (summary, with paths)

```
 codex (one native binary; the same exe also acts as sandbox / exec helper: the "arg0 trick")
 +---------------+   +--------------------------------------------------------------------------+
 | TUI (ratatui) |   | core: Session -> run_turn -> sampling request -> stream of ResponseEvent  |
 | or `exec`     |<->|   model reply has tool calls -> ToolRouter -> handler                     |
 | (JSONL out)   |   |     handler -> ToolOrchestrator:                                          |
 | or app-server |   |        1 approval (policy / exec rules / human / auto-reviewer)           |
 | (JSON-RPC)    |   |        2 run under OS sandbox   3 on denial: ask again, retry wider       |
 +---------------+   |   output truncated -> fed back as next model input                       |
                     |   history persisted to a rollout JSONL; compacted when too long          |
                     +------+--------------+----------------+----------------+-----------------+
                            |              |                |                |
                      codex-api       sandboxing       network-proxy    rmcp-client (MCP)
                      (SSE / ws to    seatbelt /       (domain rules,   + exec-server
                       Responses API) bwrap+landlock   credential       (JSON-RPC process
                                      +seccomp         broker)           runner)
```

### 1.1 Agent loop and model protocol
- `core/src/session/turn.rs`: `run_turn` -> `run_sampling_request` -> `try_run_sampling_request`. One user prompt is one turn; a turn loops model call, tool calls, model call until the model ends it (`end_turn` on the `Completed` event).
- Wire format is the OpenAI Responses API: `ResponsesApiRequest` in `codex-api/src/common.rs` (`model, stream, input, tools, tool_choice, parallel_tool_calls, reasoning, store, include, prompt_cache_key, text, client_metadata`).
- Streaming: `ResponseEvent` (`Created, OutputItemAdded, OutputItemDone, Completed{token_usage, end_turn}`) from SSE (`codex-api/src/sse/responses.rs`) or websocket (`codex-api/src/endpoint/responses_websocket.rs`).
- Tool schemas: `tools/src/tool_spec.rs` (`ToolSpec`: function, namespace, freeform, tool_search, web_search).

### 1.2 Built-in tools (`core/src/tools/handlers/`)
| Tool | File | Notes |
|---|---|---|
| shell / exec | `shell_spec.rs`, `unified_exec.rs`, `core/src/unified_exec/` | `exec_command(cmd, workdir, tty, yield_time_ms (default 10000, 250-30000), max_output_tokens (default 10000), shell, login)`. Long commands yield a session id; the model polls or writes stdin later. |
| apply_patch | `apply_patch.rs`, crate `apply-patch` | Not needed: Metatron must not write code. |
| update_plan | `plan_spec.rs` | Visible checklist. |
| view_image | `view_image.rs` | Attach a local image to the model. |
| web_search | `tools/src/tool_spec.rs` | A hosted OpenAI tool (`external_web_access`, `indexed_web_access`); Codex does not fetch pages itself. Cannot be copied. |
| MCP tools/resources | `mcp.rs`, `mcp_resource.rs` | Namespaced tools from MCP servers. |
| request_permissions, request_user_input | handlers dir | Model asks for more rights or asks the person. |
| multi_agents, tool_search, code_mode | handlers dir, `code-mode*` crates | Sub-agents, lazy tool discovery, JS code mode. Not needed in v1 (tool_search is reused, see section 8). |

### 1.3 Exec
- `core/src/exec.rs`: default timeout 10 s (`DEFAULT_EXEC_COMMAND_TIMEOUT_MS`), timeout exit code 124, I/O drain timeout 2 s.
- `core/src/unified_exec/mod.rs`: output cap 1 MiB / 10k tokens to the model, background terminal timeout 300 s, output streamed in 8 KiB deltas (`async_watcher.rs`).
- PTY and process groups: `utils/pty/src/` (`pty.rs`, `process_group.rs`, `child_reaper.rs`; crate `portable-pty 0.9`).
- Remote execution: `exec-server/` is a JSON-RPC server (ws or stdio) that spawns processes; core reaches it through `ExecutorFileSystem` and an environment registry (`exec-server/src/environment*.rs`). Pattern for Metatron's "run on a host".

### 1.4 Sandboxing (`sandboxing/`, `linux-sandbox/`, `windows-sandbox-rs/`)
- Policies (`protocol/src/protocol.rs`, `config_types.rs`, `models.rs`): `SandboxMode` = read-only | workspace-write | danger-full-access; `SandboxPolicy` adds external sandbox and a `network_access` bool; newer `PermissionProfile` = Managed{filesystem entries, network} | Disabled | External.
- macOS Seatbelt: `sandboxing/src/seatbelt.rs` builds a profile from `.sbpl` templates (`seatbelt_base_policy.sbpl`, `seatbelt_network_policy.sbpl`), with writable roots and protected metadata paths (`.git`), then runs under `sandbox-exec`.
- Linux: bubblewrap (`bwrap.rs`, `linux-sandbox/src/bwrap.rs`) + landlock (`landlock.rs`, crate `landlock 0.4.4`) + seccomp (`linux-sandbox/src/seccomp.rs`, crate `seccompiler`); managed network in a namespace (`managed_tcp*.rs`, `proxy_routing.rs`).
- Windows: separate crate (~30k lines). Out of scope for Metatron v1.
- Helper pattern: the same executable re-invokes itself as the sandbox helper (`arg0/src/lib.rs`, `arg0_dispatch`).

### 1.5 Approvals and command safety
- `AskForApproval` (`protocol/src/protocol.rs`, around line 1184): `untrusted`, `on-request` (default), `granular{sandbox_approval, rules, skill_approval, request_permissions, mcp_elicitations}`, `never`.
- `core/src/tools/orchestrator.rs`: (1) compute the approval requirement `Skip | NeedsApproval | NeedsHumanApproval | Forbidden`; (2) first attempt under the sandbox; (3) if the sandbox denies, ask again and retry wider ("escalation on failure"). Each retry needs a fresh decision.
- Exec policy (`execpolicy/`): Starlark rules `prefix_rule(pattern=[...], decision=allow|prompt|forbidden, justification, match, not_match)` plus `host_executable(name, paths)`. The `match`/`not_match` examples are unit tests checked at load. A managed `requirements.toml` can force `prompt-user` for a prefix so even auto-review cannot pass it (`app-server/README.md`).
- Dangerous-command detection: `shell-command/src/command_safety/is_dangerous_command.rs` (forced `rm`; nested wrappers fail closed). No `is_known_safe_command` allow-list was found at this commit. TODO: confirm in `core/src/exec_policy.rs`.
- "Approve on my behalf": `ApprovalsReviewer = user | auto_review` (`protocol/src/config_types.rs`, around line 183). `auto_review` routes each approval to a reviewer sub-agent applying a risk framework (`core/src/guardian/`). This is the model for Metatron's act-on-my-behalf mode.

### 1.6 Network control (`network-proxy/`, ~36k lines)
Local HTTP proxy (127.0.0.1:3128) and SOCKS5 (8081); domain allow/deny with scoped wildcards; "limited" mode (read-only methods, HTTPS MITM); MITM hooks per host/method/path; private-IP blocking; and a **credential broker with secret substitution** (`credential_broker/`, `secret_substitution.rs`): the process sees a placeholder, the trusted proxy swaps in the real secret only after the destination is authorised. This is the pattern for Metatron's cookie and vault use (section 7).

### 1.7 MCP
Client: `rmcp-client/` (stdio and streamable HTTP, OAuth login) and `codex-mcp/`, on the official Rust SDK `rmcp` (git-pinned in the root `Cargo.toml`). Server mode: `codex mcp-server` / app-server. Per-tool approval templates: `core/src/mcp_tool_approval_templates.rs`.

### 1.8 Sessions, compaction, config, instructions
- Rollout: one JSONL per thread, `rollout-{timestamp}-{thread_id}.jsonl` (`rollout/src/rollout_file_name.rs`), typed `RolloutItem`s; a SQLite index (`state/`, `rollout/src/state_db.rs`) for list and search; resume and fork read the JSONL.
- Compaction: `core/src/compact.rs` (`run_inline_auto_compact_task`): near the window limit the model summarises and history is replaced; recent user messages kept (20k token cap).
- Config: `config/` crate; layered `config.toml` (user, project, managed `requirements.toml`, cloud), `-c key=value`, profiles (`profile_toml.rs`), generated schema.
- AGENTS.md: `core/src/agents_md.rs` walks from the project root (marker `.git`) down to cwd, concatenates every `AGENTS.md`, size budget `project_doc_max_bytes`.

### 1.9 TUI, exec mode, app-server, auth, telemetry, packaging
| Area | What | Where |
|---|---|---|
| TUI | ratatui 0.30 + a forked crossterm; custom terminal, composer, bottom pane with approval overlay, diff renderer, exec cells; talks to core through app-server types in process | `tui/src/` |
| `codex exec` | Non-interactive; `--json` emits JSONL `ThreadEvent`: `thread.started, turn.started, turn.completed, turn.failed, item.started, item.updated, item.completed` | `exec/src/exec_events.rs` |
| app-server | JSON-RPC (stdio/ws): thread/turn/item methods and server-initiated approval requests (`item/commandExecution/requestApproval`); TS and Python SDKs wrap it | `app-server*`, `sdk/` |
| Auth | OAuth + PKCE + device code; tokens in the OS keyring (crate `keyring 3.6`) | `login/`, `keyring-store/` |
| Telemetry | OpenTelemetry; tool decisions tagged with their source (config / user / reviewer) | `otel/`, `core/src/tools/orchestrator.rs` |
| Packaging | One native binary per target (linux musl x64/arm64, macOS x64/arm64, windows). npm package is a tiny Node launcher that picks the platform binary | `codex-cli/` |

Size warning: Codex `core` (~535k lines incl. tests) and `tui` (~533k) are products built by a large team and mostly OpenAI-specific. Do not fork them. Copy small leaf crates, imitate patterns.

---

## 2. Techniques to adopt

| # | Codex technique (path) | Metatron capability |
|---|---|---|
| 1 | Turn loop with streamed typed events (`session/turn.rs`, `exec/src/exec_events.rs`) | Live view of what the agent is doing; `metatron exec --json` for scripts and tickets |
| 2 | Unified exec: PTY, yield-and-poll, output caps, process groups (`unified_exec/`, `utils/pty`) | Run `sar 1 60`, `tcpdump -w`, long `scp` without freezing the loop; Ctrl-C kills the whole group |
| 3 | Orchestrator: approve, sandbox, escalate on denial (`tools/orchestrator.rs`) | One place where every tool call is judged |
| 4 | Approval modes incl. `auto_review` (`protocol/src/config_types.rs`, `core/src/guardian/`) | MT-7 modes |
| 5 | Exec rules with `match`/`not_match` tests (`execpolicy/`) | Testable policy BOSS stores and ships ("sar allow, tcpdump prompt, rm forbidden") |
| 6 | OS sandbox (`sandboxing/`, `linux-sandbox/`) | Scratch Python and local analysis cannot touch the rest of the machine |
| 7 | Credential broker + secret substitution (`network-proxy/`) | Cookie and vault credentials the model and child processes never see |
| 8 | exec-server (`exec-server/`) | Same tool API for local and ssh execution |
| 9 | Rollout JSONL + resume/fork + compaction (`rollout/`, `compact.rs`) | Local record of the whole session (evidence for a ticket) |
| 10 | Layered `config.toml` + profiles + `-c` (`config/`) | `~/.config/metatron/config.toml`; BOSS-managed layer that can only tighten |
| 11 | AGENTS.md walk (`core/src/agents_md.rs`) | Per-repo and per-host runbooks as context |
| 12 | rmcp client | Grafana MCP, ticket MCP, BOSS MCP |
| 13 | Keyring + device-code login (`login/`, `keyring-store/`) | `metatron login` per person |
| 14 | arg0 trick, single binary (`arg0/`, `codex-cli/`) | Install without Python; sandbox helper inside the same binary |
| 15 | UI/core split over a JSON-RPC-like boundary | A future IDE or SDK client is cheap |

Do not adopt: Responses-API wire code, hosted web_search, apply_patch, code mode, multi-agent tools, plugin marketplace, voice, ChatGPT login, cloud tasks.

---

## 3. Rust workspace and where it lives

**Proposed (confirm): a `metatron/` Cargo workspace in this repository.** Reasons: server work (MT-10) and client change together and one PR can carry both with the contract tests; the installer, release flow, `THIRD-PARTY-NOTICES.md` and Apache-2.0 licence are here; the Python TUI to be replaced is here, so golden-transcript parity tests can run both. Isolate cost: a path-filtered CI workflow (`.github/workflows/metatron.yml`) so Rust builds run only for `metatron/**`, and `metatron/` has its own `rust-toolchain.toml`, `deny.toml` and `Cargo.lock`. Fallback: a separate repo if build times or release cadence hurt.

```
metatron/
  Cargo.toml  rust-toolchain.toml (stable, our own pin)  deny.toml  THIRD-PARTY (links to ../THIRD-PARTY-NOTICES.md)
  crates/
    metatron-cli/       binary; clap subcommands: (default TUI), exec, login, resume, doctor, sandbox, mcp, completion; arg0 dispatch
    metatron-tui/       ratatui UI: composer, streaming transcript, approval overlay, tool cells, dashboard panes
    metatron-core/      turn loop, Tool trait + registry/router, orchestrator (approve, run, escalate), context assembly
    metatron-protocol/  serde types: turn events, items, tool calls/results, approvals; JSONL event schema
    metatron-boss-client/ reqwest client: auth, SSE turn stream, idempotency, retries, policy fetch, audit upload
    metatron-exec/      PTY/pipes, process groups, timeouts, head+tail buffer, yield-and-poll sessions
    metatron-sandbox/   seatbelt profiles, bwrap/landlock/seccomp wrapper, helper mode
    metatron-policy/    rule engine, BOSS policy sync, risk classes (host / site / command)
    metatron-remote/    ssh runner (system ssh/scp first; russh later), host registry, vault lease use, transfer
    metatron-tools/     pcap, pyrun, dashboard, http_session, handoff, update_plan, view_image
    metatron-secrets/   keychain, cookie-jar parser, Chrome cookie reader, redaction, placeholder substitution
    metatron-mcp/       MCP client over rmcp with per-tool approval
    metatron-session/   local session JSONL, resume/fork, index
    metatron-audit/     local audit buffer + upload of decisions/outcomes
  third_party/codex/    vendored Codex leaf crates (section 9), original headers kept
```

Reuse map (from Codex at the pin):

| Our crate | Codex source | Mode |
|---|---|---|
| metatron-cli | `arg0/`, `cli/src/main.rs` | pattern |
| metatron-tui | `tui/` | pattern + selected files after a size check |
| metatron-core | `tools/orchestrator.rs`, `session/turn.rs` | pattern only; do not copy `core` |
| metatron-protocol | `exec/src/exec_events.rs`, `protocol/` | pattern |
| metatron-exec | `utils/pty`, `utils/head-tail-buffer`, `unified_exec/` | copy leaf crates, imitate the rest |
| metatron-sandbox | `sandboxing/` (seatbelt, bwrap), `linux-sandbox/` | copy/adapt |
| metatron-policy | `execpolicy/` (~3.3k lines), `shell-command/` | copy (or a simpler TOML rule format; decide in MT-7) |
| metatron-remote | `exec-server/` | pattern |
| metatron-secrets | `keyring-store/`, `network-proxy/src/secret_substitution.rs`, crate `keyring` | pattern |
| metatron-mcp | crate `rmcp` directly | do not copy `rmcp-client/` (44k lines, OpenAI enterprise auth) |
| metatron-session | `rollout/` | pattern (JSONL only; no SQLite at first) |
| metatron-audit | `otel/` decision tagging | pattern |

Third-party crates Codex also uses (root `Cargo.toml`): tokio, clap 4, ratatui 0.30, crossterm 0.29 (Codex uses a fork; start with upstream), portable-pty 0.9, reqwest 0.12, rmcp, landlock 0.4, seccompiler 0.5, keyring 3.6. Codex pins edition 2024 and a very new toolchain: choose our own stable pin. Keep the crate count small; if a crate has only one consumer, merge it back.

---

## 4. Tools, the `Tool` trait and risk classes

### 4.1 How a harness picks tools (MT-12 design note)
The model only calls tools the harness lists: about ten core tools, enabled MCP tools, and a `tool_search` tool for deferred ones; skills appear as name plus one line and are read only when relevant. The shell tool is the open door: any installed program, or a script the model writes on the fly. The model chooses what to run; the harness, here BOSS policy, decides whether it may run (safety rules, sandbox, approval mode), executes, returns output, and loops until the model answers.

### 4.2 How Codex defines tools (seen)
- One contract per tool: trait `ToolExecutor` (`tools/src/tool_executor.rs`): `tool_name()`, `spec()`, `exposure()`, `search_info()`, `supports_parallel_tool_calls()`, `handle(invocation)`. Spec and runtime cannot drift. Core adds hooks and telemetry via `CoreToolRuntime` (`core/src/tools/registry.rs`).
- Registry/router: ordered map by `ToolName` (plain or namespaced); the router turns a `function_call` into a `ToolCall`, dispatches, runs in parallel only if the tool allows (`core/src/tools/router.rs`, `registry.rs`, `parallel.rs`, `tool_dispatch_trace.rs`).
- Exposure: `Direct`, `Deferred` (hidden; found through `tool_search`, `tools/src/tool_search.rs`) or code-mode.
- Shell parsing: `shell-command/src/parse_command.rs`, `bash.rs` give rules real argv.
- MCP naming: `mcp__<server>__<tool>` (`codex-mcp/src/mcp/mod.rs`, lines ~71-96).
- Skills: folder with `SKILL.md` (name, description, optional interface/dependencies/policy). Only metadata goes to the model; body loads on mention (`$name`). Scopes: user, repo, plugin, system (`skills/src/model.rs`, `loading.rs`, `mentions.rs`, `selection.rs`).
- Per-step tool set recomputed from config, model capabilities, sandbox/approval mode (`core/src/tools/spec_plan.rs`, `step_settings.rs`).

### 4.3 Metatron `Tool` trait (proposed)
```rust
// metatron-core
pub trait Tool: Send + Sync {
    fn name(&self) -> ToolName;                       // plain, or mcp__server__tool
    fn spec(&self) -> ToolSpec;                       // name, description, JSON schema
    fn exposure(&self) -> Exposure;                   // Direct | Deferred
    fn risk(&self, args: &serde_json::Value) -> RiskClass;   // NEW vs Codex: tool classifies its own call
    fn parallel_ok(&self) -> bool { false }
    async fn run(&self, ctx: &ToolCtx, args: serde_json::Value) -> ToolOutcome;
}
```
`risk()` gives the policy engine structure, not just a command string.

### 4.4 Risk classes
| Class | Meaning | Examples |
|---|---|---|
| `read-local` | Reads files inside allowed roots | ffprobe, analysis of the evidence dir |
| `write-scratch` | Writes only in the per-session scratch dir, inside the sandbox | pyrun output, charts |
| `write-local` | Writes outside scratch | copy a result to `~/Movies` |
| `remote-read` | Read-only command on a remote host | `sar`, `vmstat`, `df` |
| `remote-write` | Mutating command on a remote host | `systemctl restart`, package installs |
| `capture` | Collects other people's traffic or data (hard-prompt class) | `tcpdump -w` |
| `secret-use` | Uses a credential or cookie (hard-prompt class) | `http_session` with a cookie jar |
| `app-control` | Drives other applications; runs outside the sandbox (hard-prompt class) | `osascript`, `shortcuts` |
| `network` | Outbound network from a sandboxed tool | fetch a URL without credentials |
| `handoff` | Creates BOSS work | `handoff_code_task` |

Hard-prompt classes (`capture`, `secret-use`, `app-control`, and writes on hosts) always ask a person, in every mode including act-on-my-behalf, and never offer "always allow".

### 4.5 Tool set
| Tool | Behaviour | Default approval (risky-only mode) |
|---|---|---|
| `shell_local` | Sandboxed PTY on the machine; read-only filesystem plus scratch dir; network off unless allowed. Command parsed to argv with a real shell parser; unparseable or nested-wrapper forms fail closed. | read-only verbs auto; others ask |
| `ssh_run` | Command on a registered BOSS host (key lent from the BOSS vault) or on a host from the user's ssh config (key stays in ssh-agent, audited as "outside BOSS vault"). Output capped, streamed. | ask; verbs like `sar/vmstat/iostat/top -b` allow by rule; `tcpdump`, `rm`, installs prompt or forbidden |
| `transfer` | scp/sftp download into the session evidence dir; upload only with approval | download of named files auto once the host is approved; upload asks |
| `pcap` | `tcpdump -w` with required filter, duration, packet and size caps on the host, then `transfer`; analysis with tshark or pyrun locally | always ask |
| `pyrun` | Model writes Python; runs in the sandbox with no network, read access to the evidence dir only, write to scratch; stdout and figures captured | auto inside the sandbox |
| `dashboard` | Charts from data files to a self-contained local HTML file and TUI sparklines; optional attach to ticket | auto (local files) |
| `http_session` | Fetch a URL with a cookie jar or a vault credential; GET by default; size and time caps (section 7) | ask per domain, per session |
| `mcp__*` | Any allowed MCP server | per-tool rule |
| `boss_*` | Thin tools onto BOSS: tickets, hosts, memory search, post finding to a ticket | existing BOSS permissions |
| `handoff_code_task` | Create a BOSS code task with findings attached | ask |
| `view_image`, `update_plan`, `tool_search` | See 4.2 | auto |

Example flow (resolve a customer ticket): read ticket via `boss_*`, `ssh_run sar`, `pcap`, `transfer`, `pyrun`, `dashboard`, post findings to the ticket; if a fix needs code, `handoff_code_task`.

---

## 5. Approval and sandbox model backed by BOSS policy

- **Modes** (MT-7): `read-only` (nothing mutating runs), `ask` (every action not trivially safe), `risky-only` (rules decide; prompt-class asks), `act-on-my-behalf` (a reviewer decides inside the person's own grants; time-boxed, revocable).
- **Rules.** Scope per host, per site, per command class; prefix rules with `match`/`not_match` examples that act as unit tests (Codex `execpolicy` format, or a simpler TOML; decide in MT-7). BOSS stores policy per person/role. The CLI fetches the bundle at start and caches it with its signature. A BOSS-managed layer can only tighten: `forbidden` and "must ask a human" cannot be overridden by local mode or by the reviewer.
- **Evaluation order for a tool call:** (1) tool-list filter (a tool BOSS forbids is never shown to the model); (2) parse and classify (argv, host/domain, `RiskClass`); (3) policy decision: `allow | prompt | forbidden`; (4) mode decides who answers a `prompt` (person, or reviewer if the class allows); (5) run in the sandbox if the tool is sandboxable; (6) on sandbox denial, ask again with the wider right (copied from the Codex orchestrator); each retry is a fresh decision; (7) audit event.
- **Act-on-my-behalf reviewer** runs **server-side through the BOSS agent path** (budgeted, audited), never as a local model call. It sees command, host, session context; it can only approve inside the person's grants; hard-prompt classes still ask the human.
- **Sandbox.** macOS Seatbelt, Linux bwrap + landlock + seccomp; if neither is available, sandboxed tools fall back to `prompt`. The sandbox protects the machine from tool output and generated Python. It does **not** protect a remote host: an ssh command is judged by policy only, and the UI says so. App-control tools run unsandboxed and rely on the human prompt.
- **Command safety.** Parse into argv with a real shell parser (vendored `shell-command`); validate flags per verb (for example `sar` interval and count numeric and capped; no output-file arguments), same idea as the Python `_inspect_host_command` allow-list (`engine/boss_engine/tools/builtin.py`). Flags like ffmpeg `-f lavfi` or `movie=` can read other files, so verb rules must cover them (TODO write the rule pack with tests, MT-9).
- **Audit.** Every call: session id, tool, risk class, argv (redacted), host or domain, decision, who decided (rule / person / reviewer), exit code, bytes, duration. Uploaded to BOSS; full output stays in the local session JSONL. Secrets are redacted before either leaves the process.

---

## 6. BOSS API contract

### 6.1 What exists today (read from the repo; verify before use)
| Concern | Today |
|---|---|
| Auth | Header `X-Boss-Api-Key` with a personal key (console: My account, API keys) or, on the install machine, the host tools' key `BOSS_HOST_API_KEY` (`boss identity host-key`). The Python client refuses redirects so the key cannot leak. |
| Turn | `POST /v1/metatron/session` returns the whole reply (`scripts/metatron_tui.py`, around lines 1403-1410); waits up to 900 s (`BOSS_METATRON_TURN_TIMEOUT`); recovers dropped turns by polling the thread. |
| Threads | `POST /v1/agent/threads/ensure`, `GET /v1/agent/threads/{id}/messages`, `GET /v1/agent/threads/{id}/progress` |
| Buttons | `POST /v1/engine/agent/action` (the console's own button path; `/pick N` in the terminal) |
| Other | `/v1/metatron/watch`, `/drive`, `/overwatch`, `/goals`; `/v1/tasks`; 970 Community operations reachable through the generic `/op` command (`docs/community/TERMINAL_CAPABILITY_MATRIX.md`) |
| Streaming machinery | `docs/community/CONVERSATION_STREAMS.md`: Talk/room replies stream live; `POST .../turns`, `/summary`, `/propose` take an `Idempotency-Key` bound to principal + exact request; concurrent retry gets 409; a complete bounded response replays its exact bytes; 1 MiB reply limit; deadline 5-600 s. Reuse this machinery. |
| Host reads | `inspect_host` allow-list in `engine/boss_engine/tools/builtin.py` (no `sar` yet: MT-1). `run_on_host` needs a paid edition, `BOSS_AGENT_HOST_SHELL=true`, and a person clicking a `propose_action` button. |
| Model gateway | `apps/model-gateway` (host-identity, server to server today). |
| MCP registry | `engine/boss_engine/services/mcp_registry.py`, `docs/community/MCP.md` |
| Skills | `list_skills`, `draft_skill`, `role_skills` grants |

### 6.2 MT-10 DRAFT: streaming agent turn with client-side tools

**Status: DRAFT proposal. Names, paths and shapes are not final. The MT-10 author owns the final contract, writes it into `docs/community/` (API contract) and the OpenAPI, and updates this section.** Principle: the model runs on BOSS (gateway, budget, receipts). The client declares tools it can run; BOSS streams `tool_call` events; the client runs them after approval and posts results; BOSS continues the same turn.

Versioning: every request carries `X-Metatron-Client: metatron/<semver> (<os>; <arch>)` and `X-Boss-Contract: <n>`. Responses carry `X-Boss-Version`. Server answers `426` with `{error:{code:"client_too_old", min_client}}` when too old. The client refuses mutating tools against a server that lacks the policy/audit endpoints.

#### Auth (device-code login, modelled on RFC 8628)
| Step | Request | Response |
|---|---|---|
| 1 | `POST /v1/auth/device/code` `{client_id:"metatron", scope:"metatron"}` | `{device_code, user_code, verification_uri, verification_uri_complete, expires_in, interval}` |
| 2 | User opens `verification_uri_complete` in a browser signed in to the console and approves (step-up per existing identity rules). | |
| 3 | Client polls `POST /v1/auth/device/token` `{grant_type:"urn:ietf:params:oauth:grant-type:device_code", device_code}` every `interval` s | `200 {access_token, refresh_token, token_type:"Bearer", expires_in, principal:{id, actor, roles}}` or `400 {error: authorization_pending | slow_down | expired_token | access_denied}` |
| 4 | Refresh: `POST /v1/auth/token` `{grant_type:"refresh_token", refresh_token}`; revoke: `POST /v1/auth/revoke` (also used by `metatron logout`) | |

Tokens are stored in the OS keychain (file fallback 0600 only with an explicit flag). Requests send `Authorization: Bearer`. `X-Boss-Api-Key` stays accepted for the local-install mode and for a personal API key given by env var (this is the dev path for "hello BOSS"). Redirects to another host are never followed; headers are never logged. Tokens are per person, scoped, revocable and visible in the console.

#### Capabilities
`GET /v1/metatron/capabilities` -> `{contract:n, min_client, features:["turns.stream","tools.client","policy","audit","device_login","credential_lease"], limits:{tool_result_inline_bytes, turn_deadline_s}}`.

#### Start a turn
`POST /v1/metatron/turns` with `Accept: text/event-stream` and `Idempotency-Key: <uuid per user press, reused on retries>`.
```json
{
  "project_id": "…", "thread_id": "…|null", "agent": "…|null",
  "message": "capture sar on host web-3 for 60s and attach to ticket T-123",
  "mode": "ask",                                   // read-only | ask | risky-only | act-on-my-behalf
  "client": {"name":"metatron","version":"0.1.0","os":"macos","arch":"arm64"},
  "context": {"cwd_label":"~/work/ops", "agents_md":"…", "attachments":[{"type":"image","media_type":"image/png","data_ref":"…"}]},
  "client_tools": [
    {"name":"ssh_run","description":"…","input_schema":{…JSON Schema…},"risk_class":"remote-read|remote-write","exposure":"direct"},
    {"name":"pyrun","description":"…","input_schema":{…},"risk_class":"write-scratch","exposure":"direct"}
  ]
}
```
Server rules: authenticate and authorise the principal; filter `client_tools` by role/policy (a forbidden tool is dropped, and the response says which); merge with BOSS server tools; reserve budget, then call the model through the gateway. The idempotency semantics are those of `CONVERSATION_STREAMS.md` (409 on concurrent retry, byte replay of a complete response, refusal if the same key carries a different body).

#### SSE events
Each event has `id: <turn_id>:<seq>` so a client can resume. Heartbeat: a comment line `: ping` every 15 s.
| `event:` | `data` payload | Notes |
|---|---|---|
| `turn.started` | `{turn_id, thread_id, model, mode, tools:[names visible to the model], dropped:[{name, reason}]}` | The `tools` list is recorded in the session JSONL (what the model could see). |
| `text.delta` | `{text}` | Assistant text. |
| `server_tool.started` / `server_tool.finished` | `{call_id, name, summary, ok}` | BOSS-side tools (for display only; the server ran them). |
| `tool_call` | `{call_id, name, arguments:{…}, risk_hint, deadline_s}` | Client must run it (after approval) and POST a result before `deadline_s` (default 900). `risk_hint` is advisory; the client classifies on its own. |
| `policy.denied` | `{call_id, reason, rule_id}` | Server refused a call; no client action. |
| `usage` | `{input_tokens, output_tokens, cost:{amount, currency, source}}` | |
| `turn.completed` | `{turn_id, stop_reason: end_turn|max_tokens|cancelled}` | |
| `turn.failed` | `{turn_id, code, message, retryable}` | Provider or budget failure; the unresolved-receipt rules of `CONVERSATION_STREAMS.md` apply. |

While the stream is open and waiting for a tool result the turn is in state `awaiting_tool_result`; if no result arrives by the deadline the turn fails with `code: tool_result_timeout`.

#### Tool results
`POST /v1/metatron/turns/{turn_id}/tool_results`, `Idempotency-Key: <turn_id>:<call_id>`:
```json
{
  "call_id": "…",
  "status": "ok | error | denied | cancelled",
  "output": {"content":[{"type":"text","text":"…"},{"type":"image","media_type":"image/png","data_b64":"…"}],
             "truncated": true, "exit_code": 0, "bytes": 18234, "duration_ms": 61000},
  "decision": {"by":"rule|person|reviewer", "rule_id":"…", "mode":"risky-only"}
}
```
Returns `202`. The original stream continues with more `text.delta` or `tool_call`. Limits: inline output to the server is capped (default 64 KiB, head plus tail, with `truncated:true`); full output stays in the local session. Output is redacted for secrets before upload. A `denied` result tells the model the person or policy refused, so it can adapt.

#### Resume, cancel
- `GET /v1/metatron/turns/{turn_id}/events` with `Last-Event-ID` replays missed events then continues (replaces the Python client's poll-the-thread recovery).
- `POST /v1/metatron/turns/{turn_id}/cancel` -> `202`; stream ends with `turn.completed{stop_reason:"cancelled"}`. As in `CONVERSATION_STREAMS.md`, cancel does not promise the provider stops.
- `POST /v1/metatron/session` remains for the Python TUI and `--print` until retired.

#### Policy
`GET /v1/metatron/policy` with `If-None-Match` -> `200` or `304`. Body: a signed bundle (JWS over JSON, server key published at `/v1/metatron/policy/keys`):
```json
{"version":"…","mode_floor":"ask","tools_allowed":["shell_local","ssh_run","pyrun"],
 "rules":[{"id":"r-sar","match":{"tool":"ssh_run","argv_prefix":["sar"]},"decision":"allow",
           "justification":"read-only sysstat","examples":{"match":["sar -u 1 5"],"not_match":["sar -o /tmp/x 1 5"]}},
          {"id":"r-pcap","match":{"tool":"pcap"},"decision":"prompt","hard":true}],
 "cookie_mode":{"enabled":false,"domains":[]},"mcp":{"servers":[…]},"expires_at":"…"}
```
The client refuses an unsigned or expired bundle for mutating tools; offline it falls back to read-only local analysis. Rule `examples` are executed as tests when the bundle loads. `hard:true` rules cannot be satisfied by the reviewer.

#### Reviewer (act-on-my-behalf)
`POST /v1/metatron/approvals/review` `{session_id, turn_id, call:{tool, argv_redacted, host, domain, risk_class}, mode}` -> `{decision:"approve|deny|escalate", reason, reviewer_receipt_id}`. Runs through the agent path (budgeted, audited). `escalate` means ask the person. Grants are time-boxed: `POST /v1/metatron/grants` `{scope, ttl_s}` creates, `DELETE /v1/metatron/grants/{id}` revokes.

#### Hosts and credential leases (TODO: inspect the vault API; this may need new server work)
`GET /v1/metatron/hosts` lists hosts the person may use. `POST /v1/metatron/hosts/{id}/credential-lease` `{purpose, ttl_s, session_id}` -> `{lease_id, kind:"ssh_key", expires_at, material}` where `material` is delivered once over TLS, held in memory or a 0600 temp file removed after use, and the lease use is audited. If the vault will not lend key material, the alternative is a short-lived SSH certificate. Decide in MT-10/MT-3.

#### Audit ingest
`POST /v1/metatron/audit` `{session_id, events:[{event_id, ts, kind, tool, risk_class, argv_redacted, host, domain, cookie_names, method, path, status, decision, decided_by, rule_id, exit_code, bytes, duration_ms}]}` -> `{accepted:[event_id], rejected:[{event_id, reason}]}`. Idempotent by `event_id` (client-generated UUID). The client buffers on disk and retries. Events attach to the ticket or incident when `ticket_id` is set. Never included: cookie or token values, file contents, unredacted argv secrets.

#### Errors
`{ "error": { "code": "…", "message": "…", "retryable": bool } }`. Status use: 401 unauthenticated or expired (client refreshes once), 403 policy or permission, 409 idempotency conflict, 413 too large, 426 client too old, 429 rate or budget, 5xx retryable with backoff. Codes to define: `budget_exhausted`, `tool_result_timeout`, `policy_denied`, `client_too_old`, `unresolved_receipt`.

#### Types
Generate Rust types from BOSS's OpenAPI for the typed calls; keep a generic `/op` passthrough for the long tail. Contract tests live next to the server and run against the Rust client in CI.

---

## 7. Credentials, cookies and sessions

### 7.1 Credentials
- BOSS vault is the source for host keys and service credentials. Metatron asks BOSS for a short-lived scoped lease (6.2). The user's own ssh config and agent stay usable, flagged "outside BOSS vault" in the audit.
- Login is per person (device code), token in the OS keychain.

### 7.2 Cookie / session access: the risk, plainly
A session cookie is the person's whole logged-in identity on that site. Whoever holds it can do everything the person can do there, bypassing password and usually MFA, until it expires or is revoked. Residual risks even with every safeguard: (1) prompt injection from page or dashboard text telling the agent to misuse it; (2) reading Chrome's cookie store grabs all cookies, which is how infostealers work, so a security product may flag it; (3) a cookie that leaks into logs, a transcript, a ticket or a prompt is an account takeover; (4) read-only intent is not enforceable on the site, only our method restriction limits it; (5) a site's terms may forbid automated session use.

### 7.3 Safety rules (enforced in `metatron-secrets` and the network layer, not in the prompt)
| Rule | How |
|---|---|
| Per-domain consent, per session | First use asks "Use your cookie for grafana.example.com for this session? (read-only GET)". Consent keys on the exact host(s) shown; no wildcards; ends with the session or a short TTL. |
| Two sources only | (a) a cookie file the person names (Netscape or JSON export); (b) the local Chrome store, read only after an explicit per-use ask, and only the rows for the approved domain are decrypted. The OS keychain prompt for "Chrome Safe Storage" is left visible and never auto-answered or bypassed. Firefox/Safari: TODO. |
| Never stored in BOSS | Cookie values stay in process memory; never written to the session JSONL, never sent to BOSS audit or the model gateway. Audit holds only domain, source kind, cookie names, time, URL path, method, status, bytes. |
| Model never sees the value | The tool holds the jar; an in-process fetcher attaches `Cookie:` only for the approved host; the model gets responses, not request headers. A redirect to another host drops the cookie. |
| Method and scope limits | GET/HEAD by default; anything else is a separate prompt showing method and URL. Response size and time caps. Private/loopback targets blocked unless named. |
| Output redaction | Responses are scanned for `Set-Cookie`, tokens and the cookie values before storage or display. |
| Prefer better paths | Order: MCP server, then a narrow API token from the BOSS vault (a Grafana service-account token is far safer than a browser cookie), then the cookie. Cookie mode is labelled "last resort". |
| Policy | BOSS policy can disable cookie mode per person/role/domain or allow only listed domains. Default off for roles that are not operators. |
| Revocation note | The UI tells the person to log out of the site afterwards if the cookie was exposed for a long session. |

Grafana "what happened between T1 and T2": use Grafana's HTTP API (`/api/ds/query`, dashboard JSON) with the approved credential, fetch panel data for the window, run `pyrun`, and explain with numbers cited to panels and timestamps. The cookie handling needs review by someone other than its author (MT-6 acceptance).

---

## 8. Tool ecosystem (MT-9)

- **Shell is the open door.** Same `shell_local` and `ssh_run` under BOSS policy and approval modes; macOS app automation via `osascript`/`shortcuts` is class `app-control` (always prompt, unsandboxed).
- **Tool list handshake.** At turn start the client sends the tools it can run (name, schema, risk class); BOSS filters by role and policy, merges with server tools, and shows the model the union (6.2). The exact list is recorded in the session JSONL.
- **MCP.** Source of truth is BOSS's MCP registry (`engine/boss_engine/services/mcp_registry.py`, `docs/community/MCP.md`). The CLI asks which servers the person may use and gets per-server entries (command or URL, vault auth reference, allowed tools, risk classes): (a) servers BOSS hosts or proxies (called through BOSS, audited server-side); (b) local stdio servers the person runs (started via `rmcp`, policy still applies). Namespacing `mcp__<server>__<tool>`; large servers are `Deferred` and found by `tool_search`. Per-tool policy key `mcp:<server>:<tool>`.
- **Skills.** Load BOSS-granted skills (`list_skills`, `role_skills`) and local `SKILL.md` folders (Codex format, so Codex or Claude Code skills work). Only name and description go in the prompt; the body is fetched on demand. A local skill cannot widen permissions; declared dependencies must also be allowed by policy; skill scripts run through `shell_local` and its approvals.
- **view_image.** `view_image(path)` reads from the evidence or scratch dirs (elsewhere only if approved), downsizes, sends as an image item (needs server turn support for image content, TODO verify).
- **Search.** Cannot copy Codex's hosted tool. Order: BOSS-side search/fetch (Discovery research exists as `create_discovery`), an MCP search server (SearXNG, Brave, Tavily) from the registry, or the model provider's search through the model gateway, or `http_session` on a user-chosen URL. All pass the same domain consent and audit rules.
- **Per-step tool set** recomputed from role/policy, mode (read-only hides write tools), sandbox availability, host connectivity.

### Worked example: "edit this video" (risky-only mode)
Request: trim `demo.mov` to 00:12-01:40, add a logo, export 1080p MP4, open it in QuickTime. Policy: ffmpeg allowed when it writes only to scratch; `osascript` prompt always; no network.

| Step | Model asks for | Classification | Decision |
|---|---|---|---|
| 1 | `ffprobe -v error -show_format -show_streams demo.mov` | `shell_local`, read-only verb, path under an allowed root | auto (rule), sandbox read-only |
| 2 | Read access to a file outside the workspace | read root outside default | one-time prompt for that path |
| 3 | `ffmpeg -ss 12 -to 100 -i demo.mov -i logo.png -filter_complex overlay=10:10 -vf scale=-2:1080 -c:v libx264 -crf 20 scratch/demo_cut.mp4` | writes scratch only, no network | auto under the "ffmpeg to scratch" rule; PTY output streamed and capped; long timeout via yield-and-poll |
| 4 | Extract a frame, `view_image(scratch/check.png)` | shell_local + view_image (scratch) | auto |
| 5 | Copy result next to the original | `write-local` | prompt showing source and destination |
| 6 | `osascript` to open it in QuickTime | `app-control`, outside the sandbox, full command shown | always prompt; no "always allow"; still asks in act-on-my-behalf |
| 7 | Final answer with output path and exact commands | session JSONL + audit batch (argv redacted, no file contents) | |

In act-on-my-behalf mode steps 2 and 5 can be approved by the BOSS reviewer inside the person's grants; step 6 still asks. Without sandbox support, step 3 falls back to prompt. ffmpeg inputs like `-f lavfi` or `movie=` can read other files: the verb rules need tests.

---

## 9. Reusing Codex code under Apache-2.0

BOSS is Apache-2.0 (root `LICENSE`), so combining is allowed. Obligations (Apache-2.0 section 4; not legal advice, have counsel confirm):
1. Ship the Apache-2.0 text with the binary and in the repo.
2. Keep every copyright, patent, trademark and attribution notice in copied files.
3. Mark each modified copied file with a prominent "changed by BOSS" notice.
4. Codex's `NOTICE` says some code derives from Ratatui (MIT, Florian Dehau and the Ratatui Developers). Copy that attribution into `THIRD-PARTY-NOTICES.md` and keep the MIT text for any ratatui-derived file copied from `tui/`.
5. Do not use OpenAI or Codex names or logos as product names; ours is Metatron.
6. The patent grant ends for a contributor who sues over patents in the work; note it, no action.
7. Process: vendor at the pinned commit into `metatron/third_party/codex/<crate>/` with headers kept; record commit and crate path in `THIRD-PARTY-NOTICES.md` (it already has a per-component format); run `cargo deny` (Codex ships `codex-rs/deny.toml` as a starting point) and a licence scan over the whole dependency tree before every release.
8. Codex crates are workspace version 0.0.0 (root `Cargo.toml`); assume they are not on crates.io: copy, do not depend.

Copy (small, leaf-like; TODO verify each with `cargo tree` before copying): `utils/pty`, `utils/head-tail-buffer`, `execpolicy`, `shell-command`, `keyring-store`, `sandboxing` seatbelt/bwrap parts, `linux-sandbox`, `arg0`. Imitate only: orchestrator, turn loop, rollout, exec events. Skip: `core`, `app-server`, `network-proxy` (36k lines on the `rama` stack; start with an in-process fetcher), `rmcp-client`, `login`, `windows-sandbox-rs`.

---

## 10. Migration off the Python TUI

Today: `scripts/metatron_tui.py` (about 2.6k lines, stdlib only), optional `scripts/metatron_render.py` (Rich) and `scripts/metatron_shell.py` (prompt_toolkit), thin loader `engine/boss_engine/cli/metatron.py`, launcher `boss metatron` in `scripts/install/boss.sh` (`cmd_metatron`, builds a venv on first run), and `make metatron`.

| Step | What | Python TUI |
|---|---|---|
| 0 | Freeze to bug fixes; list its slash commands and `/op` behaviours as the parity table (27 commands) | live, default |
| 1 | Rust `metatron exec` and `metatron login` on existing endpoints (no new server work) | live |
| 2 | Rust TUI with chat, pickers and `/op`; streaming once MT-10 SSE exists; ships side by side as `boss metatron --rust` | live, default |
| 3 | Local tools, approvals, sandbox, audit exist only in Rust | maintenance only |
| 4 | Rust is default; `boss metatron` launches the binary (installer downloads the per-OS binary, no venv); Python kept one release behind `--legacy` | legacy |
| 5 | Delete the three scripts, the loader and the venv bootstrap in `boss.sh` | removed |

Exit criteria for step 4: parity table all green; golden-transcript tests (same prompt script against both clients); first run works with no Python on the machine. The installer and `boss upgrade` must fetch the new binary (a CLI fix is invisible until the binary is rebuilt and distributed).

---

## 11. Build plan and ticket map

| Phase | Ticket | Scope | Size |
|---|---|---|---|
| P0 | MT-0 (1380) | Toolchain, cross-compile CI, workspace, vendoring script, notices | S |
| P0 server | MT-10 (1381) | Streaming turn with client tools, device-code login, policy bundle, audit ingest (section 6.2) | L |
| P1 | MT-1 (1368) | `inspect_host` allow-list adds sar/vmstat/iostat/mpstat/top -b + diagnostics bundle (Python/server, independent of Rust) | S |
| P1 | MT-2 (1369) | cli, boss-client, protocol, login + keychain, `exec --json`, config/profiles, completion, AGENTS.md, session resume. Split 2a client+login+exec (M), 2b TUI parity (L) | M + L |
| P2 | MT-7 (1375) | Modes + BOSS-stored policy + server-side reviewer | M |
| P2 | MT-3 (1370) | exec (PTY), sandbox, policy, remote ssh with vault lease, approval overlay, audit upload. Split 3a local + sandbox + approvals (L), 3b ssh + vault + audit (M) | L + M |
| P2 | MT-9 (1378) | Tool trait/registry, MCP, skills, search, view_image, verb rule pack | M |
| P3 | MT-4 (1371) | transfer, pcap, pyrun, dashboard, post to ticket | L |
| P3 | MT-6 (1374) | http_session + secrets, Grafana recipe, MCP first | M |
| P4 | MT-5 (1372) | handoff to code task, MCP polish, SDK/IDE later, hooks | M |
| P5 | MT-11 (1382) | Binaries, installer, retire the Python TUI | M |

Order of risk: (1) the server streaming protocol with client tools; (2) the macOS and Linux sandbox; (3) cookie handling, reviewed by a second person; (4) TUI parity.

Dependency order: MT-0 -> MT-10 -> MT-2 / MT-1 -> MT-7 -> MT-3 -> MT-9 -> MT-4 -> MT-6 -> MT-5 -> MT-11. MT-1 has no Rust dependency and can ship at any time.

### Testing strategy
- Rust: `cargo test` per crate, `cargo clippy -D warnings`, `cargo deny check`, rule-pack `match`/`not_match` tests, golden JSONL transcripts for `exec --json`, a scripted fake BOSS server (SSE) for client tests, sandbox tests that attempt forbidden reads/writes and must fail.
- Server (Python): follow the repo's test practice: run the tests the change touches, one file at a time, against the test database (named like a test one, for example `boss_test`); full suites once as the final gate.
- Contract: one shared fixture set of SSE transcripts used by both the server tests and the Rust client tests.

## 12. Open questions
1. Server-side loop (recommended) vs a client-side loop calling the gateway directly: confirm.
2. Can the vault lend an ssh key to a local process, and for how long? Or use short-lived SSH certificates?
3. Offline use: proposed read-only local analysis only.
4. Windows: not in v1.
5. Rule format: Codex Starlark `execpolicy` vs a simpler TOML (decide in MT-7).
6. Where the Rust code lives: proposed `metatron/` in this repo (section 3); confirm.
7. Legal review of vendoring; Firefox/Safari cookie sources; verify `core/src/exec_policy.rs` default rules.
