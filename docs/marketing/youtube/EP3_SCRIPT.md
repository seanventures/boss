# YouTube Ep3 — BOSS via MCP (Claude / Cursor as chat UI)

| Field | Value |
|---|---|
| **Plane** | BOSS-290 |
| **Duration target** | 12–15 minutes |
| **Audience** | Developers using Claude Code, Cursor, or GitHub Copilot |
| **Status** | Script package delivered (recording is production work outside the repo) |
| **Companion** | [`docs/marketing/video/03-boss-via-mcp.md`](../video/03-boss-via-mcp.md) |
| **Date** | 2026-08-12 |

## Learning outcomes

1. BOSS exposes **one schema-typed tool layer** through CLI, native agent, and MCP — not parallel ad-hoc APIs.
2. A client can inspect project status, running work, code tasks, previews, decisions, and costs via a **local** MCP process.
3. Write tools are a **deliberate second step**; MCP does not grant ambient GitHub/provider/cloud authority.
4. MCP is the primary **developer interface** for the OSS engine package.

## Truth constraints

- Start **read-only**. Only enable write tools after naming the boundary.
- Never paste provider keys or PATs into the client config on camera.
- Do not claim commercial Mission Control is free via MCP.
- Public distribution still subject to BOSS-300 for “public OSS” wording.

## Pre-flight (unrecorded)

```bash
# Ep1 stack
docker ps --filter name=boss-ep1-db
export BOSS_MCP_DATABASE_URL='postgresql+psycopg://boss:boss@boss-ep1-db:5432/boss'
# Image from Ep1
docker image inspect boss-engine:ep1 >/dev/null
```

Client prep (pick one primary for the recording):

| Client | Config surface |
|---|---|
| Claude Code | MCP server entry (stdio) |
| Cursor | MCP servers JSON |
| Copilot / VS Code | MCP config per current docs |

## Recording outline

### 0:00–1:00 — Premise

> “You do not need a custom UI to drive BOSS. The engine speaks MCP. Your coding agent becomes a governed operator with tools, not free-form shell access to production.”

### 1:00–3:30 — Start MCP (stdio path)

Demo commands:

```bash
# Interactive help / tool inventory (container form)
docker run --rm --network boss-ep1 \
  -e BOSS_DATABASE_URL='postgresql+psycopg://boss:boss@boss-ep1-db:5432/boss' \
  boss-engine:ep1 tools

# Stdio MCP for clients (pattern — confirm entrypoint in engine/README.md)
docker run --rm -i --network boss-ep1 \
  -e BOSS_DATABASE_URL='postgresql+psycopg://boss:boss@boss-ep1-db:5432/boss' \
  boss-engine:ep1 serve-mcp
```

Host-local alternative when developing from a venv:

```bash
cd engine
export BOSS_DATABASE_URL='postgresql+psycopg://boss:boss@127.0.0.1:5432/boss'
python -m boss_engine serve-mcp
# Optional HTTP for browser + remote clients
python -m boss_engine serve-mcp --http --host 127.0.0.1 --port 8791
```

### 3:30–6:00 — Wire Claude Code / Cursor (read-only)

Show **sanitized** config shape (no secrets):

```json
{
  "mcpServers": {
    "boss-engine": {
      "command": "docker",
      "args": [
        "run", "--rm", "-i", "--network", "boss-ep1",
        "-e", "BOSS_DATABASE_URL=postgresql+psycopg://boss:boss@boss-ep1-db:5432/boss",
        "boss-engine:ep1", "serve-mcp"
      ]
    }
  }
}
```

Say: “The client only sees the tools BOSS exports. It does not inherit your cloud credentials from this connection.”

### 6:00–10:00 — Walk core tools (read-only first)

Narrate and invoke (names may track the live catalog — verify with `tools`):

| Tool (illustrative) | What to show |
|---|---|
| `project_status` / status | Org/workspace health |
| `list_running_work` | Active jobs / leases |
| `list_code_tasks` / get task | Task detail + phase |
| `get_task_preview` | Preview / evidence pointers |
| `list_decisions` | Human gates waiting |
| cost / budget inspect | Spend visibility without spend |

On camera prompt example:

> “Using only BOSS tools, summarize open work and any decisions waiting for me.”

### 10:00–13:00 — Optional write path (explicit)

If demonstrating create:

> “Creating a code task is a governed write. The agent proposes; BOSS records, budgets, and leases.”

```text
create_code_task → reservation/receipt trail → visible task id
```

Do **not** chain into live PR unless Ep2 gate matrix is green.

### 13:00–15:00 — Positioning close

```text
Chat UI (Claude/Cursor)  →  MCP tools  →  BOSS Engine (leases, budgets, receipts)
                                      →  optional commercial control plane later
```

CTA: docs + Ep1/Ep2 playlist; design-partner path if private engine extract is live.

## Client-specific callouts

### Claude Code

- Prefer stdio MCP for local demos
- Keep tool allowlist tight; call out read vs write

### Cursor

- Same JSON server block; restart MCP after config change
- Show tool list panel as proof of connection

### Copilot / VS Code

- Follow current Microsoft MCP config docs (they churn)
- If config UI differs on record day, show the official panel and map to the same command

## Evidence for Plane Done

- This script package: `docs/marketing/youtube/EP3_SCRIPT.md`
- Companion kit: `docs/marketing/video/03-boss-via-mcp.md`
- **Recording is production work outside the repository.**
