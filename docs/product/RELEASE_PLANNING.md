# Release planning & project delivery board

Unified **project delivery board** for Mission Control: what is built, what is
to be built, and release milestones with tracker issue links.

## Goals

1. **Built** — feature catalog entries that are documented/implemented, plus
   recent successful deployments.
2. **To be built** — open tasks, ranked work items, undocument/planned features.
3. **Release plan** — named milestones with target dates, linked board items,
   and issue-tracker URLs (Plane, Jira, Linear, GitHub, Azure DevOps).
4. **Status snapshot** — compact counts for Mission Control KPI strips.

## API (`/v1/release-planning`)

| Method | Path | Purpose |
|--------|------|---------|
| GET | `/status?project_id=` | `built_count`, `todo_count`, `in_progress`, `blocked`, `last_release` |
| GET | `/board?project_id=` | Columns: `built`, `building`, `planned`, `blocked` |
| POST | `/releases` | Create release plan `{name, target_date, item_ids[], actor}` |
| GET | `/releases?project_id=` | List release plans |
| GET | `/releases/{id}?project_id=` | Release detail (+ hydrated items) |
| POST | `/releases/{id}/link-issue` | `{tracker, issue_key, url?, actor}` |
| POST | `/sync-from-features` | Pull FeatureCatalog into durable board; apply [v1 feature lock](./V1_FEATURE_LOCK.md) metadata (does not rewrite the catalog; Cloud/Ent never Community shipped) |
| POST | `/sync-from-trackers` | Best-effort work tracker bindings (fail soft) |

All mutations require an **actor** with `command_submit` permission.

### Status snapshot shape

```json
{
  "project_id": "boss-dogfood",
  "built_count": 12,
  "todo_count": 8,
  "in_progress": 3,
  "blocked": 1,
  "last_release": { "id": "rel_…", "name": "v1.2", "target_date": "2026-09-01", "…" : "…" },
  "last_deployment": { "id": "…", "status": "succeeded", "environment": "staging" },
  "totals": { "board_items": 24, "release_plans": 2 }
}
```

### Board columns

| Column | Sources |
|--------|---------|
| `built` | Feature status `Implemented`; work/task done/shipped; healthy deploys |
| `building` | Feature `Partial`; active/allocated work; running deploys |
| `planned` | Feature `Planned`; open/todo work and tasks |
| `blocked` | Feature `Blocked`; blocked/failed work or deploys |

Before the first sync, `GET /board` returns a **live** composition from
FeatureCatalog, work items, open tasks, and recent deployment rollouts
(no durable writes). After `sync-from-features` or `sync-from-trackers`,
rows live in `release_board_items` and subsequent board reads prefer that store.

### Release plans

```json
POST /v1/release-planning/releases
{
  "project_id": "boss-dogfood",
  "name": "v1.2 Mission Control",
  "target_date": "2026-09-01",
  "item_ids": ["rbi_…", "feat_…"],
  "actor": "ceo"
}
```

Link a tracker issue (URL optional — Plane templates are filled from settings):

```json
POST /v1/release-planning/releases/{id}/link-issue
{
  "project_id": "boss-dogfood",
  "tracker": "plane",
  "issue_key": "BOSS-420",
  "url": null,
  "actor": "ceo"
}
```

Issue links are upserted by `(tracker, issue_key)`. Supported tracker keys:
`plane`, `jira`, `linear`, `github`, `ado` (and any free-form string).

## Durable schema

Tables (also created lazily with `IF NOT EXISTS` on first service use):

* `release_plans` — milestone name, target date, `item_ids`, `issue_links` JSONB
* `release_board_items` — board rows keyed by `(project_id, source_type, source_id)`

See `db/schema.sql`.

## Service layout

* Engine: `engine/boss_engine/services/release_planning.py`
* V1 lock hook: `engine/boss_engine/services/v1_feature_lock.py` reads
  `docs/product/V1_FEATURE_LOCK.md` during `sync-from-features` (and the live
  board). Catalog rows are unchanged. `cloud_enterprise` rows never get
  `community_shipped=true` or column `built`.
* Router: `apps/api/app/routers/release_planning.py` (mounted from
  `mount_platform_routers`)
* Reuses: `FeatureCatalog`, `WorkOrchestrationService` (work items + tracker
  bindings)
* Events: `release_planning.release_created`,
  `release_planning.issue_linked`,
  `release_planning.synced_features`,
  `release_planning.synced_trackers`

## Web

* Client: `apps/web/lib/release-planning.ts`
* UI: `apps/web/app/release-planning-ui.tsx`
  (`ReleaseStatusPanel`, `ReleaseBoardPanel`, `ReleasePlansList`,
  `ReleasePlanningPanel`)

## Fail-soft tracker sync

`POST /sync-from-trackers` always returns HTTP 200 with `"ok": true` when the
actor is authorized. Per-binding and list failures are collected under
`errors[]` so Mission Control never hard-fails on a flaky tracker adapter.

## Tests

```bash
pytest apps/api/tests/test_release_planning_api.py -q
```

## Related

* Feature catalog: `GET /v1/features`, `docs/product/TRACEABILITY.md`
* Work orchestration: `docs/product/WORK_ORCHESTRATION_AND_DELIVERY_PRESENCE.md`
* Work trackers: `docs/product/WORK_TRACKER_PROVIDERS.md`
