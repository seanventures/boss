# Metatron vs OpenAI Codex CLI: feature gap

Date 2026-11-17. Read-only research. Codex source studied at commit 05ae1b0f (never built or run); Codex's own `docs/` folder is mostly stubs, so the inventory comes from the Rust source (`codex-rs/cli`, `tui`, `exec`, `config`, `protocol`). Counts such as "about 70 slash commands" are approximate and include experimental and debug commands.
Overview: [README.md](README.md). Design: [ARCHITECTURE.md](ARCHITECTURE.md).

## 1. The ssh + sar question

Resolved for read-only diagnostics via `MT-1` (`BOSSAPP-1368`). Free-form remote shells remain gated; bounded host diagnostics ship in Community today:

| Path | Can it capture `sar`? | Why / How |
|---|---|---|
| Agent tool `inspect_host` (read-only SSH to a registered host) from the console agent, Metatron or the boss MCP | Yes (bounded) | `engine/boss_engine/tools/host_diagnostics_rules.py` + `_inspect_host_command` allow `sar`, `vmstat`, `iostat`, `mpstat`, and `top -b -n 1` with strict flag validation, numeric caps (`interval <= 10`, `count <= 10`, `interval * count <= 30`, no `-o`/`-f`/1-int infinite streams), and metacharacter refusal before DB/SSH. Returns structured `missing_package: "sysstat"` install hint if absent. Also exports `policy_rule_table()` for `metatron-policy`. |
| Agent tool `collect_diagnostics` (`kind="read"`, `permission="host:exec"`) | Yes (snapshot) | Captures bounded CPU, memory, I/O, network, disk, and `top -b -n 1` sections (`interval_seconds * count <= 10`), degrades gracefully when `sysstat` is missing, redacts secrets, and returns `sections`, `ticket_markdown` (`<= 6000` chars), and `artifact_payload`. |
| Agent tool `run_on_host` | Only if all hold | (1) a paid edition (Community is hard off); (2) `BOSS_AGENT_HOST_SHELL=true` (default false); (3) the agent cannot call it itself: it must `propose_action`, rendering a button with the exact host and command that a person clicks (in Metatron, `/pick N`); (4) the host is a registered `deployment_hosts` row with an SSH key secret and `sysstat` installed. |
| `/op` against `/v1/deployment-hosts` | No | Register/patch operations only; no generic run-command. |
| Plain `ssh` in your own terminal | Yes, outside BOSS | No governed record, no ticket linkage. |

Remaining long-term step: free-form host work through a Community-safe governed path (`MT-3` Rust `ssh_run` with ChangeSet, policy, and audit).

## 2. What Metatron does today

One client, `scripts/metatron_tui.py` (about 2.6k lines, stdlib only), with an optional Rich renderer (`metatron_render.py`) and prompt_toolkit shell (`metatron_shell.py`).
- Entry points: `boss metatron [--print CMD] [--json] [--plain] [--new]`; `make metatron` (inside the api container); `python -m boss_engine.cli.metatron`. `boss` itself is the install lifecycle tool, not a coding CLI. The boss MCP server lets an external coding agent drive BOSS tools.
- Auth: `BOSS_API_KEY` (never printed), `BOSS_API_URL`, `BOSS_PROJECT_ID`; on the install machine it uses the install's host key. No per-person login for a remote laptop yet.
- Can do: chat with the one BOSS agent (same thread and tools as the console); numbered offers (`/pick`, `/confirm`, `/expand`, `/thread`, `/new`); `/agent`, `/model`, `/project(s)`, `/status`; work views (`/tasks`, `/task create`, `/decisions`, `/runs`, `/follow`, `/alerts`, `/goals`, captain `/watch /overwatch /drive /go`); full API reach through `/ops /describe /op /api /coverage` (970 Community operations); one-shot `--print` with `--json`; history across sessions with credential lines filtered.
- Cannot do: read or edit local files, run local commands, show a diff of local changes, accept images, mention files, sandbox, keep local sessions. It is a thin remote client.

## 3. Codex CLI inventory (cited)

| Area | Feature | Source |
|---|---|---|
| TUI | Composer with history, popups, `@` file search, image paste, external editor, vim mode | `tui/src/bottom_pane/chat_composer*`, `file_search.rs`, `external_editor.rs` |
| TUI | About 70 slash commands (/model /permissions /review /init /compact /plan /diff /mention /status /resume /fork /new /rename /archive /goal /agents /side /copy /export /mcp /skills /hooks /memories /worktree /cd /ps /stop /clear /theme /statusline /voice /ide /logout /feedback /usage ...) | `tui/src/slash_command.rs` |
| TUI | Approval overlay for commands/patches/network, diff rendering, exec cells, backtrack | `bottom_pane/approval_overlay*`, `diff_render.rs`, `exec_cell`, `app_backtrack.rs` |
| Non-interactive | `codex exec`, `--json` JSONL events, `--output-schema`, `-o` last-message file, stdin prompt, `--ephemeral` | `exec/src/cli.rs`, `event_processor_with_jsonl_output.rs` |
| Sessions | `resume` (picker, `--last`), `fork`, `archive`, `delete` | `cli/src/main.rs` |
| Review/Apply | `codex review`; `codex apply` and the apply_patch tool | `main.rs`, `apply-patch` |
| Safety | Sandbox modes read-only / workspace-write / danger-full-access; approval policies untrusted / on-request / granular / never | `protocol/src/config_types.rs`, `protocol.rs` |
| Safety | OS sandboxes (Seatbelt, bwrap, Windows), network proxy, execpolicy rules, trust per project, auto-review | `linux-sandbox`, `windows-sandbox-rs`, `sandboxing`, `network-proxy`, `execpolicy` |
| Config | `~/.codex/config.toml`, `-c key=value`, profiles, `--strict-config`, managed config | `config/src/config_toml.rs`, `cli/src/config_override_args.rs` |
| Models | Several providers, effort levels, model picker | `model-provider*`, `ollama`, `lmstudio` |
| MCP | Client (stdio/http, OAuth), `codex mcp` manage; app-server for IDEs | `rmcp-client`, `codex-mcp`, `cli/src/mcp_cmd.rs` |
| Instructions | Hierarchical AGENTS.md, skills, plugins, hooks, memories | `docs/agents_md.md`, `docs/skills.md` |
| Tools | shell, apply_patch, hosted web search, image view, multi-agent, code mode | `core/src/tools/handlers` |
| Auth | ChatGPT sign-in, API key, device login, keyring storage | `cli/src/login.rs`, `keyring-store` |
| Ops | `completion`, `doctor`, `update`, notify, OpenTelemetry, `--remote`, cloud tasks | `main.rs`, `otel` |
| IDE | /ide context, app-server protocol, TS and Python SDKs | `tui/src/ide_context`, `app-server`, `sdk/` |

