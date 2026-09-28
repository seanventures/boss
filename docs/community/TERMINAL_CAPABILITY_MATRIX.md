# Terminal capability matrix

This matrix describes the BOSS-430 TUI mapping against the frozen Community
API contract at `1a8102a2` and the running API's capability catalog. It is a
map of reachable HTTP controls, not an authorization or end-to-end execution
claim. The live `/coverage` command is authoritative for the currently running
edition and image.

## Mapping method

The terminal fetches `/v1/meta/api-catalog` and `/openapi.json` from the same
API origin as its ordinary requests. `/ops` lists domains and searches their
operations. `/describe` shows each operation's method, path, parameters,
request body, and an example with required fields. `/op` accepts a stable
method-and-path operation ID and JSON `path`, `query`, and `body` arguments.
`/api METHOD /v1/path [JSON]` remains the fallback for a mounted route missing
from OpenAPI. Both invocation paths use the same API key, project scope, and
server enforcement as direct HTTP clients.

The generated Community contract contains **981** HTTP operations: **968**
operator `/v1/` operations, each with a unique stable operation ID and a
JSON/path/query shape addressable by `/op`; **13** health, SAML, and SCIM
protocol routes remain outside the authenticated `/v1/` operator command.
The private contract has **1,022** operations: **1,009** `/v1/` operations,
of which **1,007** are `/op` addressable. The two remaining `/v1/` routes are
inbound webhook receivers that require request-signature headers and are not
operator actions. The private static contract has 529 older operations without
an `operationId`; the TUI generates a stable method-and-path alias for these
when the live API mounts them.

| Product area | Community `/v1/` operations | Examples discoverable with `/ops` |
| --- | ---: | --- |
| Core work and finance | 40 | Tasks, decisions, budget status/reservation, receipts |
| Work planning | 55 | Workflow definitions, mappings, roadmap and portfolio work |
| Delivery and runtime | 205 | Workflow runs, code tasks, deployments, PR and execution evidence |
| Delivery CI/CD | 20 | Pipeline and release controls |
| Agent Studio | 72 | Agent definitions, versions, validation, runs and skills |
| ProviderOps | 63 | Provider accounts, resources, ChangeSets and deployment targets |
| Model capacity | 20 | Entitlements and route decisions |
| Governance | 47 | Assurance, approvals, claims and evidence |
| Operations | 146 | Health, incidents, reliability and fleet controls |
| Knowledge | 20 | Artifacts, memory and search |
| Auth and identity | 21 | Sessions, members and identity controls |
| Cloud and commercial | 20 | Edition status and configured cloud connections |
| Goldeneye | 11 | Goldeneye service integration |
| Meta | 16 | Capability catalog and API coverage |
| Other classified routes | 212 | Projects, additional model and marketplace controls |
| **Total** | **968** | All mounted `/v1/` operations are searchable by method/path/summary |

The table uses the API catalog's current domain classifier. It groups 212
otherwise unclassified routes under “Other”; this is a navigation label, not
an absence of TUI coverage.

## Practical workflows

| Workflow | Discover | Invoke through the TUI | Governing server boundary |
| --- | --- | --- | --- |
| Pick a project and inspect work | `/project ID`, `/ops tasks` | `/op get_v1_tasks_339c8c60` | Project membership and task visibility |
| Create auditable work | `/describe post_v1_tasks_54d40d0b` | `/op post_v1_tasks_54d40d0b {"body":{"project_id":"ID","title":"Title","dedupe_key":"unique-key"}}` | Task creation, event and dedupe rules |
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

- `scripts/tests/test_metatron_tui.py` checks all 981 Community snapshot
  operations and verifies every `/v1/` operation has a unique, addressable
  terminal alias. It checks project selection, parameter validation, duplicate
  FastAPI IDs, server-authenticated requests, and catalog-only route reporting.
- `/coverage --json` (one-shot mode) enumerates every route from the *running*
  API's OpenAPI and catalog with `source`, `terminal_invokable`,
  `raw_api_invokable`, and `edition_policy_access`. The last field is
  `unverified_server_enforced` until an actual authorized call is made.
- A mounted route can still return an edition, role, budget, rollout, or
  approval denial. The TUI does not infer effective entitlements from OpenAPI.
- API, TUI, and MCP addressability is separate from proving every workflow on
  a clean installation. Live operator acceptance and any API behavior gaps
  remain separate release evidence.
