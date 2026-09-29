# BOSS Community

BOSS is a self-hosted control plane for software work. Give it a goal; review its plan, decisions, code, tests, costs, and receipts in one place.

## Install

On a fresh Ubuntu or Debian VM, run:

```sh
curl -fsSL https://raw.githubusercontent.com/seanventures/boss/main/install.sh | bash
```

The installer sets up Docker and the Community services, creates local credentials, seeds a project, and checks that the API and console start. It binds both to `127.0.0.1`. From another computer, use an SSH tunnel:

```sh
ssh -L 3000:127.0.0.1:3000 -L 8000:127.0.0.1:8000 user@your-vm
```

Open `http://127.0.0.1:3000`. The installer prints the install location and a command to display the console password privately. Keep the generated `.env` private and backed up.

The API and worker images include the official Claude Code, Codex CLI, and Grok
Build tools. To install those CLIs for your VM user as well, so you can sign in
and import a supported subscription:

```sh
curl -fsSL https://raw.githubusercontent.com/seanventures/boss/main/install.sh | bash -s -- --install-coding-clis
```

Authenticate each provider in the account and container BOSS uses. An existing
laptop subscription does not automatically authenticate the VM. The [model
connection guide](docs/community/MODEL_CONNECTIONS.md) shows the supported
paths and setup limits. The [installer guide](scripts/install/README.md) covers
credential import, container login, ports, and recovery.

## Use BOSS

Start in **Command Center** to submit a goal. Use **Approvals** for decisions and **Activity log** for receipts and failures. **Agent work** shows recent activity by role. **Team structure** holds planned roles and actual handoffs; changing the plan does not route tasks. **Agent Studio** holds definitions you can test and run. Installing a template does not start work or control a device.

New Discovery web research, live debugging investigations, and BOSS-initiated SSH workload
planning and deployment are paused in Community while their spend, recovery, and effect
safeguards are completed. Existing Discovery dossiers, investigation results, and deployment
records remain readable. The SSH install and tunnel commands above still work; they do not
deploy an app through BOSS.

The console and [terminal](docs/community/TERMINAL_API.md) use the authenticated HTTP API. [MCP](docs/community/MCP.md) runs under a configured project with separate transport controls and is intended for trusted clients; its available actions differ from the HTTP API.

For a guided start, [five example workflows](examples/README.md) show agents
researching, preparing content, and handing external actions to a person for
approval. The included role templates are definitions; nothing starts running
when you install them.

## Keep it current

From the installed checkout:

```sh
./scripts/install/upgrade.sh --dir "$PWD"
```

Upgrade makes a private backup before replacing the running source. To stop BOSS while retaining its credentials and data:

```sh
./scripts/install/uninstall.sh --dir "$PWD" --yes
```

Data removal is a separate, explicitly confirmed option. See [upgrade, backup, and restore](scripts/install/README.md#upgrade--recover).

## Help improve it

Use the repository's bug, installation, or feature issue form. The [feedback process](docs/community/FEEDBACK_LOOP.md) explains how a report becomes a verified change. See [Contributing](CONTRIBUTING.md) for code and documentation changes and the [Code of Conduct](CODE_OF_CONDUCT.md) for community expectations. Report vulnerabilities privately to [boss@sean.net](mailto:boss@sean.net), following [SECURITY.md](SECURITY.md).

Community runs without a hosted BOSS account. Connect your own model, repository, and infrastructure credentials only for the work you choose to enable.
