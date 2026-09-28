# Drive BOSS through the API, terminal, or MCP

BOSS has one HTTP control plane. The terminal and API-backed MCP session use
that control plane with their own credentials; the API enforces project access,
permissions, budget, approval, and audit rules for each request.

| Interface | Discover operations | Invoke an operation | Start on an installed Community VM |
|---|---|---|---|
| HTTP API | `/openapi.json` and `/v1/meta/api-catalog` | HTTP method and `/v1/` path | Use a project-bound API key; see [terminal and API access](TERMINAL_API.md). |
| Metatron terminal | `/projects`, `/ops * --page N`, `/describe ID`, `/coverage` | `/op ID {"path":{},"query":{},"body":{}}` | `make metatron` |
| API-backed MCP | `api_find_operations`, `api_describe_operation` | `api_read_operation`, `api_write_operation` | `make mcp-setup`, then `make mcp`; see [MCP access](MCP.md). |

The terminal operation ID and MCP alias both come from the HTTP method and
path. Search in either interface, inspect the requested fields, then submit
the operation. Both interfaces show API errors, including permission and
approval denials, rather than treating a submitted request as completed work.

For example, creating a task uses the same API operation in all three:

```text
HTTP    POST /v1/tasks
        {"project_id":"boss-dogfood","title":"Plan launch","dedupe_key":"launch-plan-1"}

TUI     /project boss-dogfood
        /describe post_v1_tasks_54d40d0b
        /op post_v1_tasks_54d40d0b {"body":{"title":"Plan launch","dedupe_key":"launch-plan-1"}}

MCP     api_describe_operation({"operation":"post_v1_tasks_54d40d0b"})
        api_write_operation({"operation":"post_v1_tasks_54d40d0b",
                             "body":{"title":"Plan launch","dedupe_key":"launch-plan-1"}})
```

The terminal inserts the selected `project_id` only when the typed body
declares it and the supplied body omits it. An API-backed MCP session pins one
project when it starts; its dedicated key must see exactly that project. Read
the returned task and event history to verify the result. Other operations may
require a reservation, decision, or separate approval before they can proceed.

The [terminal capability matrix](TERMINAL_CAPABILITY_MATRIX.md) maps all 982
mounted Community HTTP operations; 969 `/v1/` operator operations are
addressable by `/op`. The [MCP operation matrix](mcp-api-capability-matrix.json)
accounts for all 982: 853 are eligible for API policy checks, 75 sensitive
operator actions remain unavailable through MCP pending a trusted human
confirmation path, and 54 protocol or internal routes are excluded. The API
and terminal can submit operator requests, but existing server checks can
deny or hold an action. A mapped operation does not imply that an edition
enables the feature or that a particular credential is authorized.
