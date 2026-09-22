# BOSS

**Describe an outcome. BOSS researches it, writes the spec, builds it, tests it, and opens a pull request — and shows you its evidence at every step.**

[![License](https://img.shields.io/badge/license-Apache%202.0-blue.svg)](LICENSE)
[![Release](https://img.shields.io/badge/release-v0.14.0-blue.svg)](CHANGELOG.md)
[![Self-hosted](https://img.shields.io/badge/self--hosted-Docker-2496ED.svg)](#quick-start)

BOSS — the Business Operating System Sentinel — is a self-hosted control plane for AI software
work. You give it an outcome; a panel of agents debates it into a spec you approve, a coding
agent builds against that spec in a sandbox, and the result arrives as a tested pull request
you review. Nothing reaches the outside world without a receipt, and nothing spends money
without a reservation.

It runs on your machine with your own model keys. There is no hosted dependency.

**Community is a fully functional product, not a crippled trial.** Cloud is an optional path
when you want hosted capacity — not something you need to get started.

---

## What makes it different

Most AI coding tools help you write code faster in your editor. BOSS runs the work *around* the
code — and is accountable for it.

| | |
|---|---|
| **Governed, not autonomous** | Every step that costs money or touches the outside world needs an approval, a budget reservation, and leaves a receipt. |
| **Evidence, not assertions** | "Tests passed" means a recorded run you can open. A review that never ran cannot be reported as an opinion. |
| **A spec before a build** | An agent panel debates your directive into a PRD you approve before anything is written. |
| **Bring your own everything** | Your models, your keys, your repositories, your infrastructure. |
| **It explains itself** | Every run produces a report: what was built, whether it works, what it cost, and what to do next. |

---

## Requirements

| | |
|---|---|
| **OS** | macOS or Linux. **Windows through WSL2 only** — see [below](#windows) |
| **Docker** | Desktop (macOS/Windows) or Engine + Compose v2 (Linux) |
| **RAM** | **8 GB minimum**, 16 GB comfortable |
| **Disk** | **100 GB** |
| **A model** | an API key or a Claude/Codex subscription |

Those are measured, not guessed. The six containers idle at ~1.6 GB and the coding agent runs
*inside* the API container, so a build adds a Node process on top; under 8 GB the machine
swaps, and a host that swaps stops scheduling BOSS for minutes at a time. Images are ~11.5 GB
and building them has reached 58 GB of layer cache before pruning — see
[the installer notes](scripts/install/README.md) for the full breakdown.

### Windows

The installer is a bash script, so it needs **WSL2** — install Docker Desktop with the WSL2
backend, then run everything *inside* your WSL2 distribution and clone into the WSL2 filesystem
rather than `/mnt/c`. Native PowerShell, CMD and Git Bash are not supported.

You can still *drive* a server install from Windows, macOS or Linux alike — that only needs
`ssh`, `git` and `tar`. See [Install on a server](#install-on-a-server).

---

## Quick start

Two ways to run BOSS. **Both start with a clone on your own machine** — the installer and the
remote tooling both live in this repository.

| | [On this machine](#install-on-this-machine) | [On a server](#install-on-a-server) |
|---|---|---|
| **Good for** | trying it, developing against it | real work, a shared install, anything long-running |
| **BOSS runs on** | your own machine | the server |
| **You drive it from** | your own machine | your own machine, over SSH |
| **Console at** | `http://127.0.0.1:3000` | an SSH tunnel to the server |
| **Credentials land on** | your own machine | **the server** |

---

## Install on this machine

### 1. Clone it

```bash
git clone https://github.com/seanheiney/boss.git ~/boss
cd ~/boss
```

### 2. Install

```bash
./install.sh
```

Installing somewhere else? Pass the same path: `./install.sh --dir /opt/boss`. The installer
defaults to `~/boss` regardless of where you ran it from.

It checks the machine, picks ports that are free, starts Postgres, the API, the worker and the
console, seeds the project, and verifies it came up. It prints where everything landed:

```
  Console       http://127.0.0.1:3000/?org=org_boss
  API           http://127.0.0.1:8000
  Project       boss-dogfood
```

Credentials are written to `.env`. **Back that file up** — `INFISICAL_ENCRYPTION_KEY` is the
only thing that can read what the secret store holds.

> Re-running `./install.sh` is safe. It reuses the database password and API key an existing
> install already has.

### 3. Connect a model

BOSS ships with no model of its own. Until you connect one, it can start but it cannot think.

Open the console, go to **Repo and model**, and connect either an API key or a subscription.
The same place sets which model answers each role — builds, the panel, and the chat are
configured separately, and an install that sets only the first has a chat that cannot answer.

From the command line instead:

```bash
export BOSS_API=http://127.0.0.1:8000
export BOSS_API_KEY=$(grep '^BOSS_API_KEY=' .env | cut -d= -f2)

curl -sS -X POST "$BOSS_API/v1/setup/model" \
  -H "x-boss-api-key: $BOSS_API_KEY" -H 'Content-Type: application/json' -d '{
  "project_id": "boss-dogfood", "actor": "ceo",
  "provider": "openrouter", "api_key": "sk-or-your-key-here",
  "model": "anthropic/claude-sonnet-4.5"
}'
```

Using a **Claude or Codex subscription** rather than a key? BOSS runs in containers, so the
login on your own machine is invisible to it — see
[Connecting a model](docs/operations/SETUP.md#part-2--connect-a-model) for the three paths and
which one is yours.

### 4. Check it actually works

```bash
./scripts/install/checkup.sh            # containers, API, console URL, models, host
./scripts/install/checkup.sh --probe    # also asks a model a real question (costs money)
```

`checkup` answers the question the installer cannot: not "did it start" but "can it do its
job" — containers, API, the address compiled into the console, models connected and with
capacity, a model that actually answers, a pushable repository, and whether the host can
schedule BOSS at all. Every one of them is a failure that has actually happened here.

### 5. Give it something to do

Open the console and describe an outcome in your own words — *"a calendar that shows change
freezes and warns on overlaps"*. BOSS researches it, convenes a panel, writes a PRD for you to
approve, then builds and tests it and opens a pull request.

### Taking it off again

```bash
./scripts/install/uninstall.sh
```

Removes containers, volumes, images and `.env`, then verifies that "clean" is true rather than
trusting its own exit code. **This destroys the database and the secret-store key**, so it asks
before doing it. `--keep-env` keeps your credentials for a reinstall.

---

## Install on a server

You still clone on **your own machine**. `remote.sh` lives in this repository and drives the
server over SSH — nothing is copied up by hand, and the server never needs git or access to
this repository.

**On the server you need:** SSH access, and Docker with Compose. That is all.

`--key` is your **private** key — the file *without* `.pub`, the same one `ssh -i` takes. Its
`.pub` half is what belongs in the server's `~/.ssh/authorized_keys`. If plain
`ssh root@YOUR.SERVER.IP` already works, you can leave `--key` off entirely.

<details>
<summary><strong>No SSH key yet? Set one up — click to expand</strong></summary>

<br>

**1. Make a keypair on your own machine.** Ed25519, because it is short and every current
server accepts it. The passphrase is optional but worth having:

```bash
ssh-keygen -t ed25519 -C "boss-deploy" -f ~/.ssh/boss_deploy
```

That writes two files and they are not interchangeable:

| File | This is | Treat it as |
|---|---|---|
| `~/.ssh/boss_deploy` | the **private** key — what `--key` wants | a password. Never send it anywhere |
| `~/.ssh/boss_deploy.pub` | the **public** key | safe to paste into a form, an email, a ticket |

**2. Put the public half on the server.** Which method depends on whether you can already get
in:

*Creating the VM now?* Every provider asks for a public key at creation — Hetzner, DigitalOcean,
AWS, Vultr all have a field for it. Paste the contents of the `.pub` file:

```bash
cat ~/.ssh/boss_deploy.pub
```

*Server already exists and you can log in with a password?*

```bash
ssh-copy-id -i ~/.ssh/boss_deploy.pub root@YOUR.SERVER.IP
```

*Server exists but `ssh-copy-id` is unavailable?* The same thing by hand:

```bash
cat ~/.ssh/boss_deploy.pub | ssh root@YOUR.SERVER.IP \
  'mkdir -p ~/.ssh && cat >> ~/.ssh/authorized_keys && chmod 700 ~/.ssh && chmod 600 ~/.ssh/authorized_keys'
```

Those permissions matter — `sshd` silently ignores an `authorized_keys` that other users can
write to, which looks exactly like the key not working.

**3. Prove it before installing anything:**

```bash
ssh -i ~/.ssh/boss_deploy root@YOUR.SERVER.IP 'echo connected; docker --version'
```

You want `connected` and a Docker version. If Docker is missing, install it on the server
first — `remote.sh` checks for it and stops rather than installing it for you.

**4. Then install**, pointing `--key` at the **private** file:

```bash
./scripts/install/remote.sh --host root@YOUR.SERVER.IP --key ~/.ssh/boss_deploy install --yes
```

<br>

**If it says `Permission denied (publickey)`:** you named the `.pub` file instead of the
private one, the public half never reached the server, or `~/.ssh` on the server is
world-writable. `ssh -v` prints which key it offered.

</details>

### 1. Clone it, on your own machine

```bash
git clone https://github.com/seanheiney/boss.git ~/boss
cd ~/boss
```

You are not installing here. This is the tooling that installs onto the server.

### 2. Install onto the server

```bash
./scripts/install/remote.sh \
  --host root@YOUR.SERVER.IP \
  --key ~/.ssh/id_ed25519 \
  install --yes
```

This streams a `git archive` of your current commit over the SSH connection and runs the same
`install.sh` on the far side. What lands there is a **named commit**, recorded in
`.boss-shipped-commit`, so "which version is running" has an answer. It also points the
console at an address a browser can actually reach, rather than the `127.0.0.1` that means
*your* machine.

Ship a specific version with `--ref v1.2.0`, or pass anything through to the installer after
`--`.

### 3. Check it actually works

```bash
./scripts/install/remote.sh --host root@YOUR.SERVER.IP --key ~/.ssh/id_ed25519 smoke
./scripts/install/remote.sh --host root@YOUR.SERVER.IP --key ~/.ssh/id_ed25519 check
```

`smoke` proves four things the installer's own "done" message does not: containers up,
`/health` answering, the API able to reach its database, and the console built for a reachable
address.

### 4. Reach it from your own machine

Do not expose the server's API to the internet. Forward it over SSH instead:

```bash
./scripts/install/remote.sh --host root@YOUR.SERVER.IP --key ~/.ssh/id_ed25519 tunnel
```

Leave that running. The server's console is now at `http://127.0.0.1:3000` and its API at
`http://127.0.0.1:8000`, on your own machine.

### 5. Connect a model

Same as a local install, with one difference that catches people: **the API key is the
server's, not yours.** A credential lands in the secret store of whichever install you are
talking to.

```bash
export BOSS_API=http://127.0.0.1:8000       # through the tunnel from step 4
export BOSS_API_KEY=$(ssh root@YOUR.SERVER.IP "grep '^BOSS_API_KEY=' /opt/boss/.env | cut -d= -f2")
```

Then open `http://127.0.0.1:3000` → **Repo and model**, or use the same `curl` as the local
path above.

### Taking it off again

```bash
./scripts/install/remote.sh --host root@YOUR.SERVER.IP --key ~/.ssh/id_ed25519 uninstall --yes
```

`--yes` is required: there is no prompt over a non-interactive SSH session to catch a mistake,
and this destroys the server's database and secret-store key. It then verifies the host is
clean rather than trusting its own exit code.

Full detail — subscriptions on a server, manual install, troubleshooting:
[**SETUP.md**](docs/operations/SETUP.md).

---

## Documentation

| | |
|---|---|
| [**Setup**](docs/operations/SETUP.md) | Install, connect a model, move it to a server, manual setup, troubleshooting |
| [Features](docs/community/FEATURES.md) | What Community includes |
| [Capabilities](docs/product/CAPABILITIES.md) | Governed runtime in detail — budgets, audit, comms, entitlements |
| [Architecture](docs/ARCHITECTURE.md) | The invariants BOSS holds itself to, and the API that enforces them |
| [API](docs/api/README.md) | Auth, conventions, route index — `GET /openapi.json` when running |
| [Contributing](CONTRIBUTING.md) | Running from source, repo layout, `make` targets |
| [Community spec](docs/product/BOSS_COMMUNITY_EDITION_SPEC.md) | The product contract |
| [Changelog](CHANGELOG.md) | Release notes |

---

## Troubleshooting

**The console loads but everything is empty.** The API address is compiled into the console at
build time. On a server, the default `127.0.0.1` means *your* machine — pass `--public-url`, or
let `remote.sh` derive it.

**Builds are slow and nothing explains why.** Check the host. A machine that swaps stops
scheduling BOSS for minutes at a time; `checkup` reports this, and so does every run report.

**A model is connected but the chat cannot answer.** Builds, the panel and the chat are three
separate settings. Set all three under **Repo and model**.

More in [SETUP.md](docs/operations/SETUP.md#when-the-install-goes-wrong).

---

## Security

BOSS holds model keys, repository tokens and your project's data. It is designed to fail closed:
a missing credential produces a visible decision rather than a silent fallback.

Found a vulnerability? Please report it privately rather than opening an issue.

---

## License

[Apache 2.0](LICENSE).
