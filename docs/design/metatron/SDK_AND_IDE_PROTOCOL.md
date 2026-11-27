# Metatron Versioned JSONL Schema, SDK Protocol, Hooks, and IDE Design Note (`MT-5`)

Status: implemented for schema version `1.0` (`BOSSAPP-1372`).

## 1. Hard Architectural Invariant: Zero Local Code Editing

Metatron **never writes or patches source code locally**. Every registered tool in the binary is scanned by `ToolRegistry::scan_for_forbidden_code_edit_tools()` (enforced by static/runtime test `registered_tools_never_include_local_code_edit_or_patch_capability` in `metatron-tools`).

When an investigation discovers a required code change, the agent calls `handoff_code_task`:
1. Always prompts for explicit operator approval (`RiskClass::Handoff`).
2. Packages the root-cause findings, redacted log excerpts, and SHA-256 evidence references (strictly from the session evidence/scratch directories).
3. Submits `POST /v1/code-tasks` with a deterministic `Idempotency-Key` (`dedupe_key`) to open a governed BOSS code task (build -> test -> review -> PR).
4. Polls `GET /v1/code-tasks/{run_id}` and displays the live status and console link inside Metatron.

## 2. Versioned JSONL Event Schema (`schema_version = "1.0"`)

Running `metatron exec --json` emits one JSON object per line on `stdout`. Every line carries `"schema_version": "1.0"` alongside its discriminated `"type"` field. Export the formal JSON Schema (draft 2020-12) directly from the binary:

```sh
metatron schema > metatron-exec-event-v1.0.schema.json
```

### Event Variants

| `type` | Key fields | Meaning |
|---|---|---|
| `thread.started` | `thread_id` | Server thread bound to this turn |
| `turn.started` | `turn_id`, `thread_id`, `model`, `mode`, `tools[]`, `dropped[]` | Turn started; exact `tools[]` list visible to the model (also persisted as `turn_tools` in local session JSONL) |
| `item.started` | `id`, `kind`, `name`, `summary` | Assistant reply, server tool, or client tool started |
| `item.updated` | `id`, `delta` | Streamed text delta |
| `item.completed` | `id`, `kind`, `status`, `text?`, `exit_code?`, `truncated` | Item completed (`ok`, `error`, `denied`, `cancelled`) |
| `usage` | `input_tokens`, `output_tokens`, `cost{amount,currency,source}` | Metered token/cost accounting from BOSS gateway |
| `turn.completed` | `turn_id`, `stop_reason`, `final_message?` | Terminal success |
| `turn.failed` | `turn_id`, `code`, `message`, `retryable` | Terminal failure |

### TypeScript / Python SDK Consumption Pattern

Both TS and Python wrappers spawn `metatron exec --json --non-interactive` (or connect to a future JSON-RPC stdio server) and iterate newline-delimited JSON objects:

```python
import json, subprocess

proc = subprocess.Popen(
    ["metatron", "exec", "--json", "--non-interactive", "summarize alerts"],
    stdout=subprocess.PIPE,
    text=True,
)
for line in proc.stdout:
    ev = json.loads(line)
    assert ev["schema_version"] == "1.0"
    if ev["type"] == "turn.completed":
        print(ev["final_message"])
```

## 3. Notification Hooks (`[[hooks]]` in `~/.config/metatron/config.toml`)

Configure hooks to notify Slack/PagerDuty/desktop scripts on key lifecycle events:

```toml
[[hooks]]
name = "notify-decision"
event = "on_decision"      # on_decision | on_run_complete | on_handoff
command = ["/usr/local/bin/notify-boss.sh"]
timeout_s = 10
enabled = true
```

- Hook payloads are piped as JSON on `stdin` with `METATRON_HOOK_EVENT` set.
- All secrets and session cookies are scrubbed via `redact_secrets` before stdin write.
- Hook failures are reported visibly (exit code + redacted stderr) and never silently ignored.

## 4. IDE Integration Design Note (Out of Scope for Build in v1)

Future IDE extensions (VS Code, JetBrains, Neovim) should talk to a dedicated `metatron app-server` stdio JSON-RPC 2.0 mode reusing `metatron-protocol` DTOs:
- Client -> Server: `turn/start`, `turn/cancel`, `approval/respond`, `session/list`, `session/resume`, `tools/list`.
- Server -> Client notifications: exact v1.0 `ExecEvent` payloads + `approval/request` (matching `ApprovalPromptRequest`, preserving `hard: true` / `allow_always: false` UI invariants).
- Because Metatron never edits local code, IDE integrations remain strictly investigative and hand off code changes to BOSS via `handoff_code_task`.
