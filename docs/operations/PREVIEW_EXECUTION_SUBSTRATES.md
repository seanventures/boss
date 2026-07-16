# Preview and execution substrates

BOSS records where each code run built and where each preview is running. The
persisted value is evidence for that run; it is not recomputed from current
configuration.

## Build substrates

| Persisted value | Operator label | Meaning |
| --- | --- | --- |
| `local_subprocess` | built: API host | An allowlisted subprocess in an ephemeral host clone. It is not VM isolation. |
| `microvm` | built: micro-VM `sbx_…` | A single-tenant sandbox-service guest. The task id links to retained-rootfs inspection. |

`BOSS_SANDBOX_PROVIDER` selects the native pipeline substrate.
`BOSS_GOVERNED_SANDBOX_PROVIDER` is the explicit override for the governed,
role-separated pipeline. Both default to `local`; startup validates both values
and logs their normalized effective values together. They may differ during a
migration, but the run badge always shows the resolved value used for that run.

## Preview substrates

| Persisted value | Operator label | Use |
| --- | --- | --- |
| `preview_rootfs` | preview: retained rootfs | Preferred for a micro-VM build. Runs the app in the same retained filesystem that built and tested it. |
| `preview_docker` | preview: docker | Isolated fallback that builds/runs a container from the task branch. |
| `preview_host` | preview: host process | Portable last fallback for hosts without Docker or retained rootfs support. |

Automatic preview selection tries retained rootfs, then Docker, then host
process. Successful build VMs are held for at most two hours; preview stop or
expiry explicitly stops the rootfs server, and the sandbox-service TTL remains
the final leak guard. API startup reconciles every `running` row with the live
process/container/rootfs endpoint. It never bulk-stops previews.

## Routing, logs, and feedback

The portable route is `/v1/previews/<id>/web/`. HTML receives a base path and a
client shim for static resources, CSS URLs, `srcset`, fetch/XHR, history API,
EventSource, WebSockets, and dynamically inserted elements. Redirect `Location`
headers are rebased; non-HTML response bytes are passed through unchanged.

`GET /v1/previews/<id>/logs` streams startup logs as SSE. Failed starts carry a
classified failure kind and a concrete next action. The preview-injected
feedback control captures the current app route and selected element, then
creates a confirmation-gated feedback proposal. Only an authenticated operator
confirmation dispatches a budgeted governed retry.

## Retained-rootfs console boundary

The existing retained-rootfs console remains an inspection surface. Generic
live exec stays blocked in `sandbox_console.py`; the new sandbox-service
`preview` verb is a narrow, TTL-bound operation that starts only the detected or
explicit app command and exposes its port through the sandbox proxy. This does
not grant arbitrary browser-side shell access.
