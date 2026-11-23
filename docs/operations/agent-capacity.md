# Agent capacity on shared machines

Setup's Advanced settings includes **Agent capacity**. This is install-wide because
projects share workers and the same host. Total job slots (1–4) are independent
of the heavy-build limit (Auto or a ceiling). Until you save a value, job slots
follow the heavy-build limit, as before, so an upgrade does not raise concurrency. A 4-core / 8 GiB laptop
should start with three job slots and one heavy build.

Auto worker mode follows saved settings within a minute, without a restart. An explicit
`BOSS_WORKER_SLOTS` environment value remains an operator override. Reducing the
limit lets active jobs finish; extra slots stop leasing new work. This does not
kill agents, discard changes, or grant additional permissions.

The heavy-build admission controller still checks memory, CPU load, disk and
degradation before leasing. A numeric build limit is a ceiling, not permission to
exceed measured host capacity. When pressure eases, queued work becomes eligible
again. Parallel writing streams inside an individual build remain separately
controlled in Govern. More slots do not guarantee faster model responses.

Storage cleanup remains the Infra Agent's bounded, signed dry-run/execute flow.
On macOS with an explicit `BOSS_DOCKER_CONTEXT=colima-<profile>` (or `colima` for
the default profile), the helper measures that profile's disk images and uses
Colima SSH to trim unused filesystem blocks. It does not remove active files or
volumes. `COLIMA_HOME` supports a nonstandard Colima home. Docker Desktop retains
its existing trim path. Reported reclaimed bytes are measured from disk allocation
after trimming, not assumed from cache-prune output.

Disk warnings can remain after a successful cleanup: thresholds are independent
of the amount reclaimed. Cleanup is not permission to remove personal files,
live application data, or backups outside the existing retention policy.
