# Control surfaces

BOSS has one HTTP control plane. The Metatron terminal and the API-backed MCP session are clients of that control plane with their own credentials, and the API enforces project access, permissions, budget, approval, and audit rules on every request.

| Interface | Discover operations | Invoke an operation | Start on an installed Community VM |
|---|---|---|---|
| HTTP API | `/openapi.json` and `/v1/meta/api-catalog` | HTTP method and `/v1/` path | Use a project-bound API key; see [terminal and API access](TERMINAL_API.md). |
| Metatron terminal | `/projects`, `/ops * --page N`, `/describe ID`, `/coverage` | `/op ID {"path":{},"query":{},"body":{}}` | `make metatron` |
| API-backed MCP | `api_find_operations`, `api_describe_operation` | `api_read_operation`, `api_write_operation`; sensitive actions use `api_request_confirmation` | `make mcp-setup`, then `make mcp`; see [MCP access](MCP.md). |

## One operation, three surfaces

The terminal operation ID and the MCP alias are both derived from the HTTP method and path, so the workflow is the same everywhere:

1. Search for the operation.
2. Inspect its requested fields.
3. Submit it.

Both the terminal and MCP surface API errors, including permission and approval denials, instead of reporting a submitted request as completed work. Creating a task, for example, uses the same API operation in all three:

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

`boss-dogfood` is the default project the installer seeds on your machine.

## Project scoping

| Surface | How the project is chosen |
|---|---|
| Terminal | Inserts the selected `project_id` only when the typed body declares it and the supplied body omits it. |
| API-backed MCP | Pins one project when the session starts; its dedicated key must see exactly that project. |

To verify a result, read the returned task and its event history. Other operations may require a reservation, decision, or separate approval before they proceed.

## Coverage

| Surface | Coverage of the 993 mounted Community HTTP operations |
|---|---|
| Terminal ([capability matrix](TERMINAL_CAPABILITY_MATRIX.md)) | All 993 mapped; 980 `/v1/` operator operations are addressable by `/op`. |
| MCP ([operation matrix](mcp-api-capability-matrix.json)) | All 993 accounted for: 860 eligible for API policy checks, 64 sensitive actions require separate human approval of an exact request, 2 remain held for route-specific safety gaps, and 67 protocol or internal routes are excluded. |

!!! note "Mapped is not the same as authorized"
    The API and terminal can submit operator requests, but existing server checks can still deny or hold an action. A mapped operation does not imply that an edition enables the feature or that a particular credential is authorized.
