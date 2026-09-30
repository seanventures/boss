# Terminal and API

The Metatron terminal is a command-line client for the BOSS HTTP API. It uses the same control plane, credentials, and server-side enforcement as any other API client, and adds project selection, operation discovery, and one-shot JSON output for scripts.

New to BOSS? The [quickstart](QUICKSTART.md) gets you to a console on `127.0.0.1`, and [Features](FEATURES.md) lists what Community includes. To connect an agent client instead, see [MCP](MCP.md).

## Start the terminal

### Installed Community stack

From the install directory:

```sh
make metatron
```

This opens the terminal inside the API container with the install's configured API key and project. No host Python setup is needed.

### From a checkout

Set these environment variables, then run `python3 scripts/metatron_tui.py`:

| Variable | Required | Purpose |
|---|---|---|
| `BOSS_API_URL` | Yes | API origin |
| `BOSS_API_KEY` | Yes | Sent in the `X-Boss-Api-Key` header; never printed by the terminal |
| `BOSS_PROJECT_ID` | Yes | Initial project |
| `BOSS_ACTOR` | No | Actor the terminal sends with requests |

Examples on this page use `boss-dogfood`, the default project seeded on your machine.

## Command reference

| Command | What it does |
|---|---|
| *(plain text)* | Sends a conversational message to `/v1/metatron/session` |
| `/goals`, `/watch`, `/overwatch`, <code>/drive [auto&#124;go]</code> | Built-in Metatron captain commands |
| `/projects` | Lists projects returned by the API |
| `/project ID` | Selects a project and clears the prior Metatron conversation thread |
| `/tasks [PAGE]` | Lists 25 tasks at a time |
| `/task ID` | Shows one task |
| `/task create DEDUPE_KEY TITLE` | Creates a task |
| <code>/decisions [open&#124;all] [PAGE]</code> | Reads the decision inbox |
| `/decision ID` | Shows one decision card |
| `/decision approve ID NOTE`, `/decision reject ID NOTE` | Approves or rejects a decision with a note |
| `/ops [TERM] [--page N]` | Lists API domains or searches operations |
| `/describe OPERATION_ID` | Shows an operation's parameters and request body |
| `/op OPERATION_ID [JSON]` | Invokes an operation |
| `/api METHOD /v1/path [JSON]` | Calls a local `/v1/` route directly |
| `/coverage` | Compares the running OpenAPI with the mounted route catalog |

## Tasks

`/tasks [PAGE]` lists 25 tasks at a time, `/task ID` shows one task, and `/task create DEDUPE_KEY TITLE` creates one. Choose a stable dedupe key for each logical task and reuse it when retrying a request. The terminal sends the selected project and configured actor to the task API; the server enforces permissions and records the task and its audit event.

```text
metatron> /project boss-dogfood
metatron> /tasks
metatron> /task create launch-plan-1 Plan the community launch
metatron> /task task-123
metatron> /tasks 2
```

## Decisions

| Command | Shows |
|---|---|
| `/decisions` | Pending and delegated items |
| `/decisions all` | Every status |
| `/decisions open 2` | The next page |
| `/decision ID` | Question, risk, reason, recommendation, options, and cost summary |

Approve or reject with an explicit note:

```text
metatron> /decisions
metatron> /decision dec-launch-1
metatron> /decision approve dec-launch-1 Release gates reviewed
metatron> /decision reject dec-launch-2 Keep the spending guardrail
```

These commands use the standard decision API. The terminal includes the selected project on inbox and detail reads, and checks the detail in that project before posting an action. The API binds the action to the authenticated reviewer and enforces role, risk, confirmation, and transition rules.

!!! note
    A service key that requires a separate human confirmation still receives an HTTP 409; the terminal cannot grant that confirmation.

Use `--exec '/decision ID' --json` for the full decision card. Other decision transitions and fields are available through `/op`.

## Discover and invoke any operation

| Command | Use |
|---|---|
| `/ops` | Product domains exposed by the running API |
| `/ops TASK` | Search operations |
| `/ops * --page N` | Browse every operation in pages of 25; domain searches also accept `--page N` |
| `/describe OPERATION_ID` | Inspect parameters and request body |
| `/op OPERATION_ID [JSON]` | Invoke the operation |

Interactive output shows concise rows; one-shot `--json` returns the same page with counts and stable operation IDs.

Operation IDs are derived from the HTTP method and path, so they stay unique even when the API framework assigns duplicate IDs to a multi-method route, and they match the generated Community OpenAPI contract. The terminal fetches the live `/openapi.json` and capability catalog on each run rather than relying on a bundled route list.

### `/op` arguments

The JSON argument has three optional keys: `path`, `query`, and `body`. `path` and `query` are JSON objects.

- The terminal checks path placeholders and declared query names before sending a request.
- If an operation declares a `project_id` query parameter, the active project is inserted when omitted.
- If a typed JSON request body declares `project_id` and your body omits it, the selected project is inserted. An explicit `project_id` is preserved, and the API authorizes access to that project.
- All other required body fields must be supplied. `/describe` lists typed fields, choices, and nested required keys where the API schema provides them.

The API checks access and policy on every call.

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

### Direct routes with `/api`

For a catalog-only route or direct path control, use `/api METHOD /v1/path [JSON]`. The method may be `GET`, `POST`, `PUT`, `PATCH`, or `DELETE`; the optional JSON body applies to non-GET requests. The path must be a local `/v1/` route.

!!! warning
    Unlike `/op`, `/api` does not infer `project_id`. Include it wherever the route requires it.

```text
metatron> /api GET /v1/tasks?project_id=boss-dogfood
metatron> /api POST /v1/metatron/watch {"project_id":"boss-dogfood","actor":"executive_console"}
```

## Scripting with one-shot mode

Run one command and receive one JSON result:

```sh
python3 scripts/metatron_tui.py --exec '/api GET /v1/tasks?project_id=boss-dogfood' --json
python3 scripts/metatron_tui.py --exec '/op get_v1_tasks_339c8c60' --json
python3 scripts/metatron_tui.py --exec '/coverage' --json > tui-coverage.json
```

| Exit code | Meaning |
|---|---|
| `0` | Successful HTTP response |
| `1` | HTTP or connection failure |
| `2` | Invalid local input or missing configuration |

HTTP failures return a JSON object with `__http_error__`, `status`, and `body`.

`/coverage` compares the running `/openapi.json` with the mounted route catalog. Its JSON output includes each route's stable operation ID (if in OpenAPI), terminal addressability, raw `/api` addressability, and an explicit `unverified_server_enforced` access state.

!!! note "Discovery is not authorization"
    Discovery does not prove the caller has permission or that a paid edition feature is enabled. The API remains the authority for edition, permissions, policies, approvals, budget gates, receipts, and audit records; the terminal has no separate execution path.

## Credential handling

If an API response echoes a submitted credential, the terminal redacts values from nested secret, token, password, API-key, and credential fields (including `value` on credential routes) before showing or returning the response.

!!! warning
    Never paste API keys into an issue. Report vulnerabilities by email only, as described in [SECURITY.md](https://github.com/seanventures/boss/blob/main/SECURITY.md).

## Engine tooling

In the stock Community install, run the engine's Python module inside the API container. From the install directory:

```sh
docker compose -f scripts/install/community-compose.yml --env-file .env \
  exec -T api python -m boss_engine --project boss-dogfood tools
```

Use the same `python -m boss_engine` entry point for `call`, `code`, and `serve-mcp`. The Community API image does not install a `boss-engine` executable. If your user needs sudo for Docker, run these commands through a working Docker permission path. [MCP](MCP.md) shows the stdio command; its optional HTTP transport is read-only by default.

## Next

- [Terminal capability matrix](TERMINAL_CAPABILITY_MATRIX.md) for command examples by workflow.
- [Guide index](README.md) for install, models, feedback, and security.
- [Console access](CONSOLE_AUTH.md) if you want the browser instead of this terminal.
- [Control surfaces](CONTROL_SURFACES.md) for how the API, this terminal, and MCP line up.
