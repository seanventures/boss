# Terminal and API access

The Metatron terminal connects to the BOSS HTTP control plane. Set
`BOSS_API_URL`, `BOSS_API_KEY`, and `BOSS_PROJECT_ID`, then run
`python3 scripts/metatron_tui.py`
from a BOSS checkout. `BOSS_ACTOR` is optional. The API key is sent in the
`X-Boss-Api-Key` header and is never printed by the terminal.

For an installed Community stack, run `make metatron` from the install
directory. It opens the terminal inside the API container using the install's
configured API key and project; no host Python setup is needed.

Conversational text goes to `/v1/metatron/session`. The built-in commands are
`/goals`, `/watch`, `/overwatch`, and `/drive [auto|go]`. Use `/ops` to see
product domains exposed by the running API, `/ops TASK` to search operations,
`/describe OPERATION_ID` to inspect parameters and request body, and
`/op OPERATION_ID [JSON]` to invoke one. The operation ID is derived from HTTP
method and path, so it remains unique even when the API framework assigns
duplicate IDs to a multi-method route. It matches the generated Community
OpenAPI contract. The terminal fetches the live `/openapi.json` and capability
catalog; it does not rely on a stale bundled route list.

An `/op` JSON argument has three optional keys: `path`, `query`, and `body`.
Path and query are JSON objects. The terminal checks path placeholders and
declared query names before sending a request. If an operation declares a
`project_id` query parameter, the active project is inserted when omitted.
Write bodies must supply their own `project_id` when the API requires it. Use
`/project ID` to change the active project; this also clears the prior
Metatron conversation thread. The API checks access and policy on every call.

```text
metatron> /ops
metatron> /ops tasks
metatron> /describe get_v1_tasks_339c8c60
metatron> /op get_v1_tasks_339c8c60
metatron> /op post_v1_tasks_54d40d0b {"body":{"project_id":"boss-dogfood","title":"Plan launch","dedupe_key":"launch-plan-1"}}
metatron> /project another-project
```

For a catalog-only route or direct path control, use
`/api METHOD /v1/path [JSON]`. The method may be `GET`, `POST`, `PUT`, `PATCH`,
or `DELETE`; the optional JSON body is for non-GET requests. The path must be
a local `/v1/` route. Unlike `/op`, `/api` does not infer `project_id`, so
include it where the route requires it.

```text
metatron> /api GET /v1/tasks?project_id=boss-dogfood
metatron> /api POST /v1/metatron/watch {"project_id":"boss-dogfood","actor":"executive_console"}
```

Scripts can run one command and receive one JSON result:

```sh
python3 scripts/metatron_tui.py --exec '/api GET /v1/tasks?project_id=boss-dogfood' --json
python3 scripts/metatron_tui.py --exec '/op get_v1_tasks_339c8c60' --json
python3 scripts/metatron_tui.py --exec '/coverage' --json > tui-coverage.json
```

One-shot mode exits `0` for a successful HTTP response, `1` for an HTTP or
connection failure, and `2` for invalid local input or missing configuration.
HTTP failures return a JSON object with `__http_error__`, `status`, and `body`.
`/coverage` compares the running `/openapi.json` with the mounted route
catalog. Its JSON output includes each route's stable operation ID (if in
OpenAPI), terminal addressability, raw `/api` addressability, and an explicit
`unverified_server_enforced` access state. Discovery does not prove the caller
has permission or that a paid edition feature is enabled. The API remains the
authority for edition, permissions, policies, approvals, budget gates,
receipts, and audit records. The terminal creates no separate execution path.
See [Terminal capability matrix](TERMINAL_CAPABILITY_MATRIX.md) for exact
snapshot counts and workflow examples. API contract parity is tracked as
BOSS-416; this terminal work is BOSS-430.

For direct engine tooling in the stock Community install, run the Python
module inside the API container. For example, from the install directory:

```sh
docker compose -f scripts/install/community-compose.yml --env-file .env \
  exec -T api python -m boss_engine --project boss-dogfood tools
```

Use the same `python -m boss_engine` entry point for `call`, `code`, and
`serve-mcp`. The Community API image does not install a `boss-engine`
executable. If your user needs sudo for Docker, use a working Docker
permission path for these commands. [MCP.md](MCP.md) shows the stdio command;
its optional HTTP transport is read-only by default.
