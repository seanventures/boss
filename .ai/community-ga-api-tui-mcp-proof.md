# Community GA API / TUI / MCP proof

Branch `ai/grok/ga-api-tui-mcp`, based on `371a1c72a1fd49f3336f5790b55407d51886412a`.
Worktree `/Volumes/DataSSD1/boss-community-ga-integration/swarms/api-tui-mcp`.
This file records what was regenerated from the mounted app and what the focused tests executed.
Plane was not updated: `OPS_BASE_URL`, `OPS_TOKEN`, and `PLANE_API_KEY` were unset, and no secret was fetched.
Nothing was pushed.

## Counts regenerated from the mounted app

| Claim | Value | How it was checked |
|---|---|---|
| Community HTTP operations | 986 | `scripts/generate_openapi_contract.py --edition community --check` printed 986. `scripts/tests/test_metatron_tui.py::test_live_community_app_schema_and_catalog_map_every_mounted_operator_route` printed `{"openapi": 986, "operator": 973}`. |
| Terminal-addressable `/v1` routes | 973 | Same live TUI test. Every mounted `/v1` route was addressable. |
| MCP API-policy | 853 | `docs/community/mcp-api-capability-matrix.json` `counts.api_policy_gated`, matched by `scripts/audit_mcp_api_parity.py --check-community` and `--check`. |
| Exact human confirmation | 38 | Matrix `human_confirmation_gated`. `docs/community/FEATURES.md` had said 36 and 30; that line now says 38 and 28, the numbers the auditor just matched. |
| Route-safety held | 28 | Matrix `operator_only_unavailable`. The string `route-safety-held` is not in the tree. Held means `trusted_operator_confirmation_unavailable`. |
| Internal / protocol excluded | 67 | Matrix `excluded`. |
| Enterprise mounted operations | 1027 | `--check` and `generate_openapi_contract.py --edition enterprise --check`. Paid-only rows were not added or enabled. |

No new route was mounted. Editing a schedule is optional `schedule_id` on the existing `POST /v1/delivery-workflows/schedules`. That POST stays `api_policy_gated`. `DELETE /v1/delivery-workflows/schedules/{schedule_id}` stays `human_confirmation_gated`.

## What changed

A schedule used to accept a body with no workflow inputs and fail later, when it fired. Create now refuses a published JSON Schema `required` field that is missing or blank. Name-to-type schemas still declare nothing required. Sending `schedule_id` on the same POST replaces the stored input (and the time zone, when sent) and returns 200. Cadence is not changed. Omitting `input` on an edit does not wipe stored values. An enabled schedule still requires `workflow_execute`. The audit event `workflow.schedule_inputs_updated` stores key names and the time zone, not the values. The Delivery console collects those fields and can edit them. The terminal and MCP already forward a typed JSON body, so they address this operation rather than a second control plane.

## Execution proof

Cwd: `/Volumes/DataSSD1/boss-community-ga-integration/swarms/api-tui-mcp`.
Interpreter: the checkout `.venv311` Python, with `PYTHONPATH=.:engine` and `BOSS_EDITION=community`.
Database: a private Postgres started for this worktree only (`boss-ga-api-tui-mcp-db`, database name `boss_test`, `127.0.0.1:55432`, data directory outside the worktree). Shared compose projects were not started or stopped. The API suite confines itself to a private schema. The URL was not committed.

```
pytest -q --tb=line \
  engine/tests/test_a_workflow_can_be_scheduled.py::test_a_schedule_refuses_declared_inputs_it_was_not_given \
  engine/tests/test_mcp_api_surface.py::test_schedule_input_edit_is_typed_session_bound_and_not_a_new_plane \
  scripts/tests/test_metatron_tui.py::test_schedule_edit_uses_typed_op_input_and_returns_the_api_result \
  scripts/tests/test_metatron_tui.py::test_live_community_app_schema_and_catalog_map_every_mounted_operator_route \
  apps/api/tests/test_operator_domain_execution.py
```

Result: 7 passed, 1 warning, exit 0, 62.02s.

```
apps/web: vitest run tests/your-workflows-have-their-own-tab.test.ts
```

