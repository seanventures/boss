# Build sandbox

BOSS runs code it did not write: every build, test and agent step. This page explains how BOSS keeps that code away from the rest of the machine, and what to do on a host that cannot do it.

## How builds are isolated

- **Their own user.** Each build runs as its own unprivileged user (uid 61000–61999), never as BOSS itself. It can enter only its own directory.
- **A fenced network.** A build can reach only the BOSS API (port 8000) and boss-egress (ports 8090 and 8091). The database, the secret store, the host and the internet are refused.
- **Previews too.** An app preview runs the same way: its own user for as long as the preview lives, its files in its own directory, none of BOSS's environment, and the same fence. It may also reach its own preview port, never another preview's.
- **No credentials.** A build holds no GitHub token or provider key. boss-egress adds them to each allowed request (see [Agents and access](AGENTS_AND_ACCESS.md#github-access-for-builds)).

To set up the fence, the api container starts as root with the `NET_ADMIN` capability, installs IPv4 and IPv6 rules that match the build users, then becomes the `boss` user for good. It can never become root again. IPv6 rules may be skipped only when the kernel cannot create an IPv6 socket. Missing tools, unreadable rules or a failed rule installation hold builds; an empty IPv6 address list does not establish that the network is fenced.

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

## A skill's command runs in the same fence

An authored skill of kind `sandboxed_command` (a linter, `ffmpeg`, a validator such as `gwlint validate tenant.yaml`) used to run only on a microVM substrate. On a `runtime: local` install it can now run in the fence described above, under the **opt-in** provider `local_fenced`. Turn it on in `.env` and restart (`boss update`):

```
BOSS_GOVERNED_SANDBOX_PROVIDER=local_fenced
```

It needs the fence up: `BOSS_SANDBOX_ISOLATION` not `off` and `BOSS_SANDBOX_NETWORK` not `off`. Then a command gets:

- its own pool user and its own directory for the one call, gone when the command ends;
- no model credentials: no copy of the Claude, Codex or Grok login, and an environment of `PATH` and the locale only (plus its own `HOME` and the egress proxy);
- the command's current directory is that workdir, and a step's `files` inputs (the YAML it was handed) are written there — the input's placeholder in the command becomes the file's path;
- the same network rules as a build: loopback and the egress relays only; no `DATABASE_URL`, no provider keys in its environment;
- a bounded timeout and output capped as it is read; the declared outputs it produced are kept as artifacts (`produced_artifacts` on the step's output) so a `Deploy config` step can ship exactly those bytes. An output is kept only if it is a regular file the command's own user wrote inside its workdir: a symlink (to `/proc/self/environ`, say), a symlinked directory or a hard link is refused and nothing is stored.

This is **not** strong isolation: the command shares the control plane's kernel. Every run says so (`isolation: {provider: local_fenced, strong: false}`), it never counts towards exposing a write-capable agent publicly, and an install marked `BOSS_DEPLOYMENT_TRUST=untrusted` refuses it — such a command then needs a microVM substrate (`BOSS_GOVERNED_SANDBOX_PROVIDER=microvm`). With `BOSS_GOVERNED_SANDBOX_PROVIDER=local` the old refusal stands.

| Setting | Values | Meaning |
| --- | --- | --- |
| `BOSS_GOVERNED_SANDBOX_PROVIDER` | `local` (default) | A skill's command is refused (it would run as BOSS's own user). Builds are unchanged. |
| | `local_fenced` | A skill's command runs in the worker's per-run fence (needs `BOSS_SANDBOX_ISOLATION` and `BOSS_SANDBOX_NETWORK` not `off`). |
| | `microvm` | A skill's command runs on the sandbox host (strong isolation). |
| `BOSS_DEPLOYMENT_TRUST` | `trusted` (default) | Your own projects on your own machine. |
| | `untrusted` | Commands run only under strong isolation; `local_fenced` is refused. |
| `BOSS_LOCAL_FENCED_BINARIES` | empty (default) | Worker directories a skill may run a CLI from under `local_fenced` (comma-separated absolute paths). |

### Providing a CLI such as `gwlint` under `local_fenced`

A `sandboxed_command` skill must pin an image by digest, but under `local_fenced` **the image is not what runs**: the command runs on the worker's own filesystem. So the CLI has to be on the worker, and BOSS checks that it is the one you meant:

1. Build the CLI into a directory tree (for `gwlint`: `npm ci --omit=dev`) and mount it **read-only** on the worker, e.g. `/opt/northgate/gwlint`. Publish the same tree as the image the skill pins, so a microVM install runs the same bytes.
2. Allow its root in `.env`, then `boss update` (a person sets this; an agent cannot):
   ```
   BOSS_LOCAL_FENCED_BINARIES=/opt/northgate
   ```
3. Declare what the skill runs in its config: `"local_binaries": ["/opt/northgate/gwlint"]` (a directory or a file). Its directory, and its `bin/` if there is one, go first on the command's `PATH`, so the command can say `gwlint validate *.yaml`.

Before the command runs, every declared path must resolve (symlinks followed) under an allowed root, exist, and not be writable by every user; otherwise the step fails with the one thing to fix. The run records `image_used: false` and `local_binaries` (each path, and a file's sha256), so the evidence says which tool ran. A skill that declares nothing runs with the worker's base tools only (`sh`, `diff`, …) and still records `image_used: false`.

We chose an allow-list over refusing every image-pinned skill under `local_fenced` (every `sandboxed_command` skill pins an image, so refusing would switch the provider off) and over checking a whole-tree manifest (heavier, and a read-only mount under an operator-chosen root already stops a run from swapping the tool).

## A host that cannot fence builds

The fence needs iptables with the *owner* match, including ip6tables when the kernel supports IPv6. Some hosts do not have it: rootless Docker, gVisor, and some WSL2 and VPS kernels.

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
the resource reaper retries instead of recording a successful release. If deletion succeeds but
the ledger update fails, an existing open ledger row can be retried after an API restart. The
reaper closes that specific row only after confirming that both its database and role are absent
on the same configured server, its owner and ancestors are no longer live, and the grace period
has elapsed. It checks bounded pages and resumes on later passes, so older held rows do not
permanently block later entries. Rows alone never authorize deletion. Uncertain ownership,
changed server or prefix, unavailable checks and a remaining object stay held. Ledger recording
is best effort: an unwritten row cannot provide this restart recovery evidence. If the run's
process dies while its database remains, the separate catalog ownership sweep removes its
orphan after a grace period when the server is reachable.
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
