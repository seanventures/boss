# BOSS

**Describe an outcome. BOSS researches it, writes the spec, builds it, tests it, and opens a pull request — and shows you its evidence at every step.**

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

Credentials are written to `.env`. **Back that file up** — it holds this install's keys.

#### Where should credentials live?

The installer asks. Both answers keep everything on your own machine — the difference is two
containers, not who holds your data.

| | `--secret-store database` *(default)* | `--secret-store infisical` |
|---|---|---|
| **Containers** | 4 | 6 (~400 MB more) |
| **Credentials held by** | BOSS's own Postgres | the bundled Infisical store |
| **Stored as** | the value | a *reference* — the value never sits in BOSS's database |
| **Storing a subscription token** | asks you to confirm the database is an acceptable home | just works |
| **Pick it when** | the box is small, or you are trying BOSS out | it is a real install you will keep |

```bash
./install.sh --secret-store infisical      # or database, which is the default
```

Either way it is **your** infrastructure. `database` is not "less secure", it is fewer moving
parts with the credential in Postgres instead of beside it.

> Re-running `./install.sh` is safe. It reuses the database password and API key an existing
> install already has.

### 3. Connect a model

BOSS ships with no model of its own. Until you connect one it starts, but it cannot think.

**The easy way:** open the console at `http://127.0.0.1:3000` → **Repo and model** → connect
what you have. It walks you through it and verifies the credential before saving.

