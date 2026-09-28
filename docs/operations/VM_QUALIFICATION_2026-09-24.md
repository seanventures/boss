# Community VM installer qualification — 2026-09-24

**Scope:** disposable Ubuntu 24.04.4 and Debian 12 ARM64 VMs, each with 6 vCPU,
12 GiB assigned RAM, 100 GiB disk, Lima/VZ, and no host mounts. This is an
isolated development smoke, not a public-release or provider-subscription
qualification. x86-64 remains untested in this run.

## Source and exact entry point

The intended `https://raw.githubusercontent.com/seanventures/boss/main/install.sh`
URL was unavailable because the public repository has not been published. The
VM had no GitHub credentials. A local Git bundle containing the exact installer
branches stood in for the source URL, so the tested entry point still exercised
the **piped** `bash -s` path and its clone/update logic:

```bash
# Run on the development host after copying the branch bundle to the VM.
LIMA_HOME=/Volumes/DataSSD1/test/boss-vm-qa-lima limactl shell boss-vm-qa -- \
  env -u OPENAI_API_KEY -u ANTHROPIC_API_KEY -u BOSS_GITHUB_TOKEN -u XAI_API_KEY \
  BOSS_REPO_URL=/home/sean.guest/boss-vm-qa.bundle \
  BOSS_REPO_REF=ai/codex/community-vm-installer \
  bash -s -- --dir /home/sean.guest/boss --yes --skip-model-key \
  --install-coding-clis < install.sh
```

The first run installed Docker Engine 29.8.1, Compose 5.5.1, Buildx 0.37.1,
ensured Git, curl, OpenSSL and Python 3, and installed the three official coding CLIs. The API, worker,
web and Postgres images built and started. API and Postgres health passed, one
project was seeded, `/v1/edition` reported Community, and web `/` returned 200.
API, web and Postgres ports published on `127.0.0.1` only. The VM had about
75 GiB free after the builds and recovery drill.

Host CLI versions: Claude Code 2.1.281, Codex CLI 0.156.1, Grok Build 1.0.41.
The API container ran those same versions as its `boss` user. Existing binaries
were recognized on reinstall. No Claude, Codex or Grok subscription login was
performed or proved. In particular, Grok's container session file was absent;
a binary version check is not a subscription check. The VM cannot see a laptop
login. A real operator must authenticate under the runtime user, including
`docker compose ... exec -u boss api grok login --device-auth` for Grok.

## Upgrade and lifecycle drill

The original piped install reached the database seed but returned before final
checks because a child `docker compose exec -T` read the installer's remaining
stdin. The first piped upgrade similarly stopped after backup. Redirecting
child stdin from `/dev/null` fixed both; regression tests now assert the final
markers. The upgrade also learned to fetch a new `backup.sh` helper before
upgrading an older checkout that did not contain it.

```bash
LIMA_HOME=/Volumes/DataSSD1/test/boss-vm-qa-lima limactl shell boss-vm-qa -- \
  bash -s -- --dir /home/sean.guest/boss \
  --ref ai/codex/community-installer-lifecycle \
  < scripts/install/upgrade.sh
```

The corrected upgrade fast-forwarded `f4664b58` to `c04a94b6`, created a
private backup, rebuilt and recreated the stack, passed health/seed/edition
checks, printed the full installer success card, then printed `Upgrade
complete`. Six sampled credential fields matched the pre-upgrade `.env` without
printing their values. The Community `.env` and API container carried blank
`BOSS_GITHUB_OWNER`/`BOSS_GITHUB_REPO` instead of silently selecting the
private source repo; explicit operator values remain preserved on rerun.

Both uninstall dry runs left services and data intact. An invalid purge phrase
was rejected before mutation. Piped `uninstall.sh --yes` stopped the four
containers and retained `.env` plus four named volumes. A second run succeeded
without changing them. A piped reinstall from the same source bundle printed
the full `BOSS Community is running.` card; the six sampled credentials still
matched, the seeded project count remained one, and all four services ran on
loopback ports.

```bash
LIMA_HOME=/Volumes/DataSSD1/test/boss-vm-qa-lima limactl shell boss-vm-qa -- \
  bash -s -- --dir /home/sean.guest/boss --yes \
  < scripts/install/uninstall.sh

LIMA_HOME=/Volumes/DataSSD1/test/boss-vm-qa-lima limactl shell boss-vm-qa -- \
  env BOSS_REPO_URL=/home/sean.guest/boss-vm-qa.bundle \
  BOSS_REPO_REF=ai/codex/community-installer-lifecycle \
  bash -s -- --dir /home/sean.guest/boss --yes --skip-model-key \
  --install-coding-clis < install.sh
```

For the data-purge drill, a VM-only database row and artifact marker were
created first. Piped `uninstall.sh --purge-data --confirm-purge DELETE-BOSS-DATA`
backed up `.env`, the Postgres database and artifacts. Its backup directory
had mode 0700, its `.env` mode 0600, and its dumps were nonempty. Purge then
removed `.env` and all four named volumes while retaining source and backups.
Restoring the recorded source commit and `.env`, waiting for Postgres readiness,
running `pg_restore`, unpacking artifacts, and starting the default web profile
recovered the database row, artifact marker and API health. The exact recovery
commands are in “Upgrade & recover” in `scripts/install/README.md` in the source checkout.

