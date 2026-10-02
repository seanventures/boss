# Interfaces

BOSS Community gives you four ways to work with a running install: the installer CLI that sets it up, the Mission Control console, the Metatron terminal, and the authenticated HTTP API, plus MCP for a single trusted client. This page explains what each one is for and how they fit together. It describes release `v1.0-rc4`, a release candidate.

| Interface | Use it to | Reference |
| --- | --- | --- |
| Installer CLI | Install, upgrade, and configure BOSS on your machine | [CLI setup](cli.md) |
| Console | Submit goals, review decisions, and read the activity log | [Console access](CONSOLE_AUTH.md) |
| Terminal | Query and call API operations interactively | [Terminal](terminal.md) |
| HTTP API | Script and integrate with BOSS | [Control surfaces](CONTROL_SURFACES.md) |
| MCP | Connect one trusted client to one project | [MCP access](MCP.md) |

The console and the terminal are both clients of the HTTP API. They are not separate control planes: the API enforces project, edition, permission, budget, approval, and receipt rules on every call, no matter which interface made it.

## Installer CLI

From a checkout, install in place:

```bash
./install.sh --dir "$PWD"
```

The installer binds the API and console to `127.0.0.1` and keeps credentials in a private `.env` on that machine. To publish the API port on another address, pass `--api-bind`; the default is `127.0.0.1`.

!!! warning "Secrets never go on the command line"
    The installer refuses API keys and tokens as arguments, because the shell would keep them in history and process listings. Use the hidden prompt or the process environment instead.

After install, source `boss-community.env.sh` from the install directory to load `BOSS_API_URL`, `BOSS_API_KEY`, and `BOSS_PROJECT_ID` into your shell without printing the key. See [CLI setup](cli.md) for details, and [model connections](MODEL_CONNECTIONS.md) and [Grok CLI](GROK_CLI.md) for coding CLIs you already have.

Without a checkout, `curl -fsSL https://boss.sean.network/install.sh | bash` installs the newest release into `~/boss`. Ports, recovery, and credential rules are covered in the [installer guide](https://github.com/seanventures/boss/blob/main/scripts/install/README.md).

## Console

On the install machine, open `http://127.0.0.1:3000`. From another computer, open an SSH tunnel to the VM you administer and use the same address locally:

```bash
ssh -L 3000:127.0.0.1:3000 -L 8000:127.0.0.1:8000 user@your-vm
```

The main areas of the console are:

- **Command Center**, where you submit a goal.
- **Approvals**, where you review decisions.
- **Activity log**, which shows receipts and failures.

Sign-in and the owner session are covered in [console access](CONSOLE_AUTH.md). To serve the console on a hostname, pass `--console-origin` and run a TLS proxy you operate; the listener itself stays on loopback. See [Console hostname](hostname.md) for the full setup.

## Terminal

From the install directory, open the Metatron terminal:

```bash
make metatron
```

This target runs `python scripts/metatron_tui.py` inside the API container with the install's configured API key and project, and never prints the key. Outside the container, run `python3 scripts/metatron_tui.py` after setting `BOSS_API_URL`, `BOSS_API_KEY`, and `BOSS_PROJECT_ID`.

The core commands are:

| Command | What it does |
| --- | --- |
| `/projects` | Lists the projects available to the current credentials and marks the active one. It does not switch projects. |
| `/ops` | Reads the operation catalog of the connected API. With no argument it lists product domains; add a domain or search term to filter, or use `/ops * --page N` to browse the full list. |
| `/describe <id>` | Shows an operation's method, path, parameters, and typed body fields, plus an example of the arguments `/op` expects. It does not call the operation. |
| `/op <id> [json]` | Sends the call through the authenticated HTTP API. The optional JSON accepts only the keys `path`, `query`, and `body`. |

!!! note
    A successful `/op` response means the API accepted the call. Errors and denials are not acceptance, and acceptance does not prove that later work finished. Check the resulting status, receipt, and event records before you treat anything as done.

For command behavior, see [terminal and API access](TERMINAL_API.md). For the operations the running API exposes, see the [terminal capability matrix](TERMINAL_CAPABILITY_MATRIX.md). The short setup page is [Terminal](terminal.md).

## HTTP API and MCP

The HTTP API is the single enforcement point for BOSS. The console and terminal use it, and you can call it directly with the API key. To reach it from another machine, see [remote API access](remote-api.md).

MCP is a separate, narrower surface for one trusted client on one project:

1. On an installed stack, run `make mcp-setup` to create a dedicated project credential. The credential is not printed.
2. Run `make mcp` to start that client's session.

Each credential is bound to a single project. To use another project, set up a separate client, credential, and process. The MCP client cannot supply its own URL, method, headers, or credentials.

!!! warning
    Remote MCP over HTTP requires `BOSS_MCP_HTTP_TOKEN`.

For how MCP differs from the HTTP API, see [MCP access](MCP.md) and [API, terminal, and MCP](CONTROL_SURFACES.md).
