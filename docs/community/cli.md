# CLI

Set up your shell to talk to a local BOSS install, use the `boss identity` commands, manage the old console password, and record coding CLI logins you already have. Everything on this page runs on the install machine.

## Install and load credentials

1. From a checkout, install in place:

    ```bash
    ./install.sh --dir "$PWD"
    ```

    The installer writes `boss-community.env.sh` to that directory with mode `0600`. The file stays on the machine.

2. Source it in your current shell:

    ```bash
    source ./boss-community.env.sh
    ```

    This sets `BOSS_API_URL` (the loopback API on this machine) and `BOSS_PROJECT_ID`. It also sets `BOSS_API_KEY` to the old shared key, which stops working 30 days after the install is claimed. Replace it with a key of your own:

    ```bash
    # the host tools' key (boss issues it into .env; boss identity host-key makes a new one)
    export BOSS_API_KEY="$(sed -n 's/^BOSS_HOST_API_KEY=//p' .env)"
    ```

    Or paste a personal key from **My account › API keys** (`boss_pat_…`). It acts as you, with your access.

3. Check the API's health endpoint, using the key from the environment:

    ```bash
    curl -sS -H "Authorization: Bearer $BOSS_API_KEY" "$BOSS_API_URL/health"
    ```

!!! warning
    Keep the API key in the environment. Never pass it as a command argument, because the shell keeps a secret typed on the command line in its history.

## Named accounts from the shell

`boss identity` runs the identity commands inside the api container, as the `boss` user:

| Command | What it does |
| --- | --- |
| `boss identity setup-link` | A link to claim this BOSS, valid once for one hour (only while it has no Owner). |
| `boss identity claim-status` | Claimed or not, and when the shared password stops. |
| `boss identity host-key` | A new key for the host tools, kept in `.env` as `BOSS_HOST_API_KEY`. Never printed. |
| `boss identity recover-owner --email E` | Lets a locked-out Owner back in (a 15-minute link). |
| `boss identity legacy status` | The shared credentials still in use, and their end dates. |

The full list is in [Identity administration](IDENTITY_ADMIN.md#host-shell-commands).

## Manage the console password

The shared console password claims the install, then signs teammates in as Operator for 30 days after the claim (see [Upgrade to named accounts](IDENTITY_UPGRADE.md)). From the install directory, both commands read the private `.env`:

```bash
python3 scripts/install/console_password.py show
python3 scripts/install/console_password.py reset
```

- `show` prints the password, and only in a terminal.
- `reset` rotates the password without printing the new value, then prints the command that recreates the web service. Existing console sessions stop working.

For sign-in details, see [console access](CONSOLE_AUTH.md).

## Sign a subscription in

A Codex, Claude Code or Grok Build subscription signs in inside BOSS through the provider's own login. Nothing on this machine is copied, and there is no token to paste:

```bash
boss connect codex     # or: boss connect claude, boss connect grok
```

BOSS prints the link and one-time code the provider's login shows. Open the link, sign in, and enter the code (Claude Code shows a code for you to paste back instead). BOSS then proves the connection with one test turn. When a login was already made inside the BOSS container, record it without signing in again:

```bash
./install.sh --dir "$PWD" --connect-claude-local
./install.sh --dir "$PWD" --connect-grok-local
```

These flags only record an existing login. For the steps and limits, see [model connections](MODEL_CONNECTIONS.md#sign-a-subscription-in-inside-boss) and [Grok CLI](GROK_CLI.md).