The first recovery attempt immediately after `up -d db` failed because
Postgres had not finished starting. The guide now waits for `pg_isready`.
Its earlier final command also enabled the optional Infisical profile on a
database-backed install; the guide now selects the web profile for that
configuration. The later restore completed, including both markers. Provider
CLI session volumes are outside the backup, so a purged install requires
provider sign-in again.

After the ARM64 source-wheel image fix below, the retained Ubuntu VM also
completed a **piped upgrade** from `c04a94b6` to `31a3e085` in 105.52
seconds. The upgrade created its private backup, rebuilt API and worker with
the source wheel, passed image imports, API health, seed and Community edition,
and printed `Upgrade complete`. All four services remained running. This
recheck guards the earlier Ubuntu result against the new Dockerfile stage.

## Debian 12 ARM64 clean-host drill

The first disposable Debian VM (`6.1.0-44-cloud-arm64`) began without Docker
or any coding CLI. The piped install installed Docker and the three official
CLIs, built all images, and started Postgres. Its API repeatedly exited 132
(SIGILL) before health. `python -X faulthandler -c 'import apps.api.app.main'`
located the crash in `cryptography==50.0.0`'s native `_rust` extension. The
published ARM64 wheels for 48.0.1, 49.0.0, 50.0.0 and 50.0.1 reproduced it;
a disposable source build of 50.0.1 imported successfully. The [PyCA ARM VM
report](https://github.com/pyca/cryptography/issues/14764) describes a similar
SIGILL in the published wheel. Downgrading to a pre-47 wheel was rejected:
[PyCA's security advisory](https://github.com/pyca/cryptography/security/advisories/GHSA-537c-gmf6-5ccf)
records vulnerable bundled OpenSSL in wheels before 48.0.1.

The image now builds the **pinned** 50.0.0 package from source on ARM64 in a
separate Docker stage, then installs that local wheel in API and worker. Both
images fail their build if `cryptography.exceptions` cannot be imported. The
source-built extension links the image's Debian system `libssl.so.3` and
`libcrypto.so.3`; keep the base image and its distro security updates current.
Other architectures continue to use their normal pinned wheel. The Rust and
compiler packages stay in the wheel-builder stage, outside runtime images.
On the first Debian VM, this fix brought API/worker up; a piped safe uninstall
retained `.env` and the four named volumes.

A **second clean** Debian 12 ARM64 VM (`boss-debian-final`) was then created
from the stock Lima Debian 12 cloud image with no Docker or CLI binaries and
no host mounts. The exact final-code entry point was:

```bash
LIMA_HOME=/Volumes/DataSSD1/test/boss-debian-final-lima limactl shell boss-debian-final -- \
  env -u OPENAI_API_KEY -u ANTHROPIC_API_KEY -u BOSS_GITHUB_TOKEN -u XAI_API_KEY \
  BOSS_REPO_URL=/home/sean.guest/boss-debian-final.bundle \
  BOSS_REPO_REF=ai/codex/community-installer-lifecycle \
  bash -s -- --dir /home/sean.guest/boss --yes --skip-model-key \
  --install-coding-clis < install.sh
```

At source commit `93e2c92b`, the command exited 0 after **445.62 seconds**
(7m26s, including apt downloads, official CLI downloads, Rust source build,
three application image builds, seed and health checks). The source wheel
stage took about 100 seconds in this cold build. Docker Engine 29.8.1,
Buildx 0.37.1 and Compose 5.5.1 were installed. Claude Code 2.1.281,
Codex CLI 0.156.1 and Grok Build 1.0.41 ran both on the VM host and in the
API container. The API and worker passed the native import check, four
services ran, API health and seed passed, and an authenticated `/v1/edition`
request with `project_id=boss-dogfood` returned `community`. API, web and
Postgres published only on `127.0.0.1`. `.env` had mode 0600 and blank
operator GitHub owner/repo fields. No subscription login or proof was run.

A second **piped** install against the retained data exited 0 in **21.58
seconds** with cached images and CLI binaries. Six sampled credentials matched
their previous values, the seeded project count stayed one, and the full
`BOSS Community is running.` marker printed. A piped uninstall dry run made
no changes. Then the safe uninstall completed:

```bash
LIMA_HOME=/Volumes/DataSSD1/test/boss-debian-final-lima limactl shell boss-debian-final -- \
  bash -s -- --dir /home/sean.guest/boss --yes \
  < scripts/install/uninstall.sh
```

It left `.env` mode 0600, all four named volumes and source commit intact,
and removed all Compose containers. Both disposable Debian VMs and the
earlier Ubuntu VM were stopped after evidence capture. Docker's warmed build
cache makes ordinary reruns much faster; changing the pinned cryptography
version, the base image, or its build stage requires another ARM64 source
build and a new image import check.

## Remaining release gates

- The public `seanventures/boss` URL and OSS license decision are pending.
- This branch was tested with its own web source; the separate console-session
  auth PR must land and receive a fresh HTTPS/SSH-tunnel login and CSRF check
  before public VM exposure. In this source, `/login` returned 404.
- Grok's governed harness is in a separate PR. No real provider subscription
  was exercised on the VM, and the optional Claude/Codex connect proof was not
  run without owner credentials.
- Repeat the clean-host smoke on x86-64, Ubuntu 22.04 and Debian 13 before
  claiming those platform variants qualified. Debian 12 ARM64 and Ubuntu
  24.04 ARM64 are the only VM variants qualified here.
