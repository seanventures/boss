# MCP

BOSS Community speaks the Model Context Protocol, so agent clients can discover and drive BOSS work. Two session types are available:

| Session | Authority | Tools | Start with |
|---|---|---|---|
| **API-backed** | A service account pinned to one project, with its own key; every call goes through the HTTP API | API search and invocation tools; native engine tools are hidden | `make mcp-setup`, then `make mcp` |
| **Engine-only** | The engine's local database authority | The engine's 123 native tools (71 read, 52 write) | `python -m boss_engine ... serve-mcp` |

The engine's typed tool registry is shared by MCP, the native agent, and `python -m boss_engine call`. MCP is an agent interface to the engine; the HTTP control plane remains the complete API for BOSS administration and integrations.

## Quick start

For an installed Community stack, give your MCP client its own key once, then point your MCP client's stdio command at `make mcp` from the install directory:

```bash
make mcp-setup
make mcp
```

`make mcp-setup`:

- Creates a service account **MCP (project-id)** with access to that one project only. By default it can do what `agent_chat`, `command_submit`, `task_create`, `workflow_manage`, `workflow_execute`, `health_refresh` and `audit_export` allowed before, plus reading the project.
- Issues its key from the host shell (inside the api container, as the `boss` user), pinned to that project.
- Keeps the key in `.env` as `BOSS_MCP_API_KEY` (atomic write, mode 0600). Never prints it.
- Replaces an older MCP key (one bound to `mcp_operator` in `BOSS_API_KEYS`) and unbinds it; only then does it restart the API, once, to drop the old key.
- Run again with the same project, it changes nothing.

Owners see the account in **Identity › Service accounts**, where they can narrow its role or revoke the key.

The stdio launcher reads the key from `.env` and forwards it to a single MCP process through the environment, so Docker's command line contains only the variable name.

!!! note
    The launcher requires direct Docker access. If Docker group membership is not active yet after installation, sign out and back in.

### Choose a project and permissions

One installed `.env` MCP key belongs to one project. To use a project other than the default, set it **before the first setup**:

```bash
BOSS_PROJECT_ID=project-id make mcp-setup
BOSS_PROJECT_ID=project-id make mcp
```

Setup refuses a second project for the same key, and the launcher refuses a different project ID. For a different permission set on the selected project:

```bash
python3 scripts/install/mcp_access.py setup --restart-api --project project-id \
  --permission agent_chat --permission workflow_manage
```

Each `--permission` is either one of the capability names above or a permission such as `workflow_run:start`. An explicit list issues a new key with exactly that access (the old key stops). The API still enforces each operation's policy and approval gates. The launcher fails if the key is missing, and the API-backed session refuses a key that can see any project other than the selected one.

A person can also point a remote MCP client at a personal key (**My account › API keys**). Personal keys never hold step-up permissions; a service-account key may, for the permissions in its scopes.

!!! warning "One principal per project"
    Use a separate principal, key, and MCP process for each project. Do not add one MCP principal to several projects.

## Engine-only sessions

The Community installer already starts Postgres and seeds the default project. The legacy engine-only command remains available for a client on the same machine. It uses the engine's local database authority and exposes native writes, whereas the `make mcp` API-backed session routes every call through its own project service account.

```bash
docker compose -f scripts/install/community-compose.yml --env-file .env \
  exec -T -u boss api python -m boss_engine --project boss-dogfood serve-mcp
```

- Add `serve-mcp --read-only` if the client should only inspect BOSS.
- The stock Community API image provides the Python module, not a `boss-engine` executable.
- The user running the client needs noninteractive Docker access.
- Set the install directory as your MCP client's working directory.

### HTTP transport

A separately configured Python process with `DATABASE_URL` can serve MCP over HTTP. The stock Community Compose file does not publish an MCP HTTP port, so this is a separate, operator-managed listener. It starts read-only, and on loopback read-only HTTP does not require a token:

