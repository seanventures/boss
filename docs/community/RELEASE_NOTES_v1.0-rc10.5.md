# BOSS Community 1.0.0-rc.10.5 candidate

RC10.5 builds on the published rc10.4 candidate. It carries the work prepared on the rc10.5 and rc10.6 branches, which was merged but never tagged, and everything merged since. The candidates rc10 to rc10.4 left the package version at rc9 and the install guides at rc8; both now name this release. This is a test candidate. The automated suites pass on the release commit; the live checks listed under **Not yet verified** have not been run.

## What changed

### Engines and agents

- **One contract for every engine.** Claude Code, Codex, the Claude Agent SDK and BOSS's own agent run the same pipeline, gates, fix rounds and failure messages. Where an engine lacks a feature (session resume, a read-only flag), BOSS supplies it. See [Engine contract](../engineering/ENGINE_CONTRACT.md).
- **A conformance gate with no model.** `e2e-gate.sh engines` runs the real pipeline on a scripted engine and checks every gate on the poorest and the richest profile.
- **Every agent knows BOSS's fundamentals.** Every role, including new and custom ones, holds read access to logs, code and run history. It can query its own project's data read-only and propose a fix for a person to approve.
- **An agent session never holds the model API key.** Calls go through a loopback proxy, which records their usage.
- **Claude Agent SDK engine.** Builds, reviews and agents can run on an Anthropic API key, selected per job or for the whole install.

### Building apps

- **Java apps.** Maven and Gradle projects (Spring Boot, JAX-RS) preview in a container, deploy over SSH and are pre-checked. See [Toolchains](TOOLCHAINS.md).
- **Native packages compile.** The build image carries a C/C++ toolchain, so packages like better-sqlite3 and bcrypt install.
- **Several connectors in one build.** For example Login, Payments and Email together on one app, integrated in the app's own look.
- **New email connector.** SMTP or Resend, with a local mail catcher for previews.
- **Faster builds.** Writing stages run in parallel sessions sized from the host (Setup › Build speed). A long model stretch reports progress every 30 seconds. Every build logs where its time and tokens went.
- **A More tests pass on every code-building workflow.** It follows the preview on the same PR and never blocks the build.

### Workflows

- **New step types and controls.** For each runs one step per item of a list. A signed webhook can start a workflow. A step can back off, time out and take an error path.
- **Test runs.** A test run uses pinned data, and you can run again from a step.
- **Designer blocks.** The designer places Branch, Parallel/Join, Repeat-until and For each blocks, with arrows you can drag.
- **One approval per deploy.** The environment's policy decides whether to ask.
- **Remove a deployed app**, governed like a deploy. Data is kept unless you ask otherwise.

### Reliability

- **Startup no longer locks the whole database.** The schema apply commits each statement, and the column check only alters missing columns. Before, writes running during startup could deadlock against it.
- **Retries use the database's clock.** A retry is scheduled on the same clock that decides whether it is due.
- **Expired holds wait for a person.** The pre-check reports a positive-amount expired hold for a person to reconcile in Decisions, instead of fixing it automatically.
- **The org board only changes when something happens.** It no longer reports a change every second while an agent is working.
- **The heartbeat carries the version again**, including point releases like this one.
- **The installer picks the newest candidate by number.** It compared release names as text, so a fresh install asking for the newest candidate got rc9 instead of rc10.x.
- **Releasing a held proof from the CLI checks your identity permissions** when identity enforcement is on.

## Not yet verified

These need a laptop or real model keys and have not been run for this candidate:

- A real image build of `Dockerfile.community` with a native npm package installed inside it.
- The live per-engine matrix: Claude Code, Codex, OpenRouter, Groq and Ollama on the release gate.
- An agent diagnosing a real failed build from its logs, code and data with a live model.
- The full HelpDesk AI run: brief, build, PR, deploy and a working app.
- Website installer parity. The hosted installer is updated separately from source releases.

## Install and upgrade

Select `v1.0-rc10.5` explicitly with the installer's `--ref` option, from a checkout of that same tag. See the [Quickstart](QUICKSTART.md).

Before upgrading an existing install, take a complete protected backup (`boss upgrade` does this). Changing the checkout back does not undo database migrations.
