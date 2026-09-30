# Community interfaces

BOSS Community is software you run on a machine you administer. The installer
CLI starts that software on the machine. Afterward, the console and the
Metatron terminal both talk to the authenticated HTTP API. MCP is a separate
surface for one trusted client on one project. This repository has not been
published, and nothing on this page is a general-availability claim.

## Installer CLI

From a checkout you already have, install in place:

```bash
./install.sh --dir "$PWD"
```

The published one-liner still stops on
`REPLACE_WITH_REVIEWED_40_CHARACTER_SHA`. Leave that placeholder alone. Do not
invent a commit. Secrets are not command arguments: the installer refuses API
keys and tokens on the command line, because the shell would keep them in
history and process listings. Use the hidden prompt or the process environment
instead. The installer binds the API and console to `127.0.0.1` and leaves
credentials in the private `.env` on that machine. `--api-bind` is the explicit
way to publish the API port. The default is `127.0.0.1`.

Source `boss-community.env.sh` from the install directory for `BOSS_API_URL`,
`BOSS_API_KEY`, and `BOSS_PROJECT_ID`. The API key is not printed and is not a
command argument. That path is the [CLI setup](cli.md). Coding CLIs you already
have are in [model connections](MODEL_CONNECTIONS.md) and [Grok CLI](GROK_CLI.md).

Ports, recovery, and the credential rules are in the
[installer guide](https://github.com/seanventures/boss/blob/main/scripts/install/README.md).

## Console

After install, open `http://127.0.0.1:3000` on the install machine. From
another computer, open an SSH tunnel to a VM you administer, then use that
same address on your computer:

```bash
ssh -L 3000:127.0.0.1:3000 -L 8000:127.0.0.1:8000 user@your-vm
```

`user@your-vm` means a VM you administer, not a shared service. In the
console, **Command Center** is where you submit a goal, **Approvals** is
where you review decisions, and **Activity log** shows receipts and failures.
Sign-in and the owner session are in [console access](CONSOLE_AUTH.md). A
hostname needs a TLS proxy you run. The installer flag is `--console-origin`,
and the listener stays on loopback. The page for that proxy is not in this
manual yet.

## Terminal

On an installed Community stack, `make metatron` from the install directory
opens the Metatron TUI inside the API container. The short setup page is
[Terminal](terminal.md).

```bash
make metatron
```

That target runs `python scripts/metatron_tui.py` inside the API container
with the install's configured API key and project. The program never prints
the API key. Outside that container, the same entrypoint is
`python3 scripts/metatron_tui.py` after you set `BOSS_API_URL`,
`BOSS_API_KEY`, and `BOSS_PROJECT_ID`. Command behavior is in
[terminal and API access](TERMINAL_API.md). What the running API exposes is
in the [terminal capability matrix](TERMINAL_CAPABILITY_MATRIX.md).

`/projects` asks the API for the projects returned under the current
credentials and prints that list with the terminal's active project marked.
It does not select a different project.

`/ops` reads the catalog of the API you are connected to. With no argument it
lists product domains. Add a domain or a search term to list matching
operations, or use `/ops * --page N` to browse the full list in pages. Use it
to find an operation before you inspect or call one.

`/describe` takes one operation id from that list and shows its method, path,
parameters, and typed body fields, plus an example of the arguments `/op`
expects. It does not call the operation.

`/op` takes that operation id and optional JSON whose only keys are `path`,
`query`, and `body`, then sends the call through the authenticated HTTP API.
A successful response means the API accepted the call. An error or a denial
is not acceptance, and acceptance is not proof that later work finished.
Check the resulting status, receipt, and event records before you treat
anything as done.

## HTTP API and MCP

The console and the terminal use the authenticated HTTP API. They are not a
second control plane. The API enforces project, edition, permission, budget,
approval, and receipt rules on each call.

MCP is for a trusted client on one project. It is not the same surface as raw
HTTP. On an installed stack, `make mcp-setup` creates one dedicated project
credential, and `make mcp` starts that client's session. The credential is
not printed, and it is not reused for a second project: another project needs
its own client, credential, and process. The client does not supply its own
URL, method, headers, or credentials. Remote MCP HTTP requires
`BOSS_MCP_HTTP_TOKEN`. How this differs from the HTTP API is in
[MCP access](MCP.md) and [API, terminal, and MCP](CONTROL_SURFACES.md).
