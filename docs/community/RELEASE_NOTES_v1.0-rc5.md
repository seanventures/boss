# BOSS Community 1.0.0-rc.5

Fifth Community release candidate. Package version `1.0.0-rc.5`. It follows `v1.0-rc4` with about 500 commits.

## Install

```sh
curl -fsSL https://boss.sean.network/install.sh | bash
```

Already installed: `boss upgrade`. From a checkout: `git clone --branch v1.0-rc5 https://github.com/seanventures/boss.git` and `./install.sh --dir "$PWD" --ref v1.0-rc5`. Start with the [getting started guide](https://boss.sean.network/docs/GETTING_STARTED/).

## What's new

- **A redesigned console.**
  - **Command Center**: your team, what needs you, what is in flight, templates, the apps you have built, and 14 weeks of activity, in one read.
  - **New project**: build a new app, or add a feature to an existing app. A feature builds onto that app's branch.
  - **Research and Panels**: open from the workflow step that ran them, with the panel's votes in words and the PRD by section.
  - **Infra** (Fleet, Deploys and Previews, Cloud) and **Observability** (Pulse, Models, System, Sources).
  - **Activity log**: search, facets, a histogram, a live tail, and export. **Govern**: every limit with its real value, what enforces it, and what it stopped.
  - **Agent Studio** (agents with a Passport and Loadout: skills, MCP, instructions) and **Organization** (Teams, Flow, Lines).
  - **Factory** and **Apps**: the build floor, a cockpit for each build, and one table of every app.
  - **Setup** and **Skills & MCP** (the Kit).
- **Model router.** Setup › Model routing has two modes. **Fixed** is the default and works as before: the model you chose. **Smart** picks a model by tier, respects per-step floors, uses your subscription headroom first and then OpenRouter, escalates when a model fails, and never leaves a step stranded when one source is down. Every step shows **Why this model**.
- **Governed host deployment.** Infra › Deploys sends an app to a verified SSH host through plan → approve → apply. You see the plan, approve it, BOSS applies it and writes a receipt, and a failed health check rolls the host back.
- **Improve BOSS.** Run scorecards, experiments, and ratings, behind one opt-in switch in Govern › Sharing that is **off by default**. You can read the exact payload before anything is sent. A daily anonymous heartbeat can be turned off with `BOSS_TELEMETRY=off` or `DO_NOT_TRACK=1`. **There is no default collector**: nothing is sent, the heartbeat included, until you set a collector URL.
- **Stability.**
  - Builds are admitted against free memory and queue instead of thrashing. Workloads run in an envelope, so the control plane keeps its memory.
  - Every process a build starts is owned, Stop really stops it, and a reaper clears stale previews, orphans, temp files, images, and disk.
  - The system doctor: Shepherd and Metatron watch the machine, explain pressure in words, and offer choices. They never stop work that is waiting on a person.

## Fixed

- A failed workflow step now stops the run; you retry or skip it. A run never completes past a failure, and an agent step that did no work no longer passes its gate.
- One press, one effect: every write is idempotent, so a double click no longer starts work twice.
- An approval belongs to exactly one run and step, and a run that ends takes its unanswered approvals with it.
- A build waiting on you is never shown as stuck, and a build held by budget says so and resumes as soon as the cap allows.
- A per-minute budget limit waits and resumes; a monthly cap stops with its reason.
- A healthy Linux server is no longer told it is out of memory or to restart.
- Previews install an app's dependencies and build tools first, run the app's own build script, and open on the console's origin.
- `remote.sh update --all-services` rebuilds the console too.
- A fresh install can add, verify, and deploy to a VM without editing its environment.
- Discovery no longer gets stuck loading.
- A secret stored under a different master key shows as unavailable instead of an error.
- No credential reaches the Activity log, whichever field it was written into.
- API errors reach you as words, never `[object Object]`.
- The console's Log, Govern, Factory, and Setup pages fit a phone.

## Known issues

- **Single owner sign-in.** The console still signs in with one owner password. Per-person sign-in (Identity) is not in this release.
- **Paused in Community:** live debugging investigations; direct host changes made outside an approved plan; cloud-account (registry) deploy runs; auto-deploy; the agent host shell; host-backed preview sandboxes. Their records stay readable.
- microVM sandbox execution is not claimed; builds use the local subprocess path.
- Discovery web research needs a connected Claude Code subscription; without one the research step is skipped and says why.
- Software Factory builds need a GitHub repository connected to the project.
- The model router's **Smart** mode is new. **Fixed** stays the default.

## Verification

- Installer, upgrade, CLI, compose and docs tests passed (`engine/tests`: Community CLI, installer upgrade and backup, coding subscriptions, self-deploy, compose credentials and knobs, setup doc commands, retired-surface advice, tunnelled channel connect, fresh-install VM verify; `apps/api/tests`: upgraded install gets the cast, reinstall keeps subscriptions, the Activity log API).
- A new guard builds the OpenAPI schema route by route. It caught the Activity log's filters failing every `GET /v1/activity*` request on `main`; fixed in this release.
- `apps/web`: `tsc --noEmit` clean on a fresh build directory. `python -m compileall` clean on `engine`, `apps/api`, `apps/worker`, and `scripts`.
- Known test debt, not release blockers: the MCP and terminal capability matrices are behind the new routes; a few distribution checks expect files from the private source tree that this repository does not carry, and fail on `v1.0-rc4` too.