```bash
python -m boss_engine --project boss-dogfood serve-mcp --http --host 127.0.0.1
```

For a local HTTP agent that must create or steer work, set a private token and opt into writes. Writes require the token on every bind, including loopback.

```bash
export BOSS_MCP_HTTP_TOKEN="$(openssl rand -hex 32)"
python -m boss_engine --project boss-dogfood serve-mcp --http --allow-writes --host 127.0.0.1
```

HTTP clients send `Authorization: Bearer <BOSS_MCP_HTTP_TOKEN>` to `/mcp` and `/health`. The Mission Control snapshot page at `/` prompts for the token if the HTTP server requires it.

!!! warning "Protect the bearer token"
    Keep the token out of logs, command-line arguments, and repository files. Use TLS or a tunnel for any connection that leaves the host so the token is never sent in cleartext.

### Remote MCP

| Bind | Token | Reads | Writes |
|---|---|---|---|
| Loopback | Optional for reads | Available without a token | Require the token and `--allow-writes` |
| Non-loopback | Required: without `BOSS_MCP_HTTP_TOKEN`, BOSS exits before opening the socket, because read tools can expose project data | Require the token | Hidden by default; require the token and `--allow-writes` |

A request with a missing or wrong bearer is always rejected.

```bash
export BOSS_MCP_HTTP_TOKEN="$(openssl rand -hex 32)"
python -m boss_engine --project boss-dogfood serve-mcp --http --host 0.0.0.0
python -m boss_engine --project boss-dogfood serve-mcp --http --allow-writes --host 0.0.0.0
```

!!! note
    `make mcp-setup` only issues a key through the local install. Keep that helper, and the API it calls, on loopback. The HTTP MCP listener is separate and is not published by the stock Compose file.

## Native engine tools

The engine registry has 123 tools: 71 read and 52 write. Examples:

