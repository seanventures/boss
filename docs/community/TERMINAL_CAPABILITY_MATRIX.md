# Terminal capability matrix

This page maps the HTTP controls the Metatron terminal can reach, measured against the Community and Enterprise API contract snapshots and the running API's capability catalog.

!!! note "Reachable is not authorized"
    The matrix shows which HTTP controls are reachable from the terminal. It is not an authorization or end-to-end execution claim. For the edition and image you are actually running, the live `/coverage` command is authoritative.

## At a glance

| Contract | Total operations | `/v1/` operations | Addressable by `/op` | Outside `/op` |
|---|---:|---:|---:|---|
| Community (static snapshot) | **994** | **981** | **981** | **13** health, SAML, and SCIM protocol routes |
| Enterprise (static snapshot) | **1,035** | **1,022** | **1,020** | 13 non-`/v1/` routes, plus 2 inbound webhook receivers |

- In this repository, the Community contract is `api/openapi.yaml`. The Enterprise source keeps the same Community snapshot at `api/openapi.community.yaml` and its own contract at `api/openapi.yaml`.
- Every Community `/v1/` operator operation has a unique stable operation ID and a JSON/path/query shape addressable by `/op`. The 13 protocol routes remain outside the authenticated `/v1/` operator command.
- The two unaddressable Enterprise `/v1/` routes are inbound webhook receivers that require request-signature headers; they are not operator actions. All 1,035 static Enterprise operations have a unique `operationId`.
- The terminal's stable method-and-path alias remains available even if a live server uses a different runtime ID.

## How the terminal maps operations

The terminal fetches `/v1/meta/api-catalog` and `/openapi.json` from the same API origin it uses for ordinary requests.

| Command | Role |
|---|---|
| `/projects` | Lists projects the API returns under the current authentication settings |
| `/ops` | Lists domains and searches their operations |
| `/ops * --page N` | Complete paged discovery |
| `/describe` | Shows an operation's method, path, parameters, typed body fields, and an example with required fields |
| `/op` | Invokes a stable method-and-path operation ID with JSON `path`, `query`, and `body` arguments |
| `/api METHOD /v1/path [JSON]` | Fallback for a mounted route missing from OpenAPI |

`/op` inserts the selected project into a typed JSON body when `project_id` is declared and omitted; an explicit value is preserved for API authorization. Both invocation paths use the same API key, project scope, and server enforcement as direct HTTP clients.

## Operations by domain

| API catalog domain | Community `/v1/` operations in the current snapshot | Examples discoverable with `/ops` |
| --- | ---: | --- |
| Delivery | 228 | Workflow runs, code tasks, deployments and previews |
| Operations | 188 | Health, incidents, reliability and fleet controls |
| Agents | 125 | Agent definitions, runs, skills and process controls |
| ProviderOps | 67 | Provider accounts, resources, ChangeSets and deployment targets |
| Governance | 58 | Assurance, approvals, claims and evidence |
| Core | 57 | Tasks, decisions, budget status, receipts and events |
| Work | 57 | Workflow definitions, mappings, roadmap and portfolio work |
| Models | 31 | Model gateway, entitlements and route decisions |
| Knowledge | 29 | Artifacts, learning and search |
| Delivery CI/CD | 23 | Pipeline and release controls |
| Cloud | 20 | Edition status and configured cloud connections |
| Meta | 19 | Capability catalog and API coverage |
| Auth | 18 | Sessions, members, roles, and org membership |
| Console | 17 | Executive console and coordination read models |
| Workspace | 12 | Organizations, projects and departments |
| Goldeneye | 11 | Goldeneye service integration |
| Setup | 9 | Bootstrap and first-run setup |
| Hardware | 7 | Local machine, connected places, and the hardware agent |
| Identity | 5 | Identity provider catalog and configuration |
| **Total** | **981** | All mounted `/v1/` operations are searchable by method/path/summary |