Result: 11 passed, exit 0. The binary was the main checkout's vitest; cwd was this worktree's `apps/web`.

```
python scripts/audit_mcp_api_parity.py --check-community
python scripts/audit_mcp_api_parity.py --check
python scripts/generate_openapi_contract.py --edition community --check
python scripts/generate_openapi_contract.py --edition enterprise --check
```

Results: Community matrix current; `--check` reported 986 Community and 1027 Enterprise; both OpenAPI files current (986 and 1027).

The three operator paths in `apps/api/tests/test_operator_domain_execution.py` are authenticated (unauthenticated catalog is 401), discovered in `/v1/meta/api-catalog`, typed from the live OpenAPI body, executed, and audited:

- Core read `GET /v1/tasks` and write `POST /v1/tasks`. Result id starts with `tsk_`. Event `task.created`.
- Decision digest read `GET /v1/decisions/inbox` and write `POST /v1/decisions/digests` with no channel, so nothing is queued outbound. The catalog classifies `/v1/decisions` as domain `core`. Result status `created`, artifact id, `outbox_message_id` null. Events `decision.digest_created` and `artifact.created`.
- Delivery read `GET /v1/delivery-workflows/schedules` and write `POST /v1/delivery-workflows/schedules`. Missing `sectors` and `to` is 400. A complete input is 201 and `workflow.schedule_created` does not contain the input values. The same POST with `schedule_id` is 200, replaces the input, and `workflow.schedule_inputs_updated` records the keys. A member with only `workflow_manage` gets 403 while the schedule is enabled.

These three writes do not spend, so the record is the event (and the digest artifact). No budget reservation was added, and no spending hold was lifted.

MCP `find` / `describe` / `invoke` posts that schedule body through the existing operation, with session-bound `project_id` and `actor`, and the schedule list read returns ok. The terminal `/describe` shows the typed fields and `/op` POSTs the same path, inserting the selected project, and returns the API body.

## Held routes left held

All 28 `operator_only_unavailable` operations stay held. This change did not add authority, a budget reservation, a receipt, an approval, a lease, or a recovery path for any of them. Reason on every row: `trusted_operator_confirmation_unavailable`.

- DELETE `/v1/channels/{provider}`
- POST `/v1/code-tasks/{run_id}/deploy`
- POST `/v1/console/v2/autonomy`
- POST `/v1/console/v2/autonomy/apply`
- POST `/v1/decisions/{decision_id}/approve`
- POST `/v1/delivery/deployments/{deployment_run_id}/execute`
- POST `/v1/deployment-hosts/{host_id}/adopt-key`
- POST `/v1/deployment-hosts/{host_id}/install-key`
- POST `/v1/deployment-hosts/{host_id}/key`
- POST `/v1/deployment-hosts/{host_id}/provision`
- POST `/v1/deployment-hosts/{host_id}/retire`
- POST `/v1/deployment-runs/{deployment_run_id}/rollout/rollback`
- POST `/v1/deployment-targets/{target_id}/auto-deploy`
- POST `/v1/deployment-targets/{target_id}/bootstrap`
- POST `/v1/deployment-targets/{target_id}/purge`
- POST `/v1/deployment-targets/{target_id}/teardown`
- POST `/v1/deployments/{deployment_run_id}/promote`
- POST `/v1/discoveries/{run_id}/promote`
- POST `/v1/enterprise/identity-providers`
- POST `/v1/goldeneye/apps/deploy`
- POST `/v1/goldeneye/apps/{name}/rollback`
- POST `/v1/observability/stack/deploy`
- POST `/v1/orgs`
- DELETE `/v1/preview-sandboxes/{preview_id}`
- POST `/v1/previews/{preview_id}/approve`
- POST `/v1/roles`
- DELETE `/v1/sandboxes/{sandbox_id}`
- POST `/v1/worker-autoscaling/apply`

Worker and protocol operations stay in the 67 excluded rows. BOSS-451 spending holds were not lifted.

## Not claimed

Full engine, API, and web suites were not run. A clean-VM installer exercise was not run. Mapping of the other Community operations is still the matrix plus the live count test, not an execution of all 986 routes.
