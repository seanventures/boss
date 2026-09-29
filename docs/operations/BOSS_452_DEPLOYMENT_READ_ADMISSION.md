# BOSS-452: direct deployment read admission

`GET /v1/code-tasks/{run_id}/deployments` admits at most four concurrent reads
per API process. A request above that limit receives HTTP 503 with
`Retry-After: 1` and `detail: "Deployment reads are busy; retry shortly."`
without checking out a database connection. An admitted request keeps the
existing project-member check, project-scoped joins, and response body.

The endpoint no longer performs a separate database reachability query before
its authorized read. Connection and pool errors return the existing
`Postgres is not reachable.` 503 detail; the admission slot is released on
success, denial, and failure. The bound is per process, so total admission
scales with the number of API processes.

This is a protective limit for the simultaneous 9/8-request bursts observed
on Goldeneye. The 30-second deployment failures may involve database pool
contention, but their root cause is unconfirmed without an application
traceback. An admitted request can still wait on unrelated database users or
on a slow query. The web batch bound in PR #284 handles the Factory caller;
this limit also covers direct API callers.

Focused database-backed tests cover concurrent overload and recovery,
cross-project denial, a connection failure, and the unchanged response
shape. No live service was changed by this patch.
