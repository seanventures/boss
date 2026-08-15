# 10-agent slice — global activity log (BOSS-379)

Central, searchable, sortable ops timeline. Do **not** invent a second event
store. SoR remains `events` (+ read-only projections of process steps, jobs,
deadletters, receipts). Never return secret material.

## Plane

BOSS-379 In Progress `2210a144-3871-4d19-b568-4c4fec358045`

Do not close 337/376/377/378. No deploy. Not GA.

## Existing (extend, do not replace)

- `events` table + `emit_event` + `GET /v1/events` (limit/offset only)
- Thin Task Board list in `page.tsx` (keep it)
- `apps/web/lib/events.ts` `fetchActivityFeed`

## File ownership

1. `classify` — `engine/boss_engine/services/activity_classify.py` + `engine/tests/test_activity_classify.py`
2. `service` — `engine/boss_engine/services/activity_log.py`
3. `service-tests` — `engine/tests/test_activity_log.py`
4. `api` — `apps/api/app/routers/activity.py` + append mount in `platform.py` only
5. `api-tests` — `apps/api/tests/test_activity_api.py`
6. `ui` — `apps/web/app/activity-log-ui.tsx`
7. `ui-lib` — `apps/web/app/activity-log-actions.ts` + `apps/web/lib/activity-log.ts`
8. `nav` — ConsoleShell area `activity`, `page.tsx` mount `data-area=activity`, `globals.css` visibility, `EXISTING_AREA_IDS`
9. `web-tests` — `apps/web/tests/activity-log.test.ts`
10. `docs-sweep` — FEATURES.md ◐ row, `scripts/qa/feature_sweep.py` GET `/v1/activity` optional probe, Plane comment only

## Contract

`GET /v1/activity?project_id=&q=&kind=&status=&actor=&entity_type=&since=&until=&sort=created_at|status|kind&order=desc|asc&limit=&offset=`

Item:
```
id, source (event|process_step|job|deadletter|receipt),
kind, status (ok|failed|waiting|stuck|info),
title, actor, entity_type, entity_id,
stage, created_at, data (redacted)
```

Statuses from names/types only — never invent a success. Missing payload stays empty.

UI: search box, status/kind filters, sortable columns, click row → redacted detail.
Empty / error / loading honest. `data-testid="activity-log"`.
