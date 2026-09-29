# BOSS Community quickstart

BOSS Community runs on an Ubuntu 22.04/24.04 or Debian 12/13 VM with `sudo`
access. The installer provisions Docker Engine, Buildx, Compose and other host
dependencies, starts Postgres, API, worker and Mission Control, seeds a project,
and checks API health. Model work needs provider credentials you control, through
an API key or a supported coding subscription. Leaving both unconnected starts
the control plane and leaves model work visibly unavailable.

In Community, the console's **Save key without test** action stores an API key
securely and marks that model **unverified**. A key supplied through `.env` is
also shown as unverified. Opening setup does not contact the model provider,
and saving a key does not send a test request: live checks need a governed
budget reservation, leased job, and cost receipt. A configured key may still be
invalid or lack access to the selected model; confirm it through a governed
model run before relying on it for work.

## Install

The Community repository is not public yet. After publication, replace the
placeholder with the reviewed release commit's full 40-character SHA and run:

```bash
BOSS_RELEASE_COMMIT=REPLACE_WITH_REVIEWED_40_CHARACTER_SHA bash -c 'set -u; [[ $BOSS_RELEASE_COMMIT =~ ^[0-9a-f]{40}$ ]] || { echo "Set the reviewed 40-character release SHA" >&2; exit 2; }; tmp=$(mktemp) || exit; curl -fsSL "https://raw.githubusercontent.com/seanventures/boss/$BOSS_RELEASE_COMMIT/install.sh" -o "$tmp" && BOSS_REPO_REF="$BOSS_RELEASE_COMMIT" bash "$tmp" "$@"; status=$?; rm -f "$tmp"; exit "$status"' _
```

The download must succeed before the installer runs; the same exact commit is
used for the source checkout and verified after clone. For an unattended VM
without a model key, add `--yes --skip-model-key` after the final `_`. To
install optional host copies of the official Claude Code, Codex CLI and Grok
Build binaries, add `--install-coding-clis` there as well.

If you already have a source tree, run the installer from that directory. This
does not log you into a maintainer machine:

```bash
cd /path/to/boss
./install.sh --dir "$PWD" --install-coding-clis
```

An empty install directory is filled from that tree. It does not clone the
unpublished GitHub URL, it does not SSH to a maintainer machine, and it does
not need Plane, an ops token, or a private registry:

```bash
cd /path/to/boss
./install.sh --dir "$HOME/boss" --yes --skip-model-key
```

If this checkout is already ahead of the default branch, or that branch has
diverged, the installer keeps this source. It does not rewind the tree.
Pass `--ref` when a different commit must be checked out; a diverged ref then
stops instead of installing the wrong source.

When that reviewed commit is installed, the default checkout is `~/boss`. The
installer writes secrets to `~/boss/.env` with mode 0600 and never prints the
generated API key or console password. The file stays on that machine. The
installer does not upload it. Keep it private and do not commit it. A rerun
keeps existing credentials and data. It leaves the operator GitHub owner/repo
unbound until you configure them; the installer's source repository is not
your project repository. The API, console, and database listen on `127.0.0.1`.

The installer does not add your user to the root-equivalent Docker group. If
Docker requires administrator access, use `sudo docker compose` for manual
commands; the installer detects that access for its own commands.

Docker Desktop keeps images on its own virtual disk. That disk can be full
while the host still has free space. If Postgres then fails to initialize, the
installer prints the database log, including `No space left on device`. Grow
the virtual disk or remove unused images, then run the installer again. A
rerun keeps the credentials already written.

Installing coding CLIs does not sign in to subscriptions. A laptop login is
not available on the VM. Codex and Claude have explicit connect steps after
official VM login; see “Optional coding subscriptions” in `scripts/install/README.md`
in the source checkout.
For Grok, run device login as the BOSS service user in the API container:

```bash
cd ~/boss
docker compose -f scripts/install/community-compose.yml --env-file .env \
  exec -u boss api grok login --device-auth
```

## Open and verify

The API, console and database bind to `127.0.0.1` on the VM you administer.
From your laptop, open an SSH tunnel to that VM, not to a shared service:

```bash
ssh -L 3000:127.0.0.1:3000 -L 8000:127.0.0.1:8000 user@your-vm
```

Open `http://127.0.0.1:3000` on the laptop. To retrieve the console password
privately on the VM, run `python3 scripts/install/console_password.py show`
from the install directory. For a hostname you serve, terminate HTTPS at a
reverse proxy you control while keeping BOSS bound to loopback; follow
`scripts/install/README.md` in the source checkout for the proxy and origin
requirements.

Check health and the seeded Community edition on the VM:

```bash
cd ~/boss
source ./boss-community.env.sh
curl -fsS "$BOSS_API_URL/health"
curl -fsS -H "Authorization: Bearer $BOSS_API_KEY" \
  "$BOSS_API_URL/v1/edition?project_id=$BOSS_PROJECT_ID"
```

Health does not show that the control plane can store work. Create one task
without a model key, an email account, or a deploy target. The owner key is
the `ceo` actor. Repeat the same `dedupe_key` and the API rejects the
duplicate instead of inserting a second task:

```bash
curl -fsS -H "Authorization: Bearer $BOSS_API_KEY" \
  -H "Content-Type: application/json" \
  -d "{\"project_id\":\"$BOSS_PROJECT_ID\",\"title\":\"Local note\",\"dedupe_key\":\"local-note-1\",\"actor\":\"ceo\"}" \
  "$BOSS_API_URL/v1/tasks"
```

If you skipped the model-key prompt, add your own `OPENAI_API_KEY` to the
private `.env` on the VM and recreate API and worker. Keep the key out of shell
history and repository files:

```bash
docker compose -f scripts/install/community-compose.yml --env-file .env \
  up -d --force-recreate api worker
```

## Upgrade or remove

From the install checkout, back up and upgrade with:

```bash
cd ~/boss
./scripts/install/upgrade.sh --dir "$PWD"
```

The upgrade backs up private configuration, Postgres and artifacts before it
changes source or containers. Rerunning the one-line installer also makes a
recovery backup before it fast-forwards an existing checkout. If the old
database was stopped, the backup starts its existing container and restores its
stopped state afterward. It never substitutes a mutable image tag. If the
container is missing, cannot start, or the backup fails, the source is left
unchanged. Upgrades keep named volumes and login sessions. To
stop BOSS and retain `.env`, volumes, images, backups and the checkout:

Older safe uninstalls may already have removed the database container. Follow
the [legacy recovery procedure](../../scripts/install/README.md#recover-an-older-safe-uninstall)
before upgrading such an install.

```bash
./scripts/install/uninstall.sh --dir "$PWD" --dry-run
./scripts/install/uninstall.sh --dir "$PWD" --yes
```

Run `./install.sh --dir "$PWD" --yes --skip-model-key` to resume. To remove
data intentionally, add `--purge-data`; the script backs up first and requires
the separate `DELETE-BOSS-DATA` confirmation. Purge also removes CLI session
volumes, so provider sign-in is required after recovery. See
“Upgrade & recover” in `scripts/install/README.md` in the source checkout for
exact restore steps and limits.
