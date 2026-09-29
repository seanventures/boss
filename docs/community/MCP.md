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

To choose a different project **before the first setup**, use
`BOSS_PROJECT_ID=project-id make mcp-setup` and then
`BOSS_PROJECT_ID=project-id make mcp`. One installed `.env` MCP key and member
belong to one project. Setup refuses to add that member to a second project,
and the launcher refuses a different project ID. For a different permission
set on the selected project, run:

```bash
python3 scripts/install/mcp_access.py setup --restart-api --project project-id \
  --permission agent_chat --permission workflow_manage
```

An explicit permission list replaces that member's grants. The authenticated
API still enforces each operation's policy and approval gates. The launcher
fails if the key is missing or unbound, or if the member can see any project
other than the selected one. Use a separate principal, key, and MCP process
for another project; do not add one MCP principal to several projects.

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

Write mode advertises five additional tools: `api_find_operations`,
`api_describe_operation`, `api_read_operation`, `api_write_operation`, and
`api_request_confirmation`. Read-only mode omits both write tools.
Discovery works without an API credential in a manually started catalog-only
session; invocation remains unavailable
until a separately configured, dedicated API credential is supplied as
`BOSS_MCP_API_KEY`. The key must already be configured in the API's key/principal
map and `GET /v1/projects` must return **exactly** the pinned session project
for that principal when the session starts and before each call. A multi-project
operator needs one dedicated principal and key per project. Calls cannot switch
projects through arguments, including
operations that accept only a resource ID and no `project_id` field.
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
revision it accounts for all **986 Community** and **1,027 Enterprise**
operations:

| Mounted edition | API policy eligible | Human confirmation required | Held for route safety work | Excluded from MCP |
|---|---:|---:|---:|---:|
| Community | 853 | 29 | 37 | 67 |
| Paid-only additions | 39 | 0 | 2 | 0 |

"API policy eligible" means the operation is discoverable and the bridge may
submit it with a verified scoped principal. It does not promise that the
member has permission, that an edition includes the feature, or that an
approval has been granted. Applying an already approved Resource Plane
ChangeSet is callable because the API checks approval, active budget
reservation, and resource receipts. Workflow mapping updates are callable
through `workflow_manage`. A task can be soft-archived with `task_create` and
an audit event; a paused workflow migration can be rolled back only before
its new steps execute, with `workflow_manage` and an audit event.

Project meeting-role edits require `workflow_manage`; planned team-structure
and agent-role edits require `role_review`. The API records the authenticated
member as the actor even if a request body names another actor. Changing or
applying the gate-autonomy policy requires a human CEO member with
`decision_resolve`; dedicated MCP principals cannot be CEOs, so those two
operations are held from MCP. The standard MCP member does not receive these
management permissions by default.

For the 29 confirmation-gated actions, MCP calls `api_request_confirmation`
with the exact intended operation, path, query, and body. BOSS stores a digest
of the complete request, its method and target, and a limited review summary;
it never stores the raw body. A **separate human CEO member** reviews the
complete request and submits that exact `reviewed_request` plus its digest
through the terminal or API. The server recomputes the digest from the human's
submitted method, path, query, and body before approval; it never persists the
review submission. The human credential must not be available to the MCP
client. Approval expires in five minutes and is consumed atomically by one
matching request. Existing API permissions, budget, ChangeSet, receipt, and
route policies still apply. Request, approval, consumption, and HTTP result
are distinct audit events.

Promotion of a winning improvement trial is among these actions. It creates
an approval-gated proposal and decision; it does not adopt a new baseline or
roll out code.

The human can find the pending request with `/ops mcp-confirmations` and
`/op` on the GET confirmation operation, then approve it with `/op` on the
POST approve operation, passing `project_id`, `reviewed_digest`, and the
complete `reviewed_request` object. Direct API
equivalents are `GET /v1/mcp-confirmations?project_id=...` and
`POST /v1/mcp-confirmations/{id}/approve`. The MCP client then calls
`api_write_operation` with **identical** path, query, and body plus the returned
`confirmation_id`. The stored summary omits free-form body/query values; the
human must inspect the full request they submit for approval. For example,
with the human's own TUI key, use the operation IDs from `/describe` and send
the approval as one `/op` JSON argument:

```text
/op get_v1_mcp_confirmations_d1bc4240 {"query":{"project_id":"boss-dogfood"}}
/op post_v1_mcp_confirmations_confirmation_id_approve_3742b3e9 {"path":{"confirmation_id":"mcpconf_<id>"},"body":{"project_id":"boss-dogfood","reviewed_digest":"<digest>","reviewed_request":{"project_id":"boss-dogfood","operation":"post_v1_members_7de05164","method":"POST","path":"/v1/members","query":{},"body":{"project_id":"boss-dogfood","actor":"mcp_operator","member_id":"new_agent","display_name":"New agent","member_type":"agent","role_keys":["operator"],"permissions":["task_create"]}}}}
```

The human must replace the example values with the exact request they
reviewed, including every body and query field. A mismatch fails before
approval. The dedicated MCP member also needs `member_manage` for this
example; the default `make mcp-setup` grants do not include it. The confirmation endpoints are excluded
from MCP tool dispatch: the client cannot approve itself. The reviewer must
be an active `member_type=human` CEO with `decision_resolve` permission.

The 36 held Community and two paid-only actions return
`operator_only_unavailable`. They include direct host/provider effects without
a governed ChangeSet/receipt path, host key material, global or ID-only routes
without a sound project boundary, and routes lacking API-side actor checks.
Nine decision and approval routes remain held because their domain records
would attribute approval to the MCP agent; GitHub merge authorization also
rejects authorization by the actor who requested it. These routes need
route-specific attested-human attribution and policy checks before MCP use.
See the [operation matrix](mcp-api-capability-matrix.json) for each mounted
operation's current disposition.

The 67 excluded Community routes are internal worker and lease protocols,
webhook and identity callbacks, preview reverse proxies, the bootstrap seed,
the four confirmation protocol endpoints, and KMS lease/resolve routes that
can return raw secret material. Customer
secret-management routes remain
available through API permission checks with redacted results.

All **532 Community operations with a JSON request body** now expose named
top-level fields in the mounted OpenAPI schema; the other 454 operations have
no JSON body. No Community JSON body remains loose at the top level. Of the
532, **448** use Pydantic field handling and **84** use pass-through fields to
preserve existing handler coercion and status codes. The latter fields help MCP
clients construct requests, but their types are not enforced by Pydantic. Some
nested payloads remain intentionally flexible. The matrix's
`request_body_type_enforcement` field makes this distinction explicit; its
legacy `runtime_validated_input_schema` field describes the mounted field
schema and does not guarantee field-type enforcement. The API handler and its
policy checks remain authoritative. The public Community export can verify its
mounted subset with
`python scripts/audit_mcp_api_parity.py --check-community`.