## 4. Gap table (none / small / medium / large)

### A. Chat and TUI
| Feature | Codex | Metatron today | Gap | Plan |
|---|---|---|---|---|
| Composer, history, completion, bottom bar | Yes | Yes | none/small | Multi-line, external editor, vim keys missing (MT-2) |
| Slash commands | ~70 | 27 | small | Different set on purpose (BOSS domain). Add /init /compact /diff /copy /export /resume /fork (MT-2) |
| `@` file mentions | Yes | No | large | Local read-only file access (MT-2, MT-3) |
| Image input | Yes | No | medium | Needs the turn to accept attachments (MT-9 `view_image`, MT-10) |
| Streaming output | Yes | Spinner; reply arrives whole | medium | SSE turn (MT-10, MT-2) |
| Session resume/fork | Local | Server thread; no picker | medium | `/resume` picker, `/fork` (MT-2) |
| Non-interactive + JSON | exec, JSONL, schema, stdin | `--print --json` gives one final JSON | medium | `metatron exec` with JSONL event stream, stdin, `-o`, schema, exit codes (MT-2) |
| Shell completion | Yes | No | small | bash/zsh/fish (MT-2) |
| Themes, statusline, vim, voice | Yes | No | small | Skip voice |

### B. Local agent (adapted: Metatron never writes code)
| Feature | Codex | Metatron today | Gap | Plan |
|---|---|---|---|---|
| Read local files | Yes | No | large | Read-only roots, evidence dir (MT-3) |
| Edit files, apply_patch | Yes | No (BOSS code tasks end in a PR) | by design | **Not copied.** Hand-off to a BOSS code task (MT-5) |
| Run local shell | Yes (sandboxed) | No | large | `shell_local` with sandbox and policy (MT-3) |
| AGENTS.md | Yes | Server-side standards and skills only | medium | Read AGENTS.md/CLAUDE.md as context (MT-2) |
| Web search | Hosted OpenAI tool | Discovery runs | small | Search MCP or BOSS-side tool (MT-9) |
| `apply` diff to working tree | Yes | No | by design | Not needed |
| Review mode | `codex review` | `boss review` + governed pipeline review | small | later |

### C. Safety, sandbox, approvals
| Feature | Codex | Metatron today | Gap | Plan |
|---|---|---|---|---|
| Sandbox modes / OS sandbox | Yes | None locally (server-side sandbox for runs) | large | Seatbelt, bwrap/landlock/seccomp (MT-3) |
| Approval policies | untrusted / on-request / never | Human gates on decisions, `propose_action` buttons | none | BOSS is stronger (audited, role-based). Add modes for local tools (MT-7) |
| Exec policy rules / trust | Yes | Edition + permission model | small | BOSS-stored rules with tests (MT-7) |

### D. Integrations
| Feature | Codex | Metatron today | Gap | Plan |
|---|---|---|---|---|
| MCP client | Yes | No | medium | MCP via BOSS registry (MT-9, MT-5) |
| Config file + profiles | config.toml, `-c`, profiles | env vars only | medium | `~/.config/metatron/config.toml` (MT-2) |
| Auth | ChatGPT login, API key, device flow | host key or `BOSS_API_KEY` | medium/large | Device-code login, keychain (MT-10, MT-2) |
| Notifications | notify command | No | small | later |
| IDE integration | app-server, SDKs | None | large | P3, after a stable turn protocol |
| Plugins/skills/hooks | Yes | Skills and agents in console | small | Skills in MT-9; hooks in MT-5 |

### E. BOSS-only strengths (Codex lacks)
Governed runs (PRD meeting, human approval, role-separated build/test/review, fail-closed quality gate, PR); decisions inbox and approvals as first-class with audit and actor; multi-repo and multi-project, deployments, hosts fleet, alerts, budgets, model routing across engines; knowledge graph, memory and recall; 970 API operations through `/op`; host management with secret-backed SSH keys.

### F. Do not copy
- "Agent edits and runs anything on your machine with its own approvals" as the default for BOSS work. The governed pipeline builds, tests and reviews better.
- Codex cloud tasks, ChatGPT-only auth, desktop launcher, voice, themes, analytics.
- A second decision engine in the CLI (one agent path).
- A command filter for arbitrary host shell: keep allow-listed verbs plus policy and person-clicked approvals.
