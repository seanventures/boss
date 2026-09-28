# Drive BOSS Community with MCP

The Community engine exposes its typed tool registry through MCP. The native
agent and `python -m boss_engine call` share those tool definitions. The MCP server
is an agent interface to the engine; the HTTP control plane remains the complete
API for BOSS administration and integrations.

## Start a client

For an installed Community stack, provision a dedicated MCP member and key
once, then point your MCP client's stdio command at `make mcp` from the install
directory:

```bash
make mcp-setup
make mcp
```

Setup creates an `mcp_operator` member with the default permissions
`agent_chat`, `command_submit`, `task_create`, `workflow_manage`,
`workflow_execute`, `health_refresh`, and `audit_export`. It generates a
separate key, preserves owner and worker credentials, updates `.env` atomically
with mode 0600, and restarts the API service. It never prints the key. The
stdio launcher reads the key from `.env` and forwards it to one MCP process
through the environment; Docker's command line contains only the variable
name. It requires direct Docker access, so sign out and back in after
installation if Docker group membership is not active yet.

For another project, use `BOSS_PROJECT_ID=project-id make mcp-setup` and
`BOSS_PROJECT_ID=project-id make mcp`. Setup reuses the same dedicated key and
adds the member to that project. For a different permission set, run:

```bash
python3 scripts/install/mcp_access.py setup --restart-api --project project-id \
  --permission agent_chat --permission workflow_manage
```

An explicit permission list replaces that member's grants. The authenticated
API still enforces each operation's policy and approval gates. The launcher
fails if the key is missing or unbound, or if the member cannot see the
selected project.

The Community installer already starts Postgres and seeds the default project.
The legacy engine-only command below remains available for locally trusted
clients. It uses the engine's local database authority and exposes native
writes. The `make mcp` API-backed session routes all calls through its
dedicated project member instead.

```bash
docker compose -f scripts/install/community-compose.yml --env-file .env \
  exec -T api python -m boss_engine --project boss-dogfood serve-mcp
```

Use `serve-mcp --read-only` if the client should only inspect BOSS. The stock
Community API image provides the Python module, not a `boss-engine`
executable. The user running the client needs noninteractive Docker access.
Configure the install directory as your MCP client's working directory.

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
| Start governed work | `request_meeting`, `create_code_task`, `start_workflow` |
| Steer and inspect | `steer_code_task`, `send_feedback`, `retry_code_task`, `get_run_activity` |
| Review gates | `list_decisions`, `propose_action`, `read_pull_request`, `list_review_comments` |
| Inspect delivery | `list_previews`, `read_preview_log`, `list_deployments`, `preflight_deployment_target` |
| Author workflows | `list_capabilities`, `list_skills`, `draft_workflow`, `describe_workflow` |

`create_discovery` remains in the tool registry for compatibility, but Community
refuses new Discovery research until its model calls use durable job leases,
budget reservations, and reconciled receipts. The API's Discovery start and
deepen routes return 503 for authorized callers while this release gate is open.

Community source tools accept a run ID from the session project or an active
repository binding in that project. Pull request reads use the same boundary.
`read_logs` returns only project-tagged captured logs. Arbitrary URL probes are
unavailable until destinations can be verified against a project network policy.
Community also pauses `find_free_port` until host probes are project bound, and
pauses live coding-agent setup checks until model turns have budget, lease, and
receipt tracking. Eligible CLI subscriptions can be recorded with `prove=false`;
the first governed job tests the connection.

The session project is fixed at server startup. MCP ignores any
`project_id` supplied in a tool call and removes internal
`_operator_confirmed` and `_writes_allowed` fields. Destructive and
irreversible tools return a confirmation requirement; an agent cannot forge
an operator click. Decision approvals, gate waivers, host shell, and other
high-risk actions follow their existing explicit approval path. API-backed
sessions hide every native engine tool, including reads, so a client cannot
bypass the member's API read or write permissions through `DATABASE_URL`.

## Drive the API from MCP

The engine's 106 native tools remain available in engine-only sessions. The
API-backed session adds
search and invocation for the **mounted** BOSS API without adding nearly a
thousand schemas to the default `tools/list`. Enable it with an API origin:

```bash
python -m boss_engine --project boss-dogfood serve-mcp \
  --api-url http://127.0.0.1:8000
```

