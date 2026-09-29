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

Install from the Community repository:

```bash
curl -fsSL https://raw.githubusercontent.com/seanventures/boss/main/install.sh | bash
```

For an unattended VM without a model key, or to add the official Claude Code,
Codex CLI and Grok Build binaries, pass flags to the piped installer:

```bash
curl -fsSL https://raw.githubusercontent.com/seanventures/boss/main/install.sh |
  bash -s -- --yes --skip-model-key --install-coding-clis
```

Invited contributors can run the same installer from an authorized checkout:

```bash
cd /path/to/authorized/boss
./install.sh --dir "$PWD" --install-coding-clis
```

On a fresh public install, the default checkout is `~/boss`. The installer
writes secrets to `~/boss/.env` with mode 0600 and never prints the generated
API key or console password. A rerun keeps existing credentials and data. It
leaves the operator GitHub owner/repo unbound until you configure them; the
installer's source repository is not your project repository.

The installer does not add your user to the root-equivalent Docker group. If
Docker requires administrator access, use `sudo docker compose` for manual
commands; the installer detects that access for its own commands.

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

The API, console and database bind to `127.0.0.1` on the VM. From your laptop,
open an SSH tunnel:

```bash
ssh -L 3000:127.0.0.1:3000 -L 8000:127.0.0.1:8000 user@your-vm
```

Open `http://127.0.0.1:3000` on the laptop. To retrieve the console password
privately on the VM, run `python3 scripts/install/console_password.py show`
from the install directory. For a remote hostname, terminate HTTPS at a trusted
reverse proxy while keeping BOSS bound to loopback; follow
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
database was stopped, the backup starts it temporarily and restores its
stopped state afterward. If it cannot start or the backup fails, the source
is left unchanged. Upgrades keep named volumes and login sessions. To
stop BOSS and retain `.env`, volumes, images, backups and the checkout:

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
