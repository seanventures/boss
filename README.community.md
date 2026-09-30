# BOSS Community

BOSS is a self-hosted control plane for software work. On a machine you administer, you give it a goal and review the plan, decisions, code, tests, costs, and receipts in one console.

## What BOSS is

BOSS keeps commands, tasks, agent runs, approvals, budgets, and receipts on that machine. The console is Mission Control.

Start in **Command Center**. Use **Approvals** when a person has to decide, and **Activity log** for receipts and failures. **Agent work** shows recent activity by role. **Team structure** holds a planned role layout and the handoffs that actually happened. Moving a role on the chart does not change how tasks are routed. **Agent Studio** holds definitions you can inspect and test. Installing a definition does not start work.

Software Factory APIs and screens are part of this install. A run still stops for the approvals, budget checks, and receipts those screens show. A preview is not proof that a deploy worked. New Discovery web research, live debugging investigations, and BOSS-initiated SSH workload planning and deployment are paused while spend, recovery, and effect safeguards are finished. Saved dossiers, investigation results, and deployment records stay readable.

The [five example workflows](examples/README.md) show research, content preparation, and a handoff to a person for approval. Installing those workflows does not start work. What else is in Community is listed in [Features](docs/community/FEATURES.md).

## Who it is for

Use this if you will run BOSS yourself and keep credentials on your own computer or VM. It is not a hosted BOSS account. It does not include a hosted marketplace or managed elastic workers.

This repository has not been published. This is not a public or generally available service. Install from a checkout you already have, or wait until a reviewed commit exists.

## Install

Until that reviewed commit exists, the published command cannot succeed. Leave the placeholder as written. Do not invent a SHA, and do not point the URL at another host.

```sh
BOSS_RELEASE_COMMIT=REPLACE_WITH_REVIEWED_40_CHARACTER_SHA bash -c 'set -u; [[ $BOSS_RELEASE_COMMIT =~ ^[0-9a-f]{40}$ ]] || { echo "Set the reviewed 40-character release SHA" >&2; exit 2; }; tmp=$(mktemp) || exit; curl -fsSL "https://raw.githubusercontent.com/seanventures/boss/$BOSS_RELEASE_COMMIT/install.sh" -o "$tmp" && BOSS_REPO_REF="$BOSS_RELEASE_COMMIT" bash "$tmp" "$@"; status=$?; rm -f "$tmp"; exit "$status"' _
```

A failed download stops the command before the installer runs. The script and the checkout use that same commit.

From a checkout you already have, this does not contact a maintainer server:

```sh
./install.sh --dir "$PWD"
```

The installer sets up Docker and the Community services on that machine, creates local credentials, seeds a project, and checks that the API and console start. It binds the API and console to `127.0.0.1`. It writes `.env` on that machine and does not upload it. Keep the file private, back it up, and do not commit it.

No step requires Plane, an ops token, a private registry, or SSH to a machine you do not administer.

The optional [Goldeneye kit](docs/product/GOLDENEYE_PAAS.md) is software you run locally. It is not access to a server operated by someone else.

Do not pass API keys or tokens as arguments. Paste an OpenAI key at the installer's hidden prompt, or skip it and [connect a model](docs/community/MODEL_CONNECTIONS.md) later. Ports, recovery, and subscription import are in the [installer guide](scripts/install/README.md). One path from this checkout to a first task is the [quickstart](docs/community/QUICKSTART.md).

Host copies of the official Claude Code, Codex CLI, and Grok Build binaries are optional:

```sh
./install.sh --dir "$PWD" --install-coding-clis
```

That installs binaries. It does not sign you in. A login on your laptop is not a login on this machine.

## Open the console

On the install machine, open `http://127.0.0.1:3000`.

From another computer, tunnel to a VM you administer. `user@your-vm` means that VM. It is not a shared host and not a machine run by the maintainers:

```sh
ssh -L 3000:127.0.0.1:3000 -L 8000:127.0.0.1:8000 user@your-vm
```

Then open `http://127.0.0.1:3000` on the computer that opened the tunnel. That tunnel only reaches the console. It does not deploy an app through BOSS.

The installer prints the install location and a command that shows the console password on the install machine. The password is not the API key. See [console access](docs/community/CONSOLE_AUTH.md).

## Get help

When this repository is public, use the bug, installation, or feature form. The [feedback process](docs/community/FEEDBACK_LOOP.md) is how a report becomes a reviewed change. A docs or code proposal starts at [Contributing](CONTRIBUTING.md). The [Code of Conduct](CODE_OF_CONDUCT.md) applies in project spaces. The [guide index](docs/community/README.md) lists the next page for install, a first goal, a model, the terminal, feedback, and security.

Report a vulnerability privately to [boss@sean.net](mailto:boss@sean.net). Do not open a public issue for a vulnerability. See [SECURITY.md](SECURITY.md).

The console and the [terminal](docs/community/TERMINAL_API.md) use the authenticated HTTP API. [MCP](docs/community/MCP.md) is for a trusted client on one configured project. Its actions are not the same set as the HTTP API.

## Keep a running install

From the installed checkout:

```sh
./scripts/install/upgrade.sh --dir "$PWD"
```

Upgrade writes a private backup before it replaces the running source. To stop BOSS and keep credentials and data:

```sh
./scripts/install/uninstall.sh --dir "$PWD" --yes
```

Deleting data is a separate confirmation. See [upgrade, backup, and restore](scripts/install/README.md#upgrade--recover).

## License

Community source is Apache License 2.0. See `LICENSE`. Other components keep their own notices.
