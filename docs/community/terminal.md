# Terminal

Metatron is the interactive terminal for your BOSS install. It runs inside the API container and calls the same authenticated HTTP API as the console, so every rule the API enforces applies here too.

## Open the terminal

From the install directory:

```bash
make metatron
```

This opens the Metatron TUI inside the API container, using the host tools' key (`BOSS_HOST_API_KEY`, issued for you if missing) and the install's project. The key is never printed.

## Run it outside the container

Set `BOSS_API_URL` and `BOSS_PROJECT_ID` (sourcing `boss-community.env.sh` does), and put your own key in `BOSS_API_KEY`: a personal key from **My account › API keys**, or on the install machine `export BOSS_API_KEY="$(sed -n 's/^BOSS_HOST_API_KEY=//p' .env)"`. Then run `python3 scripts/metatron_tui.py`.

!!! warning
    Load the API key from the environment. Never pass it on the command line.

## Find an operation without knowing a route

`/api` lists the API's capability domains — on a rich terminal it opens a picker for a domain and then an operation. `/api DOMAIN` (or `/api *`) lists the operations in a numbered list, and `/api NUMBER` shows that operation's path template with ready-to-edit examples. `/api run NUMBER [JSON]` then sends it.

## Learn the commands

- [Terminal and API access](TERMINAL_API.md) is the command reference.
- The [terminal capability matrix](TERMINAL_CAPABILITY_MATRIX.md) lists the operations the running API exposes.
- [Ask the terminal](walkthroughs.md#ask-the-terminal) walks through a first session.