| Activity | MCP tools |
|---|---|
| Discover work | `project_status`, `list_running_work`, `list_code_tasks`, `get_code_task` |
| Start governed work | `request_meeting`, `create_code_task`, `start_workflow` |
| Steer and inspect | `steer_code_task`, `send_feedback`, `retry_code_task`, `get_run_activity` |
| Review gates | `list_decisions`, `propose_action`, `read_pull_request`, `list_review_comments` |
| Inspect delivery | `list_previews`, `read_preview_log`, `list_deployments`, `preflight_deployment_target` |
| Author workflows | `list_capabilities`, `list_skills`, `draft_workflow`, `describe_workflow`, `precheck_workflow` (will this run work: each step's real infrastructure, before it spends) |
| App templates | `list_templates`, `describe_template`, `apply_template`, `template_status` |
| Security loop | `run_security_scan`, `security_scan_status`, `list_security_findings`, `security_report`, `security_review_status` (start a full review with `start_workflow`, `workflow_key=security_review`) |

### Workflow pre-check and the model pre-check

`precheck_workflow` (and `python -m boss_engine.tools.workflow_precheck`) runs the coded checks: each step gets a verdict of `ready`, `fixable`, `will_fail` or `unknown`, with evidence and a fix. The checks cost no model call.

Add `--agent` to the CLI to also run the model pre-check. An agent reads the coded report and predicts failures that the coded checks do not look for. Before it starts, it receives:

- the workflow's steps and the names of its inputs
- a compact coded report: every check that is not ready, with its evidence; the ready checks by id only
- the engine, model and source each step will use
- recent failures of this workflow and project, clipped
- the installed BOSS version

Anything else it reads with its tools, so the first turn stays small.

It can then call the read tools every agent holds: logs, run turns, diagnoses, configuration, code, git history, the machine's state and `installed_binaries`. It never fixes anything.

- **Model-agnostic.** The agent uses this project's routed model (`model_routing.project_chat_adapter`, purpose `kind=agent`, `step=workflow_precheck`, `tier=fast`). It asks for the Fast tier, never a model name: in Smart routing the tier replaces the classifier's answer (a floor or sensitive work still raises it); in Fixed routing it gets the same engine's Fast-tier model (`haiku` on Claude Code) instead of the chat model. It works with any connected source: an Anthropic key, Codex or OpenRouter. Every model turn is reserved against the budget and receipted (`model.precheck_agent`).
- **Evidence required.** Each prediction lists `{step, title, verdict: will_fail|risk, evidence, fix, who: auto|person}`. A prediction without evidence is dropped. If its citations appear nowhere in what the agent saw or read, it is downgraded to `risk`. If the output is malformed, the agent is asked once more. After that, malformed items are dropped.
- **Separate from the coded checks.** Predictions print as `(model)` lines and are counted on their own (`COUNTS: …; model: will_fail N, risk M`). They also appear in `--explain` and `--json` as `report.model`. They never enter `blocking` and never change the exit code.
- **Capped.** The default cost cap is `$0.50` (`workflow_precheck_agent_max_cents`) and the default turn cap is 8 (`workflow_precheck_agent_max_turns`). No turn starts once the cost cap is reached. You can override both caps for one run with `--agent-max-usd` and `--agent-max-turns`. The report gives the engine, model and source the agent ran on, and what it cost: `model pre-check ran on <engine>/<model> via <source>, $0.0x`.
- **Waits for the per-minute budget.** A turn the burst cap defers is not a failure. The agent waits the budget's retry-after and asks again, for at most `workflow_precheck_agent_max_wait_seconds` (default 180) in all. The line then says `waited Ns for the per-minute budget`.
- **At the start door: off by default.** The model pre-check runs when a workflow starts only if the project setting `workflow_precheck_agent` is `1`, or the workflow spec sets `precheck_agent: true`. Its lines are recorded on the run and are advisory: they never refuse a start.

### Tools paused or scoped in Community

| Tool or area | Community behavior |
|---|---|
| `create_discovery` | Queues a governed `discovery.run` job: a worker leases it, and each research stage is budget-reserved, leased on the Claude Code subscription, and receipted. Without a usable Claude Code subscription the job's first stage is refused and the discovery fails with that reason; nothing is spent. |
| Source tools and pull request reads | Accept a run ID from the session project or an active repository binding in that project. |
| `read_logs` | Returns only project-tagged captured logs. |
| Arbitrary URL probes | Unavailable until destinations can be verified against a project network policy. |
| `find_free_port` | Paused until host probes are project bound. |
| Live coding-agent setup checks | Paused until model turns have budget, lease, and receipt tracking. Eligible CLI subscriptions can be recorded with `prove=false`; the first governed job tests the connection. |

### Session guarantees

- The session project is fixed at server startup. MCP ignores any `project_id` supplied in a tool call and strips the internal `_operator_confirmed` and `_writes_allowed` fields.
- Destructive and irreversible tools return a confirmation requirement; an agent cannot forge an operator click.
- Decision approvals, gate waivers, host shell, and other high-risk actions follow their existing explicit approval path.
- API-backed sessions hide every native engine tool, including reads, so a client cannot bypass the key's API read or write permissions through `DATABASE_URL`.

## Drive the API from MCP

The engine's 106 native tools remain available in engine-only sessions. The API-backed session adds search and invocation for the **mounted** BOSS API without adding nearly a thousand schemas to the default `tools/list`. Enable it with an API origin:

```bash
python -m boss_engine --project boss-dogfood serve-mcp \
  --api-url http://127.0.0.1:8000
```

| Tool | Write mode | Read-only mode |
|---|---|---|
| `api_find_operations` | Yes | Yes |
| `api_describe_operation` | Yes | Yes |
| `api_read_operation` | Yes | Yes |
| `api_write_operation` | Yes | Hidden |
| `api_request_confirmation` | Yes | Hidden |

Discovery works without an API credential in a manually started catalog-only session. Invocation stays unavailable until a separately configured, dedicated API credential is supplied as `BOSS_MCP_API_KEY`.

### Credential requirements

- The key must be a BOSS key whose account can see **exactly** the pinned session project (`GET /v1/projects`), and, when the key is pinned to projects, only that one. This is checked when the session starts and before each call.
- A multi-project operator needs one dedicated principal and key per project. Calls cannot switch projects through arguments, including operations that accept only a resource ID and no `project_id` field.
- The bridge verifies that `/v1/auth/session` reports an API key and who it belongs to. It never reads the API container's own environment, and an HTTP MCP bearer token alone grants no API authority.
- The shared console key, the worker's key and the host tools' key are rejected for API-backed MCP calls.

To provision a credential by hand, create a service account in **Identity › Service accounts**, give it a role in one project only, issue a key limited to that project, and pass that key only to the MCP process.

!!! warning
    Keep `BOSS_MCP_API_KEY` in your MCP client's private environment or secret store, never in the MCP arguments or command line. The Community setup stores it only in the private, gitignored `.env` file.

### Per-domain tools

For a focused client, add `--api-domain core` (repeat for more domains). This advertises concrete per-operation tools from those domains alongside the four discovery tools. `--api-domain all` advertises every eligible operation and can overload some MCP clients, so the default stays compact. The `other` domain is also searchable and selectable while the API taxonomy is being completed.

### Find and call an operation

Search by path, product term, or alias. Descriptions contain the exact input schema and availability. The operation alias matches the Metatron TUI's `/op` command and the generated OpenAPI contract:

```json
{"name":"api_find_operations","arguments":{"query":"tasks","domain":"core"}}
{"name":"api_describe_operation","arguments":{"operation":"post_v1_tasks_54d40d0b"}}
{"name":"api_write_operation","arguments":{"operation":"post_v1_tasks_54d40d0b","body":{"title":"Prepare launch review"}}}
```

How the bridge constrains each call:

- Only an alias found in the live mounted schema can be invoked. MCP accepts no caller-supplied URL, HTTP method, headers, or credentials.
- `project_id` is fixed to the session project wherever an operation accepts it in paths, query parameters, and JSON bodies. Global operations remain global. The API-key principal is the actor.
- The API then enforces project membership, edition, permission, policy, budget, approval, lease, and receipt rules. API errors, including 401/403/409/422, stay visible in the MCP result.
- Submitted secret material is redacted from success and error results, including nested messages. Secret references and metadata remain usable.
- Read-only MCP hides `api_write_operation` and direct write tools.

## Operation coverage

The operation inventory is generated from each edition's mounted `app.openapi()` and keyed by **method plus path**, because FastAPI's automatic operation IDs are duplicated on a few multi-method routes. Run `python scripts/audit_mcp_api_parity.py --check` to verify [`mcp-api-capability-matrix.json`](mcp-api-capability-matrix.json). At this revision it accounts for all **983 Community** and **1,035 Enterprise** operations:

| Mounted edition | API policy eligible | Human confirmation required | Held for route safety work | Excluded from MCP |
|---|---:|---:|---:|---:|
| Community | 851 | 64 | 0 | 68 |
| Paid-only additions | 39 | 0 | 2 | 0 |

The public Community export can verify its mounted subset with `python scripts/audit_mcp_api_parity.py --check-community`.

### API policy eligible

The operation is discoverable, and the bridge may submit it with a verified scoped principal. That does not mean the account has permission, that an edition includes the feature, or that an approval has been granted. Notable eligible operations:

| Operation | Requirement |
|---|---|
| Apply an already approved Resource Plane ChangeSet | Callable because the API checks approval, active budget reservation, and resource receipts |
| Workflow mapping updates | `workflow_manage` |
| Soft-archive a task | `task_create`, with an audit event |
| Roll back a paused workflow migration | `workflow_manage`, with an audit event; only before its new steps execute |
| Project meeting-role edits | `workflow_manage` |
| Planned team-structure and agent-role edits | `role_review` |

For these edits the API records the authenticated member as the actor, even if a request body names another actor. The standard MCP service account does not receive these management permissions by default.

Changing or applying the gate-autonomy policy requires a separate human CEO confirmation. The approving human, who holds `decision_resolve`, is the actor recorded on the policy. Dedicated MCP principals cannot be CEOs.

!!! note "Revoking access"
    Revocation must retain an inactive member row; Enterprise SCIM deprovisioning does this for project and org memberships. Deleting a seeded default member row is not a revocation: a later explicit bootstrap can recreate a missing default. Bootstrap preserves inactive rows and edited permissions.

### Human confirmation required

For the 64 confirmation-gated actions, MCP calls `api_request_confirmation` with the exact intended operation, path, query, and body. The flow:

1. **Request.** BOSS stores a digest of the complete request, its method and target, and a limited review summary. It never stores the raw body. The stored summary omits free-form body and query values.
2. **Review.** A **separate human CEO member** finds the pending request with `/ops mcp-confirmations` and `/op` on the GET confirmation operation (direct API: `GET /v1/mcp-confirmations?project_id=...`), and inspects the complete request.
3. **Approve.** The human submits that exact `reviewed_request` plus its digest through the terminal or API: `/op` on the POST approve operation with `project_id`, `reviewed_digest`, and the complete `reviewed_request` object (direct API: `POST /v1/mcp-confirmations/{id}/approve`). The server recomputes the digest from the human's submitted method, path, query, and body before approval, and never persists the review submission. A mismatch fails before approval.
4. **Execute.** The MCP client calls `api_write_operation` with **identical** path, query, and body plus the returned `confirmation_id`. Approval expires in five minutes and is consumed atomically by one matching request.

Existing API permissions, budget, ChangeSet, receipt, and route policies still apply. Request, approval, consumption, and HTTP result are recorded as distinct audit events. When BOSS is served behind a proxy prefix, the reviewed path remains the route-relative `/v1/...` path; the proxy prefix is not part of the digest.

With the human's own TUI key, use the operation IDs from `/describe` and send the approval as one `/op` JSON argument:

```text
/op get_v1_mcp_confirmations_d1bc4240 {"query":{"project_id":"boss-dogfood"}}
/op post_v1_mcp_confirmations_confirmation_id_approve_3742b3e9 {"path":{"confirmation_id":"mcpconf_<id>"},"body":{"project_id":"boss-dogfood","reviewed_digest":"<digest>","reviewed_request":{"project_id":"boss-dogfood","operation":"post_v1_members_7de05164","method":"POST","path":"/v1/members","query":{},"body":{"project_id":"boss-dogfood","actor":"mcp_operator","member_id":"new_agent","display_name":"New agent","member_type":"agent","role_keys":["operator"],"permissions":["task_create"]}}}}
```

Replace the example values with the exact request you reviewed, including every body and query field. In this example the dedicated MCP member also needs `member_manage`, which the default `make mcp-setup` grants do not include.

!!! warning "Keep the reviewer separate"
    The human credential must never be available to the MCP client. The confirmation endpoints are excluded from MCP tool dispatch, so the client cannot approve itself. The reviewer must be an active `member_type=human` CEO with `decision_resolve` permission.

Actions that use this exact-request review include:

| Action | Notes |
|---|---|
| Decision reject, revise, delegate, and manual execute; ChangeSet approval; GitHub merge authorization; GitHub conflict-resolution approval | These seven decision and approval actions also require the MCP member to hold the route's normal project permission. |
| Decision approval, preview approval, autonomy changes, and the other previously held product routes | Each records an actor, project scope, and a receipt. Spend requires an active budget reservation, worker and deploy effects take a closed local lease, and destructive deploy, purge, teardown, and rollback record a rollback plan. These handlers do not call an external provider from the confirmed path. |
| Promotion of a winning improvement trial | Creates an approval-gated proposal and decision. It does not adopt a new baseline or roll out code. |
| Removing a project meeting role or a declared team-chart assignment | Changes only local project records. The API still checks `workflow_manage` or `role_review` respectively and records the MCP member in the corresponding event. The team-chart route's `agent_id=*` target clears every declared assignment in that project, so the reviewer must check that exact target as well as the project. |

For the seven decision and approval actions, the API applies this boundary to every nonhuman authenticated principal, even when a caller uses direct HTTP and omits MCP headers. Bind the MCP key to an active agent or system member, never to a human member or the console's human key; a direct call with the MCP key cannot turn its approval permission into human approval. The API checks that permission and the reviewing human's active CEO membership again when it consumes the confirmation. The domain records the human reviewer as the actor, while the confirmation trail retains the MCP requester. The request preview states `effect_actor: approving_human`; the submitted body still contains the MCP member's actor ID because it is bound to the MCP session.

### Held

The 2 held Community actions and two paid-only actions return `operator_only_unavailable`. The Community holds are internal infrastructure routes and stay out of this product release; paid-only holds stay behind the edition gate. See the [operation matrix](mcp-api-capability-matrix.json) for each mounted operation's current disposition.

### Excluded

The 68 excluded Community routes are:

- internal worker and lease protocols
- webhook and identity callbacks
- preview reverse proxies
- the bootstrap seed
- the four confirmation protocol endpoints
- KMS lease/resolve routes that can return raw secret material

Customer secret-management routes remain available through API permission checks with redacted results.

## Request body schemas

| Community operations | Count |
|---|---:|
| JSON request body with named top-level fields in the mounted OpenAPI schema | **533** |
| No JSON body | 450 |
| Of the 533, Pydantic field handling | **454** |
| Of the 533, pass-through fields that preserve existing handler coercion and status codes | **79** |

No Community JSON body remains loose at the top level. The 79 pass-through bodies help MCP clients construct requests, but their field types are not enforced by Pydantic, and some nested payloads remain intentionally flexible.

The matrix's `request_body_type_enforcement` field makes this distinction explicit. Its legacy `runtime_validated_input_schema` field describes the mounted field schema and does not guarantee field-type enforcement. The API handler and its policy checks remain authoritative.

## MCP servers BOSS calls: review before unattended use

The servers you register on **Skills and MCP** (or ask BOSS to register from the chat) are the other direction: BOSS is the client. A tool on a server nobody has marked **Reviewed** never runs unattended, whatever risk tier it declares. That holds on every road to the tool: an agent calling it directly, an agent holding an authored `mcp_tool` skill that names it, and a workflow custom step bound to the tool or to such a skill. Each call becomes an approval, a custom step on an unreviewed server is always a manual step, and BOSS refuses a call that arrives without an approved decision.

**After upgrading:** servers registered before this rule were never marked, so calls that used to run now ask first. The Skills and MCP page lists every turned-on server that needs review. Open each with **Edit**, check its address, and choose *A person has checked this server*. Changing a server's URL clears its review, including a server BOSS seeded.

### Rate limits on tool calls

A rule on **Skills and MCP › Rules** with the effect **Rate limit** caps how often a tool runs: at most `max_calls` calls in `window_seconds`. The calls counted are the ones that actually ran (succeeded, deduplicated or failed) to that server and tool, read from the tool-call audit. Choose what the count is kept for:

- **per project** (the default): every call in the project. A rule written before buckets existed counts this way.
- **per run**: each run has its own count, so one looping run is stopped while other runs go on.
- **per agent**: each agent role has its own count across runs.

When the window is spent the call is refused. The refusal is structured: it names the rule, the limit, the bucket and `retry_after_seconds`, which is when the oldest counted call ages out of the window. The audit row records the refusal under `screen.policy`. An agent on BOSS's own tools receives the same fields. A caller reaching the tool through the receipt door gets `McpToolRateLimited`, which is a kind of `McpToolDenied`, so code that already handles a denial needs no change. The gateway reports the reason `rate_limited`.

**Suggested limits.** Some catalogue servers come with default limits. GitHub, for example, suggests 30 `create_*` calls an hour per run. These defaults appear under **Suggested limits** on the Rules tab once you have added the server. Nothing is applied until you press **Add**. The API lists them at `GET /v1/tool-policies/suggestions?project_id=…`, and each one is a body you can send unchanged to `POST /v1/tool-policies`.

### MCP aggregators: Composio, Arcade, Zapier, Pipedream

An aggregator puts many SaaS apps behind one MCP server. All four are in **Add MCP › Connectors** under *Automation*. Each URL belongs to your account, so replace the placeholder in the form with your own URL before **Save & test**. The test proves the URL. If the test fails, nothing is stored.

| Aggregator | Sign-in | Extra setup |
|---|---|---|
| Composio | Your Composio API key in the `x-api-key` header | The server URL names the Composio user whose connected apps the server uses. |
| Arcade | An Arcade API key as a bearer token | After saving, add the `Arcade-User-ID` header. It names the user the tools act as. |
| Zapier | The token of your Zapier MCP server | Turn on only the actions agents need. |
| Pipedream | An access token from Pipedream's OAuth client credentials | After saving, add the `x-pd-project-id`, `x-pd-environment` and `x-pd-external-user-id` headers. Add `x-pd-app-slug` to limit the server to one app. The token expires, so renew it when calls start failing with 401. |

**Risk defaults.** Every app's tools start at tier 2, so each call asks a person. Composio's meta-tools are tiered by name:

- Searching tools and reading tool schemas run without asking (tier 0).
- Running a tool and managing connections ask a person (tier 2).
- The remote workbench and shell tools are tier 3 and Off.

**Target one app with a rule.** Each aggregator puts the app's name into its tool names in its own way. Composio uses `GMAIL_*`, Arcade `Gmail_*`, Zapier `gmail_*` and Pipedream `gmail-*`. A **Tools** glob such as `GMAIL_*` on the Composio server therefore covers every Gmail tool behind it. The catalogue records each pattern as `aggregator.toolkit_pattern`. `connector_catalog.toolkit_selector` builds the selector for you.

A meta-tool that runs another tool by name, such as `COMPOSIO_MULTI_EXECUTE_TOOL`, is not covered by an app glob. Target it by its own name instead, and add a condition on the argument that names the tool.

**Suggested limits.** Each aggregator suggests a per-run rate limit. On Composio the limit is on the execute meta-tool; on the others it covers all tools. You can add these limits from **Rules › Suggested limits**.

These entries were written from each vendor's published documentation. BOSS's tests have not run them against a live account.

### Headers BOSS sends upstream

By default a call to an MCP server carries the server's sign-in, any plain headers set on its auth, and BOSS's protocol headers. You can change this per server under **Upstream headers** in the server's drawer, or with `POST /v1/mcp/servers/{id}/headers-policy`. There are three settings:

- **Send** (`forward`) turns on per-call metadata: `X-BOSS-Run-Id`, `X-BOSS-Agent`, and the W3C `traceparent` of the current span. `traceparent` is sent only when BOSS's own tracing is on. These headers go on each `tools/call` request only, never on the session's `initialize`.
- **Only send** (`allow`) is an allowlist. When it is set, BOSS strips every extra header that is not on the list. The sign-in header and BOSS's protocol headers are always sent.
- **Secret headers** (`secret_headers`) are static headers whose values are secrets, such as a tenant key. Each value is written to the secret store and read only when a call is made. The console shows only the last four characters. To keep a value that is already stored, send it as `{"keep": true}`.

A policy cannot set `Authorization`, BOSS's protocol headers or the `X-BOSS-*` names. Every header still passes BOSS's passthrough check, so no policy can send a BOSS session, key or run token to a server. An empty policy (`{}`) sends exactly what BOSS sent before this setting existed.
