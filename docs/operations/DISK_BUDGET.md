# The disk budget of a running install (BOSSAPP-1459)

An enterprise install reached about 47 GB. Nothing said where the space had gone, and most of it
grew by itself while BOSS ran: a second image set kept for `boss rollback` for ever, test data on
an install that runs no tests, hourly backups with no size cap, sandboxes of runs that died,
Postgres volumes left by test containers, and a table that kept its size after its rows went.

This page is the budget: what each part of the disk is for, the line it must stay under, and what
keeps it there. The numbers live in one place, `engine/boss_engine/services/disk_budget.py`
(`CATEGORIES`); `boss doctor` and the Storage card in **Govern › Storage** read them and warn at
80 % of a budget, before it is reached.

```
boss doctor                      # the "disk:" lines: biggest categories, and a warning per category near or over
python3 engine/boss_engine/services/disk_budget.py budget            # the table below
python3 engine/boss_engine/services/disk_budget.py report --install-dir ~/boss --json
```

## The budget

A typical install: one to three projects, used daily. The budget is the warn line, not a target.

| Category | Budget | What keeps it there |
|---|---|---|
| BOSS images | 10 GB | One image set per release; old tags are pruned after every upgrade. The image size itself is BOSSAPP-1458's budget. |
| Rollback images | 10 GB, for 72 h | One previous set only (the next upgrade replaces it). Dropped once the upgrade has run `BOSS_ROLLBACK_KEEP_HOURS` (72) with a healthy API. |
| Build cache | 10 GB | `BOSS_BUILD_CACHE_CAP_GB`, applied by every prune. Counts what Docker can free (`docker system df` Reclaimable); cache layers installed images still use are theirs, shown as a note with nothing to run. Every prune prints Docker's before/after. Beyond Docker's plain capped prune, BOSS clears cache only on a Docker that runs only BOSS (`BOSS_DEDICATED_HOST=1`) or in its own buildx builder (`BOSS_BUILDX_BUILDER`), and fails there if it is still over the cap; on a shared Docker it prints the reading and the command and leaves it to you. |
| Database: records | 3 GB | The live records. Deleting a project takes its rows. |
| Database: knowledge graph | 3 GB | One graph per project, rebuilt in place; deleting a project drops it. Bloated tables are compacted hourly. About 1 GB per 100,000 indexed nodes. |
| Database: telemetry | 2 GB | Per-table retention, hourly (`telemetry_retention.POLICIES` and `RETENTION_OWNERS`). |
| Test databases | 0 (8 GB with a test Postgres) | Only `boss dev up` makes a test Postgres. Without one, test databases and its volume are leftovers BOSS removes. |
| Backups | 5 GB | Tiered retention, then a count and size cap (`BOSS_BACKUP_MAX_COUNT` 12, `BOSS_BACKUP_SCHEDULED_CAP_GB` 5). |
| Sandboxes | 3 GB | A run's directory goes when the run ends; one a dead run left is reaped after 24 h. |
| Other BOSS volumes | 5 GB | Sign-in homes, the toolchain cache (rebuildable), artifacts, repositories. |
| Unused volumes | 1 GB | Every container BOSS removes takes its anonymous volumes with it (`docker rm -v`). |
| Containers and logs | 2 GB | Compose caps each log (50 MB × 3). |

Steady state for a typical install: a fresh install on the enterprise lane (30 GB VM, 2026-11-24,
below) took **11 GB** in all (images, volumes, an empty database) with no build cache left. Daily
use then adds the database (2–4 GB), backups (~2.5 GB: twelve dumps of about 200 MB), sandboxes
(under 1 GB between builds), other volumes (~3.5 GB) and up to `BOSS_BUILD_CACHE_CAP_GB` (10) of
build cache on a large disk (none kept on a small one): **about 20–30 GB**, plus the rollback
images (~7 GB) for the 72 hours after an upgrade.

## The images and the first install (BOSSAPP-1458)

The Python services were one fat image (every service carried the coding CLIs, compilers, the
browser and the build toolchains). They are now one family of stages: egress and mcp-tools carry
only what they run, and every image shares the layers below it, so a shared layer is stored once.
`scripts/install/image_budget.py` holds each image to a budget (uncompressed, the sum of its
layers); the enterprise lane fails its install step when one is over.

Measured on the enterprise lane (arm64), Docker 29 with the containerd image store. "On disk" is
Docker's own per-image figure (unpacked plus compressed, shared layers counted in every image);
"uncompressed" is the image's own layers.

| Image | Before, on disk | After, on disk | After, uncompressed | Budget |
|---|---|---|---|---|
| api, worker, runner | 6.84 GB | 5.55 GB | 4.0 GB (shared by all three) | 4.4 GB |
| egress (and the model gateway) | 6.84 GB | 1.38 GB | 1.0 GB, ~0.65 GB once the Agent SDK left it | 0.9 GB |
| mcp-tools | 6.84 GB | 2.28 GB | 1.7 GB, ~1.45 GB once the Agent SDK left it | 1.8 GB |
| web | 2.68 GB | 2.68 GB | 2.3 GB | 2.5 GB |

