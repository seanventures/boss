# Drive BOSS Community with MCP

The Community engine exposes its typed tool registry through MCP. The same
registry powers the native engine agent and `python -m boss_engine call`. The MCP server
is an agent interface to the engine; the HTTP control plane remains the complete
API for BOSS administration and integrations.

## Start a client

The Community installer already starts Postgres and seeds the default project.
From the install directory, configure an MCP client to launch this command
over stdio. The API container already has `DATABASE_URL`; `--project` fixes
the session project. Stdio supports governed write tools by default.

```bash
docker compose -f scripts/install/community-compose.yml --env-file .env \
  exec -T api python -m boss_engine --project boss-dogfood serve-mcp
```

Use `serve-mcp --read-only` if the client should only inspect BOSS. The stock
Community API image provides the Python module, not a `boss-engine`
executable. The user running the client needs noninteractive Docker access.
After a fresh install, sign out and back in before configuring the MCP client
so the new Docker group membership takes effect.

For a separately configured Python process with `DATABASE_URL`, an HTTP MCP
server starts read-only. The stock Community Compose file does not publish an
MCP HTTP port, so this is a separate operator-managed listener. A non-loopback
bind also requires `BOSS_MCP_HTTP_TOKEN`, because read tools can expose project
data:

```bash
python -m boss_engine --project boss-dogfood serve-mcp --http --host 127.0.0.1
```

For a local HTTP agent that must create or steer work, set a private token and
opt into writes. BOSS refuses to start write-capable HTTP on a non-loopback
address or without the token. Use an SSH tunnel to reach a VM from another
machine.

```bash
export BOSS_MCP_HTTP_TOKEN="$(openssl rand -hex 32)"
python -m boss_engine --project boss-dogfood serve-mcp --http --allow-writes --host 127.0.0.1
```

HTTP clients send `Authorization: Bearer <BOSS_MCP_HTTP_TOKEN>` to `/mcp` and
`/health`. The Mission Control snapshot page at `/` prompts for the token if
the HTTP server requires it. Keep the token out of logs, command-line arguments,
and repository files. Use TLS or a tunnel for any connection that leaves the
host so the bearer token is not sent over cleartext.

## What agents can do

The engine registry currently has 106 tools: 61 read and 45 write. Examples:

| Activity | MCP tools |
|---|---|
| Discover work | `project_status`, `list_running_work`, `list_code_tasks`, `get_code_task` |
| Start governed work | `request_meeting`, `create_discovery`, `create_code_task`, `start_workflow` |
| Steer and inspect | `steer_code_task`, `send_feedback`, `retry_code_task`, `get_run_activity` |
| Review gates | `list_decisions`, `propose_action`, `read_pull_request`, `list_review_comments` |
| Inspect delivery | `list_previews`, `read_preview_log`, `list_deployments`, `preflight_deployment_target` |
| Author workflows | `list_capabilities`, `list_skills`, `draft_workflow`, `describe_workflow` |

The session project is fixed at server startup. MCP ignores any
`project_id` supplied in a tool call and removes internal
`_operator_confirmed` and `_writes_allowed` fields. Destructive and
irreversible tools return a confirmation requirement; an agent cannot forge
an operator click. Decision approvals, gate waivers, host shell, and other
high-risk actions follow their existing explicit approval path.

## Scope and parity

MCP and `python -m boss_engine call` use the engine registry, so their tool names and
argument schemas match. The HTTP control plane has many more routes than the
engine registry, including setup, identity, provider administration, billing,
and edition-specific operations. Do not treat MCP `tools/list` as a complete
replacement for `/openapi.json` or `/v1/meta/api-catalog`. Use the authenticated
HTTP API for functions not represented by an engine tool, and keep its policy,
tenant, budget, decision, and receipt gates in place.

The engine tool registry and control plane route catalog are the sources of
truth for future parity work. Add a focused tool or API endpoint through the
existing service boundary when an agent workflow needs it; a generic
unrestricted HTTP proxy would bypass the reviewable tool contracts.
