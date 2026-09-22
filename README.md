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

- **Docker** with Compose
- **8 GB RAM minimum**, 16 GB comfortable — **100 GB disk**
- A **model key or subscription** (OpenAI, Anthropic, OpenRouter, or a Claude/Codex plan)

Those are measured, not guessed. The six containers idle at ~1.6 GB and the coding agent runs
*inside* the API container, so a build adds a Node process on top; under 8 GB the machine
swaps, and a host that swaps stops scheduling BOSS for minutes at a time. Images are ~11.5 GB
and building them has reached 58 GB of layer cache before pruning — see
[the installer notes](scripts/install/README.md) for the full breakdown.

---

## Quick start

### 1. Install

```bash
git clone https://github.com/seanheiney/boss.git ~/boss
cd ~/boss
./install.sh
```

Installing somewhere else? Pass the same path: `./install.sh --dir /opt/boss`. The installer
defaults to `~/boss` regardless of where you ran it from.

The installer checks the machine, picks ports that are free, starts Postgres, the API, the
worker and the console, seeds the project, and verifies it came up. It prints where everything
landed:

```
  Console       http://127.0.0.1:3000/?org=org_boss
  API           http://127.0.0.1:8000
  Project       boss-dogfood
```

Credentials are written to `.env`. **Back that file up** — `INFISICAL_ENCRYPTION_KEY` is the
only thing that can read what the secret store holds.

> Re-running `./install.sh` is safe. It reuses the database password and API key an existing
> install already has.

### 2. Connect a model

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

### 3. Check it actually works

```bash
./scripts/install/checkup.sh            # containers, API, console URL, models, host
./scripts/install/checkup.sh --probe    # also asks a model a real question (costs money)
```

`checkup` answers the question the installer cannot: not "did it start" but "can it do its
job" — containers, API, the address compiled into the console, models connected and with
capacity, a model that actually answers, a pushable repository, and whether the host can
schedule BOSS at all. Every one of them is a failure that has actually happened here.

### 4. Give it something to do

Open the console and describe an outcome in your own words — *"a calendar that shows change
freezes and warns on overlaps"*. BOSS researches it, convenes a panel, writes a PRD for you to
approve, then builds and tests it and opens a pull request.

---

## Install on a server

Run it from your laptop. Do not copy the repository up and SSH in.

```bash
./scripts/install/remote.sh \
  --host root@YOUR.SERVER.IP \
  --key ~/.ssh/your_key \
  install --yes
```

This streams an exact commit over SSH and runs the same installer there, so the server needs
**no git, no deploy key and no access to this repository** — only Docker. It records which
commit it shipped, and points the console at an address a browser can actually reach.

```bash
./scripts/install/remote.sh --host root@YOUR.SERVER.IP smoke     # prove it works
./scripts/install/remote.sh --host root@YOUR.SERVER.IP tunnel    # reach it from your laptop
./scripts/install/remote.sh --host root@YOUR.SERVER.IP uninstall --yes
```

`tunnel` forwards the server's API and console over SSH, which is how you configure a model on
a server without exposing its API to the internet.

Full detail, including subscriptions on a server and what to do when it goes wrong:
[**SETUP.md**](docs/operations/SETUP.md).

---

## Removing it

```bash
./scripts/install/uninstall.sh
```

Removes containers, volumes, images and `.env`, then verifies that "clean" is true rather than
trusting its own exit code. **This destroys the database and the secret-store key**, so it asks
before doing it. `--keep-env` keeps your credentials for a reinstall.

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