The first install: 24 GB free before, **9.1 GB at the lowest** during the build, 13 GB free after,
0 B of build cache kept. The peak is the images plus ONE image family's build cache: the installer
builds each image once, builds a family back to back, and on a disk under `BOSS_DISK_FLOOR_GB` (20)
free drops the finished family's cache before the next (`prune.sh --room --cache-only`). So a first
install needs **21 GB** free on Docker's disk (the 15 GB peak plus the 6 GB the e2e gate needs
after it); the installer says so before it builds (`BOSS_INSTALL_NEEDS_GB`). Before the fix it ran
a 30 GB box out of disk: BuildKit's own garbage collector dropped the Python cache while the console
built, and the worker image was then built a second time, unshared.

## Measured on the laptop install, 2026-11-24

Before the fix, and what each change frees there. Image sizes count each layer once (Docker's own
list counts a shared layer once per image, which is where "~30 GB of rollback images" came from).

| Category | Found | Cause | After |
|---|---|---|---|
| Rollback images | 6.8 GB real (30 GB as listed) | `prev-<tag>` kept for ever | 0 once the upgrade is 72 h old and healthy |
| Test Postgres volume | 4.8 GB | none — this install runs `db-test`, so it is kept (a dev install) | removed on an install with no test Postgres |
| Test databases in `db` | 0.22 GB (7 databases) | `scripts/test-tier.sh` defaulted to the install's own Postgres | dropped when no test Postgres runs; test-tier now uses `db-test` |
| Scheduled backups | 6.1 GB (13 dumps) | tiers only, no byte cap | ≤ 5 GB, ≤ 12 dumps (frees ~1.2 GB) |
| `kg_vec_1536` | 924 MB for 1,599 rows | every embedding write rewrote unchanged vectors | ~15 MB after compaction; unchanged vectors are no longer rewritten |
| Anonymous volumes | 6.0 GB (27 volumes) | `scripts/e2e-gate.sh` and the e2e harness removed their Postgres containers without `-v` | none from new runs (removed with `-v`, labelled) |
| Sandboxes | 2.9 GB (13 directories, idle since 18–22 Nov) | runs that died before releasing them | reaped after 24 h idle |

The 27 anonymous volumes already on the disk carry no label, so BOSS does not remove them (it
never touches a volume it cannot prove it made). `boss doctor` names them with the command to
review them: `docker volume ls --filter dangling=true`.

### Why `events_legacy` still holds data

`events` became a monthly-partitioned table online (ST-8): the old table was renamed
`events_legacy` and attached as the partition for **everything before the cutover month** — on the
laptop, before 2026-12-01. So `events_legacy` is not an old copy; it holds every event of October
and November, which is live. Chatter in it is deleted row by row (30 / 14 days); everything else is
audit evidence kept 90 days. Once its upper bound (1 December) is older than the 90-day window, at
the start of March 2027, the retention pass copies its non-chatter rows to `events_archive` and
drops the partition in one step. It cannot be dropped earlier without dropping live events, and it
needs no migration.

### Do the knowledge graph tables keep stale rebuilds?

No. Each project has exactly one AGE graph (`kg_<hash of the project id>`), rebuilt in place, and
every one of the 29 graphs on the laptop belongs to a live project; `kg_view_tree` and
`kg_view_edges` hold no rows of deleted projects. The knowledge tables are large because the same
big repositories are indexed in several projects (seven projects of ~51,000 nodes each). That is
live knowledge, so nothing deletes it: the budget warns, and deleting a project drops its graph.
The one stale part was `kg_vec_1536`'s dead space, which the compaction pass returns.

## The rules

* **One rollback set.** `upgrade.sh` keeps the running images as `prev-<tag>` (replacing the
  previous set). `prune.sh` (after every upgrade and update, and `boss prune`) and the storage
  cleanup drop it once `.boss-rollback` is `BOSS_ROLLBACK_KEEP_HOURS` old and the API is healthy,
  and write `BOSS_PREV_EXPIRED_AT`. `boss rollback` then refuses and names `boss upgrade --to REF`.
* **Test data only with a test Postgres.** A `db-test` container (running or stopped) keeps its
  volume and the test databases; without one, the steward (`--test-leftovers`, run by every upgrade and `boss prune`) removes the volume (checked against its
  Compose labels) and the API's reaper drops `boss_test*` / `boss_scratch*` databases nobody is
  connected to. A BOSS whose own database is a test one never drops anything.
* **Backups past the cap.** With an approved `backups` retention rule the oldest go at once.
  Without one BOSS raises one storage decision (`storage_delete`): on a production install a person
  answers it, once, and the ticked rule makes it automatic; on a dev install BOSS's dev autonomy
  answers it. The newest three always stay.
* **Every append-only table has an owner.** `telemetry_retention.RETENTION_OWNERS` names, for
  every table that only grows, the retention that bounds it or why it is kept (audit, finance,
  people's conversations). A test reads `db/schema.sql` and fails on one that is missing.
* **Compaction.** A public table of 256 MB or more whose counted live data is under 10 % of its
  file and at most 50,000 rows is rewritten (`VACUUM FULL`, lock wait 2 s) by the hourly pass.
* **Label-scoped only.** Containers BOSS removes take their anonymous volumes (`rm -v`);
  sandboxes are removed only when their uid holds no lease, runs no process, belongs to no live
  owner and nothing changed for 24 h. Nothing unlabelled is ever removed.
