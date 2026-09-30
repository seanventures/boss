# BOSS documentation

<div class="boss-stage" markdown="1">

![BOSS: the self-hosted control plane for AI software work](assets/masthead.svg)

<p class="boss-markline"><span>BOSS</span> v1.0-rc1</p>

<div class="boss-manual" markdown="1">

BOSS is the self-hosted control plane for AI software work. Give it a goal, then review the plan, the approvals, the spend, and the receipts before anything leaves your machine.

</div>

<div class="boss-receipt" markdown="1">

<p class="boss-receipt__kicker">Your install at a glance</p>

| | |
| --- | --- |
| Release | `v1.0-rc1`, commit `7b88a18` |
| Console | `127.0.0.1:3000`, or a hostname you choose |
| Credentials | A local `.env` that never leaves the machine |
| License | Apache 2.0 |

</div>

</div>

## Get started

New to BOSS? The [Quickstart](QUICKSTART.md) takes you from a fresh machine to your first reviewed goal. The release one-liner is pinned to `7b88a18a2b4cd0b14b09a47fc415f7ffd4580066`, the `v1.0-rc1` commit; from a checkout, `./install.sh --dir "$PWD"` installs from that tree.

## Choose your interface

<div class="boss-setups" markdown="1">

<div class="boss-setup" markdown="1">

### Web console

Open [http://127.0.0.1:3000](http://127.0.0.1:3000) on the install machine and sign in with the [console password](CONSOLE_AUTH.md). To serve Mission Control on a real hostname, put your own TLS proxy in front of it and pass `--console-origin` to the installer. See [Hostname](hostname.md).

</div>

<div class="boss-setup" markdown="1">

### Terminal

`make metatron` in the install directory opens the Metatron TUI. See [Terminal](terminal.md) and the [terminal and API reference](TERMINAL_API.md).

- `/projects` lists the projects you can see
- `/ops` searches the operation catalog
- `/describe` explains an operation without calling it
- `/op` calls it with `path`, `query`, and `body`

</div>

<div class="boss-setup" markdown="1">

### CLI

`source boss-community.env.sh` sets `BOSS_API_URL`, `BOSS_API_KEY`, and `BOSS_PROJECT_ID` without printing the key. See [CLI](cli.md), plus [Model connections](MODEL_CONNECTIONS.md) and [Grok CLI](GROK_CLI.md) for the coding CLIs you already use.

</div>

</div>

## Guides

| I want to | Read |
| --- | --- |
| Install and run a first goal | [Quickstart](QUICKSTART.md) |
| See what a fresh install can do | [Use cases](use-cases.md) |
| Follow a guided tour | [Walkthroughs](walkthroughs.md) |
| Pick between web, terminal, API, and MCP | [Interfaces](interfaces.md) |
| Connect a model or coding subscription | [Model connections](MODEL_CONNECTIONS.md) |
| Sign in, or add HTTPS in front of the console | [Console access](CONSOLE_AUTH.md) |
| Upgrade, uninstall, change ports, or recover | [Installer guide](https://github.com/seanventures/boss/blob/main/scripts/install/README.md) |

## Reference

| I want to | Read |
| --- | --- |
| See everything Community includes, with status | [Features](FEATURES.md) |
| Understand how the API, terminal, and MCP relate | [Control surfaces](CONTROL_SURFACES.md) |
| Drive BOSS from the terminal or scripts | [Terminal and API](TERMINAL_API.md) |
| Call BOSS from a trusted MCP client | [MCP](MCP.md) |

## Mission Control at a glance

- **Command Center** is where you start: ask BOSS for an outcome.
- **Approvals** is where a person decides on gated steps.
- **Activity log** shows every receipt and every failure.
- **Agent work** groups recent activity by role.
- **Team structure** shows your planned roles next to the handoffs that actually happened. Rearranging the chart doesn't change how work is routed.
- **Agent Studio** is where you inspect and test agent definitions. Installing a definition or an example workflow never starts work by itself.

Examples throughout these guides use the project id `boss-dogfood`, which is the project the installer seeds on your machine, and `user@your-vm` for a VM you administer.

## Get involved

| | |
| --- | --- |
| Report a bug or suggest a feature | [Feedback](FEEDBACK_LOOP.md) |
| Propose a docs or code change | [Contributing](https://github.com/seanventures/boss/blob/main/CONTRIBUTING.md) |
| Report a vulnerability privately | [Security](https://github.com/seanventures/boss/blob/main/SECURITY.md) |

BOSS Community is licensed under the Apache License 2.0. Third-party components keep their own notices.
