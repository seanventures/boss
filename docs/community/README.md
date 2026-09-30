# BOSS Community guides

<div class="boss-manual" markdown="1">

The manual for a control plane you run yourself. Give it a goal. Review the plan, the decision, and the receipt before anything leaves the machine.

</div>

<div class="boss-receipt" markdown="1">

<p class="boss-receipt__kicker">On this machine</p>

| Line | What you have |
| --- | --- |
| Machine | Yours. The installer does not call a maintainer server. |
| Console | `127.0.0.1:3000` on that machine. |
| Credentials | A local `.env`. It is not uploaded. |
| This site | Not published yet. The installer pin stays a placeholder. |

</div>

## Three setups

<div class="boss-setups" markdown="1">

<div class="boss-setup" markdown="1">

### Web console

On the install machine, open [http://127.0.0.1:3000](http://127.0.0.1:3000). Sign-in is [console access](CONSOLE_AUTH.md). A hostname needs a TLS proxy you run. The installer flag is `--console-origin`, and the listener stays on loopback.

</div>

<div class="boss-setup" markdown="1">

### Terminal

From the install directory, `make metatron` opens the Metatron TUI inside the API container. Commands are in [terminal and API access](TERMINAL_API.md). Setup is on the [terminal](terminal.md) page.

`/projects` asks the API for the projects returned under the current credentials, prints that list with the active project marked, and does not select a different project.

`/ops` reads the catalog of the API you are connected to so you can find an operation before you inspect or call one.

`/describe` takes one operation id and shows its method, path, parameters, and typed body fields, and it does not call the operation.

`/op` takes that operation id and optional JSON whose only keys are `path`, `query`, and `body`, then sends the call through the authenticated HTTP API.

</div>

<div class="boss-setup" markdown="1">

### CLI

Source `boss-community.env.sh` for `BOSS_API_URL`, `BOSS_API_KEY`, and `BOSS_PROJECT_ID`. The API key is not printed and is not a command argument. Setup is on the [CLI](cli.md) page. Coding CLIs you already have are in [model connections](MODEL_CONNECTIONS.md) and [Grok CLI](GROK_CLI.md).

</div>

</div>

Pick the page that matches what you are trying to do.

| You want to | Open |
| --- | --- |
| Install from a checkout you already have, then do one first goal | [Quickstart](QUICKSTART.md) |
| See a situation this install is for | [Use cases](use-cases.md) |
| Follow install, a first goal, and the terminal | [Walkthroughs](walkthroughs.md) |
| Choose the installer, console, terminal, API, or MCP | [Interfaces](interfaces.md) |
| See upgrade, uninstall, ports, and the unpublished one-liner | [Installer guide](https://github.com/seanventures/boss/blob/main/scripts/install/README.md) |
| Connect a model or coding subscription you already have | [Model connections](MODEL_CONNECTIONS.md) |
| Use the terminal against the HTTP API | [Terminal](TERMINAL_API.md) |
| Report a bug, a setup problem, or an idea | [Feedback](FEEDBACK_LOOP.md) |
| Report a vulnerability in private | [Security](https://github.com/seanventures/boss/blob/main/SECURITY.md) |

The install command stays pinned to `REPLACE_WITH_REVIEWED_40_CHARACTER_SHA` until a reviewed commit exists. That command stops before it downloads anything. From a checkout, `./install.sh --dir "$PWD"` does not contact a maintainer server.

## More pages

| You want to | Open |
| --- | --- |
| Sign in, or put HTTPS in front of the loopback console | [Console access](CONSOLE_AUTH.md) |
| Use a Grok Build subscription you already have | [Grok CLI](GROK_CLI.md) |
| Propose a docs or code change | [Contributing](https://github.com/seanventures/boss/blob/main/CONTRIBUTING.md) |
| See what Community includes | [Features](FEATURES.md) |
| Call BOSS from a trusted MCP client | [MCP](MCP.md) |
| See how the API, terminal, and MCP relate | [Control surfaces](CONTROL_SURFACES.md) |

`user@your-vm` in these guides means a VM you administer. No page requires Plane, an ops token, a private registry, or SSH to a machine you do not administer.

The optional [Goldeneye kit](https://github.com/seanventures/boss/blob/main/docs/product/GOLDENEYE_PAAS.md) is software you run locally. It is not access to a server operated by someone else.

## Names in the console

Mission Control is the console. Inside it:

- **Command Center** is where you start.
- **Approvals** is where a person decides.
- **Activity log** is where receipts and failures show up.
- **Agent work** groups recent activity by role.
- **Team structure** holds a planned role layout and the handoffs that happened. Moving a role does not change routing.
- **Agent Studio** holds definitions you can inspect and test. Installing a definition or an example workflow does not start work.

Examples that mention the project id `boss-dogfood` mean the project seeded on your machine, not a shared server.

## License

Apache License 2.0 applies to the Community source. See the repository `LICENSE`. Third-party components keep their own notices.
