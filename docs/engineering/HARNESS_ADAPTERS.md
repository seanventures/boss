# Harness adapters: ACP, process, http, installable manifests, and the agent's kit

BOSSAPP-853 (harness breadth) and BOSSAPP-854 (skills and MCP on every harness). Read
[ENGINE_CONTRACT.md](ENGINE_CONTRACT.md) first: everything here is held to it. Behaviour is the
same on every engine; a harness is **verified** only after a governed code task has run end to
end on it. Every harness added here starts **unverified**.

| Kind | Driver | What BOSS gets from it |
|------|--------|------------------------|
| `cli` | `GenericCliCodeAgent` (+ purpose-written `CodexCodeAgent`, `ClaudeCodeAgent`) | one process per turn, files edited in place |
| `acp` | `AcpCodeAgent` (`acp_code_agent.py`, wire in `acp_protocol.py`) | sessions, structured tool calls, permission requests BOSS answers |
| `process` | `ProcessCodeAgent` (`custom_harness.py`) | an in-house agent behind an argv contract |
| `http` | `HttpCodeAgent` (`custom_harness.py`) | an in-house agent behind a submit/poll contract |

For every kind the agent edits files **in place** in the workspace, the git diff is the change of
record, and BOSS re-runs the tests itself. What an agent reports is a summary and transcript,
never a verdict.

## ACP (Agent Client Protocol)

BOSS is the ACP **client**; the agent (Gemini CLI `gemini --experimental-acp`, OpenCode
`opencode acp`) is started on stdio and spoken to in JSON-RPC 2.0, one message per line.
`acp_protocol.PROTOCOL_VERSION = 1`; an agent answering `initialize` with another version is
refused before any work starts. The message layer is versioned separately (`LAYER_VERSION`).

Session lifecycle: `initialize` → `session/new` (or `session/load` on a follow-up turn when the
agent offers `loadSession`; the replayed history is not re-recorded) → `session/prompt` →
stopReason. `session/cancel` is sent on the stage deadline and when a call is held.

BOSS offers `fs.readTextFile` always, `fs.writeTextFile` only to a writing session, and no
terminal. Both file methods are confined to the session's cwd.

### Tool-call mapping (ACP `ToolKind` → engine contract canonical name)

| ACP kind | Canonical tool | Note |
|----------|----------------|------|
| `read` | `Read` | |
| `edit` | `Edit` | |
| `delete` | `Edit` | changes the tree; `acp_kind: delete` kept in the arguments |
| `move` | `Edit` | changes the tree; `acp_kind: move` kept in the arguments |
| `search` | `Grep` | |
| `execute` | `Bash` | `rawInput.cmd`/`command` → `command` |
| `fetch` | `WebFetch` | |
| `think` | `TodoWrite` | |
| `other` / none | the title through the contract's aliases, else the agent's own name | an MCP tool stays itself |

Arguments: `rawInput`, plus the first `locations[].path` as `path`, then the contract's key
normalisation (`file_path`, `command`). `tool_call` / `tool_call_update` with status
`completed`/`failed` become `tool_call` / `tool_result` transcript rows; `agent_message_chunk`
text is the summary. StopReason: `end_turn` → `done`; `max_tokens`, `max_turn_requests` →
`turn_cap`; `refusal`, `cancelled` → `error` (a cancel BOSS sent to hold a call → `awaiting_approval`).

### Permission requests → the existing mid-run approval path

1. A read-only session rejects every write tool (`Write`, `Edit`, `MultiEdit`, `NotebookEdit`).
2. `tool_gateway.verdict(name, agent.tool_policy, args)`: `run` → `allow_once`; `deny` → `reject_once`.
3. `wait`:
   * **MCP gateway on** (`BOSS_MCP_GATEWAY=on`, BOSSAPP-814) with the run's gateway token: a
     `gateway_request_approval` decision is raised and the call **parks** until a person answers
     it in Approvals (`mcp_gateway.raise_decision` + `park`, the stage clock stopped).
     Approved → `allow_once`; rejected/withdrawn/timed out → `reject_once`.
   * **Gateway off** (pre-814): `reject_once`, `session/cancel`, and the turn returns
     `stop_reason: awaiting_approval` with `pending_tool`, `pending_args_sha256` and
     `pending_args_preview` — the native loop's shape for an Ask tool.

BOSS never selects an `allow_always`/`reject_always` option: each call is decided on its own.

## `process` contract — `boss.harness.process/v1`

* **argv**: a template list. Placeholders `{prompt}`, `{prompt_file}`, `{workspace}`, `{model}`,
  `{mode}`, `{result_file}` are substituted; any other `{name}` is refused at load. No shell.
* **prompt_via**: `stdin` (default) | `argv` (`{prompt}` must appear) | `file` (read
  `$BOSS_PROMPT_FILE`; the file is always written).
* **cwd**: the workspace.
* **environment**: BOSS's deny-by-default agent environment (provider keys only as model-proxy
  run tokens), the manifest's `envPassthrough`, and:

  | Variable | Value |
  |----------|-------|
  | `BOSS_HARNESS_CONTRACT` | `boss.harness.process/v1` |
  | `BOSS_WORKSPACE` | the tree to edit |
  | `BOSS_MODE` | `write` or `read_only` (a read-only turn must not modify the tree) |
  | `BOSS_MODEL` | the model BOSS routed, or empty for the agent's default |
  | `BOSS_PROMPT_FILE` | the prompt, as a file |
  | `BOSS_RESULT_FILE` | where the agent MAY write its result JSON |
  | `BOSS_SESSION_ID` | set on a follow-up turn of an earlier session |
  | `BOSS_MCP_CONFIG` | the kit manifest (see below), when the agent holds servers |
  | `BOSS_SKILLS_DIR` | a directory of `<skill>/SKILL.md`, when the agent holds skills |

