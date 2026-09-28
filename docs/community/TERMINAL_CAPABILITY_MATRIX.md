# Terminal capability matrix

This matrix describes the BOSS-430 TUI mapping against the BOSS-416 Community
and private API contract snapshots and the running API's capability catalog. It
is a map of reachable HTTP controls, not an authorization or end-to-end
execution claim. The live `/coverage` command is authoritative for the
currently running edition and image.

## Mapping method

The terminal fetches `/v1/meta/api-catalog` and `/openapi.json` from the same
API origin as its ordinary requests. `/projects` lists projects returned by the
API key. `/ops` lists domains and searches their operations, with `/ops *
--page N` providing complete paged discovery. `/describe` shows each
operation's method, path, parameters, typed body fields, and an example with
required fields. `/op` accepts a stable
method-and-path operation ID and JSON `path`, `query`, and `body` arguments.
It inserts the selected project into a typed JSON body when `project_id` is
declared and omitted; an explicit value is preserved for API authorization.
`/api METHOD /v1/path [JSON]` remains the fallback for a mounted route missing
from OpenAPI. Both invocation paths use the same API key, project scope, and
server enforcement as direct HTTP clients.

The BOSS-416 static Community contract contains **982** HTTP operations:
**969** operator `/v1/` operations, each with a unique stable operation ID and
a JSON/path/query shape addressable by `/op`; **13** health, SAML, and SCIM
protocol routes remain outside the authenticated `/v1/` operator command.
In the published Community repository, this contract is `api/openapi.yaml`;
private source keeps the same Community snapshot at
`api/openapi.community.yaml`.
The static private contract has **1,023** operations: **1,010** `/v1/`
operations, of which **1,008** are `/op` addressable. The two remaining `/v1/`
routes are inbound webhook receivers that require request-signature headers
and are not operator actions. All **1,023** static private operations have a
unique `operationId`. That private contract is `api/openapi.yaml` only in the
private source repository. The TUI's stable method-and-path alias remains
available even if a live server uses a different runtime ID.

| API catalog domain | Community `/v1/` operations in the BOSS-416 snapshot | Examples discoverable with `/ops` |
| --- | ---: | --- |
| Delivery | 228 | Workflow runs, code tasks, deployments and previews |
| Operations | 184 | Health, incidents, reliability and fleet controls |
| Agents | 125 | Agent definitions, runs, skills and process controls |
| ProviderOps | 67 | Provider accounts, resources, ChangeSets and deployment targets |
| Governance | 58 | Assurance, approvals, claims and evidence |
| Core | 57 | Tasks, decisions, budget status, receipts and events |
| Work | 57 | Workflow definitions, mappings, roadmap and portfolio work |
| Models | 31 | Model gateway, entitlements and route decisions |
| Knowledge | 28 | Artifacts, learning and search |
| Delivery CI/CD | 23 | Pipeline and release controls |
| Cloud | 20 | Edition status and configured cloud connections |
| Meta | 19 | Capability catalog and API coverage |
| Auth | 18 | Sessions, members and API keys |
| Console | 17 | Executive console and coordination read models |
| Workspace | 12 | Organizations, projects and departments |
| Goldeneye | 11 | Goldeneye service integration |
| Setup | 9 | Bootstrap and first-run setup |
| Identity | 5 | Identity provider catalog and configuration |
| **Total** | **969** | All mounted `/v1/` operations are searchable by method/path/summary |

The table classifies the static Community contract with the BOSS-416 API
catalog taxonomy. Live domain counts may differ by image and edition; `/ops`
and `/coverage --json` read the running API. No Community snapshot operation
falls into the catalog's “Other” domain.

## Practical workflows

| Workflow | Discover | Invoke through the TUI | Governing server boundary |
| --- | --- | --- | --- |
| Pick a project and inspect work | `/projects`, `/project ID`, `/ops tasks` | `/op get_v1_tasks_339c8c60` | Project membership and task visibility |
| Create auditable work | `/describe post_v1_tasks_54d40d0b` | `/op post_v1_tasks_54d40d0b {"body":{"title":"Title","dedupe_key":"unique-key"}}` | Task creation, event and dedupe rules |
| Start and inspect workflows | `/ops workflow-runs` | Invoke listed create/read/run operations | Version pinning, lease and approval state |
| Author an agent | `/ops agent-studio` | Invoke listed definition/version/validate/publish operations | Project permissions, validation and publication |
| Control spend | `/ops budget` | Invoke quote/status/reserve/consume/release operations | Budget Governor and reservation rules |
| Inspect side effects | `/ops receipts` | Invoke receipt/event read operations | Scope and audit visibility |
| Manage providers | `/ops changesets` | Invoke plan, approve and apply operations | Edition, permission, ChangeSet, reservation and ResourceReceipt gates |
| Review models | `/ops model-route` | Invoke routing read/simulation operations | Entitlement, data boundary and spend policy |

For each workflow, use `/describe OPERATION_ID` before a write. The terminal
does not auto-fill write bodies or invent approvals, reservation IDs, dedupe
keys, lease tokens, or provider credentials. A successful terminal response
means the API accepted the call; subsequent work and effects must be checked
through their status, receipt, and event endpoints.

## Verification and limits

- `scripts/tests/test_metatron_tui.py` checks all 982 Community snapshot
  operations and verifies every `/v1/` operation has a unique, addressable
  terminal alias. It checks project discovery/selection, paged operation
  search, typed-body project defaults, parameter validation, duplicate FastAPI
  IDs, server-authenticated requests, and catalog-only route reporting.
- `/coverage --json` (one-shot mode) enumerates every route from the *running*
  API's OpenAPI and catalog with `source`, `terminal_invokable`,
  `raw_api_invokable`, and `edition_policy_access`. The last field is
  `unverified_server_enforced` until an actual authorized call is made.
- A mounted route can still return an edition, role, budget, rollout, or
  approval denial. The TUI does not infer effective entitlements from OpenAPI.
- API, TUI, and MCP addressability is separate from proving every workflow on
  a clean installation. Live operator acceptance and any API behavior gaps
  remain separate release evidence.
