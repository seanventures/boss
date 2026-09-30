# Terminal

Metatron is the interactive terminal for your BOSS install. It runs inside the API container and calls the same authenticated HTTP API as the console, so every rule the API enforces applies here too.

## Open the terminal

From the install directory:

```bash
make metatron
```

This opens the Metatron TUI inside the API container, using the install's API key and project. The key is never printed.

## Run it outside the container

Set `BOSS_API_URL`, `BOSS_API_KEY`, and `BOSS_PROJECT_ID` in your environment, for example by sourcing `boss-community.env.sh`, then run `python3 scripts/metatron_tui.py`.

!!! warning
    Load the API key from the environment. Never pass it on the command line.

## Learn the commands

- [Terminal and API access](TERMINAL_API.md) is the command reference.
- The [terminal capability matrix](TERMINAL_CAPABILITY_MATRIX.md) lists the operations the running API exposes.
- [Ask the terminal](walkthroughs.md#ask-the-terminal) walks through a first session.
