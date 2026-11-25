"""THE `boss` COMMAND OF THIS INSTALL, as the console should print it.

A second install on one machine has its own `boss` launcher outside the default places
(BOSS_BIN_DIR, saved in that install's .env and passed to the api container). There, plain
`boss` runs the FIRST install, so the claim page saying `boss identity setup-link` would have
claimed the other BOSS (stranger test r2, 2026-11-17). The console names the launcher's full
path instead; on a default install it stays plain `boss`.
"""

from __future__ import annotations

import os
import re

# The places install.sh puts `boss` by default: on PATH, so plain `boss` is this install.
_DEFAULT_DIRS = ("/usr/local/bin",)
_SAFE_PATH = re.compile(r"^[A-Za-z0-9._/~+-]+$")


def host_boss_command(bin_dir: str | None = None) -> str:
    """`boss`, or `<BOSS_BIN_DIR>/boss` when this install's command lives somewhere else."""
    raw = (os.environ.get("BOSS_BIN_DIR", "") if bin_dir is None else bin_dir).strip().rstrip("/")
    # ~/.local/bin is the default too; the container does not know the host HOME, so any
    # /home-like/<user>/.local/bin counts.
    if not raw or raw in _DEFAULT_DIRS or (raw.endswith("/.local/bin") and raw.count("/") <= 4):
        return "boss"
    # Shown in a copyable command: anything a shell would read differently is not printed.
    if not _SAFE_PATH.match(raw) or not raw.startswith(("/", "~")):
        return "boss"
    return f"{raw}/boss"
