# Build sandbox

BOSS runs code it did not write: every build, test and agent step. This page explains how BOSS keeps that code away from the rest of the machine, and what to do on a host that cannot do it.

## How builds are isolated

- **Their own user.** Each build runs as its own unprivileged user (uid 61000–61999), never as BOSS itself. It can enter only its own directory.
- **A fenced network.** A build can reach only the BOSS API (port 8000) and boss-egress (ports 8090 and 8091). The database, the secret store, the host and the internet are refused.
- **No credentials.** A build holds no GitHub token or provider key. boss-egress adds them to each allowed request (see [Agents and access](AGENTS_AND_ACCESS.md#github-access-for-builds)).

To set up the fence, the api container starts as root with the `NET_ADMIN` capability, installs iptables rules that match the build users, then becomes the `boss` user for good. It can never become root again.

!!! note "`docker compose exec` is root by default"
    Because the container starts as root, pass `-u boss` to anything you run in it:
    `docker compose -f scripts/install/community-compose.yml --env-file .env exec -u boss api …`.
    `boss identity` does this for you.

## Settings

Set these in `.env`, then restart (`boss update`).

| Setting | Values | Meaning |
| --- | --- | --- |
| `BOSS_SANDBOX_NETWORK` | `enforce` (default) | Fence the build network. If this host cannot, start anyway and **hold every build** until it is fixed. |
| | `required` | Fence it, or refuse to start. |
| | `off` | No fence. Builds can reach this container's networks. The api log says so at every start. |
| `BOSS_SANDBOX_ISOLATION` | `required` (default) | A build never runs as BOSS's own user. |
| | `auto` | Isolate when possible; otherwise run unisolated, with a warning in the log. |
| | `off` | Never isolate. |

## A host that cannot fence builds

The fence needs iptables with the *owner* match. Some hosts do not have it: rootless Docker, gVisor, and some WSL2 and VPS kernels.

On such a host, with the default `enforce`:

1. BOSS still starts. The api log shows a boxed warning: *could not install the sandbox network rules … BOSS IS STARTING WITHOUT IT: builds are held until this is fixed.*
2. The console, people, workflows and everything else work.
3. Every build stops before it starts, as **blocked**, with *Sandbox unavailable: this host cannot fence the build sandbox's network …* and the fix.
4. `boss doctor` fails the **sandbox** check with the same fix.

Fix it one of three ways:

- **Fix the host**: use rootful Docker on a kernel with `xt_owner` (most Linux distributions), then restart BOSS.
- **Accept an unfenced network**: set `BOSS_SANDBOX_NETWORK=off` in `.env` and restart. Builds then reach this container's networks. Do this only on a machine where that is acceptable.
- **Move BOSS** to a host that can fence.

Nothing ever runs unfenced unless you choose `off`.

!!! warning "Docker Desktop on macOS"
    The fence on Docker Desktop for Mac is not yet verified. Run `boss doctor` after installing: it tells you if builds are held.

## A database for a build's tests

The sandbox has Node, Python and Playwright. By default, Build database is **Auto**: BOSS gives
an individual build a temporary PostgreSQL database when its brief, approved plan, template,
repository dependencies or verification manifest indicate PostgreSQL. A repository can declare
that requirement explicitly:

```yaml
# boss.verify.yaml at the repo root
services: [postgres]
```

In Repo and models, choose **Always** to give every build a database, or **Never** to disable
it for the project, including when the repository declares it. The project setting is
`build_services`: `auto` (default), `always` or `never`; the older `postgres` value means Always.

At run start, before any agent runs, BOSS creates a role and an empty database for that run on
the `build-services` Postgres (its own container on port 5433, data on tmpfs; never the BOSS
database). Every command the run starts, its tests included, gets `DATABASE_URL`
(`postgresql://user:pass@host:5433/db`) and `PGHOST PGPORT PGUSER PGPASSWORD PGDATABASE`. The
database is deleted when the run ends. If deletion fails, cleanup stays pending and is logged;
the resource reaper retries instead of recording a successful release. A failed ledger update
also stays pending for retry by the running API. If the API restarts after deletion but before
that retry succeeds, the unreleased ledger entry may require reconciliation. If the run's process
dies, the reaper removes its orphaned database after a grace period when the server is reachable.
This data is temporary: restarting the build-services container loses it. It is neither the
application's deployed database nor part of the durable BOSS recovery backup.

What keeps one run out of another's database:

- each run has its own role and a random password, held only in BOSS's memory and in that run's
  processes. A different run's uid cannot read them;
- `CONNECT` on each database is revoked from everyone except its own role, and the server's
  `pg_hba` (`sameuser`) lets a role log in only to the database named like it;
- the role cannot create databases or roles, cannot read password hashes, and cannot become
  another role;
- the sandbox network fence opens only the build-services port, so the BOSS database stays
  unreachable from a build.

The password never appears in run logs, events or activity. If the run asked for Postgres and the
server is not running, the run stops with `Build services unavailable: <why>`. It does not run
the tests without the database. Automatic repository detection refuses unsafe symlink, hardlink
or replacement reads; it never follows them outside the repository. `BOSS_BUILD_SERVICES_PG_URL`
points BOSS at another server (a
superuser URL; it is never passed to a build).

## Upgrades

If a new release cannot start on your host, `boss upgrade` puts the previous release back and starts it again. See [Upgrade to named accounts](IDENTITY_UPGRADE.md).

## The egress log

boss-egress records every request a build makes, allowed or denied: when, which run, which provider, which path pattern and the result. It never records bodies or credentials. The log is split by day and kept for 30 days. Denials also go to the security audit log.
