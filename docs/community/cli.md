# CLI

From a checkout you already have, install in place. This does not contact a maintainer server:

```bash
./install.sh --dir "$PWD"
```

The installer writes `boss-community.env.sh` in that directory, mode `0600`. It is not uploaded. Source it in the shell you are using:

```bash
source ./boss-community.env.sh
```

That sets `BOSS_API_URL`, `BOSS_API_KEY`, and `BOSS_PROJECT_ID`. `BOSS_API_URL` is the loopback API on this machine. The API key is not printed. It is not a command argument. The shell would keep a secret typed on the command line.

Check the loopback health endpoint with the key from the environment. The command does not print the key:

```bash
curl -sS -H "Authorization: Bearer $BOSS_API_KEY" "$BOSS_API_URL/health"
```

## Console password

The console password is not the API key. From the install directory, `show` and `reset` read the private `.env`:

```bash
python3 scripts/install/console_password.py show
python3 scripts/install/console_password.py reset
```

`show` prints the owner password only in a terminal. `reset` rotates it, does not print the new value, and prints the command that recreates the web service. Existing console sessions stop working. Sign-in is in [console access](CONSOLE_AUTH.md).

## Coding CLIs you already have

These flags are operator setup. They do not sign you in, and they do not mean a Claude, Codex, or Grok login has already happened. Pass one when you want the installer to record a login you made with that provider's own CLI on this machine:

```bash
./install.sh --dir "$PWD" --connect-claude-local
./install.sh --dir "$PWD" --connect-codex
./install.sh --dir "$PWD" --connect-grok-local
```

The steps are in [model connections](MODEL_CONNECTIONS.md) and [Grok CLI](GROK_CLI.md).
