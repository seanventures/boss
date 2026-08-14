# On-call / SRE control plane

BOSS-native on-call automation inspired by PagerDuty, Opsgenie, and Grafana
OnCall — without requiring paid provider APIs for tests or local dogfood.

## Surface

| Path | Purpose |
|------|---------|
| `GET/POST /v1/oncall-sre/schedules` | Rotation schedules (layers + participants) |
| `GET/POST /v1/oncall-sre/policies` | Escalation policies (wait → notify channels) |
| `POST /v1/oncall-sre/pages` | Open a synthetic or reliability-linked page |
| `POST /v1/oncall-sre/incidents/{id}/page` | Bridge a reliability incident → page |
| `POST /v1/oncall-sre/incidents/{id}/ack` | Acknowledge |
| `POST /v1/oncall-sre/incidents/{id}/resolve` | Resolve |
| `GET /v1/oncall-sre/status?project_id=` | Who is on-call, open pages, adapters |
| `POST /v1/oncall-sre/from-reliability` | Page all open reliability incidents |
| `POST /v1/oncall-sre/graph/link` | Link page/incident to knowledge-graph node |
| `GET /v1/oncall-sre/adapters` | Adapter catalog + live capability |

Distinct from RB-20 conversational cards at `/v1/oncall/notify|preview`.

## Concepts

### Schedules

A schedule has one or more **layers**. Each layer lists participants and a
handoff interval (`handoff_seconds`). Current on-call is computed
deterministically from `start_at` + elapsed time.

### Escalation policies

Ordered **steps**: `wait_minutes` then one or more **channels**
(`adapter` + `target`). Steps are stored and used to pick the first-step
adapter when paging; timed multi-step escalation workers can consume the same
JSON later.

### Pages

A page is the on-call unit of work:

- Optional `reliability_incident_id` bridge into the reliability pipeline
- `dedupe_key` uniqueness per project (no duplicate effects)
- Status: `open` → `acked` → `resolved`
- Assignee from the schedule’s current holder
- `notify_result` with receipt id and `simulated` flag

### Knowledge graph

Opening a page upserts an `incident` KG node (and the service node when known)
with a `correlates_with` edge. `POST /graph/link` attaches additional nodes
(root-cause, resource, etc.) via `KnowledgeGraphService`.

## Adapters

| Adapter | Live credential env | Fallback |
|---------|---------------------|----------|
| `pagerduty` | `PAGERDUTY_TOKEN` | — |
| `opsgenie` | `OPSGENIE_API_KEY` | — |
| `grafana_oncall` | `GRAFANA_ONCALL_WEBHOOK` | `RELIABILITY_ONCALL_WEBHOOK` |
| `slack_webhook` | `ONCALL_SLACK_WEBHOOK` | `RELIABILITY_ONCALL_WEBHOOK` |
| `mattermost` | `ONCALL_MATTERMOST_WEBHOOK` | `RELIABILITY_ONCALL_WEBHOOK` |

- **Default**: no token → dry-run, receipt `succeeded` with
  `simulated=true`, `posted=false`. Safe for CI.
- **`force_live=true` without credentials**: fail closed **HTTP 422** with
  `simulated=true` and a receipt marked failed.
- **With token**: HTTP delivery; receipt succeeds or fails on outcome.

Every notify attempt uses `ReceiptService.create_or_reuse` with effect kind
`oncall_sre.notify` and a stable `dedupe_key`.

## Persistence

Tables (also created via service `ensure_tables` with `IF NOT EXISTS`):

- `oncall_schedules`
- `oncall_policies`
- `oncall_pages`

## UI

- `apps/web/lib/oncall-sre.ts` — typed client helpers
- `apps/web/app/oncall-sre-ui.tsx` — status board (on-call holders, pages, adapters)

## Hard rules (BOSS)

- No silent external effects: every notify is receipted
- No paid PD/Opsgenie dependency for green tests
- Fail closed when forced live without credentials
- Domain logic in `engine/boss_engine/services/oncall_sre.py`; router stays thin