The table classifies the static Community contract using the API catalog taxonomy. No Community snapshot operation falls into the catalog's “Other” domain. Live domain counts may differ by image and edition; `/ops` and `/coverage --json` read the running API.

### Request bodies

| Community operations | Count |
|---|---:|
| JSON request body with named top-level fields for `/describe` | 537 |
| No JSON body | 456 |
| Of the 537, bodies whose documented fields preserve the handler's existing pass-through input behavior | 84 |

For those 84 bodies, field types are guidance rather than a new validation promise. Nested extension objects can still be flexible. The API returns the final validation and policy result when `/op` invokes an operation.

## Practical workflows

| Workflow | Discover | Invoke through the TUI | Governing server boundary |
| --- | --- | --- | --- |
| Pick a project and inspect work | `/projects`, `/project ID`, `/ops tasks` | `/op get_v1_tasks_339c8c60` | Project membership and task visibility |
| Create auditable work | `/describe post_v1_tasks_54d40d0b` | `/op post_v1_tasks_54d40d0b {"body":{"title":"Title","dedupe_key":"unique-key"}}` | Task creation, event and dedupe rules |
| Review and resolve decisions | `/decisions`, `/decision ID` | `/decision approve ID NOTE` or `/decision reject ID NOTE` | Project read scope, authenticated human reviewer, role/risk/confirmation and transition rules |
| Start and inspect workflows | `/ops workflow-runs` | Invoke listed create/read/run operations | Version pinning, lease and approval state |
| Author an agent | `/ops agent-studio` | Invoke listed definition/version/validate/publish operations | Project permissions, validation and publication |
| Control spend | `/ops budget` | Invoke quote/status/reserve/consume/release operations | Budget Governor and reservation rules |
| Inspect side effects | `/ops receipts` | Invoke receipt/event read operations | Scope and audit visibility |
| Manage providers | `/ops changesets` | Invoke plan, approve and apply operations | Edition, permission, ChangeSet, reservation and ResourceReceipt gates |
| Review models | `/ops model-route` | Invoke routing read/simulation operations | Entitlement, data boundary and spend policy |

Run `/describe OPERATION_ID` before any generic write. `/op` never invents approvals, reservation IDs, dedupe keys, lease tokens, or provider credentials. A successful terminal response means the API accepted the call; check subsequent work and effects through their status, receipt, and event endpoints.

### Shortcut commands

| Area | Commands | Notes |
|---|---|---|
| Tasks | `/tasks [PAGE]`, `/task ID`, `/task create DEDUPE_KEY TITLE` | Use the same task API routes. Supply a stable dedupe key so a retried creation does not produce a second task. Other fields remain available through `/op`. |
| Decisions | <code>/decisions [open&#124;all] [PAGE]</code>, `/decision ID`, `/decision approve ID NOTE`, `/decision reject ID NOTE` | Read the scoped inbox or one card; approve and reject use the existing decision transition API after a scoped detail read. Other decision transitions use `/op`. |

For decisions, the API owns reviewer identity, role and risk checks, any required human confirmation, transition legality, and audit events.

## Verification and limits

- `scripts/tests/test_metatron_tui.py` checks all 994 Community snapshot operations and verifies that every `/v1/` operation has a unique, addressable terminal alias. It also covers project discovery and selection, paged operation search, typed-body project defaults, parameter validation, duplicate FastAPI IDs, server-authenticated requests, and catalog-only route reporting.
- `/coverage --json` (one-shot mode) enumerates every route from the *running* API's OpenAPI and catalog with `source`, `terminal_invokable`, `raw_api_invokable`, and `edition_policy_access`. The last field stays `unverified_server_enforced` until an actual authorized call is made.
- A mounted route can still return an edition, role, budget, rollout, or approval denial. The TUI does not infer effective entitlements from OpenAPI.
- Addressability through the API, TUI, and MCP is separate from proving every workflow on a clean installation. Live operator acceptance and any API behavior gaps are tracked as separate release evidence.