**From the command line:** what you run depends on whether you have an API key or a
subscription, so pick your row in [Which model path is yours?](#which-model-path-is-yours)
below.

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

<details>
<summary><strong>No SSH key yet, or not sure what <code>--key</code> wants? — click to expand</strong></summary>

<br>

`--key` is your **private** key — the file *without* `.pub`, the same one `ssh -i` takes. Its
`.pub` half is what belongs in the server's `~/.ssh/authorized_keys`. If plain
`ssh root@YOUR.SERVER.IP` already works, you can leave `--key` off entirely.

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
  install --yes --secret-store database
```

`--key` is your **private** key — the file without `.pub`. Omit it if plain `ssh root@…`
already works.

**`--secret-store` is how you choose on a server.** `--yes` makes the install
non-interactive, so it never stops to ask — leave the flag off and you get `database` without
being offered `infisical`. Same two options as a local install:

| `--secret-store database` *(default)* | `--secret-store infisical` |
|---|---|
| 4 containers. Credentials in BOSS's own Postgres. | 6 containers, ~400 MB more. Bundled store, bootstrapped for you; BOSS keeps a reference rather than the value. |
| Small box, or evaluating. | An install you intend to keep. |

Both stay on your server — see [Where should credentials
live?](#where-should-credentials-live) for the full comparison. Drop `--yes` and the installer
asks you instead.

This streams a `git archive` of your current commit over the SSH connection and runs the same
`install.sh` on the far side. What lands there is a **named commit**, recorded in
`.boss-shipped-commit`, so "which version is running" has an answer. It also points the
console at an address a browser can actually reach, rather than the `127.0.0.1` that means
*your* machine.

Ship a specific version with `--ref v1.2.0`, or pass anything through to the installer after
`--`.

When it finishes:

```
  Console   http://YOUR.SERVER.IP:3000
  API       http://YOUR.SERVER.IP:8000
```

Credentials are written to **`/opt/boss/.env` on the server**, not on your machine. **Back that
file up** — with `--secret-store infisical` it holds `INFISICAL_ENCRYPTION_KEY`, the only thing
that can read what the store contains.

> Re-running this is safe. It reuses the database password and API key the install already has,
> replaces the source tree with the commit you are shipping, and leaves the database alone.

`:8000` is the API — opening `/` there returns `{"detail":"Not Found"}`, which is correct and
not a broken install. The console is `:3000`.

### 3. Connect a model

BOSS ships with no model of its own. Until you connect one it starts, but it cannot think.

**The easy way:** open `http://YOUR.SERVER.IP:3000` → **Repo and model** → connect what you
have.

**From the command line:** what you run depends on whether you have an API key or a
subscription — pick your row in [Which model path is yours?](#which-model-path-is-yours) below.
If it is an API key, connecting it to a server is a single command; the table says so.

### 4. Check it actually works

```bash
./scripts/install/remote.sh --host root@YOUR.SERVER.IP --key ~/.ssh/id_ed25519 smoke
```

`smoke` proves four things the installer's own "done" message does not: containers up,
`/health` answering, the API able to reach its database, and the console built for an address
a browser can reach. It finishes by printing where to go.

For the fuller picture — including whether a model is connected and whether it actually
answers:

```bash
./scripts/install/remote.sh --host root@YOUR.SERVER.IP --key ~/.ssh/id_ed25519 check
```

That runs BOSS's own `checkup` on the server. Add `-- --probe` to make it ask a model a real
question, which costs money and is the only way to tell *connected* from *working*.

> **Would rather not have `:3000` and `:8000` open to the internet?** Run
> `remote.sh --host … --key … tunnel` and leave it running; the console is then at
> `http://127.0.0.1:3000` on your own machine instead. Entirely optional — every step here
> works without it.

### 5. Give it something to do

Open the console and describe an outcome in your own words — *"a calendar that shows change
freezes and warns on overlaps"*. BOSS researches it, convenes a panel, writes a PRD for you to
approve, then builds and tests it and opens a pull request.

### Taking it off again

```bash
./scripts/install/remote.sh --host root@YOUR.SERVER.IP --key ~/.ssh/id_ed25519 uninstall --yes
```

`--yes` is required: there is no prompt over a non-interactive SSH session to catch a mistake,
and this destroys the server's database and secret-store key. It then verifies the host is
clean rather than trusting its own exit code.

Full detail — subscriptions on a server, manual install, troubleshooting:
[**SETUP.md**](SETUP.md).

---

## Which model path is yours?

BOSS spends models in **three** places and they are configured separately: **builds** (writing
and reviewing code), **the panel** (the meeting that writes your PRD), and **the chat** (asking
BOSS questions). One subscription or one API key covers all three — but an install that sets
only builds has a panel and a chat that cannot answer.

Find your row. Each links to the section of [SETUP.md](SETUP.md) with the
exact commands:

| You have | Where BOSS runs | Follow |
|---|---|---|
| **An API key** — OpenAI, Anthropic, OpenRouter, or anything OpenAI-compatible | anywhere | [2A — API key](SETUP.md#2a--api-key) |
| **A Claude or Codex subscription** | in containers — *the normal install* — or on a server | [2C — send the credential](SETUP.md#2c--subscription-boss-in-containers-or-on-a-server) |
| **A Claude or Codex subscription** | directly on your host, sharing the CLI's own login | [2B — same filesystem](SETUP.md#2b--subscription-boss-on-this-machine) |
| **Neither yet** | — | [openrouter.ai/keys](https://openrouter.ai/keys) is the shortest route to one key that reaches most models |

**If you installed with either quick start above, a subscription puts you in 2C — even on your
own laptop.** BOSS runs in containers, and a container does not share your home directory, so
the `codex login` or `claude auth login` you did on this machine is invisible to it. "Same
machine" is not the question; *same filesystem as the CLI's login* is. 2B is for BOSS running
directly on your host with `pnpm dev`, or a container you `exec` into and sign in inside.

**API key onto a server?** That one is a single command — no tunnel, no exports, and it uses
the server's own credentials rather than making you find them:

```bash
./scripts/install/remote.sh --host root@YOUR.SERVER.IP --key ~/.ssh/id_ed25519 \
  model --provider PROVIDER --api-key YOUR_KEY --model MODEL_NAME
```

`PROVIDER` is `openrouter`, `openai`, `anthropic` or `custom`; `MODEL_NAME` is optional.
The key is verified against the provider before it is stored, so a bad one comes back as
`that key was rejected` and nothing is saved.

Once something is connected, [choose which model fills which
role](SETUP.md#choose-a-model-per-role) — implementer, reviewer, hard problems,
and the one that answers the chat and the panel.

---

## Documentation

| | |
|---|---|
| [**Setup**](SETUP.md) | Install, connect a model, move it to a server, manual setup, troubleshooting |
| [**Examples**](examples/) | Workflows built from the skills BOSS ships — read one, or run its setup script |
| [Features](docs/community/FEATURES.md) | What Community includes |
| [Capabilities](docs/product/CAPABILITIES.md) | Governed runtime in detail — budgets, audit, comms, entitlements |
| [Architecture](docs/ARCHITECTURE.md) | The invariants BOSS holds itself to, and the API that enforces them |
| [API](docs/api/README.md) | Auth, conventions, route index — `GET /openapi.json` when running |
| [Contributing](CONTRIBUTING.md) | Running from source, repo layout, `make` targets |
| [Community feedback](docs/community/FEEDBACK_LOOP.md) | Report an issue and see how it becomes a verified change |
| [Security](SECURITY.md) | Private vulnerability reporting |
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

More in [SETUP.md](SETUP.md#when-the-install-goes-wrong).

---

## Security

BOSS holds model keys, repository tokens and your project's data. It is designed to fail closed:
a missing credential produces a visible decision rather than a silent fallback.

Found a vulnerability? Please report it privately rather than opening an issue.

---

## License

[Apache 2.0](LICENSE).
