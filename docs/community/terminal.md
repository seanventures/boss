# Terminal

Metatron is the terminal for an install you run. It runs inside the API container and talks to that install's HTTP API. It does not open a second control plane.

From the install directory:

```bash
make metatron
```

That opens the Metatron TUI inside the API container. It uses the install's API key and project. The key is not printed.

Outside that container, the same program is `python3 scripts/metatron_tui.py` after you set `BOSS_API_URL`, `BOSS_API_KEY`, and `BOSS_PROJECT_ID`. Do not put the API key on the command line.

The operation catalog is in [terminal and API access](TERMINAL_API.md) and the [terminal capability matrix](TERMINAL_CAPABILITY_MATRIX.md). This page does not copy that matrix.
