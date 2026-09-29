# Use the terminal

The Metatron terminal talks to the BOSS HTTP API. It does not open a second control plane. From a checkout, set `BOSS_API_URL`, `BOSS_API_KEY`, and `BOSS_PROJECT_ID`, then run `python3 scripts/metatron_tui.py`. `BOSS_ACTOR` is optional. The API key is sent in the `X-Boss-Api-Key` header and is never printed by the terminal. Examples that name `boss-dogfood` mean the project seeded on your machine, not a shared server.

The [quickstart](QUICKSTART.md) is the path to a console on `127.0.0.1` if you have not installed yet. What Community includes is in [Features](FEATURES.md). A trusted MCP client is a different entry point; see [MCP](MCP.md).

For an installed Community stack, run `make metatron` from the install
directory. It opens the terminal inside the API container using the install's
configured API key and project; no host Python setup is needed.

Conversational text goes to `/v1/metatron/session`. The built-in commands are
`/goals`, `/watch`, `/overwatch`, and `/drive [auto|go]`. Use `/projects` to list
projects returned by the API and `/project ID` to select one. For everyday work,
`/tasks [PAGE]` lists 25 tasks at a time, `/task ID` shows one task, and
`/task create DEDUPE_KEY TITLE` creates one. Choose a stable dedupe key for a
logical task and reuse it if a request needs retrying. The terminal sends the
selected project and configured actor to the task API; the server enforces
permissions and records the task and audit event. For example:

```text
metatron> /project boss-dogfood
metatron> /tasks
metatron> /task create launch-plan-1 Plan the community launch
metatron> /task task-123
metatron> /tasks 2
```

Review the selected project's decision inbox with `/decisions` (pending and
delegated items), `/decisions all` (every status), or
`/decisions open 2` (the next page). `/decision ID` shows the question, risk,
reason, recommendation, options, and cost summary before an action. Approve or
reject with an explicit note:

```text
metatron> /decisions
metatron> /decision dec-launch-1
metatron> /decision approve dec-launch-1 Release gates reviewed
metatron> /decision reject dec-launch-2 Keep the spending guardrail
```

These commands use the ordinary decision API. The terminal includes the selected
project on inbox and detail reads, and checks the detail in that project before
posting an action. The API binds the action to the authenticated reviewer and
enforces role, risk, confirmation, and transition rules. A service key that
requires a separate human confirmation still receives an HTTP 409; the terminal
does not grant one. Use `--exec '/decision ID' --json` for the full decision card.
Other decision transitions and fields remain available through `/op`.

Use `/ops` to see
product domains exposed by the running API, `/ops TASK` to search operations,
or `/ops * --page N` to browse every operation in pages of 25. Domain searches
also accept `--page N`. Interactive output shows concise rows; one-shot `--json`
returns the same page with counts and stable operation IDs. Use
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
For a typed JSON request body that declares `project_id`, `/op` inserts the
selected project when the provided body omits that field. An explicit
`project_id` is preserved, and the API validates authorization for that
project. Other required body fields must still be supplied. `/describe` lists
typed fields, choices, and nested required keys where the API schema provides
them. `/project ID` also clears the prior Metatron conversation thread. The
API checks access and policy on every call.

```text
metatron> /ops
metatron> /projects
metatron> /ops tasks
metatron> /ops * --page 2
metatron> /describe get_v1_tasks_339c8c60
metatron> /op get_v1_tasks_339c8c60
metatron> /op post_v1_tasks_54d40d0b {"body":{"title":"Plan launch","dedupe_key":"launch-plan-1"}}
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
If an API response echoes a submitted credential, the terminal redacts values
from nested secret, token, password, API-key, and credential fields (including
`value` on credential routes) before showing or returning the response.
Command examples for this terminal are in the [terminal capability matrix](TERMINAL_CAPABILITY_MATRIX.md).
Scope for Community is in [Features](FEATURES.md), and [MCP](MCP.md) is the
separate client. Do not paste API keys into an issue; report a vulnerability
only by email, as [SECURITY.md](https://github.com/seanventures/boss/blob/main/SECURITY.md) describes.

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

## Next

- [Guide index](README.md) for install, models, feedback, and security.
- [Console access](CONSOLE_AUTH.md) if you want the browser instead of this terminal.
- [Control surfaces](CONTROL_SURFACES.md) for how the API, this terminal, and MCP line up.
