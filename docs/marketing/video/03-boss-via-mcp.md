---
title: "Video 03 — Use BOSS through MCP"
audience: "Developers using Claude Code, Cursor, or GitHub Copilot"
duration_target: "12–15 minutes"
status: "recording-ready draft"
verified: "2026-08-08"
---

# Video 03 — Use BOSS through MCP

This is the recording script and setup checklist for connecting an MCP-capable
coding client to the self-hosted BOSS Engine. The demo begins with a
**read-only** server: it proves that the client can inspect governed work
without acquiring ambient repository, provider, or cloud authority.

## What the viewer should learn

1. BOSS exposes one schema-typed tool layer through CLI, its native agent, and
   MCP—not parallel APIs.
2. A client can inspect project status, running work, code tasks, previews,
   decisions, and costs through a local stdio process.
3. Write tools are a deliberate second step. A client can only propose or
   invoke governed work through the BOSS tool boundary; it does not inherit
   arbitrary credentials from the MCP connection.

## Pre-flight

Complete the Docker/bootstrap path in Video 01 first. In a local terminal,
leave the PostgreSQL container running and set a non-production connection
string:

```bash
export BOSS_MCP_DATABASE_URL='postgresql+psycopg://boss:boss@boss-ep1-db:5432/boss'
docker ps --filter name=boss-ep1-db
```

- [ ] The image `boss-engine:ep1` exists locally.
- [ ] `boss-ep1-db` is a disposable local PostgreSQL container on `boss-ep1`.
- [ ] The workspace was bootstrapped with `boss-engine:ep1 bootstrap`.
- [ ] No provider credential, GitHub token, or customer repository is part of
  the demo.
- [ ] Use a server name such as `boss-engine-demo`, not a reserved client name.

## Recording story

### 0:00–1:15 — Establish the boundary

Say:

> “MCP is the developer interface to BOSS, not a bypass around BOSS. The client
> discovers BOSS tools, while BOSS still owns project scope, policy, decisions,
> budget, receipts, and durable execution.”

Show the engine tool catalog once:

```bash
docker run --rm --network boss-ep1 \
  -e DATABASE_URL="$BOSS_MCP_DATABASE_URL" \
  boss-engine:ep1 tools
```

Explain that the first client configuration starts
`serve-mcp --read-only`; only BOSS read tools will be advertised.

### 1:15–4:30 — Claude Code (recommended live client)

Claude Code supports local stdio MCP servers. Register the ephemeral demo
server without committing configuration or revealing credentials:

```bash
claude mcp add --scope local --transport stdio \
  --env DATABASE_URL="$BOSS_MCP_DATABASE_URL" \
  boss-engine-demo -- \
  docker run -i --rm --network boss-ep1 -e DATABASE_URL \
  boss-engine:ep1 serve-mcp --read-only

claude mcp list
```

Start Claude Code, open `/mcp`, and confirm that `boss-engine-demo` is
connected and exposes read tools. Ask these prompts one at a time:

```text
Use BOSS project_status. What is running in boss-dogfood?
Use BOSS list_running_work. Is any work waiting on a lease?
Use BOSS list_code_tasks and summarize the most recent tasks.
Use BOSS list_decisions. What requires a human decision?
```

If a project-local `.mcp.json` is used instead, do not record an automatic
approval. Claude Code requires workspace trust and interactive approval for a
project server. Use its connected status, not the fact that configuration was
written, as the proof that it is live.

### 4:30–6:30 — Cursor configuration

Cursor supports project configuration at `.cursor/mcp.json` and global
configuration at `~/.cursor/mcp.json`. For a screen recording, use a temporary
uncommitted project configuration; do not put a production connection string
in a repository file.

```json
{
  "mcpServers": {
    "boss-engine-demo": {
      "command": "docker",
      "args": [
        "run", "-i", "--rm", "--network", "boss-ep1",
        "-e", "DATABASE_URL",
        "boss-engine:ep1", "serve-mcp", "--read-only"
      ],
      "env": {
        "DATABASE_URL": "${BOSS_MCP_DATABASE_URL}"
      }
    }
  }
}
```

In Cursor Agent mode, verify the tool list before asking the same four
read-only prompts. If the current Cursor release does not expand environment
variables in this screen, set the value locally for the recording and delete
the temporary file afterward; never commit the value.

### 6:30–8:15 — GitHub Copilot CLI

Copilot CLI can add a local stdio server with `copilot mcp add`. The following
creates a user-local configuration; it should not be run in a shared
production profile without reviewing the command and environment:

```bash
copilot mcp add boss-engine-demo \
  --env DATABASE_URL="$BOSS_MCP_DATABASE_URL" -- \
  docker run -i --rm --network boss-ep1 -e DATABASE_URL \
  boss-engine:ep1 serve-mcp --read-only

copilot mcp get boss-engine-demo --json
```

Start Copilot in agent mode and repeat `project_status` and `list_code_tasks`.
Use `copilot mcp remove boss-engine-demo` after the recording if it was added
to a personal profile.

### 8:15–10:30 — Demonstrate a deliberate write boundary

Do **not** switch the demo server to write-enabled just to make it feel more
autonomous. First show that a write tool is absent from the read-only catalog.
Then explain the upgrade path:

```bash
# Only in a disposable local environment after an explicit decision to demo writes:
docker run -i --rm --network boss-ep1 \
  -e DATABASE_URL="$BOSS_MCP_DATABASE_URL" \
  boss-engine:ep1 serve-mcp
```

Say:

> “A write-capable MCP server still calls BOSS—not the provider directly. BOSS
> creates durable task and policy records, and work that needs authorization,
> budget, receipt, or approval remains subject to those controls.”

If this segment is shown live, use only the disposable database and stop before
creating an external PR, provider action, or deployment.

### 10:30–12:00 — Close with the operating model

Show the browser-based HTTP endpoint only as a read-only inspection surface:

```bash
docker run -d --name boss-ep3-mcp --network boss-ep1 -p 8791:8791 \
  -e DATABASE_URL="$BOSS_MCP_DATABASE_URL" \
  boss-engine:ep1 serve-mcp --http --host 0.0.0.0 --read-only

curl -fsS http://127.0.0.1:8791/health
```

Say:

> “The same governed tool catalog can be explored from an MCP client or this
> read-only Mission Control view. The transport changes; the operational record
> and policy boundary do not.”

## Client-specific proof checklist

- [ ] Claude Code: `claude mcp list` reports a connected server or `/mcp`
  visibly shows the tool count.
- [ ] Cursor: Agent mode displays the configured BOSS tools before the prompt
  is sent.
- [ ] Copilot CLI: `copilot mcp get boss-engine-demo --json` reports the
  configured server; agent mode can discover its read tools.
- [ ] At least one client returns a BOSS-grounded answer to `project_status`.
- [ ] No write tool appears in the read-only catalog.
- [ ] No credential, customer data, external PR, deploy, or provider write is
  shown.

## Cleanup

```bash
claude mcp remove boss-engine-demo
copilot mcp remove boss-engine-demo
docker rm -f boss-ep3-mcp
```

Remove the temporary `.cursor/mcp.json` before committing any project changes.

## Official client references

- [Claude Code MCP configuration](https://code.claude.com/docs/en/mcp)
- [Cursor MCP configuration](https://docs.cursor.com/context/model-context-protocol)
- [GitHub Copilot CLI MCP configuration](https://docs.github.com/en/enterprise-cloud@latest/copilot/how-tos/copilot-cli/customize-copilot/add-mcp-servers)