* **diff contract**: edit files in place in `BOSS_WORKSPACE`; never branch, commit or push.
* **result contract** (optional; without it stdout is the summary):

  ```json
  {"summary": "what changed and why", "status": "done|error|turn_cap|awaiting_approval",
   "usage": {"model": "...", "prompt_tokens": 0, "completion_tokens": 0, "cost_usd_micros": 0},
   "tool_calls": [{"name": "write_file", "input": {"path": "a.py"}, "ok": true, "output": "..."}],
   "session_id": "...", "error": "..."}
  ```

  Tool names go through the engine contract's aliases. A cost of 0 is recorded as *unreported*.
* **exit code**: 0 = the agent ran; non-zero fails the stage with the agent's own words.

## `http` contract — `boss.harness.http/v1`

| Call | Body / answer |
|------|---------------|
| `POST {base}/v1/runs` | `{contract, prompt, mode, model, session_id?, workspace: {kind: "path", path, access: "read_write"\|"read_only"}, kit: {mcpServers, skills_dir}}` → `201/202 {run_id, poll_after_ms?}` |
| `GET {base}/v1/runs/{run_id}` | `{status: queued\|running\|done\|error\|turn_cap\|awaiting_approval, summary?, usage?, tool_calls?, session_id?, error?}` (final keys as the process result) |
| `DELETE {base}/v1/runs/{run_id}` | BOSS cancels a run past the stage's deadline |

Auth: `Authorization: Bearer <value of the manifest's tokenEnv>`. Workspace kind `path` means the
service shares BOSS's filesystem (a sidecar on the same host or volume); it edits the tree in
place exactly as a process would. The conformance fake is `_FakeHttpAgent` in
`engine/tests/test_harness_breadth_853_854.py`.

## Installable adapters — `boss-harness.yaml` (`harness_plugins.py`)

```yaml
apiVersion: boss.harness/v1
kind: Harness
metadata: {id: acme.inhouse, name: acme_inhouse, version: 1.0.0, description: "..."}
spec:
  kind: process            # process | http | acp
  capabilities: [mcp, skills]
  requires: [ACME_TOKEN]
  process: {argv: [/opt/acme/agent, "--mode", "{mode}"], promptVia: stdin}
  testEnvironment: {argv: [/opt/acme/agent, --version], expect: "acme"}
```

* Found under `BOSS_HARNESS_PLUGIN_PATHS` or through the `boss.harnesses` entry-point group (the
  manifest is read from the package's installed files; **the package is never imported**).
* Trust gate (the platform plugins' rules): checksum = `platform.registry.tree_checksum`; pinned
  in `BOSS_HARNESS_LOCK` → `verified` source, refused on mismatch; unpinned → `community`,
  refused until `BOSS_HARNESS_PLUGINS_APPROVED=<id>@<checksum>` names that exact build, refused
  again when its files change. `official.` is reserved; a built-in harness name cannot be taken.
* Built with BOSS's own drivers; registered `verified=False`, `source: plugin:<id>`.
* `testEnvironment` runs only on demand (`harness_plugins.probe`), never on a task's hot path.

## The agent's kit on every harness (854)

`code_task_mcp.prepare` writes the agent's `mcp.json` (as before) and now also a skills bundle
(`<skill_key>/SKILL.md` per held, enabled skill). `harness_delivery.deliver` hands both to the
harness per run, as its spec declares, and undoes every file when the session ends.

### Delivery matrix

| Engine | MCP delivery | Skills | Verified |
|--------|--------------|--------|----------|
| claude_code | claude_flag | `{workspace}/.claude/skills/boss-<key>` | yes |
| codex | codex_home | `$CODEX_HOME/skills` (per-run home) | no |
| cursor | cursor_workspace | none (recorded) | no |
| grok | none | none (recorded) | no |
| aider | none | none (recorded) | no |
| gemini_cli | acp | none (recorded) | no |
| opencode | acp | `{workspace}/.opencode/skill/boss-<key>` | no |
| process | env_manifest | `$BOSS_SKILLS_DIR` | plugin |
| http | env_manifest | `kit.skills_dir` in the submit body | plugin |

* **claude_flag**: `--strict-mcp-config --mcp-config <file>` + `--disallowedTools` (unchanged).
* **codex_home**: a per-run `CODEX_HOME`; `config.toml` is the operator's config with its
  `mcp_servers` replaced by the agent's (`url`, `http_headers`, `disabled_tools`,
  `tool_timeout_sec` under the gateway). Everything else in the operator's home (login,
  sessions) is linked in; a login Codex rotates during the run is copied back.
* **cursor_workspace**: `<workspace>/.cursor/mcp.json`, the repository's own file restored after.
* **acp**: `session/new.mcpServers` (http variant, headers as `{name, value}`), only when the
  agent's `initialize` says `mcpCapabilities.http`; otherwise each server is recorded withheld
  (`no_http_capability`).
* **env_manifest**: `$BOSS_MCP_CONFIG` → `{"contract": "boss.harness.kit/v1", "mcpServers": {...},
  "disallowed": {server: [tool]}, "gateway": bool}`; the http kind sends `mcpServers` inline.
* **none**: nothing is written and the run logs which servers did not reach the engine.

An engine that cannot refuse a single tool (cursor, ACP, process, http) is not handed a server
carrying an Off/ungranted tool unless the MCP gateway is on (the gateway refuses those calls);
the server is recorded `cannot_disallow`. The conformance test
(`test_conformance_every_harness_sees_the_agents_servers`) runs a scripted fake per engine that
reads its config the way that engine does.
