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

To install the official Claude Code, Codex CLI, and Grok Build tools too:

```sh
curl -fsSL https://raw.githubusercontent.com/seanventures/boss/main/install.sh | bash -s -- --install-coding-clis
```

Authenticate each provider on the VM under the runtime user after installation. An existing laptop subscription does not automatically authenticate the VM. The [installer guide](scripts/install/README.md) covers supported credential import, login checks, ports, and recovery.

## Use BOSS

Start in **Command Center** to submit a goal. Use **Approvals** for decisions and **Activity log** for receipts and failures. **Agents** shows observed work; **Org Chart** is where you plan reporting lines. Installing an agent template creates a definition, not a running employee or a device action.

The console, [terminal and API](docs/community/TERMINAL_API.md), and MCP client use the same server-side permissions and audit trail. You can inspect or automate a task through any of them.

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

Use the repository's bug, installation, or feature issue form. The [feedback process](docs/community/FEEDBACK_LOOP.md) explains how a report becomes a verified change. Report vulnerabilities privately to [boss@sean.net](mailto:boss@sean.net), following [SECURITY.md](SECURITY.md).

Community runs without a hosted BOSS account. Connect your own model, repository, and infrastructure credentials only for the work you choose to enable.
