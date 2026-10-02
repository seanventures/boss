---
title: The control plane for AI software work
hide:
  - navigation
  - toc
---

<div class="boss-hero" markdown="1">

<div class="boss-hero__copy" markdown="1">

<p class="boss-eyebrow">Open source · Apache 2.0 · v1.0-rc4</p>

# The control plane for AI software work

Agents can write code, open pull requests, spend money, and touch infrastructure. BOSS decides whether they should, and keeps the proof when they do: plans you can read, budgets reserved before spend, approvals where people matter, and a receipt for every effect.

[Get started](QUICKSTART.md){ .md-button .md-button--primary }
[Deploy for your team](hostname.md){ .md-button }
[View on GitHub](https://github.com/seanventures/boss){ .md-button }

</div>

<div class="boss-receipt" markdown="1">

<p class="boss-receipt__kicker">Receipt · task created</p>

| | |
| --- | --- |
| Goal | Plan the community launch |
| Budget | Reserved before any model call |
| Approval | Held for a person in Approvals |
| Effect | Recorded once, with a dedupe key |
| Console | Your domain, or `127.0.0.1:3000` on the server |

</div>

</div>

## Why teams run BOSS

<div class="boss-cards" markdown="1">

<div class="boss-card" markdown="1">

### Nothing off the record

Every external effect is a receipt with a unique dedupe key, so a retried step is recognized instead of repeated.

</div>

<div class="boss-card" markdown="1">

### Spend reserved first

Model and tool work runs through quote, reserve, consume, and release. A runaway agent hits a budget wall, not your card.

</div>

<div class="boss-card" markdown="1">

### People decide what matters

Gated steps wait in an approval inbox with the context to decide. BOSS changes itself only through pull requests.

</div>

<div class="boss-card" markdown="1">

### No silent failures

Every job holds a heartbeated lease. Failures retry, dead-letter, or escalate to a person. Nothing is dropped.

</div>

<div class="boss-card" markdown="1">

### Your infrastructure

Self-hosted on your server and reached over HTTPS on your domain or private network. Credentials never leave it.

</div>

<div class="boss-card" markdown="1">

### Your models

OpenAI, Anthropic, OpenAI-compatible endpoints, and the Claude Code, Codex, and Grok subscriptions you already pay for.

</div>

</div>

## How it works

```text
Goal  →  Plan  →  Budget  →  Work  →  Evidence  →  Decision  →  Receipt
```

Give BOSS an outcome in **Command Center**. It drafts a plan, reserves budget before any model or tool call, runs the work on a leased worker, attaches evidence such as tests and pull requests, stops for a person wherever a decision is gated, and records a receipt for every effect. Postgres is the system of record, so every step can be audited after the fact.

## Drive it your way

<div class="boss-setups" markdown="1">

<div class="boss-setup" markdown="1">

### Mission Control

The web console for your whole team: **Command Center** for goals, **Approvals** for decisions, **Activity log** for every receipt and failure, and **Agent Studio** for testing agent definitions. [Console access](CONSOLE_AUTH.md)

</div>

<div class="boss-setup" markdown="1">

### Metatron terminal

`make metatron` opens a full terminal client. Chat with BOSS, work tasks and decisions, and reach any API operation with `/ops`, `/describe`, and `/op`. One-shot `--json` mode is built for scripts. [Terminal and API](TERMINAL_API.md)

</div>

<div class="boss-setup" markdown="1">

### MCP for your agents

Run `make mcp-setup`, then point Claude Code, Codex, or any MCP client at `make mcp`. Agents get project-scoped access to the API, with human confirmation on sensitive calls. [MCP](MCP.md)

</div>

</div>

Prefer plain HTTP? `source boss-community.env.sh` loads `BOSS_API_URL`, `BOSS_API_KEY`, and `BOSS_PROJECT_ID` without printing the key. See [CLI](cli.md) and [calling the API remotely](remote-api.md).

## Start here

| I want to | Read |
| --- | --- |
| Go from install to software BOSS built | [Getting started](GETTING_STARTED.md) |
| Install BOSS and run a first goal | [Quickstart](QUICKSTART.md) |
| Serve it to my team on a domain or private IP | [Deploy](hostname.md) |
| Connect a model or coding subscription | [Model connections](MODEL_CONNECTIONS.md) |
| See what a fresh install can do | [Use cases](use-cases.md) and [walkthroughs](walkthroughs.md) |
| Try ready-made workflows | [Example workflows](https://github.com/seanventures/boss/blob/main/examples/README.md) |
| See everything that ships, with status | [Features](FEATURES.md) |
| Upgrade, back up, or uninstall | [Installer guide](https://github.com/seanventures/boss/blob/main/scripts/install/README.md) |

## Get involved

BOSS is built in the open. [Open an issue](FEEDBACK_LOOP.md) for bugs and ideas, read the [contributing guide](https://github.com/seanventures/boss/blob/main/CONTRIBUTING.md) to send a pull request, and report vulnerabilities privately through the [security policy](https://github.com/seanventures/boss/blob/main/SECURITY.md).

BOSS Community is licensed under the Apache License 2.0.
