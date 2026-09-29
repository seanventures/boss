# Community GA database proof

Branch `ai/grok/ga-db-proof`, base `46c10ca957e8d4f01403c0ff6c6c290be44ae156`.
Worktree `/Volumes/DataSSD1/boss-community-ga-integration/swarms/db-proof`.
Nothing was pushed. The 28 held routes and BOSS-451 were not changed. No product code changed: both suites passed on the base commit.

Plane was not updated: `OPS_BASE_URL`, `OPS_TOKEN`, and `PLANE_API_KEY` were unset, and no secret was fetched.

## Database

Private container `boss-ga-db-proof` only. Published `127.0.0.1:55442` to container port 5432. Database name `boss_test`. User `boss`. Host port 5432 and 55441 were not used. Other containers were not stopped.

Schema: `db/schema.sql` through `boss_engine.db.ensure_schema` (`698/698` statements). File hash `ea1df8cef97184a01a02dab981f8f9fc1de65e65`, matching this worktree. `public` then had 255 tables. Org `org_boss`, workspace `ws_boss`, and project `boss-dogfood` were inserted so workflow foreign keys resolve. The API suite does not use that `public` schema: `apps/api/tests/conftest.py` confines the run to a private schema, calls `ensure_schema` again, and seeds it.

URL host and port only: `127.0.0.1:55442`. The password is not recorded.

## Commands

Cwd: `/Volumes/DataSSD1/boss-community-ga-integration/swarms/db-proof`.
Interpreter: `/Volumes/DataSSD1/boss-community-ga-integration/boss/.venv311/bin/python` (pytest 8.4.1).
`PYTHONPATH=.:engine`, `BOSS_EDITION=community`, `DATABASE_URL` pointed at the database above.

```
python -m pytest engine/tests/test_a_workflow_can_be_scheduled.py -ra --tb=short
```

Result: `21 passed in 0.55s`, exit 0. Collected 21, skipped 0. The five database tests executed (`@pytest.mark.skipif` on `DATABASE_URL` did not fire):

- `test_a_due_schedule_starts_a_run_and_moves_on`
- `test_a_schedule_that_is_not_due_does_not_run`
- `test_the_same_due_moment_starts_one_run_not_two`
- `test_a_schedule_pointing_at_a_workflow_with_no_version_is_refused`
- `test_resuming_a_paused_schedule_does_not_fire_it_immediately`

The other 16 are the pure schedule-arithmetic cases in the same file, including five parametrized `test_a_bad_field_is_named` cases.

```
python -m pytest apps/api/tests/test_operator_domain_execution.py -ra --tb=short
```

Result: `3 passed, 1 warning in 38.75s`, exit 0. Collected 3, skipped 0. The warning is Starlette's `anyio.abc.BlockingPortal` deprecation. The three tests executed:

- `test_core_task_is_discovered_typed_written_and_audited`
- `test_governance_digest_is_discovered_typed_written_and_audited`
- `test_delivery_schedule_requires_inputs_and_edit_is_audited`

No assertion was weakened. No fix was required.
