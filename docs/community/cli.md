# CLI

Set up your shell to talk to a local BOSS install, manage the console password, and record coding CLI logins you already have. Everything on this page runs on the install machine.

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

    This sets `BOSS_API_URL` (the loopback API on this machine), `BOSS_API_KEY`, and `BOSS_PROJECT_ID`, without printing the key.

3. Check the API's health endpoint, using the key from the environment:

    ```bash
    curl -sS -H "Authorization: Bearer $BOSS_API_KEY" "$BOSS_API_URL/health"
    ```

!!! warning
    Keep the API key in the environment. Never pass it as a command argument, because the shell keeps a secret typed on the command line in its history.

## Manage the console password

The console password is separate from the API key. From the install directory, both commands read the private `.env`:

```bash
python3 scripts/install/console_password.py show
python3 scripts/install/console_password.py reset
```

- `show` prints the owner password, and only in a terminal.
- `reset` rotates the password without printing the new value, then prints the command that recreates the web service. Existing console sessions stop working.

For sign-in details, see [console access](CONSOLE_AUTH.md).

## Record coding CLI logins

If you have already signed in to Claude Code, Codex, or Grok with that provider's own CLI on this machine, pass the matching flag so the installer records the login:

```bash
./install.sh --dir "$PWD" --connect-claude-local
./install.sh --dir "$PWD" --connect-codex
./install.sh --dir "$PWD" --connect-grok-local
```

These flags only record an existing login. They do not sign you in. For the sign-in steps, see [model connections](MODEL_CONNECTIONS.md) and [Grok CLI](GROK_CLI.md).