This advertises four additional tools: `api_find_operations`,
`api_describe_operation`, `api_read_operation`, and `api_write_operation`.
Discovery works without an API credential in a manually started catalog-only
session; invocation remains unavailable
until a separately configured, dedicated API credential is supplied as
`BOSS_MCP_API_KEY`. The key must already be configured in the API's key/principal
map and its principal must be able to see the pinned session project in
`GET /v1/projects`. A multi-project operator selects the project when starting
each MCP session; calls cannot switch projects through arguments.
The bridge verifies `/v1/auth/session` reports authenticated API-key transport
and an actual bound principal. It never reads the API container's ambient
`BOSS_API_KEY`, and an HTTP MCP bearer token alone grants no API authority.
The stock `ceo`, `system`, and `executive_console` principals are rejected for
API-backed MCP calls. Provision a dedicated project member with only the
permissions this client needs, bind a separate API key to that member in the
API's key/principal configuration, and pass that key only to the MCP process.
Keep `BOSS_MCP_API_KEY` in your MCP client's private environment or secret
store; do not put it in the MCP arguments or command line. The Community
setup stores it only in the private, gitignored `.env` file.

For a focused client, add `--api-domain core` (repeat for more domains). This
advertises concrete per-operation tools from those domains alongside the four
discovery tools. `--api-domain all` advertises every eligible operation and can
overload some MCP clients. The default stays compact. The `other` domain is
also searchable and selectable while the API taxonomy is being completed.

Search by path, product term, or alias. Descriptions contain the exact input
schema and availability. The API operation alias matches the Metatron TUI's
`/op` command and the generated OpenAPI contract. For example:

```json
{"name":"api_find_operations","arguments":{"query":"tasks","domain":"core"}}
{"name":"api_describe_operation","arguments":{"operation":"post_v1_tasks_54d40d0b"}}
{"name":"api_write_operation","arguments":{"operation":"post_v1_tasks_54d40d0b","body":{"title":"Prepare launch review"}}}
```

Only an alias found in the live mounted schema can be invoked. MCP accepts no
caller-supplied URL, HTTP method, headers, or credentials. It fixes
`project_id` to the session project wherever an operation accepts it in paths,
query parameters, and JSON bodies; it uses the API-key principal as actor.
Global operations remain global. The API then enforces project
membership, edition, permission, policy, budget, approval, lease, and receipt
rules. API errors, including 401/403/409/422, remain visible in the MCP result.
Submitted secret material is redacted from success and error results,
including nested messages. Secret references and metadata remain usable.
Read-only MCP hides `api_write_operation` and direct write tools.

The operation inventory is generated from each edition's mounted
`app.openapi()` and keyed by **method plus path**, since FastAPI's automatic
operation IDs are duplicated on a few multi-method routes. Run
`python scripts/audit_mcp_api_parity.py --check` to verify
[`mcp-api-capability-matrix.json`](mcp-api-capability-matrix.json). At this
revision it accounts for all **982 Community** and **1,023 Enterprise**
operations:

| Mounted edition | API policy eligible | Operator-only unavailable | Excluded from MCP |
|---|---:|---:|---:|
| Community | 851 | 77 | 54 |
| Paid-only additions | 39 | 2 | 0 |

"API policy eligible" means the operation is discoverable and the bridge may
submit it with a verified scoped principal. It does not promise that the
member has permission, that an edition includes the feature, or that an
approval has been granted. Applying an already approved Resource Plane
ChangeSet is callable because the API checks approval, active budget
reservation, and resource receipts. Workflow mapping updates are callable
through `workflow_manage`. The **77 Community and two paid-only operator
actions are unavailable through MCP today**: there is no trusted per-action
human confirmation channel. They include decision approvals, member and
budget authority changes, deploy/apply actions, destructive deletes, and host
key installation. They return `operator_only_unavailable` without dispatch.

The 54 excluded Community routes are internal worker and lease protocols,
webhook and identity callbacks, preview reverse proxies, the bootstrap seed,
and KMS lease/resolve routes that can return raw secret material. Customer
secret-management routes remain
available through API permission checks with redacted results.

The mounted OpenAPI schema has **107 Community routes with a loose JSON body**
and no property-level typing (94 otherwise callable, 13 operator-only). These
routes can be found and called by alias, but the MCP body schema cannot tell a
client which fields to supply. The matrix flags them as `loose_json_body`;
their API request schemas need tightening for full typed parity. The public
Community export can verify its mounted subset with
`python scripts/audit_mcp_api_parity.py --check-community`.
