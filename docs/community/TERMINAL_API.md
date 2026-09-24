# Terminal and API access

The Metatron terminal connects to the BOSS HTTP control plane. Set
`BOSS_API_URL`, `BOSS_API_KEY`, and `BOSS_PROJECT_ID`, then run
`python3 scripts/metatron_tui.py`
from a BOSS checkout. `BOSS_ACTOR` is optional. The API key is sent in the
`X-Boss-Api-Key` header and is never printed by the terminal.

Conversational text goes to `/v1/metatron/session`. The built-in commands are
`/goals`, `/watch`, `/overwatch`, and `/drive [auto|go]`. For any other BOSS
HTTP operation, use `/api METHOD /v1/path [JSON]`. The method may be `GET`,
`POST`, `PUT`, `PATCH`, or `DELETE`; the optional JSON body is for non-GET
requests. The path must be a local `/v1/` route. The terminal does not infer
`project_id` for generic API calls because endpoint scopes differ, so include
it where the route requires it.

```text
metatron> /api GET /v1/tasks?project_id=boss-dogfood
metatron> /api POST /v1/metatron/watch {"project_id":"boss-dogfood","actor":"executive_console"}
```

Scripts can run one command and receive one JSON result:

```sh
python3 scripts/metatron_tui.py --exec '/api GET /v1/tasks?project_id=boss-dogfood' --json
```

One-shot mode exits `0` for a successful HTTP response, `1` for an HTTP or
connection failure, and `2` for invalid local input or missing configuration.
HTTP failures return a JSON object with `__http_error__`, `status`, and `body`.
The API remains the authority for permissions, policies, budget gates,
receipts, and audit records. The terminal does not create a separate execution
path. Fetch `/openapi.json` from the running API for its current route schema.
Terminal, API, and MCP route parity is tracked as BOSS-416.

For direct engine tooling, `boss-engine tools`, `boss-engine call`, and
`boss-engine code -o json|stream-json` remain available. `boss-engine
serve-mcp` exposes the engine tool layer to agent clients over stdio; its
optional HTTP transport is read-only by default.
